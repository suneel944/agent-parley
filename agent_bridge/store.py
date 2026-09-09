"""Owns transactional coordination storage without third-party runtime code."""

import contextlib
import fnmatch
import hashlib
import json
import secrets
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path, PurePosixPath

from agent_bridge.state import BridgeError, lock

DATABASE = "bridge.sqlite3"
SCHEMA_VERSION = 2
MAX_BODY_BYTES = 4096
MAX_RESULT_BYTES = 8192
MAX_RECIPIENTS = 16
MAX_ROSTER = 32
MAX_EVENT_ROWS = 2000
READ_ONLY = ("fetch_inbox", "list_participants")
SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
 id INTEGER PRIMARY KEY, human_key TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS agents (
 id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id),
 name TEXT NOT NULL, token_digest TEXT UNIQUE, task_description TEXT DEFAULT '',
 last_active_ts TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 UNIQUE(project_id,name));
CREATE TABLE IF NOT EXISTS messages (
 id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id),
 sender_id INTEGER NOT NULL REFERENCES agents(id), thread_id TEXT DEFAULT '',
 subject TEXT NOT NULL, body_md TEXT NOT NULL, ack_required INTEGER DEFAULT 0,
 created_ts TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, dedup_key TEXT,
 UNIQUE(sender_id,dedup_key));
CREATE TABLE IF NOT EXISTS message_recipients (
 message_id INTEGER NOT NULL REFERENCES messages(id),
 agent_id INTEGER NOT NULL REFERENCES agents(id), read_ts TEXT, ack_ts TEXT,
 PRIMARY KEY(message_id,agent_id));
CREATE INDEX IF NOT EXISTS inbox ON message_recipients(agent_id,message_id);
CREATE INDEX IF NOT EXISTS unread ON message_recipients(agent_id)
 WHERE read_ts IS NULL;
CREATE INDEX IF NOT EXISTS pending ON message_recipients(agent_id)
 WHERE ack_ts IS NULL;
CREATE TABLE IF NOT EXISTS file_reservations (
 id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id),
 agent_id INTEGER NOT NULL REFERENCES agents(id), path_pattern TEXT NOT NULL,
 exclusive INTEGER NOT NULL, reason TEXT DEFAULT '',
 created_ts TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 expires_ts TEXT NOT NULL, released_ts TEXT);
CREATE INDEX IF NOT EXISTS leases ON file_reservations(project_id,expires_ts)
 WHERE released_ts IS NULL;
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id),
 agent_id INTEGER NOT NULL REFERENCES agents(id), tool TEXT NOT NULL,
 outcome TEXT NOT NULL, duration_ms INTEGER NOT NULL,
 result_bytes INTEGER NOT NULL,
 created_ts TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE INDEX IF NOT EXISTS history ON events(project_id,id);
