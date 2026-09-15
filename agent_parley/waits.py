"""Holds a lane's turn open until its next mail arrives or a bound expires."""

import threading
import time
from pathlib import Path

from agent_parley import roster, store
from agent_parley.state import BridgeError

TOOL = "wait_for_message"
DEFAULT_SECONDS = 30.0
MAX_SECONDS = 120.0
POLL_SECONDS = 0.25
MAX_WAITERS = 64

_ARRIVED = threading.Condition()


def delivered() -> None:
    """Wakes every waiter because mail has just committed to the store.

    A waiter that misses this signal still re-reads its inbox within
    `POLL_SECONDS`, so mail written by a command in another process, which
    shares no condition object with the service, is never lost and only ever
    arrives late by that interval.
    """
    with _ARRIVED:
        _ARRIVED.notify_all()


def bounded(value: object, ceiling: float = MAX_SECONDS) -> float:
    """Clamps a requested wait to what this service is willing to hold open.

    Args:
        value: Seconds the caller asked to wait, or None for the default.
        ceiling: Longest wait this service serves at this moment, which is
            zero once it already holds as many waits as it admits.

    Returns:
        The wait actually held, in seconds, never above the ceiling.

    Raises:
        BridgeError: If the requested wait is not a non-negative number.
    """
    if value is None:
        requested = DEFAULT_SECONDS
    elif isinstance(value, bool) or not isinstance(value, int | float):
        raise BridgeError("timeout_seconds must be a number of seconds.")
    else:
        requested = float(value)
    if requested < 0:
        raise BridgeError("timeout_seconds must not be negative.")
    return min(requested, max(ceiling, 0.0))


def _empty(filters: dict) -> dict:
    """Returns the page an inbox with nothing to report would return."""
    after = filters.get("after_id", 0)
    return {
        "messages": [],
        "next_after_id": int(after) if isinstance(after, int) else 0,
        "has_more": False,
    }


def wait(
    home: Path,
    actor: dict,
    args: dict,
    stopping: threading.Event | None = None,
    ceiling: float = MAX_SECONDS,
) -> dict:
    """Waits for the first mail matching this lane's own inbox filters.

    The wait is a sequence of bounded sleeps around the same read
    `fetch_inbox` serves, so every filter, page limit and result budget is
    the one that tool already applies and no separate query can report mail
    the lane may not read. A delivery inside this service wakes the sleep at
    once; anything written elsewhere is seen at the next read. No thread
    spins, and nothing but the caller's own connection is held between
    reads, so the store stays open to writers for the whole wait.

    A wait that reaches its bound reports an empty page rather than an
    error, records no event and changes no receipt, so a lane that waited
    for nothing is in exactly the state it started in. A lane whose
    registration is revoked, or a service that has begun stopping, ends the
    wait the same way instead of holding a turn against state that is gone.

    Args:
        home: Private bridge state root.
        actor: Authenticated project and lane, whose own inbox is read.
        args: Inbox filters, plus an optional `timeout_seconds`.
        stopping: Event a stopping service sets, or None when the caller has
            no lifecycle to observe.
        ceiling: Longest wait this service serves at this moment.

    Returns:
        The inbox page, the wait actually held as `timeout_seconds`, and
        `expired`, which is true exactly when the page is empty because the
        wait ran out.

    Raises:
        BridgeError: If the requested wait is invalid, a filter is invalid,
            or the operator paused this lane before its mail was reported.
    """
    filters = {
        name: value for name, value in args.items() if name != "timeout_seconds"
    }
    held = bounded(args.get("timeout_seconds"), ceiling)
    deadline = time.monotonic() + held
    while True:
        page = store.peek_inbox(home, actor, filters)
        if page is None:
            return {**_empty(filters), "timeout_seconds": held, "expired": True}
        if page["messages"]:
            if roster.paused(
                home, str(actor.get("project", "")), actor["name"]
            ):
                raise BridgeError(roster.PAUSED_REASON)
            return {**page, "timeout_seconds": held, "expired": False}
        remaining = deadline - time.monotonic()
        if remaining <= 0 or (stopping is not None and stopping.is_set()):
            return {**page, "timeout_seconds": held, "expired": True}
        with _ARRIVED:
            _ARRIVED.wait(min(POLL_SECONDS, remaining))
