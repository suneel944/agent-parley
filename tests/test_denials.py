"""Checks that a native permission denial becomes one operator decision."""

import json
from pathlib import Path

import pytest

from agent_parley import (
    checkpoints,
    decisions,
    denials,
    notify,
    supervision,
    timeouts,
)
from agent_parley.state import write_json

COMMAND = "git push --force origin main"
REFUSAL = (
    f"Permission to use Bash with command {COMMAND} has been denied by the "
    "Claude Code auto mode classifier. Reason: [CI Bypass]"
)


def denied(lane: Path, command: str = COMMAND, **extra: object) -> dict:
    """Builds the event Claude Code raises after its classifier denies."""
    return {
        "hook_event_name": "PermissionDenied",
        "cwd": str(lane),
        "session_id": "s1",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "tool_use_id": "t1",
        "reason": REFUSAL,
        **extra,
    }


@pytest.fixture
def quiet(monkeypatch):
    """Turns notifications on while keeping every send in this process."""
    monkeypatch.setenv("AGENT_PARLEY_NOTIFY", "fake")
    monkeypatch.setitem(notify.TRANSPORTS, "fake", lambda *args: None)
    monkeypatch.setattr(notify, "_SENDING", set())
    monkeypatch.setattr(notify, "_THREADS", [])
    monkeypatch.setattr(notify.threading.Thread, "start", lambda self: None)


@pytest.fixture
def claimed(paired):
    """Names the codex lane, recorded as holding issue 42."""
    lane = Path(paired["lanes"]["codex"])
    write_json(
        lane.parent / "issues.json",
        {"revision": 1, "issues": {"42": {"owner": "codex", "claim_id": "c"}}},
    )
    return lane


def test_the_classifier_event_names_the_command_and_the_reason(tmp_path):
    found = denials.detect(denied(tmp_path))

    assert found == denials.Denial("claude", "Bash", COMMAND, "CI Bypass")
    bare = denied(tmp_path, reason="[Interfere With Workloads]")
    assert denials.detect(bare).reason == "Interfere With Workloads"


def test_a_tool_result_quoting_the_refusal_is_not_a_denial(tmp_path):
    quoted = denied(tmp_path, hook_event_name="PostToolUse")
    quoted["tool_response"] = REFUSAL

    assert denials.detect(quoted) is None


@pytest.mark.parametrize(
    ("tool", "command", "rule"),
    [
        ("Bash", COMMAND, "Bash(git push:*)"),
        ("Bash", "pytest -q tests", "Bash(pytest:*)"),
        ("Bash", "", "Bash"),
        ("WebFetch", "https://example.com", "WebFetch"),
    ],
)
def test_the_suggested_rule_matches_the_program_prefix(tool, command, rule):
    assert denials.rule(tool, command) == rule


def test_a_denied_call_opens_exactly_one_decision(bridge, claimed, quiet):
    directory = claimed.parent

    first = checkpoints.checkpoint(
        bridge.home, directory, "codex", denied(claimed)
    )
    second = checkpoints.checkpoint(
        bridge.home, directory, "codex", denied(claimed)
    )

    assert first == second == {}
    records = json.loads((directory / decisions.RECORD_NAME).read_text())
    assert len(records) == 1
    record = next(iter(records.values()))
    assert record["kind"] == denials.KIND
    assert record["lane"] == "codex"
    assert record["issue"] == "42"
    assert record["reversibility"] == decisions.IRREVERSIBLE
    assert record["options"] == [
        denials.RUN,
        denials.RULE,
        denials.REASSIGN,
        denials.RELEASE,
    ]
    assert f"command: {COMMAND}" in record["detail"]
    assert "reason: CI Bypass" in record["detail"]
    assert "Bash(git push:*)" in record["detail"]
    logged = (directory / "codex-events.jsonl").read_text().splitlines()
    reasons = [json.loads(line)["reason_class"] for line in logged[-2:]]
    assert reasons == [checkpoints.Reason.PERMISSION_DENIED] * 2

    checkpoints.checkpoint(
        bridge.home, directory, "codex", denied(claimed, "rm -rf build")
    )
    records = json.loads((directory / decisions.RECORD_NAME).read_text())
    assert len(records) == 2


def test_no_decision_is_recorded_without_notifications(
    bridge, claimed, monkeypatch
):
    monkeypatch.delenv("AGENT_PARLEY_NOTIFY", raising=False)
    directory = claimed.parent

    checkpoints.checkpoint(bridge.home, directory, "codex", denied(claimed))

    assert not (directory / decisions.RECORD_NAME).exists()


def test_a_denial_is_never_settled_by_a_timeout(tmp_path):
    found = denials.detect(denied(tmp_path))
    opened = denials.record(tmp_path, "/repo", "codex", found, "42", now=1.0)
    later = 1.0 + timeouts.TIMEOUT_SECONDS * 10

    assert timeouts.sweep(tmp_path, tmp_path, {}, later) == []
    assert decisions.get(tmp_path, opened["id"])["state"] == decisions.OPEN
    assert timeouts.EVENTS[notify.Event.PERMISSION_DENIED] == (
        "native_permission"
    )
    assert (
        timeouts.kind_of("native_permission").reversibility
        == timeouts.IRREVERSIBLE
    )


def test_an_open_denial_defers_the_lane_wake_until_answered(tmp_path):
    found = denials.detect(denied(tmp_path))
    opened = denials.record(tmp_path, "/repo", "codex", found)
    observed = {"record": {}, "process_alive": True}

    blocked, _ = supervision._wake_block(tmp_path, "codex", observed, {}, 60)

    assert blocked == f"blocked: {denials.CAUSE} ({opened['id']})"
    assert opened["options"] == [denials.RUN, denials.RULE]
    decisions.answer(tmp_path, opened["id"], denials.RUN, "cli")
    assert supervision._wake_block(tmp_path, "codex", observed, {}, 60) == (
        "",
        0.0,
    )
