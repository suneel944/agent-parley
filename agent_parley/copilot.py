"""Adapts native Copilot CLI hook results without changing permissions."""

import re

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
