"""Follows one lane's coordination events as a stream.

`top` shows every lane as a row and `history participant NAME` lists what one
lane filed, but neither lets an operator sit on one participant and see its
coordination activity as it happens. This module tails the records that
already exist: the issue ledger, the per-lane report log, the store's mail,
reservations and served calls, and the lane's hook event log. Each poll reads
the same substrates the snapshot commands read, keeps a cursor over what it
has already printed, and prints only what is new.

The stream is coordination only. The native client's transcript stays in
that client; nothing here reads what the agent said or was told. Watching is
read-only: the store is opened without a write transaction, the event log is
read under its shared lock for the bounded lifetime of one read, and no
coordination state is mutated.

A rotation of the event log between two polls cannot drop or repeat a line,
because each record is identified by its own content rather than by its
position in a file, and the reader collects both retained files under the
lock that rotation takes exclusively.
"""

from __future__ import annotations

import contextlib
import json
import select
import sys
import termios
import time
import tty
from collections.abc import Callable, Iterator
from pathlib import Path

from agent_parley import checkpoints, history, store, views
from agent_parley.state import BridgeError

KINDS = (*history.KINDS, "call", "denied", "session")
BACKLOG = 20
INTERVAL = 0.5
HORIZON = 300.0
MAX_CALLS = 500
SESSION_EVENTS = {
    "SessionStart": "session started",
    "SessionEnd": "session ended",
}
KEY_FIELDS = (
    "kind",
    "action",
    "at",
    "issue",
    "claim_id",
    "detail",
    "message_id",
    "report_id",
    "path",
)


def _entry(
    kind: str, at: float, name: str, description: str, key: str, **extra: object
) -> dict:
    """Builds one stream record in the shape every substrate maps to."""
    return {
        "at": at,
        "kind": kind,
        "participant": name,
        "description": description,
        "key": key,
        **extra,
    }


def _describe(record: dict) -> str:
    """Words one history record as the sentence the stream prints."""
    kind = record["kind"]
    action = str(record.get("action", ""))
    detail = str(record.get("detail", ""))
    reason = detail.partition(": ")[2] if ": " in detail else ""
    if kind in ("claim", "handoff", "dependency"):
        described = f"{action} issue {record.get('issue')}"
        return f"{described} ({reason})" if reason else described
    if kind == "report":
        return f"report {action}"
    if kind == "message":
        return f"mail sent: {detail} (thread {record.get('thread_id')})"
    return detail


def _history(home: Path, directory: Path, manifest: dict, name: str) -> list:
    """Reads the ledger, report log, sent mail and reservations of one lane."""
    entries = []
    for record in history.records(home, directory, manifest, participant=name):
        key = json.dumps(
            {field: record.get(field) for field in KEY_FIELDS}, sort_keys=True
        )
        extra = {
            field: record.get(field)
            for field in ("issue", "claim_id", "message_id", "thread_id")
            if record.get(field) is not None
        }
        described = _describe(record)
        entries.append(
            _entry(record["kind"], record["at"], name, described, key, **extra)
        )
    return entries


