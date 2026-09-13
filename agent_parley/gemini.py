"""Adapts native Gemini CLI settings and hooks without changing permissions."""

import json
import os
import sys
from pathlib import Path

from agent_parley.state import BridgeError, write_json

EVENTS = {
    "SessionStart": "SessionStart",
    "BeforeAgent": "UserPromptSubmit",
    "BeforeTool": "PreToolUse",
    "AfterTool": "PostToolUse",
    "AfterAgent": "Stop",
    "SessionEnd": "SessionEnd",
}


def configure(
    directory: Path,
    name: str,
    url: str,
    hooks: dict,
    source_path: str | None = None,
) -> Path:
    """Copies native system policy into a lane-private settings overlay.

    User and project configuration and the normal authentication home remain
    selected by Gemini itself. Existing system policy is copied unchanged;
    only this lane's named MCP server and additional hooks are added.

    Args:
        directory: Private project state directory.
        name: Validated participant name.
        url: Authenticated loopback MCP endpoint.
        hooks: Common checkpoint hook definitions.
        source_path: Native system policy selected by the launch environment.

    Returns:
        Path for GEMINI_CLI_SYSTEM_SETTINGS_PATH in this launch only.

    Raises:
        BridgeError: If native settings cannot safely accept the lane hooks.
    """
    default = (
        "/Library/Application Support/GeminiCli/settings.json"
        if sys.platform == "darwin"
        else "/etc/gemini-cli/settings.json"
    )
    source = Path(
        source_path
        or os.environ.get("GEMINI_CLI_SYSTEM_SETTINGS_PATH", default)
    )
    settings = json.loads(source.read_text()) if source.exists() else {}
    if not isinstance(settings, dict):
        raise BridgeError("Gemini system settings must be an object.")
    if settings.get("hooksConfig", {}).get("enabled") is False:
        raise BridgeError(
            "Native Gemini policy disables hooks; launch refused."
        )
    servers = settings.setdefault("mcpServers", {})
    configured = settings.setdefault("hooks", {})
    if not isinstance(servers, dict) or not isinstance(configured, dict):
        raise BridgeError("Gemini MCP and hook settings must be objects.")
    if "agent_parley" in servers:
        raise BridgeError("Native system policy already defines agent_parley.")
    servers["agent_parley"] = {
        "httpUrl": url,
        "headers": {"Authorization": "Bearer ${AGENT_PARLEY_TOKEN}"},
    }
    for native, common in EVENTS.items():
        existing = configured.setdefault(native, [])
        if not isinstance(existing, list):
            raise BridgeError(f"Gemini {native} hooks must be an array.")
        command = hooks[common][0]["hooks"][0]["command"] + " --adapter gemini"
        existing.append(
            {
                "hooks": [
                    {"type": "command", "command": command, "timeout": 3000}
                ]
            }
        )
    target = directory / f"{name}-gemini-settings.json"
    write_json(target, settings)
    return target


def payload(value: dict) -> dict:
    """Translates Gemini events and MCP names for shared checkpoints."""
    translated = dict(value)
    translated["hook_event_name"] = EVENTS.get(
        value.get("hook_event_name", ""), value.get("hook_event_name", "")
    )
    tool = value.get("tool_name", "")
    if tool.startswith("mcp_agent_parley_"):
        translated["tool_name"] = tool.replace(
            "mcp_agent_parley_", "mcp__agent_parley__", 1
        )
    return translated


def response(value: dict) -> dict:
    """Translates shared checkpoint denials and context to Gemini's schema."""
    details = value.get("hookSpecificOutput", {})
    result: dict = {}
    if (
        details.get("permissionDecision") == "deny"
        or value.get("decision") == "block"
    ):
        result.update(
            decision="deny",
            reason=details.get("permissionDecisionReason")
            or value.get("reason", ""),
        )
    if context := details.get("additionalContext"):
        result["hookSpecificOutput"] = {"additionalContext": context}
    return result