"""


@contextlib.contextmanager
def connect(home: Path, *, write: bool = False) -> Iterator[sqlite3.Connection]:
    """Opens a bounded transaction and always closes its connection.

    Writers allow one second for another transaction to commit. The former
    300 ms ceiling rejected ordinary contention on loaded CI workers. SQLite
    acquires uncontended locks immediately; this budget adds no fixed delay.
    """
    db = sqlite3.connect(home / DATABASE, timeout=1.0)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    try:
        db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def initialize(home: Path) -> None:
    """Creates or upgrades the store and imports a legacy service once.

    The legacy database is read-only. Schema version publication and imported
    rows commit together, so interrupted imports can safely retry. Upgrading a
    schema-1 store adds the tool event log and reservation creation time in
    place; no coordination row is rewritten, and leases that predate the
    upgrade date from it.
    """
    with lock(home / "store.lock"):
        path = home / DATABASE
        with contextlib.closing(sqlite3.connect(path, timeout=1)) as db:
            path.chmod(0o600)
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == SCHEMA_VERSION:
                return
            if version > SCHEMA_VERSION:
                raise BridgeError(
                    "Unsupported store schema; use a newer bridge."
                )
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
            if version == 1:
                _add_reservation_created(db)
        with connect(home, write=True) as db:
            legacy = home / "mail.sqlite3"
            if version == 0 and legacy.exists():
                _import_legacy(db, legacy)
            if version == 1:
                db.execute(
                    "UPDATE file_reservations SET created_ts=CURRENT_TIMESTAMP"
                    " WHERE created_ts IS NULL"
                )
            db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


def _add_reservation_created(db: sqlite3.Connection) -> None:
    """Adds the reservation creation column to a schema-1 store."""
    columns = {
        row[1] for row in db.execute("PRAGMA table_info(file_reservations)")
    }
    if "created_ts" not in columns:
        db.execute("ALTER TABLE file_reservations ADD COLUMN created_ts TEXT")


def _import_legacy(db: sqlite3.Connection, legacy: Path) -> None:
    """Imports one consistent read-only snapshot of the legacy store."""
    columns = {
        "projects": "id,human_key",
        "agents": "id,project_id,name,task_description,last_active_ts",
        "messages": (
            "id,project_id,sender_id,thread_id,subject,body_md,"
            "ack_required,created_ts"
        ),
        "message_recipients": "message_id,agent_id,read_ts,ack_ts",
        "file_reservations": (
            "id,project_id,agent_id,path_pattern,exclusive,reason,"
            "expires_ts,released_ts"
        ),
    }
    with contextlib.closing(
        sqlite3.connect(legacy.as_uri() + "?mode=ro", uri=True)
    ) as source:
        source.execute("BEGIN")
        for table, names in columns.items():
            placeholders = ",".join("?" for _ in names.split(","))
            db.executemany(
                f"INSERT INTO {table} ({names}) VALUES ({placeholders})",
                source.execute(f"SELECT {names} FROM {table}"),
            )


def register(home: Path, root: str, name: str, token: str = "") -> dict:
    """Registers a locally authorized lane; never exposed as an MCP tool."""
    token = token or secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode()).hexdigest()
    with connect(home, write=True) as db:
        db.execute(
            "INSERT OR IGNORE INTO projects(human_key) VALUES (?)", (root,)
        )
        project = db.execute(
            "SELECT id FROM projects WHERE human_key=?", (root,)
        ).fetchone()[0]
        db.execute(
            "INSERT INTO agents(project_id,name,token_digest) VALUES (?,?,?) "
            "ON CONFLICT(project_id,name) DO UPDATE SET "
            "token_digest=excluded.token_digest",
            (project, name, digest),
        )
    return {"name": name, "registration_token": token}


def revoke(home: Path, root: str, name: str) -> int:
    """Invalidates a retired participant's credential, retaining its mail.

    Args:
        home: Private bridge state root.
        root: Canonical project key registered with the store.
        name: Registered identity of the retired participant.

    Returns:
        Number of credentials invalidated.
    """
    if not (home / DATABASE).exists():
        return 0
    with connect(home, write=True) as db:
        result = db.execute(
            "UPDATE agents SET token_digest=NULL WHERE id IN "
            "(SELECT a.id FROM agents a JOIN projects p ON p.id=a.project_id "
            "WHERE p.human_key=? AND a.name=?)",
            (root, name),
        )
        return result.rowcount


def authenticate(home: Path, token: str) -> dict | None:
    """Resolves a bearer credential to exactly one project and lane."""
    with connect(home) as db:
        row = db.execute(
            "SELECT id,project_id,name FROM agents WHERE token_digest=?",
            (hashlib.sha256(token.encode()).hexdigest(),),
        ).fetchone()
        return dict(row) if row else None


def _text(
    value: object, name: str, maximum: int, *, empty: bool = False
) -> str:
    """Validates bounded text at the protocol boundary."""
    if (
        not isinstance(value, str)
        or (not empty and not value.strip())
        or "\x00" in value
    ):
        raise BridgeError(f"{name} must be text of at most {maximum} bytes.")
    try:
        length = len(value.encode())
    except UnicodeError:
        raise BridgeError(f"{name} must contain valid Unicode.") from None
    if length > maximum:
        raise BridgeError(f"{name} exceeds its {maximum}-byte budget.")
    return value


def _number(value: object, name: str, low: int, high: int) -> int:
    """Validates integer bounds without accepting booleans as integers."""
    if type(value) is not int or not low <= value <= high:
        raise BridgeError(f"{name} must be an integer in {low}..{high}.")
    return value


def _flag(value: object, name: str) -> bool:
    """Rejects truthy strings and numeric substitutes for booleans."""
    if type(value) is not bool:
        raise BridgeError(f"{name} must be a boolean.")
    return value


def _send(db: sqlite3.Connection, actor: dict, args: dict) -> dict:
    """Atomically delivers an idempotent message to authorized recipients."""
    subject = _text(args.get("subject"), "subject", 160)
    body = _text(args.get("body_md"), "body_md", MAX_BODY_BYTES)
    thread = _text(args.get("thread_id", ""), "thread_id", 80, empty=True)
    key = _text(args.get("idempotency_key"), "idempotency_key", 80)
    ack = _flag(args.get("ack_required", False), "ack_required")
    recipients = args.get("to")
    if (
        not isinstance(recipients, list)
        or not 1 <= len(recipients) <= MAX_RECIPIENTS
    ):
        raise BridgeError(
            f"to must contain 1..{MAX_RECIPIENTS} registered participants."
        )
    ids = []
    for recipient in recipients:
        name = _text(recipient, "recipient", 80)
        row = db.execute(
            "SELECT id FROM agents WHERE project_id=? AND name=?",
            (actor["project_id"], name),
        ).fetchone()
        if not row:
            raise BridgeError("Recipient is not registered in your project.")
        ids.append(row[0])
    existing = db.execute(
        "SELECT * FROM messages WHERE sender_id=? AND dedup_key=?",
        (actor["id"], key),
    ).fetchone()
    if existing:
        previous = {
            row[0]
            for row in db.execute(
                "SELECT agent_id FROM message_recipients WHERE message_id=?",
                (existing["id"],),
            )
        }
        if (
            existing["subject"],
            existing["body_md"],
            existing["thread_id"],
            existing["ack_required"],
        ) != (subject, body, thread, ack) or previous != set(ids):
            raise BridgeError("Idempotency key already names another message.")
        return {"id": existing["id"], "duplicate": True}
    cursor = db.execute(
        "INSERT INTO messages(project_id,sender_id,subject,body_md,"
        "thread_id,ack_required,dedup_key) VALUES (?,?,?,?,?,?,?)",
        (actor["project_id"], actor["id"], subject, body, thread, ack, key),
    )
    message_id = cursor.lastrowid
    db.executemany(
        "INSERT INTO message_recipients(message_id,agent_id) VALUES (?,?)",
        [(message_id, recipient) for recipient in set(ids)],
    )
    return {"id": message_id}


def _roster(db: sqlite3.Connection, actor: dict) -> dict:
    """Lists this project's participants so peers stay addressable."""
    rows = db.execute(
        "SELECT name,substr(task_description,1,160) AS task_description,"
        "last_active_ts FROM agents WHERE project_id=? ORDER BY name LIMIT ?",
        (actor["project_id"], MAX_ROSTER),
    ).fetchall()
    return {"you": actor["name"], "participants": [dict(row) for row in rows]}


