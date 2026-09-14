"""Checks the Copilot hook wire contract through the configured command."""

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from agent_parley import copilot, roster, store
from agent_parley.cli import COPILOT_EVENTS, main

STUB = (
    "#!/usr/bin/env python3\n"
    "import json, os, sys\n"
    "with open(os.environ['CAPTURE'], 'w') as f:\n"
    " json.dump({'argv': sys.argv[1:]}, f)\n"
)


@pytest.fixture
def lane(bridge, repo, monkeypatch, tmp_path):
    """Launches a Copilot lane and reports its configured hook commands."""
    binary = tmp_path / "bin"
    binary.mkdir()
    executable = binary / "copilot"
    executable.write_text(STUB)
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(binary) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("CAPTURE", str(tmp_path / "capture.json"))
    monkeypatch.setattr(bridge, "up", lambda: None)
    store.initialize(bridge.home)
    account = tmp_path / "copilot-account"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agent-parley",
            "--home",
            str(bridge.home),
            "credentials",
            "add",
            "work",
            "--config-home",
            str(account),
        ],
    )
    assert main() == 0
    assert bridge.launch("helper", repo, "Coordinate", "copilot", "work") == 0
    data = roster.read(bridge.project(repo)[1])
    settings = json.loads((account / "settings.json").read_text())
    return {
        "commands": {
            event: entries[0]["bash"]
            for event, entries in settings["hooks"].items()
        },
        "lane": Path(data["participants"]["helper"]["lane"]),
        "directory": bridge.project(repo)[1],
    }


def fire(lane, event, payload):
    """Runs one configured hook command exactly as Copilot would run it."""
    result = subprocess.run(
        shlex.split(lane["commands"][event]),
        input=json.dumps({"hook_event_name": event, **payload}),
        capture_output=True,
        text=True,
        check=False,
        cwd=lane["lane"],
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_every_supported_event_is_registered_in_compatible_form(lane):
    assert set(lane["commands"]) == set(COPILOT_EVENTS)
    assert all(
        command.endswith("--adapter copilot")
        for command in lane["commands"].values()
    )


def test_a_native_branch_switch_is_refused_in_copilot_schema(lane):
    output = fire(
        lane,
        "PreToolUse",
        {
            "session_id": "session-1",
            "cwd": str(lane["lane"]),
            "tool_name": "shell",
            "tool_input": {"command": "git switch main"},
        },
    )
    assert output["permissionDecision"] == "deny"
    assert "owns this worktree" in output["permissionDecisionReason"]
    assert "hookSpecificOutput" not in output


def test_a_native_session_start_records_activity_and_session(lane):
    output = fire(
        lane,
        "SessionStart",
        {"session_id": "session-7", "cwd": str(lane["lane"])},
    )
    assert "Participants: helper" in output["additionalContext"]
    assert "hookSpecificOutput" not in output
    state = json.loads((lane["directory"] / "helper-activity.json").read_text())
    assert state["session_id"] == "session-7"
    assert state["event"] == "SessionStart"
    assert state["activity"] == "working"


def test_a_native_stop_without_coordination_ends_the_turn(lane):
    fire(
        lane,
        "SessionStart",
        {"session_id": "session-9", "cwd": str(lane["lane"])},
    )
    assert (
        fire(
            lane,
            "Stop",
            {"session_id": "session-9", "cwd": str(lane["lane"])},
        )
        == {}
    )


def test_an_approval_request_never_carries_a_permission_decision(lane):
    output = fire(
        lane,
        "PermissionRequest",
        {
            "session_id": "session-3",
            "cwd": str(lane["lane"]),
            "tool_name": "shell",
            "tool_input": {"command": "rm -rf /"},
        },
    )
    assert "permissionDecision" not in output
    assert "behavior" not in output


def test_a_qualified_coordination_tool_is_recognized_after_renaming():
    assert (
        copilot.payload({"tool_name": "mcp-agent-parley-fetch_inbox"})[
            "tool_name"
        ]
        == "mcp__agent_parley__fetch_inbox"
    )
    assert copilot.payload({"tool_name": "shell"})["tool_name"] == "shell"


def test_shared_output_is_flattened_into_copilot_fields():
    assert copilot.response(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "Review mail",
                "additionalContext": "Message 7",
            }
        }
    ) == {
        "permissionDecision": "deny",
        "permissionDecisionReason": "Review mail",
        "additionalContext": "Message 7",
    }
    assert copilot.response(
        {"decision": "block", "reason": "Restore branch"}
    ) == {"decision": "block", "reason": "Restore branch"}
    assert copilot.response({}) == {}
