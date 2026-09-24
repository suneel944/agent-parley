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

Every observation reaches the record through this module: a supervision
poll's liveness sample through `sample`, and hook events, dialog reads and
session changes through `submit`, which queues them in a spool the next
poll applies in arrival order with `apply`.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import time
from pathlib import Path

from agent_parley.state import BridgeError, lock

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
LIVE = frozenset({STARTING, WORKING, IDLE, BLOCKED})
WAKEABLE = frozenset({IDLE, BLOCKED, STOPPED, DEAD})
HELD_BLOCKS = frozenset({DIALOG, CAPACITY})
HOOK_STATES = {
    "SessionStart": STARTING,
    "UserPromptSubmit": WORKING,
    "PreToolUse": WORKING,
    "PostToolUse": WORKING,
    "PermissionRequest": BLOCKED,
    "Stop": IDLE,
    "SessionEnd": STOPPED,
}
MAX_EVIDENCE = 200
MAX_EVENTS = 2000
SPOOL = "lane-evidence.jsonl"
DRAINING = "lane-evidence.draining.jsonl"
SPOOL_LOCK = "lane-evidence.lock"
SPOOL_WAIT = 2.0
ACCOUNT_GAP = 300.0
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
    "CREATE TABLE IF NOT EXISTS lane_accounts ("
    " project TEXT NOT NULL, lane TEXT NOT NULL, observed REAL NOT NULL,"
    " idle REAL NOT NULL, unaccountable REAL NOT NULL,"
    " idle_causes TEXT NOT NULL, unaccountable_causes TEXT NOT NULL,"
    " state TEXT NOT NULL, has_work INTEGER NOT NULL,"
    " owns INTEGER NOT NULL, accounted REAL NOT NULL,"
    " PRIMARY KEY(project,lane))",
    "CREATE TABLE IF NOT EXISTS lane_wakes ("
    " project TEXT NOT NULL, lane TEXT NOT NULL, at REAL NOT NULL,"
    " attempts INTEGER NOT NULL, backlog TEXT NOT NULL,"
    " superseded INTEGER NOT NULL, result TEXT NOT NULL,"
    " activity TEXT NOT NULL, blocked TEXT NOT NULL, next_at REAL,"
    " exhausted_at REAL, escalated_at REAL, PRIMARY KEY(project,lane))",
)
WAKE_FIELDS = (
    "at",
    "attempts",
    "backlog",
    "superseded",
    "result",
    "activity",
    "blocked",
    "next_at",
    "exhausted_at",
    "escalated_at",
)
WAKE_JSON = frozenset({"backlog", "activity"})
FIELDS = ("state", "cause", "evidence", "session", "since", "updated")
UNUSED = frozenset({IDLE, BLOCKED, STOPPED, DEAD})
ACCOUNT_FIELDS = (
    "observed",
    "idle",
    "unaccountable",
    "idle_causes",
    "unaccountable_causes",
    "state",
    "has_work",
    "owns",
    "accounted",
)


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


