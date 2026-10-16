"""Checks the per-lane context use, diffstat and location readings."""

import datetime
import json
import os
import socket
from pathlib import Path

import pytest

from agent_parley import (
    cli,
    context,
    dashboard,
    records,
    supervision,
    views,
    webview,
)
from agent_parley.cli import git
from agent_parley.state import BridgeError

AUTHOR = (
    "-c",
    "user.name=Bridge Test",
    "-c",
    "user.email=test@example.com",
)


def rollout(lane, *infos, session="context"):
    """Writes a Codex rollout naming the lane, one token count per info."""
    today = datetime.date.today()
    directory = (
        Path(os.environ["CODEX_HOME"])
        / "sessions"
        / f"{today:%Y}"
        / f"{today:%m}"
        / f"{today:%d}"
    )
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"rollout-{session}.jsonl"
    entries = [{"payload": {"cwd": str(lane)}}] + [
        {
            "type": "event_msg",
            "payload": {"type": "token_count", "info": info},
        }
        for info in infos
    ]
    path.write_text("".join(json.dumps(entry) + "\n" for entry in entries))
    return path


def counted(tokens, window):
    """Builds the info of one Codex token count event."""
    return {
        "total_token_usage": {"total_tokens": tokens * 3},
        "last_token_usage": {"total_tokens": tokens},
        "model_context_window": window,
    }


def lane_entry(provider, lane):
    """Builds the manifest entry a reader takes for one lane."""
    return {"provider": provider, "lane": str(lane)}


def test_codex_reports_the_latest_stated_context_window_use(bridge, tmp_path):
    lane = tmp_path / "lane"
    rollout(lane, counted(1000, 10000), counted(68000, 272000))
    use = context.lane_context_use(bridge.home, lane_entry("codex", lane))
    assert use == pytest.approx(25.0)
    assert context.reports_context(bridge.home, "codex")


def test_a_record_without_a_window_size_is_unknown_not_guessed(
    bridge, tmp_path
):
    lane = tmp_path / "lane"
    info = counted(5000, 0)
    del info["model_context_window"]
    rollout(lane, info)
    entry = lane_entry("codex", lane)
    assert context.lane_context_use(bridge.home, entry) is None
    rollout(lane, counted(5000, 0))
    assert context.lane_context_use(bridge.home, entry) is None


def test_providers_without_a_stated_window_read_as_unknown(bridge, tmp_path):
    lane = tmp_path / "lane"
    transcript = (
        Path(os.environ["CLAUDE_CONFIG_DIR"])
        / "projects"
        / records.UNSAFE.sub("-", str(lane))
        / "session.jsonl"
    )
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        json.dumps({"message": {"id": "m", "usage": {"input_tokens": 190000}}})
        + "\n"
    )
    for provider in ("claude", "gemini", "copilot", "opencode", "amp"):
        entry = lane_entry(provider, lane)
        assert context.lane_context_use(bridge.home, entry) is None
        assert not context.reports_context(bridge.home, provider)
    assert not context.reports_context(bridge.home, "undefined")
    assert context.lane_context_use(bridge.home, {}) is None


def test_only_a_known_use_at_or_above_the_threshold_is_full():
    threshold = supervision.DEFAULTS["context_full_percent"]
    assert threshold == 80
    assert context.context_full(80.0, threshold)
    assert context.context_full(99.5, threshold)
    assert not context.context_full(79.9, threshold)
    assert not context.context_full(None, threshold)


@pytest.mark.parametrize("value", [0, -5, 100.5, "80", True])
def test_the_context_threshold_is_validated(value):
    with pytest.raises(BridgeError, match="context_full_percent"):
        supervision.settings({"context_full_percent": value})


def test_a_valid_context_threshold_is_kept():
    assert (
        supervision.settings({"context_full_percent": 92.5})[
            "context_full_percent"
        ]
        == 92.5
    )


def test_a_lane_location_names_the_host_and_worktree(tmp_path):
    assert context.lane_location(lane_entry("codex", tmp_path)) == {
        "host": socket.gethostname(),
        "worktree": str(tmp_path),
    }


def test_the_diffstat_counts_commits_and_edits_since_the_fork(repo, paired):
    lane = Path(paired["lanes"]["claude"])
    (lane / "new.txt").write_text("one\ntwo\n")
    git(lane, "add", "new.txt")
    git(lane, *AUTHOR, "commit", "-m", "add new")
    (lane / "shared.txt").write_text("changed\nmore\n")
    (lane / "untracked.txt").write_text("ignored\n")
    (repo / "base.txt").write_text("base moved\n")
    git(repo, "add", "base.txt")
    git(repo, *AUTHOR, "commit", "-m", "advance base")
    stats = context.lane_diffstats(paired)
    assert stats["codex"] == {"files": [], "added": 0, "deleted": 0}
    claude = stats["claude"]
    assert claude["files"] == [
        {"path": "new.txt", "added": 2, "deleted": 0},
        {"path": "shared.txt", "added": 2, "deleted": 1},
    ]
    assert (claude["added"], claude["deleted"]) == (4, 1)


