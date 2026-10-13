"""Checks that supervision returns the work of a lane proved dead."""

import os
import time
from pathlib import Path

from agent_parley import issues, lanes, lifecycle, store, supervision
from agent_parley.state import write_json

CEILING = supervision.DEFAULTS["orphan_retire_after"]


def actors(bridge, paired):
    """Registers both lanes and resolves their store identities."""
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


def died(bridge, paired, name, ago):
    """Records a lane state of `dead` that began `ago` seconds back."""
    with store.connect(bridge.home, write=True) as db:
        lanes.transition(
            db,
            paired["root"],
            name,
            lanes.DEAD,
            cause="test",
            evidence="no session process",
            now=time.time() - ago,
        )


def live(directory, name):
    """Records a live native checkpoint for a lane."""
    write_json(
        directory / f"{name}-activity.json",
        {"activity": "idle", "updated": time.time()},
    )


def edit(directory, number, change):
    """Rewrites one ledger record in place, as elapsed time would."""
    ledger = issues.snapshot(directory)
    change(ledger["issues"][number])
    write_json(directory / "issues.json", ledger)


def ended(record):
    """Marks a claim's issue closed on the forge with an open reminder."""
    record["handoff_prompt"] = {
        "trigger": issues.ENDED,
        "holder": record["owner"],
        "created": time.time(),
        "reminders": 3,
        "text": f"{record['owner']}: the pull request ended; report it",
    }


def reap(bridge, directory):
    """Runs the dead-lane poll stage once with default settings."""
    supervision.dead_lanes(
        bridge.home,
        directory,
        supervision.roster.read(directory),
        supervision.settings({}),
    )


def inbox(bridge, lane):
    """Reads one lane's own mail with bodies."""
    return store.call(
        bridge.home, lane, "fetch_inbox", {"include_bodies": True}
    )["messages"]


def test_a_dead_holders_reservation_goes_to_the_queued_peer(
    bridge, repo, paired
):
    lane = actors(bridge, paired)
    directory = Path(paired["lanes"]["claude"]).parent
    live(directory, "codex")
    bridge.issue(Path(paired["lanes"]["claude"]), "claim", "7")
    store.call(
        bridge.home,
        lane["claude"],
        "file_reservation_paths",
        {"paths": ["src/engine.py"]},
    )
    store.call(
        bridge.home,
        lane["codex"],
        "request_reservation",
        {"paths": ["src/engine.py"]},
    )
    died(bridge, paired, "claude", CEILING + 60)

    reap(bridge, directory)

    assert store.active_reservations(bridge.home, paired["root"]) == {
        "codex": ["src/engine.py"]
    }
    assert issues.snapshot(directory)["issues"]["7"]["owner"] is None
    mail = {
        message["subject"]: message for message in inbox(bridge, lane["codex"])
    }
    grant = mail["Reservation granted: src/engine.py"]
    assert "was released because claude has been dead" in grant["body_md"]
    notice = mail["claude is dead; its work returned"]["body_md"]
    assert "Claims released to the pool: #7." in notice
    assert "resume it with agent-parley run claude --resume" in notice

    reap(bridge, directory)
    assert len(inbox(bridge, lane["codex"])) == 2


def test_a_lane_dead_under_the_ceiling_keeps_its_work(bridge, repo, paired):
    actors(bridge, paired)
    directory = Path(paired["lanes"]["claude"]).parent
    bridge.issue(Path(paired["lanes"]["claude"]), "claim", "7")
    died(bridge, paired, "claude", CEILING - 60)

    reap(bridge, directory)

    assert issues.snapshot(directory)["issues"]["7"]["owner"] == "claude"


def test_a_never_launched_lane_past_the_ceiling_returns_its_claims(
    bridge, repo, paired
):
    actors(bridge, paired)
    lane = Path(paired["lanes"]["codex"])
    directory = lane.parent
    bridge.issue(lane, "claim", "9")
    manifest = supervision.roster.read(directory)

    reason = supervision.dead_reason(
        bridge.home, directory, manifest, "codex", CEILING
    )
    assert reason == ""
    reap(bridge, directory)
    assert issues.snapshot(directory)["issues"]["9"]["owner"] == "codex"

    added = time.time() - CEILING - 60
    os.utime(lane / ".git", (added, added))
    reason = supervision.dead_reason(
        bridge.home, directory, manifest, "codex", CEILING
    )
    assert reason.startswith("codex was added ")
    assert reason.endswith("s ago and never launched")
    reap(bridge, directory)
    assert issues.snapshot(directory)["issues"]["9"]["owner"] is None


def test_a_dead_lanes_claim_goes_to_the_peer_that_requested_it(
    bridge, repo, paired
):
    actors(bridge, paired)
    directory = Path(paired["lanes"]["claude"]).parent
    live(directory, "codex")
    bridge.issue(Path(paired["lanes"]["claude"]), "claim", "8")
    bridge.issue(Path(paired["lanes"]["codex"]), "request", "8")
    died(bridge, paired, "claude", CEILING + 60)

    reap(bridge, directory)

    record = issues.snapshot(directory)["issues"]["8"]
    assert record["owner"] == "codex"
    assert record["request"] is None


def test_a_dependency_on_a_blocker_closed_on_the_forge_is_cleared(
    bridge, repo, paired
):
    actors(bridge, paired)
    directory = Path(paired["lanes"]["claude"]).parent
    bridge.issue(Path(paired["lanes"]["claude"]), "claim", "5")
    bridge.issue(Path(paired["lanes"]["codex"]), "claim", "6")
    edit(directory, "5", ended)
    edit(directory, "6", lambda record: record.update(blocked_by=["5"]))

    assert lifecycle.settle_dependencies(directory) == [("6", "5")]
    assert issues.snapshot(directory)["issues"]["6"]["blocked_by"] == []


def test_reminders_about_a_dead_lane_stop(bridge, repo, paired):
    actors(bridge, paired)
    directory = Path(paired["lanes"]["claude"]).parent
    live(directory, "codex")
    bridge.issue(Path(paired["lanes"]["claude"]), "claim", "5")
    edit(directory, "5", ended)
    bridge.issue(Path(paired["lanes"]["codex"]), "claim", "6")
    edit(directory, "6", ended)
    edit(
        directory,
        "6",
        lambda record: record["handoff_prompt"].update(holder="claude"),
    )
    assert "Handoff reminder unanswered" in issues.describe(
        issues.snapshot(directory)
    )
    died(bridge, paired, "claude", CEILING + 60)

    reap(bridge, directory)

    ledger = issues.snapshot(directory)
    prompt = ledger["issues"]["5"]["handoff_prompt"] or {}
    assert ledger["issues"]["5"]["owner"] is None
    assert not prompt or prompt["responded_at"]
    assert ledger["issues"]["6"]["owner"] == "codex"
    assert ledger["issues"]["6"]["handoff_prompt"]["responded_at"]
    assert "Handoff reminder unanswered" not in issues.describe(ledger)
