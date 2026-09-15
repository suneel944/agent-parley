"""Checks the Amp settings overlay and hook contract through the launcher."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_parley import amp, cli, roster, store
from agent_parley.state import BridgeError, write_json

STUB = (
    "#!/usr/bin/env python3\n"
    "import json, os, sys\n"
    "with open(os.environ['CAPTURE'], 'w') as f:\n"
    " json.dump({'argv': sys.argv[1:],\n"
    "  'settings': os.environ['AMP_SETTINGS_FILE']}, f)\n"
)
AVAILABLE = ("PermissionRequest", "SessionEnd")


@pytest.fixture
def account(tmp_path):
    """Prepares a user settings file the overlay must preserve."""
    home = tmp_path / "amp-account"
    home.mkdir()
    write_json(
        home / "settings.json",
        {
            "amp.url": "https://ampcode.com/",
            "amp.mcpServers": {"docs": {"command": "docs-mcp", "args": []}},
            "amp.hooks": [
                {
                    "name": "notify",
                    "event": "tool:post-execute",
                    "command": "notify-send",
                }
            ],
        },
    )
    return home


def install(monkeypatch, tmp_path):
    """Places a stub amp executable first on PATH."""
    binary = tmp_path / "bin"
    binary.mkdir()
    executable = binary / "amp"
    executable.write_text(STUB)
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(binary) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("CAPTURE", str(tmp_path / "capture.json"))


@pytest.fixture
def lane(bridge, repo, monkeypatch, tmp_path, account):
    """Launches an Amp lane as if Amp raised every required guard.

    Amp raises no thread start or idle event, so the real launcher refuses
    it; the guard table is narrowed here to exercise the launch path that
    a future Amp release with those events would take.
    """
    install(monkeypatch, tmp_path)
    monkeypatch.setitem(roster.UNAVAILABLE_HOOKS, "amp", AVAILABLE)
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
    assert bridge.launch("helper", repo, "Coordinate", "amp", "work") == 0
    capture = json.loads((tmp_path / "capture.json").read_text())
    settings = json.loads(Path(capture["settings"]).read_text())
    hook = settings["amp.hooks"][-2]
    data = roster.read(bridge.project(repo)[1])
    return {
        "argv": capture["argv"],
        "path": Path(capture["settings"]),
        "settings": settings,
        "command": [hook["command"], *hook["args"]],
        "lane": Path(data["participants"]["helper"]["lane"]),
        "directory": bridge.project(repo)[1],
        "capture": tmp_path / "capture.json",
    }


def fire(lane, event, payload):
    """Runs the hook command exactly as the settings entry names it."""
    result = subprocess.run(
        lane["command"],
        input=json.dumps({"event": event, **payload}),
        capture_output=True,
        text=True,
        check=False,
        cwd=lane["lane"],
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_user_settings_are_carried_into_a_private_overlay(
    lane, bridge, account
):
    assert lane["argv"][:2] == ["--settings-file", str(lane["path"])]
    assert lane["argv"][2].endswith("\nUser task:\nCoordinate")
    assert lane["path"].is_relative_to(bridge.home)
    settings = lane["settings"]
    assert settings["amp.url"] == "https://ampcode.com/"
    assert settings["amp.mcpServers"]["docs"] == {
        "command": "docs-mcp",
        "args": [],
    }
    assert settings["amp.hooks"][0]["name"] == "notify"
    server = settings["amp.mcpServers"]["agent_parley"]
    assert server["url"].endswith("/mcp/")
    identity = json.loads(
        (lane["directory"] / "helper-identity.json").read_text()
    )
    assert server["headers"]["Authorization"] == (
        f"Bearer {identity['registration_token']}"
    )
    assert "agent_parley" not in (account / "settings.json").read_text()


def test_every_supported_event_runs_the_configured_hook_command(lane):
    assert lane["command"][-2:] == ["--adapter", "amp"]
    assert lane["command"][1:3] == ["-m", "agent_parley.hook"]
    events = [entry["event"] for entry in lane["settings"]["amp.hooks"][1:]]
    assert events == list(amp.EVENTS)
    assert roster.unavailable_hooks("amp") == list(AVAILABLE)


def test_a_native_branch_switch_is_refused_in_hook_schema(lane):
    output = fire(
        lane,
        "tool:pre-execute",
        {
            "threadId": "T-1",
            "cwd": str(lane["lane"]),
            "toolName": "Bash",
            "input": {"cmd": "git switch main"},
        },
    )
    assert output["action"] == "reject"
    assert "owns this worktree" in output["reason"]
    assert "hookSpecificOutput" not in output


def test_an_allowed_tool_call_leaves_the_decision_to_amp(lane):
    output = fire(
        lane,
        "tool:pre-execute",
        {
            "threadId": "T-2",
            "cwd": str(lane["lane"]),
            "toolName": "Bash",
            "input": {"cmd": "git status"},
        },
    )
    assert output == {}
    state = json.loads((lane["directory"] / "helper-activity.json").read_text())
    assert state["event"] == "PreToolUse"
    assert state["activity"] == "working"


def test_a_tool_result_is_observed_without_a_decision(lane):
    output = fire(
        lane,
        "tool:post-execute",
        {
            "threadId": "T-3",
            "cwd": str(lane["lane"]),
            "toolName": "Bash",
            "input": {"cmd": "ls"},
            "output": "README.md",
        },
    )
    assert output == {}


def test_resume_without_a_recorded_session_is_refused(lane, bridge, repo):
    with pytest.raises(BridgeError, match="No usable native session"):
        bridge.launch("helper", repo, "Continue", "amp", "work", resume=True)


def test_resume_continues_the_recorded_thread_and_rebuilds_the_overlay(
    lane, bridge, repo
):
    fire(
        lane,
        "tool:pre-execute",
        {
            "threadId": "T-11",
            "cwd": str(lane["lane"]),
            "toolName": "Read",
            "input": {"path": "README.md"},
        },
    )
    lane["path"].unlink()
    assert (
        bridge.launch("helper", repo, "Continue", "amp", "work", resume=True)
        == 0
    )
    capture = json.loads(lane["capture"].read_text())
    assert capture["argv"][:3] == ["threads", "continue", "T-11"]
    assert lane["path"].exists()


def test_retire_removes_the_overlay(lane, bridge, repo):
    assert lane["path"].exists()
    bridge.retire(repo, "helper")
    assert not lane["path"].exists()


def test_settings_that_already_name_the_server_are_refused(bridge, tmp_path):
    home = tmp_path / "taken"
    home.mkdir()
    write_json(home / "settings.json", {"amp.mcpServers": {"agent_parley": {}}})
    with pytest.raises(BridgeError, match="already define agent_parley"):
        amp.configure(
            tmp_path,
            "lane",
            "http://127.0.0.1/mcp/",
            "secret",
            bridge.hooks("lane", tmp_path),
            str(home),
        )
    write_json(home / "settings.json", ["not", "an", "object"])
    with pytest.raises(BridgeError, match="must be an object"):
        amp.configure(
            tmp_path,
            "lane",
            "http://127.0.0.1/mcp/",
            "secret",
            bridge.hooks("lane", tmp_path),
            str(home / "settings.json"),
        )


def test_hook_events_and_fields_reach_the_shared_parser():
    normalized = amp.payload(
        {
            "event": "tool:pre-execute",
            "tool": "mcp__agent_parley__fetch_inbox",
            "arguments": {"limit": 1},
            "thread_id": "T-5",
        }
    )
    assert normalized == {
        "hook_event_name": "PreToolUse",
        "tool_name": "mcp__agent_parley__fetch_inbox",
        "tool_input": {"limit": 1},
        "session_id": "T-5",
    }
    assert amp.payload({"hook_event_name": "Stop"}) == {
        "hook_event_name": "Stop"
    }
    assert amp.response(
        {
            "hookSpecificOutput": {
                "permissionDecision": "deny",
                "permissionDecisionReason": "Review mail",
            }
        }
    ) == {"action": "reject", "reason": "Review mail"}
    assert amp.response({"decision": "block", "reason": "Restore"}) == {}
    assert amp.response({}) == {}


def test_provider_inspection_exposes_unavailable_hooks(bridge):
    inspected = roster.inspect(bridge.home)
    assert inspected["amp"]["unavailable_hooks"] == [
        "SessionStart",
        "UserPromptSubmit",
        "PermissionRequest",
        "Stop",
        "SessionEnd",
    ]
    assert inspected["amp"]["command"] == "amp"
    assert inspected["opencode"]["unavailable_hooks"] == ["SessionEnd"]


def test_a_launch_is_refused_because_amp_lacks_required_guards(
    bridge, repo, monkeypatch, tmp_path
):
    install(monkeypatch, tmp_path)
    with pytest.raises(
        BridgeError, match="cannot deliver SessionStart, Stop, so"
    ):
        bridge.launch("helper", repo, "Coordinate", "amp")
    assert not (tmp_path / "capture.json").exists()
