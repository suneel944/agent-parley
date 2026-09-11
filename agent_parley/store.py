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

from agent_parley.roster import OPERATOR
from agent_parley.state import BridgeError, lock

DATABASE = "bridge.sqlite3"
SCHEMA_VERSION = 4
BUSY_TIMEOUT = 5.0
MAX_BODY_BYTES = 4096
MAX_RESULT_BYTES = 8192
MAX_RECIPIENTS = 16
MAX_ROSTER = 32
MAX_EVENT_ROWS = 2000
MAX_THREAD_PAGE = 10
MAX_SEARCH_HITS = 5
MAX_QUERY_BYTES = 160
PREVIEW_CHARACTERS = 240
READ_ONLY = (
    "fetch_inbox",
    "list_participants",
    "read_thread",
    "search_messages",
)
NO_PROJECT = (
    "This repository has no coordination project yet; launch a participant "
    "once with agent-parley run so the project registers."
)
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
CREATE INDEX IF NOT EXISTS threads ON messages(project_id,thread_id,id);
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
 expires_ts TEXT, released_ts TEXT);
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
SEARCH_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS message_search USING fts5(
 subject, body_md, content='messages', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS message_indexed AFTER INSERT ON messages BEGIN
 INSERT INTO message_search(rowid,subject,body_md)
 VALUES (new.id,new.subject,new.body_md);
END;
CREATE TRIGGER IF NOT EXISTS message_unindexed AFTER DELETE ON messages BEGIN
 INSERT INTO message_search(message_search,rowid,subject,body_md)
 VALUES ('delete',old.id,old.subject,old.body_md);
END;
"""


@contextlib.contextmanager
def connect(home: Path, *, write: bool = False) -> Iterator[sqlite3.Connection]:
    """Opens a bounded transaction and always closes its connection.

    A writer waits `BUSY_TIMEOUT` seconds for the holding transaction to
    commit before it reports the store as locked. The sixteen concurrent
    workers of the service serialize their writes in about a tenth of a
    second on an idle machine, so the budget carries some fifty times that
    queue. Loaded shared machines stretch the same queue past the former one
    second ceiling, which turned ordinary contention into a refused
    coordination call. SQLite acquires uncontended locks immediately, so the
    budget adds no fixed delay; it bounds only a wait that is already
    happening.
    """
    db = sqlite3.connect(home / DATABASE, timeout=BUSY_TIMEOUT)
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

    Upgrading a schema-2 store gives every stored message that carries no
    thread its own thread identifier, indexes threads, and builds the
    full-text index over stored subjects and bodies. Message text is read
    rather than rewritten. Where SQLite was built without FTS5 the index is
    skipped and the store still opens; searching then matches substrings.

    Upgrading a schema-3 store makes a lease's time to live optional, and
    every existing lease keeps the deadline it was taken with. No coordination
    value is rewritten, and each step is skipped once its result is already
    present, so an interrupted upgrade safely retries.
    """
    with lock(home / "store.lock"):
        path = home / DATABASE
        with contextlib.closing(
            sqlite3.connect(path, timeout=BUSY_TIMEOUT)
        ) as db:
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
            _rebuild_reservations(db)
            _add_message_search(db)
        with connect(home, write=True) as db:
            legacy = home / "mail.sqlite3"
            if version == 0 and legacy.exists():
                _import_legacy(db, legacy)
            if version == 1:
                db.execute(
                    "UPDATE file_reservations SET created_ts=CURRENT_TIMESTAMP"
                    " WHERE created_ts IS NULL"
                )
            _open_threads(db)
            _rebuild_search(db)
            db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


def _add_reservation_created(db: sqlite3.Connection) -> None:
    """Adds the reservation creation column to a schema-1 store."""
    columns = {
        row[1] for row in db.execute("PRAGMA table_info(file_reservations)")
    }
    if "created_ts" not in columns:
        db.execute("ALTER TABLE file_reservations ADD COLUMN created_ts TEXT")


def _add_message_search(db: sqlite3.Connection) -> None:
    """Creates the full-text index where the SQLite build provides FTS5.

    A build without the FTS5 module rejects the virtual table, which leaves
    the store usable and search degraded rather than the store unopenable.
    """
    with contextlib.suppress(sqlite3.OperationalError):
        db.executescript(SEARCH_SCHEMA)


def _searchable(db: sqlite3.Connection) -> bool:
    """Reports whether this store carries a full-text index."""
    return bool(
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='message_search'"
        ).fetchone()
    )


