"""Checks the advisory notice for an operator edit on a reserved path."""

import itertools
import json
import subprocess
import sys
from pathlib import Path

from agent_parley import checkpoints, cli, dashboard, store, supervision
from agent_parley.state import write_json


def reserve(bridge, root, name, *keys):
    """Reserves keys as one registered lane through the served tool path."""
    store.initialize(bridge.home)
    token = store.register(bridge.home, root, name)["registration_token"]
    holder = store.authenticate(bridge.home, token)
    assert holder is not None
    return store.call(
        bridge.home, holder, "file_reservation_paths", {"paths": list(keys)}
    )


def top_json(bridge, monkeypatch, capsys, *flags):
    """Runs one JSON frame of the live view and reads its lane records."""
    monkeypatch.setattr(
        sys,
        "argv",
        ["agent-parley", "--home", str(bridge.home), "top", "--json", *flags],
    )
    assert cli.main() == 0
    document = json.loads(capsys.readouterr().out)
    return {
        row["participant"]: row
        for row in document["projects"][0]["participants"]
    }


def test_a_dirty_reserved_path_is_reported_for_the_reserving_lane_only(
    bridge, repo, paired, monkeypatch, capsys
):
    reserve(bridge, paired["root"], "claude", "shared.txt")
    reserve(bridge, paired["root"], "codex", "other.txt")
    (repo / "shared.txt").write_text("operator change\n")
    assert supervision.operator_edits(bridge.home, paired) == {
        "claude": ["shared.txt"]
    }
    view = dashboard.collect(bridge.home, False, {})
    rows = {row["participant"]: row for row in view["projects"][0]["rows"]}
    assert rows["claude"]["operator_edits"] == ["shared.txt"]
    assert rows["codex"]["operator_edits"] == []
    marker = rows["claude"]["operator_edit"]
    assert marker.startswith("operator edited reserved path shared.txt")
    assert "nothing was reverted" in marker
    assert any(line == f"    {marker}" for line in dashboard.render(view))
    bridge.status(cli.Selection(participant="claude"))
    assert marker in capsys.readouterr().out
    bridge.status(cli.Selection(participant="codex"))
    assert "operator edited" not in capsys.readouterr().out
    reported = top_json(bridge, monkeypatch, capsys)
    assert reported["claude"]["operator_edits"] == ["shared.txt"]
    assert reported["codex"]["operator_edits"] == []


def test_a_clean_checkout_reports_nothing(bridge, repo, paired):
    reserve(bridge, paired["root"], "claude", "shared.txt")
    assert supervision.operator_edits(bridge.home, paired) == {}
    view = dashboard.collect(bridge.home, False, {})
    for row in view["projects"][0]["rows"]:
        assert row["operator_edits"] == []
        assert row["operator_edit"] == ""


def test_a_glob_reservation_matches_a_new_file_under_it(bridge, repo, paired):
    reserve(bridge, paired["root"], "claude", "src/*.py", "docs/")
    (repo / "src").mkdir()
    (repo / "src" / "engine.py").write_text("pass\n")
    (repo / "src" / "notes.txt").write_text("free\n")
    (repo / "docs").mkdir()
    (repo / "docs" / "guide.md").write_text("draft\n")
    assert supervision.operator_edits(bridge.home, paired) == {
        "claude": ["docs/guide.md", "src/engine.py"]
    }


def test_the_top_flag_suppresses_the_reading(
    bridge, repo, paired, monkeypatch, capsys
):
    reserve(bridge, paired["root"], "claude", "shared.txt")
    (repo / "shared.txt").write_text("operator change\n")
    assert top_json(bridge, monkeypatch, capsys)["claude"][
        "operator_edits"
    ] == ["shared.txt"]
    quiet = top_json(bridge, monkeypatch, capsys, "--no-operator-edits")
    assert quiet["claude"]["operator_edits"] == []
    view = dashboard.collect(bridge.home, False, {}, operator_edits=False)
    assert view["projects"][0]["rows"][0]["operator_edits"] == []


def test_the_hook_notifies_the_lane_once_per_distinct_path_set(
    bridge, repo, paired
):
    reserve(bridge, paired["root"], "claude", "shared.txt", "notes.md")
    (repo / "shared.txt").write_text("operator change\n")
    directory = Path(paired["lanes"]["claude"]).parent
    write_json(directory / "claude-identity.json", {"name": "claude"})
    payload = {
        "hook_event_name": "PreToolUse",
        "session_id": "test",
        "cwd": paired["lanes"]["claude"],
        "tool_name": "Bash",
    }
    first = checkpoints.checkpoint(bridge.home, directory, "claude", payload)
    context = first["hookSpecificOutput"]["additionalContext"]
    assert "Operator edit on a path you reserved: shared.txt" in context
    assert "Nothing was reverted" in context
    assert "permissionDecision" not in first["hookSpecificOutput"]
    state = json.loads((directory / "claude-activity.json").read_text())
    assert state["operator_edits"] == ["shared.txt"]
    assert (
        checkpoints.checkpoint(bridge.home, directory, "claude", payload) == {}
    )
    (repo / "notes.md").write_text("more\n")
    again = checkpoints.checkpoint(bridge.home, directory, "claude", payload)
    context = again["hookSpecificOutput"]["additionalContext"]
    assert "notes.md, shared.txt" in context
    assert (
        checkpoints.checkpoint(bridge.home, directory, "claude", payload) == {}
    )
    codex = Path(paired["lanes"]["codex"]).parent
    write_json(codex / "codex-identity.json", {"name": "codex"})
    store.register(bridge.home, paired["root"], "codex")
    peer = {**payload, "cwd": paired["lanes"]["codex"]}
    quiet = checkpoints.checkpoint(bridge.home, codex, "codex", peer)
    assert "Operator edit" not in json.dumps(quiet)


