"""Checks native settings preservation and the Gemini hook wire contract."""

import json
import os
import shlex
import subprocess
from pathlib import Path

import pytest

from agent_parley import dialogs, gemini, hook, roster, store
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


@pytest.mark.parametrize(
    ("command", "denied"),
    [
        ("git worktree add {wt}/fix -b fix/foo", False),
        ("git -C .. worktree add {name}-wt/up", False),
        ("git worktree add /tmp/x -b fix/foo", True),
        ("git worktree add -b fix/foo ../beside", True),
        ("git -C .. worktree add beside", True),
        ("git worktree add ~/stray-wt", True),
    ],
)
def test_a_worktree_add_outside_the_lane_wt_is_refused(lane, command, denied):
    path = Path(lane["lane"]).resolve()
    home = Path("~/stray-wt").expanduser().resolve()
    text = command.format(wt=f"{path}-wt", name=path.name)
    output = fire(
        lane,
        "BeforeTool",
        {
            "session_id": "session-wt",
            "cwd": str(path),
            "tool_name": "run_shell_command",
            "tool_input": {"command": text},
        },
    )
    assert (output.get("decision") == "deny") is denied
    if denied:
        assert f"inside {path}-wt/" in output["reason"]
    if "~" in command:
        assert f"{home} is outside it" in output["reason"]


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


def test_a_tool_permission_notification_records_the_lane_blocked(lane):
    fire(
        lane,
        "SessionStart",
        {"session_id": "session-3", "cwd": str(lane["lane"])},
    )
    output = fire(
        lane,
        "Notification",
        {
            "session_id": "session-3",
            "cwd": str(lane["lane"]),
            "notification_type": "ToolPermission",
            "message": "Allow execution?",
            "details": {"toolName": "run_shell_command", "command": "ls"},
        },
    )
    assert output == {}
    state = json.loads((lane["directory"] / "helper-activity.json").read_text())
    assert state["activity"] == f"{dialogs.APPROVAL}: run_shell_command"
    assert state["dialog"]["tool"] == "run_shell_command"


def test_other_notifications_are_ignored_and_never_decide(lane):
    fire(
        lane,
        "SessionStart",
        {"session_id": "session-4", "cwd": str(lane["lane"])},
    )
    output = fire(
        lane,
        "Notification",
        {
            "session_id": "session-4",
            "cwd": str(lane["lane"]),
            "notification_type": "Idle",
        },
    )
    assert output == {}
    state = json.loads((lane["directory"] / "helper-activity.json").read_text())
    assert state["activity"] == "working"
    assert "dialog" not in state


def test_a_tool_permission_payload_names_the_shared_tool():
    assert gemini.payload(
        {
            "hook_event_name": "Notification",
            "notification_type": "ToolPermission",
            "details": {"toolName": "mcp_agent_parley_send_message"},
        }
    ) == {
        "hook_event_name": "PermissionRequest",
        "notification_type": "ToolPermission",
        "details": {"toolName": "mcp_agent_parley_send_message"},
        "tool_name": "mcp__agent_parley__send_message",
        "tool_input": {"toolName": "mcp_agent_parley_send_message"},
    }
    assert gemini.payload(
        {"hook_event_name": "Notification", "notification_type": "Idle"}
    ) == {"hook_event_name": "Notification", "notification_type": "Idle"}
    assert gemini.response({}) == {}


@pytest.mark.parametrize(
    "value",
    [
        {"hook_event_name": "BeforeTool", "tool_name": 5},
        {"hook_event_name": ["BeforeTool"], "tool_name": "run_shell_command"},
    ],
)
def test_a_wrongly_typed_field_passes_through_untranslated(value):
    translated = gemini.payload(value)
    assert translated["tool_name"] == value["tool_name"]
    assert type(translated["hook_event_name"]) is type(value["hook_event_name"])


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
    result = gemini.configure(
        tmp_path,
        "lane",
        "http://127.0.0.1:42/mcp/",
        bridge.hooks("lane", tmp_path),
        str(native),
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
    with pytest.raises(BridgeError, match="disables hooks"):
        gemini.configure(
            tmp_path,
            "lane",
            "http://127.0.0.1/mcp/",
            bridge.hooks("lane", tmp_path),
            str(native),
        )


def test_relaunch_inside_a_lane_copies_native_policy_not_the_overlay(
    lane, bridge, repo, monkeypatch, tmp_path
):
    monkeypatch.setenv("GEMINI_CLI_SYSTEM_SETTINGS_PATH", str(lane["overlay"]))
    assert bridge.launch("helper", repo, "Continue", "gemini") == 0
    capture = json.loads((tmp_path / "capture.json").read_text())
    settings = json.loads(Path(capture["settings"]).read_text())
    lane_hooks = [
        entry
        for entry in settings["hooks"]["BeforeTool"]
        if entry["hooks"][0]["command"].endswith("--adapter gemini")
    ]
    assert len(lane_hooks) == 1


def test_inherited_environment_drops_lane_values_only(
    bridge, monkeypatch, tmp_path
):
    native = tmp_path / "system.json"
    overlay = bridge.home / "projects" / "key" / "helper-gemini-settings.json"
    monkeypatch.setenv("GEMINI_CLI_SYSTEM_SETTINGS_PATH", str(native))
    monkeypatch.setenv("AMP_SETTINGS_FILE", str(overlay))
    monkeypatch.setenv("AGENT_PARLEY_TOKEN", "secret")
    monkeypatch.setenv(hook.PARTICIPANT_ENV, "helper")
    monkeypatch.setenv(hook.HOOK_PID_ENV, "1")
    monkeypatch.setenv(hook.ANSWERED_ENV, "409")
    environment = hook.inherited_environment(bridge.home)
    assert environment["GEMINI_CLI_SYSTEM_SETTINGS_PATH"] == str(native)
    assert not {
        "AMP_SETTINGS_FILE",
        "AGENT_PARLEY_TOKEN",
        hook.PARTICIPANT_ENV,
        hook.HOOK_PID_ENV,
        hook.ANSWERED_ENV,
    } & set(environment)


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


def test_an_opted_in_gemini_lane_trusts_only_the_bridge_server(
    lane, bridge, repo, capsys
):
    assert "trust" not in lane["settings"]["mcpServers"]["agent_parley"]
    capsys.readouterr()
    assert bridge.launch("helper", repo, "Coordinate", "gemini") == 0
    assert "helper is a gemini lane" in capsys.readouterr().err
    path = lane["directory"] / "project.json"
    manifest = json.loads(path.read_text())
    manifest.setdefault("supervision", {})[dialogs.PRE_APPROVE] = True
    write_json(path, manifest)
    lane["native"].write_text(
        json.dumps({"mcpServers": {"docs": {"command": "docs-mcp"}}})
    )
    assert bridge.launch("helper", repo, "Coordinate", "gemini") == 0
    assert "is a gemini lane" not in capsys.readouterr().err
    servers = json.loads(lane["overlay"].read_text())["mcpServers"]
    assert servers["agent_parley"]["trust"] is True
    assert servers["docs"] == {"command": "docs-mcp"}
