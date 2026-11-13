"""Worktrees a lane's native client makes are observed, never created."""

import asyncio
import json
import os
import shutil
from pathlib import Path

import pytest

from agent_parley import checkpoints, dashboard, observed, roster, store
from agent_parley.cli import git
from agent_parley.state import write_json

AUTHOR = (
    "-c",
    "user.name=Bridge Test",
    "-c",
    "user.email=test@example.com",
)
TOOL = {"hook_event_name": "PostToolUse", "tool_name": "Read", "tool_input": {}}
STOP = {"hook_event_name": "Stop", "stop_hook_active": False}


@pytest.fixture
def lane(bridge, repo, paired):
    """Returns the claude lane of a paired roster with identities written."""
    store.initialize(bridge.home)
    for name in ("claude", "codex"):
        asyncio.run(bridge.identity(name, paired))
    return Path(paired["lanes"]["claude"]).resolve()


def add(lane, target, branch, start="HEAD"):
    """Adds a worktree of the lane's repository, as the native client does."""
    git(lane, "worktree", "add", "-b", branch, str(target), start)
    return target.resolve()


def serve(bridge, lane, payload, cwd):
    """Serves one claude hook event in-process from a working directory."""
    return checkpoints.serve(
        bridge.home,
        {
            "directory": str(lane.parent),
            "participant": "claude",
            "payload": {**payload, "cwd": str(cwd), "session_id": "s1"},
        },
    )


def events(lane):
    """Reads the claude lane's recorded hook events."""
    path = lane.parent / "claude-events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def records(lane):
    """Reads the claude lane's observed worktree records."""
    return observed.of(roster.read(lane.parent), "claude")


def reserve_as_codex(bridge, lane, key):
    """Reserves one path as the codex lane through the served tool path."""
    identity = json.loads((lane.parent / "codex-identity.json").read_text())
    actor = store.authenticate(bridge.home, identity["registration_token"])
    assert actor is not None
    return store.call(
        bridge.home, actor, "file_reservation_paths", {"paths": [key]}
    )


def test_a_cli_worktree_in_the_lane_is_registered_and_listed(bridge, lane):
    tree = add(lane, lane / ".claude" / "worktrees" / "x", "wt-x")
    served = serve(bridge, lane, TOOL, tree)
    assert served["status"] == 0
    assert events(lane)[-1]["reason_class"] != "outside_lane"
    (found,) = records(lane)
    assert found["path"] == str(tree)
    assert found["branch"] == "wt-x"
    assert found["base"] == "lane"
    assert found["flagged"] is False
    assert found["closed"] is None
    view = dashboard.collect(bridge.home, False, {})
    rows = {row["participant"]: row for row in view["projects"][0]["rows"]}
    assert rows["claude"]["worktrees"][0]["path"] == str(tree)
    assert f"worktree {tree} on wt-x (base lane)" in dashboard.notes(
        rows["claude"]
    )


def test_a_worktree_outside_the_lane_is_registered_and_flagged(
    bridge, lane, tmp_path
):
    tree = add(lane, tmp_path / "elsewhere", "wt-far")
    served = serve(bridge, lane, TOOL, tree)
    assert served["status"] == 0
    assert events(lane)[-1]["reason_class"] != "outside_lane"
    (found,) = records(lane)
    assert found["flagged"] is True
    view = dashboard.collect(bridge.home, False, {})
    rows = {row["participant"]: row for row in view["projects"][0]["rows"]}
    assert any("flagged" in note for note in dashboard.notes(rows["claude"]))


def test_a_removed_worktree_is_closed_at_stop_and_keeps_its_lease(bridge, lane):
    """Closing leaves the lease: the store has no per-key release."""
    tree = add(lane, lane / ".claude" / "worktrees" / "x", "wt-x")
    serve(bridge, lane, TOOL, tree)
    shutil.rmtree(tree)
    served = serve(bridge, lane, STOP, lane)
    assert served["status"] == 0
    (found,) = records(lane)
    assert found["closed"] is not None
    held = reserve_as_codex(bridge, lane, ".claude/worktrees/x")
    assert held["conflicts"]


def test_a_peer_reserving_a_registered_worktree_meets_a_conflict(bridge, lane):
    tree = add(lane, lane / ".claude" / "worktrees" / "x", "wt-x")
    serve(bridge, lane, TOOL, tree)
    result = reserve_as_codex(bridge, lane, ".claude/worktrees/x")
    assert result["granted"] == []
    assert result["conflicts"][0]["owner"] == "claude"


