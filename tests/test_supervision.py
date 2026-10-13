"""Exercises liveness, reminders and bounded wake decisions in local state."""

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from agent_parley import (
    checkpoints,
    cli,
    dialogs,
    issues,
    lanes,
    process,
    roster,
    server,
    state,
    store,
    supervision,
    terminal,
)
from agent_parley.checkpoints import mailbox, participant_liveness
from agent_parley.state import write_json


def registered(bridge, paired):
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


def rewake(bridge, paired, directory, name, **changes):
    """Rewrites a lane's recorded wake fields and returns them as they were."""
    record = supervision.wake_record(bridge.home, paired["root"], name)
    supervision.store_wake(
        bridge.home, directory, paired["root"], name, {**record, **changes}
    )
    return record


def sampled(bridge, paired, directory, name, inactive_after=1, session=None):
    """Applies one poll's liveness sample to a lane's state record.

    A wake decides from the record, so a test that calls it directly first
    records what the poll would have. A session names the one a native hook
    would have recorded.
    """
    observed = supervision.presence(directory, name, inactive_after)
    with store.connect(bridge.home, write=True) as db:
        record = lanes.sample(
            db,
            paired["root"],
            name,
            observed,
            dead_after=supervision.DEFAULTS["stalled_after"],
        )
        if session:
            lanes.transition(
                db, paired["root"], name, record["state"], session=session
            )
    return observed


def send(bridge, actor, recipient, key="pending"):
    return store.call(
        bridge.home,
        actor,
        "send_message",
        {
            "to": [recipient],
            "subject": "Review",
            "body_md": "Review the result",
            "idempotency_key": key,
            "ack_required": True,
        },
    )


def test_presence_separates_an_idle_lane_from_a_stopped_one(tmp_path):
    write_json(
        tmp_path / "lane-activity.json",
        {
            "session_pid": os.getpid(),
            "session_ticks": process.start_ticks(os.getpid()),
            "updated": time.time() - 60,
            "activity": "working",
        },
    )
    assert (
        supervision.presence(tmp_path, "lane", 100)["state"]
        == supervision.ACTIVE
    )
    quiet = supervision.presence(tmp_path, "lane", 30)
    assert quiet["state"] == supervision.IDLE and quiet["process_alive"]
    write_json(tmp_path / "lane-activity.json", {"updated": time.time()})
    unknown = supervision.presence(tmp_path, "lane")
    assert unknown["state"] == supervision.UNKNOWN
    assert unknown["process_alive"] is None


def live(tmp_path, **extra):
    """Publishes an activity record whose session process is alive."""
    write_json(
        tmp_path / "lane-activity.json",
        {
            "session_pid": os.getpid(),
            "session_ticks": process.start_ticks(os.getpid()),
            **extra,
        },
    )


def test_an_open_tool_call_reads_as_working_until_the_tool_timeout(tmp_path):
    live(
        tmp_path,
        activity="working",
        event="PreToolUse",
        updated=time.time() - 359,
    )
    derived = supervision.lane_state(
        json.loads((tmp_path / "lane-activity.json").read_text()), 300
    )
    assert derived["state"] == supervision.WORKING
    assert derived["evidence"] == "working; tool call in flight"
    assert derived["stale"] is False
    assert derived["age_seconds"] == 359
    observed = supervision.presence(tmp_path, "lane", 300)
    assert observed["state"] == supervision.ACTIVE
    assert observed["activity"] == supervision.WORKING
    assert (
        participant_liveness(tmp_path, "lane", 300)
        == "working; tool call in flight; event 359s ago"
    )
    live(
        tmp_path,
        activity="working",
        event="PreToolUse",
        updated=time.time() - supervision.TOOL_TIMEOUT - 1,
    )
    assert supervision.presence(tmp_path, "lane", 300)["stale"] is True


def test_an_aged_record_reads_as_stale_with_its_age_not_as_current(tmp_path):
    live(tmp_path, activity="working", updated=time.time() - 108363)
    derived = supervision.lane_state(
        json.loads((tmp_path / "lane-activity.json").read_text()), 300
    )
    assert derived["state"] == supervision.IDLE
    assert derived["stale"] is True
    assert derived["evidence"] == "stale; last working"
    assert derived["age_seconds"] == 108363
    assert supervision.presence(tmp_path, "lane", 300)["state"] == (
        supervision.IDLE
    )
    assert (
        participant_liveness(tmp_path, "lane", 300)
        == "stale; last working; event 108363s ago"
    )


def test_a_lane_with_a_live_process_is_never_reported_stopped(tmp_path):
    for recorded in ("stopped", "idle", "waiting for approval", "working"):
        live(tmp_path, activity=recorded, updated=time.time())
        derived = supervision.lane_state(
            json.loads((tmp_path / "lane-activity.json").read_text()), 300
        )
        assert derived["state"] != supervision.STOPPED
        assert derived["process_alive"] is True
        assert supervision.presence(tmp_path, "lane", 300)["state"] != (
            supervision.STOPPED
        )
        assert "stopped" not in participant_liveness(tmp_path, "lane", 300)
    live(tmp_path, activity="stopped", updated=time.time() - 108363)
    assert supervision.lane_state(
        json.loads((tmp_path / "lane-activity.json").read_text()), 300
    )["state"] == (supervision.IDLE)


def test_presence_reports_no_age_before_the_first_checkpoint(tmp_path):
    absent = supervision.presence(tmp_path, "lane")
    assert absent["state"] == supervision.STOPPED
    assert absent["last_active"] is None
    assert absent["age_seconds"] is None
    write_json(
        tmp_path / "lane-activity.json",
        {
            "session_pid": os.getpid(),
            "session_ticks": process.start_ticks(os.getpid()),
            "activity": "working",
        },
    )
    started = supervision.presence(tmp_path, "lane", 30)
    assert started["state"] == supervision.ACTIVE
    assert started["process_alive"]
    assert started["age_seconds"] is None


def test_send_reports_unreachable_and_status_lists_ack_age(
    bridge, paired, capsys
):
    actors = registered(bridge, paired)
    directory = Path(paired["lanes"]["claude"]).parent
    supervision.poll(bridge.home, directory)
    message = send(bridge, actors["claude"], "codex")
    warning = message["recipient_warnings"][0]
    assert warning["recipient"] == "codex"
    assert warning["state"] == "unreachable"
    assert warning["summary"] == "queued for codex (unreachable)"
    pending = mailbox(bridge.home, paired["root"], "codex")["outstanding_ack"]
    assert pending[0]["id"] == message["id"]
    assert pending[0]["age_seconds"] >= 0
    bridge.status(cli.Selection(participant="codex"))
    assert "Awaiting acknowledgement: message" in capsys.readouterr().out


def test_send_reports_a_quiet_live_recipient_as_idle_not_unreachable(
    bridge, paired
):
    actors = registered(bridge, paired)
    directory = Path(paired["lanes"]["claude"]).parent
    write_json(
        directory / "codex-activity.json",
        {
            "activity": "idle",
            "updated": time.time() - 500,
            "session_pid": os.getpid(),
            "session_ticks": process.start_ticks(os.getpid()),
        },
    )
    supervision.poll(bridge.home, directory)
    lanes = bridge.status_snapshot()["projects"][0]["participants"]
    observed = {
        record["participant"]: record["availability"] for record in lanes
    }
    assert observed["codex"]["state"] == "idle"
    assert observed["codex"]["process_alive"]
    assert observed["claude"]["state"] == "stopped"
    warning = send(bridge, actors["claude"], "codex")["recipient_warnings"][0]
    assert warning["state"] == "idle"
    assert warning["summary"] == "queued for codex (idle; wake requested)"


