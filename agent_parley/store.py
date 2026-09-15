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

from agent_parley import (
    attachments,
    forecast,
    issues,
    protocol,
    retries,
    roster,
)
from agent_parley.roster import OPERATOR
from agent_parley.state import BridgeError, lock

DATABASE = "bridge.sqlite3"
SCHEMA_VERSION = 9
SCHEMA_ABSENT = "absent"
SCHEMA_BEHIND = "needs migration"
SCHEMA_CURRENT = "ok"
SCHEMA_UNSUPPORTED = "unsupported"
SCHEMA_USABLE = frozenset({SCHEMA_ABSENT, SCHEMA_CURRENT})
BUSY_TIMEOUT = 5.0
MAX_BODY_BYTES = 4096
MAX_RESULT_BYTES = 8192
MAX_RECIPIENTS = 16
MAX_ROSTER = 32
MAX_EVENT_ROWS = 2000
MAX_THREAD_PAGE = 10
MAX_SEARCH_HITS = 5
MAX_QUERY_BYTES = 160
MAX_REPEATS = 24
SCHEDULE_FIELDS = (
    "id",
    "kind",
    "recipient",
    "actor",
    "subject",
    "body_md",
    "issue",
    "dedup_key",
    "sequence",
    "ack_required",
    "ack_within",
    "not_before",
    "condition",
    "unless_reported",
    "every_seconds",
    "repeats_left",
    "created_ts",
    "delivered_ts",
)
PREVIEW_CHARACTERS = 240
READ_ONLY = (
    "fetch_inbox",
    "list_participants",
    "read_attachment",
    "read_thread",
    "search_decisions",
    "search_messages",
)
ATTACHED = ("send_message", "read_attachment")
PRESENCE_WARNINGS = {
    "idle": ("idle", "idle; wake requested"),
    "stopped": ("unreachable", "unreachable"),
    "unreachable": ("unreachable", "unreachable"),
}
RETRIED = {
    "acknowledge_message": ("message_id",),
    "mark_message_read": ("message_id",),
    "file_reservation_paths": ("paths", "ttl_seconds", "exclusive", "reason"),
    "release_file_reservations": (),
}
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
 ack_deadline_ts TEXT, claim_id TEXT, decision INTEGER NOT NULL DEFAULT 0,
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
 expires_ts TEXT, released_ts TEXT, claim_id TEXT);
CREATE INDEX IF NOT EXISTS leases ON file_reservations(project_id,expires_ts)
 WHERE released_ts IS NULL;
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id),
 agent_id INTEGER NOT NULL REFERENCES agents(id), tool TEXT NOT NULL,
 outcome TEXT NOT NULL, duration_ms INTEGER NOT NULL,
 result_bytes INTEGER NOT NULL,
 created_ts TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE INDEX IF NOT EXISTS history ON events(project_id,id);
CREATE TABLE IF NOT EXISTS participant_presence (
 agent_id INTEGER PRIMARY KEY REFERENCES agents(id), state TEXT NOT NULL,
 process_alive INTEGER NOT NULL, observed_ts REAL NOT NULL, last_active REAL);
CREATE TABLE IF NOT EXISTS idempotent_calls (
 id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id),
 agent_id INTEGER NOT NULL REFERENCES agents(id), tool TEXT NOT NULL,
 idempotency_key TEXT NOT NULL, request_digest TEXT NOT NULL,
 outcome TEXT NOT NULL, result_json TEXT NOT NULL,
 created_ts TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE UNIQUE INDEX IF NOT EXISTS retried
 ON idempotent_calls(agent_id,tool,idempotency_key);
CREATE TABLE IF NOT EXISTS scheduled_deliveries (
 id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id),
 kind TEXT NOT NULL, recipient TEXT NOT NULL, actor TEXT NOT NULL DEFAULT '',
 subject TEXT NOT NULL DEFAULT '', body_md TEXT NOT NULL DEFAULT '',
 issue TEXT NOT NULL DEFAULT '', dedup_key TEXT NOT NULL DEFAULT '',
 sequence INTEGER NOT NULL DEFAULT 0, ack_required INTEGER NOT NULL DEFAULT 0,
 ack_within REAL, not_before REAL, condition TEXT NOT NULL DEFAULT '',
 unless_reported INTEGER NOT NULL DEFAULT 0, every_seconds REAL,
 repeats_left INTEGER NOT NULL DEFAULT 1, created_ts REAL NOT NULL,
 delivered_ts REAL, cancelled_ts REAL);
CREATE INDEX IF NOT EXISTS undelivered ON scheduled_deliveries(project_id)
 WHERE delivered_ts IS NULL AND cancelled_ts IS NULL;
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
def connect(
    home: Path, *, write: bool = False, timeout: float = BUSY_TIMEOUT
) -> Iterator[sqlite3.Connection]:
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
    Telemetry can opt out of waiting with a zero timeout.

    Transaction acquisition uses a monotonic deadline rather than SQLite's
    accumulated sleep budget, which can overrun wall time on macOS. A writer
    holds its reservation before yielding, so its statements do not need a
    second busy wait.
    """
    db = sqlite3.connect(home / DATABASE, timeout=0 if write else timeout)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                break
            except sqlite3.OperationalError as exc:
                remaining = deadline - time.monotonic()
                if (
                    getattr(exc, "sqlite_errorcode", 0) & 0xFF
                    != sqlite3.SQLITE_BUSY
                    or remaining <= 0
                ):
                    raise
                time.sleep(min(0.05, remaining))
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


@contextlib.contextmanager
def reading(
    home: Path, db: sqlite3.Connection | None = None
) -> Iterator[sqlite3.Connection]:
    """Yields a caller's read transaction, or opens and closes its own.

    A caller that answers several questions about one project in a single
    frame opens one transaction and passes it in, so every answer describes
    the same instant and the frame pays one connection instead of one per
    question. A caller with nothing to share passes nothing and keeps the
    previous bounded read.

    Args:
        home: Private bridge state root.
        db: Open read transaction to reuse, or ``None`` to open one.

    Yields:
        The connection the caller's statements run on.
    """
    if db is not None:
        yield db
        return
    with connect(home) as own:
        yield own


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

    Upgrading a store written before the decision log marks every stored
    message as ordinary mail, so nothing a lane sent in private becomes
    project-wide by being upgraded.
    """
    with lock(home / "store.lock"):
        path = home / DATABASE
        with contextlib.closing(
            sqlite3.connect(path, timeout=BUSY_TIMEOUT)
        ) as db:
            path.chmod(0o600)
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise BridgeError(
                    "Unsupported store schema; use a newer bridge."
                )
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA foreign_keys=ON")
            db.executescript(SCHEMA)
            with db:
                db.execute("BEGIN IMMEDIATE")
                if version == SCHEMA_VERSION:
                    _add_message_search(db)
                    return
                _add_ack_deadline(db)
                _add_decision_flag(db)
                if version == 1:
                    _add_reservation_created(db)
                _rebuild_reservations(db)
                _add_claim_correlation(db)
                _add_message_search(db)
                legacy = home / "mail.sqlite3"
                if version == 0 and legacy.exists():
                    _import_legacy(db, legacy)
                if version == 1:
                    db.execute(
                        "UPDATE file_reservations "
                        "SET created_ts=CURRENT_TIMESTAMP "
                        "WHERE created_ts IS NULL"
                    )
                _open_threads(db)
                _rebuild_search(db)
                db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


def _add_ack_deadline(db: sqlite3.Connection) -> None:
    """Adds the optional acknowledgement deadline to an older store.

    The column is additive and nullable, so every stored message keeps its
    text, its recipients and its timestamps, and a message delivered before
    the upgrade simply carries no deadline.
    """
    columns = {row[1] for row in db.execute("PRAGMA table_info(messages)")}
    if "ack_deadline_ts" not in columns:
        db.execute("ALTER TABLE messages ADD COLUMN ack_deadline_ts TEXT")


