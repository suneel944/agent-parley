"""Checks that a failed launch never destroys the last resumable session."""

import json
from pathlib import Path

import pytest

from agent_parley import checkpoints, cli, terminal
from agent_parley.state import BridgeError, write_json

CONFIRMED = "12345678-abcd-1234-abcd-123456789abc"
REPLACEMENT = "87654321-dcba-4321-dcba-cba987654321"


@pytest.fixture
def lane(bridge, repo, monkeypatch):
    """Prepares a stopped Claude lane holding one confirmed session."""
    manifest = bridge.add_participant(repo, "claude", "claude")
    directory = Path(manifest["lanes"]["claude"]).parent
    write_json(
        directory / "claude-activity.json",
        {"activity": "stopped", "session_id": CONFIRMED},
    )
    monkeypatch.setattr(bridge, "up", lambda: None)

    async def identity(*args):
        return {"registration_token": "test-only"}

    monkeypatch.setattr(bridge, "identity", identity)
    original = cli.shutil.which
    monkeypatch.setattr(
        cli.shutil,
        "which",
        lambda name: "/bin/true" if name == "claude" else original(name),
    )
    return directory


def state(directory):
    """Reads the lane's recorded activity."""
    return json.loads((directory / "claude-activity.json").read_text())


def test_a_nonzero_exit_before_any_hook_keeps_the_resumable_session(
    bridge, repo, lane, monkeypatch
):
    monkeypatch.setattr(terminal, "run", lambda *args, **kwargs: 42)
    assert bridge.launch("claude", repo, terminal.PROMPT, resume=True) == 42
    recorded = state(lane)
    assert recorded["resumable_session"] == CONFIRMED
    assert recorded["session_id"] == ""
    assert recorded["activity"] == "stopped"


def test_a_launch_exception_before_any_hook_keeps_the_resumable_session(
    bridge, repo, lane, monkeypatch
):
    def explode(*args, **kwargs):
        raise OSError("native client could not start")

    monkeypatch.setattr(terminal, "run", explode)
    with pytest.raises(OSError, match="could not start"):
        bridge.launch("claude", repo, terminal.PROMPT, resume=True)
    assert state(lane)["resumable_session"] == CONFIRMED


def test_a_failed_launch_can_still_be_resumed_with_the_same_identity(
    bridge, repo, lane, monkeypatch
):
    monkeypatch.setattr(terminal, "run", lambda *args, **kwargs: 42)
    assert bridge.launch("claude", repo, terminal.PROMPT, resume=True) == 42
    captured: list = []
    monkeypatch.setattr(
        terminal,
        "run",
        lambda command, *args, **kwargs: captured.append(command) or 0,
    )
    assert bridge.launch("claude", repo, terminal.PROMPT, resume=True) == 0
    assert captured[0][1:3] == ["--resume", CONFIRMED]


def test_a_confirmed_native_session_replaces_the_resumable_identity(
    bridge, repo, lane, monkeypatch
):
    monkeypatch.setattr(terminal, "run", lambda *args, **kwargs: 0)
    assert bridge.launch("claude", repo, terminal.PROMPT, resume=True) == 0
    assert state(lane)["resumable_session"] == CONFIRMED
    write_json(lane / "claude-identity.json", {"name": "claude"})
    checkpoints.checkpoint(
        bridge.home,
        lane,
        "claude",
        {
            "hook_event_name": "SessionStart",
            "session_id": REPLACEMENT,
            "cwd": str(lane / "claude"),
        },
    )
    assert state(lane)["resumable_session"] == REPLACEMENT


def test_a_lane_that_never_reported_a_session_refuses_to_resume(
    bridge, repo, lane, monkeypatch
):
    write_json(lane / "claude-activity.json", {"activity": "stopped"})
    monkeypatch.setattr(
        terminal, "run", lambda *args, **kwargs: pytest.fail("launched")
    )
    with pytest.raises(BridgeError, match="No usable native session"):
        bridge.launch("claude", repo, terminal.PROMPT, resume=True)