def test_release_reminds_waiter_without_transferring_work(bridge, paired):
    actors = registered(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    peer = Path(paired["lanes"]["codex"])
    bridge.issue(lane, "claim", "1")
    bridge.issue(peer, "claim", "2")
    bridge.issue(peer, "block", "2", on="1")
    bridge.issue(lane, "release", "1")
    ledger = issues.snapshot(lane.parent)
    prompt = ledger["issues"]["1"]["handoff_prompt"]
    assert prompt["waiting"] == ["codex"]
    assert "unanswered" in issues.describe(ledger)
    supervision.reminders(lane.parent, roster.read(lane.parent), set())
    assert issues.snapshot(lane.parent)["revision"] == ledger["revision"]
    send(bridge, actors["claude"], "codex", "handoff")
    supervision.observe_responses(
        bridge.home, lane.parent, roster.read(lane.parent)
    )
    after = issues.snapshot(lane.parent)
    assert after["issues"]["1"]["owner"] is None
    assert after["issues"]["2"]["owner"] == "codex"
    assert after["issues"]["1"]["handoff_prompt"]["responded_at"]


def test_withheld_completion_note_answers_the_release_reminder(bridge, paired):
    actors = registered(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    bridge.issue(lane, "claim", "1")
    bridge.issue(lane, "release", "1")
    supervision.reminders(lane.parent, roster.read(lane.parent), {"1"})
    prompt = issues.snapshot(lane.parent)["issues"]["1"]["handoff_prompt"]
    assert prompt["waiting"] == []
    sent = store.call(
        bridge.home,
        actors["claude"],
        "send_message",
        {
            "to": ["codex"],
            "subject": "Merged #1 into main",
            "body_md": "Released #1 after the merge.",
            "idempotency_key": "done",
        },
    )
    assert sent["withheld"] == ["codex"]
    supervision.observe_responses(
        bridge.home, lane.parent, roster.read(lane.parent)
    )
    record = issues.snapshot(lane.parent)["issues"]["1"]
    assert record["owner"] is None
    assert record["handoff_prompt"]["responded_at"]
    assert not mailbox(bridge.home, paired["root"], "codex")["unread"]


def test_closed_pr_reminds_holder_and_preserves_claim(
    bridge, paired, monkeypatch
):
    registered(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    peer = Path(paired["lanes"]["codex"])
    bridge.issue(lane, "claim", "1")
    bridge.issue(peer, "claim", "2")
    bridge.issue(peer, "block", "2", on="1")
    monkeypatch.setattr(
        supervision.forge,
        "branch_completion",
        lambda *args: ("MERGED", time.time()),
    )
    supervision.poll(bridge.home, lane.parent)
    record = issues.snapshot(lane.parent)["issues"]["1"]
    assert record["owner"] == "claude"
    assert record["handoff_prompt"]["trigger"] == "pull request ended"


def test_a_completion_message_sent_before_the_poll_saw_the_close_counts(
    bridge, paired, monkeypatch
):
    actors = registered(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    bridge.issue(lane, "claim", "1")
    claimed_at = issues.snapshot(lane.parent)["issues"]["1"]["history"][-1][
        "at"
    ]
    monkeypatch.setattr(
        supervision.forge,
        "issue_completion",
        lambda *args: {
            "state": "MERGED",
            "closed_at": claimed_at,
            "pull_request": 2,
            "url": "https://example.invalid/pull/2",
            "branch": "",
            "commit": "abc123",
        },
    )
    store.call(
        bridge.home,
        actors["claude"],
        "send_message",
        {
            "to": ["codex"],
            "subject": "Merged #1",
            "body_md": "Landed the fix for #1.",
            "idempotency_key": "done-1",
        },
    )
    supervision.poll(bridge.home, lane.parent)
    record = issues.snapshot(lane.parent)["issues"]["1"]
    assert record["handoff_prompt"]["trigger"] == "pull request ended"
    assert record["handoff_prompt"]["responded_at"]
    assert record["owner"] == "claude"


def test_an_escalated_completion_reminder_stops_waking_the_holder(
    bridge, paired, monkeypatch
):
    registered(bridge, paired)
    lane = Path(paired["lanes"]["codex"])
    bridge.issue(lane, "claim", "1")
    supervision.reminders(lane.parent, roster.read(lane.parent), {"1"})
    write_json(
        lane.parent / "codex-activity.json",
        {
            "activity": "idle",
            "updated": time.time() - 500,
            "session_pid": os.getpid(),
            "session_ticks": process.start_ticks(os.getpid()),
        },
    )
    calls = []
    monkeypatch.setattr(
        terminal, "request", lambda *args: calls.append(args) or "accepted"
    )
    config = {**supervision.DEFAULTS, "inactive_after": 1}
    observed = sampled(bridge, paired, lane.parent, "codex")
    rewake(bridge, paired, lane.parent, "codex", at=0, backlog=[])
    supervision.wake(
        bridge.home, lane.parent, paired, "codex", observed, config
    )
    assert len(calls) == 1
    ledger = issues.snapshot(lane.parent)
    record = ledger["issues"]["1"]
    record["unresolved_completion"] = {
        "claim_id": record["claim_id"],
        "holder": "codex",
        "prompt": record["handoff_prompt"]["id"],
    }
    ledger["revision"] += 1
    write_json(lane.parent / "issues.json", ledger)
    rewake(bridge, paired, lane.parent, "codex", at=0)
    supervision.wake(
        bridge.home, lane.parent, paired, "codex", observed, config
    )
    assert len(calls) == 1


def test_live_idle_wakes_back_off_without_acknowledging(
    bridge, paired, monkeypatch
):
    actors = registered(bridge, paired)
    lane = Path(paired["lanes"]["codex"])
    write_json(
        lane.parent / "codex-activity.json",
        {
            "activity": "idle",
            "updated": time.time() - 500,
            "session_pid": os.getpid(),
            "session_ticks": process.start_ticks(os.getpid()),
        },
    )
    message = send(bridge, actors["claude"], "codex")
    calls = []
    monkeypatch.setattr(
        terminal, "request", lambda *args: calls.append(args) or "accepted"
    )
    config = {**supervision.DEFAULTS, "inactive_after": 1}
    observed = sampled(bridge, paired, lane.parent, "codex")
    for _ in range(5):
        supervision.wake(
            bridge.home, lane.parent, paired, "codex", observed, config
        )
        record = rewake(bridge, paired, lane.parent, "codex", at=0)
    assert len(calls) == 5
    assert record["attempts"] == 5 and record["exhausted_at"]
    assert record["next_at"] - record["at"] == 16
    rewake(bridge, paired, lane.parent, "codex", **record)
    supervision.wake(
        bridge.home, lane.parent, paired, "codex", observed, config
    )
    assert len(calls) == 5
    assert (
        mailbox(bridge.home, paired["root"], "codex")["outstanding_ack"][0][
            "id"
        ]
        == message["id"]
    )


def test_a_busy_refusal_does_not_consume_a_bounded_attempt(
    bridge, paired, monkeypatch
):
    actors = registered(bridge, paired)
    lane = Path(paired["lanes"]["codex"])
    write_json(
        lane.parent / "codex-activity.json",
        {
            "activity": "idle",
            "updated": time.time() - 500,
            "session_pid": os.getpid(),
            "session_ticks": process.start_ticks(os.getpid()),
        },
    )
    send(bridge, actors["claude"], "codex")
    answers = iter(
        ["busy:turn", "busy:input", "busy:repeat", "busy:turn"]
        + ["accepted"] * 5
    )
    calls = []
    monkeypatch.setattr(
        terminal,
        "request",
        lambda *args: calls.append(args) or next(answers),
    )
    config = {**supervision.DEFAULTS, "inactive_after": 1}
    observed = sampled(bridge, paired, lane.parent, "codex")
    for _ in range(9):
        supervision.wake(
            bridge.home, lane.parent, paired, "codex", observed, config
        )
        rewake(bridge, paired, lane.parent, "codex", at=0)
    assert len(calls) == 9
    assert rewake(bridge, paired, lane.parent, "codex")["attempts"] == 5


def test_stale_working_lane_exhausts_its_wake_budget(
    bridge, paired, monkeypatch
):
    actors = registered(bridge, paired)
    lane = Path(paired["lanes"]["codex"])
    activity = lane.parent / "codex-activity.json"
    write_json(
        activity,
        {
            "activity": "working",
            "updated": time.time() - 500,
            "session_pid": os.getpid(),
            "session_ticks": process.start_ticks(os.getpid()),
        },
    )
    send(bridge, actors["claude"], "codex")

    def request(*args):
        state = json.loads(activity.read_text())
        return "busy:turn" if terminal.turn_busy(state, 1) else "accepted"

    monkeypatch.setattr(terminal, "request", request)
    config = {**supervision.DEFAULTS, "inactive_after": 1}
    observed = sampled(bridge, paired, lane.parent, "codex")
    for _ in range(supervision.WORK_WAKE_ATTEMPTS):
        supervision.wake(
            bridge.home, lane.parent, paired, "codex", observed, config
        )
        record = rewake(bridge, paired, lane.parent, "codex", at=0)
    assert record["result"] == "accepted"
    assert record["attempts"] == supervision.WORK_WAKE_ATTEMPTS
    assert record["exhausted_at"]


def test_launcher_reads_a_fresh_working_label_as_busy():
    fresh = {
        "activity": "working",
        "updated": time.time(),
        "session_pid": os.getpid(),
        "session_ticks": process.start_ticks(os.getpid()),
    }
    stale = {**fresh, "updated": time.time() - 500}
    assert terminal.turn_busy(fresh, 300)
    assert not terminal.turn_busy(stale, 300)
    assert not terminal.turn_busy({**stale, "activity": "idle"}, 300)
    assert terminal.turn_busy({**stale, "updated": "x"}, 300)


def test_launcher_types_a_wake_only_into_an_idle_lane():
    idle = {
        "activity": "idle",
        "updated": time.time(),
        "session_pid": os.getpid(),
        "session_ticks": process.start_ticks(os.getpid()),
    }
    question = {
        **idle,
        "activity": f"{dialogs.MARKER}asks the operator",
        "updated": time.time() - 500,
    }
    unknown = {"activity": "idle", "updated": time.time()}
    assert not terminal.turn_busy(idle, 300)
    assert supervision.lane_state(question, 300)["state"] == supervision.WAITING
    assert terminal.turn_busy(question, 300)
    assert supervision.lane_state(unknown, 300)["state"] == supervision.UNKNOWN
    assert terminal.turn_busy(unknown, 300)


def test_launcher_reads_recent_terminal_output_as_a_turn():
    stale = {
        "activity": "working",
        "updated": time.time() - 500,
        "session_pid": os.getpid(),
        "session_ticks": process.start_ticks(os.getpid()),
    }
    assert terminal.turn_busy(stale, 300, 10)
    assert not terminal.turn_busy(stale, 300, 400)
    assert not terminal.turn_busy(stale, 300, None)


@pytest.mark.parametrize(
    "content", ["[]", "{", json.dumps({"activity": "idle", "updated": "x"})]
)
def test_a_malformed_activity_file_reads_as_unknown_presence(
    bridge, paired, content
):
    registered(bridge, paired)
    directory = Path(paired["lanes"]["codex"]).parent
    (directory / "codex-activity.json").write_text(content)
    write_json(
        directory / "claude-activity.json",
        {
            "activity": "working",
            "updated": time.time(),
            "session_id": "claude-session",
            "session_pid": os.getpid(),
            "session_ticks": process.start_ticks(os.getpid()),
        },
    )

    reading = supervision.presence(directory, "codex")
    supervision.poll(bridge.home, directory)

    assert reading["state"] == supervision.UNKNOWN
    assert reading["process_alive"] is None
    assert supervision.presence(directory, "claude")["process_alive"]


def test_permission_prompt_is_never_woken(bridge, paired, monkeypatch):
    actors = registered(bridge, paired)
    directory = Path(paired["lanes"]["codex"]).parent
    write_json(
        directory / "codex-activity.json", {"activity": "waiting for approval"}
    )
    send(bridge, actors["claude"], "codex")
    monkeypatch.setattr(
        terminal, "request", lambda *args: pytest.fail("woke approval")
    )
    supervision.wake(
        bridge.home,
        directory,
        paired,
        "codex",
        {"process_alive": True},
        supervision.DEFAULTS,
    )


@pytest.mark.parametrize(
    ("label", "dialog"),
    [
        ("waiting for approval: Bash", {"name": "tool-permission"}),
        (
            "dialog: asks the operator: Which branch?",
            {"name": "question", "action": "ask", "escalated": True},
        ),
        (
            "dialog: an unrecognized native prompt",
            {"name": "unknown", "action": "escalate", "escalated": True},
        ),
    ],
)
@pytest.mark.parametrize("event", ["Stop", "PreToolUse"])
def test_a_lane_parked_on_an_operator_dialog_is_never_woken(
    bridge, paired, monkeypatch, label, dialog, event
):
    actors = registered(bridge, paired)
    directory = Path(paired["lanes"]["codex"]).parent
    write_json(
        directory / "codex-activity.json",
        {
            "activity": label,
            "dialog": dialog,
            "event": event,
            "updated": time.time() - 500,
            "session_pid": os.getpid(),
            "session_ticks": process.start_ticks(os.getpid()),
        },
    )
    send(bridge, actors["claude"], "codex")
    monkeypatch.setattr(
        terminal, "request", lambda *args: pytest.fail("woke a dialog")
    )
    config = {**supervision.DEFAULTS, "inactive_after": 1}
    observed = sampled(bridge, paired, directory, "codex")
    assert observed["activity"] == supervision.WAITING
    for _ in range(supervision.WORK_WAKE_ATTEMPTS + 1):
        supervision.wake(
            bridge.home, directory, paired, "codex", observed, config
        )
    record = supervision.wake_record(bridge.home, paired["root"], "codex")
    assert not record.get("attempts")
    assert not record.get("escalated_at")
    with store.connect(bridge.home) as db:
        assert lanes.read(db, paired["root"], "codex")["state"] == (
            lanes.BLOCKED
        )


def test_a_lane_blocked_past_the_bound_escalates_once(
    bridge, paired, monkeypatch
):
    actors = registered(bridge, paired)
    directory = Path(paired["lanes"]["codex"]).parent
    write_json(
        directory / "codex-activity.json",
        {
            "activity": "waiting for approval: Bash",
            "dialog": {"name": "tool-permission"},
            "event": "PreToolUse",
            "updated": time.time() - 500,
            "session_pid": os.getpid(),
            "session_ticks": process.start_ticks(os.getpid()),
        },
    )
    send(bridge, actors["claude"], "codex")
    monkeypatch.setattr(
        terminal, "request", lambda *args: pytest.fail("woke a dialog")
    )
    sent = []
    monkeypatch.setattr(
        supervision.notify,
        "deliver",
        lambda directory, name, event, fields: sent.append(
            (name, event, fields)
        ),
    )
    config = {**supervision.DEFAULTS, "inactive_after": 1}
    observed = sampled(bridge, paired, directory, "codex")
    rewake(
        bridge,
        paired,
        directory,
        "codex",
        at=time.time() - 60,
        attempts=1,
        result="resume requested (launcher 1)",
    )
    supervision.wake(bridge.home, directory, paired, "codex", observed, config)
    assert not sent
    assert not supervision.wake_record(
        bridge.home, paired["root"], "codex"
    ).get("escalated_at")

    since = time.time() - supervision.BLOCKED_ESCALATE_AFTER - 1
    with store.connect(bridge.home) as db:
        db.execute(
            "UPDATE lane_states SET since=? WHERE project=? AND lane=?",
            (since, paired["root"], "codex"),
        )
    for _ in range(supervision.WORK_WAKE_ATTEMPTS + 1):
        supervision.wake(
            bridge.home, directory, paired, "codex", observed, config
        )

    record = supervision.wake_record(bridge.home, paired["root"], "codex")
    assert record["attempts"] == 1
    assert record["escalated_at"]
    assert record["blocked"] == "blocked: approval"
    assert sent
    assert {(name, event) for name, event, _ in sent} == {
        ("codex", supervision.notify.Event.LANE_BLOCKED)
    }
    assert {fields["since"] for _, _, fields in sent} == {str(since)}
    assert "blocked: approval" in sent[0][2]["detail"]


@pytest.mark.parametrize("state", [lanes.WORKING, lanes.IDLE])
def test_a_lane_that_leaves_blocked_clears_its_recorded_block(
    bridge, paired, monkeypatch, state
):
    registered(bridge, paired)
    directory = Path(paired["lanes"]["codex"]).parent
    with store.connect(bridge.home, write=True) as db:
        lanes.transition(
            db, paired["root"], "codex", lanes.BLOCKED, cause="approval"
        )
    rewake(
        bridge,
        paired,
        directory,
        "codex",
        at=time.time() - 60,
        attempts=1,
        result="resume requested (launcher 1)",
        blocked="blocked: approval",
        escalated_at=time.time() - 30,
        exhausted_at=time.time() - 30,
    )
    with store.connect(bridge.home, write=True) as db:
        lanes.transition(db, paired["root"], "codex", state)
    monkeypatch.setattr(
        terminal, "request", lambda *args: pytest.fail("woke the lane")
    )
    config = {**supervision.DEFAULTS, "inactive_after": 1}
    observed = supervision.presence(directory, "codex", 1)
    supervision.wake(bridge.home, directory, paired, "codex", observed, config)

    record = supervision.wake_record(bridge.home, paired["root"], "codex")
    assert record["blocked"] == ""
    assert not record.get("escalated_at")
    assert not record.get("exhausted_at")
    assert record["attempts"] == 1


def test_a_lane_still_blocked_keeps_its_recorded_block(bridge, paired):
    registered(bridge, paired)
    directory = Path(paired["lanes"]["codex"]).parent
    with store.connect(bridge.home, write=True) as db:
        lanes.transition(
            db, paired["root"], "codex", lanes.BLOCKED, cause="approval"
        )
    rewake(
        bridge,
        paired,
        directory,
        "codex",
        at=time.time() - 60,
        attempts=1,
        result="resume requested (launcher 1)",
        blocked="blocked: approval",
        escalated_at=1.0,
    )
    config = {**supervision.DEFAULTS, "inactive_after": 1}
    observed = supervision.presence(directory, "codex", 1)
    supervision.wake(bridge.home, directory, paired, "codex", observed, config)

    record = supervision.wake_record(bridge.home, paired["root"], "codex")
    assert record["blocked"] == "blocked: approval"
    assert record["escalated_at"] == 1.0


def test_a_lane_blocked_notifies_once_per_block(bridge, monkeypatch):
    monkeypatch.setenv("AGENT_PARLEY_NOTIFY", "telegram")
    monkeypatch.setenv("AGENT_PARLEY_TELEGRAM_TOKEN", "token")
    monkeypatch.setenv("AGENT_PARLEY_TELEGRAM_CHAT", "chat")
    calls = []
    monkeypatch.setattr(
        supervision.notify,
        "send",
        lambda config, subject, body: (
            calls.append(subject) or [{"transport": "telegram", "ok": True}]
        ),
    )
    directory = bridge.home / "project"
    directory.mkdir()
    fields = {"since": "1.0", "detail": "blocked: approval for 1900s"}

    for _ in range(3):
        supervision.notify.deliver(
            directory, "codex", supervision.notify.Event.LANE_BLOCKED, fields
        )
        supervision.notify.drain()

    assert len(calls) == 1
    assert "still blocked" in calls[0]


def test_an_unrecorded_lane_parked_on_a_dialog_is_never_woken(
    bridge, paired, monkeypatch
):
    actors = registered(bridge, paired)
    directory = Path(paired["lanes"]["codex"]).parent
    write_json(
        directory / "codex-activity.json",
        {
            "activity": "dialog: asks the operator: Which branch?",
            "dialog": {"name": "question", "action": "ask"},
            "updated": time.time() - 500,
            "session_pid": os.getpid(),
            "session_ticks": process.start_ticks(os.getpid()),
        },
    )
    send(bridge, actors["claude"], "codex")
    monkeypatch.setattr(
        terminal, "request", lambda *args: pytest.fail("woke a dialog")
    )
    monkeypatch.setattr(supervision, "condition", lambda *args: None)
    supervision.wake(
        bridge.home,
        directory,
        paired,
        "codex",
        {"process_alive": True, "age_seconds": 500, "stale": True},
        {**supervision.DEFAULTS, "inactive_after": 1},
    )


def test_a_wake_without_a_session_process_is_retried_when_it_returns(
    bridge, paired, monkeypatch
):
    actors = registered(bridge, paired)
    directory = Path(paired["lanes"]["codex"]).parent
    write_json(directory / "codex-activity.json", {"activity": "stopped"})
    send(bridge, actors["claude"], "codex")
    spawned = supervision.subprocess.Popen

    def guard(*args, **kwargs):
        if args and "agent_parley.cli" in list(args[0]):
            pytest.fail("resumed without a session")
        return spawned(*args, **kwargs)

    monkeypatch.setattr(supervision.subprocess, "Popen", guard)
    calls = []
    monkeypatch.setattr(
        terminal, "request", lambda *args: calls.append(args) or "accepted"
    )
    config = {**supervision.DEFAULTS, "inactive_after": 1}
    path = directory / "codex-wake.json"
    sampled(bridge, paired, directory, "codex")
    supervision.wake(
        bridge.home,
        directory,
        paired,
        "codex",
        {"process_alive": False},
        config,
    )
    first = json.loads(path.read_text())
    assert first["result"] == "manual attention required"
    assert first["attempts"] == 1
    assert first["next_at"] == pytest.approx(first["at"] + 1)
    rewake(bridge, paired, directory, "codex", at=0)

    supervision.wake(
        bridge.home,
        directory,
        paired,
        "codex",
        {"process_alive": False},
        config,
    )

    parked = json.loads(path.read_text())
    assert not calls
    assert parked["attempts"] == 1
    assert parked["blocked"] == "its session process is not running"
    assert not parked.get("exhausted_at")
    write_json(
        directory / "codex-activity.json",
        {
            "activity": "idle",
            "updated": time.time() - 500,
            "session_pid": os.getpid(),
            "session_ticks": process.start_ticks(os.getpid()),
        },
    )
    sampled(bridge, paired, directory, "codex")
    observed = sampled(bridge, paired, directory, "codex")

    supervision.wake(bridge.home, directory, paired, "codex", observed, config)

    retried = json.loads(path.read_text())
    assert calls
    assert retried["attempts"] == 2
    assert retried["result"] == "accepted"
    assert retried["blocked"] == ""


def test_a_wake_blocked_by_exhausted_capacity_is_retried_at_the_reset(
    bridge, paired, monkeypatch
):
    actors = registered(bridge, paired)
    directory = Path(paired["lanes"]["codex"]).parent
    write_json(
        directory / "codex-activity.json",
        {
            "activity": "idle",
            "updated": time.time() - 500,
            "session_pid": os.getpid(),
            "session_ticks": process.start_ticks(os.getpid()),
        },
    )
    message = send(bridge, actors["claude"], "codex")
    reset_at = time.time() + 900
    supervision.record_capacity(
        directory,
        "codex",
        {
            "state": "exhausted",
            "observed_at": time.time(),
            "reset_at": reset_at,
            "source": "codex-session-record",
            "session_id": "rollout-capacity",
            "observation_id": "refusal-1",
        },
    )
    calls = []
    monkeypatch.setattr(
        terminal, "request", lambda *args: calls.append(args) or "accepted"
    )
    config = {**supervision.DEFAULTS, "inactive_after": 1}
    observed = sampled(bridge, paired, directory, "codex")
    path = directory / "codex-wake.json"
    rewake(
        bridge,
        paired,
        directory,
        "codex",
        at=0,
        backlog=[str(message["id"])],
        attempts=1,
        result="manual attention required",
    )

    supervision.wake(bridge.home, directory, paired, "codex", observed, config)

    parked = json.loads(path.read_text())
    assert not calls
    assert parked["attempts"] == 1
    assert parked["next_at"] == reset_at
    assert "provider capacity is exhausted" in parked["blocked"]
    supervision.record_capacity(
        directory,
        "codex",
        {
            "state": "exhausted",
            "observed_at": time.time(),
            "reset_at": time.time() - 1,
            "source": "codex-session-record",
            "session_id": "rollout-capacity",
            "observation_id": "refusal-2",
        },
    )

    supervision.wake(bridge.home, directory, paired, "codex", observed, config)

    retried = json.loads(path.read_text())
    assert calls
    assert retried["attempts"] == 2
    assert retried["result"] == "accepted"


def test_a_declared_wait_defers_a_wake_until_its_next_check(
    bridge, paired, monkeypatch
):
    actors = registered(bridge, paired)
    directory = Path(paired["lanes"]["codex"]).parent
    write_json(
        directory / "codex-activity.json",
        {
            "activity": "idle",
            "updated": time.time() - 500,
            "session_pid": os.getpid(),
            "session_ticks": process.start_ticks(os.getpid()),
        },
    )
    message = send(bridge, actors["claude"], "codex")
    until = time.time() + 900
    supervision.record_wait(
        directory, "codex", "3 background jobs", until, 3600
    )
    calls = []
    monkeypatch.setattr(
        terminal, "request", lambda *args: calls.append(args) or "accepted"
    )
    config = {**supervision.DEFAULTS, "inactive_after": 1}
    observed = sampled(bridge, paired, directory, "codex")
    path = directory / "codex-wake.json"
    rewake(
        bridge,
        paired,
        directory,
        "codex",
        at=0,
        backlog=[str(message["id"])],
        attempts=1,
        result="manual attention required",
    )

    supervision.wake(bridge.home, directory, paired, "codex", observed, config)

    parked = json.loads(path.read_text())
    assert not calls
    assert parked["attempts"] == 1
    assert parked["next_at"] == until
    assert "waiting on 3 background jobs" in parked["blocked"]

    supervision.record_wait(
        directory, "codex", "3 background jobs", time.time() - 1, 3600
    )

    supervision.wake(bridge.home, directory, paired, "codex", observed, config)

    retried = json.loads(path.read_text())
    assert calls
    assert retried["attempts"] == 2
    assert retried["result"] == "accepted"


def test_direct_mail_about_a_held_issue_wakes_through_a_declared_wait(
    bridge, paired, monkeypatch
):
    actors = registered(bridge, paired)
    lane = Path(paired["lanes"]["codex"])
    directory = lane.parent
    bridge.issue(lane, "claim", "7")
    write_json(
        directory / "codex-activity.json",
        {
            "activity": "idle",
            "updated": time.time() - 500,
            "session_pid": os.getpid(),
            "session_ticks": process.start_ticks(os.getpid()),
        },
    )
    supervision.record_wait(
        directory, "codex", "gpu forecast run", time.time() + 900, 3600
    )
    calls = []
    monkeypatch.setattr(
        terminal, "request", lambda *args: calls.append(args) or "accepted"
    )
    config = {**supervision.DEFAULTS, "inactive_after": 1}
    observed = sampled(bridge, paired, directory, "codex")
    path = directory / "codex-wake.json"
    other = store.call(
        bridge.home,
        actors["claude"],
        "send_message",
        {
            "to": ["codex"],
            "subject": "Lunch",
            "body_md": "Unrelated to #70",
            "idempotency_key": "other",
        },
    )
    assert (
        supervision.held_issue_mail(bridge.home, directory, paired, "codex")
        == []
    )
    rewake(
        bridge,
        paired,
        directory,
        "codex",
        at=0,
        backlog=[str(other["id"])],
        attempts=1,
        result="manual attention required",
    )

    supervision.wake(bridge.home, directory, paired, "codex", observed, config)

    assert not calls
    assert (
        "waiting on gpu forecast run" in json.loads(path.read_text())["blocked"]
    )

    asked = store.call(
        bridge.home,
        actors["claude"],
        "send_message",
        {
            "to": ["codex"],
            "subject": "Forecast",
            "body_md": "Is #7 still running?",
            "idempotency_key": "asked",
        },
    )
    assert supervision.held_issue_mail(
        bridge.home, directory, paired, "codex"
    ) == [str(asked["id"])]

    supervision.wake(bridge.home, directory, paired, "codex", observed, config)

    woken = json.loads(path.read_text())
    assert calls
    assert woken["result"] == "accepted"
    assert str(asked["id"]) in woken["backlog"]
    assert str(other["id"]) in woken["backlog"]


def test_held_issue_mail_is_searched_only_while_a_wait_is_in_force(tmp_path):
    searched = []
    observed = {"record": {}, "process_alive": True}

    def search():
        searched.append(True)
        return []

    assert supervision._wake_block(
        tmp_path, "codex", observed, {}, 60, search
    ) == ("", 0.0)
    assert searched == []
    supervision.record_wait(
        tmp_path, "codex", "gpu forecast run", time.time() + 900, 3600
    )
    blocked, _ = supervision._wake_block(
        tmp_path, "codex", observed, {}, 60, search
    )
    assert blocked == "waiting on gpu forecast run"
    assert searched == [True]
    assert supervision._wake_block(
        tmp_path, "codex", observed, {}, 60, lambda: ["1"]
    ) == ("", 0.0)


def test_a_declared_wait_is_capped_by_its_project_ceiling(tmp_path):
    now = time.time()

    recorded = supervision.record_wait(
        tmp_path, "codex", "a long benchmark", now + 7200, 600
    )

    assert recorded["until"] <= time.time() + 600
    assert recorded["until"] < now + 7200
    read_back = supervision.published_wait(tmp_path, "codex")
    assert read_back["reason"] == "a long benchmark"
    assert read_back["until"] == recorded["until"]


def test_status_reports_the_next_wake_or_the_exhausted_budget(
    bridge, paired, capsys
):
    registered(bridge, paired)
    directory = Path(paired["lanes"]["codex"]).parent
    rewake(
        bridge,
        paired,
        directory,
        "codex",
        at=time.time() - 10,
        backlog=["1"],
        attempts=1,
        result="manual attention required",
        blocked="its screen state is waiting for approval",
        next_at=time.time() + 120,
    )

    bridge.status(cli.Selection(participant="codex"))

    scheduled = capsys.readouterr().out
    assert "Next wake in 1" in scheduled
    assert "blocked: its screen state is waiting for approval" in scheduled
    rewake(
        bridge,
        paired,
        directory,
        "codex",
        at=time.time() - 10,
        backlog=["1"],
        attempts=supervision.WORK_WAKE_ATTEMPTS,
        result="manual attention required",
        blocked="its session process is not running",
        next_at=None,
        exhausted_at=time.time() - 5,
    )

    bridge.status(cli.Selection(participant="codex"))

    printed = capsys.readouterr().out
    assert f"attempt 3/{supervision.WORK_WAKE_ATTEMPTS}" in printed
    assert (
        "Wake budget exhausted; last cause: its session process is not running"
        in printed
    )


def test_dead_manual_session_resumes_from_its_recorded_session(
    bridge, paired, monkeypatch
):
    actors = registered(bridge, paired)
    directory = Path(paired["lanes"]["codex"]).parent
    native = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"]
    )
    native_ticks = process.start_ticks(native.pid)
    native.kill()
    native.wait(timeout=5)
    write_json(
        directory / "codex-activity.json",
        {
            "activity": "working",
            "session_id": "manual-session",
            "session_pid": native.pid,
            "session_ticks": native_ticks,
        },
    )
    send(bridge, actors["claude"], "codex")

    class Child:
        pid = 4321

    launched = []
    monkeypatch.setattr(
        supervision.subprocess,
        "Popen",
        lambda command, **kwargs: launched.append((command, kwargs)) or Child(),
    )
    monkeypatch.setattr(supervision, "track_launcher", lambda child: None)
    observed = sampled(
        bridge, paired, directory, "codex", 300, session="manual-session"
    )
    assert observed["process_alive"] is False
    supervision.wake(
        bridge.home,
        directory,
        paired,
        "codex",
        observed,
        supervision.DEFAULTS,
    )
    assert launched
    assert "--resume" in launched[0][0]
    record = json.loads((directory / "codex-wake.json").read_text())
    assert record["result"] == "resume requested (launcher 4321)"


@pytest.mark.parametrize("opted", [False, True])
def test_a_claude_resume_without_the_approval_opt_in_is_withheld(
    bridge, paired, monkeypatch, opted
):
    actors = registered(bridge, paired)
    directory = Path(paired["lanes"]["claude"]).parent
    native = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"]
    )
    native_ticks = process.start_ticks(native.pid)
    native.kill()
    native.wait(timeout=5)
    write_json(
        directory / "claude-activity.json",
        {
            "activity": "working",
            "session_id": "manual-session",
            "session_pid": native.pid,
            "session_ticks": native_ticks,
        },
    )
    send(bridge, actors["codex"], "claude")

    class Child:
        pid = 4321

    launched = []
    monkeypatch.setattr(
        supervision.subprocess,
        "Popen",
        lambda command, **kwargs: launched.append(command) or Child(),
    )
    monkeypatch.setattr(supervision, "track_launcher", lambda child: None)
    manifest = {
        **paired,
        "supervision": {**(paired.get("supervision") or {})},
    }
    if opted:
        manifest["supervision"]["approve_bridge_tools"] = True
    assert supervision.opt_in_missing(bridge.home, manifest, "claude") is (
        not opted
    )
    supervision.wake(
        bridge.home,
        directory,
        manifest,
        "claude",
        sampled(
            bridge, paired, directory, "claude", 300, session="manual-session"
        ),
        supervision.DEFAULTS,
    )
    record = json.loads((directory / "claude-wake.json").read_text())
    if opted:
        assert launched and "--resume" in launched[0]
    else:
        assert not launched
        assert record["result"] == supervision.OPT_IN_MISSING
        assert record["attempts"] == 0
        assert not record["exhausted_at"]


@pytest.mark.parametrize("adapter", roster.ADAPTERS)
def test_every_adapter_that_carries_the_opt_in_reports_its_gap(bridge, adapter):
    manifest = {"participants": {"lane": {"provider": adapter}}}
    carried = roster.APPROVAL[adapter] is not None
    assert supervision.opt_in_missing(bridge.home, manifest, "lane") is carried
    assert supervision.opt_in_adapter(bridge.home, manifest, "lane") == (
        adapter if carried else ""
    )
    manifest["supervision"] = {dialogs.PRE_APPROVE: True}
    assert not supervision.opt_in_missing(bridge.home, manifest, "lane")


def test_auto_mode_clears_the_gap_of_a_claude_lane_only(bridge):
    manifest = {
        "supervision": {dialogs.AUTO_MODE: True},
        "participants": {
            "claude": {"provider": "claude"},
            "gemini": {"provider": "gemini"},
        },
    }
    assert not supervision.opt_in_missing(bridge.home, manifest, "claude")
    assert supervision.opt_in_missing(bridge.home, manifest, "gemini")


def test_operator_stop_holds_until_the_next_launch(
    bridge, repo, paired, monkeypatch
):
    actors = registered(bridge, paired)
    directory = Path(paired["lanes"]["codex"]).parent
    path = directory / "codex-activity.json"
    write_json(
        path,
        {
            "activity": "stopped",
            "session_id": "12345678-abcd-1234-abcd-123456789abc",
        },
    )
    send(bridge, actors["claude"], "codex")
    bridge.stop(repo, "codex")
    assert json.loads(path.read_text())["operator_stopped"] is True

    class Child:
        pid = 4321

    launched = []
    popen = supervision.subprocess.Popen
    monkeypatch.setattr(
        supervision.subprocess,
        "Popen",
        lambda command, **kwargs: launched.append(command) or Child(),
    )
    monkeypatch.setattr(supervision, "track_launcher", lambda child: None)
    observed = sampled(bridge, paired, directory, "codex", 300)
    supervision.wake(
        bridge.home, directory, paired, "codex", observed, supervision.DEFAULTS
    )
    assert launched == []

    monkeypatch.setattr(supervision.subprocess, "Popen", popen)
    monkeypatch.setattr(bridge, "up", lambda: None)

    async def identity(*args):
        return {"registration_token": "test-only"}

    monkeypatch.setattr(bridge, "identity", identity)
    from agent_parley import cli

    original = cli.shutil.which
    monkeypatch.setattr(
        cli.shutil,
        "which",
        lambda name: "/bin/true" if name == "codex" else original(name),
    )
    monkeypatch.setattr(terminal, "run", lambda *args, **kwargs: 0)
    assert bridge.launch("codex", repo, terminal.PROMPT, resume=True) == 0
    assert "operator_stopped" not in json.loads(path.read_text())


def test_stopping_a_live_session_with_backlog_is_not_resumed_by_a_wake(
    bridge, repo, paired, monkeypatch
):
    actors = registered(bridge, paired)
    directory = Path(paired["lanes"]["codex"]).parent
    path = directory / "codex-activity.json"
    send(bridge, actors["claude"], "codex")
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"]
    )
    try:
        write_json(
            path,
            {
                "activity": "idle",
                "session_id": "12345678-abcd-1234-abcd-123456789abc",
                "session_pid": child.pid,
                "session_ticks": process.start_ticks(child.pid),
                "updated": time.time(),
            },
        )
        assert "ended from the base checkout" in bridge.stop(repo, "codex")
        assert child.wait(10) is not None
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(10)
    stopped = json.loads(path.read_text())
    assert stopped["operator_stopped"] is True
    assert stopped["session_id"]

    class Child:
        pid = 4321

    launched = []
    monkeypatch.setattr(
        supervision.subprocess,
        "Popen",
        lambda command, **kwargs: launched.append(command) or Child(),
    )
    monkeypatch.setattr(supervision, "track_launcher", lambda child: None)
    observed = sampled(
        bridge, paired, directory, "codex", 300, stopped["session_id"]
    )
    supervision.wake(
        bridge.home, directory, paired, "codex", observed, supervision.DEFAULTS
    )
    assert launched == []
    stopped.pop("operator_stopped")
    write_json(path, stopped)
    supervision.wake(
        bridge.home, directory, paired, "codex", observed, supervision.DEFAULTS
    )
    assert launched and "--resume" in launched[0]


@pytest.mark.parametrize(
    "answer,result,attempts",
    [
        ("accepted", "accepted", 1),
        ("unavailable", supervision.SESSION_HELD, 0),
    ],
)
def test_a_held_session_lock_is_asked_over_its_socket_never_resumed(
    bridge, paired, monkeypatch, answer, result, attempts
):
    actors = registered(bridge, paired)
    directory = Path(paired["lanes"]["codex"]).parent
    write_json(
        directory / "codex-activity.json",
        {"activity": "stopped", "session_id": "live-session"},
    )
    send(bridge, actors["claude"], "codex")
    monkeypatch.setattr(
        supervision.subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("resumed a held session"),
    )
    asked = []
    monkeypatch.setattr(
        terminal,
        "request",
        lambda directory, name: asked.append(name) or answer,
    )
    with state.lock(directory / "codex.session.lock"):
        assert supervision.session_held(directory, "codex")
        supervision.wake(
            bridge.home,
            directory,
            paired,
            "codex",
            {"process_alive": False},
            supervision.DEFAULTS,
        )
    assert not supervision.session_held(directory, "codex")
    assert asked == ["codex"]
    record = json.loads((directory / "codex-wake.json").read_text())
    assert record["result"] == result
    assert record["attempts"] == attempts


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="only Linux reports a process's working directory",
)
def test_a_client_in_the_lane_worktree_withholds_the_dead_verdict(
    bridge, paired
):
    registered(bridge, paired)
    lane = Path(paired["lanes"]["codex"])
    directory = lane.parent
    command = roster.provider(
        bridge.home, paired["participants"]["codex"]["provider"]
    )["command"]
    gone = subprocess.Popen([sys.executable, "-c", ""])
    gone.wait(10)
    write_json(
        directory / "codex-activity.json",
        {
            "session_id": "s1",
            "session_pid": gone.pid,
            "session_ticks": "1",
            "activity": "stopped",
            "updated": time.time() - 7200,
        },
    )
    assert supervision.rebind_sessions(bridge.home, directory, paired) == []
    sampled(bridge, paired, directory, "codex", 300)
    assert supervision.dead_reason(bridge.home, directory, paired, "codex", 0)
    client = subprocess.Popen(
        ["bash", "-c", f"exec -a {command} sleep 120"], cwd=lane
    )
    try:
        deadline = time.monotonic() + 10
        cmdline = Path(f"/proc/{client.pid}/cmdline")
        while not cmdline.read_bytes().startswith(command.encode()):
            assert time.monotonic() < deadline
            time.sleep(0.05)

        assert supervision.rebind_sessions(bridge.home, directory, paired) == [
            "codex"
        ]

        published = checkpoints.activity(directory, "codex")
        assert published["session_pid"] == client.pid
        assert published["session_id"] == "s1"
        sampled(bridge, paired, directory, "codex", 300)
        recorded = supervision.condition(bridge.home, paired["root"], "codex")
        assert recorded["state"] == lanes.STARTING
        assert not supervision.dead_reason(
            bridge.home, directory, paired, "codex", 0
        )
    finally:
        client.kill()
        client.wait(10)


