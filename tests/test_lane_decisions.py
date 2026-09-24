"""Pins supervision decisions that read the lane state record."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent_parley import (
    checkpoints,
    issues,
    lanes,
    process,
    store,
    supervision,
    terminal,
)
from agent_parley.state import write_json

STALLED = supervision.DEFAULTS["stalled_after"]


def registered(bridge, paired):
    """Registers both lanes and returns their served identities."""
    store.initialize(bridge.home)
    return {
        name: store.authenticate(
            bridge.home,
            store.register(bridge.home, paired["root"], name)[
                "registration_token"
            ],
        )
        for name in ("claude", "codex")
    }


def place(bridge, paired, name, state, cause=""):
    """Moves one lane's record to a state through a legal path."""
    with store.connect(bridge.home, write=True) as db:
        if state not in lanes.TRANSITIONS[""]:
            lanes.transition(db, paired["root"], name, lanes.STOPPED)
        lanes.transition(db, paired["root"], name, state, cause=cause)


def recorded(bridge, paired, name):
    """Reads one lane's record."""
    with store.connect(bridge.home) as db:
        return lanes.read(db, paired["root"], name)


def idle_live(directory, name):
    """Publishes a live session that went quiet long ago."""
    write_json(
        directory / f"{name}-activity.json",
        {
            "activity": "idle",
            "updated": time.time() - 500,
            "session_pid": os.getpid(),
            "session_ticks": process.start_ticks(os.getpid()),
        },
    )


def killed(directory, name, age):
    """Records a session process that was killed and then aged."""
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    ticks = process.start_ticks(child.pid)
    child.kill()
    child.wait()
    write_json(
        directory / f"{name}-activity.json",
        {
            "activity": "working",
            "updated": time.time() - age,
            "session_id": f"{name}-session",
            "session_pid": child.pid,
            "session_ticks": ticks,
        },
    )


def mail(bridge, actors):
    """Leaves codex one message that owes an acknowledgement."""
    store.call(
        bridge.home,
        actors["claude"],
        "send_message",
        {
            "to": ["codex"],
            "subject": "Review",
            "body_md": "Review the result",
            "idempotency_key": "pending",
            "ack_required": True,
        },
    )


LIVE = {"process_alive": True, "evidence": "", "age_seconds": 5}


@pytest.mark.parametrize(
    ("current", "observed", "expected"),
    [
        (None, {"process_alive": None}, None),
        (lanes.WORKING, {"process_alive": False}, (lanes.STOPPED, "")),
        (lanes.DEAD, {"process_alive": False}, None),
        (lanes.RECLAIMED, {"process_alive": False}, None),
        (lanes.STOPPED, {**LIVE, "activity": "idle"}, (lanes.STARTING, "")),
        (lanes.DEAD, {**LIVE, "activity": "working"}, (lanes.STARTING, "")),
        (lanes.IDLE, {**LIVE, "activity": "working"}, (lanes.WORKING, "")),
        (lanes.WORKING, {**LIVE, "activity": "idle"}, (lanes.IDLE, "")),
        (
            lanes.WORKING,
            {**LIVE, "activity": "waiting", "evidence": "waiting for approval"},
            (lanes.BLOCKED, lanes.APPROVAL),
        ),
        (
            lanes.IDLE,
            {**LIVE, "activity": "waiting", "evidence": "session ended"},
            (lanes.BLOCKED, lanes.PROMPT),
        ),
        (None, {**LIVE, "activity": "unknown"}, None),
    ],
)
def test_a_liveness_sample_reads_as_one_state(current, observed, expected):
    record = None if current is None else {"state": current, "cause": ""}
    assert lanes.from_liveness(record, observed) == expected


@pytest.mark.parametrize("cause", sorted(lanes.HELD_BLOCKS))
def test_a_screen_block_outlives_the_activity_label(cause):
    record = {"state": lanes.BLOCKED, "cause": cause}
    assert lanes.from_liveness(record, {**LIVE, "activity": "working"}) is None


def test_a_stopped_lane_ages_into_dead(tmp_path):
    home = tmp_path / "state"
    home.mkdir()
    store.initialize(home)
    gone = {"process_alive": False, "age_seconds": 10, "evidence": "stopped"}
    with store.connect(home, write=True) as db:
        first = lanes.sample(db, "/r", "a", gone, dead_after=60, now=100.0)
        later = lanes.sample(db, "/r", "a", gone, dead_after=60, now=170.0)
    assert first["state"] == lanes.STOPPED
    assert later["state"] == lanes.DEAD


@pytest.mark.parametrize(
    ("state", "woken"),
    [
        (lanes.STARTING, False),
        (lanes.WORKING, False),
        (lanes.RECLAIMED, False),
        (lanes.IDLE, True),
        (lanes.STOPPED, True),
        (lanes.DEAD, True),
    ],
)
def test_a_wake_acts_only_on_a_lane_whose_state_allows_it(
    bridge, paired, monkeypatch, state, woken
):
    actors = registered(bridge, paired)
    directory = Path(paired["lanes"]["codex"]).parent
    idle_live(directory, "codex")
    mail(bridge, actors)
    place(bridge, paired, "codex", state)
    calls = []
    monkeypatch.setattr(
        terminal, "request", lambda *args: calls.append(args) or "accepted"
    )
    config = {**supervision.DEFAULTS, "inactive_after": 1}
    observed = supervision.presence(directory, "codex", 1)
    supervision.wake(bridge.home, directory, paired, "codex", observed, config)
    assert bool(calls) is woken


