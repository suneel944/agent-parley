"""Checks the OpenCode overlay and plugin wire contract through the launcher."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_parley import cli, opencode, roster, store
from agent_parley.state import BridgeError, write_json

STUB = (
    "#!/usr/bin/env python3\n"
    "import json, os, sys\n"
    "with open(os.environ['CAPTURE'], 'w') as f:\n"
    " json.dump({'argv': sys.argv[1:],\n"
    "  'config_dir': os.environ['OPENCODE_CONFIG_DIR']}, f)\n"
)


@pytest.fixture
def account(tmp_path):
    """Prepares a user configuration directory the overlay must preserve."""
    home = tmp_path / "opencode-account"
    home.mkdir()
    write_json(
        home / "opencode.json",
        {
            "$schema": "https://opencode.ai/config.json",
            "model": "anthropic/claude-sonnet-4-5",
            "mcp": {"docs": {"type": "local", "command": ["docs-mcp"]}},
            "plugin": ["opencode-notify"],
        },
    )
    return home


@pytest.fixture
def lane(bridge, repo, monkeypatch, tmp_path, account):
    """Launches an OpenCode lane and reports the plugin's hook command."""
    binary = tmp_path / "bin"
    binary.mkdir()
    executable = binary / "opencode"
    executable.write_text(STUB)
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(binary) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("CAPTURE", str(tmp_path / "capture.json"))
    monkeypatch.setattr(bridge, "up", lambda: None)
    monkeypatch.setattr(
        cli.terminal,
        "run",
        lambda command, lane, env, agent, attached: subprocess.call(
            command, cwd=lane, env=env
        ),
    )
    store.initialize(bridge.home)
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
    assert cli.main() == 0
    assert bridge.launch("helper", repo, "Coordinate", "opencode", "work") == 0
    capture = json.loads((tmp_path / "capture.json").read_text())
    overlay = Path(capture["config_dir"])
    plugin = (overlay / "plugin" / "agent-parley.js").read_text()
    command = json.loads(
        plugin.split("const COMMAND = ", 1)[1].split(";\n", 1)[0]
    )
    data = roster.read(bridge.project(repo)[1])
    return {
        "argv": capture["argv"],
        "overlay": overlay,
        "plugin": plugin,
        "command": command,
        "lane": Path(data["participants"]["helper"]["lane"]),
        "directory": bridge.project(repo)[1],
        "capture": tmp_path / "capture.json",
    }


