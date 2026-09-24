"""Owns transactional coordination storage without third-party runtime code."""

import contextlib
import fnmatch
import hashlib
import json
import re
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
    recommend,
    retries,
    roster,
)
from agent_parley.roster import OPERATOR
from agent_parley.state import BridgeError, lock

DATABASE = "bridge.sqlite3"
SCHEMA_VERSION = 11
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
REFUSAL_SECONDS = 86400.0
MAX_THREAD_PAGE = 10
MAX_SEARCH_HITS = 5
MAX_QUERY_BYTES = 160
MAX_SUPERSEDE_REASON = 200
MAX_REPEATS = 24
MAX_QUEUED_REQUESTS = 32
MAX_NOTICE_CHARACTERS = 1000
DEFAULT_ACK_SECONDS = 240
RESERVATION_GRACE = 1800
BASE_TOPIC = "base"
DIRECT_TOPIC = "direct"
MAX_TOPIC = 80
MIN_STEM = 3
BROADCAST_MIN = 3
MARK_READ_TIMEOUT = 0.5
BASE_NOTE = re.compile(
    r"^\W*(?:main|master|trunk|base|integration\S*|origin/\S+)\s+"
    r"(?:is|now|at|moved|advanced)\b|\bmerged\b",
    re.IGNORECASE,
)
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
    "next_issues",
    "read_attachment",
    "read_thread",
    "search_decisions",
    "search_messages",
    "wait_for_message",
)
ATTACHED = ("send_message", "read_attachment", "review_report")
PRESENCE_WARNINGS = {
    "idle": ("idle", "idle; wake requested"),
    "stopped": ("unreachable", "unreachable"),
    "unknown": (
        "unknown",
        "process identity unavailable; manual attention required",
    ),
    "unreachable": ("unreachable", "unreachable"),
}
RETRIED = {
    "acknowledge_message": ("message_id",),
    "mark_message_read": ("message_id",),
    "file_reservation_paths": ("paths", "ttl_seconds", "exclusive", "reason"),
    "request_reservation": ("paths", "ttl_seconds", "exclusive", "reason"),
    "cancel_reservation_request": ("request_id",),
    "release_file_reservations": (),
}
RESERVING = ("file_reservation_paths", "request_reservation")
RETIRE = "retire"
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
 topic TEXT NOT NULL DEFAULT '', feed INTEGER NOT NULL DEFAULT 0,
 UNIQUE(sender_id,dedup_key));
CREATE INDEX IF NOT EXISTS threads ON messages(project_id,thread_id,id);
CREATE TABLE IF NOT EXISTS message_recipients (
 message_id INTEGER NOT NULL REFERENCES messages(id),
 agent_id INTEGER NOT NULL REFERENCES agents(id), read_ts TEXT, ack_ts TEXT,
 superseded_ts TEXT, superseded_reason TEXT,
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
 expires_ts TEXT, released_ts TEXT, claim_id TEXT, ttl_seconds INTEGER);
CREATE INDEX IF NOT EXISTS leases ON file_reservations(project_id,expires_ts)
 WHERE released_ts IS NULL;
CREATE TABLE IF NOT EXISTS reservation_requests (
 id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id),
 agent_id INTEGER NOT NULL REFERENCES agents(id), path_pattern TEXT NOT NULL,
 exclusive INTEGER NOT NULL, reason TEXT DEFAULT '', ttl_seconds INTEGER,
 created_ts TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 granted_ts TEXT, cancelled_ts TEXT, claim_id TEXT);
CREATE INDEX IF NOT EXISTS queued ON reservation_requests(project_id,id)
 WHERE granted_ts IS NULL AND cancelled_ts IS NULL;
CREATE TABLE IF NOT EXISTS reservation_refusals (
 project_id INTEGER NOT NULL REFERENCES projects(id),
 agent_id INTEGER NOT NULL REFERENCES agents(id), holder TEXT NOT NULL,
 path_pattern TEXT NOT NULL, refused_ts REAL NOT NULL,
 PRIMARY KEY(project_id,agent_id,holder,path_pattern));
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

    Upgrading a store written before leases recorded their declared window
    backfills that window from the distance between a lease's creation and
    its deadline, so a renewal restores what its holder asked for rather
    than a value this build invented.

    Upgrading a store written before mail supersession adds the marker and
    its reason to each delivery. Every message keeps its text, its recipients
    and its receipts, and mail delivered before the upgrade reads as live
    until the claim it was sent under closes, so no backlog is retired by
    being upgraded.

    Upgrading a store written before topics and the project feed gives every
    stored message an empty topic and keeps it out of the feed, so routing
    and supersession apply only to mail sent after the upgrade.

    A store already stamped with this build's schema still has every column
    this build owns verified and added where it is missing. The stamp says
    which upgrade ran, not that its columns are all present, and a store
    missing one under a current stamp would otherwise fail every read that
    names it while every restart took the same early return. Each column
    step reads the table first, so a complete store changes nothing.

    Every step a store past the lease rebuild takes is additive, so a service
    still running an older build keeps serving from the migrated store, and a
    newer build can migrate it in place without stopping the lanes that are
    working.
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
                _add_ack_deadline(db)
                _add_decision_flag(db)
                _add_reservation_created(db)
                _rebuild_reservations(db)
                _add_claim_correlation(db)
                _add_reservation_ttl(db)
                _add_supersession(db)
                _add_topic(db)
                _add_message_search(db)
                if version == SCHEMA_VERSION:
                    return
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


def _add_reservation_ttl(db: sqlite3.Connection) -> None:
    """Records the window a lease was taken for, where it is missing.

    A renewal restores the window its holder declared rather than inventing
    one, so the declared time to live is kept beside the deadline it produced.
    The column is additive and nullable: a lease taken without a deadline
    keeps none, and a lease stored before the upgrade is backfilled from the
    distance between its creation and its deadline, which is the window it
    was taken with.

    It runs after the reservation table has been rebuilt into its current
    shape, because that rebuild copies a fixed column list.

    Args:
        db: Open upgrade transaction owned by the caller.
    """
    columns = {
        row[1] for row in db.execute("PRAGMA table_info(file_reservations)")
    }
    if "ttl_seconds" not in columns:
        db.execute(
            "ALTER TABLE file_reservations ADD COLUMN ttl_seconds INTEGER"
        )
    db.execute(
        "UPDATE file_reservations SET ttl_seconds="
        "max(1,strftime('%s',expires_ts)-strftime('%s',created_ts)) "
        "WHERE ttl_seconds IS NULL AND expires_ts IS NOT NULL "
        "AND created_ts IS NOT NULL"
    )


def _add_supersession(db: sqlite3.Connection) -> None:
    """Adds the supersession marker to deliveries that carry none.

    Both columns are additive and nullable, so a delivery written before the
    upgrade keeps its receipts and reads as live. Supersession is recorded
    beside the receipt rather than derived at reading time, because the reason
    a delivery stopped mattering is a fact about the claim that ended, which a
    later reading of the ledger can no longer recover.
    """
    columns = {
        row[1] for row in db.execute("PRAGMA table_info(message_recipients)")
    }
    for column in ("superseded_ts", "superseded_reason"):
        if column not in columns:
            db.execute(
                f"ALTER TABLE message_recipients ADD COLUMN {column} TEXT"
            )


def _add_topic(db: sqlite3.Connection) -> None:
    """Adds the message topic and the project feed marker to an older store.

    Both columns are additive with empty defaults, so a message stored before
    the upgrade carries no topic, supersedes nothing, stays in the mailboxes
    it was delivered to and never appears in the project feed.

    Args:
        db: Open upgrade transaction owned by the caller.
    """
    columns = {row[1] for row in db.execute("PRAGMA table_info(messages)")}
    if "topic" not in columns:
        db.execute(
            "ALTER TABLE messages ADD COLUMN topic TEXT NOT NULL DEFAULT ''"
        )
    if "feed" not in columns:
        db.execute(
            "ALTER TABLE messages ADD COLUMN feed INTEGER NOT NULL DEFAULT 0"
        )
    db.execute(
        "CREATE INDEX IF NOT EXISTS feed ON messages(project_id,id) "
        "WHERE feed=1"
    )


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

    Queued reservation requests expire with the credential, in the same
    transaction, because a lane that can no longer be addressed can neither
    take a key nor be told that it did. Leases the participant already holds
    are left exactly as they were; releasing them stays an explicit act.

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
        return _expire(db, root, name)


