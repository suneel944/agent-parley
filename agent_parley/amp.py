"""Adapts native Amp settings and tool hooks to shared checkpoints.

Amp reads one ``settings.json`` from ``~/.config/amp``, or from the file
``AMP_SETTINGS_FILE`` or ``--settings-file`` names, holding MCP servers under
``amp.mcpServers`` and hook commands under ``amp.hooks``. Its hooks fire
around tool execution only; no thread start, prompt, approval or idle event
runs a command. The lane therefore receives a private copy of the user's
settings, selected with ``AMP_SETTINGS_FILE`` for one launch only, holding
the lane's MCP server and one hook per supported event that runs the
configured hook command. Every other shared event is reported as
unavailable, so the launcher refuses a lane rather than running it without
the guards those events carry.
"""

import json
import os
import shlex
from pathlib import Path

from agent_parley import protocol
from agent_parley.state import BridgeError, write_json

EVENTS = {
    "tool:pre-execute": "PreToolUse",
    "tool:post-execute": "PostToolUse",
}
SETTINGS = "settings.json"
SERVERS = "amp.mcpServers"
HOOKS = "amp.hooks"
TOOL_FIELDS = ("tool_name", "tool", "toolName")
INPUT_FIELDS = ("tool_input", "input", "arguments")
RESULT_FIELDS = ("tool_response", "output", "result")
SESSION_FIELDS = ("session_id", "threadId", "threadID", "thread_id")


def configure(
    directory: Path,
    name: str,
    url: str,
    token: str,
    hooks: dict,
    source_path: str | None = None,
) -> Path:
    """Copies the user's Amp settings into a lane-private settings file.

    The lane's MCP server is added under ``amp.mcpServers`` and one hook per
    event in ``EVENTS`` is appended under ``amp.hooks``, each running the
    shared hook command. Every other key is carried unchanged, the source
    file is never written, and Amp's own credential store outside the
    settings file stays in effect. The registration credential is written
    literally because Amp substitutes no environment references in settings;
    it sits beside the identity file that already holds it, under the
    private state root.

    Args:
        directory: Private project state directory.
        name: Validated participant name.
        url: Authenticated loopback MCP endpoint.
        token: The lane's registration credential.
        hooks: Common checkpoint hook definitions.
        source_path: Settings file, or the directory holding one, selected
            by the launch environment; Amp's default location when unset.

    Returns:
        Settings file for AMP_SETTINGS_FILE in this launch only.

    Raises:
        BridgeError: If the user's settings cannot safely accept the lane
            server or hooks.
    """
    source = Path(
        source_path
        or os.environ.get(
            "AMP_SETTINGS_FILE",
            str(Path.home() / ".config" / "amp" / SETTINGS),
        )
    )
    if source.is_dir():
        source = source / SETTINGS
    settings = json.loads(source.read_text()) if source.exists() else {}
    if not isinstance(settings, dict):
        raise BridgeError("Amp settings must be an object.")
    servers = settings.setdefault(SERVERS, {})
    entries = settings.setdefault(HOOKS, [])
    if not isinstance(servers, dict) or not isinstance(entries, list):
        raise BridgeError(
            f"Amp {SERVERS} and {HOOKS} must be an object and an array."
        )
    if "agent_parley" in servers:
        raise BridgeError("Amp settings already define agent_parley.")
    servers["agent_parley"] = {
        "url": url,
        "headers": {
            "Authorization": f"Bearer {token}",
            protocol.HEADER: str(protocol.PROTOCOL),
        },
    }
    command = shlex.split(hooks["PreToolUse"][0]["hooks"][0]["command"])
    command += ["--adapter", "amp"]
    for event in EVENTS:
        entries.append(
            {
                "name": f"agent-parley-{EVENTS[event].lower()}",
                "event": event,
                "command": command[0],
                "args": command[1:],
                "timeout": 3000,
            }
        )
    path = directory / f"{name}-amp-settings.json"
    write_json(path, settings)
    return path


def payload(value: dict) -> dict:
    """Translates a native tool hook input into a shared checkpoint.

    The shared parser reads the event name from ``EVENTS`` and the tool
    name, input, result and thread identity under the field names every
    other adapter uses.
    """
    aliases = (
        ("tool_name", TOOL_FIELDS),
        ("tool_input", INPUT_FIELDS),
        ("tool_response", RESULT_FIELDS),
        ("session_id", SESSION_FIELDS),
    )
    named = {field for _, fields in aliases for field in fields}
    named.update(("event", "hook_event_name"))
    translated = {key: item for key, item in value.items() if key not in named}
    event = value.get("hook_event_name", value.get("event", ""))
    translated["hook_event_name"] = EVENTS.get(event, event)
    for target, fields in aliases:
        for field in fields:
            if field in value:
                translated[target] = value[field]
                break
    return translated


def response(value: dict) -> dict:
    """Flattens shared checkpoint output into Amp's hook decision.

    A refusal becomes ``reject`` with its reason. Any other result is left
    empty so Amp's own permission flow decides; the hook never allows a
    call on the lane's behalf, so no native approval is bypassed.
    """
    details = value.get("hookSpecificOutput", {})
    if details.get("permissionDecision") == "deny":
        return {
            "action": "reject",
            "reason": details.get("permissionDecisionReason", ""),
        }
    return {}
