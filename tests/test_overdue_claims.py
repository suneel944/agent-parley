"""Exercises the terminating transition for an overdue claim's silent holder."""

import json
import os
import time
from pathlib import Path

import pytest

from agent_parley import (
    denials,
    issues,
    lanes,
    lifecycle,
    process,
    store,
    supervision,
)
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


def aged(directory):
    """Ages the recorded overdue step past one inactivity window."""
    edit(
        directory,
        "7",
        lambda record: record["overdue_recovery"].update(
            at=time.time() - WINDOW - 2
        ),
    )


def woke(bridge, directory, paired, record, result="resume requested"):
    """Records the dispatcher's wake attempt for the claim's overdue step."""
    supervision.store_wake(
        bridge.home,
        directory,
        paired["root"],
        "claude",
        {
            "at": time.time() - WINDOW - 1,
            "backlog": [f"overdue:{record['overdue_recovery']['id']}"],
            "attempts": 1,
            "result": result,
        },
    )


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

    assert step(bridge, directory, paired)["offer"] is None
    aged(directory)
    woke(bridge, directory, paired, woken)
    offered = step(bridge, directory, paired)
    assert offered["owner"] == "claude"
    assert offered["offer"]["to"] == "codex"
    assert "after a wake delivered at" in offered["offer"]["summary"]
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


def ended(record):
    """Records the reminder a forge-observed completion writes its holder."""
    record["handoff_prompt"] = {
        "id": "7:ended",
        "holder": "claude",
        "created": time.time(),
        "trigger": supervision.ENDED,
    }


def unresolved(record):
    """Records an unresolved completion on the claim's current generation."""
    record["unresolved_completion"] = {
        "claim_id": record["claim_id"],
        "reason": "claude left 3 completion reminders unanswered",
        "reminders": 3,
        "observed_at": time.time(),
    }


@pytest.mark.parametrize("observed", [ended, unresolved])
def test_an_observed_complete_claim_is_never_offered_or_released(
    bridge, paired, observed
):
    directory = overdue(bridge, paired)
    edit(directory, "7", observed)
    for _ in range(3):
        kept = step(bridge, directory, paired)
        edit(
            directory,
            "7",
            lambda record: (record.get("overdue_recovery") or {}).update(
                at=time.time() - WINDOW - 1
            ),
        )
    assert kept["owner"] == "claude"
    assert kept["offer"] is None
    assert "overdue_recovery" not in kept
    assert not any(
        entry["action"].startswith("overdue-") for entry in kept["history"]
    )


def test_an_overdue_peer_is_rated_on_the_projects_inactive_window(
    bridge, paired, monkeypatch
):
    directory = overdue(bridge, paired)
    manifest = json.loads((directory / "project.json").read_text())
    config = {**supervision.DEFAULTS, "inactive_after": 900}
    rated = []

    def fit(*args):
        rated.append(args)
        return {"fit": True}

    monkeypatch.setattr(supervision, "fit", fit)
    peer = supervision._overdue_peer(
        bridge.home, directory, manifest, config, {}, "claude"
    )
    assert peer == "codex"
    assert [args[3:] for args in rated] == [
        ("codex", config["stalled_after"], 900)
    ]


def test_a_holder_parked_on_an_operator_dialog_is_not_silent(bridge, paired):
    directory = overdue(bridge, paired)
    state = json.loads((directory / "claude-activity.json").read_text())
    write_json(
        directory / "claude-activity.json",
        {
            **state,
            "activity": "dialog: asks the operator: Which branch?",
            "dialog": {"name": "question", "action": "ask"},
        },
    )
    observed = supervision.presence(directory, "claude", WINDOW)
    assert not supervision.holder_silent(directory, "claude", observed, WINDOW)
    kept = step(bridge, directory, paired)
    assert "overdue_recovery" not in kept
    write_json(
        directory / "claude-activity.json",
        {
            **state,
            "activity": "dialog: usage limit",
            "dialog": {"name": "usage", "action": "exhausted"},
        },
    )
    observed = supervision.presence(directory, "claude", WINDOW)
    assert supervision.holder_silent(directory, "claude", observed, WINDOW)


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


def test_a_long_running_tool_call_under_the_timeout_is_not_silent(
    bridge, paired
):
    directory = overdue(bridge, paired)
    events(directory, "claude", ("PreToolUse", WINDOW + 150))
    assert supervision.tool_silence(directory, "claude") == 0.0
    observed = supervision.presence(directory, "claude", WINDOW)
    assert not supervision.holder_silent(directory, "claude", observed, WINDOW)
    kept = step(bridge, directory, paired)
    assert kept["owner"] == "claude"
    assert "overdue_recovery" not in kept


def polled_three_times(bridge, directory, paired):
    """Polls three times, ageing the step and recording a wake between."""
    for _ in range(3):
        kept = step(bridge, directory, paired)
        if kept.get("overdue_recovery"):
            aged(directory)
            woke(bridge, directory, paired, kept)
    return kept


def test_an_operator_blocked_overdue_claim_is_never_moved(bridge, paired):
    directory = overdue(bridge, paired)
    lifecycle.record_report(
        directory, "claude", "blocked", "", "needs operator host", issue="7"
    )
    kept = polled_three_times(bridge, directory, paired)
    assert kept["owner"] == "claude" and kept["offer"] is None
    assert "overdue_recovery" not in kept
    execution = lifecycle.state(kept)
    assert execution["state"] == lifecycle.BLOCKED
    assert execution["blocker"] == {"reason": "needs operator host"}
    manifest = json.loads((directory / "project.json").read_text())
    supervision.deadline_notices(directory, manifest)
    text = issues.snapshot(directory)["issues"]["7"]["deadline_notice"]["text"]
    assert "blocked on needs operator host" in text
    assert "does not move it" in text and "offered away" not in text

    lifecycle.record_report(
        directory, "claude", "partial", "", "host is back", issue="7"
    )
    edit(
        directory, "7", lambda record: record.update(deadline=time.time() - 60)
    )
    assert step(bridge, directory, paired)["overdue_recovery"]["step"] == "wake"