def _store(home: Path, manifest: dict, name: str) -> list:
    """Reads the mail one lane received and the calls served for it."""
    if not (home / store.DATABASE).exists():
        return []
    identity = manifest["participants"][name].get("display", name)
    lanes = {
        participant["display"]: lane
        for lane, participant in manifest["participants"].items()
    }
    entries = []
    with store.connect(home) as db:
        for row in db.execute(
            "SELECT m.id,m.thread_id,s.name AS sender,"
            "unixepoch(m.created_ts) AS created FROM message_recipients r "
            "JOIN messages m ON m.id=r.message_id "
            "JOIN agents s ON s.id=m.sender_id "
            "JOIN agents a ON a.id=r.agent_id "
            "JOIN projects p ON p.id=a.project_id "
            "WHERE p.human_key=? AND a.name=? ORDER BY m.id DESC LIMIT ?",
            (manifest["root"], identity, history.MAX_RECORDS),
        ):
            sender = lanes.get(row["sender"], row["sender"])
            entries.append(
                _entry(
                    "message",
                    float(row["created"] or 0),
                    name,
                    f"mail from {sender} (thread {row['thread_id']})",
                    f"mail:{row['id']}",
                    message_id=row["id"],
                    thread_id=row["thread_id"],
                    sender=sender,
                )
            )
        for row in db.execute(
            "SELECT e.id,e.tool,e.outcome,unixepoch(e.created_ts) AS created "
            "FROM events e JOIN agents a ON a.id=e.agent_id "
            "JOIN projects p ON p.id=a.project_id "
            "WHERE p.human_key=? AND a.name=? ORDER BY e.id DESC LIMIT ?",
            (manifest["root"], identity, MAX_CALLS),
        ):
            entries.append(
                _entry(
                    "call",
                    float(row["created"] or 0),
                    name,
                    f"call {row['tool']} {row['outcome']}",
                    f"call:{row['id']}",
                    tool=row["tool"],
                    outcome=row["outcome"],
                )
            )
    return entries


def _hooks(directory: Path, name: str, since: float) -> list:
    """Reads session boundaries and denials from the lane's hook event log."""
    entries = []
    for event in checkpoints.read_events(directory, name, since):
        kind = ""
        described = ""
        if event.get("event") in SESSION_EVENTS:
            kind = "session"
            described = SESSION_EVENTS[str(event["event"])]
        elif event.get("decision") in ("deny", "block"):
            kind = "denied"
            subject = event.get("tool_name") or event.get("event") or "event"
            described = f"denied {subject} ({event.get('reason_class', '')})"
        if not kind:
            continue
        entries.append(
            _entry(
                kind,
                float(event.get("ts", 0) or 0),
                name,
                described,
                json.dumps(event, sort_keys=True),
                event=event.get("event"),
                tool=event.get("tool_name"),
                reason=event.get("reason_class"),
            )
        )
    return entries


def collect(
    home: Path,
    directory: Path,
    manifest: dict,
    name: str,
    since: float = 0.0,
    kinds: tuple[str, ...] = (),
) -> list[dict]:
    """Reads every coordination event of one lane, oldest first.

    Args:
        home: Private bridge state root.
        directory: Private state directory for the common repository.
        manifest: Project manifest holding the roster.
        name: Participant whose events are read.
        since: Unix time floor; older records are omitted.
        kinds: Kinds to keep; every kind when empty.

    Returns:
        Records oldest first, each carrying a stable ``key`` that identifies
        it independently of where it was read from.
    """
    collected = [
        *_history(home, directory, manifest, name),
        *_store(home, manifest, name),
        *_hooks(directory, name, since),
    ]
    selected = [
        record
        for record in collected
        if record["at"] >= since and (not kinds or record["kind"] in kinds)
    ]
    return sorted(selected, key=lambda record: (record["at"], record["key"]))


def line(record: dict, json_lines: bool = False) -> str:
    """Formats one record as a plain line or one JSON Lines object.

    Args:
        record: Record produced by :func:`collect`.
        json_lines: Whether to print a JSON object rather than a plain line.

    Returns:
        ``TIME KIND description`` with the time in RFC 3339, or a JSON object
        carrying the same fields and the record's identifiers.
    """
    stamp = views.timestamp(record["at"])
    if json_lines:
        fields = {key: value for key, value in record.items() if key != "key"}
        return json.dumps({**fields, "at": stamp}, sort_keys=True)
    return f"{stamp} {record['kind']} {record['description']}"