def _open_threads(db: sqlite3.Connection) -> None:
    """Gives every stored message without a thread one of its own."""
    db.execute(
        "UPDATE messages SET thread_id='t'||id "
        "WHERE thread_id IS NULL OR thread_id=''"
    )


def _rebuild_search(db: sqlite3.Connection) -> None:
    """Indexes every stored message when this store carries an index."""
    if _searchable(db):
        db.execute(
            "INSERT INTO message_search(message_search) VALUES ('rebuild')"
        )


def _rebuild_reservations(db: sqlite3.Connection) -> None:
    """Makes a lease's time to live optional in an older store.

    A store written before the opt-in required every lease to carry a
    deadline. SQLite cannot relax that requirement in place, so the table is
    copied once into its current shape. Every lease keeps the deadline it was
    taken with, and a lease that gained its creation column in the schema-1
    upgrade is dated from this copy. The copy is skipped once the column
    already accepts a lease without a deadline.
    """
    required = [
        row[3]
        for row in db.execute("PRAGMA table_info(file_reservations)")
        if row[1] == "expires_ts"
    ]
    if not required or not required[0]:
        return
    db.executescript(
        "CREATE TABLE rebuilt_reservations ("
        " id INTEGER PRIMARY KEY,"
        " project_id INTEGER NOT NULL REFERENCES projects(id),"
        " agent_id INTEGER NOT NULL REFERENCES agents(id),"
        " path_pattern TEXT NOT NULL, exclusive INTEGER NOT NULL,"
        " reason TEXT DEFAULT '',"
        " created_ts TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,"
        " expires_ts TEXT, released_ts TEXT);"
        "INSERT INTO rebuilt_reservations (id,project_id,agent_id,"
        "path_pattern,exclusive,reason,created_ts,expires_ts,released_ts) "
        "SELECT id,project_id,agent_id,path_pattern,exclusive,reason,"
        "coalesce(created_ts,CURRENT_TIMESTAMP),expires_ts,released_ts "
        "FROM file_reservations;"
        "DROP TABLE file_reservations;"
        "ALTER TABLE rebuilt_reservations RENAME TO file_reservations;"
        "CREATE INDEX IF NOT EXISTS leases ON "
        "file_reservations(project_id,expires_ts) WHERE released_ts IS NULL;"
    )


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
    """Registers a locally authorized lane; never exposed as an MCP tool.

    Args:
        home: Private bridge state root.
        root: Canonical project key registered with the store.
        name: Identity the lane presents to its peers.
        token: Credential retained from an earlier registration, or empty to
            mint one.

    Returns:
        The registered identity and its bearer credential.

    Raises:
        BridgeError: If the name is the reserved operator identity, which
            writes from the command line and never holds a credential.
    """
    if name == OPERATOR:
        raise BridgeError(
            f"{OPERATOR!r} is the command-line operator identity; it cannot "
            "be registered or hold a coordination credential."
        )
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


def _opened_thread(actor: dict, key: str) -> str:
    """Derives the identifier of the thread a message opens.

    The identifier is derived from the sender and its idempotency key rather
    than allocated, so an interrupted send that retries resolves the same
    thread instead of opening a second one.

    Args:
        actor: Authenticated project and lane.
        key: Idempotency key naming this send.

    Returns:
        Opaque thread identifier within this project.
    """
    seed = f"{actor['project_id']}\x00{actor['id']}\x00{key}"
    return hashlib.sha256(seed.encode()).hexdigest()[:32]