def _add_decision_flag(db: sqlite3.Connection) -> None:
    """Adds the decision marker and its index to an older store.

    The column is additive and defaults to zero, so every message stored
    before the upgrade stays ordinary mail that only its sender and its
    recipients can read. The partial index covers the decision log alone, so
    the far larger body of private mail costs nothing to keep out of it.
    """
    columns = {row[1] for row in db.execute("PRAGMA table_info(messages)")}
    if "decision" not in columns:
        db.execute(
            "ALTER TABLE messages ADD COLUMN decision "
            "INTEGER NOT NULL DEFAULT 0"
        )
    db.execute(
        "CREATE INDEX IF NOT EXISTS decisions ON messages(project_id,id) "
        "WHERE decision=1"
    )


def _add_claim_correlation(db: sqlite3.Connection) -> None:
    """Adds the claim identifier a record was made under, where it is missing.

    The column is additive and nullable. A message or a reservation written
    before the upgrade keeps every value it had and simply carries no claim,
    which history reports as unknown rather than inventing a correlation that
    was never recorded.

    It runs after the reservation table has been rebuilt into its current
    shape, because that rebuild copies a fixed column list and would otherwise
    drop a column added before it.
    """
    for table in ("messages", "file_reservations"):
        columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        if "claim_id" not in columns:
            db.execute(f"ALTER TABLE {table} ADD COLUMN claim_id TEXT")


def _add_reservation_created(db: sqlite3.Connection) -> None:
    """Adds the reservation creation column to a schema-1 store."""
    columns = {
        row[1] for row in db.execute("PRAGMA table_info(file_reservations)")
    }
    if "created_ts" not in columns:
        db.execute("ALTER TABLE file_reservations ADD COLUMN created_ts TEXT")


def _add_message_search(db: sqlite3.Connection) -> None:
    """Creates the full-text index where the SQLite build provides FTS5.

    Startup reconciles triggers even at the current schema version. Without
    FTS5, old triggers must be removed so ordinary message writes still work.
    When FTS5 returns, missing triggers cause an index rebuild to include mail
    delivered by the interpreter that could not maintain it.
    """
    if not _fts_available(db):
        db.execute("DROP TRIGGER IF EXISTS message_indexed")
        db.execute("DROP TRIGGER IF EXISTS message_unindexed")
        return
    triggers = db.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='trigger' "
        "AND name IN ('message_indexed','message_unindexed')"
    ).fetchone()[0]
    statement = ""
    for line in SEARCH_SCHEMA.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            db.execute(statement)
            statement = ""
    if triggers != 2:
        _rebuild_search(db)


def _fts_available(db: sqlite3.Connection) -> bool:
    """Reports whether this interpreter can use and maintain an FTS5 index."""
    return any(row[0] == "fts5" for row in db.execute("PRAGMA module_list"))


