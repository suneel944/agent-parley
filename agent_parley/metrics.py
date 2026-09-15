"""Measures lane idleness and waiting from records the runtime already keeps.

Agent Parley exists to keep several lanes working at once, and until now it
never measured how much of that time was actually spent working. These readings
answer two questions with numbers rather than impressions: how long a lane went
without coordination activity, and how long each pending item waited before
somebody answered it.

Every figure is derived from coordination state: the per-lane hook event log,
the served-call events in the store, the mail timestamps, and the issue ledger.
Nothing here asks a vendor, starts a probe or infers what a native client was
doing inside a turn. An idle figure therefore states observed coordination
inactivity, not native work time, and a reading whose window reaches past what
retention kept is reported as incomplete rather than quietly counted as zero.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path

from agent_parley import attachments, checkpoints, issues, process, store
from agent_parley.state import (
    MAX_LOG_BYTES,
    MAX_LOG_RECORDS,
    BridgeError,
    lock,
    trim_log,
)

TURN_END = frozenset({"Stop", "SessionEnd"})
REPORTS = "reports.jsonl"
MAX_REPORT_RECORDS = MAX_LOG_RECORDS
MAX_REPORT_LOG_BYTES = MAX_LOG_BYTES
MAX_REPORT_BYTES = 4096
MAX_WAITS = 64


def report_path(directory: Path, name: str) -> Path:
    """Returns the durable report, decision and integration log for one lane.

    Args:
        directory: Private state directory for the common repository.
        name: Participant that owns the lane.

    Returns:
        The append-only log path, which lives in coordination state rather
        than in the target repository.
    """
    return directory / f"{name}-{REPORTS}"


def record_report(directory: Path, name: str, entry: dict) -> dict:
    """Appends one durable report or integration record for a lane.

    A lane's activity file keeps only the latest report, so the seconds
    between reporting ready and being integrated could not be recovered from
    it. This log keeps each report and each integration as its own record, so
    the interval between them is a subtraction rather than a guess. Recording
    is best effort: a failed append never fails the report or the merge it
    describes.

    The log is bounded the way every line log the runtime keeps is bounded.
    Once it passes `MAX_REPORT_LOG_BYTES` it is rewritten in place with only
    the newest `MAX_REPORT_RECORDS` records, which is every record a reader
    would return anyway, so a lane that reports on every turn never leaves
    a file that grows for the life of the project. The append and the
    rewrite share one short lock so a concurrent append is never dropped by
    the rewrite; when that lock is busy the record is appended without it
    and the rewrite waits for a later, uncontended append.

    Args:
        directory: Private state directory for the common repository.
        name: Participant that owns the lane.
        entry: Record fields, without its identifier or timestamp.

    Returns:
        The appended record, including the identifier and the time it was
        recorded.
    """
    record = {"id": uuid.uuid4().hex[:16], "at": time.time(), **entry}
    path = report_path(directory, name)
    line = json.dumps(record) + "\n"
    try:
        try:
            with lock(directory / f"{name}-reports.lock", timeout=0.2):
                with path.open("a", encoding="utf-8") as stream:
                    stream.write(line)
                for dropped in trim_log(
                    path, MAX_REPORT_LOG_BYTES, MAX_REPORT_RECORDS
                ):
                    _drop_attachment(directory, dropped)
        except BridgeError:
            with path.open("a", encoding="utf-8") as stream:
                stream.write(line)
    except OSError:
        return record
    return record


def _drop_attachment(directory: Path, line: str) -> None:
    """Removes the attachment of one report record the rotation dropped.

    Args:
        directory: Private state directory for the common repository.
        line: Serialized record leaving the log; a damaged line is ignored.
    """
    try:
        dropped = json.loads(line)
    except ValueError:
        return
    if isinstance(dropped, dict) and dropped.get("attachment"):
        attachments.remove(directory, str(dropped["attachment"]))


def report_records(
    directory: Path, name: str, since: float = 0.0
) -> list[dict]:
    """Reads one lane's durable report and integration records.

    Args:
        directory: Private state directory for the common repository.
        name: Participant that owns the lane.
        since: Unix time floor; older records are omitted. Zero reads
            everything retained.

    Returns:
        Records oldest first. A damaged line is skipped rather than failing a
        report, and the most recent records are kept when the log is long.
    """
    path = report_path(directory, name)
    if not path.exists():
        return []
    records = []
    try:
        text = path.read_text(errors="ignore")
    except OSError:
        return []
    for line in text.splitlines()[-MAX_REPORT_RECORDS:]:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if (
            isinstance(record, dict)
            and float(record.get("at", 0) or 0) >= since
        ):
            records.append(record)
    return records


def idle_intervals(
    directory: Path, name: str, since: float = 0.0, now: float = 0.0
) -> dict:
    """Derives the intervals one lane spent without coordination activity.

    An interval opens when a turn ends, which the native client reports as a
    ``Stop`` or ``SessionEnd`` checkpoint, and closes at the lane's next
    recorded activity. The interval still open at the time of the reading is
    reported as open, and only counted while the recorded session process is
    alive, because a stopped lane is not idle: it is stopped.

    Args:
        directory: Private state directory for the common repository.
        name: Participant that owns the lane.
        since: Unix time floor for the window. Zero covers everything
            retained.
        now: Instant the window ends at; the current time when zero.

    Returns:
        The intervals, their total in seconds, and whether the reading covers
        the whole window. A window that starts before the oldest retained
        record is incomplete, and the total then understates the truth rather
        than pretending to be exact.
    """
    ends = now or time.time()
    try:
        entries = checkpoints.read_events(directory, name, since)
    except BridgeError:
        return {"intervals": [], "seconds": 0, "complete": False}
    stamps = sorted(
        (float(entry.get("ts", 0) or 0), str(entry.get("event", "")))
        for entry in entries
    )
    intervals: list[dict] = []
    opened: float | None = None
    for stamp, event in stamps:
        if opened is not None and stamp > opened:
            intervals.append(
                {"start": opened, "end": stamp, "seconds": int(stamp - opened)}
            )
            opened = None
        if event in TURN_END:
            opened = stamp
    if opened is not None:
        state = checkpoints.activity(directory, name)
        if (
            process.alive(state.get("session_pid"), state.get("session_ticks"))
            and ends > opened
        ):
            intervals.append(
                {
                    "start": opened,
                    "end": ends,
                    "seconds": int(ends - opened),
                    "open": True,
                }
            )
    oldest = stamps[0][0] if stamps else ends
    return {
        "intervals": intervals,
        "seconds": sum(interval["seconds"] for interval in intervals),
        "complete": bool(stamps) and (since == 0.0 or oldest <= since + 1),
    }


def _mail_waits(
    home: Path,
    root: str,
    display: str,
    since: float,
    db: sqlite3.Connection | None = None,
) -> list[dict]:
    """Reports how long each message waited to be read and acknowledged."""
    if db is None and not (home / store.DATABASE).exists():
        return []
    waits: list[dict] = []
    with store.reading(home, db) as db:
        agent = db.execute(
            "SELECT a.id FROM agents a JOIN projects p ON p.id=a.project_id "
            "WHERE p.human_key=? AND a.name=?",
            (root, display),
        ).fetchone()
        if not agent:
            return []
        rows = db.execute(
            "SELECT m.id,s.name AS sender,m.ack_required,"
            "unixepoch(m.created_ts) AS created,"
            "unixepoch(r.read_ts) AS read_at,unixepoch(r.ack_ts) AS ack_at "
            "FROM message_recipients r JOIN messages m ON m.id=r.message_id "
            "JOIN agents s ON s.id=m.sender_id "
            "WHERE r.agent_id=? AND unixepoch(m.created_ts)>=? "
            "ORDER BY m.id DESC LIMIT ?",
            (agent["id"], int(since), MAX_WAITS),
        ).fetchall()
    stamp = time.time()
    for row in rows:
        waits.append(
            {
                "kind": "message_read",
                "message_id": row["id"],
                "sender": row["sender"],
                "seconds": int((row["read_at"] or stamp) - row["created"]),
                "complete": row["read_at"] is not None,
            }
        )
        if row["ack_required"]:
            waits.append(
                {
                    "kind": "acknowledgement",
                    "message_id": row["id"],
                    "sender": row["sender"],
                    "seconds": int((row["ack_at"] or stamp) - row["created"]),
                    "complete": row["ack_at"] is not None,
                }
            )
    return waits


def _offer_waits(
    directory: Path, name: str, since: float, ledger: dict | None = None
) -> list[dict]:
    """Reports how long each handoff offer waited to be answered."""
    waits = []
    stamp = time.time()
    if ledger is None:
        ledger = issues.snapshot(directory)
    for number, record in ledger["issues"].items():
        opened = 0.0
        for entry in record.get("history", []):
            at = float(entry.get("at", 0) or 0)
            if entry.get("action") == "offer" and entry.get("actor") == name:
                opened = at
            elif entry.get("action") in ("accept", "decline") and opened:
                if at >= since:
                    waits.append(
                        {
                            "kind": "handoff_offer",
                            "issue": int(number),
                            "answer": entry["action"],
                            "seconds": int(at - opened),
                            "complete": True,
                        }
                    )
                opened = 0.0
        offer = record.get("offer")
        if offer and record.get("owner") == name:
            waits.append(
                {
                    "kind": "handoff_offer",
                    "issue": int(number),
                    "answer": "pending",
                    "offer_id": offer["id"],
                    "seconds": int(stamp - float(offer.get("created", stamp))),
                    "complete": False,
                }
            )
    return waits


def _report_waits(directory: Path, name: str, since: float) -> list[dict]:
    """Reports how long each ready report waited to be integrated."""
    waits = []
    stamp = time.time()
    pending: dict | None = None
    for record in report_records(directory, name):
        if record.get("kind") == "report":
            pending = record if record.get("state") == "ready" else None
        elif record.get("kind") == "integration" and pending:
            if float(record.get("at", 0) or 0) >= since:
                waits.append(
                    {
                        "kind": "report_integration",
                        "action": record.get("action", ""),
                        "report_id": pending["id"],
                        "seconds": int(record["at"] - pending["at"]),
                        "complete": True,
                    }
                )
            pending = None
    if pending and float(pending.get("at", 0) or 0) >= since:
        waits.append(
            {
                "kind": "report_integration",
                "action": "pending",
                "report_id": pending["id"],
                "seconds": int(stamp - pending["at"]),
                "complete": False,
            }
        )
    return waits


def waits(
    home: Path,
    directory: Path,
    manifest: dict,
    name: str,
    since: float = 0.0,
    *,
    db: sqlite3.Connection | None = None,
    ledger: dict | None = None,
) -> list[dict]:
    """Reports every recorded wait for one lane inside a window.

    Args:
        home: Private bridge state root.
        directory: Private state directory for the common repository.
        manifest: Project manifest holding this participant.
        name: Participant that owns the lane.
        since: Unix time floor for the window. Zero covers everything
            retained.
        db: Open read transaction to answer mail waits from, or ``None`` to
            open one.
        ledger: Issue snapshot already read for this project, or ``None`` to
            read one.

    Returns:
        One record per wait, each naming its kind, the item it belongs to, the
        seconds waited, and whether the wait has ended. A wait that has not
        ended reports the seconds so far and ``complete`` false, so a pending
        item is never read as an answered one.
    """
    display = manifest["participants"][name]["display"]
    try:
        mail = _mail_waits(home, manifest["root"], display, since, db)
    except (BridgeError, OSError, sqlite3.Error):
        mail = []
    return [
        *mail,
        *_offer_waits(directory, name, since, ledger),
        *_report_waits(directory, name, since),
    ]


def pending(reported: list[dict]) -> list[dict]:
    """Returns only the waits that have not ended, longest first."""
    return sorted(
        (wait for wait in reported if not wait["complete"]),
        key=lambda wait: wait["seconds"],
        reverse=True,
    )