def _answered_thread(db: sqlite3.Connection, actor: dict, value: object) -> str:
    """Resolves the thread carried by the message a send answers.

    Args:
        db: Open transaction owned by the caller.
        actor: Authenticated project and lane.
        value: Identifier of the message being answered.

    Returns:
        Thread identifier of the answered message.

    Raises:
        BridgeError: If the message is outside this sender's own mail.
    """
    answered = _number(value, "reply_to", 1, 2**63 - 1)
    row = db.execute(
        "SELECT m.thread_id FROM messages m LEFT JOIN message_recipients r "
        "ON r.message_id=m.id AND r.agent_id=? WHERE m.id=? AND m.project_id=? "
        "AND (m.sender_id=? OR r.agent_id IS NOT NULL)",
        (actor["id"], answered, actor["project_id"], actor["id"]),
    ).fetchone()
    if not row:
        raise BridgeError("reply_to must name a message you sent or received.")
    return row[0]


def _send(db: sqlite3.Connection, actor: dict, args: dict) -> dict:
    """Atomically delivers an idempotent message to authorized recipients.

    A send naming ``reply_to`` joins the thread of the message it answers. A
    send naming neither ``reply_to`` nor ``thread_id`` opens its own thread,
    so every delivered message belongs to exactly one thread.
    """
    subject = _text(args.get("subject"), "subject", 160)
    body = _text(args.get("body_md"), "body_md", MAX_BODY_BYTES)
    thread = _text(args.get("thread_id", ""), "thread_id", 80, empty=True)
    key = _text(args.get("idempotency_key"), "idempotency_key", 80)
    ack = _flag(args.get("ack_required", False), "ack_required")
    if "reply_to" in args:
        if thread:
            raise BridgeError("Answer with reply_to or thread_id, not both.")
        thread = _answered_thread(db, actor, args["reply_to"])
    thread = thread or _opened_thread(actor, key)
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
        return {
            "id": existing["id"],
            "thread_id": existing["thread_id"],
            "duplicate": True,
        }
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
    return {"id": message_id, "thread_id": thread}


def _roster(db: sqlite3.Connection, actor: dict) -> dict:
    """Lists this project's participants so peers stay addressable."""
    rows = db.execute(
        "SELECT name,substr(task_description,1,160) AS task_description,"
        "last_active_ts FROM agents WHERE project_id=? ORDER BY name LIMIT ?",
        (actor["project_id"], MAX_ROSTER),
    ).fetchall()
    return {"you": actor["name"], "participants": [dict(row) for row in rows]}


