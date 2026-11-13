"""Settled coordination rows past the retention window are pruned."""

import contextlib
import sqlite3
import time

from agent_parley import attachments, store

DAY = 86400


def actor(bridge, root, name):
    """Registers one lane and resolves its own store identity."""
    store.initialize(bridge.home)
    token = store.register(bridge.home, root, name)["registration_token"]
    resolved = store.authenticate(bridge.home, token)
    assert resolved is not None
    return resolved


def send(bridge, sender, key, body="Status update.", **arguments):
    """Sends one message to codex and returns its identifier."""
    return store.call(
        bridge.home,
        sender,
        "send_message",
        {
            "to": ["codex"],
            "subject": f"Note {key}",
            "body_md": body,
            "idempotency_key": key,
            **arguments,
        },
    )["id"]


def age(bridge, days):
    """Moves every recorded coordination row back by a number of days."""
    shift = f"-{days} days"
    with contextlib.closing(
        sqlite3.connect(bridge.home / store.DATABASE)
    ) as db:
        db.execute(
            "UPDATE messages SET created_ts=datetime(created_ts,?)", (shift,)
        )
        db.execute(
            "UPDATE file_reservations SET released_ts=datetime(released_ts,?)",
            (shift,),
        )
        db.commit()


def ids(bridge, table):
    """Lists the identifiers a table still holds."""
    with contextlib.closing(
        sqlite3.connect(bridge.home / store.DATABASE)
    ) as db:
        return {row[0] for row in db.execute(f"SELECT id FROM {table}")}


def test_prune_keeps_unread_unacknowledged_decision_and_long_mail(
    bridge, repo, paired
):
    claude = actor(bridge, paired["root"], "claude")
    codex = actor(bridge, paired["root"], "codex")
    read = send(bridge, claude, "read")
    acked = send(bridge, claude, "acked", ack_required=True)
    pending = send(bridge, claude, "pending", ack_required=True)
    unread = send(bridge, claude, "unread")
    decided = send(bridge, claude, "decided", decision=True)
    long = send(bridge, claude, "long", body="L" * 5000)
    for message in (read, acked, pending, decided, long):
        store.call(
            bridge.home, codex, "mark_message_read", {"message_id": message}
        )
    store.call(bridge.home, codex, "acknowledge_message", {"message_id": acked})
    newest = send(bridge, claude, "newest")
    store.call(bridge.home, codex, "mark_message_read", {"message_id": newest})
    age(bridge, 400)
    counts = store.prune(bridge.home, time.time())
    assert counts["messages"] == 2
    assert ids(bridge, "messages") == {
        pending,
        unread,
        decided,
        long,
        newest,
    }
    later = send(bridge, claude, "later")
    assert later > newest
    with contextlib.closing(
        sqlite3.connect(bridge.home / store.DATABASE)
    ) as db:
        db.execute(
            "INSERT INTO message_search(message_search) "
            "VALUES ('integrity-check')"
        )
    found = store.call(
        bridge.home, codex, "search_messages", {"query": "Note"}
    )["messages"]
    assert {hit["id"] for hit in found} >= {pending, unread}
    directory = bridge.project(repo)[1]
    assert attachments.held(directory, "claude") == {
        attachments.reference("message", long): 5000
    }


def test_prune_leaves_recent_settled_rows(bridge, repo, paired):
    claude = actor(bridge, paired["root"], "claude")
    codex = actor(bridge, paired["root"], "codex")
    first = send(bridge, claude, "first")
    send(bridge, claude, "second")
    store.call(bridge.home, codex, "mark_message_read", {"message_id": first})
    age(bridge, 30)
    assert sum(store.prune(bridge.home, time.time()).values()) == 0
    assert first in ids(bridge, "messages")


def test_prune_drops_released_reservations_and_keeps_live_ones(
    bridge, repo, paired
):
    claude = actor(bridge, paired["root"], "claude")
    codex = actor(bridge, paired["root"], "codex")
    for path in ("src/a.py", "src/b.py"):
        store.call(
            bridge.home, claude, "file_reservation_paths", {"paths": [path]}
        )
        store.call(bridge.home, claude, "release_file_reservations", {})
    store.call(
        bridge.home, codex, "file_reservation_paths", {"paths": ["src/c.py"]}
    )
    before = ids(bridge, "file_reservations")
    age(bridge, 400)
    counts = store.prune(bridge.home, time.time())
    assert counts["file_reservations"] == 2
    kept = ids(bridge, "file_reservations")
    assert kept == {max(before)}
    with contextlib.closing(
        sqlite3.connect(bridge.home / store.DATABASE)
    ) as db:
        assert db.execute(
            "SELECT count(*) FROM file_reservations WHERE released_ts IS NULL"
        ).fetchone() == (1,)


def test_prune_is_a_no_op_without_a_store(tmp_path):
    counts = store.prune(tmp_path, time.time())
    assert set(counts.values()) == {0}
