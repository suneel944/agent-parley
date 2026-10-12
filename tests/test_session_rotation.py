"""Checks that a lane rotates to a fresh native session after a claim."""

import json
import os
import select
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from agent_parley import checkpoints, cli, process, roster, terminal
from agent_parley.state import BridgeError, write_json
from agent_parley.supervision import settings

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


def at_rotation_point(directory):
    """Records a ready report on #7 as the lane's last report."""
    write_json(
        directory / "claude-activity.json",
        {
            **state(directory),
            "outcome": "ready",
            "summary": "Shipped the parser fix.",
            "remaining": "",
            "rotation": {"at": 1.0, "issue": "7", "outcome": "ready"},
        },
    )


def captured_launch(bridge, repo, monkeypatch):
    """Resumes the lane once and returns the native command it ran."""
    commands: list = []
    monkeypatch.setattr(
        terminal,
        "run",
        lambda command, *args, **kwargs: commands.append(command) or 0,
    )
    assert bridge.launch("claude", repo, terminal.PROMPT, resume=True) == 0
    return commands[0]


def test_a_ready_report_on_a_claim_records_a_rotation_point(
    bridge, repo, paired
):
    directory = bridge.project(repo)[1]
    lane = paired["lanes"]["claude"]
    bridge.issue(lane, "claim", "5")
    bridge.report(lane, "ready", "Done.", "", "make check", issue="5")
    rotation = json.loads((directory / "claude-activity.json").read_text())[
        "rotation"
    ]
    assert rotation["issue"] == "5"
    assert rotation["outcome"] == "ready"


def test_a_partial_report_records_no_rotation_point(bridge, repo, paired):
    directory = bridge.project(repo)[1]
    lane = paired["lanes"]["claude"]
    bridge.issue(lane, "claim", "5")
    bridge.report(lane, "partial", "Half.", "the tests", "", issue="5")
    recorded = json.loads((directory / "claude-activity.json").read_text())
    assert "rotation" not in recorded


def test_a_resume_at_a_rotation_point_starts_a_fresh_session(
    bridge, repo, lane, monkeypatch
):
    at_rotation_point(lane)
    command = captured_launch(bridge, repo, monkeypatch)
    assert "--resume" not in command
    recorded = state(lane)
    assert recorded["rotated_from"] == CONFIRMED
    assert "rotation" not in recorded


def test_a_rotation_forgets_the_offers_the_old_session_was_given(
    bridge, repo, lane, monkeypatch
):
    write_json(
        lane / "claude-work.json",
        {
            "offer": {"id": "abc"},
            "dispatch": {"state": "pending", "delivered": True},
            "delivered": ["abc"],
        },
    )
    at_rotation_point(lane)
    captured_launch(bridge, repo, monkeypatch)
    published = json.loads((lane / "claude-work.json").read_text())
    assert "delivered" not in published
    assert "delivered" not in published["dispatch"]
    assert published["offer"] == {"id": "abc"}


def test_a_resume_without_rotation_keeps_the_delivered_offers(
    bridge, repo, lane, monkeypatch
):
    write_json(lane / "claude-work.json", {"delivered": ["abc"]})
    captured_launch(bridge, repo, monkeypatch)
    published = json.loads((lane / "claude-work.json").read_text())
    assert published["delivered"] == ["abc"]


def test_the_fresh_session_bootstrap_names_lane_claims_and_last_report(
    bridge, repo, lane, monkeypatch
):
    worktree = roster.read(lane)["participants"]["claude"]["lane"]
    bridge.issue(Path(worktree), "claim", "7")
    at_rotation_point(lane)
    task = captured_launch(bridge, repo, monkeypatch)[-1]
    assert task.startswith("Fresh session:")
    assert "Lane: claude (claude)" in task
    assert "Open claims: #7 (" in task
    assert "Last report: ready on #7: Shipped the parser fix." in task
    assert task.endswith(terminal.PROMPT)


def test_the_next_resume_keys_on_the_fresh_session_id(
    bridge, repo, lane, monkeypatch
):
    at_rotation_point(lane)
    captured_launch(bridge, repo, monkeypatch)
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
    command = captured_launch(bridge, repo, monkeypatch)
    assert command[1:3] == ["--resume", REPLACEMENT]


def test_rotation_off_resumes_the_recorded_session(
    bridge, repo, lane, monkeypatch
):
    manifest = roster.read(lane)
    manifest["supervision"] = {"rotate": False}
    write_json(lane / "project.json", manifest)
    at_rotation_point(lane)
    command = captured_launch(bridge, repo, monkeypatch)
    assert command[1:3] == ["--resume", CONFIRMED]
    assert not command[-1].startswith("Fresh session:")


def test_the_rotate_setting_defaults_on_and_must_be_a_boolean():
    assert settings({})["rotate"] is True
    with pytest.raises(BridgeError, match="rotate must be a boolean"):
        settings({"rotate": "no"})


@pytest.mark.parametrize("rotate", [True, False])
def test_a_detached_launcher_ends_an_idle_session_at_a_rotation_point(
    rotate,
):
    with tempfile.TemporaryDirectory(prefix="wake-") as temporary:
        directory = Path(temporary)
        lane = directory / "lane"
        lane.mkdir()
        write_json(
            directory / "lane-activity.json",
            {
                "activity": "idle",
                "updated": 1,
                "session_pid": os.getpid(),
                "session_ticks": process.start_ticks(os.getpid()),
                "rotation": {"at": 1.0, "issue": "7", "outcome": "ready"},
            },
        )
        script = (
            "print('READY', flush=True)\n"
            "line = input()\nprint('RECEIVED:' + line, flush=True)\n"
        )
        harness = (
            "import os, sys\nfrom pathlib import Path\n"
            "from agent_parley.terminal import run\n"
            "raise SystemExit(run([sys.executable, '-c', sys.argv[2]], "
            "Path(sys.argv[1]), dict(os.environ), 'lane', attached=False, "
            f"rotate={rotate}))"
        )
        child = subprocess.Popen(
            [sys.executable, "-c", harness, str(lane), script],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            assert select.select([child.stdout], [], [], 10)[0]
            assert b"READY" in child.stdout.readline()
            expected = terminal.ROTATING if rotate else "accepted"
            assert terminal.request(directory, "lane") == expected
            output, _ = child.communicate(timeout=10)
            received = ("RECEIVED:" + terminal.PROMPT).encode() in output
            assert received is not rotate
            assert not (directory / "lane-wake.sock").exists()
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=10)