def _searchable(db: sqlite3.Connection) -> bool:
    """Reports whether this store carries a full-text index."""
    return _fts_available(db) and bool(
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
    db.execute(
        "CREATE TABLE rebuilt_reservations ("
        " id INTEGER PRIMARY KEY,"
        " project_id INTEGER NOT NULL REFERENCES projects(id),"
        " agent_id INTEGER NOT NULL REFERENCES agents(id),"
        " path_pattern TEXT NOT NULL, exclusive INTEGER NOT NULL,"
        " reason TEXT DEFAULT '',"
        " created_ts TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,"
        " expires_ts TEXT, released_ts TEXT)"
    )
    db.execute(
        "INSERT INTO rebuilt_reservations (id,project_id,agent_id,"
        "path_pattern,exclusive,reason,created_ts,expires_ts,released_ts) "
        "SELECT id,project_id,agent_id,path_pattern,exclusive,reason,"
        "coalesce(created_ts,CURRENT_TIMESTAMP),expires_ts,released_ts "
        "FROM file_reservations"
    )
    db.execute("DROP TABLE file_reservations")
    db.execute("ALTER TABLE rebuilt_reservations RENAME TO file_reservations")
    db.execute(
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
    """Resolves a bearer credential to exactly one project and lane.

    The canonical project key travels with the actor so a caller outside the
    store can find the project's manifest without a checkout to resolve its
    directory key from.
    """
    with connect(home) as db:
        row = db.execute(
            "SELECT a.id,a.project_id,a.name,p.human_key AS project "
            "FROM agents a JOIN projects p ON p.id=a.project_id "
            "WHERE a.token_digest=?",
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


def _send(
    db: sqlite3.Connection,
    actor: dict,
    args: dict,
    claim: str = "",
    directory: Path | None = None,
) -> dict:
    """Atomically delivers an idempotent message to authorized recipients.

    A send naming ``reply_to`` joins the thread of the message it answers. A
    send naming neither ``reply_to`` nor ``thread_id`` opens its own thread,
    so every delivered message belongs to exactly one thread.

    A body above ``MAX_BODY_BYTES`` is spilled whole to an attachment keyed
    by the message identifier, and the stored body is the bounded slice
    ending with that reference and the byte count, so a recipient's inbox
    and checkpoint previews stay exactly as bounded as before.

    A send marked ``decision`` is additionally recorded in the project-wide
    decision log, which every registered participant can search. Only that
    marking widens what a message reveals; ordinary mail stays readable by
    its sender and its recipients alone. A decision addresses recipients
    like any other message, and may address none, which records the decision
    without putting it in an inbox.
    """
    subject = _text(args.get("subject"), "subject", 160)
    body = _text(
        args.get("body_md"), "body_md", attachments.MAX_ATTACHMENT_BYTES
    )
    oversized = len(body.encode()) > MAX_BODY_BYTES
    if oversized and directory is None:
        raise BridgeError(
            f"body_md exceeds its {MAX_BODY_BYTES}-byte budget and this "
            "project keeps no attachments."
        )
    thread = _text(args.get("thread_id", ""), "thread_id", 80, empty=True)
    key = _text(args.get("idempotency_key"), "idempotency_key", 80)
    ack = _flag(args.get("ack_required", False), "ack_required")
    decision = _flag(args.get("decision", False), "decision")
    within = args.get("ack_within")
    if within is not None:
        within = _number(within, "ack_within", 1, 86400 * 30)
    if "reply_to" in args:
        if thread:
            raise BridgeError("Answer with reply_to or thread_id, not both.")
        thread = _answered_thread(db, actor, args["reply_to"])
    thread = thread or _opened_thread(actor, key)
    recipients = args.get("to", [] if decision else None)
    lowest = 0 if decision else 1
    if (
        not isinstance(recipients, list)
        or not lowest <= len(recipients) <= MAX_RECIPIENTS
    ):
        raise BridgeError(
            f"to must contain {lowest}..{MAX_RECIPIENTS} registered "
            "participants."
        )
    ids = []
    for recipient in recipients:
        name = _text(recipient, "recipient", 80)
        row = db.execute(
            "SELECT id,token_digest FROM agents WHERE project_id=? AND name=?",
            (actor["project_id"], name),
        ).fetchone()
        if not row:
            raise BridgeError("Recipient is not registered in your project.")
        if row["token_digest"] is None:
            raise BridgeError(
                f"Recipient {name!r} cannot receive mail: "
                "operator or retired participant has no active credential."
            )
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
        stored = (
            attachments.bounded("message", existing["id"], body, MAX_BODY_BYTES)
            if oversized
            else body
        )
        if (
            existing["subject"],
            existing["body_md"],
            existing["thread_id"],
            existing["ack_required"],
            bool(existing["decision"]),
        ) != (subject, stored, thread, ack, decision) or previous != set(ids):
            raise BridgeError("Idempotency key already names another message.")
        return {
            "id": existing["id"],
            "thread_id": existing["thread_id"],
            "duplicate": True,
        }
    cursor = db.execute(
        "INSERT INTO messages(project_id,sender_id,subject,body_md,"
        "thread_id,ack_required,dedup_key,ack_deadline_ts,claim_id,decision) "
        "VALUES (?,?,?,?,?,?,?,datetime('now',?),?,?)",
        (
            actor["project_id"],
            actor["id"],
            subject,
            body,
            thread,
            ack,
            key,
            None if within is None else f"+{int(within)} seconds",
            claim or None,
            decision,
        ),
    )
    message_id = cursor.lastrowid
    db.executemany(
        "INSERT INTO message_recipients(message_id,agent_id) VALUES (?,?)",
        [(message_id, recipient) for recipient in set(ids)],
    )
    result = {"id": message_id, "thread_id": thread}
    if decision:
        result["decision"] = True
    if oversized and directory is not None:
        stored, ref = attachments.spill(
            directory,
            "message",
            message_id,
            body,
            MAX_BODY_BYTES,
            actor["name"],
            [str(name) for name in recipients],
        )
        db.execute(
            "UPDATE messages SET body_md=? WHERE id=?", (stored, message_id)
        )
        result["attachment"] = ref
        result["attachment_bytes"] = len(body.encode())
    return result


def _roster(db: sqlite3.Connection, actor: dict) -> dict:
    """Lists this project's participants so peers stay addressable."""
    rows = db.execute(
        "SELECT name,substr(task_description,1,160) AS task_description,"
        "last_active_ts FROM agents WHERE project_id=? "
        "AND token_digest IS NOT NULL ORDER BY name LIMIT ?",
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
    unread = _flag(args.get("unread", False), "unread")
    unacknowledged = _flag(args.get("unacknowledged", False), "unacknowledged")
    offset = _number(args.get("body_offset", 0), "body_offset", 0, 10**9)
    rows = db.execute(
        "SELECT m.id,a.name AS sender,m.thread_id,m.subject,m.body_md,"
        "m.ack_required,r.read_ts,r.ack_ts "
        "FROM messages m JOIN agents a ON a.id=m.sender_id "
        "JOIN message_recipients r ON r.message_id=m.id "
        "WHERE r.agent_id=? AND m.id>? "
        "AND (?=0 OR r.read_ts IS NULL) "
        "AND (?=0 OR (m.ack_required=1 AND r.ack_ts IS NULL)) "
        "ORDER BY m.id LIMIT ?",
        (actor["id"], after, unread, unacknowledged, limit + 1),
    ).fetchall()
    result: dict = {"messages": [], "next_after_id": after, "has_more": False}
    for row in rows[:limit]:
        item = dict(row)
        if not bodies:
            item.pop("body_md")
        item["subject"] = item["subject"][:160]
        if bodies:
            full = item["body_md"]
            item["body_md"] = full[offset : offset + 1024]
            if offset + 1024 < len(full):
                item["next_body_offset"] = offset + 1024
        candidate = {
            **result,
            "messages": [*result["messages"], item],
            "next_after_id": row["id"],
        }
        if (
            len(json.dumps(candidate, ensure_ascii=False).encode())
            > MAX_RESULT_BYTES
        ):
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
        candidate = {
            **result,
            "messages": [*reported, dict(row)],
            "has_more": False,
        }
        if "next_after_id" in result:
            candidate["next_after_id"] = row["id"]
        if (
            len(json.dumps(candidate, ensure_ascii=False).encode())
            > MAX_RESULT_BYTES
        ):
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


DECISION_SCOPE = (
    "JOIN agents a ON a.id=m.sender_id WHERE m.project_id=? "
    "AND m.decision=1 AND (?=0 OR m.created_ts>=datetime('now',?)) "
)
DECISION_INDEX = (
    "FROM message_search JOIN messages m ON m.id=message_search.rowid "
)


def _decision_rows(
    db: sqlite3.Connection,
    project_id: int,
    query: str,
    window: int,
    limit: int,
    indexed: bool,
) -> list[sqlite3.Row]:
    """Reads one page of a project's decision log, newest first.

    Args:
        db: Open transaction owned by the caller.
        project_id: Project whose decision log is read.
        query: Text to match, or empty to read the newest decisions.
        window: Seconds back the page may reach, or zero for the whole log.
        limit: Maximum decisions reported.
        indexed: Whether a full-text index can answer this query.

    Returns:
        Candidate rows, one beyond the limit where further ones exist.
    """
    scope = (PREVIEW_CHARACTERS, project_id, window, f"-{window} seconds")
    if not query:
        return db.execute(
            MAIL_COLUMNS
            + "FROM messages m "
            + DECISION_SCOPE
            + "ORDER BY m.id DESC LIMIT ?",
            (*scope, limit + 1),
        ).fetchall()
    if indexed:
        return db.execute(
            MAIL_COLUMNS
            + DECISION_INDEX
            + DECISION_SCOPE
            + "AND message_search MATCH ? ORDER BY m.id DESC LIMIT ?",
            (*scope, _phrase(query), limit + 1),
        ).fetchall()
    pattern = f"%{_wildcards(query)}%"
    return db.execute(
        MAIL_COLUMNS
        + "FROM messages m "
        + DECISION_SCOPE
        + "AND (m.subject LIKE ? ESCAPE '\\' OR m.body_md LIKE ? ESCAPE '\\') "
        "ORDER BY m.id DESC LIMIT ?",
        (*scope, pattern, pattern, limit + 1),
    ).fetchall()


def _decisions(db: sqlite3.Connection, actor: dict, args: dict) -> dict:
    """Reads this project's decision log, newest decision first.

    Every registered participant of the project reads the same log, because a
    decision is recorded precisely so a lane that was neither sender nor
    recipient stops repeating or contradicting it. Ordinary mail is untouched
    by this scope and stays private to its sender and its recipients.

    An empty query lists the newest decisions rather than matching text, and
    ``since`` bounds the page to decisions recorded within that many seconds.
    Where the SQLite build provides no full-text index a query is matched as
    a literal substring, exactly as ordinary mail search degrades.
    """
    query = _text(args.get("query", ""), "query", MAX_QUERY_BYTES, empty=True)
    window = _number(args.get("since", 0), "since", 0, 10**9)
    limit = _number(
        args.get("limit", MAX_SEARCH_HITS), "limit", 1, MAX_SEARCH_HITS
    )
    indexed = bool(query) and _searchable(db)
    if not query:
        index = "recent"
    else:
        index = "fts5" if indexed else "substring"
    rows = _decision_rows(
        db, actor["project_id"], query, window, limit, indexed
    )
    result: dict = {
        "query": query,
        "since_seconds": window,
        "index": index,
    }
    messages = _bounded(result, rows[:limit])
    result["messages"] = messages
    result["has_more"] = len(rows) > len(messages)
    return result


def named_resource(pattern: str) -> bool:
    """Reports whether a reservation key names a resource rather than a path.

    A resource is written with a scheme, such as ``port:5432`` or
    ``suite:integration``, and a repository-relative path never carries one,
    so the two key types cannot be confused for each other.

    Args:
        pattern: Reservation key as the caller wrote it.

    Returns:
        Whether the key names a resource.
    """
    scheme, separator, _ = pattern.partition(":")
    return bool(separator) and "/" not in scheme


def overlapping(pattern: str, other: str) -> bool:
    """Reports whether two reservation keys can name the same thing.

    Named resources overlap only when spelled identically. Paths overlap when
    either matches the other as a glob, when one lies under the other as a
    directory, or when both are globs, which the matcher cannot compare and
    so treats as a collision. A plain path checked against a pattern uses the
    same rule, so a dirty file in a checkout is matched exactly the way a
    competing reservation would be.

    Args:
        pattern: Reservation key or repository-relative path.
        other: Reservation key it is compared against.

    Returns:
        Whether the two keys overlap.
    """
    if named_resource(pattern) or named_resource(other):
        return pattern == other
    both_globs = any(c in pattern for c in "*?[") and any(
        c in other for c in "*?["
    )
    return (
        both_globs
        or fnmatch.fnmatchcase(pattern, other)
        or fnmatch.fnmatchcase(other, pattern)
        or pattern.startswith(other.rstrip("/") + "/")
        or other.startswith(pattern.rstrip("/") + "/")
    )


def _reserve(
    db: sqlite3.Connection,
    actor: dict,
    args: dict,
    declared: frozenset[str] | None = None,
    claim: str = "",
    commits: list[list[str]] | None = None,
) -> dict:
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

    A key written with a scheme, such as ``port:5432``, ``db:local``,
    ``suite:integration`` or ``device:android-1``, reserves a named resource
    rather than a path. Lanes collide on those as readily as on files, and a
    worktree isolates neither. A named resource conflicts on an exact match
    only: no glob, no prefix and no path containment applies to it, because a
    port number is not a directory. Where a project declares which resources
    exist, an undeclared name is refused with that list; where it declares
    none, every well-formed name is accepted.

    Args:
        db: Open transaction owned by the caller.
        actor: Authenticated project and lane.
        args: Validated tool arguments.
        declared: Resources this project declared, or None when it declared
            none and any well-formed name is acceptable.
        claim: Claim identifier the lane holds, recorded beside each lease so
            history can follow one piece of work. Empty records no claim.
        commits: Files per recent commit of the base checkout, read before
            the transaction opened, or None when no history is available.

    Returns:
        The granted leases, or the conflicts that granted nothing. A grant
        carries ``forecast`` when a file that habitually changes together
        with a granted path is held by a peer: each entry names the path,
        the peer and the shared commit count. The forecast is advisory and
        bounded like the conflicts; it never withholds a grant.

    Raises:
        BridgeError: If a key is malformed, names an undeclared resource, or
            the lane already holds the maximum number of leases.
    """
    paths = args.get("paths")
    if not isinstance(paths, list) or not 1 <= len(paths) <= 16:
        raise BridgeError(
            "paths must contain 1..16 repository-relative paths or named "
            "resources such as port:5432."
        )
    ttl = args.get("ttl_seconds")
    if ttl is not None:
        ttl = _number(ttl, "ttl_seconds", 30, 3600)
    exclusive = _flag(args.get("exclusive", True), "exclusive")
    reason = _text(args.get("reason", ""), "reason", 160, empty=True)
    keys = []
    for pattern in paths:
        _text(pattern, "path", 240)
        if named_resource(pattern):
            if not roster.RESOURCE.fullmatch(pattern):
                raise BridgeError(
                    f"{pattern!r} is not a named resource; write a scheme and "
                    "a name, such as port:5432 or suite:integration."
                )
            if declared is not None and pattern not in declared:
                raise BridgeError(
                    f"{pattern!r} is not declared for this project. Declared "
                    "resources: " + (", ".join(sorted(declared)) or "none")
                )
            keys.append(pattern)
            continue
        parts = PurePosixPath(pattern)
        if (
            parts.is_absolute()
            or ".." in parts.parts
            or "\\" in pattern
            or str(parts) == "."
        ):
            raise BridgeError("Reservations require repository-relative paths.")
        keys.append(str(parts))
    paths = keys
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
            if (exclusive or lease["exclusive"]) and overlapping(
                pattern, other
            ):
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
            "exclusive,reason,expires_ts,claim_id) VALUES (?,?,?,?,?,"
            "datetime('now',?),?)",
            (
                actor["project_id"],
                actor["id"],
                pattern,
                exclusive,
                reason,
                None if ttl is None else f"+{ttl} seconds",
                claim or None,
            ),
        )
        granted.append({"id": cursor.lastrowid, "path": pattern})
    result = {"granted": granted, "conflicts": []}
    held: dict[str, list[str]] = {}
    for lease in leases:
        held.setdefault(lease["name"], []).append(lease["path_pattern"])
    likely = forecast.collisions(
        forecast.cochanges(commits or [], paths, overlapping), held, overlapping
    )
    advisory: list[dict] = []
    for entry in likely:
        candidate = {**result, "forecast": [*advisory, entry]}
        if (
            len(json.dumps(candidate, ensure_ascii=False).encode())
            > MAX_RESULT_BYTES
        ):
            break
        advisory.append(entry)
    if advisory:
        result["forecast"] = advisory
    return result


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

    Rejected calls and read-only results attempt a separate write transaction
    for telemetry and retention. This acquisition never waits for a writer;
    a busy or unavailable store loses the record rather than delaying the call.
    """
    with contextlib.suppress(sqlite3.Error):
        with connect(home, write=True, timeout=0) as db:
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
    except (BridgeError, sqlite3.OperationalError) as exc:
        _observe(home, actor, tool, "error", started, 0)
        if isinstance(exc, BridgeError):
            _refused(home, actor, tool, args, exc)
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
    if tool == "send_message" and args.get("ack_required"):
        with contextlib.suppress(sqlite3.Error):
            _recipient_warnings(home, actor, args, result)
    return result


def schema_version(home: Path) -> int:
    """Returns the schema version the store on disk declares.

    Args:
        home: Private bridge state root.

    Returns:
        The declared schema, or zero when no store has been created yet or the
        file cannot be opened. Reading never creates or upgrades a store.
    """
    path = home / DATABASE
    if not path.exists():
        return 0
    try:
        with contextlib.closing(sqlite3.connect(path)) as db:
            return int(db.execute("PRAGMA user_version").fetchone()[0])
    except sqlite3.Error:
        return 0


def schema_state(schema: int) -> str:
    """Classifies a store schema against the schema this build writes.

    A store behind this build is not compatible with it. Every process running
    this code queries columns the older store does not have, so reporting the
    pair as merely older would describe a working system during an outage.
    Migration happens when the service opens the store, so the state names
    what is missing rather than performing it.

    Args:
        schema: Schema the store on disk declares, or zero when none exists.

    Returns:
        `SCHEMA_ABSENT` when no store has been created, `SCHEMA_CURRENT` when
        the store matches this build, `SCHEMA_BEHIND` when it predates this
        build and has not been migrated, and `SCHEMA_UNSUPPORTED` when a newer
        build wrote it, which is refused rather than downgraded.
    """
    if not schema:
        return SCHEMA_ABSENT
    if schema < SCHEMA_VERSION:
        return SCHEMA_BEHIND
    if schema > SCHEMA_VERSION:
        return SCHEMA_UNSUPPORTED
    return SCHEMA_CURRENT


def remedy(state: str) -> str:
    """Names the operator action that makes a store state usable again.

    Every surface that reports an unusable store prescribes the same repair,
    so the mapping lives beside the classification rather than being restated
    wherever a report is rendered.

    Args:
        state: Classification returned by `schema_state`.

    Returns:
        The command that repairs the store, or an empty string when the state
        needs no action.
    """
    if state == SCHEMA_BEHIND:
        return protocol.MIGRATE
    if state == SCHEMA_UNSUPPORTED:
        return protocol.UPGRADE
    return ""


def refused(home: Path, actor: dict, tool: str) -> None:
    """Records a call refused at the transport boundary.

    A refusal that never reaches a tool still cost the lane its turn, so it is
    counted where every other denial is counted rather than disappearing.

    Args:
        home: Private bridge state root.
        actor: Authenticated project and lane.
        tool: Tool the refused call named, or an empty name.
    """
    _observe(home, actor, tool or "unknown", "error", time.monotonic(), 0)


def _refused(
    home: Path, actor: dict, tool: str, args: dict, exc: BridgeError
) -> None:
    """Records a refusal so a retry is refused identically, never widened.

    A refusal changed nothing, so its transaction has already rolled back and
    the key is recorded afterwards. An interruption before that record leaves
    the key absent, and the retry is evaluated again and refused again, so the
    window costs a repeated evaluation rather than a replayed effect.
    Authorization decided after the first refusal never reaches a call that
    carries the refused key, because the recorded refusal answers it.

    Args:
        home: Private bridge state root.
        actor: Authenticated project and lane.
        tool: Coordination tool that refused the call.
        args: Arguments the refused call carried.
        exc: Refusal reported to the caller.
    """
    if tool not in RETRIED:
        return
    with contextlib.suppress(BridgeError, sqlite3.Error):
        key = retries.validate(args.get("idempotency_key"))
        if not key:
            return
        fingerprint = retries.digest(
            tool, {name: args.get(name) for name in RETRIED[tool]}
        )
        with connect(home, write=True) as db:
            _retain(db, actor, tool, key, fingerprint, retries.DENIED, str(exc))


def _recipient_warnings(
    home: Path, actor: dict, args: dict, result: dict
) -> None:
    """Adds observed availability without failing an already committed send.

    A recipient whose launcher is alive but between turns is reported as idle
    and its summary says a wake was requested, because the supervision poll
    asks an idle lane with a backlog to take its turn. Only a recipient whose
    recorded session process is gone is called unreachable, so a peer reading
    the result never treats a live lane as absent. A presence row written by
    an earlier build, before idle existed, still reads as unreachable.

    Args:
        home: Private bridge state root.
        actor: Authenticated project and lane.
        args: Arguments the committed send carried.
        result: Send result the warnings are added to.
    """
    with connect(home) as db:
        warnings = []
        for name in args["to"]:
            row = db.execute(
                "SELECT s.state,s.observed_ts FROM agents a LEFT JOIN "
                "participant_presence s ON s.agent_id=a.id "
                "WHERE a.project_id=? AND a.name=?",
                (actor["project_id"], name),
            ).fetchone()
            observed = PRESENCE_WARNINGS.get(row["state"]) if row else None
            if observed:
                state, detail = observed
                warnings.append(
                    {
                        "recipient": name,
                        "state": state,
                        "summary": f"queued for {name} ({detail})",
                        "observed_ts": row["observed_ts"],
                    }
                )
        if warnings:
            result["recipient_warnings"] = warnings


def _dispatch(
    home: Path, actor: dict, tool: str, args: dict, started: float
) -> dict:
    """Runs one coordination tool inside its own bounded transaction.

    A reservation is validated against the resources its project declared.
    That declaration lives in the project manifest beside the roster, which
    only a state directory can resolve, so it is read here and handed to the
    tool rather than looked up inside the transaction. The co-change history
    a reservation is forecast against is read the same way, before the
    transaction, so a Git read never holds the store's write lock.
    """
    declared = (
        declared_resources(home, str(actor.get("project", "")))
        if tool == "file_reservation_paths"
        else None
    )
    claim = (
        held_claim(home, str(actor.get("project", "")), actor["name"])
        if tool in ("file_reservation_paths", "send_message")
        else ""
    )
    commits = (
        cochange_history(home, str(actor.get("project", "")))
        if tool == "file_reservation_paths"
        else None
    )
    directory = (
        roster.locate(home, str(actor.get("project", "")))
        if tool in ATTACHED
        else None
    )
    with connect(home, write=tool not in READ_ONLY) as db:
        result = _serve(
            db, actor, tool, args, declared, claim, commits, directory
        )
        if tool not in READ_ONLY:
            _event(
                db,
                actor,
                tool,
                "conflict" if result.get("conflicts") else "ok",
                started,
                len(json.dumps(result, ensure_ascii=False).encode()),
            )
        return result


def declared_resources(home: Path, root: str) -> frozenset[str] | None:
    """Reads the named resources a project declared, if it declared any.

    Args:
        home: Private bridge state root.
        root: Canonical project key recorded in the manifest.

    Returns:
        The declared resources, or None when the project declares none and any
        well-formed name is acceptable. An unreadable manifest also reports
        None, because a declaration must be readable to restrict anything.
    """
    directory = roster.locate(home, root) if root else None
    if directory is None:
        return None
    try:
        resources = roster.read(directory)["resources"]
    except (BridgeError, OSError, ValueError):
        return None
    return frozenset(resources) if resources else None


def cochange_history(home: Path, root: str) -> list[list[str]]:
    """Reads the recent commit history a reservation is forecast against.

    Args:
        home: Private bridge state root.
        root: Canonical project key, which is the base checkout's path.

    Returns:
        Files per recent commit of the base checkout, or an empty list when
        the project is not registered or Git cannot answer in time.
    """
    directory = roster.locate(home, root) if root else None
    if directory is None:
        return []
    return forecast.history(root, directory)


def held_claim(home: Path, root: str, display: str) -> str:
    """Returns the claim identifier a lane currently works under.

    Every record a lane makes while it holds a claim carries that claim's
    identifier, so a later reading can follow one piece of work from the claim
    through its reservations, messages and reports to the pull request that
    ended it. A lane holding several claims is correlated to its most recent
    one, and a lane holding none records no correlation at all rather than a
    guessed one.

    Args:
        home: Private bridge state root.
        root: Canonical project key registered with the store.
        display: Registered identity behind the served call.

    Returns:
        The claim identifier, or an empty string when the lane holds no claim
        or the project state cannot be read.
    """
    directory = roster.locate(home, root) if root else None
    if directory is None:
        return ""
    try:
        manifest = roster.read(directory)
        ledger = issues.snapshot(directory)
    except (BridgeError, OSError, ValueError):
        return ""
    names = [
        name
        for name, participant in manifest["participants"].items()
        if participant["display"] == display
    ]
    if not names:
        return ""
    latest = (0.0, "")
    for record in ledger["issues"].values():
        if record.get("owner") not in names or not record.get("claim_id"):
            continue
        started = max(
            (
                float(entry.get("at", 0) or 0)
                for entry in record.get("history", [])
                if entry.get("claim_id") == record["claim_id"]
            ),
            default=0.0,
        )
        if started >= latest[0]:
            latest = (started, str(record["claim_id"]))
    return latest[1]


def _recorded(
    db: sqlite3.Connection, actor: dict, tool: str, key: str
) -> dict | None:
    """Reads what an earlier call carrying this key from this lane returned."""
    row = db.execute(
        "SELECT request_digest,outcome,result_json FROM idempotent_calls "
        "WHERE agent_id=? AND tool=? AND idempotency_key=?",
        (actor["id"], tool, key),
    ).fetchone()
    return (
        {
            "request_digest": row["request_digest"],
            "outcome": row["outcome"],
            "result": json.loads(row["result_json"]),
        }
        if row
        else None
    )


def _retain(
    db: sqlite3.Connection,
    actor: dict,
    tool: str,
    key: str,
    digest: str,
    outcome: str,
    result: object,
) -> None:
    """Records one served or refused call inside the transaction that ran it.

    Retention is bounded per participant at `retries.RETAINED_CALLS` keys, so
    a lane that retries indefinitely cannot grow the store without limit. The
    oldest keys are discarded first, and a key discarded after its window
    simply behaves as a first call again.
    """
    db.execute(
        "INSERT OR IGNORE INTO idempotent_calls(project_id,agent_id,tool,"
        "idempotency_key,request_digest,outcome,result_json) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            actor["project_id"],
            actor["id"],
            tool,
            key,
            digest,
            outcome,
            json.dumps(result, ensure_ascii=False),
        ),
    )
    db.execute(
        "DELETE FROM idempotent_calls WHERE agent_id=? AND id NOT IN "
        "(SELECT id FROM idempotent_calls WHERE agent_id=? "
        "ORDER BY id DESC LIMIT ?)",
        (actor["id"], actor["id"], retries.RETAINED_CALLS),
    )


def _serve(
    db: sqlite3.Connection,
    actor: dict,
    tool: str,
    args: dict,
    declared: frozenset[str] | None = None,
    claim: str = "",
    commits: list[list[str]] | None = None,
    directory: Path | None = None,
) -> dict:
    """Applies one validated coordination tool to the open transaction.

    A writing tool that carries an idempotency key is served once. A repeat
    from the same lane with the same key returns the first result and writes
    nothing further, and the key is recorded in this same transaction, so an
    interruption between the effect and its key cannot leave a replayable
    call. A key repeated with different arguments is refused by name.

    Marking a message read or acknowledged stamps only the timestamp that is
    still unset, so a retry or a later acknowledgement keeps the first reading
    and the first acknowledgement as recorded. Marking read never disturbs an
    acknowledgement already recorded against the same message.

    Args:
        db: Open transaction owned by the caller.
        actor: Authenticated project and lane.
        tool: Coordination tool named by the caller.
        args: Validated tool arguments.
        declared: Named resources this project declared, or None when it
            declared none.
        claim: Claim identifier the calling lane holds, recorded beside the
            records a tool writes. Empty records no claim.
        commits: Files per recent commit of the base checkout for a
            reservation forecast, or None when none was read.
        directory: Project state directory attachments live under, or None
            when the project has no registered manifest.

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
    key = retries.validate(args.get("idempotency_key"))
    if not key or tool not in RETRIED:
        return _effect(
            db, actor, tool, args, declared, claim, commits, directory
        )
    fingerprint = retries.digest(
        tool, {name: args.get(name) for name in RETRIED[tool]}
    )
    if recorded := _recorded(db, actor, tool, key):
        return retries.replayed(recorded, tool, key, fingerprint)
    result = _effect(db, actor, tool, args, declared, claim, commits, directory)
    _retain(db, actor, tool, key, fingerprint, retries.SERVED, result)
    return result


def _effect(
    db: sqlite3.Connection,
    actor: dict,
    tool: str,
    args: dict,
    declared: frozenset[str] | None,
    claim: str,
    commits: list[list[str]] | None = None,
    directory: Path | None = None,
) -> dict:
    """Performs the effect of one coordination tool without retry bookkeeping.

    Args:
        db: Open transaction owned by the caller.
        actor: Authenticated project and lane.
        tool: Coordination tool named by the caller.
        args: Validated tool arguments.
        declared: Named resources this project declared, or None when it
            declared none.
        claim: Claim identifier the calling lane holds, recorded beside the
            records a tool writes. Empty records no claim.
        commits: Files per recent commit of the base checkout for a
            reservation forecast, or None when none was read.
        directory: Project state directory attachments live under, or None
            when the project has no registered manifest.

    Returns:
        The tool result.

    Raises:
        BridgeError: If the tool is unknown or its preconditions fail.
    """
    if tool == "send_message":
        return _send(db, actor, args, claim, directory)
    if tool == "read_attachment":
        if directory is None:
            raise BridgeError("This project keeps no attachments.")
        return attachments.page(
            directory,
            attachments.validate(args.get("reference")),
            actor["name"],
            _number(args.get("offset", 0), "offset", 0, 10**9),
        )
    if tool == "fetch_inbox":
        return _inbox(db, actor, args)
    if tool == "list_participants":
        return _roster(db, actor)
    if tool == "read_thread":
        return _thread(db, actor, args)
    if tool == "search_messages":
        return _search(db, actor, args)
    if tool == "search_decisions":
        return _decisions(db, actor, args)
    if tool == "file_reservation_paths":
        return _reserve(db, actor, args, declared, claim, commits)
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
            "UPDATE message_recipients "
            "SET read_ts=COALESCE(read_ts,CURRENT_TIMESTAMP)"
            + (",ack_ts=COALESCE(ack_ts,CURRENT_TIMESTAMP)" if ack else "")
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
    record no tool event. Served MCP reads separately attempt a nonwaiting
    telemetry write after reading the mail.

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


def read_message(home: Path, root: str, name: str, message_id: int) -> dict:
    """Reads one message whole as the participant that sent or received it.

    Args:
        home: Private bridge state root.
        root: Canonical project key registered with the store.
        name: Registered identity whose own mail is read.
        message_id: Message identifier.

    Returns:
        The message with its stored body, which ends with an attachment
        reference when the body was spilled.

    Raises:
        BridgeError: If no store exists, the participant is unregistered, or
            the message is not one this participant sent or received.
    """
    if not (home / DATABASE).exists():
        raise BridgeError("No coordination store yet; run agent-parley up.")
    with connect(home) as db:
        actor = _identify(db, root, name)
        row = db.execute(
            "SELECT m.id,a.name AS sender,m.thread_id,m.subject,m.body_md,"
            "m.created_ts,m.ack_required FROM messages m "
            + MAIL_SCOPE
            + "AND m.id=?",
            (actor["id"], actor["project_id"], actor["id"], message_id),
        ).fetchone()
    if not row:
        raise BridgeError(
            f"Message {message_id} is not one {name} sent or received."
        )
    return dict(row)


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


def search_decisions(
    home: Path,
    root: str,
    name: str,
    query: str = "",
    limit: int = MAX_SEARCH_HITS,
    since: int = 0,
) -> dict:
    """Reads the project-wide decision log as a registered participant.

    Args:
        home: Private bridge state root.
        root: Canonical project key registered with the store.
        name: Registered identity reading the log, which every registered
            participant may do regardless of who sent the decision.
        query: Text to match against subjects and bodies, or empty to read
            the newest decisions.
        limit: Maximum decisions reported.
        since: Seconds back the page may reach, or zero for the whole log.

    Returns:
        Matching decisions newest first, naming the index that answered.

    Raises:
        BridgeError: If no store exists or the reader is unregistered.
    """
    if not (home / DATABASE).exists():
        raise BridgeError("No coordination store yet; run agent-parley up.")
    with connect(home) as db:
        actor = _identify(db, root, name)
        return _decisions(
            db, actor, {"query": query, "limit": limit, "since": since}
        )


def decide(home: Path, root: str, subject: str, body: str, key: str) -> dict:
    """Records one supervising operator's decision for the whole project.

    The decision takes the ordinary send path, so it is deduplicated by its
    key and bounded by the same body and attachment rules as mail. It
    addresses no inbox: every registered participant reads it by searching
    the decision log instead.

    Args:
        home: Private bridge state root.
        root: Canonical project key registered with the store.
        subject: Subject line the decision is found under.
        body: Decision text every participant can read.
        key: Idempotency key; an identical record returns the original.

    Returns:
        The recorded decision identifier, carrying ``duplicate`` when this key
        already named exactly this decision.

    Raises:
        BridgeError: If the project is not registered or the decision fails
            validation.
    """
    if not (home / DATABASE).exists():
        raise BridgeError(NO_PROJECT)
    with connect(home, write=True) as db:
        project = db.execute(
            "SELECT id FROM projects WHERE human_key=?", (root,)
        ).fetchone()
        if not project:
            raise BridgeError(NO_PROJECT)
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
                "subject": subject,
                "body_md": body,
                "idempotency_key": key,
                "decision": True,
            },
        )


def list_messages(
    home: Path, root: str, name: str, limit: int = MAX_SEARCH_HITS
) -> dict:
    """Lists a registered participant's own mail, newest first.

    The listing answers the same question a search answers without a query
    to write, so an operator reads what is waiting in a lane's inbox without
    knowing the search syntax. It reads only: nothing is marked read,
    acknowledged or delivered by listing it.

    Args:
        home: Private bridge state root.
        root: Canonical project key registered with the store.
        name: Registered identity whose own mail is listed.
        limit: Maximum messages reported.

    Returns:
        The most recent messages this participant sent or received, newest
        first, and whether more were held back by the limit or the result
        budget.

    Raises:
        BridgeError: If no store exists or the participant is unregistered.
    """
    if not (home / DATABASE).exists():
        raise BridgeError("No coordination store yet; run agent-parley up.")
    with connect(home) as db:
        actor = _identify(db, root, name)
        bounded = _number(limit, "limit", 1, MAX_SEARCH_HITS)
        rows = db.execute(
            MAIL_COLUMNS
            + "FROM messages m "
            + MAIL_SCOPE
            + "ORDER BY m.id DESC LIMIT ?",
            (
                PREVIEW_CHARACTERS,
                actor["id"],
                actor["project_id"],
                actor["id"],
                bounded + 1,
            ),
        ).fetchall()
        result: dict = {"limit": bounded}
        messages = _bounded(result, rows[:bounded])
        result["messages"] = messages
        result["has_more"] = len(rows) > len(messages)
        return result


def acknowledge(home: Path, root: str, identifier: int) -> dict:
    """Records the supervising operator's acknowledgement of one message.

    A message that requires an acknowledgement is answered by the lane that
    holds it. Where that lane cannot answer, the condition stays on the
    problem list with no control to clear it, so the operator records the
    acknowledgement instead. Nothing else moves: no ownership changes, no
    reservation is released and no lane is woken.

    Args:
        home: Private bridge state root.
        root: Canonical project key registered with the store.
        identifier: Message awaiting an acknowledgement.

    Returns:
        The message identifier and the registered identities the
        acknowledgement was recorded for.

    Raises:
        BridgeError: If no store exists, or that message is not awaiting an
            acknowledgement in this project.
    """
    if not (home / DATABASE).exists():
        raise BridgeError(NO_PROJECT)
    with connect(home, write=True) as db:
        rows = db.execute(
            "SELECT a.name AS name FROM message_recipients r "
            "JOIN messages m ON m.id=r.message_id "
            "JOIN agents a ON a.id=r.agent_id "
            "JOIN projects p ON p.id=m.project_id "
            "WHERE p.human_key=? AND m.id=? AND m.ack_required=1 "
            "AND r.ack_ts IS NULL ORDER BY a.name",
            (root, identifier),
        ).fetchall()
        if not rows:
            raise BridgeError(
                f"Message {identifier} awaits no acknowledgement here."
            )
        db.execute(
            "UPDATE message_recipients SET "
            "read_ts=COALESCE(read_ts,CURRENT_TIMESTAMP),"
            "ack_ts=CURRENT_TIMESTAMP WHERE message_id=? AND ack_ts IS NULL",
            (identifier,),
        )
        return {
            "id": identifier,
            "participants": [row["name"] for row in rows],
        }


def speak(
    home: Path,
    root: str,
    name: str,
    subject: str,
    body: str,
    key: str,
    *,
    ack: bool = False,
    within: float | None = None,
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
        within: Seconds the acknowledgement is expected to take, recorded as a
            deadline beside the message. Past it the acknowledgement reads as
            overdue; nothing is escalated, resent or acknowledged for the lane.

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
                "ack_within": within,
            },
        )


def _project_id(db: sqlite3.Connection, root: str) -> int:
    """Returns the registered project identifier for a canonical key."""
    row = db.execute(
        "SELECT id FROM projects WHERE human_key=?", (root,)
    ).fetchone()
    if not row:
        raise BridgeError(NO_PROJECT)
    return int(row[0])


def _schedule_row(
    db: sqlite3.Connection, project_id: int, identifier: int
) -> sqlite3.Row | None:
    """Returns one undelivered scheduled item of a project, if it exists."""
    return db.execute(
        "SELECT * FROM scheduled_deliveries WHERE id=? AND project_id=? "
        "AND delivered_ts IS NULL AND cancelled_ts IS NULL",
        (identifier, project_id),
    ).fetchone()


def _advance_schedule(
    db: sqlite3.Connection, row: sqlite3.Row, now: float
) -> None:
    """Records one delivery and enrolls the next occurrence of a repeat.

    The successor keeps the recording time of the first occurrence, so a
    condition that compares against when the operator recorded the item reads
    the same for every occurrence of one repeat. Its sequence number advances,
    which gives each occurrence its own deduplication key and stops a restart
    from delivering an occurrence twice.
    """
    db.execute(
        "UPDATE scheduled_deliveries SET delivered_ts=? WHERE id=?",
        (now, row["id"]),
    )
    if row["repeats_left"] <= 1 or not row["every_seconds"]:
        return
    db.execute(
        "INSERT INTO scheduled_deliveries(project_id,kind,recipient,actor,"
        "subject,body_md,issue,dedup_key,sequence,ack_required,ack_within,"
        "not_before,condition,unless_reported,every_seconds,repeats_left,"
        "created_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            row["project_id"],
            row["kind"],
            row["recipient"],
            row["actor"],
            row["subject"],
            row["body_md"],
            row["issue"],
            row["dedup_key"],
            row["sequence"] + 1,
            row["ack_required"],
            row["ack_within"],
            (row["not_before"] or now) + row["every_seconds"],
            row["condition"],
            row["unless_reported"],
            row["every_seconds"],
            row["repeats_left"] - 1,
            row["created_ts"],
        ),
    )


def schedule(home: Path, root: str, item: dict) -> dict:
    """Records one operator message or handoff offer for later delivery.

    Recording delivers nothing. The item waits in the coordination store until
    the supervision poll finds its time reached or its condition recorded, so a
    stopped service delivers nothing and loses nothing.

    Args:
        home: Private bridge state root.
        root: Canonical project key registered with the store.
        item: Kind, recipient, acting lane, subject, body, issue number,
            deduplication key, acknowledgement fields, not-before time,
            condition text, report opt-out and bounded repeat of the item.

    Returns:
        The recorded item identifier beside the condition it waits on.

    Raises:
        BridgeError: If the project is unregistered or the item is invalid.
    """
    kind = str(item.get("kind", "message"))
    recipient = str(item.get("recipient", ""))
    body = str(item.get("body_md", ""))
    repeats = int(item.get("repeats_left", 1))
    if kind not in ("message", "offer"):
        raise BridgeError("A scheduled item is a message or an offer.")
    if not recipient:
        raise BridgeError("A scheduled item needs a recipient.")
    if len(body.encode()) > MAX_BODY_BYTES:
        raise BridgeError(f"Body exceeds {MAX_BODY_BYTES} bytes.")
    if not 1 <= repeats <= MAX_REPEATS:
        raise BridgeError(
            f"A repeating message is capped at {MAX_REPEATS} deliveries."
        )
    if not (home / DATABASE).exists():
        raise BridgeError(NO_PROJECT)
    with connect(home, write=True) as db:
        project_id = _project_id(db, root)
        cursor = db.execute(
            "INSERT INTO scheduled_deliveries(project_id,kind,recipient,actor,"
            "subject,body_md,issue,dedup_key,ack_required,ack_within,"
            "not_before,condition,unless_reported,every_seconds,repeats_left,"
            "created_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                project_id,
                kind,
                recipient,
                str(item.get("actor", "")),
                str(item.get("subject", "")),
                body,
                str(item.get("issue", "")),
                str(item.get("dedup_key", "")),
                int(bool(item.get("ack_required"))),
                item.get("ack_within"),
                item.get("not_before"),
                str(item.get("condition", "")),
                int(bool(item.get("unless_reported"))),
                item.get("every_seconds"),
                repeats,
                time.time(),
            ),
        )
        return {
            "id": cursor.lastrowid,
            "kind": kind,
            "recipient": recipient,
            "not_before": item.get("not_before"),
            "condition": str(item.get("condition", "")),
            "repeats_left": repeats,
        }


def schedules(
    home: Path, root: str, *, db: sqlite3.Connection | None = None
) -> list[dict]:
    """Reports every recorded item this project has not delivered yet.

    The reading writes nothing and delivers nothing, so a status view, a
    dashboard refresh or a script can read pending operator work without
    releasing any of it into a lane's inbox.

    Args:
        home: Private bridge state root.
        root: Canonical project key registered with the store.
        db: Open read transaction to answer from, or ``None`` to open one.

    Returns:
        Undelivered items oldest first, each carrying its recipient, its
        not-before time, its condition and how many deliveries remain.
    """
    if db is None and not (home / DATABASE).exists():
        return []
    with reading(home, db) as db:
        return [
            {field: row[field] for field in SCHEDULE_FIELDS}
            for row in db.execute(
                "SELECT s.* FROM scheduled_deliveries s "
                "JOIN projects p ON p.id=s.project_id WHERE p.human_key=? "
                "AND s.delivered_ts IS NULL AND s.cancelled_ts IS NULL "
                "ORDER BY s.id",
                (root,),
            )
        ]


def cancel_schedule(home: Path, root: str, identifier: int) -> dict:
    """Removes one undelivered item from a project's pending work.

    Args:
        home: Private bridge state root.
        root: Canonical project key registered with the store.
        identifier: Pending item the operator named.

    Returns:
        Whether an undelivered item carried that identifier.

    Raises:
        BridgeError: If the project is not registered.
    """
    if not (home / DATABASE).exists():
        raise BridgeError(NO_PROJECT)
    with connect(home, write=True) as db:
        project_id = _project_id(db, root)
        row = _schedule_row(db, project_id, identifier)
        if row is None:
            return {"id": identifier, "cancelled": False}
        db.execute(
            "UPDATE scheduled_deliveries SET cancelled_ts=? WHERE id=?",
            (time.time(), identifier),
        )
        return {"id": identifier, "cancelled": True}


def complete_schedule(home: Path, root: str, identifier: int) -> dict:
    """Marks one item delivered after its own substrate recorded it.

    A handoff offer lives in the issue ledger rather than the mailbox, so the
    supervisor applies the transition first and then records the delivery here.

    Args:
        home: Private bridge state root.
        root: Canonical project key registered with the store.
        identifier: Pending item that was applied.

    Returns:
        Whether an undelivered item carried that identifier.

    Raises:
        BridgeError: If the project is not registered.
    """
    if not (home / DATABASE).exists():
        raise BridgeError(NO_PROJECT)
    with connect(home, write=True) as db:
        project_id = _project_id(db, root)
        row = _schedule_row(db, project_id, identifier)
        if row is None:
            return {"id": identifier, "delivered": False}
        _advance_schedule(db, row, time.time())
        return {"id": identifier, "delivered": True}


def deliver_schedule(home: Path, root: str, identifier: int, name: str) -> dict:
    """Delivers one recorded operator message into a lane's inbox.

    The send, the delivery record and the enrollment of the next occurrence of
    a bounded repeat commit together in one write transaction, so an
    interrupted poll either delivers the occurrence once or leaves it waiting.

    Args:
        home: Private bridge state root.
        root: Canonical project key registered with the store.
        identifier: Pending item to deliver.
        name: Registered identity of the addressed participant.

    Returns:
        Whether the item was delivered and the message identifier it produced.
        A participant that has not registered with the store yet leaves the
        item waiting rather than losing it.

    Raises:
        BridgeError: If the project is not registered or the send is invalid.
    """
    if not (home / DATABASE).exists():
        raise BridgeError(NO_PROJECT)
    with connect(home, write=True) as db:
        project_id = _project_id(db, root)
        row = _schedule_row(db, project_id, identifier)
        if row is None:
            return {"id": identifier, "delivered": False, "message_id": None}
        registered = db.execute(
            "SELECT id FROM agents WHERE project_id=? AND name=?",
            (project_id, name),
        ).fetchone()
        if not registered:
            return {"id": identifier, "delivered": False, "message_id": None}
        db.execute(
            "INSERT INTO agents(project_id,name) VALUES (?,?) "
            "ON CONFLICT(project_id,name) DO NOTHING",
            (project_id, OPERATOR),
        )
        actor = db.execute(
            "SELECT id,project_id,name FROM agents "
            "WHERE project_id=? AND name=?",
            (project_id, OPERATOR),
        ).fetchone()
        key = row["dedup_key"]
        if row["sequence"]:
            key = f"{key}-{row['sequence']}"
        sent = _send(
            db,
            dict(actor),
            {
                "to": [name],
                "subject": row["subject"],
                "body_md": row["body_md"],
                "idempotency_key": key,
                "ack_required": bool(row["ack_required"]),
                "ack_within": row["ack_within"],
            },
        )
        _advance_schedule(db, row, time.time())
        return {
            "id": identifier,
            "delivered": True,
            "message_id": sent["id"],
        }


def waiting(home: Path, root: str, name: str) -> dict:
    """Reports one lane's oldest unanswered item and its last served call.

    A stalled lane looks healthy from every angle the view already had: the
    process is alive, the branch is right, and the mail counter is a number
    rather than a wait. These two readings are what turns those into a stall:
    how long the oldest item has waited, and how long it has been since the
    lane last had a coordination call served for it.

    Args:
        home: Private bridge state root.
        root: Canonical project key registered with the store.
        name: Registered identity whose mailbox is read.

    Returns:
        The oldest waiting item with its kind, sender, identifier and age in
        seconds, and the age of the last served call. An item is ``None`` when
        the lane owes nothing, and the served age is ``None`` when no call was
        ever served or the record has been retired.
    """
    report: dict = {
        "kind": None,
        "message_id": None,
        "sender": "",
        "age_seconds": 0,
        "served_age_seconds": None,
    }
    if not (home / DATABASE).exists():
        return report
    with connect(home) as db:
        agent = db.execute(
            "SELECT a.id FROM agents a JOIN projects p ON p.id=a.project_id "
            "WHERE p.human_key=? AND a.name=?",
            (root, name),
        ).fetchone()
        if not agent:
            return report
        served = db.execute(
            "SELECT max(0,unixepoch('now')-unixepoch(max(created_ts))) AS age "
            "FROM events WHERE agent_id=?",
            (agent["id"],),
        ).fetchone()
        report["served_age_seconds"] = served["age"]
        for kind, condition in (
            ("acknowledgement", "m.ack_required=1 AND r.ack_ts IS NULL"),
            ("unread", "r.read_ts IS NULL"),
        ):
            row = db.execute(
                "SELECT m.id,a.name AS sender,"
                "max(0,unixepoch('now')-unixepoch(m.created_ts)) AS age "
                "FROM message_recipients r "
                "JOIN messages m ON m.id=r.message_id "
                "JOIN agents a ON a.id=m.sender_id "
                f"WHERE r.agent_id=? AND {condition} ORDER BY m.id LIMIT 1",
                (agent["id"],),
            ).fetchone()
            if row and (
                report["kind"] is None or row["age"] > report["age_seconds"]
            ):
                report.update(
                    kind=kind,
                    message_id=row["id"],
                    sender=row["sender"],
                    age_seconds=row["age"],
                )
    return report


def usage(
    home: Path, root: str, *, db: sqlite3.Connection | None = None
) -> dict[str, dict]:
    """Reports retained tool events and held leases for one project.

    Args:
        home: Private bridge state root.
        root: Canonical project key registered with the store.
        db: Open read transaction to answer from, or ``None`` to open one.

    Returns:
        Mapping of registered identity to served calls, rejected calls,
        returned bytes, held leases, how many of those leases are past a
        declared time to live, and the age of its oldest held lease. A stale
        lease is still held and still counted; nothing releases it on its
        owner's behalf. Counts cover retained events only; older events are
        retired.
    """
    if db is None and not (home / DATABASE).exists():
        return {}
    report: dict[str, dict] = {}
    with reading(home, db) as db:
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


def transfer_reservations(
    home: Path,
    root: str,
    source: str,
    target: str,
    keys: list[str],
    claim: str = "",
) -> list[str]:
    """Moves advisory reservations between lanes in one store transaction.

    Reservations are advisory. They record which lane declared an intent to
    edit a path or hold a named resource; nothing in the file system enforces
    them. Moving them when a handoff is accepted keeps that declaration
    truthful, because the lane that now owns the work is the lane a peer reads
    on the key.

    The release and the grant run inside one SQLite transaction, so no reader
    observes both lanes holding a key, and none observes neither holding it. A
    key the source no longer holds is skipped rather than invented for the
    target, and a key the target already holds is superseded by the moved
    lease so one lane never accumulates two live records of one key.

    Args:
        home: Private bridge state root.
        root: Canonical project key registered with the store.
        source: Registered identity handing the work on.
        target: Registered identity accepting it.
        keys: Reservation keys the accepted handoff named.
        claim: Claim identifier the accepting lane now works under, recorded
            beside each moved lease. Empty records no claim.

    Returns:
        The keys that moved, in sorted order.

    Raises:
        BridgeError: If no store exists or either identity is unregistered.
    """
    if not keys:
        return []
    if not (home / DATABASE).exists():
        raise BridgeError("No coordination store yet; run agent-parley up.")
    wanted = sorted(set(keys))
    moved: list[str] = []
    with connect(home, write=True) as db:
        holder = _identify(db, root, source)
        receiver = _identify(db, root, target)
        held = db.execute(
            "SELECT id,path_pattern,exclusive,reason,expires_ts "
            "FROM file_reservations WHERE project_id=? AND agent_id=? "
            "AND released_ts IS NULL AND path_pattern IN ("
            + ",".join("?" * len(wanted))
            + ") ORDER BY path_pattern",
            (holder["project_id"], holder["id"], *wanted),
        ).fetchall()
        for lease in held:
            db.execute(
                "UPDATE file_reservations SET released_ts=CURRENT_TIMESTAMP "
                "WHERE id=? OR (agent_id=? AND path_pattern=? "
                "AND released_ts IS NULL)",
                (lease["id"], receiver["id"], lease["path_pattern"]),
            )
            db.execute(
                "INSERT INTO file_reservations(project_id,agent_id,"
                "path_pattern,exclusive,reason,expires_ts,claim_id) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    receiver["project_id"],
                    receiver["id"],
                    lease["path_pattern"],
                    lease["exclusive"],
                    lease["reason"],
                    lease["expires_ts"],
                    claim or None,
                ),
            )
            moved.append(lease["path_pattern"])
    return moved


def active_reservations(home: Path, root: str) -> dict[str, list[str]]:
    """Reports every unreleased, unexpired reservation key for one project.

    Args:
        home: Private bridge state root.
        root: Canonical project key registered with the store.

    Returns:
        Mapping of registered identity to the keys it holds, in sorted order.
        A lease past its declared time to live is left out: it is still held
        and still reported as stale elsewhere, but the paths it names no
        longer read as reserved for the purpose of naming a collision.
    """
    if not (home / DATABASE).exists():
        return {}
    held: dict[str, list[str]] = {}
    with connect(home) as db:
        for row in db.execute(
            "SELECT a.name AS name,f.path_pattern AS pattern "
            "FROM file_reservations f JOIN agents a ON a.id=f.agent_id "
            "JOIN projects p ON p.id=a.project_id WHERE p.human_key=? "
            "AND f.released_ts IS NULL AND (f.expires_ts IS NULL "
            "OR f.expires_ts>CURRENT_TIMESTAMP) ORDER BY a.name,f.path_pattern",
            (root,),
        ):
            held.setdefault(row["name"], []).append(row["pattern"])
    return held
