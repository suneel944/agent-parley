"""Records a tool call the provider's own permission layer refused.

A client running in an automatic permission mode can refuse a tool call
without drawing any prompt: Claude Code's auto mode classifier denies the
call, the lane is told only in the tool result, and its turn goes on or ends
on that refusal. No permission prompt stands on the screen, so neither the
dialog watcher nor the permission prompt notification sees it, and the lane
stops with nobody told.

`EVENTS` names each native hook event that reports such a refusal and the
adapter whose client raises it; the launcher installs the lane's hook on it
for that adapter only. `MARKERS` holds the text each adapter's client writes
for one, from which the stated reason is read. Another adapter is added by
naming its event and its marker. Only Claude Code is recorded:
``PermissionDenied``, which the ``claude`` CLI raises after its auto mode
classifier denies a tool call, carrying the tool name, its input and the
reason. No Codex denial is modelled, because no Codex hook event reporting
one has been observed.

A refusal becomes one operator decision, keyed by the lane, the claim it
holds and the exact refused command, so the same refusal repeated opens no
second decision. It is recorded irreversible, so no timeout ever answers it
with a default, and while it is open the supervisor defers every wake of the
lane under `CAUSE` instead of asking it for another turn. Nothing here
retries, rewords or works around the refused call, and no permission rule or
bypass flag is ever written: the rule the decision shows is a suggestion the
operator can add to their own settings.
"""

import json
import re
import shlex
from collections.abc import Mapping
from pathlib import Path
from typing import NamedTuple

from agent_parley import decisions
from agent_parley.state import BridgeError

KIND = "permission_denied"
CAUSE = "permission denied"
MAX_COMMAND_BYTES = 512
MAX_REASON_BYTES = 200
SHELL_TOOLS = frozenset({"Bash"})
TARGET_FIELDS = (
    "command",
    "file_path",
    "notebook_path",
    "path",
    "url",
    "pattern",
)
RUN = "run it yourself"
RULE = "add a rule"
REASSIGN = "reassign"
RELEASE = "release"
EVENTS: dict[str, str] = {"PermissionDenied": "claude"}
MARKERS: dict[str, re.Pattern[str]] = {
    "claude": re.compile(
        r"denied by the Claude Code auto mode classifier\.\s*"
        r"Reason:\s*\[?(?P<reason>[^\]\n]+)\]?"
    ),
}


class Denial(NamedTuple):
    """One tool call a provider's permission layer refused.

    Attributes:
        provider: Adapter whose client's permission layer refused the call.
        tool: Native tool name.
        command: The exact refused command, or the tool's input when the
            tool runs no command.
        reason: The reason the permission layer stated.
    """

    provider: str
    tool: str
    command: str
    reason: str


def _clipped(text: str, budget: int) -> str:
    """Cuts text to a byte budget without splitting a character.

    Args:
        text: Text to keep.
        budget: Byte cap.

    Returns:
        The text, at most ``budget`` bytes long.
    """
    return text.encode()[:budget].decode(errors="ignore")


def _command(tool_input: object) -> str:
    """Reads the command or target a tool call named.

    Args:
        tool_input: The native tool input.

    Returns:
        The first command or path field it carries, the whole input as JSON
        when it carries none, clipped to `MAX_COMMAND_BYTES`.
    """
    if not isinstance(tool_input, Mapping):
        return _clipped(str(tool_input or ""), MAX_COMMAND_BYTES)
    for field in TARGET_FIELDS:
        value = tool_input.get(field)
        if isinstance(value, str) and value:
            return _clipped(value, MAX_COMMAND_BYTES)
    text = json.dumps(tool_input, sort_keys=True) if tool_input else ""
    return _clipped(text, MAX_COMMAND_BYTES)


def _reason(provider: str, text: str) -> str:
    """Reads the stated reason out of a provider's refusal text.

    Args:
        provider: Provider that refused the call.
        text: The reason field or the refusal text it wrote.

    Returns:
        The reason its marker names, or the text's first line without its
        enclosing brackets when the marker does not match, clipped to
        `MAX_REASON_BYTES`.
    """
    marker = MARKERS.get(provider)
    found = marker.search(text) if marker else None
    stated = found["reason"] if found else text.strip().partition("\n")[0]
    return _clipped(stated.strip().strip("[]").strip(), MAX_REASON_BYTES)