def read_wake(db: sqlite3.Connection, root: str, lane: str) -> dict:
    """Reads the wake fields of one lane's state.

    The wake attempts spent on the current backlog, the backoff that spaces
    the next one, the cause a wake is parked on and its escalation are part
    of the lane's state, so they are kept here beside the state record
    rather than in a file only the wake path reads.

    Args:
        db: Open transaction on the coordination store.
        root: Canonical project key.
        lane: Participant that owns the lane.

    Returns:
        The last attempt's time, the attempts counted, the backlog and the
        superseded mail they were counted against, the last result, the
        progress marker, the parked cause, the next attempt time and the
        times the budget was exhausted and escalated; empty when no wake
        was ever recorded.
    """
    try:
        row = db.execute(
            f"SELECT {','.join(WAKE_FIELDS)} FROM lane_wakes "
            "WHERE project=? AND lane=?",
            (root, lane),
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if _absent(exc):
            return {}
        raise
    if row is None:
        return {}
    record = dict(zip(WAKE_FIELDS, row, strict=True))
    for key in WAKE_JSON:
        record[key] = json.loads(record[key])
    return record


def write_wake(
    db: sqlite3.Connection, root: str, lane: str, record: dict
) -> None:
    """Replaces the wake fields of one lane's state.

    Args:
        db: Open write transaction on the coordination store.
        root: Canonical project key.
        lane: Participant that owns the lane.
        record: Wake fields as `read_wake` returns them; a missing field is
            stored as its empty value.
    """
    ensure(db)
    values = (
        float(record.get("at") or 0.0),
        int(record.get("attempts") or 0),
        json.dumps(record.get("backlog") or [], default=str),
        int(record.get("superseded") or 0),
        str(record.get("result") or ""),
        json.dumps(record.get("activity") or {}, sort_keys=True, default=str),
        str(record.get("blocked") or ""),
        record.get("next_at"),
        record.get("exhausted_at"),
        record.get("escalated_at"),
    )
    db.execute(
        f"INSERT INTO lane_wakes(project,lane,{','.join(WAKE_FIELDS)}) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(project,lane) DO UPDATE "
        "SET " + ",".join(f"{key}=excluded.{key}" for key in WAKE_FIELDS),
        (root, lane, *values),
    )


def from_liveness(
    record: dict | None, observed: dict
) -> tuple[str, str] | None:
    """Reads the state one liveness sample is evidence of.

    A sample with no trustworthy process identity moves no lane, unless it
    reads `stopped`: the launcher recorded that its session ended, or the
    lane never published any activity at all, so there is no session that
    could be running. A gone process stops a lane that was live, and a
    running process starts a lane recorded as stopped, dead or reclaimed.
    A lane held by a dialog or an exhausted capacity keeps that block until
    the screen evidence that set it is withdrawn, because the activity label
    a sample is derived from cannot see the screen. An approval label blocks
    however old it is: an approval prompt records no hook until it is
    answered, so its age says nothing about whether it is still open.

    Args:
        record: The lane's current record, or None.
        observed: Presence reading taken by the supervision poll, carrying
            `process_alive`, the derived `activity` and its `evidence`.

    Returns:
        The target state and its cause, or None when the sample moves
        nothing.
    """
    alive = observed.get("process_alive")
    current = "" if record is None else record["state"]
    activity = str(observed.get("activity", ""))
    if alive is None and activity != STOPPED:
        return None
    if alive is not True:
        return None if current in (DEAD, RECLAIMED) else (STOPPED, "")
    if current in (STOPPED, DEAD, RECLAIMED):
        return STARTING, ""
    if record is not None and record["cause"] in HELD_BLOCKS:
        return None
    evidence = str(observed.get("evidence", ""))
    if "approval" in evidence:
        return BLOCKED, APPROVAL
    if activity == "waiting":
        return BLOCKED, PROMPT
    if activity == IDLE:
        return IDLE, ""
    if activity == WORKING:
        return WORKING, ""
    return None


def sample(
    db: sqlite3.Connection,
    root: str,
    lane: str,
    observed: dict,
    *,
    dead_after: float,
    now: float | None = None,
) -> dict | None:
    """Applies one liveness sample and ages a stopped lane into `dead`.

    A lane is dead once it has been stopped for `dead_after` seconds, or once
    the evidence the sample was derived from is that old, so a lane that was
    already long gone when this record was first written is not given a
    second grace period.

    Args:
        db: Open write transaction on the coordination store.
        root: Canonical project key.
        lane: Participant that owns the lane.
        observed: Presence reading taken by the supervision poll.
        dead_after: Seconds a stopped lane waits before it reads as dead.
        now: Unix time of the sample, or None for the current time.

    Returns:
        The lane's record once the sample is applied, or None when the lane
        still has no record.
    """
    moment = time.time() if now is None else now
    record = read(db, root, lane)
    move = from_liveness(record, observed)
    if move is not None:
        state, cause = move
        record = transition(
            db,
            root,
            lane,
            state,
            cause=cause,
            evidence=f"liveness: {observed.get('evidence', '')}",
            now=moment,
        )["record"]
    if record is not None and record["state"] == STOPPED:
        age = observed.get("age_seconds")
        held = moment - float(record["since"])
        if held >= dead_after or (age is not None and age >= dead_after):
            record = transition(
                db,
                root,
                lane,
                DEAD,
                evidence=f"stopped for {int(max(held, age or 0))}s",
                now=moment,
            )["record"]
    return record


def wakes(record: dict | None) -> bool:
    """Reports whether a wake may act on a lane in its recorded state.

    Args:
        record: The lane's current record, or None when none exists yet.

    Returns:
        Whether the lane is idle, stopped, dead or blocked. A blocked lane is
        let through only so the caller can defer it under its named cause
        rather than wake it. A starting, working or reclaimed lane is never
        woken, and a lane with no record is left to the caller's own reading.
    """
    return record is None or record["state"] in WAKEABLE


def hook_evidence(event: str) -> tuple[str, str] | None:
    """Reads the state one native hook event is evidence of.

    Args:
        event: Native hook event name.

    Returns:
        The target state and its cause, or None for an event that says
        nothing about the lane's condition.
    """
    target = HOOK_STATES.get(event)
    if target is None:
        return None
    return target, APPROVAL if target == BLOCKED else ""


def submit(
    directory: Path,
    lane: str,
    source: str,
    target: str,
    *,
    cause: str = "",
    evidence: str = "",
    session: str = "",
    now: float | None = None,
) -> None:
    """Queues one observation of a lane for the state module to apply.

    A hook or a terminal watcher must never wait on the store write lock,
    so evidence is appended to the project's spool instead and the next
    supervision poll applies it, in arrival order, through `transition`.
    Evidence that arrives while the store is busy therefore waits in the
    spool rather than being dropped. The spool lock is held only for one
    append or for the rename that starts a drain.

    Args:
        directory: Private project state directory.
        lane: Participant that owns the lane.
        source: What observed it: a hook event, dialog or session change.
        target: State the observation is evidence of.
        cause: Named cause of a `blocked` target.
        evidence: Short description of what was observed.
        session: Native session identity the observation names, if any.
        now: Unix time of the observation, or None for the current time.
    """
    item = {
        "ts": time.time() if now is None else now,
        "lane": lane,
        "source": source,
        "state": target,
        "cause": cause,
        "evidence": evidence[:MAX_EVIDENCE],
        "session": session,
    }
    with contextlib.suppress(OSError, BridgeError):
        with lock(directory / SPOOL_LOCK, timeout=SPOOL_WAIT):
            with (directory / SPOOL).open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(item) + "\n")


