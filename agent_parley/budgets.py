"""Derives a lane's consumption against its advisory budget.

A budget is a line an operator draws, not a gate the runtime enforces. The
readings it is compared with already exist: the tokens a lane's own native
client recorded, the coordination calls the store served for it, and the
hours its session process has been alive. No vendor is asked and no price is
applied, so a token budget is a count and never spend. Crossing a limit marks
the lane, prints the figures and sends the lane one notice; nothing is
stopped, revoked or refused, and what to do about it stays the operator's
decision.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from agent_parley import checkpoints, process, records, roster, store
from agent_parley.state import BridgeError

UNITS = {"tokens": "tokens", "calls": "calls", "hours": "h"}


def limits(home: Path, manifest: dict, name: str) -> dict:
    """Resolves the limits that apply to one lane.

    A limit recorded on the participant wins over one recorded on its
    provider, which wins over the project default, field by field, so a
    project can give every lane a ceiling and one lane can still be given
    its own.

    Args:
        home: Private bridge state root.
        manifest: Project manifest holding this participant.
        name: Participant that owns the lane.

    Returns:
        The effective limit per budget field, absent where none applies.
    """
    participant = manifest["participants"][name]
    try:
        inherited = roster.budget(
            dict(
                roster.provider(home, participant["provider"]).get("budget")
                or {}
            )
        )
    except BridgeError:
        inherited = {}
    layers = (
        manifest.get("budget") or {},
        inherited,
        participant.get("budget") or {},
    )
    result: dict = {}
    for layer in layers:
        result.update(layer)
    return result


def consumption(
    home: Path,
    directory: Path,
    manifest: dict,
    name: str,
    usage: dict,
    cache: dict,
    wanted: set[str],
) -> dict:
    """Reads what one lane has consumed, from records that already exist.

    Args:
        home: Private bridge state root.
        directory: Private state directory for the common repository.
        manifest: Project manifest holding this participant.
        name: Participant that owns the lane.
        usage: Served-call statistics per registered identity, as the store
            reports them.
        cache: Caller-owned session-record reading cache.
        wanted: Fields a limit applies to; the others are not read, so a
            lane without a token budget never has its transcript parsed.

    Returns:
        Tokens the lane's client recorded, or None when unreadable or not
        wanted; calls served for it; hours its recorded session process has
        been alive, zero when no live session is recorded.
    """
    participant = manifest["participants"][name]
    hours = 0.0
    if "hours" in wanted:
        state = checkpoints.activity(directory, name)
        started = state.get("session_started")
        if isinstance(started, (int, float)) and process.alive(
            state.get("session_pid"), state.get("session_ticks")
        ):
            hours = max(0.0, time.time() - float(started)) / 3600
    return {
        "tokens": (
            records.reported_tokens(home, participant, cache)
            if "tokens" in wanted
            else None
        ),
        "calls": int(usage.get(participant["display"], {}).get("calls", 0)),
        "hours": round(hours, 2),
    }


def report(
    home: Path,
    directory: Path,
    manifest: dict,
    name: str,
    usage: dict,
    cache: dict | None = None,
) -> dict:
    """Compares one lane's consumption with the limits that apply to it.

    Args:
        home: Private bridge state root.
        directory: Private state directory for the common repository.
        manifest: Project manifest holding this participant.
        name: Participant that owns the lane.
        usage: Served-call statistics per registered identity.
        cache: Session-record reading cache; a fresh one when None.

    Returns:
        The effective limits, the readings, the share consumed per limited
        field as a percentage, the fields whose limit is crossed, and
        whether any is. A lane with no limit is never over budget, and an
        unreadable token count never crosses a token limit.
    """
    applied = limits(home, manifest, name)
    used = consumption(
        home,
        directory,
        manifest,
        name,
        usage,
        {} if cache is None else cache,
        set(applied),
    )
    share = {}
    crossed = []
    for field in roster.BUDGET_FIELDS:
        limit = applied.get(field)
        reading = used[field]
        if limit is None or reading is None:
            continue
        share[field] = int(reading * 100 // limit)
        if reading > limit:
            crossed.append(field)
    return {
        "limits": applied,
        "used": used,
        "share": share,
        "crossed": crossed,
        "over": bool(crossed),
    }


def standing(home: Path, directory: Path, manifest: dict, name: str) -> dict:
    """Compares one lane with its budget, reading the store only when needed.

    Args:
        home: Private bridge state root.
        directory: Private state directory for the common repository.
        manifest: Project manifest holding this participant.
        name: Participant that owns the lane.

    Returns:
        The comparison ``report`` produces. Served-call statistics are read
        only when a call limit applies, and an unreadable store counts no
        calls rather than failing the caller.
    """
    usage: dict = {}
    if "calls" in limits(home, manifest, name):
        try:
            usage = store.usage(home, manifest["root"])
        except sqlite3.Error:
            usage = {}
    return report(home, directory, manifest, name, usage)


def _figure(field: str, value: float | None) -> str:
    """Formats one reading or limit in the units of its field."""
    if value is None:
        return "?"
    text = f"{value:,}" if type(value) is int else f"{value:g}"
    return text + ("h" if field == "hours" else "")


def marker(reading: dict) -> str:
    """Describes a lane's standing against its budget in one line.

    Args:
        reading: Comparison produced by ``report``.

    Returns:
        An empty string when no limit applies; otherwise the share consumed
        per limited field, led by ``over budget`` when any limit is crossed.
    """
    if not reading["limits"]:
        return ""
    parts = []
    for field in roster.BUDGET_FIELDS:
        if field not in reading["limits"]:
            continue
        figures = (
            f"{_figure(field, reading['used'][field])} of "
            f"{_figure(field, reading['limits'][field])}"
        )
        share = reading["share"].get(field)
        parts.append(
            f"{field} {figures}"
            + (f" ({share}%)" if share is not None else "")
            + ("!" if field in reading["crossed"] else "")
        )
    lead = "over budget" if reading["over"] else "budget"
    return f"{lead}; " + ", ".join(parts)


def notice(reading: dict) -> str:
    """Words the one advisory notice a lane receives on crossing a limit.

    Args:
        reading: Comparison produced by ``report``.

    Returns:
        A bounded sentence naming each crossed limit and its reading, and
        stating that nothing is stopped. Empty when no limit is crossed.
    """
    if not reading["crossed"]:
        return ""
    figures = ", ".join(
        f"{field} {_figure(field, reading['used'][field])} of "
        f"{_figure(field, reading['limits'][field])}"
        for field in reading["crossed"]
    )
    return (
        f"Budget notice: this lane is over its advisory budget ({figures}). "
        "Nothing is stopped or refused; the operator decides. Finish or "
        "report your current step and keep coordination brief."
    )