def follow(
    source: Callable[[float], list[dict]],
    emit: Callable[[dict], None],
    *,
    stop: Callable[[], bool],
    sleep: Callable[[float], None],
    interval: float = INTERVAL,
    since: float = 0.0,
    backlog: int = BACKLOG,
) -> None:
    """Emits the backlog once, then every new record until told to stop.

    The cursor is the set of keys already emitted inside a sliding floor. The
    floor trails the newest record by a fixed horizon, so a record committed
    with a slightly earlier time than one already printed is still caught,
    and keys older than the floor are forgotten so a long watch does not grow
    without bound.

    Args:
        source: Reads every record at or after a Unix time floor.
        emit: Receives each record exactly once, oldest first.
        stop: Reports whether the operator has asked to leave.
        sleep: Waits between polls; injected so a test never sleeps.
        interval: Seconds between polls.
        since: Backlog floor; the newest ``backlog`` records when zero.
        backlog: Records to print first when no floor is given.
    """
    floor = since
    seen: dict[str, float] = {}
    records = source(floor)
    if not since:
        records = records[-backlog:]
    while True:
        for record in records:
            if record["key"] in seen:
                continue
            seen[record["key"]] = record["at"]
            emit(record)
        if seen:
            floor = max(floor, max(seen.values()) - HORIZON)
            seen = {key: at for key, at in seen.items() if at >= floor}
        if stop():
            return
        sleep(interval)
        records = source(floor)


@contextlib.contextmanager
def keys() -> Iterator[Callable[[], bool]]:
    """Yields a check for the ``q`` key, inert when stdin is not a terminal.

    A terminal is put into cbreak mode for the lifetime of the stream so a
    single key is read without waiting for a line, and is restored on the way
    out. A pipe or a file on stdin cannot deliver a key, so the check reports
    nothing and the stream stops on interrupt alone.

    Yields:
        A callable reporting whether ``q`` was pressed since the last check.
    """
    if not sys.stdin.isatty():
        yield lambda: False
        return
    descriptor = sys.stdin.fileno()
    saved = termios.tcgetattr(descriptor)
    tty.setcbreak(descriptor)

    def pressed() -> bool:
        """Reports whether a pending keystroke is ``q``."""
        ready, _, _ = select.select([sys.stdin], [], [], 0)
        return bool(ready) and sys.stdin.read(1) in ("q", "Q")

    try:
        yield pressed
    finally:
        termios.tcsetattr(descriptor, termios.TCSADRAIN, saved)


def run(
    home: Path,
    directory: Path,
    manifest: dict,
    name: str,
    *,
    since: float = 0.0,
    kinds: tuple[str, ...] = (),
    json_lines: bool = False,
    interval: float = INTERVAL,
    sleep: Callable[[float], None] = time.sleep,
    stop: Callable[[], bool] | None = None,
) -> None:
    """Streams one lane's coordination events to standard output.

    Args:
        home: Private bridge state root.
        directory: Private state directory for the common repository.
        manifest: Project manifest holding the roster.
        name: Participant to follow.
        since: Seconds of backlog to print first; the newest twenty records
            when zero.
        kinds: Kinds to print; every kind when empty.
        json_lines: Print one JSON object per line instead of plain text.
        interval: Seconds between polls.
        sleep: Waits between polls; injected so a test never sleeps.
        stop: Replaces the ``q`` key check when given.

    Raises:
        BridgeError: If the named lane is not a participant of the project.
    """
    if name not in manifest["participants"]:
        raise BridgeError(
            f"{name} is not a participant in this project; "
            "run agent-parley participant list."
        )
    floor = time.time() - since if since else 0.0

    def source(after: float) -> list[dict]:
        """Reads the lane's records at or after one floor."""
        return collect(home, directory, manifest, name, after, kinds)

    def emit(record: dict) -> None:
        """Prints one record and flushes so a pipe sees it now."""
        print(line(record, json_lines), flush=True)

    if not json_lines and sys.stdout.isatty():
        print(
            f"Following {name}; coordination events only, q leaves; "
            "this stream never writes state",
            flush=True,
        )
    with (
        contextlib.suppress(KeyboardInterrupt),
        contextlib.ExitStack() as stack,
    ):
        pressed = stop if stop is not None else stack.enter_context(keys())
        follow(
            source,
            emit,
            stop=pressed,
            sleep=sleep,
            interval=interval,
            since=floor,
        )