def _inbox(db: sqlite3.Connection, actor: dict, args: dict) -> dict:
    """Pages metadata and legacy bodies within a bounded response budget."""
    after = _number(args.get("after_id", 0), "after_id", 0, 2**63 - 1)
    limit = _number(args.get("limit", 5), "limit", 1, 5)
    bodies = _flag(args.get("include_bodies", False), "include_bodies")
    rows = db.execute(
        "SELECT m.id,a.name AS sender,m.subject,m.body_md,m.ack_required "
        "FROM messages m JOIN agents a ON a.id=m.sender_id "
        "JOIN message_recipients r ON r.message_id=m.id "
        "WHERE r.agent_id=? AND m.id>? ORDER BY m.id LIMIT ?",
        (actor["id"], after, limit + 1),
    ).fetchall()
    result: dict = {"messages": [], "next_after_id": after, "has_more": False}
    for row in rows[:limit]:
        item = dict(row)
        if not bodies:
            item.pop("body_md")
        item["subject"] = item["subject"][:160]
        offset = _number(args.get("body_offset", 0), "body_offset", 0, 10**9)
        if bodies:
            full = item["body_md"]
            item["body_md"] = full[offset : offset + 1024]
            if offset + 1024 < len(full):
                item["next_body_offset"] = offset + 1024
        candidate = {**result, "messages": [*result["messages"], item]}
        if len(json.dumps(candidate, ensure_ascii=False).encode()) > 7500:
            break
        result["messages"].append(item)
        result["next_after_id"] = row["id"]
    result["has_more"] = len(rows) > len(result["messages"])
    return result