def pending(directory: Path) -> list[dict]:
    """Takes the queued evidence of one project for a drain, oldest first.

    The spool is renamed to a draining file so appends carry on into a new
    spool while this drain runs. A draining file left by a drain the store
    refused is taken again, whole, before any newer spool, so order holds
    across a busy store.

    Args:
        directory: Private project state directory.

    Returns:
        The queued observations; an unreadable line is skipped.
    """
    draining = directory / DRAINING
    with contextlib.suppress(OSError, BridgeError):
        with lock(directory / SPOOL_LOCK, timeout=SPOOL_WAIT):
            if not draining.exists() and (directory / SPOOL).exists():
                (directory / SPOOL).replace(draining)
    items = []
    with contextlib.suppress(OSError):
        for line in draining.read_text(encoding="utf-8").splitlines():
            with contextlib.suppress(ValueError):
                item = json.loads(line)
                if isinstance(item, dict):
                    items.append(item)
    return items


def settled(directory: Path) -> None:
    """Discards the draining file once its evidence is committed.

    Args:
        directory: Private project state directory.
    """
    with contextlib.suppress(OSError):
        (directory / DRAINING).unlink(missing_ok=True)


def apply(db: sqlite3.Connection, root: str, items: list[dict]) -> int:
    """Applies queued evidence to the lane records in arrival order.

    A hook seen from a lane recorded as dead or reclaimed proves a running
    session, so the lane is started before the observed state is applied.
    A new session identity is passed through, so a session change becomes
    a transition that records both identities rather than a dropped hook.
    An observation older than the record's last update, because it waited
    in the spool while the store was busy, is applied at that update time
    so a record's times never run backwards.

    Args:
        db: Open write transaction on the coordination store.
        root: Canonical project key.
        items: Observations taken by `pending`.

    Returns:
        How many observations changed a lane's record.
    """
    changed = 0
    for item in items:
        lane = str(item.get("lane", ""))
        target = str(item.get("state", ""))
        cause = str(item.get("cause", ""))
        if not lane or target not in STATES:
            continue
        if (target == BLOCKED) != (cause in CAUSES):
            continue
        evidence = f"{item.get('source', '')}: {item.get('evidence', '')}"
        session = str(item.get("session", "")) or None
        current = read(db, root, lane)
        moment = float(item.get("ts") or time.time())
        if current is not None:
            moment = max(moment, float(current["updated"]))
        if (
            current is not None
            and current["state"] in (DEAD, RECLAIMED)
            and target in LIVE - {STARTING}
        ):
            transition(db, root, lane, STARTING, evidence=evidence, now=moment)
        result = transition(
            db,
            root,
            lane,
            target,
            cause=cause,
            evidence=evidence,
            session=session,
            now=moment,
        )
        changed += int(result["changed"])
    return changed


