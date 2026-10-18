"""Holds a lane's turn open for its next mail, or hands a long wait over.

A short wait is held open inside the service until mail arrives or a bound
expires. A long wait is handed to the service instead: the lane records what
it waits for and ends its turn, and the supervisor's ordinary wake path gives
it one turn when that arrives. The record lives in the coordination store, so
`status` reads what a lane waits for and since when.
"""

import sqlite3
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
HAND_OFF = ("mail", "handoff", "check")
SERVICE = "service"
POLLING = "polling"
SCHEMA = (
    "CREATE TABLE IF NOT EXISTS lane_waits ("
    " project TEXT NOT NULL, lane TEXT NOT NULL, kind TEXT NOT NULL,"
    " thread_id TEXT NOT NULL DEFAULT '', since REAL NOT NULL,"
    " wake TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',"
    " woken_at REAL, PRIMARY KEY(project,lane))"
)
FIELDS = ("kind", "thread_id", "since", "wake", "reason", "woken_at")

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


def ensure(db: sqlite3.Connection) -> None:
    """Creates the handed-off wait table inside the caller's transaction.

    Args:
        db: Open write transaction on the coordination store.
    """
    db.execute(SCHEMA)


def wake_path(home: Path, root: str, display: str) -> str:
    """Names why the service could not wake this lane, if anything.

    A handed-off wait is only safe when the supervisor would give the lane
    its turn back: the wake loop is on for the project and for the lane, and
    the lane's provider is one the supervisor can resume once its process
    has ended. Every other lane keeps the bounded wait and is marked polling.

    Args:
        home: Private bridge state root.
        root: Canonical project key the lane authenticated under.
        display: Registered identity of the lane.

    Returns:
        An empty string when the service wakes this lane, else the reason it
        cannot, which `status` shows beside the polling wait.
    """
    from agent_parley import supervision

    directory = roster.locate(home, root)
    if directory is None:
        return "no project manifest names this lane"
    try:
        manifest = roster.read(directory)
    except (BridgeError, OSError, ValueError):
        return "the project manifest is unreadable"
    participant = next(
        (
            entry
            for entry in manifest["participants"].values()
            if entry.get("display") == display
        ),
        None,
    )
    if participant is None:
        return "no participant in the manifest has this identity"
    if not supervision.configuration(home, manifest)["wake"]:
        return "the wake loop is off for this project"
    if not participant.get("wake", True):
        return "wake is off for this lane"
    try:
        entry = roster.provider(home, str(participant.get("provider", "")))
    except BridgeError:
        return "its provider is not defined"
    if entry.get("adapter") not in roster.ADAPTERS:
        return "its provider has no resume path"
    if entry.get("require_env"):
        return "its provider needs environment the service cannot supply"
    return ""


