"""Records what a lane did in the turn that left it waiting on the operator.

When a lane stops for an approval, a native dialog or a decision, the
operator is asked a question without the turn that led to it. A recap keeps
that turn beside the question: the lane's last assistant message, the files
changed since the turn began, the commands it ran and the ones that failed.

Every part is read, never generated. The message and the commands come from
the lane's native session record through `records.turn_reading`, the files
from Git against the commit the prompt hook recorded as the turn's start,
and no model is asked. Each part and the whole record are capped in bytes.

A recap is stored as ``<lane>-recap.json`` in the project's private state
directory and read only by operator surfaces: lane detail and the decision
message. It is never written to mail, the feed or any notice a lane
receives, because a lane's prose and commands are its own.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import time
from collections.abc import Mapping
from pathlib import Path

from agent_parley import checkpoints, records, worktrees
from agent_parley.state import BridgeError, write_json

RECORD_SUFFIX = "-recap.json"
MAX_RECAP_BYTES = 2048
MAX_MESSAGE_BYTES = 640
MAX_ITEM_BYTES = 160
MAX_ITEMS = 8
ELLIPSIS = "..."


def _clipped(text: str, limit: int) -> str:
    """Flattens text to one line and cuts it to a byte cap.

    Args:
        text: Text to keep.
        limit: Maximum UTF-8 bytes kept, the ellipsis included.

    Returns:
        The text on one line, ending in an ellipsis when it was cut.
    """
    flat = " ".join(text.split())
    if len(flat.encode()) <= limit:
        return flat
    return checkpoints.clip(flat, max(limit - len(ELLIPSIS), 0)) + ELLIPSIS


def _size(record: Mapping[str, object]) -> int:
    """Measures a record as the UTF-8 bytes of its compact JSON.

    Args:
        record: Record to measure.

    Returns:
        Its encoded length in bytes.
    """
    return len(json.dumps(record, ensure_ascii=False).encode())


def _items(values: list[str]) -> tuple[list[str], int]:
    """Keeps the last items of a list, each cut to the item cap.

    Args:
        values: Items in the order they happened.

    Returns:
        The last `MAX_ITEMS` items, clipped, and how many were left out.
    """
    kept = values[-MAX_ITEMS:]
    return (
        [_clipped(value, MAX_ITEM_BYTES) for value in kept],
        len(values) - len(kept),
    )


def assemble(
    *,
    message: str,
    files: list[str],
    commands: list[str],
    failed: list[str],
    started: float,
    now: float,
) -> dict:
    """Builds a recap record from its parts, applying every byte cap.

    The message keeps its first `MAX_MESSAGE_BYTES` bytes; files, commands
    and failures keep their last `MAX_ITEMS` entries of at most
    `MAX_ITEM_BYTES` bytes each and count the rest. Items are then dropped
    from the front, files first, until the encoded record fits
    `MAX_RECAP_BYTES`.

    Args:
        message: The lane's last assistant message in the turn.
        files: Paths changed since the turn began.
        commands: Commands the turn ran, oldest first.
        failed: Commands among them that failed, oldest first.
        started: Unix time the turn began, zero when unknown.
        now: Unix time the recap is taken.

    Returns:
        The recap record, at most `MAX_RECAP_BYTES` bytes as JSON.
    """
    record: dict = {
        "at": now,
        "started": started,
        "message": _clipped(message, MAX_MESSAGE_BYTES),
    }
    for name, values in (
        ("files", files),
        ("commands", commands),
        ("failed", failed),
    ):
        record[name], record[f"{name}_omitted"] = _items(values)
    for name in ("files", "commands", "failed"):
        while record[name] and _size(record) > MAX_RECAP_BYTES:
            record[name].pop(0)
            record[f"{name}_omitted"] += 1
    return record


def changed_files(lane: Path, base: str) -> list[str]:
    """Lists the paths a lane changed since a commit, untracked ones too.

    Args:
        lane: The lane's worktree.
        base: Commit the turn began on; the current commit when empty or no
            longer readable.

    Returns:
        Sorted paths relative to the worktree, or an empty list when Git
        cannot read it.
    """
    try:
        try:
            tracked = worktrees.git(
                lane, "diff", "--name-only", base or "HEAD", "--"
            )
        except BridgeError:
            tracked = worktrees.git(lane, "diff", "--name-only", "HEAD", "--")
        untracked = worktrees.git(
            lane, "ls-files", "--others", "--exclude-standard"
        )
    except (BridgeError, OSError, subprocess.TimeoutExpired):
        return []
    return sorted(set((tracked + "\n" + untracked).split()) - {""})


def path(directory: Path, name: str) -> Path:
    """Names the file one lane's recap is stored in.

    Args:
        directory: Private project state directory.
        name: Participant that owns the lane.

    Returns:
        The ``<name>-recap.json`` path in the state directory.
    """
    return directory / f"{name}{RECORD_SUFFIX}"


def capture(
    home: Path,
    directory: Path,
    name: str,
    participant: Mapping[str, object],
    now: float | None = None,
) -> dict:
    """Builds and stores the recap of a lane's current turn.

    Args:
        home: Private bridge state root.
        directory: Private project state directory.
        name: Participant that owns the lane.
        participant: Manifest entry naming the lane, provider and account.
        now: Unix time of the recap, or None for the current time.

    Returns:
        The stored recap record.
    """
    turn = checkpoints.activity(directory, name).get("turn") or {}
    reading = records.turn_reading(home, dict(participant)) or {}
    record = assemble(
        message=str(reading.get("message", "")),
        files=changed_files(
            Path(str(participant.get("lane", ""))), str(turn.get("head", ""))
        ),
        commands=list(reading.get("commands", [])),
        failed=list(reading.get("failed", [])),
        started=float(turn.get("started", 0.0) or 0.0),
        now=time.time() if now is None else now,
    )
    write_json(path(directory, name), record)
    return record


def lane_recap(directory: Path, name: str) -> dict | None:
    """Reads one lane's stored recap without changing anything.

    Args:
        directory: Private project state directory.
        name: Participant that owns the lane.

    Returns:
        The recap record, or None when none is stored or it cannot be
        decoded.
    """
    try:
        value = json.loads(path(directory, name).read_text())
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def ensure(
    home: Path,
    directory: Path,
    name: str,
    participant: Mapping[str, object],
) -> dict | None:
    """Returns a lane's recap, taking it again when the lane moved since.

    A stored recap is current while it is no older than the lane's last
    published activity, so one wait is recapped once however many decisions
    or polls read it, and a lane that worked again is recapped afresh.
    Failures leave the stored recap as it was, because a recap is
    best-effort context and must never stop a decision from being sent.

    Args:
        home: Private bridge state root.
        directory: Private project state directory.
        name: Participant that owns the lane.
        participant: Manifest entry naming the lane, provider and account.

    Returns:
        The current recap, or None when none could be read or taken.
    """
    stored = lane_recap(directory, name)
    updated = checkpoints.activity(directory, name).get("updated") or 0.0
    if stored is not None and float(stored.get("at", 0.0)) >= float(updated):
        return stored
    with contextlib.suppress(BridgeError, OSError, TypeError, ValueError):
        return capture(home, directory, name, participant)
    return stored


def format_recap(recap: Mapping[str, object] | None, limit: int) -> str:
    """Renders a recap as the lines an operator reads above the answers.

    Args:
        recap: Recap record, or None.
        limit: Maximum UTF-8 bytes of the rendered text.

    Returns:
        A ``Last turn`` block naming the message, changed files, commands
        and failed commands, or an empty string when the recap is missing
        or holds nothing.
    """
    if not recap:
        return ""
    lines = []
    if recap.get("message"):
        lines.append(f"  said: {recap['message']}")
    for name, label in (
        ("files", "changed"),
        ("commands", "ran"),
        ("failed", "failed"),
    ):
        listed = recap.get(name)
        values = (
            [str(value) for value in listed] if isinstance(listed, list) else []
        )
        omitted = int(str(recap.get(f"{name}_omitted", 0) or 0))
        if not values and not omitted:
            continue
        more = f" (+{omitted} more)" if omitted else ""
        if name == "files":
            lines.append(f"  {label}: {', '.join(values)}{more}".rstrip())
        else:
            lines.extend(f"  {label}: {value}" for value in values)
            if more:
                lines.append(f"  {label}:{more}")
    if not lines:
        return ""
    text = "\n".join(["Last turn:", *lines])
    if len(text.encode()) <= limit:
        return text
    return checkpoints.clip(text, max(limit - len(ELLIPSIS), 0)) + ELLIPSIS
