"""Exercises the terminating transition for an overdue claim's silent holder."""

import json
import os
import time
from pathlib import Path

from agent_parley import issues, process, store, supervision
from agent_parley.state import write_json

WINDOW = 300


def registered(bridge, paired):
    """Registers both lanes so fitness checks can read their mail."""
    store.initialize(bridge.home)
    for name in ("claude", "codex"):
        store.register(bridge.home, paired["root"], name)


def live(directory, name, age):
    """Records a live session whose last native checkpoint is `age` old."""
    write_json(
        directory / f"{name}-activity.json",
        {
            "activity": "idle",
            "updated": time.time() - age,
            "session_id": f"{name}-session",
            "session_pid": os.getpid(),
            "session_ticks": process.start_ticks(os.getpid()),
        },
    )


def events(directory, name, *entries):
    """Appends native hook events, each an (event, seconds ago) pair."""
    with (directory / f"{name}-events.jsonl").open("a") as stream:
        for event, ago in entries:
            record = {"ts": time.time() - ago, "event": event}
            stream.write(json.dumps(record) + "\n")


def edit(directory, number, change):
    """Rewrites one ledger record in place, as elapsed time would."""
    ledger = issues.snapshot(directory)
    change(ledger["issues"][number])
    write_json(directory / "issues.json", ledger)


def step(bridge, directory, paired):
    """Runs the overdue-claim stage once against fresh presence readings."""
    manifest = json.loads((directory / "project.json").read_text())
    config = {**supervision.DEFAULTS, "inactive_after": WINDOW}
    observations = {
        name: supervision.presence(directory, name, WINDOW)
        for name in manifest["participants"]
    }
    supervision.overdue_claims(
        bridge.home, directory, manifest, config, observations
    )
    return issues.snapshot(directory)["issues"]["7"]


def overdue(bridge, paired):
    """Leaves claude holding an overdue claim after an hour of no tool call."""
    registered(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    directory = lane.parent
    bridge.issue(lane, "claim", "7")
    edit(
        directory, "7", lambda record: record.update(deadline=time.time() - 60)
    )
    live(directory, "claude", 3600)
    events(directory, "claude", ("PostToolUse", 3600))
    live(directory, "codex", 5)
    events(directory, "codex", ("PostToolUse", 5))
    return directory


def test_a_silent_holder_is_woken_then_offered_away_then_released(
    bridge, paired
):
    directory = overdue(bridge, paired)

    woken = step(bridge, directory, paired)
    assert woken["owner"] == "claude"
    assert woken["overdue_recovery"]["step"] == "wake"
    assert woken["attempts"] == 1
    assert woken["history"][-1]["action"] == "overdue-wake"

    edit(
        directory,
        "7",
        lambda record: record["overdue_recovery"].update(
            at=time.time() - WINDOW - 1
        ),
    )
    offered = step(bridge, directory, paired)
    assert offered["owner"] == "claude"
    assert offered["offer"]["to"] == "codex"
    assert "Recovery checkpoint issue-7-" in offered["offer"]["summary"]
    assert offered["offer"]["commit"]
    assert offered["attempts"] == 2
    assert offered["history"][-1]["action"] == "overdue-offer"

    edit(
        directory,
        "7",
        lambda record: record["offer"].update(deadline=time.time() - 1),
    )
    released = step(bridge, directory, paired)
    assert released["owner"] is None and released["offer"] is None
    actions = [entry["action"] for entry in released["history"]]
    assert actions[-3:] == ["overdue-release", "cancel", "release"]
    assert issues.released(released)


def test_a_holder_that_works_again_keeps_its_overdue_claim(bridge, paired):
    directory = overdue(bridge, paired)
    assert step(bridge, directory, paired)["overdue_recovery"]["step"] == "wake"
    events(directory, "claude", ("PostToolUse", 1))
    kept = step(bridge, directory, paired)
    assert kept["owner"] == "claude" and kept["offer"] is None
    assert "overdue_recovery" not in kept


def test_a_resume_that_ends_without_work_does_not_extend_silence(
    bridge, paired
):
    directory = overdue(bridge, paired)
    events(directory, "claude", ("SessionStart", 30), ("SessionEnd", 10))
    live(directory, "claude", 10)
    observed = supervision.presence(directory, "claude", WINDOW)
    assert observed["age_seconds"] < WINDOW
    assert supervision.tool_silence(directory, "claude") >= 3600
    assert supervision.holder_silent(directory, "claude", observed, WINDOW)
    assert step(bridge, directory, paired)["overdue_recovery"]["step"] == "wake"


def test_a_claim_without_a_deadline_takes_the_project_default(bridge, paired):
    registered(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    directory = lane.parent
    bridge.issue(lane, "claim", "7")
    edit(directory, "7", lambda record: record.update(deadline=None))
    manifest = {**paired, "deadlines": {"claim": 3600}}
    supervision.deadline_defaults(directory, manifest)
    record = issues.snapshot(directory)["issues"]["7"]
    started = supervision.claimed_since(record)
    assert record["deadline"] == started + 3600


def test_a_resume_whose_tool_call_never_completes_does_not_extend_silence(
    bridge, paired
):
    directory = overdue(bridge, paired)
    events(
        directory,
        "claude",
        ("SessionStart", 40),
        ("PreToolUse", 30),
        ("SessionEnd", 10),
    )
    live(directory, "claude", 10)
    assert supervision.tool_silence(directory, "claude") >= 3600
    observed = supervision.presence(directory, "claude", WINDOW)
    assert supervision.holder_silent(directory, "claude", observed, WINDOW)


def test_a_tool_call_still_running_counts_as_work(bridge, paired):
    directory = overdue(bridge, paired)
    events(directory, "claude", ("SessionStart", 40), ("PreToolUse", 30))
    assert supervision.tool_silence(directory, "claude") < WINDOW


def test_issue_list_names_a_claim_without_a_deadline(bridge, paired):
    registered(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    directory = lane.parent
    bridge.issue(lane, "claim", "7")
    edit(directory, "7", lambda record: record.update(deadline=None))
    listing = issues.describe(issues.snapshot(directory))
    assert listing.startswith("#7: claude;")
    assert listing.endswith("; no deadline")
    edit(directory, "7", lambda record: record.update(deadline=1e12))
    assert "no deadline" not in issues.describe(issues.snapshot(directory))