def _expire(db: sqlite3.Connection, root: str, name: str) -> int:
    """Invalidates one credential and expires its queued requests together.

    Args:
        db: Open write transaction owned by the caller.
        root: Canonical project key registered with the store.
        name: Registered identity whose credential is invalidated.

    Returns:
        Number of credentials invalidated.
    """
    selection = (
        "SELECT a.id FROM agents a JOIN projects p ON p.id=a.project_id "
        "WHERE p.human_key=? AND a.name=?"
    )
    db.execute(
        "UPDATE reservation_requests SET cancelled_ts=CURRENT_TIMESTAMP "
        "WHERE granted_ts IS NULL AND cancelled_ts IS NULL "
        f"AND agent_id IN ({selection})",
        (root, name),
    )
    result = db.execute(
        f"UPDATE agents SET token_digest=NULL WHERE id IN ({selection})",
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


def ack_seconds(manifest: dict | None) -> float:
    """Reads the acknowledgement deadline one project records.

    Args:
        manifest: Project manifest, or None when none was read.

    Returns:
        The project's recorded ``ack`` deadline in seconds, or
        ``DEFAULT_ACK_SECONDS`` when it records none.
    """
    recorded = ((manifest or {}).get("deadlines") or {}).get("ack")
    return float(DEFAULT_ACK_SECONDS if recorded is None else recorded)


def message_topic(subject: str, explicit: str = "") -> str:
    """Names the topic a message supersedes earlier mail under.

    A sender may name a topic. Without one, a subject announcing where a base
    branch now points or that work merged is a base note, whose newest copy
    is the only one worth reading; any other subject has no topic and so
    replaces nothing.

    Args:
        subject: Message subject.
        explicit: Topic the sender named, or an empty string.

    Returns:
        The topic, ``BASE_TOPIC`` for a base note, or an empty string.
    """
    if explicit:
        return explicit
    return BASE_TOPIC if BASE_NOTE.search(subject) else ""


def mentions(text: str, name: str) -> bool:
    """Reports whether text names a lane as a word of its own.

    Args:
        text: Subject and body of a message.
        name: Registered identity of a lane.

    Returns:
        Whether the name appears with no word character or hyphen on either
        side, so ``claude`` is not found inside ``claude-2``.
    """
    return bool(re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", text, re.I))


def reservation_stem(pattern: str) -> str:
    """Returns the literal part of a reservation key a message could quote.

    Args:
        pattern: Reservation key, a path, a glob or a named resource.

    Returns:
        The key up to its first glob character without a trailing slash, or
        an empty string when that part is too short to identify anything.
    """
    stem = re.split(r"[*?\[]", pattern, maxsplit=1)[0].rstrip("/")
    return stem if len(stem) >= MIN_STEM else ""


def lane_work(directory: Path | None) -> dict[str, list[str]]:
    """Maps each registered identity to the issue numbers its lane owns.

    Args:
        directory: Project state directory, or None when none is known.

    Returns:
        Registered identity to owned issue numbers. An unreadable manifest or
        ledger yields an empty mapping, so routing falls back to mentions and
        reserved paths alone.
    """
    if directory is None:
        return {}
    try:
        manifest = roster.read(directory)
        owned = issues.holders(issues.snapshot(directory))
    except (BridgeError, OSError, ValueError, KeyError):
        return {}
    return {
        participant["display"]: owned.get(name, [])
        for name, participant in manifest["participants"].items()
    }


def _concerned(
    db: sqlite3.Connection,
    recipient: int,
    name: str,
    text: str,
    claim: str,
    owned: list[str],
) -> bool:
    """Reports whether one message concerns one of the lanes it addressed.

    A message concerns a lane that it names, whose reserved paths it quotes,
    whose owned issue it cites by number, or whose reservations were taken
    under the claim the message was sent from.

    Args:
        db: Open transaction owned by the caller.
        recipient: Store identifier of the addressed lane.
        name: Registered identity of the addressed lane.
        text: Subject and body of the message.
        claim: Claim the sender held, or an empty string.
        owned: Issue numbers the addressed lane owns.

    Returns:
        Whether the message should reach this lane's mailbox.
    """
    if mentions(text, name):
        return True
    if any(re.search(rf"#{re.escape(number)}\b", text) for number in owned):
        return True
    for row in db.execute(
        "SELECT path_pattern,claim_id FROM file_reservations "
        "WHERE agent_id=? AND released_ts IS NULL",
        (recipient,),
    ):
        if claim and row["claim_id"] == claim:
            return True
        stem = reservation_stem(row["path_pattern"])
        if stem and stem in text:
            return True
    return False


def _routed(
    db: sqlite3.Connection,
    actor: dict,
    addressed: dict[int, str],
    text: str,
    topic: str,
    claim: str,
    directory: Path | None,
) -> set[int]:
    """Chooses which addressed lanes a status message reaches.

    A base note goes to the project feed and no mailbox. A broadcast, a note
    addressed to every other lane that can receive mail and to at least
    ``BROADCAST_MIN`` of them, reaches only the lanes it concerns, and the
    feed carries it for everybody else. A note to a chosen subset of lanes
    is an explicit address and reaches every lane it names.

    Args:
        db: Open transaction owned by the caller.
        actor: Authenticated sender.
        addressed: Store identifier to registered identity of each recipient
            the sender named.
        text: Subject and body of the message.
        topic: Topic the message carries.
        claim: Claim the sender held, or an empty string.
        directory: Project state directory, or None when none is known.

    Returns:
        Store identifiers of the lanes whose mailboxes receive the message.
    """
    if topic == BASE_TOPIC:
        return set()
    peers = {
        row[0]
        for row in db.execute(
            "SELECT id FROM agents WHERE project_id=? AND id<>? "
            "AND token_digest IS NOT NULL",
            (actor["project_id"], actor["id"]),
        )
    }
    if len(addressed) < BROADCAST_MIN or not peers <= set(addressed):
        return set(addressed)
    owned = lane_work(directory)
    return {
        recipient
        for recipient, name in addressed.items()
        if _concerned(db, recipient, name, text, claim, owned.get(name, []))
    }


def mark_read(home: Path, root: str, name: str, ids: list[int]) -> int:
    """Records that a checkpoint delivered messages into a lane's context.

    Only the reading time still unset is stamped, and an acknowledgement is
    never recorded on the lane's behalf, so a message that asks for one is
    still owed. A store that cannot be written leaves the mail unread, which
    only means it is offered again.

    Args:
        home: Private bridge state root.
        root: Canonical project key registered with the store.
        name: Registered identity the mail was delivered to.
        ids: Messages the delivered context carried.

    Returns:
        The number of deliveries newly marked read.
    """
    if not ids or not (home / DATABASE).exists():
        return 0
    marks = ",".join("?" * len(ids))
    try:
        with connect(home, write=True, timeout=MARK_READ_TIMEOUT) as db:
            cursor = db.execute(
                "UPDATE message_recipients SET read_ts=CURRENT_TIMESTAMP "
                f"WHERE read_ts IS NULL AND message_id IN ({marks}) "
                "AND agent_id=(SELECT a.id FROM agents a JOIN projects p "
                "ON p.id=a.project_id WHERE p.human_key=? AND a.name=?)",
                (*ids, root, name),
            )
            return cursor.rowcount
    except (sqlite3.Error, OSError):
        return 0


def feed(home: Path, root: str, after: int = 0, limit: int = 3) -> dict:
    """Reads the newest live project feed entries after a cursor.

    An entry is live while no newer entry from the same sender carries the
    same non-empty topic; a replaced entry is only counted.

    Args:
        home: Private bridge state root.
        root: Canonical project key registered with the store.
        after: Last feed entry this reader already saw.
        limit: Most entries to return.

    Returns:
        ``items`` newest first, each with its identifier, sender, topic,
        subject and creation time, and ``superseded``, the number of entries
        after the cursor that a newer one replaced.
    """
    if not (home / DATABASE).exists():
        return {"items": [], "superseded": 0}
    replaced = (
        "EXISTS (SELECT 1 FROM messages n WHERE n.project_id=m.project_id "
        "AND n.feed=1 AND n.sender_id=m.sender_id AND n.topic=m.topic "
        "AND m.topic<>'' AND n.id>m.id)"
    )
    with connect(home) as db:
        rows = db.execute(
            "SELECT m.id,a.name AS sender,m.topic,substr(m.subject,1,80) "
            "AS subject,m.created_ts FROM messages m "
            "JOIN agents a ON a.id=m.sender_id "
            "JOIN projects p ON p.id=m.project_id WHERE p.human_key=? "
            f"AND m.feed=1 AND m.id>? AND NOT {replaced} "
            "ORDER BY m.id DESC LIMIT ?",
            (root, after, limit),
        ).fetchall()
        superseded = db.execute(
            "SELECT count(*) FROM messages m JOIN projects p "
            "ON p.id=m.project_id WHERE p.human_key=? AND m.feed=1 "
            f"AND m.id>? AND {replaced}",
            (root, after),
        ).fetchone()[0]
    return {"items": [dict(row) for row in rows], "superseded": superseded}


def _ack_window(directory: Path | None) -> float:
    """Resolves the deadline an acknowledgement request carries by default.

    Every acknowledgement request carries a deadline, so an unanswered one is
    returned to its sender and retired instead of waiting forever in a lane
    that may never read mail again. The project's recorded ``ack`` default
    wins; a project that records none takes ``DEFAULT_ACK_SECONDS``, which is
    shorter than the default inactivity threshold so a missed acknowledgement
    is known before the lane itself reads as idle.

    Args:
        directory: Project state directory holding the manifest, or None when
            the caller resolved no project.

    Returns:
        Seconds the recorded deadline is measured from.
    """
    if directory is not None:
        with contextlib.suppress(BridgeError, OSError, ValueError):
            return ack_seconds(roster.read(directory))
    return float(DEFAULT_ACK_SECONDS)


def _send(
    db: sqlite3.Connection,
    actor: dict,
    args: dict,
    claim: str = "",
    directory: Path | None = None,
    route: bool = False,
) -> dict:
    """Atomically delivers an idempotent message to authorized recipients.

    A lane send (``route``) that neither requires acknowledgement nor
    records a decision is routed by relevance: a base-advance or merge note
    goes to the project feed alone, and a broadcast to every other lane
    reaches only those whose claim, reserved paths or name it concerns. The
    rest read it from the feed. A send with a topic supersedes the same
    sender's older unread message on that topic for each recipient.

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
    if ack and within is None:
        within = _ack_window(directory)
    if "reply_to" in args:
        if thread:
            raise BridgeError("Answer with reply_to or thread_id, not both.")
        thread = _answered_thread(db, actor, args["reply_to"])
    thread = thread or _opened_thread(actor, key)
    topic = message_topic(
        subject, _text(args.get("topic", ""), "topic", MAX_TOPIC, empty=True)
    )
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
    addressed: dict[int, str] = {}
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
        addressed[row[0]] = name
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
        ) != (subject, stored, thread, ack, decision) or (
            not previous <= set(ids)
            if existing["feed"]
            else previous != set(ids)
        ):
            raise BridgeError("Idempotency key already names another message.")
        return {
            "id": existing["id"],
            "thread_id": existing["thread_id"],
            "duplicate": True,
        }
    delivered = set(ids)
    if route and not ack and not decision:
        delivered = _routed(
            db, actor, addressed, f"{subject}\n{body}", topic, claim, directory
        )
    fed = route and delivered != set(ids)
    cursor = db.execute(
        "INSERT INTO messages(project_id,sender_id,subject,body_md,"
        "thread_id,ack_required,dedup_key,ack_deadline_ts,claim_id,decision,"
        "topic,feed) VALUES (?,?,?,?,?,?,?,datetime('now',?),?,?,?,?)",
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
            topic,
            fed,
        ),
    )
    message_id = cursor.lastrowid
    db.executemany(
        "INSERT INTO message_recipients(message_id,agent_id) VALUES (?,?)",
        [(message_id, recipient) for recipient in delivered],
    )
    if topic and delivered:
        db.execute(
            "UPDATE message_recipients SET superseded_ts=CURRENT_TIMESTAMP,"
            "superseded_reason=? WHERE superseded_ts IS NULL AND agent_id IN "
            "(SELECT agent_id FROM message_recipients WHERE message_id=?) "
            "AND message_id IN (SELECT id FROM messages WHERE sender_id=? "
            "AND topic=? AND id<?) AND (read_ts IS NULL OR (ack_ts IS NULL "
            "AND EXISTS (SELECT 1 FROM messages m WHERE "
            "m.id=message_recipients.message_id AND m.ack_required=1)))",
            (
                f"superseded by message {message_id}",
                message_id,
                actor["id"],
                topic,
                message_id,
            ),
        )
    result: dict = {"id": message_id, "thread_id": thread}
    if topic:
        result["topic"] = topic
    if fed:
        result["feed"] = True
        result["withheld"] = sorted(
            name
            for recipient, name in addressed.items()
            if recipient not in delivered
        )
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
    that thread or answer into it without a further lookup. Naming a thread
    narrows the page to that conversation, so a recipient expecting one
    answer does not page past unrelated mail to find it.
    """
    after = _number(args.get("after_id", 0), "after_id", 0, 2**63 - 1)
    limit = _number(args.get("limit", 5), "limit", 1, 5)
    bodies = _flag(args.get("include_bodies", False), "include_bodies")
    unread = _flag(args.get("unread", False), "unread")
    unacknowledged = _flag(args.get("unacknowledged", False), "unacknowledged")
    thread = _text(args.get("thread_id", ""), "thread_id", 80, empty=True)
    offset = _number(args.get("body_offset", 0), "body_offset", 0, 10**9)
    rows = db.execute(
        "SELECT m.id,a.name AS sender,m.thread_id,m.subject,m.body_md,"
        "m.ack_required,r.read_ts,r.ack_ts "
        "FROM messages m JOIN agents a ON a.id=m.sender_id "
        "JOIN message_recipients r ON r.message_id=m.id "
        "WHERE r.agent_id=? AND m.id>? "
        "AND (?=0 OR r.read_ts IS NULL) "
        "AND (?=0 OR (m.ack_required=1 AND r.ack_ts IS NULL)) "
        "AND (?='' OR m.thread_id=?) "
        "ORDER BY m.id LIMIT ?",
        (actor["id"], after, unread, unacknowledged, thread, thread, limit + 1),
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


def peek_inbox(home: Path, actor: dict, args: dict) -> dict | None:
    """Reads one inbox page for a caller that repeats the read while waiting.

    The page is exactly what `fetch_inbox` reports, read in its own bounded
    read transaction, but no tool event is recorded: a lane waiting for mail
    would otherwise write one event per read and bury the calls an operator
    reads the log for. The send that delivers the mail is recorded as it
    always was.

    Args:
        home: Private bridge state root.
        actor: Authenticated project and lane, whose own inbox is read.
        args: Inbox filters, as `fetch_inbox` accepts them.

    Returns:
        The inbox page, or None once this lane holds no active registration,
        so a revoked credential ends a repeated read instead of serving it.

    Raises:
        BridgeError: If a filter is invalid.
    """
    with connect(home) as db:
        registered = db.execute(
            "SELECT 1 FROM agents WHERE id=? AND token_digest IS NOT NULL",
            (actor["id"],),
        ).fetchone()
        if registered is None:
            return None
        return _inbox(db, actor, args)


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
    competing reservation would be. A key without glob characters is only
    ever the text being matched, never a compiled pattern, which gives the
    same answer without one regular expression per dirty path.

    Args:
        pattern: Reservation key or repository-relative path.
        other: Reservation key it is compared against.

    Returns:
        Whether the two keys overlap.
    """
    if named_resource(pattern) or named_resource(other):
        return pattern == other
    pattern_glob = any(c in pattern for c in "*?[")
    other_glob = any(c in other for c in "*?[")
    return (
        (pattern_glob and other_glob)
        or pattern == other
        or (other_glob and fnmatch.fnmatchcase(pattern, other))
        or (pattern_glob and fnmatch.fnmatchcase(other, pattern))
        or pattern.startswith(other.rstrip("/") + "/")
        or other.startswith(pattern.rstrip("/") + "/")
    )


def overlapping_paths(paths: list[str], patterns: list[str]) -> set[str]:
    """Names the paths that overlap any pattern, as ``overlapping`` judges.

    A dirty checkout can list tens of thousands of paths, and one call per
    path and pattern pair costs a second on every reading. A plain pattern
    is answered instead with one set lookup for itself, one for each
    directory above it, and one prefix scan for the paths under it. A glob
    pattern is compiled once and matched against every plain path, and
    collides with every globbed path. A named resource overlaps only its own
    spelling, so either side being one is answered by a set lookup; only a
    globbed path checked against a plain pattern uses the pairwise rule.

    Args:
        paths: Reservation keys or repository-relative paths.
        patterns: Reservation keys each path is compared against.

    Returns:
        Every path that ``overlapping(path, pattern)`` reports for some
        pattern.
    """
    if not patterns:
        return set()
    globbed = [
        path for path in paths if "*" in path or "?" in path or "[" in path
    ]
    trimmed: dict[str, list[str]] = {}
    for path in paths:
        trimmed.setdefault(path.rstrip("/"), []).append(path)
    exact = set(paths)
    shaped = [path for path in globbed if not named_resource(path)]
    skipped = set(globbed)
    plain = [
        path
        for path in paths
        if path not in skipped and not named_resource(path)
    ]
    found: set[str] = set()
    for pattern in patterns:
        if pattern in exact:
            found.add(pattern)
        if named_resource(pattern):
            continue
        for index, character in enumerate(pattern):
            if character == "/":
                found.update(trimmed.get(pattern[:index], ()))
        prefix = pattern.rstrip("/") + "/"
        if any(c in pattern for c in "*?["):
            matches = re.compile(fnmatch.translate(pattern)).match
            found.update(shaped)
            found.update(
                path
                for path in plain
                if matches(path) or path.startswith(prefix)
            )
            continue
        found.update(path for path in paths if path.startswith(prefix))
        found.update(path for path in globbed if overlapping(path, pattern))
    return found


def _keys(
    args: dict, declared: frozenset[str] | None
) -> tuple[list[str], int | None, bool, str]:
    """Validates one reservation batch into the keys and terms it asks for.

    The same reading serves a grant and a queued request, so a key refused
    for one is refused identically for the other.

    Args:
        args: Validated tool arguments.
        declared: Resources this project declared, or None when it declared
            none and any well-formed name is acceptable.

    Returns:
        The normalized keys, the optional time to live, whether the batch is
        exclusive, and the declared reason.

    Raises:
        BridgeError: If the batch is malformed or names an undeclared
            resource.
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
    return keys, ttl, exclusive, reason


def _operator(db: sqlite3.Connection, project_id: int) -> dict:
    """Returns the command-line operator identity of one project.

    Args:
        db: Open write transaction owned by the caller.
        project_id: Registered project the identity belongs to.

    Returns:
        The operator identity, registered on first use, which is the sender
        of a notice no lane composed.
    """
    db.execute(
        "INSERT INTO agents(project_id,name) VALUES (?,?) "
        "ON CONFLICT(project_id,name) DO NOTHING",
        (project_id, OPERATOR),
    )
    return dict(
        db.execute(
            "SELECT id,project_id,name FROM agents "
            "WHERE project_id=? AND name=?",
            (project_id, OPERATOR),
        ).fetchone()
    )


def _expired_leases(
    db: sqlite3.Connection, project_id: int
) -> list[sqlite3.Row]:
    """Names the leases of one project that no longer describe live work.

    A lease is reclaimable once its declared deadline has passed and either
    the last observation of its holder found no live session process, found
    the holder idle past the project's inactive threshold, or it has been
    past that deadline longer than ``RESERVATION_GRACE``. A lane idle that
    long is not working under the lease, and only a tool call renews one, so
    a lane parked on a dialog or resumed without working loses the key to
    the queue rather than holding it for the whole grace. The grace covers
    a holder observed as active, which is woken and renews the lease from
    its next tool call before any peer takes the key. A holder never
    observed at all is not assumed dead: only the grace reclaims its lease.

    Args:
        db: Open transaction owned by the caller.
        project_id: Registered project whose leases are read.

    Returns:
        One row per reclaimable lease, with its holder, its key and how long
        it has been past its deadline, in holder and key order.
    """
    return db.execute(
        "SELECT f.id,f.agent_id,f.path_pattern,a.name,"
        "max(0,unixepoch('now')-unixepoch(f.expires_ts)) AS stale_seconds "
        "FROM file_reservations f JOIN agents a ON a.id=f.agent_id "
        "LEFT JOIN participant_presence s ON s.agent_id=f.agent_id "
        "WHERE f.project_id=? AND f.released_ts IS NULL "
        "AND f.expires_ts IS NOT NULL AND f.expires_ts<=CURRENT_TIMESTAMP "
        "AND (s.process_alive=0 OR s.state='idle' "
        "OR unixepoch('now')-unixepoch(f.expires_ts)>=?) "
        "ORDER BY a.name,f.path_pattern",
        (project_id, RESERVATION_GRACE),
    ).fetchall()


def _reclaim(db: sqlite3.Connection, project_id: int) -> list[dict]:
    """Releases expired leases nobody is working under and hands them on.

    The release, the grant it enables and both notices share the caller's
    transaction, so no reader sees a key reclaimed with its queue untouched.
    Reservations stay advisory throughout: this changes who is told that a
    key is free, not what the file system allows.

    Args:
        db: Open write transaction owned by the caller.
        project_id: Registered project whose leases are swept.

    Returns:
        One entry per holder whose leases were reclaimed, naming that holder,
        the released keys, the notice it was sent and the lanes that took the
        keys from its queue.
    """
    holders: dict[str, list[sqlite3.Row]] = {}
    for row in _expired_leases(db, project_id):
        holders.setdefault(row["name"], []).append(row)
    reclaimed: list[dict] = []
    for name, leases in holders.items():
        keys = sorted({lease["path_pattern"] for lease in leases})
        db.execute(
            "UPDATE file_reservations SET released_ts=CURRENT_TIMESTAMP "
            "WHERE id IN (" + ",".join("?" * len(leases)) + ")",
            tuple(lease["id"] for lease in leases),
        )
        holder = {
            "id": leases[0]["agent_id"],
            "project_id": project_id,
            "name": name,
        }
        granted = _grant_queued(db, holder, keys, reclaimed=True)
        subject, body = _reclaim_notice(
            keys, max(lease["stale_seconds"] for lease in leases), granted
        )
        message = _send(
            db,
            _operator(db, project_id),
            {
                "to": [name],
                "subject": subject,
                "body_md": body,
                "idempotency_key": (
                    f"reservation-reclaimed-"
                    f"{max(lease['id'] for lease in leases)}"
                ),
            },
        )
        reclaimed.append(
            {
                "agent": name,
                "paths": keys,
                "message_id": message["id"],
                "granted": granted,
            }
        )
    return reclaimed


def _reclaim_notice(
    keys: list[str], stale_seconds: int, granted: list[dict]
) -> tuple[str, str]:
    """Words the notice a holder reads when its expired leases are released.

    Args:
        keys: Keys released from that holder, in key order.
        stale_seconds: Age of the oldest released lease past its deadline.
        granted: Lanes that took those keys from the queue, if any.

    Returns:
        The bounded subject and body of the notice.
    """
    listed = ", ".join(keys)[:MAX_NOTICE_CHARACTERS]
    took = ", ".join(entry["agent"] for entry in granted)
    return (
        f"Reservation expired and released: {listed}"[:160],
        f"Your reservation of {listed} passed its declared time to live "
        f"{stale_seconds} seconds ago and was released, because no live "
        "session was observed for it and nothing renewed it within the "
        f"{RESERVATION_GRACE}-second grace. "
        + (f"{took} took it from the queue. " if took else "")
        + "Reservations are advisory: nothing on disk was locked or "
        "reverted, and your work is untouched. Reserve the keys again with "
        "file_reservation_paths if you are still editing them.",
    )


def _conflicts(
    db: sqlite3.Connection, actor: dict, paths: list[str], exclusive: bool
) -> tuple[list[sqlite3.Row], list[dict]]:
    """Names every peer lease that blocks one batch of keys.

    Args:
        db: Open transaction owned by the caller.
        actor: Authenticated project and lane.
        paths: Normalized keys the batch asks for.
        exclusive: Whether the batch asks exclusively.

    Returns:
        Every live peer lease of this project, and one conflict entry per
        blocked key and blocking lease, in key order.
    """
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
    for pattern in sorted(set(paths)):
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
    return leases, conflicts


def _refusal(conflicts: list[dict], queued: list[dict] | None = None) -> dict:
    """Reports a refused batch within the serialized response budget.

    Args:
        conflicts: Every conflict the batch raised, in key order.
        queued: Queued requests recorded for those conflicts, or None when
            the batch queued nothing and reports no queue at all.

    Returns:
        The granted-nothing result, the conflicts that fit the budget, and
        ``has_more`` when further conflicts exist beyond the reported ones.
    """
    reported: list[dict] = []
    for conflict in conflicts[:16]:
        candidate = {"granted": [], "conflicts": [*reported, conflict]}
        if queued is not None:
            candidate["queued"] = queued
        if (
            len(json.dumps(candidate, ensure_ascii=False).encode())
            > MAX_RESULT_BYTES
        ):
            break
        reported.append(conflict)
    result = {
        "granted": [],
        "conflicts": reported,
        "has_more": len(reported) < len(conflicts),
    }
    if queued is not None:
        result["queued"] = queued
    return result


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

    ``ttl_seconds`` is optional. Without it the lease carries no deadline,
    never reports as stale and is never reclaimed. With it, a lease whose
    deadline has passed carries ``stale`` in the conflict it raises, so a
    reader can tell a working owner from one that died holding the path. An
    expired lease keeps blocking while its holder may still be working: it is
    reclaimed only once the last observation of that holder found no live
    session, or once it has been expired longer than ``RESERVATION_GRACE``,
    whichever comes first. A holder that is alive renews it from its next
    checkpoint instead.

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
    paths, ttl, exclusive, reason = _keys(args, declared)
    _reclaim(db, actor["project_id"])
    leases, conflicts = _conflicts(db, actor, paths, exclusive)
    if conflicts:
        return _refusal(conflicts)
    return _grant(
        db, actor, paths, ttl, exclusive, reason, claim, leases, commits
    )


def _grant(
    db: sqlite3.Connection,
    actor: dict,
    paths: list[str],
    ttl: int | None,
    exclusive: bool,
    reason: str,
    claim: str,
    leases: list[sqlite3.Row],
    commits: list[list[str]] | None,
) -> dict:
    """Records one unconflicted batch of leases and forecasts its collisions.

    Args:
        db: Open transaction owned by the caller.
        actor: Authenticated project and lane.
        paths: Normalized keys to record, already free of conflicts.
        ttl: Seconds until the lease reports stale, or None for no deadline.
        exclusive: Whether the batch was asked for exclusively.
        reason: Declared scope shown to peers the lease blocks.
        claim: Claim identifier the lane holds, recorded beside each lease.
        leases: Live peer leases read for the conflict check, reused for the
            forecast so the same instant answers both.
        commits: Files per recent commit of the base checkout, or None when
            no history is available.

    Returns:
        The granted leases, carrying ``forecast`` where a peer holds a file
        that habitually changes with a granted path.

    Raises:
        BridgeError: If the lane already holds the maximum number of leases.
    """
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
            "exclusive,reason,expires_ts,claim_id,ttl_seconds) VALUES "
            "(?,?,?,?,?,datetime('now',?),?,?)",
            (
                actor["project_id"],
                actor["id"],
                pattern,
                exclusive,
                reason,
                None if ttl is None else f"+{ttl} seconds",
                claim or None,
                ttl,
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


def _request(
    db: sqlite3.Connection,
    actor: dict,
    args: dict,
    declared: frozenset[str] | None = None,
    claim: str = "",
    commits: list[list[str]] | None = None,
) -> dict:
    """Takes the leases that are free and queues a request for the rest.

    A batch that conflicts with nothing is granted exactly as
    ``file_reservation_paths`` grants it, so a lane never has to ask twice
    and no key is queued behind a holder that released it meanwhile. A batch
    that conflicts grants nothing and records one queued request per blocked
    key, naming the holder and the place in that key's queue. Asking twice
    for a key this lane already queued keeps the first request and its
    place rather than taking a second one.

    A queued request reserves nothing. Reservations stay advisory, so a
    queue holds no path, blocks no peer and locks nothing on disk; it
    records who asked first and is read when the holder releases.

    Args:
        db: Open transaction owned by the caller.
        actor: Authenticated project and lane.
        args: Validated tool arguments.
        declared: Resources this project declared, or None when it declared
            none and any well-formed name is acceptable.
        claim: Claim identifier the lane holds, recorded beside each request
            and beside the lease a granted request becomes. Empty records no
            claim.
        commits: Files per recent commit of the base checkout, or None when
            no history is available.

    Returns:
        The granted leases and an empty queue, or the conflicts that granted
        nothing beside one ``queued`` entry per blocked key, each naming the
        request identifier, the key, its holder and its place in that queue.

    Raises:
        BridgeError: If a key is malformed, names an undeclared resource, or
            the lane already queued the maximum number of requests.
    """
    paths, ttl, exclusive, reason = _keys(args, declared)
    _reclaim(db, actor["project_id"])
    leases, conflicts = _conflicts(db, actor, paths, exclusive)
    if not conflicts:
        granted = _grant(
            db, actor, paths, ttl, exclusive, reason, claim, leases, commits
        )
        return {**granted, "queued": []}
    queued = _queue(db, actor, conflicts, ttl, exclusive, reason, claim)
    return _refusal(conflicts, queued)


def _queue(
    db: sqlite3.Connection,
    actor: dict,
    conflicts: list[dict],
    ttl: int | None,
    exclusive: bool,
    reason: str,
    claim: str,
) -> list[dict]:
    """Records this lane's place behind the holders of the blocked keys.

    Args:
        db: Open transaction owned by the caller.
        actor: Authenticated project and lane.
        conflicts: Conflicts the batch raised, in key order.
        ttl: Seconds the granted lease will run for, or None for no deadline.
        exclusive: Whether the batch was asked for exclusively.
        reason: Declared scope, carried onto the lease a grant writes.
        claim: Claim identifier the lane holds, or empty for none.

    Returns:
        One entry per blocked key, in key order, naming the request, the
        holder that blocked it and its place in that key's queue.

    Raises:
        BridgeError: If the lane already queued the maximum number of
            requests.
    """
    live = db.execute(
        "SELECT id,agent_id,path_pattern FROM reservation_requests "
        "WHERE project_id=? AND granted_ts IS NULL AND cancelled_ts IS NULL "
        "ORDER BY id",
        (actor["project_id"],),
    ).fetchall()
    queue: list[dict] = [dict(row) for row in live]
    mine = [row for row in queue if row["agent_id"] == actor["id"]]
    entries: list[dict] = []
    for path in sorted({conflict["path"] for conflict in conflicts}):
        owner = next(
            conflict["owner"]
            for conflict in conflicts
            if conflict["path"] == path
        )
        waiting = next(
            (row for row in mine if row["path_pattern"] == path), None
        )
        if waiting is None:
            if len(mine) >= MAX_QUEUED_REQUESTS:
                raise BridgeError(
                    "Cancel queued reservation requests before adding more."
                )
            cursor = db.execute(
                "INSERT INTO reservation_requests(project_id,agent_id,"
                "path_pattern,exclusive,reason,ttl_seconds,claim_id) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    actor["project_id"],
                    actor["id"],
                    path,
                    exclusive,
                    reason,
                    ttl,
                    claim or None,
                ),
            )
            waiting = {
                "id": cursor.lastrowid,
                "agent_id": actor["id"],
                "path_pattern": path,
            }
            queue.append(waiting)
            mine.append(waiting)
        ahead = sum(
            1
            for row in queue
            if row["id"] < waiting["id"]
            and row["agent_id"] != actor["id"]
            and overlapping(path, row["path_pattern"])
        )
        entries.append(
            {
                "id": waiting["id"],
                "path": path,
                "owner": owner,
                "position": ahead + 1,
            }
        )
    return entries


def _cancel_request(db: sqlite3.Connection, actor: dict, args: dict) -> dict:
    """Withdraws this lane's own queued requests, or one named request.

    Args:
        db: Open transaction owned by the caller.
        actor: Authenticated project and lane.
        args: Validated tool arguments, optionally naming ``request_id``.

    Returns:
        How many queued requests were withdrawn.

    Raises:
        BridgeError: If the named request is not this lane's own queued
            request.
    """
    statement = (
        "UPDATE reservation_requests SET cancelled_ts=CURRENT_TIMESTAMP "
        "WHERE agent_id=? AND granted_ts IS NULL AND cancelled_ts IS NULL"
    )
    if args.get("request_id") is None:
        return {"cancelled": db.execute(statement, (actor["id"],)).rowcount}
    identifier = _number(args["request_id"], "request_id", 1, 2**63 - 1)
    cancelled = db.execute(
        statement + " AND id=?", (actor["id"], identifier)
    ).rowcount
    if not cancelled:
        raise BridgeError("You have no queued request with that identifier.")
    return {"cancelled": cancelled}


def _release(db: sqlite3.Connection, actor: dict) -> dict:
    """Releases this lane's leases and grants what a peer queued for them.

    The release, the grant and the notice that names it are one transaction,
    so no reader sees a key released with its queue untouched, and the lane
    that was told it holds the key holds it in the same committed state.

    Args:
        db: Open transaction owned by the caller.
        actor: Authenticated project and lane.

    Returns:
        How many leases were released and, as ``granted``, one entry per
        lane that took queued keys, naming that lane, its keys and the
        notice it was sent.
    """
    held = db.execute(
        "SELECT path_pattern FROM file_reservations WHERE agent_id=? "
        "AND released_ts IS NULL",
        (actor["id"],),
    ).fetchall()
    released = db.execute(
        "UPDATE file_reservations SET released_ts=CURRENT_TIMESTAMP "
        "WHERE agent_id=? AND released_ts IS NULL",
        (actor["id"],),
    ).rowcount
    granted = _grant_queued(db, actor, sorted({row[0] for row in held}))
    return {"released": released, "granted": granted}


def _grant_queued(
    db: sqlite3.Connection,
    actor: dict,
    released: list[str],
    reclaimed: bool = False,
) -> list[dict]:
    """Hands each released key to the lane that queued for it first.

    Requests are read in the order they were recorded, so the first lane to
    ask for a key is the first to take it, and a key that another lane still
    holds is left queued rather than granted twice. A request whose lane no
    longer holds a credential is left alone: a revoked registration expires
    its requests instead.

    Args:
        db: Open transaction owned by the caller.
        actor: Authenticated project and lane, which is releasing.
        released: Keys this lane just released, in key order.
        reclaimed: Whether the release was the runtime reclaiming expired
            leases rather than the holder releasing them, which the notice
            says so the taking lane knows the holder never handed over.

    Returns:
        One entry per lane granted something, naming that lane, the keys it
        took and the identifier of the single notice it was sent.
    """
    if not released:
        return []
    queued = db.execute(
        "SELECT r.id,r.agent_id,r.path_pattern,r.exclusive,r.reason,"
        "r.ttl_seconds,r.claim_id,a.name FROM reservation_requests r "
        "JOIN agents a ON a.id=r.agent_id WHERE r.project_id=? "
        "AND r.agent_id!=? AND r.granted_ts IS NULL "
        "AND r.cancelled_ts IS NULL AND a.token_digest IS NOT NULL "
        "ORDER BY r.id",
        (actor["project_id"], actor["id"]),
    ).fetchall()
    if not queued:
        return []
    held = [
        (row["agent_id"], row["path_pattern"], row["exclusive"])
        for row in db.execute(
            "SELECT agent_id,path_pattern,exclusive FROM file_reservations "
            "WHERE project_id=? AND released_ts IS NULL",
            (actor["project_id"],),
        )
    ]
    taken: dict[str, list[str]] = {}
    first: dict[str, int] = {}
    for request in queued:
        pattern = request["path_pattern"]
        if not any(overlapping(pattern, key) for key in released):
            continue
        if any(
            owner != request["agent_id"]
            and (request["exclusive"] or exclusive)
            and overlapping(pattern, key)
            for owner, key, exclusive in held
        ):
            continue
        db.execute(
            "UPDATE file_reservations SET released_ts=CURRENT_TIMESTAMP "
            "WHERE agent_id=? AND path_pattern=? AND released_ts IS NULL",
            (request["agent_id"], pattern),
        )
        db.execute(
            "INSERT INTO file_reservations(project_id,agent_id,path_pattern,"
            "exclusive,reason,expires_ts,claim_id,ttl_seconds) VALUES "
            "(?,?,?,?,?,datetime('now',?),?,?)",
            (
                actor["project_id"],
                request["agent_id"],
                pattern,
                request["exclusive"],
                request["reason"],
                (
                    None
                    if request["ttl_seconds"] is None
                    else f"+{request['ttl_seconds']} seconds"
                ),
                request["claim_id"],
                request["ttl_seconds"],
            ),
        )
        db.execute(
            "UPDATE reservation_requests SET granted_ts=CURRENT_TIMESTAMP "
            "WHERE id=?",
            (request["id"],),
        )
        held.append((request["agent_id"], pattern, request["exclusive"]))
        taken.setdefault(request["name"], []).append(pattern)
        first.setdefault(request["name"], request["id"])
    notices = []
    for name in sorted(taken):
        keys = taken[name]
        subject, body = _notice(actor["name"], keys, reclaimed)
        message = _send(
            db,
            actor,
            {
                "to": [name],
                "subject": subject,
                "body_md": body,
                "idempotency_key": f"reservation-granted-{first[name]}",
            },
        )
        notices.append(
            {"agent": name, "paths": keys, "message_id": message["id"]}
        )
    return notices


def _notice(
    holder: str, keys: list[str], reclaimed: bool = False
) -> tuple[str, str]:
    """Words the one notice a lane reads when its queued keys are granted.

    Args:
        holder: Lane that held the keys.
        keys: Keys the reading lane now holds, in the order granted.
        reclaimed: Whether the runtime released the keys because they were
            expired and unworked, rather than the holder releasing them.

    Returns:
        The bounded subject and body of the notice.
    """
    listed = ", ".join(keys)[:MAX_NOTICE_CHARACTERS]
    handed = (
        f"{holder}'s reservation of {listed} expired unrenewed and was released"
        if reclaimed
        else f"{holder} released {listed}"
    )
    return (
        f"Reservation granted: {listed}"[:160],
        f"{handed}, and your queued request for those "
        "keys is granted. The reservation is advisory: it records that you "
        "declared the edit, and nothing on disk is locked. Release it with "
        "release_file_reservations when the work is done.",
    )


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


def _layout(home: Path) -> int:
    """Reads the counter SQLite advances on every change to the store layout.

    Args:
        home: Private bridge state root.

    Returns:
        The store's schema cookie, or -1 when it cannot be read.
    """
    try:
        with contextlib.closing(sqlite3.connect(home / DATABASE)) as db:
            return int(db.execute("PRAGMA schema_version").fetchone()[0])
    except sqlite3.Error:
        return -1


def reconcile(home: Path, cause: str = "") -> str:
    """Migrates a store this build has outgrown and describes the skew.

    A package upgrade reaches every hook on the machine before the service
    restarts, so the first read that names a new column used to fail with a
    raw SQLite message and every lane was refused until an operator stopped
    all of them. The migration is additive and serialized by the store lock,
    so the build that found the skew performs it in place and the lanes keep
    working. A store stamped current that is still missing a column, as the
    failed read names it, is repaired the same way.

    Args:
        home: Private bridge state root.
        cause: Text of the failure that exposed the store, when one did.

    Returns:
        One sentence naming the schema the store carries, the schema this
        build needs and what was done or what closes the gap, or an empty
        string when the store matches this build and needed no repair.
    """
    schema = schema_version(home)
    state = schema_state(schema)
    needed = f"this build needs schema {SCHEMA_VERSION}"
    missing = state == SCHEMA_CURRENT and "no such column" in cause
    if state == SCHEMA_BEHIND or missing:
        before = _layout(home)
        try:
            initialize(home)
        except (BridgeError, OSError, sqlite3.Error) as exc:
            if missing:
                return ""
            return (
                f"Store schema {schema} is behind; {needed}, and migrating "
                f"it in place failed: {exc}. {protocol.MIGRATE}"
            )
        if missing and _layout(home) == before:
            return ""
        found = "lacked a column" if missing else "was behind"
        return (
            f"Store schema {schema} {found}; {needed}. It was migrated in "
            "place without stopping any lane; retry the call."
        )
    if state == SCHEMA_UNSUPPORTED:
        return f"Store schema {schema} is newer; {needed}. {protocol.UPGRADE}"
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

    A recommendation reads the ledger, the recorded plan and, best effort, the
    forge. It writes nothing and its slowest reading is another process, so it
    is answered entirely outside the transaction for the same reason.

    A retirement returns work through the issue ledger and inspects a Git
    worktree before it touches the store at all, so it too runs its own steps
    first and then opens one transaction for the mail, the leases and the
    credential it ends with.
    """
    if tool == "next_issues":
        return _recommended(home, actor, args)
    if tool == RETIRE:
        return _retire(home, actor, started)
    declared = (
        declared_resources(home, str(actor.get("project", "")))
        if tool in RESERVING
        else None
    )
    claim = (
        held_claim(home, str(actor.get("project", "")), actor["name"])
        if tool in (*RESERVING, "send_message")
        else ""
    )
    commits = (
        cochange_history(home, str(actor.get("project", "")))
        if tool in RESERVING
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
        if tool in RESERVING and result.get("conflicts"):
            _record_refusals(db, actor, result["conflicts"])
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


def _lane_provider(directory: Path, display: str) -> str:
    """Names the provider driving the lane a served call authenticated as."""
    try:
        participants = roster.read(directory)["participants"]
    except (BridgeError, OSError, ValueError):
        return ""
    return next(
        (
            str(entry.get("provider", ""))
            for entry in participants.values()
            if entry.get("display") == display
        ),
        "",
    )


def _recommended(home: Path, actor: dict, args: dict) -> dict:
    """Ranks the unclaimed issues the calling lane could take next.

    The recommendation is advice. It claims nothing, offers nothing and
    reserves nothing, so a lane still takes the issue it chooses through the
    explicit claim and still races a peer that chose the same one.

    Args:
        home: Private bridge state root.
        actor: Authenticated project and lane.
        args: Validated tool arguments.

    Returns:
        The lane's provider, whether the forge answered with any paths, and
        the ranked candidates with the reasons for their order.

    Raises:
        BridgeError: If the project keeps no registered state directory, or
            the requested limit is out of bounds.
    """
    root = str(actor.get("project", ""))
    directory = roster.locate(home, root) if root else None
    if directory is None:
        raise BridgeError("This project keeps no issue ledger.")
    limit = _number(
        args.get("limit", recommend.MAX_SHORTLIST),
        "limit",
        1,
        recommend.MAX_SHORTLIST,
    )
    held = active_reservations(home, root)
    held.pop(actor["name"], None)
    return recommend.shortlist(
        directory,
        root,
        Path(root),
        _lane_provider(directory, actor["name"]),
        held,
        overlapping,
        limit,
    )


def _retirement_notice(lane: str, numbers: list[str]) -> tuple[str, str]:
    """Words the notice a lane reads when a peer it handed work to retires.

    Args:
        lane: Lane that retired.
        numbers: Issues that lane had accepted from the reading lane.

    Returns:
        The bounded subject and body of the notice.
    """
    listed = ", ".join(f"#{number}" for number in numbers)[
        :MAX_NOTICE_CHARACTERS
    ]
    return (
        f"Retired, work returned: {listed}"[:160],
        f"{lane} retired and released {listed}, which you had handed to it. "
        "That work is unclaimed again, so claim it back or offer it to "
        "another lane. Nothing was claimed on your behalf.",
    )


def _addressable(
    db: sqlite3.Connection, actor: dict, candidates: dict[str, list[str]]
) -> list[str]:
    """Keeps the identities in a project that can still receive mail.

    A lane that is itself retired, or that never registered, holds no active
    credential and can be told nothing. Leaving it out of the notices keeps a
    retirement from failing on a peer that already left.

    Args:
        db: Open transaction owned by the caller.
        actor: Authenticated project and lane.
        candidates: Registered identities considered, keyed by identity.

    Returns:
        The identities that hold an active credential in this project.
    """
    if not candidates:
        return []
    rows = db.execute(
        "SELECT name FROM agents WHERE project_id=? "
        "AND token_digest IS NOT NULL",
        (actor["project_id"],),
    ).fetchall()
    active = {row["name"] for row in rows}
    return [name for name in candidates if name and name in active]


def _retire(home: Path, actor: dict, started: float) -> dict:
    """Retires the calling lane and invalidates its own credential last.

    The lane's work leaves through the issue ledger and its worktree is
    inspected before the store is touched, because neither can be undone by
    rolling a transaction back. One transaction then releases the advisory
    leases it holds, grants any key a peer was queued for, tells each lane
    that had handed it work where that work went, and invalidates the
    credential the call itself authenticated with.

    Args:
        home: Private bridge state root.
        actor: Authenticated project and lane.
        started: Monotonic reading taken before the call was dispatched.

    Returns:
        What the retirement did, with the leases released, the keys granted to
        queued peers, and the notices sent.

    Raises:
        BridgeError: If the project keeps no registered state directory, or
            the calling lane is not one of its participants.
    """
    from agent_parley import retirement

    root = str(actor.get("project", ""))
    directory = roster.locate(home, root) if root else None
    if directory is None:
        raise BridgeError(NO_PROJECT)
    participants = roster.read(directory)["participants"]
    name = next(
        (
            participant
            for participant, entry in participants.items()
            if entry.get("display") == actor["name"]
        ),
        "",
    )
    if not name:
        raise BridgeError("This lane is not a participant of this project.")
    report = retirement.withdraw(directory, name)
    senders = {
        str(participants[sender].get("display", "")): numbers
        for sender, numbers in report.pop("senders", {}).items()
        if sender in participants
    }
    with connect(home, write=True) as db:
        leases = _release(db, actor)
        notices = []
        for sender in sorted(_addressable(db, actor, senders)):
            subject, body = _retirement_notice(actor["name"], senders[sender])
            message = _send(
                db,
                actor,
                {
                    "to": [sender],
                    "subject": subject,
                    "body_md": body,
                    "idempotency_key": f"retired-{sender}"[:80],
                },
            )
            notices.append(
                {
                    "agent": sender,
                    "issues": senders[sender],
                    "message_id": message["id"],
                }
            )
        result = {
            **report,
            "reservations_released": leases["released"],
            "granted": leases["granted"],
            "notices": notices,
            "credentials_invalidated": _expire(db, root, actor["name"]),
        }
        _event(
            db,
            actor,
            RETIRE,
            "ok",
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


def supersede_claim(home: Path, root: str, claim: str, reason: str) -> int:
    """Retires the outstanding mail one ownership generation sent.

    Every message a lane sends while it holds a claim carries that claim's
    identifier. When the claim closes or moves to another lane, the mail it
    sent stops describing work anybody can still act on, so each delivery
    that is still unread, or still owes an acknowledgement, is marked
    superseded with the reason. Nothing is read, acknowledged or deleted on a
    lane's behalf: the receipts keep their original empty values, and the
    marker only says the sender no longer expects an answer.

    Args:
        home: Private bridge state root.
        root: Canonical project key registered with the store.
        claim: Ownership generation whose mail is being retired. An empty
            identifier retires nothing, because mail that carries no claim
            was never bound to one.
        reason: Why the claim stopped being actionable, recorded beside each
            delivery and bounded before it is stored.

    Returns:
        The number of deliveries marked superseded by this call.
    """
    if not claim or not (home / DATABASE).exists():
        return 0
    with connect(home, write=True) as db:
        cursor = db.execute(
            "UPDATE message_recipients SET superseded_ts=CURRENT_TIMESTAMP,"
            "superseded_reason=? WHERE superseded_ts IS NULL AND EXISTS ("
            "SELECT 1 FROM messages m JOIN projects p ON p.id=m.project_id "
            "WHERE m.id=message_recipients.message_id AND p.human_key=? "
            "AND m.claim_id=? AND (message_recipients.read_ts IS NULL OR "
            "(m.ack_required=1 AND message_recipients.ack_ts IS NULL)))",
            (reason[:MAX_SUPERSEDE_REASON], root, claim),
        )
        return cursor.rowcount


def supersede_recipient(home: Path, root: str, name: str, reason: str) -> int:
    """Retires the outstanding deliveries addressed to a lane that left.

    A retired lane will read nothing and acknowledge nothing, so each of its
    deliveries still unread, or still owing an acknowledgement, is marked
    superseded with the reason. Only that lane's own receipt is marked: a
    peer the same message was also addressed to keeps its delivery exactly
    as it was, and nothing is read, acknowledged or deleted.

    Args:
        home: Private bridge state root.
        root: Canonical project key registered with the store.
        name: Registered identity of the lane that left.
        reason: Why the deliveries stopped being actionable.

    Returns:
        The number of deliveries marked superseded by this call.
    """
    if not (home / DATABASE).exists():
        return 0
    with connect(home, write=True) as db:
        cursor = db.execute(
            "UPDATE message_recipients SET superseded_ts=CURRENT_TIMESTAMP,"
            "superseded_reason=? WHERE superseded_ts IS NULL AND agent_id IN ("
            "SELECT a.id FROM agents a JOIN projects p ON p.id=a.project_id "
            "WHERE p.human_key=? AND a.name=?) AND (read_ts IS NULL OR ("
            "ack_ts IS NULL AND EXISTS (SELECT 1 FROM messages m WHERE "
            "m.id=message_recipients.message_id AND m.ack_required=1)))",
            (reason[:MAX_SUPERSEDE_REASON], root, name),
        )
        return cursor.rowcount


def supersede_project_claim(directory: Path, claim: str, reason: str) -> int:
    """Retires the mail of a claim that a project transition just ended.

    The transition that closes or reassigns a claim is authoritative and has
    already been published when this runs. Supersession is bookkeeping over
    mail that transition made dead, so a store or a manifest that cannot be
    read retires nothing and never fails the transition it follows; the mail
    simply stays live, which is what it was before.

    Args:
        directory: Private project state directory holding the manifest.
        claim: Ownership generation whose mail is being retired.
        reason: Why the claim stopped being actionable.

    Returns:
        The number of deliveries marked superseded, or zero when project
        state could not be read.
    """
    if not claim:
        return 0
    try:
        manifest = roster.read(directory)
        return supersede_claim(
            directory.parent.parent, manifest["root"], claim, reason
        )
    except (BridgeError, OSError, ValueError, KeyError, sqlite3.Error):
        return 0


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
        return _send(db, actor, args, claim, directory, route=True)
    if tool == "review_report":
        if directory is None:
            raise BridgeError(NO_PROJECT)
        return _reviewed(directory, actor, args)
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
    if tool == "request_reservation":
        return _request(db, actor, args, declared, claim, commits)
    if tool == "cancel_reservation_request":
        return _cancel_request(db, actor, args)
    if tool == "release_file_reservations":
        return _release(db, actor)
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


def _reviewed(directory: Path, actor: dict, args: dict) -> dict:
    """Records the calling lane's verdict on a peer's report.

    The lane behind the served call is the reviewer, so a lane can no more
    review its own report over this tool than it can from its own command
    line. The verdict is that lane's own claim about work it did not do: it
    approves nothing, moves no ownership and gates no integration.

    The report log lives in coordination state rather than in the store, so
    the verdict is written where reports are kept and only its served call is
    recorded here.

    Args:
        directory: Private state directory for the common repository.
        actor: Authenticated project and lane.
        args: Validated tool arguments.

    Returns:
        The recorded verdict.

    Raises:
        BridgeError: If the caller is not a registered participant, the
            report identifier is malformed, no lane recorded that report, or
            the calling lane wrote it.
    """
    from agent_parley import metrics

    identifier = args.get("report_id")
    if not isinstance(identifier, str) or not identifier.strip():
        raise BridgeError("report_id must name a recorded report.")
    participants = roster.read(directory)["participants"]
    reviewer = next(
        (
            name
            for name, entry in participants.items()
            if entry.get("display") == actor["name"]
        ),
        "",
    )
    if not reviewer:
        raise BridgeError("This lane is not a participant of this project.")
    return metrics.record_review(
        directory,
        list(participants),
        reviewer,
        identifier.strip(),
        str(args.get("verdict", "")),
        str(args.get("evidence", "")),
    )


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


def _unanswered(home: Path, root: str, deadline: str) -> list[dict]:
    """Groups the unanswered acknowledgement rows one deadline clause selects.

    Args:
        home: Private bridge state root.
        root: Canonical project key registered with the store.
        deadline: Clause restricting the recorded deadline, appended to the
            selection of every acknowledgement no recipient has answered.

    Returns:
        One entry per message, oldest message first, naming the sender, the
        subject, how long the message has waited, how long its deadline has
        been past and the registered identities that have not acknowledged
        it. A broadcast is one entry holding every silent recipient rather
        than one entry per recipient. A project with no store yet has nothing
        to report.
    """
    if not (home / DATABASE).exists():
        return []
    with connect(home) as db:
        rows = db.execute(
            "SELECT m.id AS message_id,m.subject AS subject,"
            "s.name AS sender,a.name AS recipient,"
            "CAST((julianday('now')-julianday(m.created_ts))*86400 "
            "AS INTEGER) AS waiting,"
            "CAST((julianday('now')-julianday(m.ack_deadline_ts))*86400 "
            "AS INTEGER) AS overdue FROM messages m "
            "JOIN message_recipients r ON r.message_id=m.id "
            "JOIN agents a ON a.id=r.agent_id "
            "JOIN agents s ON s.id=m.sender_id "
            "JOIN projects p ON p.id=m.project_id "
            "WHERE p.human_key=? AND m.ack_required=1 AND r.ack_ts IS NULL "
            f"AND r.superseded_ts IS NULL AND {deadline} ORDER BY m.id,a.name",
            (root,),
        ).fetchall()
    breaches: dict[int, dict] = {}
    for row in rows:
        breach = breaches.setdefault(
            row["message_id"],
            {
                "message_id": row["message_id"],
                "subject": row["subject"],
                "sender": row["sender"],
                "waiting_seconds": max(0, int(row["waiting"] or 0)),
                "overdue_seconds": max(0, int(row["overdue"] or 0)),
                "recipients": [],
            },
        )
        breach["recipients"].append(row["recipient"])
    return list(breaches.values())


def overdue_acknowledgements(home: Path, root: str) -> list[dict]:
    """Lists the acknowledgement requests whose deadline passed unanswered.

    Args:
        home: Private bridge state root.
        root: Canonical project key registered with the store.

    Returns:
        One entry per message as :func:`_unanswered` shapes it, restricted to
        the messages whose recorded deadline is already past.
    """
    return _unanswered(
        home,
        root,
        "m.ack_deadline_ts IS NOT NULL AND m.ack_deadline_ts<=datetime('now')",
    )


def pending_acknowledgements(home: Path, root: str) -> list[dict]:
    """Lists the acknowledgement requests still inside their deadline.

    These are the requests a recipient may yet answer, so they are exactly
    the ones a fitness reading can still act on: past the deadline the
    request belongs to :func:`overdue_acknowledgements`, which returns it to
    its sender and retires it. The two sets never overlap.

    Args:
        home: Private bridge state root.
        root: Canonical project key registered with the store.

    Returns:
        One entry per message as :func:`_unanswered` shapes it, restricted to
        the messages whose deadline has not passed or was never recorded.
    """
    return _unanswered(
        home,
        root,
        "(m.ack_deadline_ts IS NULL OR m.ack_deadline_ts>datetime('now'))",
    )


def retire_acknowledgement(home: Path, root: str, identifier: int) -> bool:
    """Retires one acknowledgement expectation whose deadline has passed.

    The expectation is cleared on the message instead of being recorded as an
    answer: a recipient that never acknowledged keeps no acknowledgement time,
    so what happened stays readable while the message stops being reported as
    outstanding. Retiring acknowledges nothing for any lane, moves no
    ownership and deletes no mail.

    Args:
        home: Private bridge state root.
        root: Canonical project key registered with the store.
        identifier: Message whose acknowledgement expectation is retired.

    Returns:
        Whether an outstanding expectation was retired by this call.
    """
    if not (home / DATABASE).exists():
        return False
    with connect(home, write=True) as db:
        cursor = db.execute(
            "UPDATE messages SET ack_required=0 WHERE id=? AND ack_required=1 "
            "AND project_id=(SELECT id FROM projects WHERE human_key=?)",
            (identifier, root),
        )
        return cursor.rowcount > 0


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

    Mail superseded by a claim that closed or moved is not an unanswered
    item: nobody is waiting on it, so it never makes a lane read as stalled.

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
                f"WHERE r.agent_id=? AND r.superseded_ts IS NULL "
                f"AND {condition} ORDER BY m.id LIMIT 1",
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


def _waiting_on(db: sqlite3.Connection, root: str) -> dict[str, list[str]]:
    """Maps each holding lane to the lanes queued behind the keys it holds.

    A queued request is matched to a holder by the same overlap rule that
    refused it, so a request queued on a directory or a glob is read under
    the lane whose lease blocked it, and a request blocked by two lanes is
    reported under both.

    Args:
        db: Open read transaction to answer from.
        root: Canonical project key registered with the store.

    Returns:
        Mapping of holding identity to the requesting identities waiting on
        it, one entry per queued request.
    """
    requests = db.execute(
        "SELECT a.name AS name,r.path_pattern AS pattern,"
        "r.agent_id AS agent_id,r.exclusive AS exclusive "
        "FROM reservation_requests r JOIN agents a ON a.id=r.agent_id "
        "JOIN projects p ON p.id=r.project_id WHERE p.human_key=? "
        "AND r.granted_ts IS NULL AND r.cancelled_ts IS NULL ORDER BY r.id",
        (root,),
    ).fetchall()
    if not requests:
        return {}
    leases = db.execute(
        "SELECT a.name AS name,f.path_pattern AS pattern,"
        "f.agent_id AS agent_id,f.exclusive AS exclusive "
        "FROM file_reservations f JOIN agents a ON a.id=f.agent_id "
        "JOIN projects p ON p.id=f.project_id WHERE p.human_key=? "
        "AND f.released_ts IS NULL",
        (root,),
    ).fetchall()
    waiting: dict[str, list[str]] = {}
    for request in requests:
        blockers = {
            lease["name"]
            for lease in leases
            if lease["agent_id"] != request["agent_id"]
            and (request["exclusive"] or lease["exclusive"])
            and overlapping(request["pattern"], lease["pattern"])
        }
        for holder in blockers:
            waiting.setdefault(holder, []).append(request["name"])
    return waiting


def _record_refusals(
    db: sqlite3.Connection, actor: dict, conflicts: list[dict]
) -> None:
    """Records which holder refused this lane which key.

    A refused lane that did not queue leaves nothing behind in the queue, so
    an idle holder blocking it was visible only to the lane that asked. One
    row per refused lane, holder and key is kept at its latest refusal, and
    rows older than `REFUSAL_SECONDS` are retired on every write.

    Args:
        db: Open transaction that also carries the refused call.
        actor: Authenticated project and the lane that was refused.
        conflicts: Conflicts the refused batch reported.
    """
    now = time.time()
    db.executemany(
        "INSERT INTO reservation_refusals(project_id,agent_id,holder,"
        "path_pattern,refused_ts) VALUES (?,?,?,?,?) "
        "ON CONFLICT(project_id,agent_id,holder,path_pattern) "
        "DO UPDATE SET refused_ts=excluded.refused_ts",
        [
            (
                actor["project_id"],
                actor["id"],
                conflict["owner"],
                conflict["path"],
                now,
            )
            for conflict in conflicts
        ],
    )
    db.execute(
        "DELETE FROM reservation_refusals WHERE project_id=? AND refused_ts<?",
        (actor["project_id"], now - REFUSAL_SECONDS),
    )


def _refused_by(db: sqlite3.Connection, root: str) -> dict[str, list[str]]:
    """Maps each holding lane to the lanes it refused and still blocks.

    A refusal is reported under its holder only while that holder keeps a
    live lease overlapping the refused key, so a holder that released,
    expired into reclamation or was reclaimed drops out without a separate
    cleanup.

    Args:
        db: Open read transaction to answer from.
        root: Canonical project key registered with the store.

    Returns:
        Mapping of holding identity to the sorted identities it refused.
    """
    refusals = db.execute(
        "SELECT r.holder AS holder,r.path_pattern AS pattern,"
        "a.name AS name FROM reservation_refusals r "
        "JOIN agents a ON a.id=r.agent_id "
        "JOIN projects p ON p.id=r.project_id WHERE p.human_key=?",
        (root,),
    ).fetchall()
    if not refusals:
        return {}
    leases = db.execute(
        "SELECT a.name AS name,f.path_pattern AS pattern "
        "FROM file_reservations f JOIN agents a ON a.id=f.agent_id "
        "JOIN projects p ON p.id=f.project_id WHERE p.human_key=? "
        "AND f.released_ts IS NULL",
        (root,),
    ).fetchall()
    refused: dict[str, set[str]] = {}
    for refusal in refusals:
        if any(
            lease["name"] == refusal["holder"]
            and overlapping(refusal["pattern"], lease["pattern"])
            for lease in leases
        ):
            refused.setdefault(refusal["holder"], set()).add(refusal["name"])
    return {holder: sorted(names) for holder, names in refused.items()}


def usage(
    home: Path, root: str, *, db: sqlite3.Connection | None = None
) -> dict[str, dict]:
    """Reports retained tool events, held leases and their queues.

    Args:
        home: Private bridge state root.
        root: Canonical project key registered with the store.
        db: Open read transaction to answer from, or ``None`` to open one.

    Returns:
        Mapping of registered identity to served calls, rejected calls,
        returned bytes, held leases, how many of those leases are past a
        declared time to live, the age of its oldest held lease, how long
        the oldest expired one has been expired, and the queued reservation
        requests waiting on the keys it holds with the lanes that asked,
        and the lanes it refused a key it still holds as ``refused``. An
        expired lease is counted apart from the live ones and keeps blocking
        until it is renewed, released or reclaimed; a queued request holds
        nothing of its own. Counts cover retained events only; older events
        are retired.
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
                "stale_lease_age": 0,
                "queued": 0,
                "queued_by": [],
                "refused": [],
            }
        for holder, waiting in _waiting_on(db, root).items():
            report.setdefault(holder, {}).update(
                queued=len(waiting), queued_by=sorted(set(waiting))
            )
        for holder, refused in _refused_by(db, root).items():
            report.setdefault(holder, {}).update(refused=refused)
        for row in db.execute(
            "SELECT a.name AS name,count(*) AS leases,"
            "coalesce(sum(f.expires_ts IS NOT NULL "
            "AND f.expires_ts<=CURRENT_TIMESTAMP),0) AS stale_leases,"
            "cast(strftime('%s','now')-strftime('%s',min(f.created_ts)) "
            "AS INTEGER) AS lease_age,"
            "coalesce(max(0,unixepoch('now')-unixepoch(min(CASE WHEN "
            "f.expires_ts<=CURRENT_TIMESTAMP THEN f.expires_ts END))),0) "
            "AS stale_lease_age FROM file_reservations f "
            "JOIN agents a ON a.id=f.agent_id "
            "JOIN projects p ON p.id=a.project_id WHERE p.human_key=? "
            "AND f.released_ts IS NULL GROUP BY a.id",
            (root,),
        ):
            report.setdefault(row["name"], {}).update(
                leases=row["leases"],
                stale_leases=row["stale_leases"],
                lease_age=max(0, row["lease_age"] or 0),
                stale_lease_age=row["stale_lease_age"],
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
            "SELECT id,path_pattern,exclusive,reason,expires_ts,ttl_seconds "
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
                "path_pattern,exclusive,reason,expires_ts,claim_id,"
                "ttl_seconds) VALUES (?,?,?,?,?,?,?,?)",
                (
                    receiver["project_id"],
                    receiver["id"],
                    lease["path_pattern"],
                    lease["exclusive"],
                    lease["reason"],
                    lease["expires_ts"],
                    claim or None,
                    lease["ttl_seconds"],
                ),
            )
            moved.append(lease["path_pattern"])
    return moved


def transfer_claim_reservations(
    home: Path,
    root: str,
    source: str,
    target: str,
    source_claim: str,
    target_claim: str,
) -> list[str]:
    """Moves only reservations correlated with one recovered claim.

    Args:
        home: Private bridge state root.
        root: Canonical project key registered with the store.
        source: Registered identity that lost the claim.
        target: Registered identity that recovered the claim.
        source_claim: Previous ownership generation recorded on reservations.
        target_claim: New ownership generation recorded on moved reservations.

    Returns:
        Keys moved in sorted order. Reservations for other claims survive.

    Raises:
        BridgeError: If no store exists or either identity is unregistered.
    """
    if not source_claim:
        return []
    if not (home / DATABASE).exists():
        raise BridgeError("No coordination store yet; run agent-parley up.")
    moved: list[str] = []
    with connect(home, write=True) as db:
        holder = _identify(db, root, source)
        receiver = _identify(db, root, target)
        held = db.execute(
            "SELECT id,path_pattern,exclusive,reason,expires_ts,ttl_seconds "
            "FROM file_reservations WHERE project_id=? AND agent_id=? "
            "AND claim_id=? AND released_ts IS NULL ORDER BY path_pattern",
            (holder["project_id"], holder["id"], source_claim),
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
                "path_pattern,exclusive,reason,expires_ts,claim_id,"
                "ttl_seconds) VALUES (?,?,?,?,?,?,?,?)",
                (
                    receiver["project_id"],
                    receiver["id"],
                    lease["path_pattern"],
                    lease["exclusive"],
                    lease["reason"],
                    lease["expires_ts"],
                    target_claim or None,
                    lease["ttl_seconds"],
                ),
            )
            moved.append(lease["path_pattern"])
    return moved


def release_reservations(home: Path, root: str, name: str) -> list[str]:
    """Releases every advisory reservation one lane still holds.

    Reservations are advisory declarations of intent, never enforced file
    system locks. A lane whose claims a peer has taken holds declarations that
    no longer describe anybody's work, so the take releases them in one store
    transaction and the keys read as free to whoever reserves them next.

    Nothing is granted to the taking lane here: it reserves what it needs
    itself, which keeps every grant a declaration a lane made for itself.

    Args:
        home: Private bridge state root.
        root: Canonical project key registered with the store.
        name: Registered identity whose reservations are released.

    Returns:
        The released keys, in sorted order, including a lease past its
        declared time to live, which is still held until it is released.

    Raises:
        BridgeError: If no store exists or the identity is unregistered.
    """
    if not (home / DATABASE).exists():
        raise BridgeError("No coordination store yet; run agent-parley up.")
    with connect(home, write=True) as db:
        holder = _identify(db, root, name)
        released = [
            row["path_pattern"]
            for row in db.execute(
                "SELECT path_pattern FROM file_reservations WHERE "
                "project_id=? AND agent_id=? AND released_ts IS NULL "
                "ORDER BY path_pattern",
                (holder["project_id"], holder["id"]),
            )
        ]
        db.execute(
            "UPDATE file_reservations SET released_ts=CURRENT_TIMESTAMP "
            "WHERE project_id=? AND agent_id=? AND released_ts IS NULL",
            (holder["project_id"], holder["id"]),
        )
    return released


def reclaim_expired(home: Path, root: str) -> list[dict]:
    """Sweeps one project's expired leases so a queue is never held forever.

    A queued request is only read when a key is released, so a holder that
    stops coordinating would otherwise keep a peer waiting without limit.
    Sweeping releases the leases no live session is working under and grants
    what peers queued for them, and both lanes are told in the same
    transaction. Reservations stay advisory; nothing on disk is affected.

    Args:
        home: Private bridge state root.
        root: Canonical project key registered with the store.

    Returns:
        One entry per holder whose leases were reclaimed, naming that holder,
        the released keys, the notice it was sent and the lanes that took the
        keys. An unregistered project or an absent store reclaims nothing.
    """
    if not (home / DATABASE).exists():
        return []
    with connect(home, write=True) as db:
        row = db.execute(
            "SELECT id FROM projects WHERE human_key=?", (root,)
        ).fetchone()
        return _reclaim(db, int(row[0])) if row else []


def renew_reservations(home: Path, root: str, name: str) -> dict:
    """Renews a working lane's expired leases from its own checkpoint.

    A lane that is still editing the keys it declared keeps them by
    coordinating: each checkpoint restores the window its holder asked for,
    so an expired lease belonging to live work is never reclaimed from it. A
    lease correlated with a claim that lane no longer holds describes nobody's
    work, so it is released instead, its queue is granted, and the lane is
    told which keys it lost and why.

    Args:
        home: Private bridge state root.
        root: Canonical project key registered with the store.
        name: Registered identity whose own leases are renewed.

    Returns:
        The renewed keys and the released keys, both in key order.

    Raises:
        BridgeError: If no store exists or the identity is unregistered.
    """
    if not (home / DATABASE).exists():
        raise BridgeError("No coordination store yet; run agent-parley up.")
    claim = held_claim(home, root, name)
    with connect(home, write=True) as db:
        holder = _identify(db, root, name)
        expired = db.execute(
            "SELECT id,path_pattern,claim_id,ttl_seconds FROM "
            "file_reservations WHERE project_id=? AND agent_id=? "
            "AND released_ts IS NULL AND expires_ts IS NOT NULL "
            "AND expires_ts<=CURRENT_TIMESTAMP ORDER BY path_pattern",
            (holder["project_id"], holder["id"]),
        ).fetchall()
        renewed: list[str] = []
        dropped: list[sqlite3.Row] = []
        for lease in expired:
            if lease["claim_id"] and lease["claim_id"] != claim:
                dropped.append(lease)
                continue
            window = lease["ttl_seconds"] or RESERVATION_GRACE
            db.execute(
                "UPDATE file_reservations SET expires_ts=datetime('now',?) "
                "WHERE id=?",
                (f"+{window} seconds", lease["id"]),
            )
            renewed.append(lease["path_pattern"])
        released = sorted({lease["path_pattern"] for lease in dropped})
        if dropped:
            db.execute(
                "UPDATE file_reservations SET released_ts=CURRENT_TIMESTAMP "
                "WHERE id IN (" + ",".join("?" * len(dropped)) + ")",
                tuple(lease["id"] for lease in dropped),
            )
            _grant_queued(db, holder, released, reclaimed=True)
            _send(
                db,
                _operator(db, holder["project_id"]),
                {
                    "to": [name],
                    "subject": (
                        "Reservation released with its claim: "
                        + ", ".join(released)
                    )[:160],
                    "body_md": (
                        "Your reservation of "
                        + ", ".join(released)[:MAX_NOTICE_CHARACTERS]
                        + " passed its declared time to live under a claim "
                        "you no longer hold, so it was released and any peer "
                        "queued for those keys now holds them. Reservations "
                        "are advisory: nothing on disk was locked or "
                        "reverted."
                    ),
                    "idempotency_key": (
                        f"reservation-claim-closed-"
                        f"{max(lease['id'] for lease in dropped)}"
                    ),
                },
            )
        return {"renewed": sorted(set(renewed)), "released": released}


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
