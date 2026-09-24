"""Owns the one authoritative state record of every lane.

A lane's condition used to be inferred separately by every decision from
whichever of its files that decision happened to read: the activity label,
the wake and work records, the presence row and the claim fields. Those
records are written independently, so the same lane could read as idle to
one decision and as working to another in the same poll, and a fix to one
signal left the next decision reading a different one.

This module keeps one record per lane in the coordination store, with a
closed set of states and a closed table of legal transitions. Every change
goes through `transition`, which validates the source state, writes the new
state with its cause and its evidence in the same transaction, and appends
one event. A transition the table does not allow is refused and recorded as
refused, never applied. The per-lane files stay as evidence and caches.

The tables are created by this module the first time it writes, inside the
writing transaction, so an existing store needs no separate upgrade step
and a reader of a store that has never held a lane state sees no record.
"""

from __future__ import annotations

import sqlite3
import time

STARTING = "starting"
WORKING = "working"
IDLE = "idle"
BLOCKED = "blocked"
STOPPED = "stopped"
DEAD = "dead"
RECLAIMED = "reclaimed"
STATES = (STARTING, WORKING, IDLE, BLOCKED, STOPPED, DEAD, RECLAIMED)
DIALOG = "dialog"
CAPACITY = "capacity"
PROMPT = "prompt"
APPROVAL = "approval"
CAUSES = frozenset({DIALOG, CAPACITY, PROMPT, APPROVAL})
TRANSITIONS: dict[str, frozenset[str]] = {
    "": frozenset({STARTING, WORKING, IDLE, BLOCKED, STOPPED, DEAD}),
    STARTING: frozenset({STARTING, WORKING, IDLE, BLOCKED, STOPPED, DEAD}),
    WORKING: frozenset({STARTING, WORKING, IDLE, BLOCKED, STOPPED, DEAD}),
    IDLE: frozenset({STARTING, WORKING, IDLE, BLOCKED, STOPPED, DEAD}),
    BLOCKED: frozenset({STARTING, WORKING, IDLE, BLOCKED, STOPPED, DEAD}),
    STOPPED: frozenset(
        {STARTING, WORKING, IDLE, BLOCKED, STOPPED, DEAD, RECLAIMED}
    ),
    DEAD: frozenset({STARTING, DEAD, RECLAIMED}),
    RECLAIMED: frozenset({STARTING, RECLAIMED}),
}
MAX_EVIDENCE = 200
MAX_EVENTS = 2000
SCHEMA = (
    "CREATE TABLE IF NOT EXISTS lane_states ("
    " project TEXT NOT NULL, lane TEXT NOT NULL, state TEXT NOT NULL,"
    " cause TEXT NOT NULL DEFAULT '', evidence TEXT NOT NULL DEFAULT '',"
    " session TEXT NOT NULL DEFAULT '', since REAL NOT NULL,"
    " updated REAL NOT NULL, PRIMARY KEY(project,lane))",
    "CREATE TABLE IF NOT EXISTS lane_events ("
    " id INTEGER PRIMARY KEY, project TEXT NOT NULL, lane TEXT NOT NULL,"
    " kind TEXT NOT NULL, source TEXT NOT NULL, target TEXT NOT NULL,"
    " cause TEXT NOT NULL DEFAULT '', evidence TEXT NOT NULL DEFAULT '',"
    " detail TEXT NOT NULL DEFAULT '', ts REAL NOT NULL)",
    "CREATE INDEX IF NOT EXISTS lane_history ON lane_events(project,lane,id)",
)
FIELDS = ("state", "cause", "evidence", "session", "since", "updated")


def ensure(db: sqlite3.Connection) -> None:
    """Creates the lane state tables inside the caller's write transaction.

    Args:
        db: Open write transaction on the coordination store.
    """
    for statement in SCHEMA:
        db.execute(statement)


