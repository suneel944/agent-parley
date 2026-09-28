"""Adapts native Copilot CLI hook results without changing permissions.

It also merges lane hooks and the MCP server into a Copilot profile at
launch, and removes one retired lane's entries from that profile again.
"""

import json
import re
import shlex
from pathlib import Path

from agent_parley import roster
from agent_parley.state import BridgeError, lock, write_json

MCP_TOOL = re.compile(r"^mcp[_-]+agent[_-]+parley[_-]+(?P<tool>.+)$")


def payload(value: dict) -> dict:
    """Normalizes Copilot's MCP tool names for shared checkpoints.

    Copilot CLI sends the compatible snake_case event payload whenever its
    hook is registered under the PascalCase event name, so the shared parser
    reads its lifecycle fields unchanged. Only the qualified MCP tool name
    differs, and it is normalized so a lane that calls a coordination tool is
    recognized as doing so rather than being refused again.

    Args:
        value: Native Copilot hook event in the compatible payload format.

    Returns:
        The same event with any Agent Parley tool name in shared form.
    """
    match = MCP_TOOL.match(str(value.get("tool_name", "")))
    if not match:
        return value
    return {**value, "tool_name": f"mcp__agent_parley__{match['tool']}"}


def response(value: dict) -> dict:
    """Translates shared checkpoint output to Copilot's flat hook schema.

    Copilot reads ``permissionDecision``, ``permissionDecisionReason`` and
    ``additionalContext`` at the top level of a hook result rather than inside
    ``hookSpecificOutput``. A blocking stop decision already uses the field
    names Copilot expects and is carried through unchanged. Nothing here
    grants a permission Copilot refused or answers a native approval prompt.

    Args:
        value: Shared checkpoint output in the common hook schema.

    Returns:
        Hook output in the schema Copilot's compatible format expects.
    """
    details = value.get("hookSpecificOutput") or {}
    result = {key: value[key] for key in ("decision", "reason") if key in value}
    result.update(
        {
            key: details[key]
            for key in (
                "permissionDecision",
                "permissionDecisionReason",
                "additionalContext",
            )
            if key in details
        }
    )
    return result


def configure_copilot(home: Path, server: dict, hooks: dict) -> None:
    """Merges lane configuration without replacing native user settings.

    Copilot CLI selects its payload format from the case of the configured
    event name: a camelCase name delivers camelCase fields such as
    ``sessionId`` and ``toolArgs``, while a PascalCase name delivers the
    compatible snake_case fields the shared checkpoint parser already reads.
    Lane hooks are therefore registered under the shared PascalCase names, so
    a native event reaches the coordination guards instead of being discarded
    at the ignored-event boundary.

    Existing hook order is retained and identical lane hooks are not appended
    again on relaunch. Both documents are validated before either is written.

    Args:
        home: Credential profile's native configuration directory.
        server: Agent Parley MCP server definition.
        hooks: Native hook events and their command lists.

    Raises:
        BridgeError: If either existing document has an incompatible shape.
    """
    with lock(home / "agent-parley-config.lock"):
        documents = []
        for filename, key in (
            ("mcp-config.json", "mcpServers"),
            ("settings.json", "hooks"),
        ):
            path = home / filename
            try:
                data = json.loads(path.read_text()) if path.exists() else {}
            except ValueError as exc:
                raise BridgeError(f"Invalid configuration in {path}.") from exc
            if not isinstance(data, dict) or not isinstance(
                data.get(key, {}), dict
            ):
                raise BridgeError(f"Expected an object for {key} in {path}.")
            data.setdefault(key, {})
            documents.append((path, data))
        documents[0][1]["mcpServers"]["agent_parley"] = server
        settings = documents[1][1]
        settings.setdefault("version", 1)
        for event, commands in hooks.items():
            existing = settings["hooks"].setdefault(event, [])
            if not isinstance(existing, list):
                raise BridgeError(f"Expected a hook list for {event}.")
            existing.extend(
                command for command in commands if command not in existing
            )
        for path, data in documents:
            write_json(path, data)


def release_copilot(home: Path, participant: dict, name: str) -> None:
    """Removes one retired lane's hooks from its Copilot profile directory.

    Only entries whose command names this participant are removed, so other
    lanes sharing the profile and the operator's own hooks are untouched. The
    ``agent_parley`` MCP server entry is removed once no lane hook remains.
    A profile that no longer resolves, or files that were never written,
    leave nothing to do.

    Args:
        home: Private bridge state root.
        participant: Recorded participant entry naming provider and profile.
        name: Participant name the hook command carries.
    """
    try:
        entry = roster.provider(home, participant["provider"])
        account = roster.launch_environment(
            home, entry, participant.get("credential")
        )
    except BridgeError:
        return
    config_home = account.get(entry.get("home_env", ""))
    if entry["adapter"] != "copilot" or not config_home:
        return
    settings_path = Path(config_home) / "settings.json"
    servers_path = Path(config_home) / "mcp-config.json"
    with lock(Path(config_home) / "agent-parley-config.lock"):
        try:
            settings = json.loads(settings_path.read_text())
        except (OSError, ValueError):
            return
        hooks = settings.get("hooks")
        if not isinstance(hooks, dict):
            return
        remaining = 0
        for event, commands in hooks.items():
            if isinstance(commands, list):
                hooks[event] = [
                    command
                    for command in commands
                    if not owned_hook(command, name)
                ]
                remaining += sum(
                    bridge_hook(str(command.get("bash", "")))
                    for command in hooks[event]
                    if isinstance(command, dict)
                )
        write_json(settings_path, settings)
        if remaining or not servers_path.exists():
            return
        try:
            servers = json.loads(servers_path.read_text())
        except ValueError:
            return
        if isinstance(servers, dict) and isinstance(
            servers.get("mcpServers"), dict
        ):
            servers["mcpServers"].pop("agent_parley", None)
            write_json(servers_path, servers)


def bridge_hook(text: str) -> bool:
    """Reports whether a recorded command runs this project's hook.

    The launcher configures the shell client when a Bash interpreter is
    available and the Python module when it is not, so ownership is decided
    by either name rather than by the one that happens to be current.

    Args:
        text: Command line recorded in a native settings file.

    Returns:
        Whether the command runs the served client or the in-process hook.
    """
    from agent_parley import hook as hook_client

    return "agent_parley.hook" in text or hook_client.CLIENT_NAME in text


def owned_hook(command: object, name: str) -> bool:
    """Reports whether a Copilot hook entry runs this participant's hook."""
    if not isinstance(command, dict):
        return False
    try:
        words = shlex.split(str(command.get("bash", "")))
    except ValueError:
        return False
    return any(bridge_hook(word) for word in words) and any(
        words[index : index + 2] == ["--participant", name]
        for index in range(len(words) - 1)
    )
