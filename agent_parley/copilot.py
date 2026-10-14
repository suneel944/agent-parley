"""Adapts native Copilot CLI hook results without changing permissions.

It also merges lane hooks and the MCP server into a Copilot profile at
launch, and removes one retired lane's entries from that profile again.
"""

import json
import re
import shlex
import sys
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


def configure_copilot(
    home: Path, server: dict, hooks: dict, owner: str = ""
) -> None:
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

    Copilot runs every hook in a profile for every session on it, so a hook
    another participant left behind would decide this lane's events. With
    ``owner`` named, every bridge hook naming a different participant is
    dropped; the launcher has already refused the launch when such a
    participant is still live, so only retired or absent lanes lose entries.

    Args:
        home: Credential profile's native configuration directory.
        server: Agent Parley MCP server definition.
        hooks: Native hook events and their command lists.
        owner: Participant the profile now serves, or empty to keep every
            existing entry.

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
            if owner:
                existing[:] = [
                    command
                    for command in existing
                    if hook_participant(command) in (None, owner)
                ]
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
    Files that were never written leave nothing to do.

    The profile directory is resolved from the recorded credential alone,
    without the variables a launch requires from the caller's shell, because
    a retirement runs from whatever shell or service retires the lane. A
    profile that no longer resolves is reported on standard error, since
    the lane's hooks then stay in that directory for every later session.

    Args:
        home: Private bridge state root.
        participant: Recorded participant entry naming provider and profile.
        name: Participant name the hook command carries.
    """
    try:
        entry = roster.provider(home, participant["provider"])
    except BridgeError:
        return
    if entry["adapter"] != "copilot":
        return
    try:
        config_home = roster.config_home(
            home, entry, participant.get("credential")
        )
    except BridgeError as error:
        print(
            f"Could not remove {name}'s Copilot hooks: {error}",
            file=sys.stderr,
        )
        return
    if not config_home:
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


def hook_participant(command: object) -> str | None:
    """Names the participant a Copilot hook entry runs this bridge's hook for.

    Args:
        command: One entry of a hook list in a Copilot ``settings.json``.

    Returns:
        The ``--participant`` value of a bridge hook, or None for an entry
        that is not one, such as the operator's own hook.
    """
    if not isinstance(command, dict):
        return None
    try:
        words = shlex.split(str(command.get("bash", "")))
    except ValueError:
        return None
    if not any(bridge_hook(word) for word in words):
        return None
    for index in range(len(words) - 1):
        if words[index] == "--participant":
            return words[index + 1]
    return None


def owned_hook(command: object, name: str) -> bool:
    """Reports whether a Copilot hook entry runs this participant's hook."""
    return hook_participant(command) == name


def occupants(
    home: Path, config_home: Path, directory: Path, name: str
) -> list[str]:
    """Names the other live lanes whose hooks a Copilot profile already runs.

    A lane counts once its hooks are in the profile's ``settings.json`` and
    it is a participant of a project in this home that is not retired and
    resolves to the same profile directory on a ``copilot`` adapter. A lane
    refused before configuring its hooks therefore never blocks another.

    Args:
        home: Private bridge state root.
        config_home: Profile directory the launching lane would configure.
        directory: Project state directory of the launching lane.
        name: Launching participant, which is never its own occupant.

    Returns:
        Sorted participant names, empty when the profile is free.
    """
    try:
        settings = json.loads((config_home / "settings.json").read_text())
    except (OSError, ValueError):
        return []
    hooks = settings.get("hooks") if isinstance(settings, dict) else None
    hooked = {
        hook_participant(command)
        for commands in (hooks.values() if isinstance(hooks, dict) else ())
        if isinstance(commands, list)
        for command in commands
    }
    found = set()
    for path in sorted((home / "projects").glob("*/project.json")):
        try:
            participants = roster.read(path.parent)["participants"]
        except (BridgeError, OSError, ValueError, KeyError):
            continue
        for other, entry in participants.items():
            if (
                other not in hooked
                or (path.parent == directory and other == name)
                or roster.retired(entry)
            ):
                continue
            try:
                provider = roster.provider(home, entry["provider"])
                selected = roster.config_home(
                    home, provider, entry.get("credential")
                )
            except (BridgeError, KeyError):
                continue
            if (
                provider["adapter"] == "copilot"
                and selected
                and Path(selected) == config_home
            ):
                found.add(other)
    return sorted(found)