def fire(lane, event, payload):
    """Runs the plugin's hook command exactly as the plugin spawns it."""
    result = subprocess.run(
        lane["command"],
        input=json.dumps({"hook_event_name": event, **payload}),
        capture_output=True,
        text=True,
        check=False,
        cwd=lane["lane"],
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_user_configuration_is_carried_into_a_private_overlay(
    lane, bridge, account
):
    assert lane["argv"][0] == "--prompt"
    assert lane["overlay"].is_relative_to(bridge.home)
    settings = json.loads((lane["overlay"] / "opencode.json").read_text())
    assert settings["model"] == "anthropic/claude-sonnet-4-5"
    assert settings["mcp"]["docs"] == {
        "type": "local",
        "command": ["docs-mcp"],
    }
    assert settings["plugin"] == ["opencode-notify"]
    server = settings["mcp"]["agent_parley"]
    assert server["type"] == "remote"
    assert server["url"].endswith("/mcp/")
    assert server["headers"]["Authorization"] == (
        "Bearer {env:AGENT_PARLEY_TOKEN}"
    )
    assert "agent_parley" not in (account / "opencode.json").read_text()
    assert not (account / "plugin").exists()


def test_every_supported_event_runs_the_configured_hook_command(lane):
    assert lane["command"][-2:] == ["--adapter", "opencode"]
    assert cli.bridge_hook(" ".join(lane["command"]))
    for event in opencode.EVENTS:
        assert f'"{event}"' in lane["plugin"]
    assert roster.unavailable_hooks("opencode") == ["SessionEnd"]


def test_a_native_branch_switch_is_refused_in_plugin_schema(lane):
    output = fire(
        lane,
        "tool.execute.before",
        {
            "session_id": "ses_1",
            "cwd": str(lane["lane"]),
            "tool_name": "bash",
            "tool_input": {"command": "git switch main"},
        },
    )
    assert output["decision"] == "deny"
    assert "owns this worktree" in output["reason"]
    assert "hookSpecificOutput" not in output


def test_a_native_session_start_records_activity_and_session(lane):
    output = fire(
        lane,
        "session.created",
        {"session_id": "ses_7", "cwd": str(lane["lane"])},
    )
    assert "Participants: helper" in output["context"]
    state = json.loads((lane["directory"] / "helper-activity.json").read_text())
    assert state["session_id"] == "ses_7"
    assert state["event"] == "SessionStart"
    assert state["activity"] == "working"


def test_an_idle_session_without_coordination_ends_the_turn(lane):
    fire(
        lane,
        "session.created",
        {"session_id": "ses_9", "cwd": str(lane["lane"])},
    )
    assert (
        fire(
            lane,
            "session.idle",
            {"session_id": "ses_9", "cwd": str(lane["lane"])},
        )
        == {}
    )


def test_an_approval_request_never_carries_a_permission_decision(lane):
    output = fire(
        lane,
        "permission.ask",
        {
            "session_id": "ses_3",
            "cwd": str(lane["lane"]),
            "tool_name": "bash",
            "tool_input": {"command": "rm -rf /"},
        },
    )
    assert "decision" not in output


def test_resume_without_a_recorded_session_is_refused(lane, bridge, repo):
    with pytest.raises(BridgeError, match="No usable native session"):
        bridge.launch(
            "helper", repo, "Continue", "opencode", "work", resume=True
        )


def test_resume_selects_the_recorded_session_and_rebuilds_the_overlay(
    lane, bridge, repo
):
    fire(
        lane,
        "session.created",
        {"session_id": "ses_11", "cwd": str(lane["lane"])},
    )
    (lane["overlay"] / "plugin" / "agent-parley.js").unlink()
    assert (
        bridge.launch(
            "helper", repo, "Continue", "opencode", "work", resume=True
        )
        == 0
    )
    capture = json.loads(lane["capture"].read_text())
    assert capture["argv"][:2] == ["--session", "ses_11"]
    assert (lane["overlay"] / "plugin" / "agent-parley.js").exists()


def test_a_configuration_that_already_names_the_server_is_refused(
    bridge, tmp_path
):
    home = tmp_path / "taken"
    home.mkdir()
    write_json(home / "opencode.json", {"mcp": {"agent_parley": {}}})
    with pytest.raises(BridgeError, match="already defines agent_parley"):
        opencode.configure(
            tmp_path,
            "lane",
            "http://127.0.0.1/mcp/",
            bridge.hooks("lane", tmp_path),
            str(home),
        )
    write_json(home / "opencode.json", ["not", "an", "object"])
    with pytest.raises(BridgeError, match="must be an object"):
        opencode.configure(
            tmp_path,
            "lane",
            "http://127.0.0.1/mcp/",
            bridge.hooks("lane", tmp_path),
            str(home),
        )


def test_a_jsonc_only_configuration_is_refused_with_the_fix(bridge, tmp_path):
    home = tmp_path / "jsonc"
    home.mkdir()
    (home / "opencode.jsonc").write_text("{ // comment\n}\n")
    with pytest.raises(BridgeError, match="save it as opencode.json"):
        opencode.configure(
            tmp_path,
            "lane",
            "http://127.0.0.1/mcp/",
            bridge.hooks("lane", tmp_path),
            str(home),
        )


def test_plugin_events_and_tool_names_reach_the_shared_parser():
    normalized = opencode.payload(
        {
            "hook_event_name": "tool.execute.before",
            "tool_name": "agent_parley_fetch_inbox",
        }
    )
    assert normalized["hook_event_name"] == "PreToolUse"
    assert normalized["tool_name"] == "mcp__agent_parley__fetch_inbox"
    assert opencode.payload({"hook_event_name": "Stop"}) == {
        "hook_event_name": "Stop"
    }
    assert opencode.response(
        {
            "hookSpecificOutput": {
                "permissionDecision": "deny",
                "permissionDecisionReason": "Review mail",
                "additionalContext": "Message 7",
            }
        }
    ) == {"decision": "deny", "reason": "Review mail", "context": "Message 7"}
    assert opencode.response(
        {"decision": "block", "reason": "Restore branch"}
    ) == {"decision": "block", "reason": "Restore branch"}
    assert opencode.response({}) == {}


def test_provider_inspection_exposes_unavailable_hooks(bridge):
    inspected = roster.inspect(bridge.home)
    assert inspected["opencode"]["unavailable_hooks"] == ["SessionEnd"]
    assert inspected["gemini"]["unavailable_hooks"] == ["PermissionRequest"]
    assert inspected["copilot"]["unavailable_hooks"] == []
    assert inspected["claude"]["unavailable_hooks"] == []
    assert inspected["codex"]["unavailable_hooks"] == []
    assert roster.unavailable_hooks("unknown") == list(roster.HOOK_EVENTS)


def test_an_adapter_missing_a_required_guard_refuses_to_launch(
    bridge, repo, monkeypatch, tmp_path
):
    binary = tmp_path / "bin"
    binary.mkdir()
    executable = binary / "opencode"
    executable.write_text(STUB)
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(binary) + os.pathsep + os.environ["PATH"])
    monkeypatch.setitem(
        roster.UNAVAILABLE_HOOKS, "opencode", ("PreToolUse", "SessionEnd")
    )
    with pytest.raises(BridgeError, match="cannot deliver PreToolUse"):
        bridge.launch("helper", repo, "Coordinate", "opencode")
