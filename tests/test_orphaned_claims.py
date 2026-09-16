"""Checks how a dead lane's claims are marked, announced and taken over."""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent_parley import (
    dashboard,
    issues,
    process,
    store,
    supervision,
    tables,
)
from agent_parley.state import BridgeError, write_json

STALLED = supervision.DEFAULTS["stalled_after"]


def registered(bridge, paired):
    """Registers both lanes so mail and reservations can be recorded."""
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


def running(directory, name, age=0.0):
    """Records a live session process for one lane."""
    write_json(
        directory / f"{name}-activity.json",
        {
            "activity": "working",
            "updated": time.time() - age,
            "session_pid": os.getpid(),
            "session_ticks": process.start_ticks(os.getpid()),
        },
    )


def killed(directory, name, age):
    """Records a session process that started, was killed and then aged."""
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
            "session_pid": child.pid,
            "session_ticks": ticks,
        },
    )
    return child.pid


def reserve(bridge, actor, path):
    """Reserves one path for a lane through the served call path."""
    return store.call(
        bridge.home,
        actor,
        "file_reservation_paths",
        {"paths": [path], "reason": "orphan fixture"},
    )


def inbox(bridge, paired, name):
    """Returns the subjects and previews waiting for one lane."""
    listed = store.list_messages(bridge.home, paired["root"], name)
    return [
        (message["subject"], message["body_md"])
        for message in listed["messages"]
    ]


def test_a_killed_lane_is_orphaned_announced_and_taken_by_a_peer(
    bridge, repo, paired
):
    actors = registered(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    peer = Path(paired["lanes"]["codex"])
    directory = lane.parent
    bridge.issue(lane, "claim", "42")
    reserve(bridge, actors["claude"], "src/app.py")
    killed(directory, "claude", STALLED + 100)
    running(directory, "codex")

    supervision.poll(bridge.home, directory)

    record = issues.snapshot(directory)["issues"]["42"]
    assert record["owner"] == "claude"
    assert record["orphan"]["owner"] == "claude"
    assert record["orphan"]["reservations"] == ["src/app.py"]
    assert "no running session process" in record["orphan"]["reason"]
    listing = issues.describe(issues.snapshot(directory))
    assert "orphaned" in listing and "--take-orphaned" in listing
    subject, body = inbox(bridge, paired, "codex")[0]
    assert subject == "Orphaned claims held by claude"
    assert "#42" in body and "src/app.py" in body
    assert not inbox(bridge, paired, "claude")
    held = bridge.status_snapshot()["projects"][0]["participants"]
    reported = {lane["participant"]: lane for lane in held}
    assert reported["claude"]["claims"][0]["orphaned"] is True
    assert "#42*" in tables.status_row(reported["claude"], ())
    rows = {
        row["participant"]: row
        for row in dashboard.collect(bridge.home, False, {})["projects"][0][
            "rows"
        ]
    }
    assert rows["claude"]["orphaned"] == ["42"]
    assert "#42*" in rows["claude"]["issues"]
    assert "orphaned claims #42" in rows["claude"]["orphan"]

    taken = bridge.issue(peer, "claim", "42", take_orphaned=True)

    assert taken["owner"] == "codex"
    assert taken["taken"]["from"] == "claude"
    assert "no running session process" in taken["taken"]["reason"]
    assert taken["reservations_released"] == ["src/app.py"]
    assert "orphan" not in taken
    assert taken["history"][-1]["action"] == "take"
    assert store.active_reservations(bridge.home, paired["root"]) == {}


def test_a_live_but_idle_lane_is_never_orphaned(bridge, repo, paired):
    registered(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    directory = lane.parent
    bridge.issue(lane, "claim", "42")
    running(directory, "claude", STALLED + 100)
    running(directory, "codex")

    supervision.poll(bridge.home, directory)

    assert "orphan" not in issues.snapshot(directory)["issues"]["42"]
    assert not inbox(bridge, paired, "codex")


def test_a_lane_gone_for_less_than_the_threshold_is_never_orphaned(
    bridge, repo, paired
):
    registered(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    directory = lane.parent
    bridge.issue(lane, "claim", "42")
    killed(directory, "claude", STALLED - 60)
    running(directory, "codex")

    supervision.poll(bridge.home, directory)

    assert "orphan" not in issues.snapshot(directory)["issues"]["42"]


def test_the_notice_is_recorded_once_for_one_orphaning(bridge, repo, paired):
    registered(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    directory = lane.parent
    bridge.issue(lane, "claim", "42")
    killed(directory, "claude", STALLED + 100)
    running(directory, "codex")

    supervision.poll(bridge.home, directory)
    revision = issues.snapshot(directory)["revision"]
    supervision.poll(bridge.home, directory)

    assert issues.snapshot(directory)["revision"] == revision
    assert len(inbox(bridge, paired, "codex")) == 1


def test_a_live_owner_is_never_taken_from(bridge, repo, paired):
    lane = Path(paired["lanes"]["claude"])
    peer = Path(paired["lanes"]["codex"])
    bridge.issue(lane, "claim", "42")

    with pytest.raises(BridgeError) as refusal:
        bridge.issue(peer, "claim", "42", take_orphaned=True)

    assert "does not read as orphaned" in str(refusal.value)
    assert issues.snapshot(lane.parent)["issues"]["42"]["owner"] == "claude"


def test_an_unclaimed_issue_is_never_taken(bridge, repo, paired):
    peer = Path(paired["lanes"]["codex"])

    with pytest.raises(BridgeError) as refusal:
        bridge.issue(peer, "claim", "42", take_orphaned=True)

    assert "no orphaned owner" in str(refusal.value)


def test_a_peer_without_the_flag_is_told_how_to_take_it(bridge, repo, paired):
    registered(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    peer = Path(paired["lanes"]["codex"])
    directory = lane.parent
    bridge.issue(lane, "claim", "42")
    killed(directory, "claude", STALLED + 100)
    running(directory, "codex")
    supervision.poll(bridge.home, directory)

    with pytest.raises(BridgeError) as refusal:
        bridge.issue(peer, "claim", "42")

    assert "--take-orphaned" in str(refusal.value)
    assert issues.snapshot(directory)["issues"]["42"]["owner"] == "claude"


def test_a_returning_owner_regains_nothing_without_claiming_again(
    bridge, repo, paired
):
    registered(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    directory = lane.parent
    bridge.issue(lane, "claim", "42")
    killed(directory, "claude", STALLED + 100)
    running(directory, "codex")
    supervision.poll(bridge.home, directory)
    marked = issues.snapshot(directory)["issues"]["42"]

    running(directory, "claude")
    supervision.poll(bridge.home, directory)
    restarted = issues.snapshot(directory)["issues"]["42"]
    assert restarted["orphan"]["id"] == marked["orphan"]["id"]

    reclaimed = bridge.issue(lane, "claim", "42")
    assert "orphan" not in reclaimed
    assert reclaimed["claim_id"] != marked["claim_id"]
    assert reclaimed["history"][-1]["action"] == "claim"