def test_a_cwd_in_an_unrelated_repository_is_still_refused(
    bridge, lane, tmp_path
):
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init")
    (other / "a.txt").write_text("a\n")
    git(other, "add", "a.txt")
    git(other, *AUTHOR, "commit", "-m", "other")
    pre = {"hook_event_name": "PreToolUse", "tool_name": "Read"}
    served = serve(bridge, lane, {**pre, "tool_input": {}}, other.resolve())
    assert '"permissionDecision": "deny"' in served["stdout"]
    assert events(lane)[-1]["reason_class"] == "outside_lane"
    assert records(lane) == []


@pytest.mark.parametrize("place", ["root", "peer"])
def test_the_main_checkout_and_a_peer_lane_are_never_registered(
    bridge, lane, repo, paired, place
):
    where = repo if place == "root" else Path(paired["lanes"]["codex"])
    pre = {"hook_event_name": "PreToolUse", "tool_name": "Read"}
    served = serve(bridge, lane, {**pre, "tool_input": {}}, where.resolve())
    assert '"permissionDecision": "deny"' in served["stdout"]
    assert events(lane)[-1]["reason_class"] == "outside_lane"
    assert records(lane) == []


@pytest.mark.parametrize(
    "event,field",
    [("TaskCompleted", "progressed"), ("TeammateIdle", "teammate_idle")],
)
@pytest.mark.parametrize("subagent", [False, True])
def test_a_team_event_records_activity_and_never_blocks(
    bridge, lane, event, field, subagent
):
    payload = {"hook_event_name": event}
    if subagent:
        payload["agent_id"] = "helper-1"
    served = serve(bridge, lane, payload, lane)
    assert served["status"] == 0
    assert json.loads(served["stdout"]) == {}
    state = json.loads((lane.parent / "claude-activity.json").read_text())
    assert state[field]
    assert state["event"] == event


def test_the_claude_launch_hooks_team_events_and_no_worktree_hook(
    bridge, repo, monkeypatch, tmp_path
):
    roster.define_provider(
        bridge.home, "stub-claude", "claude", "stub-claude", "", [], []
    )
    binary = tmp_path / "bin"
    binary.mkdir()
    script = binary / "stub-claude"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "with open(os.environ['CAPTURE'], 'w') as f:\n"
        " json.dump({'argv': sys.argv[1:]}, f)\n"
    )
    script.chmod(0o755)
    capture = tmp_path / "stub-claude.json"
    monkeypatch.setenv("CAPTURE", str(capture))
    monkeypatch.setenv("PATH", str(binary) + os.pathsep + os.environ["PATH"])
    data = bridge.add_participant(repo, "lane", "stub-claude")
    directory = Path(data["participants"]["lane"]["lane"]).parent
    monkeypatch.setattr(bridge, "up", lambda: None)

    async def fake_identity(*args):
        return {"registration_token": "test-scoped-credential"}

    monkeypatch.setattr(bridge, "identity", fake_identity)
    assert bridge.launch("lane", repo, "Work on issue 1385") == 0
    argv = json.loads(capture.read_text())["argv"]
    hooks = json.loads(argv[argv.index("--settings") + 1])["hooks"]
    assert {"TaskCompleted", "TeammateIdle"} <= set(hooks)
    assert not {"WorktreeCreate", "WorktreeRemove"} & set(hooks)
    assert set(bridge.hooks("lane", directory)) == set(checkpoints.EVENTS)


def test_the_base_reads_lane_or_default(bridge, lane, repo):
    (lane / "lane.txt").write_text("lane work\n")
    git(lane, "add", "lane.txt")
    git(lane, *AUTHOR, "commit", "-m", "lane work")
    root = git(repo, "rev-list", "--max-parents=0", "HEAD").strip()
    cli = lane / ".claude" / "worktrees"
    near = add(lane, cli / "near", "wt-near")
    far = add(lane, cli / "far", "wt-far", root)
    serve(bridge, lane, TOOL, near)
    serve(bridge, lane, TOOL, far)
    bases = {found["branch"]: found["base"] for found in records(lane)}
    assert bases == {"wt-near": "lane", "wt-far": "default"}


def test_a_known_worktree_refreshes_seen_at_most_once_a_minute(
    bridge, lane, monkeypatch
):
    tree = add(lane, lane / ".claude" / "worktrees" / "x", "wt-x")
    serve(bridge, lane, TOOL, tree)
    first = records(lane)[0]["seen"]
    serve(bridge, lane, TOOL, tree)
    assert records(lane)[0]["seen"] == first
    manifest = roster.read(lane.parent)
    manifest["participants"]["claude"]["worktrees"][0]["seen"] = first - 120
    write_json(lane.parent / "project.json", manifest)
    serve(bridge, lane, TOOL, tree)
    assert records(lane)[0]["seen"] > first - 120
