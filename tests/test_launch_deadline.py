"""Checks the start deadline a launch is judged against."""

import json
import os
import time
from pathlib import Path

import pytest

from agent_parley import store, supervision, terminal
from agent_parley.checkpoints import checkpoint
from agent_parley.process import start_ticks
from agent_parley.state import write_json


@pytest.fixture(autouse=True)
def quiet_forge(monkeypatch):
    """Keeps the poll off the host forge while these tests run."""
    monkeypatch.setattr(
        supervision.forge, "branch_completion", lambda *args: None
    )


def registered(bridge, paired):
    """Registers both lanes so mail and presence are readable."""
    store.initialize(bridge.home)
    for name in ("claude", "codex"):
        store.register(bridge.home, paired["root"], name)


def launching(directory, name, ago=0.0):
    """Publishes the state a launcher writes before it starts a client."""
    write_json(
        directory / f"{name}-activity.json",
        {
            "session_pid": os.getpid(),
            "session_ticks": start_ticks(os.getpid()),
            "activity": supervision.STARTING,
            "launcher_managed": True,
            "session_id": "",
            "updated": time.time() - ago,
            "session_started": time.time() - ago,
        },
    )


def alive(directory, name, **extra):
    """Publishes a live, quiet session for one lane."""
    write_json(
        directory / f"{name}-activity.json",
        {
            "session_pid": os.getpid(),
            "session_ticks": start_ticks(os.getpid()),
            "activity": "idle",
            "updated": time.time(),
            **extra,
        },
    )


def turn_ended(directory, name, ago):
    """Records one retained turn end so an idle stretch reads as open."""
    (directory / f"{name}-events.jsonl").write_text(
        json.dumps({"ts": time.time() - ago, "event": "Stop"}) + "\n"
    )


def published(directory, name):
    """Reads one lane's published activity state."""
    return json.loads((directory / f"{name}-activity.json").read_text())


def test_a_launch_with_no_hook_event_inside_the_deadline_is_not_started(
    bridge, repo, paired
):
    registered(bridge, paired)
    directory = Path(paired["lanes"]["codex"]).parent
    launching(directory, "codex")

    assert supervision.launches(directory, paired, supervision.DEFAULTS) == []
    assert published(directory, "codex")["activity"] == supervision.STARTING

    launching(directory, "codex", ago=120)
    supervision.poll(bridge.home, directory)

    state = published(directory, "codex")
    assert state["activity"] == supervision.NOT_STARTED
    assert state["not_started"]["deadline"] == 30
    assert state["not_started"]["waited"] >= 120
    assert state["session_id"] == ""
    work = supervision.published_work(directory, "codex")
    assert work["failed"] == ["session"]
    assert "never started within 30s" in work["reason"]


def test_a_hook_event_after_the_deadline_clears_the_not_started_mark(
    bridge, repo, paired
):
    registered(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    directory = lane.parent
    launching(directory, "claude", ago=120)
    write_json(directory / "claude-identity.json", {"name": "claude"})

    assert supervision.launches(directory, paired, supervision.DEFAULTS) == [
        "claude"
    ]

    checkpoint(
        bridge.home,
        directory,
        "claude",
        {
            "hook_event_name": "Stop",
            "session_id": "claude-late",
            "cwd": str(lane),
        },
    )

    state = published(directory, "claude")
    assert state["activity"] == "idle"
    assert "not_started" not in state
    result = supervision.fit(
        bridge.home,
        directory,
        paired,
        "claude",
        supervision.DEFAULTS["stalled_after"],
    )
    assert result["checks"]["session"] is True


def test_a_lane_that_did_not_start_is_never_named_by_a_rebalance(
    bridge, repo, paired
):
    registered(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    directory = lane.parent
    alive(directory, "claude")
    launching(directory, "codex", ago=120)
    turn_ended(directory, "codex", 3600)
    bridge.issue(lane, "claim", "2")
    bridge.issue(lane, "claim", "3")

    supervision.poll(bridge.home, directory)

    assert supervision.idle_seconds(directory, "codex") >= 3600
    assert supervision.published_work(directory, "codex")["fit"] is False
    assert supervision.published_work(directory, "codex")["offer"] is None
    offer = supervision.published_work(directory, "claude")["offer"]
    assert offer["kind"] == "continue"
    assert "codex" not in offer["text"]


def test_a_wake_spends_no_attempt_on_a_lane_that_never_started(
    bridge, repo, paired, monkeypatch
):
    registered(bridge, paired)
    directory = Path(paired["lanes"]["codex"]).parent
    launching(directory, "codex", ago=120)
    calls = []
    monkeypatch.setattr(
        terminal, "request", lambda *args: calls.append(args) or "accepted"
    )
    config = {**supervision.DEFAULTS, "inactive_after": 1}
    path = directory / "codex-wake.json"
    write_json(
        path,
        {
            "at": 0,
            "backlog": ["1"],
            "attempts": 1,
            "result": "manual attention required",
        },
    )
    supervision.launches(directory, paired, config)
    observed = supervision.presence(directory, "codex", 1)

    supervision.wake(bridge.home, directory, paired, "codex", observed, config)

    parked = json.loads(path.read_text())
    assert not calls
    assert parked["attempts"] == 1
    assert parked["blocked"] == "it never started within 30s of its launch"


def test_the_start_deadline_is_configurable_and_bounded(bridge, paired):
    assert supervision.DEFAULTS["start_deadline"] == 30
    manifest = {**paired, "supervision": {"start_deadline": 120}}
    resolved = supervision.configuration(bridge.home, manifest)
    assert resolved["start_deadline"] == 120
    for value in (0, 86401, True, "30"):
        with pytest.raises(Exception, match="start_deadline|boolean|between"):
            supervision.settings({"start_deadline": value})
