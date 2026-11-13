"""Checks the token readers for Gemini CLI, Copilot CLI and OpenCode lanes.

Each fixture reproduces the native client's own storage layout: Gemini CLI
0.64 chat logs, Copilot CLI session-state event logs, and the OpenCode 1.19
session table.
"""

import contextlib
import json
import math
import os
import sqlite3
import time
from pathlib import Path

from agent_parley import budgets, records, roster


def lines(path, entries):
    """Writes JSON lines to a session log, creating its directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(entry) + "\n" for entry in entries))
    return path


def gemini_log(lane, entries, name="session-1.jsonl", slug="lane"):
    """Writes a Gemini CLI chat log and registers the lane's project."""
    root = Path(records.DEFAULT_HOMES["gemini"]) / ".gemini"
    root.mkdir(parents=True, exist_ok=True)
    (root / "projects.json").write_text(
        json.dumps({"projects": {str(lane): slug}})
    )
    metadata = {"sessionId": name, "projectHash": "x", "messages": []}
    return lines(root / "tmp" / slug / "chats" / name, [metadata, *entries])


def gemini_message(identifier, total=None):
    """Builds a Gemini CLI message record, with tokens when given."""
    entry = {"id": identifier, "type": "gemini", "content": "ok"}
    if total is not None:
        entry["tokens"] = {"input": 1, "output": 1, "total": total}
    return entry


def copilot_log(lane, session, entries):
    """Writes a Copilot CLI event log whose start names the lane."""
    start = {
        "type": "session.start",
        "data": {"sessionId": session, "context": {"cwd": str(lane)}},
    }
    path = (
        Path(records.DEFAULT_HOMES["copilot"])
        / "session-state"
        / session
        / "events.jsonl"
    )
    return lines(path, [start, *entries])


def shutdown(**models):
    """Builds a Copilot CLI shutdown event with per-model usage."""
    return {
        "type": "session.shutdown",
        "data": {
            "modelMetrics": {
                model: {"requests": {"count": 1}, "usage": usage}
                for model, usage in models.items()
            }
        },
    }


def opencode_db(rows, columns=True):
    """Writes an OpenCode database holding the given session rows."""
    path = Path(os.environ["XDG_DATA_HOME"]) / "opencode" / "opencode.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    counts = (
        ", tokens_input INTEGER, tokens_output INTEGER,"
        " tokens_reasoning INTEGER, tokens_cache_read INTEGER,"
        " tokens_cache_write INTEGER"
        if columns
        else ""
    )
    with contextlib.closing(sqlite3.connect(path)) as connection:
        connection.execute("DROP TABLE IF EXISTS session")
        connection.execute(
            "CREATE TABLE session (id TEXT PRIMARY KEY, parent_id TEXT,"
            f" directory TEXT, time_updated INTEGER{counts})"
        )
        for row in rows:
            connection.execute(
                "INSERT INTO session VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                row,
            )
        connection.commit()
    return path


def lane_entry(provider, lane):
    """Builds the manifest entry a reader takes for one lane."""
    return {"provider": provider, "lane": str(lane)}


def test_every_built_in_provider_but_amp_has_a_token_reader(bridge):
    assert records.readers() == [
        "claude",
        "codex",
        "copilot",
        "gemini",
        "opencode",
    ]
    assert not records.measures_tokens(bridge.home, "amp")
    assert (
        records.reported_tokens(bridge.home, lane_entry("amp", "/lane"), {})
        is None
    )


def test_gemini_counts_each_message_once_from_its_first_token_copy(
    bridge, tmp_path
):
    lane = tmp_path / "lane"
    gemini_log(
        lane,
        [
            {"id": "u1", "type": "user", "content": "the tokens word"},
            gemini_message("g1"),
            gemini_message("g1", 120),
            gemini_message("g1", 120),
            gemini_message("g2", 30),
            {"$rewindTo": "g2"},
        ],
    )
    participant = lane_entry("gemini", lane)
    assert records.reported_tokens(bridge.home, participant, {}) == 150
    sources = records.lane_sources(bridge.home, participant, {}, 0.0)
    assert [source["tokens"] for source in sources] == [150]
    assert sources[0]["last"] == "g2"


def test_gemini_reads_subagent_logs_and_skips_unregistered_lanes(
    bridge, tmp_path
):
    lane = tmp_path / "lane"
    main = gemini_log(lane, [gemini_message("g1", 40)])
    lines(
        main.parent / "parent-1" / "sub.jsonl",
        [{"sessionId": "sub"}, gemini_message("s1", 5)],
    )
    participant = lane_entry("gemini", lane)
    sources = records.lane_sources(bridge.home, participant, {}, 0.0)
    assert sorted(source["tokens"] for source in sources) == [5, 40]
    other = lane_entry("gemini", tmp_path / "other")
    assert records.reported_tokens(bridge.home, other, {}) is None
    gemini_log(lane, [gemini_message("g1", 40)], slug="../escape")
    assert records.reported_tokens(bridge.home, participant, {}) is None