def test_a_blocked_lane_is_deferred_under_its_cause(
    bridge, paired, monkeypatch
):
    actors = registered(bridge, paired)
    directory = Path(paired["lanes"]["codex"]).parent
    idle_live(directory, "codex")
    mail(bridge, actors)
    calls = []
    monkeypatch.setattr(
        terminal, "request", lambda *args: calls.append(args) or "accepted"
    )
    config = {**supervision.DEFAULTS, "inactive_after": 1}
    observed = supervision.presence(directory, "codex", 1)
    supervision.wake(bridge.home, directory, paired, "codex", observed, config)
    assert len(calls) == 1
    place(bridge, paired, "codex", lanes.BLOCKED, lanes.CAPACITY)
    path = directory / "codex-wake.json"
    write_json(path, {**json.loads(path.read_text()), "at": 0})
    supervision.wake(bridge.home, directory, paired, "codex", observed, config)
    assert len(calls) == 1
    assert json.loads(path.read_text())["blocked"] == "blocked: capacity"


def test_a_lane_answering_wakes_with_a_bare_stop_still_escalates(
    bridge, paired, monkeypatch
):
    actors = registered(bridge, paired)
    directory = Path(paired["lanes"]["codex"]).parent
    idle_live(directory, "codex")
    mail(bridge, actors)
    calls = []
    monkeypatch.setattr(
        terminal, "request", lambda *args: calls.append(args) or "accepted"
    )
    config = {**supervision.DEFAULTS, "inactive_after": 1}
    observed = supervision.presence(directory, "codex", 1)
    path = directory / "codex-wake.json"
    for _ in range(5):
        supervision.wake(
            bridge.home, directory, paired, "codex", observed, config
        )
        place(bridge, paired, "codex", lanes.WORKING)
        place(bridge, paired, "codex", lanes.IDLE)
        write_json(path, {**json.loads(path.read_text()), "at": 0})
    record = json.loads(path.read_text())
    assert len(calls) == 5
    assert record["attempts"] == 5 and record["exhausted_at"]


def test_a_dead_record_orphans_the_claim_and_a_live_one_does_not(
    bridge, repo, paired
):
    registered(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    directory = lane.parent
    bridge.issue(lane, "claim", "42")
    killed(directory, "claude", STALLED + 100)
    config = supervision.configuration(bridge.home, paired)
    place(bridge, paired, "claude", lanes.WORKING)
    supervision.orphans(bridge.home, directory, paired, config)
    assert not issues.snapshot(directory)["issues"]["42"].get("orphan")
    place(bridge, paired, "claude", lanes.DEAD)
    supervision.orphans(bridge.home, directory, paired, config)
    assert issues.snapshot(directory)["issues"]["42"]["orphan"]["owner"] == (
        "claude"
    )


def test_a_poll_records_a_killed_lane_as_dead_and_orphans_it(
    bridge, repo, paired
):
    registered(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    directory = lane.parent
    bridge.issue(lane, "claim", "42")
    killed(directory, "claude", STALLED + 100)
    supervision.poll(bridge.home, directory)
    assert recorded(bridge, paired, "claude")["state"] == lanes.DEAD
    assert issues.snapshot(directory)["issues"]["42"]["orphan"]


def lease_holder(bridge, paired):
    """Leaves claude one expired lease a queued peer is waiting for."""
    actors = registered(bridge, paired)
    store.call(
        bridge.home,
        actors["claude"],
        "file_reservation_paths",
        {"paths": ["src/engine.py"], "ttl_seconds": 3600},
    )
    store.call(
        bridge.home,
        actors["codex"],
        "request_reservation",
        {"paths": ["src/engine.py"]},
    )
    with store.connect(bridge.home, write=True) as db:
        db.execute(
            "UPDATE file_reservations SET expires_ts=datetime('now',"
            "'-60 seconds') WHERE agent_id=? AND released_ts IS NULL",
            (actors["claude"]["id"],),
        )
    return actors["claude"]


def expired(bridge, holder):
    """Counts the lane's expired unreleased leases."""
    with store.connect(bridge.home) as db:
        return db.execute(
            "SELECT count(*) FROM file_reservations WHERE agent_id=? AND "
            "released_ts IS NULL AND expires_ts<=CURRENT_TIMESTAMP",
            (holder["id"],),
        ).fetchone()[0]


def hooked(bridge, paired, event):
    """Runs one lifecycle checkpoint for claude."""
    directory = Path(paired["lanes"]["claude"]).parent
    write_json(directory / "claude-identity.json", {"name": "claude"})
    payload = {
        "hook_event_name": event,
        "session_id": "test",
        "cwd": paired["lanes"]["claude"],
        "tool_name": "Bash",
    }
    checkpoints.checkpoint(bridge.home, directory, "claude", payload)


def test_a_bare_stop_does_not_renew_an_expired_lease(bridge, repo, paired):
    holder = lease_holder(bridge, paired)
    hooked(bridge, paired, "Stop")
    assert expired(bridge, holder) == 1
    hooked(bridge, paired, "PreToolUse")
    assert expired(bridge, holder) == 0