def _reserve(db: sqlite3.Connection, actor: dict, args: dict) -> dict:
    """Grants all requested leases or none; two globs conservatively overlap.

    A conflict names the blocking owner and that owner's declared reason,
    clipped to 80 characters, so a denied caller can judge the overlap without
    a further round trip. The reason is omitted when the owner declared none.
    Conflicts are reported until the response budget is reached; ``has_more``
    states that further conflicts exist beyond the reported ones.
    """
    paths = args.get("paths")
    if not isinstance(paths, list) or not 1 <= len(paths) <= 16:
        raise BridgeError("paths must contain 1..16 repository-relative paths.")
    ttl = _number(args.get("ttl_seconds", 900), "ttl_seconds", 30, 3600)
    exclusive = _flag(args.get("exclusive", True), "exclusive")
    reason = _text(args.get("reason", ""), "reason", 160, empty=True)
    for pattern in paths:
        _text(pattern, "path", 240)
        parts = PurePosixPath(pattern)
        if (
            parts.is_absolute()
            or ".." in parts.parts
            or "\\" in pattern
            or str(parts) == "."
        ):
            raise BridgeError("Reservations require repository-relative paths.")
    paths = [str(PurePosixPath(pattern)) for pattern in paths]
    leases = db.execute(
        "SELECT f.id,f.path_pattern,f.exclusive,a.name,"
        "substr(f.reason,1,80) AS reason FROM file_reservations f "
        "JOIN agents a ON a.id=f.agent_id WHERE f.project_id=? "
        "AND f.agent_id!=? AND f.released_ts IS NULL "
        "AND f.expires_ts>CURRENT_TIMESTAMP",
        (actor["project_id"], actor["id"]),
    ).fetchall()
    conflicts = []
    for pattern in set(paths):
        for lease in leases:
            other = lease["path_pattern"]
            both_globs = any(c in pattern for c in "*?[") and any(
                c in other for c in "*?["
            )
            overlaps = (
                both_globs
                or fnmatch.fnmatchcase(pattern, other)
                or fnmatch.fnmatchcase(other, pattern)
                or pattern.startswith(other.rstrip("/") + "/")
                or other.startswith(pattern.rstrip("/") + "/")
            )
            if (exclusive or lease["exclusive"]) and overlaps:
                conflict = {"path": pattern, "owner": lease["name"]}
                if lease["reason"]:
                    conflict["reason"] = lease["reason"]
                conflicts.append(conflict)
    if conflicts:
        reported: list[dict] = []
        for conflict in conflicts[:16]:
            candidate = {"granted": [], "conflicts": [*reported, conflict]}
            if (
                len(json.dumps(candidate, ensure_ascii=False).encode())
                > MAX_RESULT_BYTES
            ):
                break
            reported.append(conflict)
        return {
            "granted": [],
            "conflicts": reported,
            "has_more": len(reported) < len(conflicts),
        }
    owned = db.execute(
        "SELECT path_pattern FROM file_reservations WHERE agent_id=? "
        "AND released_ts IS NULL AND expires_ts>CURRENT_TIMESTAMP",
        (actor["id"],),
    ).fetchall()
    if len({row[0] for row in owned} | set(paths)) > 128:
        raise BridgeError("Release unused reservations before adding more.")
    granted = []
    for pattern in sorted(set(paths)):
        db.execute(
            "UPDATE file_reservations SET released_ts=CURRENT_TIMESTAMP "
            "WHERE agent_id=? AND path_pattern=? AND released_ts IS NULL",
            (actor["id"], pattern),
        )
        cursor = db.execute(
            "INSERT INTO file_reservations(project_id,agent_id,path_pattern,"
            "exclusive,reason,expires_ts) VALUES (?,?,?,?,?,"
            "datetime('now',?))",
            (
                actor["project_id"],
                actor["id"],
                pattern,
                exclusive,
                reason,
                f"+{ttl} seconds",
            ),
        )
        granted.append({"id": cursor.lastrowid, "path": pattern})
    return {"granted": granted, "conflicts": []}