def test_copilot_takes_the_latest_shutdown_totals_of_the_lane_session(
    bridge, tmp_path
):
    lane = tmp_path / "lane"
    participant = lane_entry("copilot", lane)
    path = copilot_log(
        lane,
        "s1",
        [{"type": "assistant.message", "data": {"outputTokens": 9}}],
    )
    assert records.reported_tokens(bridge.home, participant, {}) == 0
    first = shutdown(
        a={"inputTokens": 100, "outputTokens": 20, "cacheReadTokens": 50},
        b={"inputTokens": 10, "outputTokens": 2},
    )
    resumed = shutdown(a={"inputTokens": 170, "outputTokens": 30})
    with path.open("a") as handle:
        handle.write(json.dumps(first) + "\n")
    assert records.reported_tokens(bridge.home, participant, {}) == 132
    with path.open("a") as handle:
        handle.write(json.dumps({"type": "session.resume", "data": {}}) + "\n")
        handle.write(json.dumps(resumed) + "\n")
    assert records.reported_tokens(bridge.home, participant, {}) == 200
    copilot_log(tmp_path / "other", "s2", [shutdown(a={"inputTokens": 7})])
    sources = records.lane_sources(bridge.home, participant, {}, 0.0)
    assert [source["tokens"] for source in sources] == [200]


def test_opencode_reads_the_newest_session_with_its_subagents(bridge, tmp_path):
    lane = tmp_path / "lane"
    participant = lane_entry("opencode", lane)
    assert records.reported_tokens(bridge.home, participant, {}) is None
    opencode_db(
        [
            ("old", None, str(lane), 1000, 7, 0, 0, 0, 0),
            ("new", None, str(lane), 9000, 10, 20, 5, 3, 2),
            ("sub", "new", str(lane), 5000, 4, 6, 0, 0, 0),
            ("far", None, str(tmp_path / "other"), 9999, 900, 0, 0, 0, 0),
        ]
    )
    assert records.reported_tokens(bridge.home, participant, {}) == 50
    sources = records.lane_sources(bridge.home, participant, {}, 0.0)
    assert {source["key"]: source["tokens"] for source in sources} == {
        "session:old": 7,
        "session:new": 50,
    }


def test_opencode_counts_only_growth_after_counting_began(bridge, tmp_path):
    lane = tmp_path / "lane"
    participant = lane_entry("opencode", lane)
    opencode_db([("s1", None, str(lane), 1000, 100, 0, 0, 0, 0)])
    begun = records.lane_sources(bridge.home, participant, {}, math.inf)
    assert begun == [
        {"key": "session:s1", "offset": 100, "tokens": 0, "last": ""}
    ]
    opencode_db(
        [
            ("s1", None, str(lane), 2000, 130, 0, 0, 0, 0),
            ("s2", None, str(lane), 3000, 8, 0, 0, 0, 0),
        ]
    )
    known = {"session:s1": {"offset": 100, "tokens": 0, "last": ""}}
    later = records.lane_sources(bridge.home, participant, known, 2.5)
    assert {source["key"]: source["tokens"] for source in later} == {
        "session:s1": 30,
        "session:s2": 8,
    }


def test_an_opencode_database_without_token_columns_reads_as_no_record(
    bridge, tmp_path
):
    lane = tmp_path / "lane"
    path = opencode_db([], columns=False)
    with contextlib.closing(sqlite3.connect(path)) as connection:
        connection.execute(
            "INSERT INTO session VALUES (?, ?, ?, ?)",
            ("s1", None, str(lane), 1000),
        )
        connection.commit()
    participant = lane_entry("opencode", lane)
    assert records.reported_tokens(bridge.home, participant, {}) is None
    assert records.lane_sources(bridge.home, participant, {}, 0.0) is None


def test_an_enforced_token_limit_counts_gemini_and_opencode_lanes(
    bridge, repo, paired
):
    lanes = bridge.add_participant(repo, "gem", "gemini")["lanes"]
    lanes = bridge.add_participant(repo, "code", "opencode")["lanes"]
    bridge.enforce(repo, {"tokens": 1_000})
    directory = bridge.project(repo, create=False)[1]
    manifest = roster.read(directory)
    assert records.unmeasured(bridge.home, manifest) == []
    log = gemini_log(lanes["gem"], [gemini_message("g1", 300)])
    os.utime(log, (time.time() + 5, time.time() + 5))
    opencode_db(
        [
            (
                "s1",
                None,
                lanes["code"],
                int((time.time() + 5) * 1000),
                400,
                0,
                0,
                0,
                0,
            )
        ]
    )
    reading = budgets.account(bridge.home, directory, manifest)
    assert reading["used"]["tokens"] == 700
