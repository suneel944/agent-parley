"""Checks native settings preservation and the Gemini hook wire contract."""

import json

import pytest

from agent_parley import gemini
from agent_parley.state import BridgeError, write_json


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