def test_a_git_failure_reports_nothing_without_raising(
    bridge, repo, paired, monkeypatch
):
    reserve(bridge, paired["root"], "claude", "shared.txt")
    (repo / "shared.txt").write_text("operator change\n")

    def timing_out(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs.get("timeout", 0))

    def missing(*args, **kwargs):
        raise OSError("no git")

    monkeypatch.setattr(supervision.subprocess, "run", timing_out)
    assert supervision.dirty_paths(paired["root"]) is None
    assert supervision.operator_edits(bridge.home, paired) == {}
    monkeypatch.setattr(supervision.subprocess, "run", missing)
    assert supervision.operator_edits(bridge.home, paired) == {}
    view = dashboard.collect(bridge.home, False, {})
    assert view["projects"][0]["rows"][0]["operator_edits"] == []


def test_the_hook_reads_the_poll_reading_instead_of_asking_git(
    bridge, repo, paired, monkeypatch
):
    reserve(bridge, paired["root"], "claude", "shared.txt")
    (repo / "shared.txt").write_text("operator change\n")
    directory = Path(paired["lanes"]["claude"]).parent
    write_json(directory / "claude-identity.json", {"name": "claude"})
    kept = supervision.refresh_readings(bridge.home, paired, 60)
    assert kept[0] == {"claude": ["shared.txt"]}

    def asked(*args, **kwargs):
        raise AssertionError("the hook path asked Git")

    monkeypatch.setattr(supervision, "operator_edits", asked)
    monkeypatch.setattr(supervision, "base_advances", asked)
    payload = {
        "hook_event_name": "PreToolUse",
        "session_id": "test",
        "cwd": paired["lanes"]["claude"],
        "tool_name": "Bash",
    }
    first = checkpoints.checkpoint(bridge.home, directory, "claude", payload)
    context = first["hookSpecificOutput"]["additionalContext"]
    assert "Operator edit on a path you reserved: shared.txt" in context


def test_status_reads_the_published_poll_reading_instead_of_asking_git(
    bridge, repo, paired, monkeypatch
):
    reserve(bridge, paired["root"], "claude", "shared.txt")
    (repo / "shared.txt").write_text("operator change\n")
    supervision.refresh_readings(bridge.home, paired, 60)
    supervision._READINGS.clear()

    def asked(*args, **kwargs):
        raise AssertionError("status asked Git for a poll reading")

    monkeypatch.setattr(supervision, "operator_edits", asked)
    monkeypatch.setattr(supervision, "base_advances", asked)
    lanes = {
        lane["participant"]: lane
        for lane in bridge.status_snapshot()["projects"][0]["participants"]
    }
    assert lanes["claude"]["operator_edits"] == ["shared.txt"]
    view = dashboard.collect(bridge.home, False, {})
    rows = {row["participant"]: row for row in view["projects"][0]["rows"]}
    assert rows["claude"]["operator_edits"] == ["shared.txt"]


def test_an_expired_publication_is_read_from_git_again(bridge, repo, paired):
    reserve(bridge, paired["root"], "claude", "shared.txt")
    supervision.refresh_readings(bridge.home, paired, -1)
    supervision._READINGS.clear()
    (repo / "shared.txt").write_text("operator change\n")

    assert supervision.readings(bridge.home, paired)[0] == {
        "claude": ["shared.txt"]
    }


def test_the_batch_matcher_agrees_with_the_pairwise_rule():
    keys = [
        "a",
        "a/",
        "a/b",
        "a/b/",
        "a/b/c.py",
        "a/*",
        "a/b/*.py",
        "*.py",
        "ab",
        "ab/c",
        "x:y",
        "x:y/z",
        "[ab]/c",
        "?",
        "c/d/e",
    ]
    for size in (1, 2):
        for patterns in itertools.combinations(keys, size):
            expected = {
                path
                for path in keys
                for pattern in patterns
                if store.overlapping(path, pattern)
            }
            assert store.overlapping_paths(keys, list(patterns)) == expected
    assert store.overlapping_paths(keys, []) == set()