def label(record: dict) -> str:
    """Names a lane's state with its cause, as accounting keys it.

    Args:
        record: The lane's state record.

    Returns:
        `blocked: capacity` for a blocked lane, otherwise the state alone.
    """
    if record["cause"]:
        return f"{record['state']}: {record['cause']}"
    return str(record["state"])


def read_accounts(db: sqlite3.Connection, root: str) -> dict[str, dict]:
    """Reads the idle and accountability totals of every lane of a project.

    Args:
        db: Open transaction on the coordination store.
        root: Canonical project key.

    Returns:
        Each lane's totals keyed by the participant that owns it.
    """
    try:
        rows = db.execute(
            f"SELECT lane,{','.join(ACCOUNT_FIELDS)} FROM lane_accounts "
            "WHERE project=?",
            (root,),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if _absent(exc):
            return {}
        raise
    accounts = {}
    for row in rows:
        values = dict(zip(ACCOUNT_FIELDS, row[1:], strict=True))
        for key in ("idle_causes", "unaccountable_causes"):
            values[key] = json.loads(values[key])
        values["has_work"] = bool(values["has_work"])
        values["owns"] = bool(values["owns"])
        accounts[row[0]] = values
    return accounts


def account(
    db: sqlite3.Connection,
    root: str,
    lane: str,
    record: dict | None,
    *,
    has_work: bool,
    owns: bool,
    now: float | None = None,
) -> dict | None:
    """Adds the time since the last poll to a lane's idle and claim totals.

    Idle time is time the lane spent `idle`, `blocked`, `stopped` or `dead`
    while claimable or owned work existed. Unaccountable time is time the
    lane owned a claim while it was not `working`. Each total is kept per
    state and cause, so the largest one can be named. The span since the
    last poll is split where the lane last changed state: the part before
    is charged to the state and work the last poll saw, the rest to the
    current ones. A span longer than `ACCOUNT_GAP`, a stopped service,
    is not charged to anything. An `accounting` event is appended each
    time either total crosses a whole minute.

    Args:
        db: Open write transaction on the coordination store.
        root: Canonical project key.
        lane: Participant that owns the lane.
        record: The lane's current state record, or None.
        has_work: Whether claimable or owned work exists for the lane.
        owns: Whether the lane owns an open claim.
        now: Unix time of the poll, or None for the current time.

    Returns:
        The lane's totals, or None when the lane has no state record.
    """
    if record is None:
        return None
    moment = time.time() if now is None else now
    ensure(db)
    previous = read_accounts(db, root).get(lane)
    current = label(record)
    totals: dict = {
        "observed": 0.0,
        "idle": 0.0,
        "unaccountable": 0.0,
        "idle_causes": {},
        "unaccountable_causes": {},
    }
    if previous is not None:
        totals = {
            **{key: previous[key] for key in totals},
            "idle_causes": dict(previous["idle_causes"]),
            "unaccountable_causes": dict(previous["unaccountable_causes"]),
        }
        start = float(previous["accounted"])
        cut = min(max(float(record["since"]), start), moment)
        pieces = (
            (
                cut - start,
                previous["state"],
                previous["has_work"],
                previous["owns"],
            ),
            (moment - cut, current, has_work, owns),
        )
        if moment - start <= ACCOUNT_GAP:
            for seconds, key, work, owned in pieces:
                _charge(totals, seconds, key, work, owned)
    db.execute(
        "INSERT INTO lane_accounts(project,lane,observed,idle,unaccountable,"
        "idle_causes,unaccountable_causes,state,has_work,owns,accounted) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(project,lane) DO UPDATE "
        "SET observed=excluded.observed,idle=excluded.idle,"
        "unaccountable=excluded.unaccountable,"
        "idle_causes=excluded.idle_causes,"
        "unaccountable_causes=excluded.unaccountable_causes,"
        "state=excluded.state,has_work=excluded.has_work,"
        "owns=excluded.owns,accounted=excluded.accounted",
        (
            root,
            lane,
            totals["observed"],
            totals["idle"],
            totals["unaccountable"],
            json.dumps(totals["idle_causes"], sort_keys=True),
            json.dumps(totals["unaccountable_causes"], sort_keys=True),
            current,
            int(has_work),
            int(owns),
            moment,
        ),
    )
    minutes = (int(totals["idle"] // 60), int(totals["unaccountable"] // 60))
    if previous is not None and minutes != (
        int(previous["idle"] // 60),
        int(previous["unaccountable"] // 60),
    ):
        report = summary(totals)
        _event(
            db,
            root,
            lane,
            "accounting",
            previous["state"],
            current,
            evidence=describe_account(report),
            detail=json.dumps(report, sort_keys=True),
            now=moment,
        )
    return totals


def _charge(
    totals: dict, seconds: float, key: str, work: bool, owned: bool
) -> None:
    """Charges one span spent in one state to a lane's totals."""
    if seconds <= 0:
        return
    totals["observed"] += seconds
    state = key.split(":", 1)[0]
    if work and state in UNUSED:
        totals["idle"] += seconds
        causes = totals["idle_causes"]
        causes[key] = causes.get(key, 0.0) + seconds
    if owned and state != WORKING:
        totals["unaccountable"] += seconds
        causes = totals["unaccountable_causes"]
        causes[key] = causes.get(key, 0.0) + seconds


def _top(causes: dict[str, float]) -> str:
    """Names the largest cause with its minutes, or nothing."""
    if not causes:
        return ""
    key = max(sorted(causes), key=lambda name: causes[name])
    return f"{key}, {round(causes[key] / 60)} min"


def summary(totals: dict) -> dict:
    """Reports idle lane-minutes per lane-hour and unaccountable minutes.

    Args:
        totals: One lane's totals, or several lanes' merged by `combine`.

    Returns:
        Observed, idle and unaccountable minutes, idle minutes per observed
        lane-hour, and the top cause of each total.
    """
    observed = float(totals["observed"])
    idle = float(totals["idle"]) / 60
    return {
        "observed_minutes": round(observed / 60, 1),
        "idle_minutes": round(idle, 1),
        "idle_per_lane_hour": round(idle / (observed / 3600), 1)
        if observed
        else 0.0,
        "idle_cause": _top(totals["idle_causes"]),
        "unaccountable_minutes": round(float(totals["unaccountable"]) / 60, 1),
        "unaccountable_cause": _top(totals["unaccountable_causes"]),
    }


def combine(accounts: dict[str, dict]) -> dict:
    """Merges every lane's totals into the project's, keyed per lane.

    Args:
        accounts: Each lane's totals keyed by participant.

    Returns:
        Totals shaped like one lane's, whose causes name their lane.
    """
    merged: dict = {
        "observed": 0.0,
        "idle": 0.0,
        "unaccountable": 0.0,
        "idle_causes": {},
        "unaccountable_causes": {},
    }
    for lane, totals in sorted(accounts.items()):
        for key in ("observed", "idle", "unaccountable"):
            merged[key] += float(totals[key])
        for key in ("idle_causes", "unaccountable_causes"):
            for cause, seconds in totals[key].items():
                merged[key][f"{lane} {cause}"] = seconds
    return merged


def describe_account(report: dict) -> str:
    """Renders a `summary` as one status line.

    Args:
        report: Result of `summary`.

    Returns:
        Idle minutes per lane-hour and unaccountable claim-minutes, each
        with its top cause when it has one.
    """
    idle = f"idle {report['idle_per_lane_hour']} min/lane-hour"
    if report["idle_cause"]:
        idle += f" (top: {report['idle_cause']})"
    claims = f"unaccountable claims {report['unaccountable_minutes']} min"
    if report["unaccountable_cause"]:
        claims += f" (top: {report['unaccountable_cause']})"
    return f"{idle}; {claims}"


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