def test_manual_session_without_process_identity_requires_attention(
    bridge, paired, monkeypatch
):
    actors = registered(bridge, paired)
    directory = Path(paired["lanes"]["codex"]).parent
    write_json(
        directory / "codex-activity.json",
        {
            "activity": "working",
            "session_id": "manual-session",
            "updated": time.time() - 500,
        },
    )
    send(bridge, actors["claude"], "codex")
    monkeypatch.setattr(
        supervision.subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("adopted an unknown process"),
    )
    observed = supervision.presence(directory, "codex")
    assert observed["state"] == supervision.UNKNOWN
    assert observed["process_alive"] is None
    supervision.wake(
        bridge.home,
        directory,
        paired,
        "codex",
        observed,
        supervision.DEFAULTS,
    )
    record = json.loads((directory / "codex-wake.json").read_text())
    assert record["result"] == "manual attention required"


def test_stopped_lane_without_a_recorded_session_requires_attention(
    bridge, paired, monkeypatch
):
    actors = registered(bridge, paired)
    directory = Path(paired["lanes"]["codex"]).parent
    write_json(directory / "codex-activity.json", {"activity": "stopped"})
    send(bridge, actors["claude"], "codex")
    monkeypatch.setattr(
        supervision.subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("resumed without a session"),
    )
    supervision.wake(
        bridge.home,
        directory,
        paired,
        "codex",
        {"process_alive": False},
        supervision.DEFAULTS,
    )
    record = json.loads((directory / "codex-wake.json").read_text())
    assert record["result"] == "manual attention required"