def test_a_claim_blocked_on_another_issue_is_kept(bridge, paired):
    directory = overdue(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    bridge.issue(lane, "claim", "9")
    lifecycle.record_report(
        directory,
        "claude",
        "blocked",
        "",
        "waits on #9",
        issue="7",
        resume_on="9",
    )
    kept = polled_three_times(bridge, directory, paired)
    assert kept["owner"] == "claude" and kept["offer"] is None
    assert lifecycle.state(kept)["resume_when"]


def test_an_open_denial_decision_holds_the_overdue_transition(bridge, paired):
    directory = overdue(bridge, paired)
    denials.record(
        directory,
        paired["root"],
        "claude",
        denials.Denial("claude", "Bash", "make deploy", "classifier"),
        issue="7",
    )
    kept = polled_three_times(bridge, directory, paired)
    assert denials.waiting(directory, "claude")
    assert kept["owner"] == "claude" and kept["offer"] is None
    assert "overdue_recovery" not in kept


def test_a_blocked_lane_holds_the_overdue_transition(bridge, paired):
    directory = overdue(bridge, paired)
    with store.connect(bridge.home, write=True) as db:
        lanes.transition(
            db, paired["root"], "claude", lanes.BLOCKED, cause="approval"
        )
    kept = polled_three_times(bridge, directory, paired)
    assert kept["owner"] == "claude" and kept["offer"] is None


def test_a_wake_that_could_not_reach_the_holder_says_so(bridge, paired):
    directory = overdue(bridge, paired)
    woken = step(bridge, directory, paired)
    aged(directory)
    woke(bridge, directory, paired, woken, supervision.WAKE_UNAVAILABLE)
    summary = step(bridge, directory, paired)["offer"]["summary"]
    assert "could not be woken at" in summary
    assert "after a wake" not in summary


def test_a_wake_never_attempted_advances_one_window_after_the_step(
    bridge, paired
):
    directory = overdue(bridge, paired)
    step(bridge, directory, paired)
    aged(directory)
    asked = issues.snapshot(directory)["issues"]["7"]["overdue_recovery"]["at"]
    offered = step(bridge, directory, paired)
    moment = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(asked + WINDOW))
    assert offered["offer"]["to"] == "codex"
    assert f"could not be woken at {moment}" in offered["offer"]["summary"]
    assert offered["history"][-1]["action"] == "overdue-offer"


def test_a_declared_wait_holds_the_overdue_transition(bridge, paired):
    directory = overdue(bridge, paired)
    supervision.record_wait(
        directory,
        "claude",
        "gpu forecast run",
        time.time() + 3600,
        supervision.DEFAULT_WAIT_CEILING,
    )
    kept = polled_three_times(bridge, directory, paired)
    assert kept["owner"] == "claude" and kept["offer"] is None
    assert "overdue_recovery" not in kept
    wait = json.loads((directory / "claude-wait.json").read_text())
    write_json(
        directory / "claude-wait.json", {**wait, "until": time.time() - 1}
    )
    assert step(bridge, directory, paired)["overdue_recovery"]["step"] == "wake"


def test_a_held_holder_is_not_named_to_idle_peers(bridge, paired):
    directory = overdue(bridge, paired)
    manifest = json.loads((directory / "project.json").read_text())
    pool = issues.snapshot(directory)
    pool["issues"]["9"] = {
        "owner": "codex",
        "claim_id": "codex-c",
        "offer": None,
        "blocked_by": [],
        "execution": {
            "authorized": True,
            "state": "ready",
            "claim_id": "codex-c",
        },
    }
    leads = supervision._idle_leads(
        bridge.home, directory, manifest, "codex", pool, 0
    )
    assert leads["stalled"] == {"issue": "7", "owner": "claude"}
    denials.record(
        directory,
        paired["root"],
        "claude",
        denials.Denial("claude", "Bash", "make deploy", "classifier"),
        issue="7",
    )
    leads = supervision._idle_leads(
        bridge.home, directory, manifest, "codex", pool, 0
    )
    assert leads["stalled"] is None


def test_a_holder_offer_survives_the_overdue_transition(bridge, paired):
    directory = overdue(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    woken = step(bridge, directory, paired)
    assert woken["overdue_recovery"]["step"] == "wake"
    bridge.issue(lane, "offer", "7", to="codex", summary="hand over")
    chosen = issues.snapshot(directory)["issues"]["7"]["offer"]
    aged_offer = polled_three_times(bridge, directory, paired)
    assert aged_offer["offer"]["id"] == chosen["id"]
    assert aged_offer["offer"]["to"] == "codex"
    assert "overdue_recovery" not in aged_offer
    actions = [entry["action"] for entry in aged_offer["history"]]
    assert "cancel" not in actions

    edit(
        directory,
        "7",
        lambda record: record["offer"].update(deadline=time.time() - 1),
    )
    manifest = json.loads((directory / "project.json").read_text())
    supervision.expire_offers(directory, manifest)
    assert issues.snapshot(directory)["issues"]["7"]["offer"] is None
    assert step(bridge, directory, paired)["overdue_recovery"]["step"] == "wake"


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
