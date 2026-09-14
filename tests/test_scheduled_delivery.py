"""Checks operator items delivered at a time or on a recorded condition."""

import time

import pytest

from agent_parley import store, supervision
from agent_parley.state import BridgeError, write_json


def ready(bridge, paired):
    """Registers the claude lane with the coordination store."""
    store.initialize(bridge.home)
    for participant in ("claude", "codex"):
        store.register(bridge.home, paired["root"], participant)
    return paired["participants"]["claude"]["display"]


def inbox(bridge, paired, name):
    """Returns the oldest item the claude lane is waiting on."""
    return store.waiting(bridge.home, paired["root"], name)


def test_a_message_whose_time_passed_is_delivered_by_the_poll(
    bridge, repo, paired
):
    name = ready(bridge, paired)
    directory = bridge.project(repo)[1]
    recorded = bridge.say(
        repo, "claude", "Rebase before you open it.", at=time.time() - 1
    )
    assert recorded["kind"] == "message"
    assert inbox(bridge, paired, name)["kind"] is None
    supervision.poll(bridge.home, directory)
    waiting = inbox(bridge, paired, name)
    assert waiting["kind"] == "unread"
    assert waiting["sender"] == "operator"
    assert store.schedules(bridge.home, paired["root"]) == []


def test_a_message_waits_until_its_time_arrives(bridge, repo, paired):
    name = ready(bridge, paired)
    directory = bridge.project(repo)[1]
    recorded = bridge.say(repo, "claude", "Later, not now.", after=3600)
    supervision.poll(bridge.home, directory)
    assert inbox(bridge, paired, name)["kind"] is None
    pending = store.schedules(bridge.home, paired["root"])
    assert [item["id"] for item in pending] == [recorded["id"]]
    assert pending[0]["not_before"] > time.time()
    lanes = bridge.status_snapshot()["projects"][0]["participants"]
    counted = {lane["participant"]: lane["mail"] for lane in lanes}
    assert counted["claude"]["pending_operator_items"] == 1
    assert counted["codex"]["pending_operator_items"] == 0


def test_a_released_issue_releases_the_message_that_waited_on_it(
    bridge, repo, paired
):
    name = ready(bridge, paired)
    directory = bridge.project(repo)[1]
    lane = paired["lanes"]["claude"]
    bridge.issue(lane, "claim", "17")
    bridge.say(repo, "claude", "Pick up 18 next.", when_released="17")
    supervision.poll(bridge.home, directory)
    assert inbox(bridge, paired, name)["kind"] is None
    bridge.issue(lane, "release", "17")
    supervision.poll(bridge.home, directory)
    assert inbox(bridge, paired, name)["kind"] == "unread"
    assert store.schedules(bridge.home, paired["root"]) == []


def test_a_report_drops_a_delayed_message_recorded_with_the_opt_out(
    bridge, repo, paired
):
    name = ready(bridge, paired)
    directory = bridge.project(repo)[1]
    bridge.say(
        repo, "claude", "Report in, please.", after=3600, unless_reported=True
    )
    write_json(
        directory / "claude-activity.json", {"reported_at": time.time() + 1}
    )
    supervision.poll(bridge.home, directory)
    assert inbox(bridge, paired, name)["kind"] is None
    assert store.schedules(bridge.home, paired["root"]) == []


def test_a_bounded_repeat_counts_its_deliveries_and_stops(bridge, repo, paired):
    ready(bridge, paired)
    directory = bridge.project(repo)[1]
    base = time.time()
    recorded = bridge.say(
        repo,
        "claude",
        "Status, please.",
        at=base + 60,
        every=60,
        until=base + 300,
    )
    assert recorded["repeats_left"] == 5
    with store.connect(bridge.home, write=True) as db:
        db.execute(
            "UPDATE scheduled_deliveries SET not_before=? WHERE id=?",
            (time.time() - 1, recorded["id"]),
        )
    supervision.poll(bridge.home, directory)
    pending = store.schedules(bridge.home, paired["root"])
    assert len(pending) == 1
    assert pending[0]["repeats_left"] == 4
    assert pending[0]["sequence"] == 1


def test_an_unbounded_or_contradictory_repeat_is_refused(bridge, repo, paired):
    ready(bridge, paired)
    with pytest.raises(BridgeError, match="must be bounded"):
        bridge.say(repo, "claude", "Forever.", every=60)
    with pytest.raises(BridgeError, match="bounds a repeat"):
        bridge.say(repo, "claude", "Nothing repeats.", until=time.time() + 60)
    with pytest.raises(BridgeError, match="delayed message"):
        bridge.say(repo, "claude", "Now or never.", unless_reported=True)


def test_a_pending_item_is_listed_and_cancelled(bridge, repo, paired):
    name = ready(bridge, paired)
    directory = bridge.project(repo)[1]
    recorded = bridge.say(repo, "claude", "Drop me.", after=3600)
    listed = bridge.mail(repo, "pending")["pending"]
    assert [item["id"] for item in listed] == [recorded["id"]]
    assert listed[0]["recipient"] == "claude"
    assert bridge.mail(repo, "cancel", identifier=recorded["id"]) == {
        "id": recorded["id"],
        "cancelled": True,
    }
    assert bridge.mail(repo, "pending")["pending"] == []
    supervision.poll(bridge.home, directory)
    assert inbox(bridge, paired, name)["kind"] is None


def test_a_recorded_offer_is_applied_once_its_blocker_is_released(
    bridge, repo, paired
):
    ready(bridge, paired)
    directory = bridge.project(repo)[1]
    claude = paired["lanes"]["claude"]
    codex = paired["lanes"]["codex"]
    bridge.issue(claude, "claim", "21")
    bridge.issue(codex, "claim", "22")
    bridge.issue(
        claude,
        "offer",
        "21",
        to="codex",
        summary="Blocked until 22 clears.",
        when_released="22",
    )
    supervision.poll(bridge.home, directory)
    ledger = bridge.issue(repo, "list")["issues"]
    assert ledger["21"]["offer"] is None
    bridge.issue(codex, "release", "22")
    supervision.poll(bridge.home, directory)
    ledger = bridge.issue(repo, "list")["issues"]
    assert ledger["21"]["offer"]["to"] == "codex"
    assert store.schedules(bridge.home, paired["root"]) == []


def test_a_migrated_store_reports_the_current_schema(bridge, repo, paired):
    ready(bridge, paired)
    with store.connect(bridge.home) as db:
        version = db.execute("PRAGMA user_version").fetchone()[0]
    assert store.schema_state(version) == store.SCHEMA_CURRENT