def pending(db: sqlite3.Connection, root: str, display: str) -> dict | None:
    """Reads one lane's handed-off wait.

    Args:
        db: Open transaction on the coordination store.
        root: Canonical project key.
        display: Registered identity of the lane.

    Returns:
        What the lane waits for as `kind`, the `thread_id` it named, `since`
        as a Unix time and `age_seconds`, `wake` as `service` or `polling`
        with the `reason` a polling lane cannot be woken, and `woken_at`
        once the service asked it for its turn; None when nothing is handed
        off.
    """
    try:
        row = db.execute(
            f"SELECT {','.join(FIELDS)} FROM lane_waits "
            "WHERE project=? AND lane=?",
            (root, display),
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return None
        raise
    if row is None:
        return None
    record = dict(zip(FIELDS, tuple(row), strict=True))
    record["age_seconds"] = max(0, int(time.time() - float(record["since"])))
    return record


def hand_off(
    home: Path,
    actor: dict,
    args: dict,
    stopping: threading.Event | None = None,
    ceiling: float = MAX_SECONDS,
) -> dict:
    """Hands a long wait to the service so the lane can end its turn.

    The inbox is read once with the same filters first, so mail that has
    already arrived is returned at once and no wait is recorded. Otherwise
    the wait is recorded with what it is for and since when, and a repeated
    hand-off of the same wait keeps its first time until the lane is woken.
    A lane the service can wake is told to end its turn: the supervisor's
    existing wake path asks it for one turn once the mail, the handoff offer
    or the pull request notice it waits for is in its backlog, so a wait of
    any length costs one turn to start and one to resume. A lane the service
    cannot wake is marked polling with the reason and held the ordinary
    bounded wait, so it keeps calling as before. The lane's next served call
    of any other kind ends the record.

    Args:
        home: Private bridge state root.
        actor: Authenticated project and lane, whose own inbox is read.
        args: Inbox filters, `hand_off` naming what the lane waits for, one
            of `HAND_OFF`, and an optional `timeout_seconds`, which bounds
            only a polling lane's wait.
        stopping: Event a stopping service sets, or None when the caller has
            no lifecycle to observe.
        ceiling: Longest wait this service serves at this moment.

    Returns:
        The inbox page, `hand_off` holding the recorded wait as `pending`
        reads it, or None when matching mail was already there, and
        `end_turn`, true exactly when the lane should end its turn and leave
        the wake to the service.

    Raises:
        BridgeError: If `hand_off` names nothing a lane can wait for, a filter
            or the timeout is invalid, or the operator paused this lane.
    """
    kind = args.get("hand_off")
    if kind not in HAND_OFF:
        raise BridgeError(f"hand_off must be one of {', '.join(HAND_OFF)}.")
    filters = {
        name: value for name, value in args.items() if name != "hand_off"
    }
    bounded(filters.get("timeout_seconds"), ceiling)
    page = wait(home, actor, {**filters, "timeout_seconds": 0}, stopping, 0.0)
    if page["messages"]:
        return {**page, "hand_off": None, "end_turn": False}
    root = str(actor.get("project", ""))
    reason = wake_path(home, root, actor["name"])
    with store.connect(home, write=True) as db:
        ensure(db)
        db.execute(
            "INSERT INTO lane_waits(project,lane,kind,thread_id,since,wake,"
            "reason,woken_at) VALUES(?,?,?,?,?,?,?,NULL) "
            "ON CONFLICT(project,lane) DO UPDATE SET since=CASE WHEN "
            "kind=excluded.kind AND thread_id=excluded.thread_id AND "
            "woken_at IS NULL THEN since ELSE excluded.since END,"
            "kind=excluded.kind,thread_id=excluded.thread_id,"
            "wake=excluded.wake,reason=excluded.reason,woken_at=NULL",
            (
                root,
                actor["name"],
                kind,
                str(filters.get("thread_id") or ""),
                time.time(),
                POLLING if reason else SERVICE,
                reason,
            ),
        )
        recorded = pending(db, root, actor["name"])
    if reason:
        page = wait(home, actor, filters, stopping, ceiling)
    return {**page, "hand_off": recorded, "end_turn": not reason}


def resumed(home: Path, actor: dict) -> None:
    """Ends a lane's handed-off wait because it is taking a turn again.

    Any served call other than another hand-off means the lane holds a
    turn, whether the service woke it, an operator prompted it or it never
    ended its turn, so the record no longer reports it as waiting. The read
    comes first, so a lane with nothing handed off costs no write.

    Args:
        home: Private bridge state root.
        actor: Authenticated project and lane making the call.
    """
    root = str(actor.get("project", ""))
    with store.connect(home) as db:
        if pending(db, root, actor["name"]) is None:
            return
    with store.connect(home, write=True) as db:
        db.execute(
            "DELETE FROM lane_waits WHERE project=? AND lane=?",
            (root, actor["name"]),
        )


def woke(home: Path, root: str, display: str, at: float) -> bool:
    """Records that the service asked a waiting lane for its turn.

    Args:
        home: Private bridge state root.
        root: Canonical project key.
        display: Registered identity of the lane.
        at: Unix time the wake was accepted or the resume was started.

    Returns:
        Whether a handed-off wait the service owns was marked woken; a lane
        with no such wait, or one already woken, changes nothing.
    """
    with store.connect(home) as db:
        record = pending(db, root, display)
    if not record or record["wake"] != SERVICE or record["woken_at"]:
        return False
    with store.connect(home, write=True) as db:
        return (
            db.execute(
                "UPDATE lane_waits SET woken_at=? WHERE project=? AND lane=? "
                "AND wake=? AND woken_at IS NULL",
                (at, root, display, SERVICE),
            ).rowcount
            == 1
        )