def _event(
    db: sqlite3.Connection,
    actor: dict,
    tool: str,
    outcome: str,
    started: float,
    result_bytes: int,
) -> None:
    """Appends one tool event and retires this project's oldest events.

    Args:
        db: Open transaction that also carries the tool's own effect.
        actor: Authenticated project and lane.
        tool: Coordination tool that was called.
        outcome: ``ok`` for a served call, ``error`` for a rejected one.
        started: Monotonic reading taken before the call was dispatched.
        result_bytes: Encoded size of the served result, zero on rejection.
    """
    db.execute(
        "INSERT INTO events(project_id,agent_id,tool,outcome,duration_ms,"
        "result_bytes) VALUES (?,?,?,?,?,?)",
        (
            actor["project_id"],
            actor["id"],
            tool,
            outcome,
            int((time.monotonic() - started) * 1000),
            result_bytes,
        ),
    )
    db.execute(
        "DELETE FROM events WHERE project_id=? AND id<=(SELECT id FROM events "
        "WHERE project_id=? ORDER BY id DESC LIMIT 1 OFFSET ?)",
        (actor["project_id"], actor["project_id"], MAX_EVENT_ROWS),
    )


def _observe(
    home: Path,
    actor: dict,
    tool: str,
    outcome: str,
    started: float,
    result_bytes: int,
) -> None:
    """Records an event that cannot share the served call's transaction.

    A rejected call rolls its transaction back, and a read-only call holds no
    write lock, so both are recorded afterwards in their own short
    transaction. Telemetry never decides an outcome: a store that is busy or
    unavailable loses the record rather than the call.
    """
    with contextlib.suppress(sqlite3.Error):
        with connect(home, write=True) as db:
            _event(db, actor, tool, outcome, started, result_bytes)


def call(home: Path, actor: dict, tool: str, args: dict) -> dict:
    """Executes a validated MCP operation under one atomic transaction.

    A writing tool records its event inside that transaction, so an event
    exists exactly when the effect it describes was committed.

    Args:
        home: Private bridge state root.
        actor: Authenticated project and lane.
        tool: Coordination tool named by the caller.
        args: Validated tool arguments.

    Returns:
        The tool result.

    Raises:
        BridgeError: If validation or the tool's own preconditions fail.
    """
    started = time.monotonic()
    try:
        result = _dispatch(home, actor, tool, args, started)
    except BridgeError:
        _observe(home, actor, tool, "error", started, 0)
        raise
    if tool in READ_ONLY:
        _observe(
            home,
            actor,
            tool,
            "ok",
            started,
            len(json.dumps(result, ensure_ascii=False).encode()),
        )
    return result