def _absent(exc: sqlite3.OperationalError) -> bool:
    """Reports whether a read failed only because no lane state exists yet."""
    return "no such table" in str(exc)


def read(db: sqlite3.Connection, root: str, lane: str) -> dict | None:
    """Reads one lane's state record.

    Args:
        db: Open transaction on the coordination store.
        root: Canonical project key.
        lane: Participant that owns the lane.

    Returns:
        The record's state, cause, evidence, session, the time the state was
        entered and the time it was last confirmed, or None when the lane has
        no record.
    """
    try:
        row = db.execute(
            f"SELECT {','.join(FIELDS)} FROM lane_states "
            "WHERE project=? AND lane=?",
            (root, lane),
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if _absent(exc):
            return None
        raise
    return None if row is None else dict(zip(FIELDS, row, strict=True))


def read_all(db: sqlite3.Connection, root: str) -> dict[str, dict]:
    """Reads every lane state record of one project.

    Args:
        db: Open transaction on the coordination store.
        root: Canonical project key.

    Returns:
        Each lane's record keyed by the participant that owns it.
    """
    try:
        rows = db.execute(
            f"SELECT lane,{','.join(FIELDS)} FROM lane_states WHERE project=?",
            (root,),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if _absent(exc):
            return {}
        raise
    return {row[0]: dict(zip(FIELDS, row[1:], strict=True)) for row in rows}


def _event(
    db: sqlite3.Connection,
    root: str,
    lane: str,
    kind: str,
    source: str,
    target: str,
    *,
    cause: str = "",
    evidence: str = "",
    detail: str = "",
    now: float,
) -> None:
    """Appends one lane event and bounds the project's retained history."""
    db.execute(
        "INSERT INTO lane_events(project,lane,kind,source,target,cause,"
        "evidence,detail,ts) VALUES (?,?,?,?,?,?,?,?,?)",
        (root, lane, kind, source, target, cause, evidence, detail, now),
    )
    db.execute(
        "DELETE FROM lane_events WHERE project=? AND id<=(SELECT id FROM "
        "lane_events WHERE project=? ORDER BY id DESC LIMIT 1 OFFSET ?)",
        (root, root, MAX_EVENTS),
    )


def transition(
    db: sqlite3.Connection,
    root: str,
    lane: str,
    target: str,
    *,
    cause: str = "",
    evidence: str = "",
    session: str | None = None,
    now: float | None = None,
) -> dict:
    """Moves one lane to a new state if the transition table allows it.

    A `blocked` state must name its cause, and no other state carries one.
    Confirming the state, cause and session the lane already has changes
    nothing and appends nothing, so a repeated observation does not flood
    the history. A new session identity on an unchanged state is still a
    transition, recorded with both identities, because a lane that changed
    session is the same lane and the change must not be lost.

    Args:
        db: Open write transaction on the coordination store.
        root: Canonical project key.
        lane: Participant that owns the lane.
        target: State the lane is moving to.
        cause: Named cause of a `blocked` state.
        evidence: Short description of the observation that caused the
            move, bounded to `MAX_EVIDENCE` characters.
        session: Native session identity the observation names, or None to
            keep the recorded one.
        now: Unix time of the observation, or None for the current time.

    Returns:
        Whether the transition was accepted, whether it changed the record,
        the state it left and the record as it now stands.

    Raises:
        ValueError: If the target state or blocked cause is not one this
            module defines.
    """
    if target not in STATES:
        raise ValueError(f"Unknown lane state {target!r}.")
    if (target == BLOCKED) != (cause in CAUSES):
        raise ValueError(f"A {target} lane cannot carry cause {cause!r}.")
    moment = time.time() if now is None else now
    ensure(db)
    current = read(db, root, lane)
    source = "" if current is None else current["state"]
    recorded = "" if current is None else current["session"]
    kept = recorded if session is None or not session else session
    evidence = evidence[:MAX_EVIDENCE]
    if target not in TRANSITIONS[source]:
        _event(
            db,
            root,
            lane,
            "refused",
            source,
            target,
            cause=cause,
            evidence=evidence,
            now=moment,
        )
        return {
            "accepted": False,
            "changed": False,
            "source": source,
            "record": current,
        }
    if (
        current is not None
        and source == target
        and current["cause"] == cause
        and kept == recorded
    ):
        db.execute(
            "UPDATE lane_states SET updated=? WHERE project=? AND lane=?",
            (moment, root, lane),
        )
        return {
            "accepted": True,
            "changed": False,
            "source": source,
            "record": {**current, "updated": moment},
        }
    since = moment if current is None or source != target else current["since"]
    record = {
        "state": target,
        "cause": cause,
        "evidence": evidence,
        "session": kept,
        "since": since,
        "updated": moment,
    }
    db.execute(
        "INSERT INTO lane_states(project,lane,state,cause,evidence,session,"
        "since,updated) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(project,lane) "
        "DO UPDATE SET state=excluded.state,cause=excluded.cause,"
        "evidence=excluded.evidence,session=excluded.session,"
        "since=excluded.since,updated=excluded.updated",
        (root, lane, target, cause, evidence, kept, since, moment),
    )
    _event(
        db,
        root,
        lane,
        "transition",
        source,
        target,
        cause=cause,
        evidence=evidence,
        detail=f"session {recorded} -> {kept}" if kept != recorded else "",
        now=moment,
    )
    return {
        "accepted": True,
        "changed": True,
        "source": source,
        "record": record,
    }


def history(
    db: sqlite3.Connection, root: str, lane: str = "", kind: str = ""
) -> list[dict]:
    """Reads the retained lane events of one project, oldest first.

    Args:
        db: Open transaction on the coordination store.
        root: Canonical project key.
        lane: Only this lane's events, or every lane's when empty.
        kind: Only events of this kind, or every kind when empty.

    Returns:
        One mapping per event with its lane, kind, source and target states,
        cause, evidence, detail and time.
    """
    names = (
        "lane",
        "kind",
        "source",
        "target",
        "cause",
        "evidence",
        "detail",
        "ts",
    )
    try:
        rows = db.execute(
            f"SELECT {','.join(names)} FROM lane_events WHERE project=? "
            "AND (?='' OR lane=?) AND (?='' OR kind=?) ORDER BY id",
            (root, lane, lane, kind, kind),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if _absent(exc):
            return []
        raise
    return [dict(zip(names, row, strict=True)) for row in rows]


def view(record: dict | None, now: float | None = None) -> dict | None:
    """Shapes a lane state record for the machine-readable status document.

    Args:
        record: Lane state record as `read` returns it, or None.
        now: Unix time the span is measured to, or None for the current
            time.

    Returns:
        The state, its cause and evidence, the Unix time it was entered and
        the whole seconds it has been held, or None when the lane has no
        record yet.
    """
    if record is None:
        return None
    moment = time.time() if now is None else now
    return {
        "state": record["state"],
        "cause": record["cause"],
        "evidence": record["evidence"],
        "since": record["since"],
        "seconds": int(max(0.0, moment - float(record["since"]))),
    }


def describe(record: dict, now: float | None = None) -> str:
    """Names a lane's state, its cause and how long it has held it.

    Args:
        record: Lane state record as `read` returns it.
        now: Unix time the span is measured to, or None for the current
            time.

    Returns:
        Text such as ``blocked: approval 3m`` for the operator views.
    """
    moment = time.time() if now is None else now
    span = max(0.0, moment - float(record["since"]))
    held = (
        f"{int(span)}s"
        if span < 90
        else f"{int(span / 60)}m"
        if span < 5400
        else f"{int(span / 3600)}h"
    )
    named = (
        f"{record['state']}: {record['cause']}"
        if record["cause"]
        else (record["state"])
    )
    return f"{named} {held}"
