"""Pins the one authoritative lane state record and its transitions."""

import itertools

import pytest

from agent_parley import lanes, store

ROOT = "/repo"


@pytest.fixture
def home(tmp_path):
    """Creates an initialized coordination store."""
    path = tmp_path / "state"
    path.mkdir()
    store.initialize(path)
    return path


def cause_for(state):
    """Names the cause a state must carry to be valid."""
    return lanes.APPROVAL if state == lanes.BLOCKED else ""


def placed(home, state):
    """Puts the lane into one state through a legal path from no record."""
    with store.connect(home, write=True) as db:
        if state in lanes.TRANSITIONS[""]:
            lanes.transition(db, ROOT, "claude", state, cause=cause_for(state))
        else:
            lanes.transition(db, ROOT, "claude", lanes.STOPPED)
            lanes.transition(db, ROOT, "claude", state)


PAIRS = list(itertools.product(lanes.STATES, lanes.STATES))


@pytest.mark.parametrize(("source", "target"), PAIRS)
def test_every_transition_follows_the_table(home, source, target):
    placed(home, source)
    with store.connect(home, write=True) as db:
        result = lanes.transition(
            db,
            ROOT,
            "claude",
            target,
            cause=cause_for(target),
            evidence="observed",
            now=10**10,
        )
        record = lanes.read(db, ROOT, "claude")
        refused = lanes.history(db, ROOT, "claude", "refused")
    legal = target in lanes.TRANSITIONS[source]
    assert result["accepted"] is legal
    assert result["source"] == source
    if legal:
        assert record["state"] == target
        assert not refused
    else:
        assert record["state"] == source
        assert refused[-1]["source"] == source
        assert refused[-1]["target"] == target


def test_a_first_record_cannot_start_reclaimed(home):
    with store.connect(home, write=True) as db:
        result = lanes.transition(db, ROOT, "claude", lanes.RECLAIMED)
        assert not result["accepted"]
        assert lanes.read(db, ROOT, "claude") is None


def test_a_dead_lane_does_not_return_to_work_without_a_start(home):
    placed(home, lanes.DEAD)
    with store.connect(home, write=True) as db:
        assert not lanes.transition(db, ROOT, "claude", lanes.WORKING)[
            "accepted"
        ]
        assert lanes.transition(db, ROOT, "claude", lanes.STARTING)["accepted"]
        assert lanes.transition(db, ROOT, "claude", lanes.WORKING)["accepted"]


def test_blocked_names_a_cause_and_nothing_else_does(home):
    with store.connect(home, write=True) as db:
        with pytest.raises(ValueError):
            lanes.transition(db, ROOT, "claude", lanes.BLOCKED)
        with pytest.raises(ValueError):
            lanes.transition(db, ROOT, "claude", lanes.IDLE, cause="dialog")
        with pytest.raises(ValueError):
            lanes.transition(db, ROOT, "claude", "asleep")


def test_a_repeated_observation_changes_nothing_but_its_time(home):
    with store.connect(home, write=True) as db:
        lanes.transition(db, ROOT, "claude", lanes.IDLE, now=100.0)
        again = lanes.transition(db, ROOT, "claude", lanes.IDLE, now=160.0)
        record = lanes.read(db, ROOT, "claude")
        events = lanes.history(db, ROOT, "claude")
    assert again["accepted"] and not again["changed"]
    assert record["since"] == 100.0
    assert record["updated"] == 160.0
    assert len(events) == 1


def test_a_new_blocked_cause_is_a_recorded_transition(home):
    with store.connect(home, write=True) as db:
        lanes.transition(db, ROOT, "claude", lanes.BLOCKED, cause="approval")
        moved = lanes.transition(
            db, ROOT, "claude", lanes.BLOCKED, cause="capacity"
        )
        assert moved["changed"]
        assert lanes.read(db, ROOT, "claude")["cause"] == "capacity"


def test_a_session_change_keeps_the_lane_and_records_both_ids(home):
    with store.connect(home, write=True) as db:
        lanes.transition(db, ROOT, "claude", lanes.WORKING, session="one")
        moved = lanes.transition(
            db, ROOT, "claude", lanes.WORKING, session="two"
        )
        record = lanes.read(db, ROOT, "claude")
        last = lanes.history(db, ROOT, "claude")[-1]
    assert moved["changed"]
    assert record["session"] == "two"
    assert last["detail"] == "session one -> two"


def test_a_store_without_lane_states_reads_no_record(home):
    with store.connect(home) as db:
        assert lanes.read(db, ROOT, "claude") is None
        assert lanes.read_all(db, ROOT) == {}
        assert lanes.history(db, ROOT) == []


def test_status_reports_the_lane_condition_from_the_record(bridge, paired):
    store.initialize(bridge.home)
    root = paired["root"]
    with store.connect(bridge.home, write=True) as db:
        lanes.transition(
            db, root, "claude", lanes.BLOCKED, cause=lanes.APPROVAL
        )
    lanes_by_name = {
        lane["participant"]: lane
        for lane in bridge.status_snapshot()["projects"][0]["participants"]
    }
    claude = lanes_by_name["claude"]
    assert claude["condition"]["state"] == lanes.BLOCKED
    assert claude["condition"]["cause"] == lanes.APPROVAL
    assert claude["session"].startswith("blocked: approval ")
    assert lanes_by_name["codex"]["condition"] is None


def test_describe_names_state_cause_and_span():
    record = {"state": "blocked", "cause": "capacity", "since": 0.0}
    assert lanes.describe(record, now=42 * 60) == "blocked: capacity 42m"
    assert lanes.describe({**record, "state": "idle", "cause": ""}, 30) == (
        "idle 30s"
    )
