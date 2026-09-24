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


def unthrottle(bridge, paired, directory, name):
    """Ages a lane's recorded wake spacing and returns the new fields."""
    record = {**supervision.wake_record(bridge.home, paired["root"], name)}
    record["at"] = 0
    supervision.store_wake(bridge.home, directory, paired["root"], name, record)
    return record


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
    ("state", "result"),
    [
        (lanes.STARTING, None),
        (lanes.WORKING, None),
        (lanes.RECLAIMED, None),
        (lanes.IDLE, "accepted"),
        (lanes.STOPPED, supervision.WAKE_ATTENTION),
        (lanes.DEAD, supervision.WAKE_ATTENTION),
    ],
)
def test_a_wake_acts_only_on_a_lane_whose_state_allows_it(
    bridge, paired, monkeypatch, state, result
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
    parked = supervision.wake_record(bridge.home, paired["root"], "codex")
    assert parked.get("result") == result
    assert bool(calls) is (state == lanes.IDLE)


def test_a_blocked_lane_is_deferred_under_its_cause(
    bridge, paired, monkeypatch
):
    actors = registered(bridge, paired)
    directory = Path(paired["lanes"]["codex"]).parent
    idle_live(directory, "codex")
    mail(bridge, actors)
    place(bridge, paired, "codex", lanes.IDLE)
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
    unthrottle(bridge, paired, directory, "codex")
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
    place(bridge, paired, "codex", lanes.IDLE)
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
        unthrottle(bridge, paired, directory, "codex")
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


def test_wake_spacing_lives_in_the_lane_state_not_its_published_copy(
    bridge, paired, monkeypatch
):
    actors = registered(bridge, paired)
    directory = Path(paired["lanes"]["codex"]).parent
    idle_live(directory, "codex")
    mail(bridge, actors)
    place(bridge, paired, "codex", lanes.IDLE)
    calls = []
    monkeypatch.setattr(
        terminal, "request", lambda *args: calls.append(args) or "accepted"
    )
    config = {**supervision.DEFAULTS, "inactive_after": 1}
    observed = supervision.presence(directory, "codex", 1)
    supervision.wake(bridge.home, directory, paired, "codex", observed, config)
    assert len(calls) == 1
    path = directory / "codex-wake.json"
    write_json(path, {**json.loads(path.read_text()), "at": 0})
    supervision.wake(bridge.home, directory, paired, "codex", observed, config)
    assert len(calls) == 1
    with store.connect(bridge.home) as db:
        stored = lanes.read_wake(db, paired["root"], "codex")
    assert stored["attempts"] == 1
    assert stored["result"] == "accepted"


def test_an_activity_label_the_record_contradicts_never_decides_a_wake(
    bridge, paired, monkeypatch
):
    actors = registered(bridge, paired)
    directory = Path(paired["lanes"]["codex"]).parent
    idle_live(directory, "codex")
    activity = directory / "codex-activity.json"
    write_json(
        activity,
        {
            **json.loads(activity.read_text()),
            "activity": "waiting for approval",
        },
    )
    mail(bridge, actors)
    place(bridge, paired, "codex", lanes.IDLE)
    calls = []
    monkeypatch.setattr(
        terminal, "request", lambda *args: calls.append(args) or "accepted"
    )
    config = {**supervision.DEFAULTS, "inactive_after": 1}
    observed = supervision.presence(directory, "codex", 1)
    supervision.wake(bridge.home, directory, paired, "codex", observed, config)
    assert len(calls) == 1


def test_a_working_lane_with_no_process_identity_goes_idle_when_stale(
    bridge, paired, monkeypatch
):
    actors = registered(bridge, paired)
    directory = Path(paired["lanes"]["codex"]).parent
    write_json(
        directory / "codex-activity.json",
        {
            "activity": "working",
            "event": "PreToolUse",
            "updated": time.time() - supervision.TOOL_TIMEOUT - 100,
        },
    )
    lanes.submit(directory, "codex", "PreToolUse", lanes.WORKING)
    supervision.settle_evidence(bridge.home, directory, paired)
    assert recorded(bridge, paired, "codex")["state"] == lanes.WORKING
    mail(bridge, actors)
    config = {**supervision.DEFAULTS, "inactive_after": 1}
    readings = {
        name: supervision.presence(directory, name, 1)
        for name in paired["participants"]
    }
    assert readings["codex"]["process_alive"] is None
    supervision.settle_lanes(bridge.home, paired, config, readings)
    assert recorded(bridge, paired, "codex")["state"] == lanes.IDLE
    calls = []
    monkeypatch.setattr(
        terminal, "request", lambda *args: calls.append(args) or "accepted"
    )
    supervision.wake(
        bridge.home, directory, paired, "codex", readings["codex"], config
    )
    assert len(calls) == 1


def test_a_stale_blocked_lane_is_published_idle_and_loses_its_lease(
    bridge, paired
):
    holder = lease_holder(bridge, paired)
    directory = Path(paired["lanes"]["claude"]).parent
    idle_live(directory, "claude")
    place(bridge, paired, "claude", lanes.BLOCKED, lanes.APPROVAL)
    readings = {
        name: supervision.presence(directory, name, 1)
        for name in paired["participants"]
    }
    observations = supervision.recorded_observations(
        bridge.home, paired, readings
    )
    assert observations["claude"]["state"] == supervision.IDLE
    supervision._publish_presence(bridge.home, paired, observations)
    store.reclaim_expired(bridge.home, paired["root"])
    assert expired(bridge, holder) == 0


def test_hook_evidence_newer_than_the_poll_reading_is_kept(
    bridge, paired, monkeypatch
):
    registered(bridge, paired)
    directory = Path(paired["lanes"]["codex"]).parent
    idle_live(directory, "codex")
    activity = directory / "codex-activity.json"
    settle = supervision.settle_evidence

    def approval_first(home, state, manifest):
        """Serves a permission request just before the spool is drained."""
        write_json(
            activity,
            {
                **json.loads(activity.read_text()),
                "activity": "waiting for approval",
                "event": "PermissionRequest",
                "updated": time.time(),
            },
        )
        lanes.submit(
            directory,
            "codex",
            "PermissionRequest",
            lanes.BLOCKED,
            cause=lanes.APPROVAL,
        )
        settle(home, state, manifest)

    monkeypatch.setattr(supervision, "settle_evidence", approval_first)
    monkeypatch.setattr(terminal, "request", lambda *args: "accepted")
    supervision.poll(bridge.home, directory)
    record = recorded(bridge, paired, "codex")
    assert (record["state"], record["cause"]) == (lanes.BLOCKED, lanes.APPROVAL)


def test_an_upgrade_keeps_the_wake_budget_published_before_it(
    bridge, paired, monkeypatch
):
    actors = registered(bridge, paired)
    directory = Path(paired["lanes"]["codex"]).parent
    idle_live(directory, "codex")
    mail(bridge, actors)
    place(bridge, paired, "codex", lanes.IDLE)
    monkeypatch.setattr(terminal, "request", lambda *args: "accepted")
    config = {**supervision.DEFAULTS, "inactive_after": 1}
    observed = supervision.presence(directory, "codex", 1)
    supervision.wake(bridge.home, directory, paired, "codex", observed, config)
    first = supervision.wake_record(bridge.home, paired["root"], "codex")
    escalated = time.time() - 60
    write_json(
        directory / "codex-wake.json",
        {
            **first,
            "at": 0,
            "attempts": supervision.WORK_WAKE_ATTEMPTS,
            "exhausted_at": escalated,
            "escalated_at": escalated,
        },
    )
    with store.connect(bridge.home, write=True) as db:
        db.execute("DELETE FROM lane_wakes")
    supervision.wake(bridge.home, directory, paired, "codex", observed, config)
    stored = supervision.wake_record(bridge.home, paired["root"], "codex")
    assert stored["attempts"] > supervision.WORK_WAKE_ATTEMPTS
    assert stored["escalated_at"] == escalated
    assert stored["exhausted_at"] == escalated


def test_a_rebooted_record_is_not_woken_whatever_its_activity_file_says(
    bridge, paired, monkeypatch
):
    actors = registered(bridge, paired)
    directory = Path(paired["lanes"]["codex"]).parent
    idle_live(directory, "codex")
    mail(bridge, actors)
    write_json(directory / supervision.BOOT_RECORD, {"boot_id": "old"})
    monkeypatch.setattr(process, "boot_id", lambda: "new")
    assert supervision.settle_reboot(bridge.home, directory, paired) == [
        "codex"
    ]
    assert lanes.rebooted(recorded(bridge, paired, "codex"))
    idle_live(directory, "codex")
    calls = []
    monkeypatch.setattr(
        terminal, "request", lambda *args: calls.append(args) or "accepted"
    )
    config = {**supervision.DEFAULTS, "inactive_after": 1}
    observed = supervision.presence(directory, "codex", 1)
    supervision.wake(bridge.home, directory, paired, "codex", observed, config)
    assert not calls
    assert supervision.wake_record(bridge.home, paired["root"], "codex") == {}


def test_acknowledgement_debt_follows_the_record_not_the_activity_file(
    bridge, paired
):
    actors = registered(bridge, paired)
    directory = Path(paired["lanes"]["codex"]).parent
    idle_live(directory, "codex")
    mail(bridge, actors)
    place(bridge, paired, "codex", lanes.WORKING)
    check = supervision._mail_check(
        bridge.home, directory, paired, "codex", 0, 1
    )
    assert check[0] is False
    with store.connect(bridge.home, write=True) as db:
        lanes.transition(
            db, paired["root"], "codex", lanes.IDLE, now=time.time() - 10
        )
    check = supervision._mail_check(
        bridge.home, directory, paired, "codex", 0, 1
    )
    assert check == (True, "")


def test_status_reads_the_lane_condition_from_its_record(bridge, paired):
    registered(bridge, paired)
    directory = Path(paired["lanes"]["codex"]).parent
    write_json(
        directory / "codex-activity.json",
        {
            "activity": "working",
            "updated": time.time(),
            "session_pid": os.getpid(),
            "session_ticks": process.start_ticks(os.getpid()),
        },
    )
    place(bridge, paired, "codex", lanes.DEAD)
    participants = bridge.status_snapshot()["projects"][0]["participants"]
    codex = next(
        record for record in participants if record["participant"] == "codex"
    )
    assert codex["availability"]["state"] == supervision.STOPPED
    assert codex["availability"]["process_alive"] is False
    assert codex["session"].startswith("dead")


def test_fit_reads_the_lane_state_not_the_activity_file(bridge, paired):
    registered(bridge, paired)
    directory = Path(paired["lanes"]["codex"]).parent
    killed(directory, "codex", 10)
    place(bridge, paired, "codex", lanes.WORKING)
    working = supervision.fit(bridge.home, directory, paired, "codex", STALLED)
    assert working["checks"]["session"] is True
    place(bridge, paired, "codex", lanes.BLOCKED, lanes.DIALOG)
    blocked = supervision.fit(bridge.home, directory, paired, "codex", STALLED)
    assert blocked["checks"]["session"] is False
    assert "is blocked (dialog)" in blocked["reason"]


def test_a_lane_without_a_record_is_still_orphaned(bridge, repo, paired):
    registered(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    directory = lane.parent
    bridge.issue(lane, "claim", "42")
    killed(directory, "claude", STALLED + 100)
    assert recorded(bridge, paired, "claude") is None
    config = supervision.configuration(bridge.home, paired)
    supervision.orphans(bridge.home, directory, paired, config)
    assert issues.snapshot(directory)["issues"]["42"]["orphan"]["owner"] == (
        "claude"
    )


def test_a_lane_without_a_record_is_still_woken(bridge, paired, monkeypatch):
    actors = registered(bridge, paired)
    directory = Path(paired["lanes"]["codex"]).parent
    idle_live(directory, "codex")
    mail(bridge, actors)
    assert recorded(bridge, paired, "codex") is None
    calls = []
    monkeypatch.setattr(
        terminal, "request", lambda *args: calls.append(args) or "accepted"
    )
    config = {**supervision.DEFAULTS, "inactive_after": 1}
    observed = supervision.presence(directory, "codex", 1)
    supervision.wake(bridge.home, directory, paired, "codex", observed, config)
    assert len(calls) == 1