def test_a_binary_change_has_no_line_counts(repo, paired):
    lane = Path(paired["lanes"]["codex"])
    (lane / "blob.bin").write_bytes(b"\x00\x01\x02")
    git(lane, "add", "blob.bin")
    stats = context.lane_diffstats(paired)["codex"]
    assert stats == {
        "files": [{"path": "blob.bin", "added": None, "deleted": None}],
        "added": 0,
        "deleted": 0,
    }


def test_an_unreadable_base_or_lane_reads_as_unknown(tmp_path, paired):
    gone = {**paired, "root": str(tmp_path / "missing")}
    assert context.lane_diffstats(gone) == {"claude": None, "codex": None}
    lane = lane_entry("codex", tmp_path / "missing")
    assert context.lane_diffstat(lane, "HEAD") is None


def edited_lane(paired, name="claude"):
    """Adds one committed file to a lane and returns its worktree."""
    lane = Path(paired["lanes"][name])
    (lane / "new.txt").write_text("one\ntwo\n")
    git(lane, "add", "new.txt")
    git(lane, *AUTHOR, "commit", "-m", "add new")
    return lane


def test_cells_mark_an_unknown_reading_and_a_full_window():
    unknown = {"context_percent": None, "diff_files": None}
    assert context.window_cell(unknown) == "unknown"
    assert context.diff_cell(unknown) == "-"
    assert context.location_cell({}) == "-"
    full = {"context_percent": 85.4, "context_full": True}
    assert context.window_cell(full) == "85%!"
    assert context.window_cell({**full, "context_full": False}) == "85%"
    stat = {"diff_files": 2, "diff_added": 4, "diff_deleted": 1}
    assert context.diff_cell(stat) == "+4/-1 2f"
    assert context.location_cell({"host": "h", "worktree": "/w"}) == "h:/w"


def test_status_reports_the_fields_for_json_and_lane_detail(
    bridge, paired, capsys
):
    lane = edited_lane(paired)
    rollout(Path(paired["lanes"]["codex"]), counted(90000, 100000))
    frame = bridge.status_snapshot()
    records = {
        record["participant"]: record
        for record in frame["projects"][0]["participants"]
    }
    claude, codex = records["claude"], records["codex"]
    assert claude["context_percent"] is None
    assert claude["context_full"] is False
    assert claude["diff_files"] == 1
    assert claude["diff_paths"] == ["new.txt"]
    assert (claude["diff_added"], claude["diff_deleted"]) == (2, 0)
    assert claude["worktree"] == str(lane)
    assert claude["host"] == socket.gethostname()
    assert codex["context_percent"] == pytest.approx(90.0)
    assert codex["context_full"] is True
    cli.lane_detail(claude, paired)
    shown = capsys.readouterr().out
    assert "Context window: unknown; diff +2/-0 1f" in shown
    assert "Changed files: new.txt" in shown


def test_top_and_the_web_view_show_the_fields_from_one_diff_pass(
    bridge, paired, monkeypatch
):
    edited_lane(paired)
    rollout(Path(paired["lanes"]["codex"]), counted(90000, 100000))
    passes = []
    original = context.lane_diffstats
    monkeypatch.setattr(
        dashboard,
        "lane_diffstats",
        lambda manifest: passes.append(manifest["root"]) or original(manifest),
    )
    view = dashboard.collect(bridge.home, False, {})
    assert len(passes) == 1
    rows = {row["participant"]: row for row in view["projects"][0]["rows"]}
    names = [name for name, _ in dashboard.COLUMNS]
    claude = dict(zip(names, dashboard.row_cells(rows["claude"]), strict=True))
    codex = dict(zip(names, dashboard.row_cells(rows["codex"]), strict=True))
    assert claude["WINDOW"] == "unknown"
    assert claude["DIFF"] == "+2/-0 1f"
    assert claude["LOCATION"].endswith(str(rows["claude"]["worktree"]))
    assert codex["WINDOW"] == "90%!"
    assert {"WINDOW", "DIFF"} <= set(dashboard.DEFAULT_COLUMNS)
    page = webview.page(view, 5)
    assert "<th>WINDOW</th>" in page
    assert "<td>90%!</td>" in page
    live = views.frame(view)["projects"][0]["participants"]
    by_name = {item["participant"]: item for item in live}
    assert by_name["claude"]["context_percent"] is None
    assert by_name["claude"]["diff_paths"] == ["new.txt"]
    assert by_name["codex"]["context_full"] is True
