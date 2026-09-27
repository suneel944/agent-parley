"""Derives idle lane-minutes and unaccountable claim-minutes from states."""

import json
from pathlib import Path

import pytest

from agent_parley import dashboard, lanes, store, supervision

ROOT = "/repo"


@pytest.fixture
def home(tmp_path):
    """Creates an initialized coordination store."""
    path = tmp_path / "state"
    path.mkdir()
    store.initialize(path)
    return path


def script(home, steps, lane="a"):
    """Plays (time, state, cause, has_work, owns) steps through one poll each.

    Returns:
        The lane's totals after the last step and its accounting events.
    """
    totals = None
    with store.connect(home, write=True) as db:
        for now, state, cause, has_work, owns in steps:
            lanes.transition(db, ROOT, lane, state, cause=cause, now=now)
            totals = lanes.account(
                db,
                ROOT,
                lane,
                lanes.read(db, ROOT, lane),
                has_work=has_work,
                owns=owns,
                now=now,
            )
        events = lanes.history(db, ROOT, lane, "accounting")
    return totals, events


BLOCKED = (lanes.BLOCKED, lanes.CAPACITY)
STEPS = [
    (0.0, lanes.WORKING, "", True, True),
    (60.0, lanes.WORKING, "", True, True),
    (90.0, *BLOCKED, True, True),
    (240.0, *BLOCKED, True, True),
    (300.0, lanes.IDLE, "", False, False),
    (360.0, lanes.IDLE, "", False, False),
]


def test_a_scripted_sequence_yields_both_numbers_and_their_causes(home):
    totals, events = script(home, STEPS)
    report = lanes.summary(totals)
    assert totals["observed"] == 360.0
    assert totals["idle"] == 210.0
    assert totals["unaccountable"] == 210.0
    assert report == {
        "observed_minutes": 6.0,
        "idle_minutes": 3.5,
        "idle_per_lane_hour": 35.0,
        "idle_cause": "blocked: capacity, 4 min",
        "unaccountable_minutes": 3.5,
        "unaccountable_cause": "blocked: capacity, 4 min",
    }
    assert [
        json.loads(event["detail"])["idle_minutes"] for event in events
    ] == [2.5, 3.5]
    last = json.loads(events[-1]["detail"])
    assert last["idle_per_lane_hour"] == 42.0
    assert events[-1]["evidence"] == lanes.describe_account(last)


def test_an_owned_claim_on_an_idle_lane_is_unaccountable_time(home):
    totals, _ = script(
        home,
        [
            (0.0, lanes.IDLE, "", True, True),
            (120.0, lanes.IDLE, "", True, True),
            (180.0, lanes.WORKING, "", True, True),
        ],
    )
    assert totals["observed"] == 180.0
    assert totals["unaccountable_causes"] == {"idle": 180.0}
    assert totals["idle_causes"] == {"idle": 180.0}


def test_an_idle_lane_with_no_work_anywhere_is_not_idle_time(home):
    totals, events = script(
        home,
        [
            (0.0, lanes.IDLE, "", False, False),
            (200.0, lanes.IDLE, "", False, False),
        ],
    )
    assert totals["observed"] == 200.0
    assert totals["idle"] == 0.0 and totals["unaccountable"] == 0.0
    assert events == []


def test_a_span_longer_than_the_gap_is_charged_to_nothing(home):
    totals, _ = script(
        home,
        [
            (0.0, lanes.STOPPED, "", True, True),
            (lanes.ACCOUNT_GAP + 1, lanes.STOPPED, "", True, True),
        ],
    )
    assert totals["observed"] == 0.0


def test_a_lane_without_a_record_is_not_accounted(home):
    with store.connect(home, write=True) as db:
        assert (
            lanes.account(db, ROOT, "a", None, has_work=True, owns=True) is None
        )
        assert lanes.read_accounts(db, ROOT) == {}


def test_the_project_numbers_merge_lanes_and_name_the_lane(home):
    script(home, STEPS, "claude")
    script(home, [(0.0, lanes.WORKING, "", True, False)], "codex")
    with store.connect(home) as db:
        accounts = lanes.read_accounts(db, ROOT)
    report = lanes.summary(lanes.combine(accounts))
    assert report["idle_cause"] == "claude blocked: capacity, 4 min"
    assert report["idle_minutes"] == 3.5


def test_status_shows_the_accounting_of_each_lane_and_project(
    bridge, paired, capsys
):
    store.initialize(bridge.home)
    root = paired["root"]
    with store.connect(bridge.home, write=True) as db:
        for now, state, cause, has_work, owns in STEPS:
            lanes.transition(db, root, "claude", state, cause=cause, now=now)
            lanes.account(
                db,
                root,
                "claude",
                lanes.read(db, root, "claude"),
                has_work=has_work,
                owns=owns,
                now=now,
            )
    project = bridge.status_snapshot()["projects"][0]
    by_name = {lane["participant"]: lane for lane in project["participants"]}
    assert by_name["claude"]["accounting"]["idle_per_lane_hour"] == 35.0
    assert by_name["codex"]["accounting"] is None
    assert project["accounting"]["unaccountable_cause"] == (
        "claude blocked: capacity, 4 min"
    )
    bridge.status()
    assert (
        "Lanes: idle 35.0 min/lane-hour (top: claude blocked: capacity, 4 min)"
        in capsys.readouterr().out
    )


def test_top_shows_the_accounting_of_each_lane(bridge, paired):
    store.initialize(bridge.home)
    root = paired["root"]
    with store.connect(bridge.home, write=True) as db:
        for now, state, cause, has_work, owns in STEPS:
            lanes.transition(db, root, "claude", state, cause=cause, now=now)
            lanes.account(
                db,
                root,
                "claude",
                lanes.read(db, root, "claude"),
                has_work=has_work,
                owns=owns,
                now=now,
            )
    view = dashboard.collect(bridge.home, False, {})
    rows = {row["participant"]: row for row in view["projects"][0]["rows"]}
    unaccountable = rows["claude"]["accounting"]["unaccountable_minutes"]
    lines = dashboard.render(view)
    header = next(line for line in lines if "PARTICIPANT" in line)
    claude = next(line for line in lines if line.startswith("claude"))
    assert "UNUSED" in header
    assert f"35.0/{unaccountable}" in claude
    assert rows["codex"]["accounting"] is None


def test_a_poll_accounts_an_owned_claim(bridge, repo, paired):
    store.initialize(bridge.home)
    lane = Path(paired["lanes"]["claude"])
    bridge.issue(lane, "claim", "42")
    with store.connect(bridge.home, write=True) as db:
        lanes.transition(db, paired["root"], "claude", lanes.IDLE)
    supervision.account_lanes(bridge.home, lane.parent, paired)
    with store.connect(bridge.home) as db:
        accounts = lanes.read_accounts(db, paired["root"])
    assert accounts["claude"]["owns"] is True
    assert accounts["claude"]["has_work"] is True
    assert accounts["claude"]["state"] == lanes.IDLE
    assert "codex" not in accounts
