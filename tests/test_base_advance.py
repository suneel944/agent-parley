"""Checks the advisory reading of a base branch that moved under a lane."""

import json
import subprocess
import sys
from pathlib import Path

from agent_parley import checkpoints, cli, dashboard, store, supervision
from agent_parley.cli import git
from agent_parley.state import write_json

AUTHOR = (
    "-c",
    "user.name=Bridge Test",
    "-c",
    "user.email=test@example.com",
)


def reserve(bridge, root, name, *keys):
    """Reserves keys as one registered lane through the served tool path."""
    store.initialize(bridge.home)
    token = store.register(bridge.home, root, name)["registration_token"]
    holder = store.authenticate(bridge.home, token)
    assert holder is not None
    return store.call(
        bridge.home, holder, "file_reservation_paths", {"paths": list(keys)}
    )


def commit(checkout, name, text):
    """Records one commit in a checkout, advancing the branch it is on."""
    path = Path(checkout) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    git(Path(checkout), "add", name)
    git(Path(checkout), *AUTHOR, "commit", "-m", f"change {name}")


def hook(bridge, paired, name):
    """Runs one lifecycle checkpoint for a lane and reports its output."""
    directory = Path(paired["lanes"][name]).parent
    write_json(directory / f"{name}-identity.json", {"name": name})
    payload = {
        "hook_event_name": "PreToolUse",
        "session_id": "test",
        "cwd": paired["lanes"][name],
        "tool_name": "Bash",
    }
    return checkpoints.checkpoint(bridge.home, directory, name, payload)


def top_json(bridge, monkeypatch, capsys):
    """Runs one JSON frame of the live view and reads its lane records."""
    monkeypatch.setattr(
        sys,
        "argv",
        ["agent-parley", "--home", str(bridge.home), "top", "--json"],
    )
    assert cli.main() == 0
    document = json.loads(capsys.readouterr().out)
    return {
        row["participant"]: row
        for row in document["projects"][0]["participants"]
    }


def test_a_base_commit_on_a_reserved_path_marks_the_lane_and_notifies(
    bridge, repo, paired, monkeypatch, capsys
):
    reserve(bridge, paired["root"], "claude", "shared.txt", "notes.md")
    reserve(bridge, paired["root"], "codex", "other.txt")
    commit(repo, "shared.txt", "peer merged this\n")
    assert supervision.base_advances(bridge.home, paired) == {
        "claude": ["shared.txt"]
    }
    view = dashboard.collect(bridge.home, False, {})
    rows = {row["participant"]: row for row in view["projects"][0]["rows"]}
    assert rows["claude"]["base_advance_paths"] == ["shared.txt"]
    assert rows["codex"]["base_advance_paths"] == []
    marker = rows["claude"]["base_advance"]
    assert marker.startswith("base advanced over held path shared.txt")
    assert "nothing was rebased" in marker
    assert any(line == f"    {marker}" for line in dashboard.render(view))
    bridge.status(cli.Selection(participant="claude"))
    assert marker in capsys.readouterr().out
    bridge.status(cli.Selection(participant="codex"))
    assert "base advanced" not in capsys.readouterr().out
    reported = top_json(bridge, monkeypatch, capsys)
    assert reported["claude"]["base_advance_paths"] == ["shared.txt"]
    assert reported["codex"]["base_advance_paths"] == []
    first = hook(bridge, paired, "claude")
    context = first["hookSpecificOutput"]["additionalContext"]
    assert "The base branch advanced over paths you hold: shared.txt" in context
    assert "Nothing was rebased or paused" in context
    directory = Path(paired["lanes"]["claude"]).parent
    state = json.loads((directory / "claude-activity.json").read_text())
    assert state["base_advance"] == ["shared.txt"]
    assert hook(bridge, paired, "claude") == {}
    commit(repo, "notes.md", "and this\n")
    again = hook(bridge, paired, "claude")
    context = again["hookSpecificOutput"]["additionalContext"]
    assert "notes.md, shared.txt" in context
    assert hook(bridge, paired, "claude") == {}


def test_every_lane_behind_the_base_is_read_with_its_own_paths(
    bridge, repo, paired
):
    reserve(bridge, paired["root"], "claude", "first.txt")
    reserve(bridge, paired["root"], "codex", "second.txt")
    commit(repo, "first.txt", "one\n")
    commit(repo, "second.txt", "two\n")
    assert supervision.base_advances(bridge.home, paired) == {
        "claude": ["first.txt"],
        "codex": ["second.txt"],
    }


def test_a_base_advance_over_no_held_path_is_silent(bridge, repo, paired):
    reserve(bridge, paired["root"], "claude", "shared.txt")
    store.register(bridge.home, paired["root"], "codex")
    commit(repo, "untouched.txt", "nobody holds this\n")
    assert supervision.base_advances(bridge.home, paired) == {}
    view = dashboard.collect(bridge.home, False, {})
    for row in view["projects"][0]["rows"]:
        assert row["base_advance_paths"] == []
        assert row["base_advance"] == ""
    assert "base branch advanced" not in json.dumps(
        hook(bridge, paired, "claude")
    )


def test_a_committed_lane_path_is_held_without_a_reservation(
    bridge, repo, paired
):
    store.initialize(bridge.home)
    store.register(bridge.home, paired["root"], "claude")
    commit(paired["lanes"]["claude"], "engine.py", "lane work\n")
    commit(repo, "engine.py", "base work\n")
    assert supervision.base_advances(bridge.home, paired) == {
        "claude": ["engine.py"]
    }


def test_an_uncommitted_lane_edit_is_held_as_well(bridge, repo, paired):
    store.initialize(bridge.home)
    store.register(bridge.home, paired["root"], "claude")
    (Path(paired["lanes"]["claude"]) / "draft.md").write_text("in progress\n")
    commit(repo, "draft.md", "base wrote it first\n")
    assert supervision.base_advances(bridge.home, paired) == {
        "claude": ["draft.md"]
    }


def test_a_current_base_and_an_unreadable_one_report_nothing(
    bridge, repo, paired, monkeypatch
):
    reserve(bridge, paired["root"], "claude", "shared.txt")
    assert supervision.base_advances(bridge.home, paired) == {}
    commit(repo, "shared.txt", "peer merged this\n")

    def timing_out(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs.get("timeout", 0))

    monkeypatch.setattr(supervision.subprocess, "run", timing_out)
    assert supervision.base_advances(bridge.home, paired) == {}
    view = dashboard.collect(bridge.home, False, {})
    assert view["projects"][0]["rows"][0]["base_advance_paths"] == []
