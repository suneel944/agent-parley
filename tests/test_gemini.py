"""Checks native settings preservation and the Gemini hook wire contract."""

import json
import os
import shlex
import subprocess
from pathlib import Path

import pytest

from agent_parley import gemini, roster, store
from agent_parley.state import BridgeError, write_json

STUB = (
    "#!/usr/bin/env python3\n"
    "import json, os, sys\n"
    "with open(os.environ['CAPTURE'], 'w') as f:\n"
    " json.dump({'argv': sys.argv[1:],\n"
    "  'settings': os.environ['GEMINI_CLI_SYSTEM_SETTINGS_PATH']}, f)\n"
)


@pytest.fixture
def lane(bridge, repo, monkeypatch, tmp_path):
    """Launches a Gemini lane and reports its configured hook commands."""
    binary = tmp_path / "bin"
    binary.mkdir()
    executable = binary / "gemini"
    executable.write_text(STUB)
    executable.chmod(0o755)
    native = tmp_path / "system.json"
    write_json(
        native,
        {
            "security": {"auth": {"selectedType": "oauth-personal"}},
            "hooks": {
                "BeforeTool": [{"hooks": [{"command": "native-policy"}]}]
            },
        },
    )
    monkeypatch.setenv("GEMINI_CLI_SYSTEM_SETTINGS_PATH", str(native))
    monkeypatch.setenv("PATH", str(binary) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("CAPTURE", str(tmp_path / "capture.json"))
    monkeypatch.setattr(bridge, "up", lambda: None)
    store.initialize(bridge.home)
    assert bridge.launch("helper", repo, "Coordinate", "gemini") == 0
    capture = json.loads((tmp_path / "capture.json").read_text())
    settings = json.loads(Path(capture["settings"]).read_text())
    data = roster.read(bridge.project(repo)[1])
    return {
        "native": native,
        "overlay": Path(capture["settings"]),
        "settings": settings,
        "commands": {
            event: entries[-1]["hooks"][0]["command"]
            for event, entries in settings["hooks"].items()
        },
        "lane": Path(data["participants"]["helper"]["lane"]),
        "directory": bridge.project(repo)[1],
    }


def fire(lane, event, payload):
    """Runs one configured hook command exactly as Gemini would run it."""
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


def test_every_native_event_runs_the_configured_hook_command(lane, bridge):
    assert set(lane["commands"]) == set(gemini.EVENTS)
    assert all(
        command.endswith("--adapter gemini")
        for command in lane["commands"].values()
    )
    assert lane["overlay"].is_relative_to(bridge.home)
    assert lane["settings"]["hooks"]["BeforeTool"][0] == {
        "hooks": [{"command": "native-policy"}]
    }
    assert roster.unavailable_hooks("gemini") == ["PermissionRequest"]


def test_a_native_branch_switch_is_refused_in_gemini_schema(lane):
    output = fire(
        lane,
        "BeforeTool",
        {
            "session_id": "session-1",
            "cwd": str(lane["lane"]),
            "tool_name": "run_shell_command",
            "tool_input": {"command": "git switch main"},
        },
    )
    assert output["decision"] == "deny"
    assert "owns this worktree" in output["reason"]


def test_a_native_session_start_records_activity_and_session(lane):
    output = fire(
        lane,
        "SessionStart",
        {"session_id": "session-7", "cwd": str(lane["lane"])},
    )
    assert (
        "Participants: helper"
        in (output["hookSpecificOutput"]["additionalContext"])
    )
    state = json.loads((lane["directory"] / "helper-activity.json").read_text())
    assert state["session_id"] == "session-7"
    assert state["activity"] == "working"


def test_a_native_after_agent_without_coordination_ends_the_turn(lane):
    fire(
        lane,
        "SessionStart",
        {"session_id": "session-9", "cwd": str(lane["lane"])},
    )
    assert (
        fire(
            lane,
            "AfterAgent",
            {"session_id": "session-9", "cwd": str(lane["lane"])},
        )
        == {}
    )


def test_resume_without_a_recorded_session_is_refused(lane, bridge, repo):
    with pytest.raises(BridgeError, match="No usable native session"):
        bridge.launch("helper", repo, "Continue", "gemini", resume=True)


def test_a_relaunch_rebuilds_the_overlay_from_native_policy(lane, bridge, repo):
    lane["overlay"].write_text("{corrupt")
    assert bridge.launch("helper", repo, "Coordinate", "gemini") == 0
    settings = json.loads(lane["overlay"].read_text())
    assert len(settings["hooks"]["BeforeTool"]) == 2
    assert json.loads(lane["native"].read_text())["hooks"] == {
        "BeforeTool": [{"hooks": [{"command": "native-policy"}]}]
    }
    bridge.retire(repo, "helper")
    assert not lane["overlay"].exists()


def test_system_policy_and_existing_hooks_survive_overlay(
    bridge, tmp_path, monkeypatch
):
    native = tmp_path / "system.json"
    original = {
        "security": {"auth": {"selectedType": "oauth-personal"}},
        "tools": {"exclude": ["run_shell_command"]},
        "hooks": {"BeforeTool": [{"hooks": [{"command": "native-policy"}]}]},
    }
    write_json(native, original)
    monkeypatch.setenv("GEMINI_CLI_SYSTEM_SETTINGS_PATH", str(native))
    result = gemini.configure(
        tmp_path,
        "lane",
        "http://127.0.0.1:42/mcp/",
        bridge.hooks("lane", tmp_path),
    )
    overlay = json.loads(result.read_text())
    assert overlay["security"] == original["security"]
    assert overlay["tools"] == original["tools"]
    assert (
        overlay["hooks"]["BeforeTool"][0] == original["hooks"]["BeforeTool"][0]
    )
    assert json.loads(native.read_text()) == original
    assert "${AGENT_PARLEY_TOKEN}" in result.read_text()


def test_disabled_native_hooks_refuse_launch(bridge, tmp_path, monkeypatch):
    native = tmp_path / "system.json"
    write_json(native, {"hooksConfig": {"enabled": False}})
    monkeypatch.setenv("GEMINI_CLI_SYSTEM_SETTINGS_PATH", str(native))
    with pytest.raises(BridgeError, match="disables hooks"):
        gemini.configure(
            tmp_path,
            "lane",
            "http://127.0.0.1/mcp/",
            bridge.hooks("lane", tmp_path),
        )


def test_native_before_tool_can_receive_denial_and_recover_via_mcp():
    normalized = gemini.payload(
        {
            "hook_event_name": "BeforeTool",
            "tool_name": "mcp_agent_parley_fetch_inbox",
        }
    )
    assert normalized["hook_event_name"] == "PreToolUse"
    assert normalized["tool_name"] == "mcp__agent_parley__fetch_inbox"
    result = gemini.response(
        {
            "hookSpecificOutput": {
                "permissionDecision": "deny",
                "permissionDecisionReason": "Review mail",
                "additionalContext": "Message 7",
            }
        }
    )
    assert result == {
        "decision": "deny",
        "reason": "Review mail",
        "hookSpecificOutput": {"additionalContext": "Message 7"},
    }
    assert gemini.response(
        {"decision": "block", "reason": "Restore branch"}
    ) == {"decision": "deny", "reason": "Restore branch"}