def detect(payload: Mapping[str, object]) -> Denial | None:
    """Recognizes a native hook event that reports a refused tool call.

    Only an event `EVENTS` names is read, so a tool result that merely
    quotes a refusal, such as a file holding the marker text, is never
    taken for one.

    Args:
        payload: Native lifecycle event.

    Returns:
        The refusal, or None when the event reports none.
    """
    provider = EVENTS.get(str(payload.get("hook_event_name", "")))
    if provider is None:
        return None
    return Denial(
        provider,
        str(payload.get("tool_name", "")) or "an unnamed tool",
        _command(payload.get("tool_input")),
        _reason(provider, str(payload.get("reason", "") or "")),
    )


def rule(tool: str, command: str) -> str:
    """Suggests the permission rule that would allow a refused call.

    The suggestion is shown to the operator and never written anywhere. A
    shell command is matched on its program and, when the next word is not
    an option, its subcommand, in the ``Tool(prefix:*)`` form Claude Code
    settings take; any other tool is named whole.

    Args:
        tool: Native tool name.
        command: The refused command.

    Returns:
        The suggested rule.
    """
    if tool not in SHELL_TOOLS:
        return tool
    try:
        words = shlex.split(command)
    except ValueError:
        words = command.split()
    prefix = words[:1]
    if len(words) > 1 and not words[1].startswith("-"):
        prefix = words[:2]
    return f"{tool}({' '.join(prefix)}:*)" if prefix else tool


def options(claimed: bool) -> tuple[str, ...]:
    """Lists the answers a refusal offers.

    Args:
        claimed: Whether the lane holds a claim that can move.

    Returns:
        Running the command by hand and adding a rule, then reassigning or
        releasing the claim when there is one.
    """
    return (RUN, RULE, REASSIGN, RELEASE) if claimed else (RUN, RULE)


def record(
    directory: Path,
    project: str,
    lane: str,
    denial: Denial,
    issue: str = "",
    now: float = 0.0,
) -> dict:
    """Opens the one decision a refusal asks for, or refreshes it.

    Args:
        directory: Private project state directory.
        project: Canonical project root.
        lane: Lane whose tool call was refused.
        denial: The refusal.
        issue: Issue number of the claim the lane holds, if any.
        now: Unix time; the clock when zero.

    Returns:
        The open decision record.

    Raises:
        BridgeError: If the decision cannot be recorded.
    """
    suggested = rule(denial.tool, denial.command)
    detail = "\n".join(
        (
            f"claim: #{issue}" if issue else "claim: none",
            f"tool: {denial.tool}",
            f"command: {denial.command}",
            f"reason: {denial.reason or 'not stated'}",
            f"{RUN}: run the command above in the lane's worktree",
            f"{RULE}: {suggested}",
        )
    )
    try:
        return decisions.open_or_refresh(
            directory,
            project=project,
            lane=lane,
            kind=KIND,
            key="\x00".join((issue, denial.tool, denial.command)),
            question=f"{denial.provider} denied a {denial.tool} call",
            options=options(bool(issue)),
            issue=issue,
            reversibility=decisions.IRREVERSIBLE,
            detail=detail,
            now=now,
        )
    except OSError as exc:
        raise BridgeError(f"Denial decision not recorded: {exc}") from exc


def waiting(directory: Path, lane: str, now: float = 0.0) -> dict:
    """Finds the open refusal decision that holds a lane's wakes.

    Args:
        directory: Private project state directory.
        lane: Lane to look up.
        now: Unix time; the clock when zero.

    Returns:
        The oldest open refusal decision of the lane, or an empty mapping.
    """
    for entry in decisions.list_open(directory, now):
        if entry.get("kind") == KIND and entry.get("lane") == lane:
            return entry
    return {}