def _inbox(db: sqlite3.Connection, actor: dict, args: dict) -> dict:
    """Pages metadata and legacy bodies within a bounded response budget.

    Each message reports the thread it belongs to, so a recipient can read
    that thread or answer into it without a further lookup.
    """
    after = _number(args.get("after_id", 0), "after_id", 0, 2**63 - 1)
    limit = _number(args.get("limit", 5), "limit", 1, 5)
    bodies = _flag(args.get("include_bodies", False), "include_bodies")
    rows = db.execute(
        "SELECT m.id,a.name AS sender,m.thread_id,m.subject,m.body_md,"
        "m.ack_required "
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


MAIL_COLUMNS = (
    "SELECT m.id,a.name AS sender,m.thread_id,"
    "substr(m.subject,1,160) AS subject,"
    "substr(m.body_md,1,?) AS body_md,m.created_ts,m.ack_required "
)
MAIL_SCOPE = (
    "JOIN agents a ON a.id=m.sender_id LEFT JOIN message_recipients r "
    "ON r.message_id=m.id AND r.agent_id=? WHERE m.project_id=? "
    "AND (m.sender_id=? OR r.agent_id IS NOT NULL) "
)


def _bounded(result: dict, rows: list[sqlite3.Row]) -> list[dict]:
    """Returns the leading rows whose serialized result stays in budget.

    Args:
        result: Response fields that accompany the reported messages.
        rows: Candidate rows, already ordered and count-limited.

    Returns:
        Reported messages, which may be fewer than the candidates.
    """
    reported: list[dict] = []
    for row in rows:
        candidate = {**result, "messages": [*reported, dict(row)]}
        if len(json.dumps(candidate, ensure_ascii=False).encode()) > 7500:
            break
        reported.append(dict(row))
    return reported


def _thread(db: sqlite3.Connection, actor: dict, args: dict) -> dict:
    """Reads one thread's messages in send order within a bounded budget.

    Only mail this participant sent or received is reported, so reading a
    thread never widens what a lane can already see.
    """
    thread = _text(args.get("thread_id"), "thread_id", 80)
    after = _number(args.get("after_id", 0), "after_id", 0, 2**63 - 1)
    limit = _number(
        args.get("limit", MAX_THREAD_PAGE), "limit", 1, MAX_THREAD_PAGE
    )
    rows = db.execute(
        MAIL_COLUMNS
        + "FROM messages m "
        + MAIL_SCOPE
        + "AND m.thread_id=? AND m.id>? ORDER BY m.id LIMIT ?",
        (
            PREVIEW_CHARACTERS,
            actor["id"],
            actor["project_id"],
            actor["id"],
            thread,
            after,
            limit + 1,
        ),
    ).fetchall()
    result: dict = {"thread_id": thread, "next_after_id": after}
    messages = _bounded(result, rows[:limit])
    if messages:
        result["next_after_id"] = messages[-1]["id"]
    result["messages"] = messages
    result["has_more"] = len(rows) > len(messages)
    return result


def _phrase(query: str) -> str:
    """Quotes a query as one FTS5 phrase so its operators stay literal."""
    return '"' + query.replace('"', '""') + '"'


def _wildcards(query: str) -> str:
    """Escapes LIKE wildcards so a query matches literal text only."""
    for character in ("\\", "%", "_"):
        query = query.replace(character, "\\" + character)
    return query


def _indexed_matches(
    db: sqlite3.Connection, actor: dict, query: str, limit: int
) -> list[sqlite3.Row]:
    """Matches the query as one phrase against the full-text index.

    The index is named rather than aliased because FTS5 resolves a MATCH
    constraint against the table name alone.
    """
    return db.execute(
        MAIL_COLUMNS
        + "FROM message_search JOIN messages m ON m.id=message_search.rowid "
        + MAIL_SCOPE
        + "AND message_search MATCH ? ORDER BY m.id DESC LIMIT ?",
        (
            PREVIEW_CHARACTERS,
            actor["id"],
            actor["project_id"],
            actor["id"],
            _phrase(query),
            limit + 1,
        ),
    ).fetchall()


def _substring_matches(
    db: sqlite3.Connection, actor: dict, query: str, limit: int
) -> list[sqlite3.Row]:
    """Matches the query as a literal substring of a subject or body."""
    pattern = f"%{_wildcards(query)}%"
    return db.execute(
        MAIL_COLUMNS
        + "FROM messages m "
        + MAIL_SCOPE
        + "AND (m.subject LIKE ? ESCAPE '\\' OR m.body_md LIKE ? ESCAPE '\\') "
        "ORDER BY m.id DESC LIMIT ?",
        (
            PREVIEW_CHARACTERS,
            actor["id"],
            actor["project_id"],
            actor["id"],
            pattern,
            pattern,
            limit + 1,
        ),
    ).fetchall()


def _search(db: sqlite3.Connection, actor: dict, args: dict) -> dict:
    """Searches this participant's own mail, newest match first.

    A store whose SQLite build provides FTS5 matches the query as a phrase
    of indexed terms. Where that index is absent the same query is matched
    as a literal substring of a subject or body, which is slower and narrower
    but keeps searching available. The reported ``index`` names which of the
    two answered the call.
    """
    query = _text(args.get("query"), "query", MAX_QUERY_BYTES)
    limit = _number(
        args.get("limit", MAX_SEARCH_HITS), "limit", 1, MAX_SEARCH_HITS
    )
    rows = None
    if _searchable(db):
        with contextlib.suppress(sqlite3.OperationalError):
            rows = _indexed_matches(db, actor, query, limit)
    indexed = rows is not None
    if rows is None:
        rows = _substring_matches(db, actor, query, limit)
    result: dict = {
        "query": query,
        "index": "fts5" if indexed else "substring",
    }
    messages = _bounded(result, rows[:limit])
    result["messages"] = messages
    result["has_more"] = len(rows) > len(messages)
    return result


def _reserve(db: sqlite3.Connection, actor: dict, args: dict) -> dict:
    """Grants all requested leases or none; two globs conservatively overlap.

    A conflict names the blocking owner and that owner's declared reason,
    clipped to 80 characters, so a denied caller can judge the overlap without
    a further round trip. The reason is omitted when the owner declared none.
    Conflicts are reported until the response budget is reached; ``has_more``
    states that further conflicts exist beyond the reported ones.

    ``ttl_seconds`` is optional. Without it the lease carries no deadline and
    never reports as stale. With it, a lease whose deadline has passed carries
    ``stale`` in the conflict it raises, so a reader can tell a working owner
    from one that died holding the path. Staleness is a report: the lease is
    not revoked, not reassigned, and blocks exactly the paths it already
    blocked until its owner releases it.
    """
    paths = args.get("paths")
    if not isinstance(paths, list) or not 1 <= len(paths) <= 16:
        raise BridgeError("paths must contain 1..16 repository-relative paths.")
    ttl = args.get("ttl_seconds")
    if ttl is not None:
        ttl = _number(ttl, "ttl_seconds", 30, 3600)
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
        "substr(f.reason,1,80) AS reason,"
        "(f.expires_ts IS NOT NULL AND f.expires_ts<=CURRENT_TIMESTAMP) "
        "AS stale FROM file_reservations f "
        "JOIN agents a ON a.id=f.agent_id WHERE f.project_id=? "
        "AND f.agent_id!=? AND f.released_ts IS NULL",
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
                if lease["stale"]:
                    conflict["stale"] = True
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
        "AND released_ts IS NULL",
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
                None if ttl is None else f"+{ttl} seconds",
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
    if tool == "read_thread":
        return _thread(db, actor, args)
    if tool == "search_messages":
        return _search(db, actor, args)
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


def _identify(db: sqlite3.Connection, root: str, name: str) -> dict:
    """Resolves a registered participant to its own store identity."""
    row = db.execute(
        "SELECT a.id,a.project_id,a.name FROM agents a "
        "JOIN projects p ON p.id=a.project_id WHERE p.human_key=? AND a.name=?",
        (root, name),
    ).fetchone()
    if not row:
        raise BridgeError("This participant has no registered mail yet.")
    return dict(row)


def read_thread(
    home: Path, root: str, name: str, thread: str, after: int = 0
) -> dict:
    """Reads one thread's mail as a registered participant.

    Operator reads are not served MCP calls, so they hold no write lock and
    record no tool event.

    Args:
        home: Private bridge state root.
        root: Canonical project key registered with the store.
        name: Registered identity whose own mail is read.
        thread: Thread identifier to read.
        after: Last message identifier already read.

    Returns:
        One page of the thread in send order, with paging state.

    Raises:
        BridgeError: If no store exists or the participant is unregistered.
    """
    if not (home / DATABASE).exists():
        raise BridgeError("No coordination store yet; run agent-parley up.")
    with connect(home) as db:
        actor = _identify(db, root, name)
        return _thread(db, actor, {"thread_id": thread, "after_id": after})


def search_messages(
    home: Path,
    root: str,
    name: str,
    query: str,
    limit: int = MAX_SEARCH_HITS,
) -> dict:
    """Searches a registered participant's own mail for text.

    Args:
        home: Private bridge state root.
        root: Canonical project key registered with the store.
        name: Registered identity whose own mail is searched.
        query: Text to match against subjects and bodies.
        limit: Maximum hits reported.

    Returns:
        Matching messages newest first, naming the index that answered.

    Raises:
        BridgeError: If no store exists or the participant is unregistered.
    """
    if not (home / DATABASE).exists():
        raise BridgeError("No coordination store yet; run agent-parley up.")
    with connect(home) as db:
        actor = _identify(db, root, name)
        return _search(db, actor, {"query": query, "limit": limit})


def speak(
    home: Path,
    root: str,
    name: str,
    subject: str,
    body: str,
    key: str,
    *,
    ack: bool = False,
) -> dict:
    """Delivers one supervising operator's message to a registered lane.

    The message takes the ordinary send path, so it is deduplicated by its
    key, can require acknowledgement, and is read back beside peer traffic.
    The operator row carries no credential digest, so it never resolves a
    bearer token and no served session can write in its name.

    Args:
        home: Private bridge state root.
        root: Canonical project key registered with the store.
        name: Registered identity of the participant being addressed.
        subject: Subject line shown in that participant's inbox.
        body: Message body the participant reads.
        key: Idempotency key; an identical resend returns the original.
        ack: Whether the participant must acknowledge the message.

    Returns:
        The delivered message identifier, carrying ``duplicate`` when this key
        already named exactly this message.

    Raises:
        BridgeError: If the project or the addressed participant is not
            registered, or the message fails validation.
    """
    if not (home / DATABASE).exists():
        raise BridgeError(NO_PROJECT)
    with connect(home, write=True) as db:
        project = db.execute(
            "SELECT id FROM projects WHERE human_key=?", (root,)
        ).fetchone()
        if not project:
            raise BridgeError(NO_PROJECT)
        registered = db.execute(
            "SELECT id FROM agents WHERE project_id=? AND name=?",
            (project[0], name),
        ).fetchone()
        if not registered:
            raise BridgeError(
                f"{name} has not registered with the coordination store; "
                "launch that participant once with agent-parley run."
            )
        db.execute(
            "INSERT INTO agents(project_id,name) VALUES (?,?) "
            "ON CONFLICT(project_id,name) DO NOTHING",
            (project[0], OPERATOR),
        )
        actor = db.execute(
            "SELECT id,project_id,name FROM agents "
            "WHERE project_id=? AND name=?",
            (project[0], OPERATOR),
        ).fetchone()
        return _send(
            db,
            dict(actor),
            {
                "to": [name],
                "subject": subject,
                "body_md": body,
                "idempotency_key": key,
                "ack_required": ack,
            },
        )


def usage(home: Path, root: str) -> dict[str, dict]:
    """Reports retained tool events and held leases for one project.

    Args:
        home: Private bridge state root.
        root: Canonical project key registered with the store.

    Returns:
        Mapping of registered identity to served calls, rejected calls,
        returned bytes, held leases, how many of those leases are past a
        declared time to live, and the age of its oldest held lease. A stale
        lease is still held and still counted; nothing releases it on its
        owner's behalf. Counts cover retained events only; older events are
        retired.
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
            report[row["name"]] = {
                **dict(row),
                "leases": 0,
                "stale_leases": 0,
                "lease_age": 0,
            }
        for row in db.execute(
            "SELECT a.name AS name,count(*) AS leases,"
            "coalesce(sum(f.expires_ts IS NOT NULL "
            "AND f.expires_ts<=CURRENT_TIMESTAMP),0) AS stale_leases,"
            "cast(strftime('%s','now')-strftime('%s',min(f.created_ts)) "
            "AS INTEGER) AS lease_age FROM file_reservations f "
            "JOIN agents a ON a.id=f.agent_id "
            "JOIN projects p ON p.id=a.project_id WHERE p.human_key=? "
            "AND f.released_ts IS NULL GROUP BY a.id",
            (root,),
        ):
            report.setdefault(row["name"], {}).update(
                leases=row["leases"],
                stale_leases=row["stale_leases"],
                lease_age=max(0, row["lease_age"] or 0),
            )
    return report
