"""Checks every lane throughput counter against a temporary repository."""

import datetime
import json
import sys
from pathlib import Path

import pytest

from agent_parley import cli, decisions, lanes, metrics, records, store
from agent_parley.state import write_json

HOUR = 3600.0


def iso(moment):
    """Formats a Unix time the way native clients stamp their records."""
    return (
        datetime.datetime.fromtimestamp(moment, datetime.UTC)
        .isoformat()
        .replace("+00:00", "Z")
    )


def lines(path, entries):
    """Writes JSON lines to a native session record."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(entry) + "\n" for entry in entries))
    return path


def claude_message(identifier, moment, entered, produced):
    """Builds one Claude transcript record carrying usage."""
    return {
        "timestamp": iso(moment),
        "message": {
            "id": identifier,
            "usage": {
                "input_tokens": entered,
                "cache_read_input_tokens": 1,
                "output_tokens": produced,
            },
        },
    }


def resolved(holder, number, merged):
    """Builds one ledger record whose claim a merged pull request ended."""
    return {
        "owner": None,
        "history": [
            {"action": "claim", "at": merged - 600, "owner": holder},
            {
                "action": "resolve",
                "at": merged,
                "outcome": "complete",
                "holder": holder,
                "evidence": {
                    "state": "MERGED",
                    "closed_at": merged,
                    "pull_request": number + 100,
                },
            },
        ],
    }


def run(monkeypatch, capsys, *arguments):
    """Runs one CLI invocation and returns its standard output."""
    monkeypatch.setattr(sys, "argv", ["agent-parley", *arguments])
    assert cli.main() == 0
    return capsys.readouterr().out


@pytest.fixture
def window(bridge, repo, paired):
    """Records one window of lane activity and returns its bounds."""
    directory = bridge.project(repo)[1]
    store.initialize(bridge.home)
    for name in ("claude", "codex"):
        store.register(bridge.home, paired["root"], name)
    now = metrics.time.time()
    since = now - HOUR
    steps = (
        (now - 7200, lanes.WORKING, ""),
        (now - 3240, lanes.IDLE, ""),
        (now - 2880, lanes.BLOCKED, lanes.APPROVAL),
        (now - 2520, lanes.WORKING, ""),
        (now - 2160, lanes.BLOCKED, lanes.CAPACITY),
        (now - 1800, lanes.STOPPED, ""),
        (now - 1440, lanes.STARTING, ""),
        (now - 1080, lanes.WORKING, ""),
    )
    with store.connect(bridge.home, write=True) as db:
        for moment, state, cause in steps:
            lanes.transition(
                db, paired["root"], "claude", state, cause=cause, now=moment
            )
        lanes.transition(
            db, paired["root"], "codex", lanes.WORKING, now=now - 1800
        )
    write_json(
        directory / "issues.json",
        {
            "revision": 1,
            "issues": {
                "41": resolved("claude", 41, now - 7200),
                "42": resolved("claude", 42, now - 1200),
                "43": resolved("codex", 43, now - 600),
            },
        },
    )
    lane = paired["lanes"]["claude"]
    lines(
        Path(records.DEFAULT_HOMES["claude"])
        / "projects"
        / records.UNSAFE.sub("-", str(lane))
        / "session.jsonl",
        [
            {"type": "user", "timestamp": iso(now - 1000)},
            claude_message("m0", now - 7200, 1000, 1000),
            claude_message("m1", now - 1000, 100, 10),
            claude_message("m1", now - 990, 100, 10),
            claude_message("m2", now - 500, 200, 20),
        ],
    )
    metrics.record_report(
        directory, "claude", {"kind": "approval", "decision": "approve"}
    )
    record = decisions.open_or_refresh(
        directory,
        project=paired["root"],
        lane="claude",
        kind="prompt",
        key="k",
        question="Allow?",
        options=["yes", "no"],
        now=now - 900,
    )
    decisions.answer(directory, record["id"], "yes", "operator", now=now - 800)
    bridge.say(repo, "claude", "First")
    bridge.say(repo, "claude", "Second")
    bridge.pause(repo, "claude")
    bridge.pause(repo, "claude", resume=True)
    bridge.report(lane, "ready", "Engine built", "", "12 tests passed")
    return {"since": since, "now": now, "root": paired["root"]}


def test_lane_hours_follow_the_recorded_transitions(bridge, window):
    with store.connect(bridge.home) as db:
        reading = metrics.lane_hours(
            db, window["root"], "claude", window["since"], window["now"]
        )
        codex = metrics.lane_hours(
            db, window["root"], "codex", window["since"], window["now"]
        )
        missing = metrics.lane_hours(
            db, window["root"], "nobody", window["since"], window["now"]
        )
    assert reading["seconds"] == pytest.approx(
        {
            "active": 2160.0,
            "idle": 360.0,
            "operator_wait": 360.0,
            "blocked": 360.0,
        }
    )
    assert reading["starts"] == 1
    assert reading["complete"] is True
    assert codex["seconds"]["active"] == pytest.approx(1800.0)
    assert codex["complete"] is True
    assert missing["complete"] is False
    assert (
        metrics.lane_hours(None, window["root"], "claude", 0.0, 1.0)["complete"]
        is False
    )


def test_history_that_starts_inside_the_window_is_incomplete(bridge, window):
    with store.connect(bridge.home, write=True) as db:
        db.execute(
            "DELETE FROM lane_events WHERE lane='claude' AND ts<?",
            (window["since"],),
        )
    with store.connect(bridge.home) as db:
        reading = metrics.lane_hours(
            db, window["root"], "claude", window["since"], window["now"]
        )
    assert reading["complete"] is False
    assert reading["seconds"]["active"] == pytest.approx(1800.0)


def test_every_counter_is_reported_per_lane_and_project(bridge, window):
    reading = metrics.throughput(bridge.home, window["since"])
    project = reading["projects"][0]
    claude = project["lanes"]["claude"]
    assert claude["merged_issues"] == 1
    assert claude["active_hours"] == 0.6
    assert claude["idle_hours"] == 0.1
    assert claude["operator_wait_hours"] == 0.1
    assert claude["blocked_hours"] == 0.1
    assert claude["input_tokens"] == 302
    assert claude["output_tokens"] == 30
    assert claude["approvals"] == 1
    assert claude["decisions"] == 1
    assert claude["operator_mail"] == 2
    assert claude["resumes"] == 1
    assert claude["operator_touches"] == 5
    assert claude["unattributed_starts"] == 1
    assert claude["merge_waits"] == 1
    assert claude["merge_waits_pending"] == 1
    codex = project["lanes"]["codex"]
    assert codex["merged_issues"] == 1
    assert codex["input_tokens"] == 0
    totals = project["totals"]
    assert totals["merged_issues"] == 2
    assert totals["operator_mail"] == 2
    assert totals["operator_touches"] == 5
    assert totals["input_tokens"] == 302
    assert totals["history_complete"] is True


def test_the_merge_queue_wait_ends_at_integration(bridge, repo, window):
    directory = bridge.project(repo)[1]
    metrics.record_report(
        directory, "claude", {"kind": "integration", "action": "merge"}
    )
    claude = metrics.throughput(bridge.home, window["since"])["projects"][0][
        "lanes"
    ]["claude"]
    assert claude["merge_waits"] == 1
    assert claude["merge_waits_pending"] == 0


def test_running_totals_count_only_the_window(tmp_path):
    codex = lines(
        tmp_path / "rollout.jsonl",
        [
            {
                "timestamp": iso(50),
                "payload": {
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 100,
                            "output_tokens": 10,
                        }
                    }
                },
            },
            {
                "timestamp": iso(150),
                "payload": {
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 400,
                            "output_tokens": 40,
                        }
                    }
                },
            },
            {
                "timestamp": iso(300),
                "payload": {
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 900,
                            "output_tokens": 90,
                        }
                    }
                },
            },
        ],
    )
    assert metrics._file_tokens(codex, "codex", 100.0, 200.0) == (300, 30)
    copilot = lines(
        tmp_path / "events.jsonl",
        [
            {
                "type": "session.shutdown",
                "timestamp": iso(150),
                "data": {
                    "modelMetrics": {
                        "a": {"usage": {"inputTokens": 7, "outputTokens": 3}},
                        "b": {"usage": {"inputTokens": 1, "outputTokens": 1}},
                    }
                },
            }
        ],
    )
    assert metrics._file_tokens(copilot, "copilot", 100.0, 200.0) == (8, 4)
    gemini = lines(
        tmp_path / "chat.jsonl",
        [
            {"id": "g1", "type": "gemini", "timestamp": iso(150)},
            {
                "id": "g1",
                "type": "gemini",
                "timestamp": iso(150),
                "tokens": {"input": 5, "output": 2, "total": 7},
            },
            {
                "id": "g1",
                "type": "gemini",
                "timestamp": iso(160),
                "tokens": {"input": 5, "output": 2, "total": 7},
            },
            {
                "id": "g2",
                "type": "gemini",
                "tokens": {"input": 9, "output": 9, "total": 18},
            },
        ],
    )
    assert metrics._file_tokens(gemini, "gemini", 100.0, 200.0) == (5, 2)


def test_a_provider_without_a_split_reader_reports_no_tokens(bridge):
    lane = {"provider": "opencode", "lane": "/lane"}
    assert metrics.native_tokens(bridge.home, lane, 0.0, 1.0) is None
    assert metrics.native_tokens(bridge.home, {"provider": "amp"}, 0, 1) is None


def test_the_command_prints_text_and_json(bridge, window, monkeypatch, capsys):
    home = ["--home", str(bridge.home)]
    start = iso(window["since"])
    printed = run(
        monkeypatch, capsys, *home, "metrics", "throughput", "--since", start
    )
    assert "MERGED" in printed and "claude" in printed
    assert "1 approvals, 1 decisions, 2 mail, 1 resumes" in printed
    assert "1 lane starts unattributed" in printed
    document = json.loads(
        run(
            monkeypatch,
            capsys,
            *home,
            "metrics",
            "throughput",
            "--since",
            "2h",
            "--json",
        )
    )
    claude = document["projects"][0]["lanes"]["claude"]
    assert claude["merged_issues"] == 1
    assert claude["input_tokens"] == 302


def test_a_quiet_window_prints_no_table(bridge, paired):
    reading = metrics.throughput(bridge.home, metrics.time.time())
    printed = metrics.describe_throughput(reading)
    assert "MERGED" not in printed
    assert "1 projects recorded nothing in the window." in printed


def test_a_window_start_is_an_instant_or_a_window():
    assert cli.instant("2026-10-15T09:01:00Z") == 1792054860.0
    assert cli.instant("1h") == pytest.approx(metrics.time.time() - HOUR, 5)
    with pytest.raises(ValueError):
        cli.instant("yesterday")
