"""Checks the lane hook gate on `git worktree add` outside `<lane>-wt`."""

import json
from pathlib import Path

import pytest

from agent_parley import lanes
from agent_parley.checkpoints import checkpoint, stray_worktree
from agent_parley.state import write_json

PROVIDERS = ("claude", "codex")


def payload(lane, command, cwd=None):
    """Builds a native shell tool payload for one lane command."""
    return {
        "hook_event_name": "PreToolUse",
        "cwd": str(cwd or lane),
        "session_id": "s1",
        "tool_name": "Bash",
        "tool_input": {"command": command},
    }


def decide(bridge, paired, name, command):
    """Runs one PreToolUse decision for a registered lane."""
    lane = Path(paired["lanes"][name]).resolve()
    write_json(lane.parent / f"{name}-identity.json", {"name": name})
    return lane, checkpoint(
        bridge.home, lane.parent, name, payload(lane, command)
    )


def refused(output):
    """Reports whether a hook output denies the call."""
    details = output.get("hookSpecificOutput", {})
    return details.get("permissionDecision") == "deny"


def reasons(lane, name):
    """Lists the reason class of every decision a lane recorded."""
    path = lane.parent / f"{name}-events.jsonl"
    return [
        json.loads(line)["reason_class"]
        for line in path.read_text().splitlines()
    ]


@pytest.mark.parametrize("name", PROVIDERS)
@pytest.mark.parametrize(
    "command",
    [
        "git worktree add {wt}/fix -b fix/foo",
        "git worktree add -b fix/foo --lock --reason 'keep it' {wt}/held",
        "git worktree add --detach {wt}/look HEAD",
        "git worktree add -- {wt}/plain",
        "git worktree add ../{lane_name}-wt/relative",
        "git -C .. worktree add {lane_name}-wt/from-parent",
        "rtk git worktree add -B fix/bar {wt}/proxied",
    ],
)
def test_an_add_inside_the_lane_wt_directory_is_allowed(
    bridge, paired, name, command
):
    lane = Path(paired["lanes"][name]).resolve()
    text = command.format(wt=lanes.extras(lane), lane_name=lane.name)
    assert stray_worktree(payload(lane, text), lane) is None
    decide(bridge, paired, name, text)
    assert "worktree_refused" not in reasons(lane, name)


@pytest.mark.parametrize("name", PROVIDERS)
@pytest.mark.parametrize(
    "command",
    [
        "git worktree add /tmp/x -b fix/foo",
        "git worktree add ~/tm-wt-1871",
        "git worktree add {parent}/beside",
        "git worktree add ../beside",
        "git worktree add -b fix/foo --lock {lane}/nested",
        "git -C .. worktree add beside",
        "git -C {parent} worktree add --detach beside HEAD",
        "git worktree add {wt}",
        "git status && git worktree add /tmp/chained",
    ],
)
def test_an_add_outside_the_lane_wt_directory_is_denied(
    bridge, paired, name, command
):
    lane = Path(paired["lanes"][name]).resolve()
    text = command.format(wt=lanes.extras(lane), parent=lane.parent, lane=lane)
    lane, output = decide(bridge, paired, name, text)
    assert refused(output)
    reason = output["hookSpecificOutput"]["permissionDecisionReason"]
    assert f"inside {lanes.extras(lane)}/" in reason
    assert "not an authority boundary" in reason
    events = (lane.parent / f"{name}-events.jsonl").read_text()
    record = json.loads(events.splitlines()[-1])
    assert reasons(lane, name)[-1] == "worktree_refused"
    assert record["decision"] == "deny"


def test_a_relative_target_resolves_against_the_working_directory(paired):
    lane = Path(paired["lanes"]["claude"]).resolve()
    inner = lane / "src"
    inner.mkdir()
    stray = stray_worktree(
        payload(lane, "git worktree add ../../x -b fix/x", inner), lane
    )
    assert stray is not None
    assert stray[0] == lane.parent / "x"
    allowed = stray_worktree(
        payload(lane, f"git worktree add ../../{lane.name}-wt/x", inner),
        lane,
    )
    assert allowed is None


def test_the_denial_names_the_command_to_run_instead(paired):
    lane = Path(paired["lanes"]["codex"]).resolve()
    target, suggested = stray_worktree(
        payload(lane, "git -C . worktree add -b fix/foo /tmp/work main"),
        lane,
    )
    assert target == Path("/tmp/work").resolve()
    assert suggested == (
        f"git -C . worktree add -b fix/foo {lanes.extras(lane)}/work main"
    )


@pytest.mark.parametrize(
    "command",
    [
        "git worktree move /tmp/a /tmp/b",
        "git worktree remove /tmp/x",
        "git worktree list",
        "git worktree prune",
        "git worktree add",
        "git status",
        "echo git worktree add /tmp/x",
    ],
)
def test_other_commands_pass_the_gate(paired, command):
    lane = Path(paired["lanes"]["claude"]).resolve()
    assert stray_worktree(payload(lane, command), lane) is None


def test_a_call_without_a_shell_command_passes_the_gate(paired):
    lane = Path(paired["lanes"]["claude"]).resolve()
    edit = {
        "hook_event_name": "PreToolUse",
        "cwd": str(lane),
        "tool_name": "Edit",
        "tool_input": {"file_path": str(lane / "a.py")},
    }
    assert stray_worktree(edit, lane) is None
    assert stray_worktree({**edit, "tool_input": "x"}, lane) is None


def test_the_gate_is_relative_to_the_session_s_own_lane(paired):
    claude = Path(paired["lanes"]["claude"]).resolve()
    codex = Path(paired["lanes"]["codex"]).resolve()
    command = f"git worktree add {lanes.extras(claude)}/mine"
    assert stray_worktree(payload(claude, command), claude) is None
    assert stray_worktree(payload(codex, command), codex) is not None
