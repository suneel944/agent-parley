"""Checks that a lane rotates to a fresh native session after a claim."""

import json
import os
import select
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from agent_parley import (
    checkpoints,
    cli,
    process,
    records,
    roster,
    supervision,
    terminal,
)
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


def idle_for(directory, seconds):
    """Records the lane's last hook event ``seconds`` ago."""
    write_json(
        directory / "claude-activity.json",
        {**state(directory), "updated": time.time() - seconds},
    )


def test_the_cache_settings_default_and_validate():
    assert settings({})["cache_window"] == 3300
    assert settings({})["cache_restart_tokens"] == 50000
    with pytest.raises(BridgeError, match="cache_window must be between"):
        settings({"cache_window": 0})
    with pytest.raises(BridgeError, match="cache_restart_tokens must be"):
        settings({"cache_restart_tokens": -1})


@pytest.mark.parametrize(
    ("adapter", "idle", "tokens", "expected"),
    [
        ("claude", 60, None, supervision.CACHE_RESUME),
        ("claude", 4000, 1000, supervision.CACHE_RESUME),
        ("claude", 4000, 50000, supervision.CACHE_RESTART),
        ("claude", 4000, None, supervision.CACHE_RESTART),
        ("codex", 4000, 1000, supervision.CACHE_RESUME),
        ("gemini", 4000, 1000, supervision.CACHE_RESUME),
        ("opencode", 4000, 1000, supervision.CACHE_RESUME),
        ("copilot", 4000, 0, supervision.CACHE_RESTART),
        ("amp", 4000, 0, supervision.CACHE_RESTART),
        ("unlisted", 4000, 0, supervision.CACHE_RESTART),
    ],
)
def test_a_cold_wake_resumes_only_a_small_measured_session(
    adapter, idle, tokens, expected
):
    config = settings({})
    assert supervision.cache_action(adapter, idle, tokens, config) == expected


def test_every_policy_names_compact_resume_and_fresh_session():
    for policy in supervision.CACHE_POLICIES.values():
        assert set(policy) == {"compact", "resume", "fresh", "sized"}
        assert policy["fresh"] is True


def test_a_lane_idle_past_its_cache_records_a_cache_rotation_point(
    bridge, lane
):
    idle_for(lane, 4000)
    point = supervision.cache_rotation(bridge.home, lane, "claude")
    assert point is not None
    assert point["outcome"] == supervision.CACHE_ROTATION
    assert point["session"] == CONFIRMED
    assert state(lane)["rotation"] == point


def test_a_warm_lane_records_no_cache_rotation_point(bridge, lane):
    idle_for(lane, 60)
    assert supervision.cache_rotation(bridge.home, lane, "claude") is None
    assert "rotation" not in state(lane)


def test_a_small_cold_session_keeps_its_session(bridge, lane, monkeypatch):
    monkeypatch.setattr(records, "reported_tokens", lambda *a, **k: 10)
    idle_for(lane, 4000)
    assert supervision.cache_rotation(bridge.home, lane, "claude") is None


def test_rotation_off_records_no_cache_rotation_point(bridge, lane):
    manifest = roster.read(lane)
    manifest["supervision"] = {"rotate": False}
    write_json(lane / "project.json", manifest)
    idle_for(lane, 4000)
    assert supervision.cache_rotation(bridge.home, lane, "claude") is None


def test_a_service_resume_past_the_cache_starts_a_seeded_fresh_session(
    bridge, repo, lane, monkeypatch
):
    worktree = roster.read(lane)["participants"]["claude"]["lane"]
    bridge.issue(Path(worktree), "claim", "7")
    idle_for(lane, 4000)
    command = captured_launch(bridge, repo, monkeypatch)
    assert "--resume" not in command
    task = command[-1]
    assert "after its prompt cache expired while it sat idle" in task
    assert "\nBranch: " in task
    assert "Open claims: #7 (" in task
    assert "\nReservations: " in task
    assert "\nMail: " in task
    recorded = state(lane)
    assert recorded["rotated_from"] == CONFIRMED
    assert "rotation" not in recorded


def test_a_service_resume_of_a_warm_lane_keeps_its_session(
    bridge, repo, lane, monkeypatch
):
    idle_for(lane, 60)
    command = captured_launch(bridge, repo, monkeypatch)
    assert command[1:3] == ["--resume", CONFIRMED]


def test_a_launcher_without_a_state_root_never_rotates_for_its_cache(
    tmp_path,
):
    assert terminal._cache_expired(tmp_path, "lane", None) is False