def test_global_wake_opt_out_wins_over_project(bridge, paired, monkeypatch):
    registered(bridge, paired)
    directory = Path(paired["lanes"]["codex"]).parent
    write_json(bridge.home / "supervision.json", {"wake": False})
    manifest = roster.read(directory)
    manifest["supervision"] = {"wake": True}
    write_json(directory / "project.json", manifest)
    monkeypatch.setattr(
        supervision, "wake", lambda *args: pytest.fail("wake disabled")
    )
    supervision.poll(bridge.home, directory)


@pytest.mark.parametrize("provider", ["claude", "codex", "gemini"])
def test_stopped_resume_keeps_native_interactive_permissions(
    bridge, repo, tmp_path, monkeypatch, capsys, provider
):
    manifest = bridge.add_participant(repo, provider, provider)
    directory = Path(manifest["lanes"][provider]).parent
    write_json(
        directory / f"{provider}-activity.json",
        {
            "activity": "stopped",
            "session_id": "12345678-abcd-1234-abcd-123456789abc",
        },
    )
    monkeypatch.setattr(bridge, "up", lambda: None)

    async def identity(*args):
        return {"registration_token": "test-only"}

    monkeypatch.setattr(bridge, "identity", identity)
    from agent_parley import cli

    original = cli.shutil.which
    monkeypatch.setattr(
        cli.shutil,
        "which",
        lambda name: "/bin/true" if name == provider else original(name),
    )
    captured = []
    monkeypatch.setattr(
        terminal,
        "run",
        lambda command, *args, **kwargs: captured.append(command) or 0,
    )
    assert bridge.launch(provider, repo, terminal.PROMPT, resume=True) == 0
    warned = "no bridge tool approval is recorded" in capsys.readouterr().err
    assert warned is (roster.APPROVAL[provider] is not None)
    assert "12345678-abcd-1234-abcd-123456789abc" in captured[0]
    assert "exec" not in captured[0] and "--print" not in captured[0]
    assert not any(
        "bypass" in argument or "skip-permission" in argument
        for argument in captured[0]
    )


