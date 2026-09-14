"""Checks that a coordination outage stays answerable after recovery."""

import json
import sqlite3
from pathlib import Path

from agent_parley import checkpoints
from agent_parley.state import write_json


def entries(directory, agent="claude"):
    path = directory / f"{agent}-events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_a_recorded_decision_carries_its_cause(tmp_path):
    checkpoints.record(
        tmp_path,
        "claude",
        {"hook_event_name": "PreToolUse", "tool_name": "Bash"},
        checkpoints.Reason.COORDINATION_UNAVAILABLE,
        None,
        "idle",
        "no such column: m.ack_deadline_ts",
    )
    recorded = entries(tmp_path)[0]
    assert recorded["cause"] == "no such column: m.ack_deadline_ts"
    assert recorded["reason_class"] == "coordination_unavailable"


def test_an_ordinary_decision_records_no_cause(tmp_path):
    checkpoints.record(
        tmp_path,
        "claude",
        {"hook_event_name": "Stop"},
        checkpoints.Reason.OBSERVED,
        None,
    )
    assert entries(tmp_path)[0]["cause"] == ""


def test_a_long_cause_is_bounded(tmp_path):
    checkpoints.record(
        tmp_path,
        "claude",
        {"hook_event_name": "Stop"},
        checkpoints.Reason.COORDINATION_UNAVAILABLE,
        None,
        "",
        "x" * (checkpoints.MAX_CAUSE_BYTES * 3),
    )
    assert len(entries(tmp_path)[0]["cause"]) == checkpoints.MAX_CAUSE_BYTES


def test_the_cause_survives_the_recovery_that_clears_live_state(tmp_path):
    failure = "no such column: m.ack_deadline_ts"
    checkpoints.record(
        tmp_path,
        "claude",
        {"hook_event_name": "PreToolUse"},
        checkpoints.Reason.COORDINATION_UNAVAILABLE,
        None,
        "",
        failure,
    )
    checkpoints.record(
        tmp_path,
        "claude",
        {"hook_event_name": "PreToolUse"},
        checkpoints.Reason.OBSERVED,
        None,
    )
    summary = checkpoints.event_summary(tmp_path, "claude")
    assert summary["events"] == 2
    assert summary["last_reason"] == "observed"
    assert summary["last_cause"] == failure


def test_a_lane_that_never_failed_reports_no_cause(tmp_path):
    checkpoints.record(
        tmp_path,
        "claude",
        {"hook_event_name": "Stop"},
        checkpoints.Reason.OBSERVED,
        None,
    )
    assert checkpoints.event_summary(tmp_path, "claude")["last_cause"] == ""


def test_a_store_failure_reaches_the_event_log(
    bridge, repo, paired, monkeypatch
):
    lane = Path(paired["lanes"]["codex"])
    write_json(lane.parent / "codex-identity.json", {"name": "codex"})

    def unreadable(*args, **kwargs):
        raise sqlite3.OperationalError("no such column: m.ack_deadline_ts")

    monkeypatch.setattr(checkpoints, "mailbox", unreadable)
    output = checkpoints.checkpoint(
        bridge.home,
        lane.parent,
        "codex",
        {
            "hook_event_name": "PreToolUse",
            "cwd": str(lane),
            "tool_name": "Read",
            "tool_input": {},
        },
    )
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
    recorded = entries(lane.parent, "codex")[-1]
    assert recorded["reason_class"] == "coordination_unavailable"
    assert "ack_deadline_ts" in recorded["cause"]