def _dispatch(
    home: Path, actor: dict, tool: str, args: dict, started: float
) -> dict:
    """Runs one coordination tool inside its own bounded transaction."""
    with connect(home, write=tool not in READ_ONLY) as db:
        result = _serve(db, actor, tool, args)
        if tool not in READ_ONLY:
            _event(
                db,
                actor,
                tool,
                "ok",
                started,
                len(json.dumps(result, ensure_ascii=False).encode()),
            )
        return result


def _serve(db: sqlite3.Connection, actor: dict, tool: str, args: dict) -> dict:
    """Applies one validated coordination tool to the open transaction.

    Args:
        db: Open transaction owned by the caller.
        actor: Authenticated project and lane.
        tool: Coordination tool named by the caller.
        args: Validated tool arguments.

    Returns:
        The tool result.

    Raises:
        BridgeError: If the tool is unknown or its preconditions fail.
    """
    if tool not in READ_ONLY:
        db.execute(
            "UPDATE agents SET last_active_ts=CURRENT_TIMESTAMP WHERE id=?",
            (actor["id"],),
        )
    if tool == "send_message":
        return _send(db, actor, args)
    if tool == "fetch_inbox":
        return _inbox(db, actor, args)
    if tool == "list_participants":
        return _roster(db, actor)
    if tool == "file_reservation_paths":
        return _reserve(db, actor, args)
    if tool == "release_file_reservations":
        result = db.execute(
            "UPDATE file_reservations SET released_ts=CURRENT_TIMESTAMP "
            "WHERE agent_id=? AND released_ts IS NULL",
            (actor["id"],),
        )
        return {"released": result.rowcount}
    if tool in ("acknowledge_message", "mark_message_read"):
        message = _number(args.get("message_id"), "message_id", 1, 2**63 - 1)
        ack = tool == "acknowledge_message"
        result = db.execute(
            "UPDATE message_recipients SET read_ts=CURRENT_TIMESTAMP"
            + (",ack_ts=CURRENT_TIMESTAMP" if ack else "")
            + " WHERE message_id=? AND agent_id=?",
            (message, actor["id"]),
        )
        if not result.rowcount:
            raise BridgeError("Message is not in your inbox.")
        return {"id": message, "acknowledged": ack}
    raise BridgeError("Unknown coordination tool.")


def usage(home: Path, root: str) -> dict[str, dict]:
    """Reports retained tool events and live leases for one project.

    Args:
        home: Private bridge state root.
        root: Canonical project key registered with the store.

    Returns:
        Mapping of registered identity to served calls, rejected calls,
        returned bytes, held leases, and the age of its oldest live lease.
        Counts cover retained events only; older events are retired.
    """
    if not (home / DATABASE).exists():
        return {}
    report: dict[str, dict] = {}
    with connect(home) as db:
        for row in db.execute(
            "SELECT a.name AS name,count(e.id) AS calls,"
            "coalesce(sum(e.outcome='error'),0) AS errors,"
            "coalesce(sum(e.result_bytes),0) AS result_bytes "
            "FROM agents a JOIN projects p ON p.id=a.project_id "
            "LEFT JOIN events e ON e.agent_id=a.id "
            "WHERE p.human_key=? GROUP BY a.id",
            (root,),
        ):
            report[row["name"]] = {**dict(row), "leases": 0, "lease_age": 0}
        for row in db.execute(
            "SELECT a.name AS name,count(*) AS leases,"
            "cast(strftime('%s','now')-strftime('%s',min(f.created_ts)) "
            "AS INTEGER) AS lease_age FROM file_reservations f "
            "JOIN agents a ON a.id=f.agent_id "
            "JOIN projects p ON p.id=a.project_id WHERE p.human_key=? "
            "AND f.released_ts IS NULL AND f.expires_ts>CURRENT_TIMESTAMP "
            "GROUP BY a.id",
            (root,),
        ):
            report.setdefault(row["name"], {}).update(
                leases=row["leases"], lease_age=max(0, row["lease_age"] or 0)
            )
    return report
