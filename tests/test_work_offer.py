"""Checks the capacity gate and the advisory work offers built on it."""

import json
import os
import time
from pathlib import Path

import pytest

from agent_parley import (
    dashboard,
    issues,
    records,
    store,
    supervision,
    views,
)
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


def refused(paired, name, ago):
    """Writes a usage refusal into the lane's own client session record."""
    lane = Path(paired["lanes"][name])
    directory = (
        Path(os.environ["CLAUDE_CONFIG_DIR"])
        / "projects"
        / records.UNSAFE.sub("-", str(lane))
    )
    directory.mkdir(parents=True, exist_ok=True)
    moment = time.time() - ago
    (directory / "session.jsonl").write_text(
        json.dumps(
            {
                "type": "assistant",
                "isApiErrorMessage": True,
                "timestamp": time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(moment)
                ),
                "message": {
                    "content": [
                        {"type": "text", "text": "API Error: rate limit"}
                    ]
                },
            }
        )
        + "\n"
    )


def offer_for(directory, name):
    """Returns the advisory offer published for one lane, if any."""
    return supervision.published_work(directory, name)["offer"]


def test_unclaimed_work_is_ordered_by_the_peers_that_wait_on_it(
    bridge, repo, paired
):
    lane = Path(paired["lanes"]["claude"])
    peer = Path(paired["lanes"]["codex"])
    for number in ("4", "7", "9"):
        bridge.issue(lane, "claim", number)
        bridge.issue(lane, "release", number)
    bridge.issue(peer, "claim", "2")
    bridge.issue(peer, "block", "2", on="9")
    bridge.issue(lane, "claim", "5")
    bridge.issue(lane, "block", "5", on="4")
    bridge.issue(lane, "release", "5")
    ledger = issues.snapshot(lane.parent)
    assert issues.unclaimed(ledger) == ["9", "4", "7"]
    assert issues.holders(ledger) == {"codex": ["2"]}


def test_a_fit_idle_lane_is_offered_unclaimed_and_shed_able_work(
    bridge, repo, paired
):
    registered(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    peer = Path(paired["lanes"]["codex"])
    directory = lane.parent
    alive(directory, "claude")
    bridge.issue(lane, "claim", "9")
    bridge.issue(lane, "release", "9")
    bridge.issue(peer, "claim", "2")
    bridge.issue(peer, "claim", "3")
    supervision.poll(bridge.home, directory)
    published = supervision.published_work(directory, "claude")
    assert published["fit"] is True
    assert published["failed"] == []
    assert published["offer"]["kind"] == "pull"
    assert "#9" in published["offer"]["text"]
    assert "codex" in published["offer"]["text"]


def test_a_recent_usage_refusal_makes_a_lane_unfit_and_unoffered(
    bridge, repo, paired
):
    registered(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    peer = Path(paired["lanes"]["codex"])
    directory = lane.parent
    alive(directory, "claude")
    bridge.issue(peer, "claim", "2")
    bridge.issue(peer, "claim", "3")
    refused(paired, "claude", 30)
    supervision.poll(bridge.home, directory)
    published = supervision.published_work(directory, "claude")
    assert published["fit"] is False
    assert published["failed"] == ["capacity"]
    assert published["checks"]["capacity"] is False
    assert "refused a request" in published["reason"]
    assert published["offer"] is None


def test_a_refusal_older_than_the_interval_no_longer_blocks_an_offer(
    bridge, repo, paired
):
    registered(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    peer = Path(paired["lanes"]["codex"])
    directory = lane.parent
    alive(directory, "claude")
    bridge.issue(peer, "claim", "2")
    bridge.issue(peer, "claim", "3")
    refused(paired, "claude", 4000)
    supervision.poll(bridge.home, directory)
    published = supervision.published_work(directory, "claude")
    assert published["checks"]["capacity"] is True
    assert published["fit"] is True
    assert published["offer"]["kind"] == "pull"


def test_a_provider_that_publishes_nothing_skips_the_capacity_check(
    bridge, repo, paired
):
    registered(bridge, paired)
    directory = Path(paired["lanes"]["claude"]).parent
    alive(directory, "claude")
    supervision.poll(bridge.home, directory)
    published = supervision.published_work(directory, "claude")
    assert published["checks"]["capacity"] is None
    assert published["fit"] is True


def test_a_stopped_lane_is_unfit_and_never_named_by_a_rebalance(
    bridge, repo, paired
):
    registered(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    directory = lane.parent
    alive(directory, "codex", activity="stopped")
    turn_ended(directory, "codex", 3600)
    bridge.issue(lane, "claim", "2")
    bridge.issue(lane, "claim", "3")
    supervision.poll(bridge.home, directory)
    assert supervision.published_work(directory, "codex")["failed"] == [
        "session"
    ]
    assert offer_for(directory, "claude") is None


def test_a_busy_lane_is_told_which_fit_peer_has_been_idle(bridge, repo, paired):
    registered(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    directory = lane.parent
    alive(directory, "codex")
    turn_ended(directory, "codex", 3600)
    bridge.issue(lane, "claim", "2")
    bridge.issue(lane, "claim", "3")
    supervision.poll(bridge.home, directory)
    assert supervision.idle_seconds(directory, "codex") >= 3600
    offer = offer_for(directory, "claude")
    assert offer["kind"] == "rebalance"
    assert "codex" in offer["text"]
    assert "#2" in offer["text"] and "#3" in offer["text"]
    assert issues.snapshot(directory)["issues"]["2"]["owner"] == "claude"


def test_the_checkpoint_carries_one_offer_and_then_stays_quiet(
    bridge, repo, paired
):
    registered(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    peer = Path(paired["lanes"]["codex"])
    directory = lane.parent
    alive(directory, "claude")
    write_json(directory / "claude-identity.json", {"name": "claude"})
    bridge.issue(peer, "claim", "2")
    bridge.issue(peer, "claim", "3")
    supervision.poll(bridge.home, directory)
    stop = {
        "hook_event_name": "Stop",
        "session_id": "claude-work",
        "cwd": str(lane),
    }
    first = checkpoint(bridge.home, directory, "claude", stop)
    assert first["decision"] == "block"
    assert "Work offer" in first["reason"]
    assert len(first["reason"].encode()) <= 1536
    assert checkpoint(bridge.home, directory, "claude", stop) == {}


def test_top_reports_the_fit_result_and_a_pending_offer(bridge, repo, paired):
    registered(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    peer = Path(paired["lanes"]["codex"])
    directory = lane.parent
    alive(directory, "claude")
    bridge.issue(peer, "claim", "2")
    bridge.issue(peer, "claim", "3")
    supervision.poll(bridge.home, directory)
    view = dashboard.collect(bridge.home, False, {})
    rows = {row["participant"]: row for row in view["projects"][0]["rows"]}
    assert rows["claude"]["fit"] is True
    assert rows["claude"]["work_offer"] is True
    assert rows["claude"]["offer_kind"] == "pull"
    lines = "\n".join(dashboard.render(view))
    assert "FIT" in lines
    assert "pull offer pending" in lines
    reported = views.frame(view)["projects"][0]["participants"]
    assert reported[0]["fit"] is True
    assert reported[0]["work_offer"] == "pull"