def test_reaping_collects_an_exited_launcher_without_blocking():
    exited = subprocess.Popen(
        [sys.executable, "-c", "raise SystemExit(0)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    running = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        supervision.track_launcher(exited)
        supervision.track_launcher(running)
        exited.wait(timeout=10)
        deadline = time.monotonic() + 10
        while supervision.reap_launchers() > 1:
            assert time.monotonic() < deadline
            time.sleep(0.05)
        assert exited.returncode == 0
        assert running.poll() is None
        assert not zombies_of(os.getpid())
    finally:
        running.kill()
        running.wait(timeout=10)
        supervision.reap_launchers()


def zombies_of(parent):
    found = []
    if not Path("/proc").is_dir():
        return found
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
        except (OSError, IndexError):
            continue
        if fields[0] == "Z" and fields[1] == str(parent):
            found.append(int(entry.name))
    return found


def idle_lane(directory, name, age, activity="idle", **extra):
    """Records a live session whose last native checkpoint is `age` old."""
    write_json(
        directory / f"{name}-activity.json",
        {
            "activity": activity,
            "updated": time.time() - age,
            "session_pid": os.getpid(),
            "session_ticks": process.start_ticks(os.getpid()),
            **extra,
        },
    )


def exhausted(directory, name, ago, **extra):
    """Records a usage-limit exhaustion that named no reset."""
    return supervision.record_capacity(
        directory,
        name,
        {
            "state": "exhausted",
            "observed_at": time.time() - ago,
            "reset_at": None,
            "source": "native-dialog",
            "observation_id": f"dialog:{ago}",
            **extra,
        },
    )


def empty_commit(lane):
    """Records one commit in a lane, the progress a working lane makes."""
    subprocess.run(
        [
            "git",
            "-C",
            str(lane),
            "-c",
            "user.name=Lane",
            "-c",
            "user.email=lane@example.invalid",
            "commit",
            "--allow-empty",
            "-qm",
            "progress",
        ],
        check=True,
    )


def test_an_exhaustion_is_attributed_to_the_lanes_session(bridge, paired):
    directory = Path(paired["lanes"]["claude"]).parent
    idle_lane(directory, "claude", 10, session_id="claude-session")
    assert exhausted(directory, "claude", 5)["session_id"] == "claude-session"


def test_an_exhaustion_without_a_session_never_fails_the_poll(
    bridge, paired, monkeypatch
):
    registered(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    directory = lane.parent
    bridge.issue(lane, "claim", "1")
    idle_lane(directory, "claude", 1000)
    exhausted(directory, "claude", 900)
    monkeypatch.setattr(terminal, "request", lambda *args: "accepted")
    supervision.poll(bridge.home, directory)
    assert issues.supervision_error(directory) is None


def test_a_failing_stage_is_recorded_and_the_rest_of_the_poll_runs(
    bridge, paired, monkeypatch
):
    registered(bridge, paired)
    directory = Path(paired["lanes"]["claude"]).parent

    def broken(*args):
        raise ValueError("malformed record")

    woken = []
    monkeypatch.setattr(supervision, "work", broken)
    monkeypatch.setattr(
        supervision,
        "wake",
        lambda home, path, manifest, name, *rest: woken.append(name),
    )
    supervision.poll(bridge.home, directory)
    assert sorted(woken) == ["claude", "codex"]
    error = issues.supervision_error(directory)
    assert error["detail"].startswith("work: ValueError: malformed record at ")
    assert cli.supervision_failure(error).startswith("Supervision: failing")
    monkeypatch.setattr(supervision, "work", lambda *args: None)
    supervision.poll(bridge.home, directory)
    assert issues.supervision_error(directory) is None


def test_one_lanes_unexpected_wake_failure_never_skips_the_next(
    bridge, paired, monkeypatch
):
    registered(bridge, paired)
    directory = Path(paired["lanes"]["claude"]).parent
    woken = []

    def wake(home, path, manifest, name, *rest):
        if name == "claude":
            raise KeyError("holder")
        woken.append(name)

    monkeypatch.setattr(supervision, "wake", wake)
    supervision.poll(bridge.home, directory)
    assert woken == ["codex"]
    detail = issues.supervision_error(directory)["detail"]
    assert detail.startswith("wake claude: KeyError: 'holder' at ")
    assert "test_supervision.py:" in detail
    polled = supervision.last_poll(directory)
    assert polled["failed"] == 1
    assert polled["clean_at"] is None


def test_a_held_write_lock_delays_the_presence_write_without_skipping_it(
    bridge, paired
):
    registered(bridge, paired)
    directory = Path(paired["lanes"]["claude"]).parent
    holder = sqlite3.connect(
        bridge.home / store.DATABASE, check_same_thread=False
    )
    holder.execute("BEGIN IMMEDIATE")
    releaser = threading.Timer(0.5, holder.commit)
    releaser.start()
    supervision.poll(bridge.home, directory)
    releaser.join()
    holder.close()
    assert issues.supervision_error(directory) is None
    with store.connect(bridge.home) as db:
        assert db.execute(
            "SELECT count(*) FROM participant_presence"
        ).fetchone()[0] == len(paired["participants"])


def test_an_unexpected_poll_exception_is_recorded_and_supervision_survives(
    bridge, paired, monkeypatch
):
    registered(bridge, paired)
    directory = Path(paired["lanes"]["claude"]).parent
    stopped = threading.Event()
    polls = []

    def broken(home, path):
        polls.append(path)
        if len(polls) == 1:
            raise KeyError("waiting")

    def reap():
        if len(polls) > 1:
            stopped.set()

    monkeypatch.setattr(supervision, "poll", broken)
    monkeypatch.setattr(supervision, "reap_launchers", reap)
    monkeypatch.setattr(
        supervision,
        "configuration",
        lambda home, manifest: {**supervision.DEFAULTS, "interval": 0},
    )
    supervision.run(bridge.home, stopped)
    assert len(polls) == 2
    detail = issues.supervision_error(directory)["detail"]
    assert detail.startswith("poll: KeyError: 'waiting' at ")


def test_status_reports_when_supervision_last_polled(bridge, paired, capsys):
    registered(bridge, paired)
    directory = Path(paired["lanes"]["claude"]).parent
    supervision.poll(bridge.home, directory)
    polled = supervision.last_poll(directory)
    assert polled["clean_at"] == polled["at"]
    project = bridge.status_snapshot()["projects"][0]
    assert project["supervision_poll"] == polled
    bridge.status()
    assert "Supervision: last poll 0s ago in " in capsys.readouterr().out
    polled["stages"] = {"work": 0.25, "wake claude": 4.5}
    assert ", slowest wake claude 4.50s" in cli.supervision_liveness(polled)


def test_a_stale_working_label_on_a_live_process_is_woken(
    bridge, paired, monkeypatch
):
    actors = registered(bridge, paired)
    directory = Path(paired["lanes"]["codex"]).parent
    idle_lane(directory, "codex", 1000, activity="working")
    send(bridge, actors["claude"], "codex")
    calls = []
    monkeypatch.setattr(
        terminal, "request", lambda *args: calls.append(args) or "accepted"
    )
    config = {**supervision.DEFAULTS, "inactive_after": 300}
    observed = sampled(bridge, paired, directory, "codex", 300)
    supervision.wake(bridge.home, directory, paired, "codex", observed, config)
    assert len(calls) == 1


def test_a_current_working_label_still_blocks_the_wake(
    bridge, paired, monkeypatch
):
    actors = registered(bridge, paired)
    directory = Path(paired["lanes"]["codex"]).parent
    idle_lane(directory, "codex", 120, activity="working")
    send(bridge, actors["claude"], "codex")
    rewake(
        bridge,
        paired,
        directory,
        "codex",
        at=time.time() - 1000,
        attempts=1,
        result="accepted",
    )
    monkeypatch.setattr(
        terminal, "request", lambda *args: pytest.fail("woke a working lane")
    )
    config = {**supervision.DEFAULTS, "inactive_after": 300}
    observed = sampled(bridge, paired, directory, "codex", 300)
    supervision.wake(bridge.home, directory, paired, "codex", observed, config)
    record = json.loads((directory / "codex-wake.json").read_text())
    assert record["attempts"] == 1
    assert record["result"] == "accepted"


def test_an_exhaustion_without_a_reset_is_probed_on_a_backoff():
    observation = {"observed_at": 100.0, "probes": 0}
    assert supervision.exhaustion_probe_due(observation, 60) == 160
    later = {"observed_at": 100.0, "probed_at": 500.0, "probes": 2}
    assert supervision.exhaustion_probe_due(later, 60) == 740
    capped = {"observed_at": 100.0, "probes": 30}
    assert supervision.exhaustion_probe_due(capped, 60) == 3700


def test_an_old_exhaustion_without_a_reset_is_woken_and_cleared(
    bridge, paired, monkeypatch
):
    actors = registered(bridge, paired)
    directory = Path(paired["lanes"]["codex"]).parent
    idle_lane(directory, "codex", 1000)
    exhausted(directory, "codex", 1000)
    send(bridge, actors["claude"], "codex")
    calls = []
    monkeypatch.setattr(
        terminal, "request", lambda *args: calls.append(args) or "accepted"
    )
    config = {**supervision.DEFAULTS, "inactive_after": 300}
    observed = sampled(bridge, paired, directory, "codex", 300)
    supervision.wake(bridge.home, directory, paired, "codex", observed, config)
    assert len(calls) == 1
    capacity = supervision.published_capacity(directory, "codex")
    assert capacity["state"] == "available"
    assert capacity["source"] == "wake-accepted"


def test_a_refused_probe_pushes_the_next_one_back(bridge, paired):
    directory = Path(paired["lanes"]["codex"]).parent
    exhausted(directory, "codex", 1000)
    supervision.probe_exhaustion(directory, "codex", "busy:turn")
    capacity = supervision.published_capacity(directory, "codex")
    assert capacity["state"] == "exhausted" and capacity["probes"] == 1
    assert supervision.exhaustion_probe_due(capacity, 300) == pytest.approx(
        capacity["probed_at"] + 600
    )


def test_bare_stops_escalate_and_a_commit_resets_the_budget(
    bridge, paired, monkeypatch
):
    actors = registered(bridge, paired)
    lane = Path(paired["lanes"]["codex"])
    directory = lane.parent
    idle_lane(directory, "codex", 1000)
    send(bridge, actors["claude"], "codex")
    monkeypatch.setattr(terminal, "request", lambda *args: "accepted")
    config = {**supervision.DEFAULTS, "inactive_after": 1}
    observed = supervision.presence(directory, "codex", 1)

    def woken():
        supervision.wake(
            bridge.home, directory, paired, "codex", observed, config
        )
        record = rewake(bridge, paired, directory, "codex", at=0)
        with (directory / "codex-events.jsonl").open("a") as stream:
            stream.write(json.dumps({"ts": time.time(), "event": "Stop"}))
            stream.write("\n")
        return record

    for _ in range(3):
        record = woken()
    assert record["attempts"] == 3 and record["exhausted_at"]
    empty_commit(lane)
    record = woken()
    assert record["attempts"] == 1 and record["exhausted_at"] is None


def test_a_spent_budget_asks_the_operator_about_orphaned_claims(
    bridge, paired, monkeypatch
):
    actors = registered(bridge, paired)
    lane = Path(paired["lanes"]["codex"])
    directory = lane.parent
    bridge.issue(lane, "claim", "1")
    with state.lock(directory / "issues.lock"):
        ledger = issues.snapshot(directory)
        ledger["issues"]["1"]["orphan"] = {"id": "x", "owner": "codex"}
        write_json(directory / "issues.json", ledger)
    idle_lane(directory, "codex", 1000)
    send(bridge, actors["claude"], "codex")
    monkeypatch.setattr(terminal, "request", lambda *args: "accepted")
    sent = []
    monkeypatch.setattr(
        supervision.notify,
        "deliver",
        lambda directory, agent, event, fields: sent.append(
            (agent, event, dict(fields))
        ),
    )
    config = {**supervision.DEFAULTS, "inactive_after": 1}
    observed = supervision.presence(directory, "codex", 1)
    for _ in range(supervision.WORK_WAKE_ATTEMPTS):
        supervision.wake(
            bridge.home, directory, paired, "codex", observed, config
        )
        record = rewake(bridge, paired, directory, "codex", at=0)
    assert record["exhausted_at"] and not sent
    supervision.wake(bridge.home, directory, paired, "codex", observed, config)
    agent, event, fields = sent[0]
    assert (agent, event) == ("codex", supervision.notify.Event.ORPHAN_DECISION)
    assert fields["issue"] == "#1"
    assert fields["since"] == int(record["exhausted_at"])
    assert "--take-orphaned" in fields["detail"]
    assert fields["detail"].endswith(
        "returns its claims and reservations once it has been dead for "
        "the orphan ceiling."
    )
    assert issues.snapshot(directory)["issues"]["1"]["owner"] == "codex"


def test_a_recovery_burst_queues_one_capture_with_merged_evidence(
    bridge, monkeypatch
):
    started = threading.Event()
    release = threading.Event()
    calls = []

    def capture(directory, agent, payload, timeout, evidence=None):
        calls.append((payload, evidence))
        started.set()
        release.wait(20)
        return True

    monkeypatch.setattr(checkpoints, "recover", capture)
    lane = ("project", "codex")
    passing = {
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"cmd": "make check"},
        "tool_response": {"exit_code": 0},
    }

    def owed(payload):
        return {
            "directory": str(bridge.home),
            "participant": "codex",
            "payload": payload,
        }

    with server.Server(bridge.home, bridge.config) as instance:
        instance.recover(lane, owed({"step": 0}))
        assert started.wait(20)
        instance.recover(lane, owed(passing))
        for step in range(1, 4):
            instance.recover(lane, owed({"step": step}))
        release.set()
        deadline = time.monotonic() + 20
        while lane in instance.recovering:
            assert time.monotonic() < deadline
            time.sleep(0.05)

    assert [payload for payload, _ in calls] == [{"step": 0}, {"step": 3}]
    assert [evidence for _, evidence in calls] == [None, passing]


def test_a_failed_issue_reading_is_not_repeated_inside_the_retry_window(
    tmp_path, monkeypatch
):
    calls = []
    monkeypatch.setattr(
        supervision.forge,
        "issue_completion",
        lambda root, number: calls.append(number),
    )
    for _ in range(5):
        assert supervision._issue_reading(tmp_path, "7") is None
    assert calls == ["7"]
    key = (str(tmp_path), "7")
    supervision._issue_readings[key] = (time.time() - 1, None)
    supervision._issue_reading(tmp_path, "7")
    assert calls == ["7", "7"]


def test_a_refusing_forge_is_left_alone_until_the_retry_window_ends(
    tmp_path, monkeypatch
):
    def unread(*args, **kwargs):
        raise AssertionError("the forge is not read while it refuses")

    monkeypatch.setattr(supervision.forge, "issue_completion", unread)
    monkeypatch.setattr(supervision.forge, "open_pull_requests", unread)
    monkeypatch.setattr(supervision.forge, "integration_landings", unread)
    monkeypatch.setattr(
        supervision.forge,
        "_failure",
        {"at": time.time(), "kind": "auth", "detail": "HTTP 401"},
    )
    manifest = {
        "root": str(tmp_path),
        "participants": {"claude": {"branch": "lane"}},
    }
    ledger = {"issues": {"9": {"owner": "claude"}}}
    assert supervision.completed_claims(manifest, ledger) == {}
    assert supervision._landing_reading(tmp_path, "integration") == {}
    supervision.pull_request_wakes(tmp_path, tmp_path, manifest)
    assert not (tmp_path / supervision.PULL_REQUEST_RECORD).exists()
    supervision.forge._failure["kind"] = "missing"
    assert not supervision._forge_backoff()
    supervision.forge._failure.update(kind="rate", at=time.time() - 61)
    assert not supervision._forge_backoff()
