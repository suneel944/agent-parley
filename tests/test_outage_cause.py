"""Checks that a coordination outage stays answerable after recovery."""

import json
import sqlite3
from pathlib import Path

import pytest

from agent_parley import checkpoints, protocol, store
from agent_parley.state import write_json

FAILURE = "no such column: m.ack_deadline_ts"


def entries(directory, agent="claude"):
    path = directory / f"{agent}-events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def during_outage(bridge, paired, monkeypatch, payload):
    lane = Path(paired["lanes"]["codex"])
    write_json(lane.parent / "codex-identity.json", {"name": "codex"})

    def unreadable(*args, **kwargs):
        raise sqlite3.OperationalError(FAILURE)

    monkeypatch.setattr(checkpoints, "mailbox", unreadable)
    output = checkpoints.checkpoint(
        bridge.home,
        lane.parent,
        "codex",
        {
            "hook_event_name": "PreToolUse",
            "cwd": str(lane),
            "tool_input": {},
            **payload,
        },
    )
    return output, lane.parent


def details(output):
    return output.get("hookSpecificOutput") or {}


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
    output, directory = during_outage(
        bridge, paired, monkeypatch, {"tool_name": "Edit"}
    )
    assert details(output)["permissionDecision"] == "deny"
    recorded = entries(directory, "codex")[-1]
    assert recorded["reason_class"] == "coordination_unavailable"
    assert "ack_deadline_ts" in recorded["cause"]


def test_a_denial_names_the_failure_that_caused_it(
    bridge, repo, paired, monkeypatch
):
    output, _ = during_outage(
        bridge, paired, monkeypatch, {"tool_name": "Edit"}
    )
    assert FAILURE in details(output)["permissionDecisionReason"]


def test_a_read_still_runs_during_an_outage(bridge, repo, paired, monkeypatch):
    output, directory = during_outage(
        bridge, paired, monkeypatch, {"tool_name": "Read"}
    )
    assert "permissionDecision" not in details(output)
    assert FAILURE in details(output)["additionalContext"]
    assert entries(directory, "codex")[-1]["cause"] == FAILURE


def test_the_prescribed_status_check_still_runs(
    bridge, repo, paired, monkeypatch
):
    output, _ = during_outage(
        bridge,
        paired,
        monkeypatch,
        {
            "tool_name": "Bash",
            "tool_input": {"command": "agent-parley status"},
        },
    )
    assert "permissionDecision" not in details(output)


def test_an_edit_chained_onto_a_bridge_command_is_still_denied(
    bridge, repo, paired, monkeypatch
):
    output, _ = during_outage(
        bridge,
        paired,
        monkeypatch,
        {
            "tool_name": "Bash",
            "tool_input": {
                "command": "agent-parley status && git commit -m done"
            },
        },
    )
    assert details(output)["permissionDecision"] == "deny"


def test_a_store_behind_the_code_prescribes_its_migration(
    bridge, repo, paired, monkeypatch
):
    with store.connect(bridge.home, write=True) as db:
        db.execute(f"PRAGMA user_version={store.SCHEMA_VERSION - 1}")
    output, _ = during_outage(
        bridge, paired, monkeypatch, {"tool_name": "Edit"}
    )
    assert protocol.MIGRATE in details(output)["permissionDecisionReason"]


def test_an_ordinary_outage_prescribes_the_status_check(
    bridge, repo, paired, monkeypatch
):
    output, _ = during_outage(
        bridge, paired, monkeypatch, {"tool_name": "Edit"}
    )
    reason = details(output)["permissionDecisionReason"]
    assert checkpoints.OUTAGE_CHECK in reason
    assert protocol.MIGRATE not in reason


@pytest.mark.parametrize(
    "command",
    [
        "agent-parley status > agent_parley/cli.py",
        "agent-parley status >agent_parley/cli.py",
        "agent-parley status >> notes.txt",
        "agent-parley status &> notes.txt",
        "agent-parley status < notes.txt",
        "agent-parley status <<< text",
        "agent-parley status $(git commit -m done)",
        "agent-parley status `git commit -m done`",
        "agent-parley status <(git log)",
        "agent-parley status\ngit commit -m done",
    ],
)
def test_a_redirected_or_substituted_command_is_denied(
    bridge, repo, paired, monkeypatch, command
):
    output, _ = during_outage(
        bridge,
        paired,
        monkeypatch,
        {"tool_name": "Bash", "tool_input": {"command": command}},
    )
    assert details(output)["permissionDecision"] == "deny"


def test_the_documented_repair_is_still_cleared(
    bridge, repo, paired, monkeypatch
):
    output, _ = during_outage(
        bridge,
        paired,
        monkeypatch,
        {
            "tool_name": "Bash",
            "tool_input": {"command": "agent-parley down && agent-parley up"},
        },
    )
    assert "permissionDecision" not in details(output)


def test_a_store_behind_the_code_is_not_reported_ready(
    bridge, repo, paired, monkeypatch
):
    monkeypatch.setattr(type(bridge), "server_process", lambda self: True)
    monkeypatch.setattr(type(bridge), "ready", lambda self: True)
    assert bridge.status_snapshot()["server"]["ready"] is True
    with store.connect(bridge.home, write=True) as db:
        db.execute(f"PRAGMA user_version={store.SCHEMA_VERSION - 1}")
    assert bridge.status_snapshot()["server"]["ready"] is False
    monkeypatch.undo()


def test_status_names_the_repair_for_an_unusable_store(
    bridge, repo, paired, capsys
):
    with store.connect(bridge.home, write=True) as db:
        db.execute(f"PRAGMA user_version={store.SCHEMA_VERSION - 1}")
    bridge.status()
    printed = capsys.readouterr().out
    assert "Server: not ready" in printed
    assert protocol.MIGRATE in printed


def test_a_store_state_maps_to_one_remedy():
    assert store.remedy(store.SCHEMA_BEHIND) == protocol.MIGRATE
    assert store.remedy(store.SCHEMA_UNSUPPORTED) == protocol.UPGRADE
    assert store.remedy(store.SCHEMA_CURRENT) == ""
    assert store.remedy(store.SCHEMA_ABSENT) == ""
