"""Tests transactional coordination, transport isolation and context budgets."""

import asyncio
import contextlib
import hashlib
import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest

from agent_parley import store
from agent_parley.checkpoints import MAX_CONTEXT_BYTES, checkpoint, mailbox
from agent_parley.issues import describe
from agent_parley.server import MAX_REQUEST_BYTES, TOOLS
from agent_parley.state import BridgeError


@pytest.fixture
def actors(bridge):
    """Registers two independent scopes in an initialized local database."""
    store.initialize(bridge.home)
    identities = [
        store.register(bridge.home, "/project", name)
        for name in ("GreenCastle", "BlueLake")
    ]
    return [
        store.authenticate(bridge.home, row["registration_token"])
        for row in identities
    ]


def message(**overrides):
    """Builds a valid small message for mutation-focused tests."""
    return {
        "to": ["BlueLake"],
        "subject": "Contract",
        "body_md": "Changed",
        "idempotency_key": "contract-1",
        **overrides,
    }


def test_concurrent_sends_are_idempotent_and_changed_retries_fail(
    bridge, actors
):
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(
                lambda _: store.call(
                    bridge.home, actors[0], "send_message", message()
                ),
                range(16),
            )
        )
    assert len({row["id"] for row in results}) == 1
    with pytest.raises(BridgeError, match="another message"):
        store.call(
            bridge.home, actors[0], "send_message", message(body_md="Different")
        )
    inbox = store.call(bridge.home, actors[1], "fetch_inbox", {})
    assert len(inbox["messages"]) == 1
    assert "body_md" not in inbox["messages"][0]


def test_ownership_listing_reports_liveness_and_keeps_the_owner():
    state = {
        "revision": 1,
        "issues": {"7": {"owner": "claude-1", "offer": None, "history": []}},
    }
    assert describe(state) == "#7: claude-1"
    assert describe(state, {"codex-1": "working"}) == "#7: claude-1"
    assert describe(state, {"claude-1": "stopped; event 900s ago"}) == (
        "#7: claude-1 (stopped; event 900s ago)"
    )


def test_ownership_listing_names_the_holder_of_each_blocking_issue():
    state = {
        "revision": 4,
        "issues": {
            "7": {
                "owner": "claude-1",
                "offer": None,
                "blocked_by": ["4", "9"],
                "history": [],
            },
            "4": {"owner": "codex-1", "offer": None, "history": []},
        },
    }
    assert describe(state) == (
        "#4: codex-1\n#7: claude-1; waits on #4 (codex-1), #9 (unclaimed)"
    )


def test_reservations_serialize_conflicts_renew_and_expire(bridge, actors):
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda actor: store.call(
                    bridge.home,
                    actor,
                    "file_reservation_paths",
                    {
                        "paths": ["./src/**"],
                        "ttl_seconds": 30,
                        "reason": "refactor store",
                    },
                ),
                actors,
            )
        )
    assert sum(bool(row["granted"]) for row in results) == 1
    loser = actors[next(i for i, row in enumerate(results) if row["conflicts"])]
    denied = store.call(
        bridge.home,
        loser,
        "file_reservation_paths",
        {"paths": ["safe.py", "src/api.py"]},
    )
    assert denied["conflicts"] and denied["granted"] == []
    assert denied["conflicts"][0]["reason"] == "refactor store"
    with store.connect(bridge.home, write=True) as db:
        db.execute("UPDATE file_reservations SET expires_ts='2000-01-01'")
    assert store.call(
        bridge.home, loser, "file_reservation_paths", {"paths": ["src/api.py"]}
    )["granted"]
    assert store.call(
        bridge.home, loser, "file_reservation_paths", {"paths": ["src/api.py"]}
    )["granted"]
    other = next(actor for actor in actors if actor is not loser)
    silent = store.call(
        bridge.home, other, "file_reservation_paths", {"paths": ["src/api.py"]}
    )
    assert silent["granted"] == []
    assert "reason" not in silent["conflicts"][0]
    with store.connect(bridge.home) as db:
        assert (
            db.execute(
                "SELECT count(*) FROM file_reservations WHERE "
                "expires_ts>CURRENT_TIMESTAMP AND released_ts IS NULL"
            ).fetchone()[0]
            == 1
        )


@pytest.mark.parametrize(
    "tool,args",
    [
        ("file_reservation_paths", {"paths": ["../peer/file"]}),
        ("file_reservation_paths", {"paths": ["/tmp/file"]}),
        ("file_reservation_paths", {"paths": ["."]}),
        ("file_reservation_paths", {"paths": ["a"], "ttl_seconds": True}),
        ("file_reservation_paths", {"paths": ["a"], "exclusive": "false"}),
        ("send_message", message(body_md="x" * 4097)),
        ("send_message", message(body_md="😀" * 1025)),
        ("send_message", message(body_md="\ud800")),
        ("send_message", message(to=["Foreign"])),
        ("send_message", message(ack_required=1)),
        ("fetch_inbox", {"after_id": -1}),
        ("fetch_inbox", {"limit": 100}),
        ("acknowledge_message", {"message_id": 999}),
    ],
)
def test_invalid_input_has_no_side_effects(bridge, actors, tool, args):
    with pytest.raises(BridgeError):
        store.call(bridge.home, actors[0], tool, args)
    with store.connect(bridge.home) as db:
        assert db.execute("SELECT count(*) FROM messages").fetchone()[0] == 0
        assert (
            db.execute("SELECT count(*) FROM file_reservations").fetchone()[0]
            == 0
        )


def test_transport_scopes_identity_and_rejects_foreign_origins(bridge, actors):
    bridge.up()
    identity = store.register(bridge.home, "/project", "BlueLake")
    token = identity["registration_token"]
    stranger = store.register(bridge.home, "/other", "BlueLake")
    sent = store.call(bridge.home, actors[0], "send_message", message())
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "acknowledge_message",
            "arguments": {"message_id": sent["id"]},
        },
    }
    with httpx.Client(base_url=bridge.url, trust_env=False) as client:
        foreign = client.post(
            "/mcp/",
            json=request,
            headers={
                "Authorization": f"Bearer {stranger['registration_token']}"
            },
        )
        assert foreign.json()["result"]["isError"]
        headers = {"Authorization": f"Bearer {token}"}
        assert (
            client.post(
                "/mcp/",
                json=request,
                headers={**headers, "Origin": "https://evil.example"},
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/mcp/",
                json=request,
                headers={**headers, "Host": "evil.example"},
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/mcp/",
                json=request,
                headers={**headers, "MCP-Protocol-Version": "1900-01-01"},
            ).status_code
            == 400
        )
        assert (
            client.post(
                "/mcp/",
                content=b"x" * (MAX_REQUEST_BYTES + 1),
                headers={**headers, "Content-Type": "application/json"},
            ).status_code
            == 413
        )
        request["params"]["arguments"]["agent_name"] = "GreenCastle"
        assert client.post("/mcp/", json=request, headers=headers).json()[
            "result"
        ]["isError"]
        request["params"]["arguments"].pop("agent_name")
        assert (
            not client.post("/mcp/", json=request, headers=headers)
            .json()["result"]
            .get("isError")
        )
        assert client.get("/mcp/", headers=headers).status_code == 405


def test_context_is_bounded_incremental_and_never_auto_acknowledges(
    bridge, repo, paired
):
    data = paired
    bridge.up()
    for lane in ("claude", "codex"):
        asyncio.run(bridge.identity(lane, data))
    directory = Path(data["lanes"]["codex"]).parent
    identity = json.loads((directory / "claude-identity.json").read_text())
    actor = store.authenticate(bridge.home, identity["registration_token"])
    for index in range(9):
        store.call(
            bridge.home,
            actor,
            "send_message",
            message(
                to=["codex"],
                subject="新" * 50,
                body_md="😀" * 1000,
                ack_required=True,
                idempotency_key=str(index),
            ),
        )
    payload = {
        "hook_event_name": "PreToolUse",
        "session_id": "budget",
        "cwd": data["lanes"]["codex"],
        "tool_name": "apply_patch",
    }
    seen = []
    for _ in range(3):
        result = checkpoint(bridge.home, directory, "codex", payload)
        text = result["hookSpecificOutput"]["additionalContext"]
        assert len(text.encode()) <= MAX_CONTEXT_BYTES
        seen.append(text)
    assert len(set(seen)) == 3
    for _ in range(20):
        assert checkpoint(bridge.home, directory, "codex", payload) == {}
    assert mailbox(bridge.home, data["root"], "codex")["pending_ack"] == 9
    state = json.loads((directory / "codex-activity.json").read_text())
    assert state["injections"] == 3
    assert state["injected_bytes"] == sum(len(text.encode()) for text in seen)
    assert len(json.dumps(TOOLS).encode()) < 5000


def test_tool_events_record_served_and_rejected_calls_within_a_bound(
    bridge, actors, monkeypatch
):
    monkeypatch.setattr(store, "MAX_EVENT_ROWS", 4)
    sent = store.call(bridge.home, actors[0], "send_message", message())
    store.call(bridge.home, actors[1], "fetch_inbox", {})
    with pytest.raises(BridgeError):
        store.call(
            bridge.home,
            actors[1],
            "acknowledge_message",
            {"message_id": sent["id"] + 999},
        )
    with store.connect(bridge.home) as db:
        rows = db.execute(
            "SELECT tool,outcome,result_bytes,duration_ms FROM events "
            "ORDER BY id"
        ).fetchall()
    assert [(row["tool"], row["outcome"]) for row in rows] == [
        ("send_message", "ok"),
        ("fetch_inbox", "ok"),
        ("acknowledge_message", "error"),
    ]
    assert rows[1]["result_bytes"] > 0 and rows[2]["result_bytes"] == 0
    assert all(row["duration_ms"] >= 0 for row in rows)
    for index in range(6):
        store.call(
            bridge.home,
            actors[0],
            "send_message",
            message(idempotency_key=f"bounded-{index}"),
        )
    with store.connect(bridge.home) as db:
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 4


def test_schema_upgrade_adds_events_and_lease_age_without_rewrites(
    bridge, actors
):
    store.call(bridge.home, actors[0], "send_message", message())
    store.call(
        bridge.home, actors[0], "file_reservation_paths", {"paths": ["src/a"]}
    )
    with store.connect(bridge.home, write=True) as db:
        db.execute("DROP TABLE events")
        db.execute("ALTER TABLE file_reservations DROP COLUMN created_ts")
        db.execute("PRAGMA user_version=1")
    store.initialize(bridge.home)
    with store.connect(bridge.home) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM messages").fetchone()[0] == 1
        assert (
            db.execute(
                "SELECT count(*) FROM file_reservations "
                "WHERE created_ts IS NULL"
            ).fetchone()[0]
            == 0
        )
    usage = store.usage(bridge.home, "/project")
    assert usage["GreenCastle"]["leases"] == 1
    assert usage["GreenCastle"]["lease_age"] >= 0


def test_reservation_conflicts_stay_inside_the_response_budget(bridge, actors):
    globs = [f"src/{'deep/' * 40}{index}/*.py" for index in range(16)]
    held = store.call(
        bridge.home,
        actors[0],
        "file_reservation_paths",
        {"paths": globs, "reason": "wide refactor " * 10},
    )
    assert len(held["granted"]) == 16
    denied = store.call(
        bridge.home, actors[1], "file_reservation_paths", {"paths": globs}
    )
    assert denied["granted"] == [] and denied["has_more"]
    assert (
        len(json.dumps(denied, ensure_ascii=False).encode())
        <= store.MAX_RESULT_BYTES
    )


def test_body_paging_preserves_unicode_and_read_is_not_ack(bridge, actors):
    body = "😀" * 1024
    sent = store.call(
        bridge.home,
        actors[0],
        "send_message",
        message(body_md=body, ack_required=True),
    )
    read = store.call(
        bridge.home, actors[1], "fetch_inbox", {"include_bodies": True}
    )
    assert read["messages"][0]["body_md"] == body
    assert (
        len(json.dumps(read, ensure_ascii=False).encode())
        <= store.MAX_RESULT_BYTES
    )
    store.call(
        bridge.home, actors[1], "mark_message_read", {"message_id": sent["id"]}
    )
    assert mailbox(bridge.home, "/project", "BlueLake")["pending_ack"] == 1
    with store.connect(bridge.home, write=True) as db:
        db.execute("UPDATE messages SET body_md=?", ("abcdefgh" * 1000,))
    pieces = []
    offset = 0
    while True:
        page = store.call(
            bridge.home,
            actors[1],
            "fetch_inbox",
            {"limit": 1, "include_bodies": True, "body_offset": offset},
        )["messages"][0]
        pieces.append(page["body_md"])
        if "next_body_offset" not in page:
            break
        offset = page["next_body_offset"]
    assert "".join(pieces) == "abcdefgh" * 1000


@pytest.mark.parametrize("corrupt", [False, True])
def test_legacy_import_preserves_source_and_retries_atomically(
    bridge, actors, corrupt
):
    store.call(
        bridge.home, actors[0], "send_message", message(ack_required=True)
    )
    legacy = bridge.home / "mail.sqlite3"
    with contextlib.closing(sqlite3.connect(legacy)) as destination:
        with store.connect(bridge.home) as source:
            source.backup(destination)
    original = hashlib.sha256(legacy.read_bytes()).hexdigest()
    (bridge.home / store.DATABASE).unlink()
    if corrupt:
        with contextlib.closing(sqlite3.connect(legacy)) as db:
            db.execute(
                "INSERT INTO message_recipients(message_id,agent_id) "
                "VALUES (999,999)"
            )
            db.commit()
        with pytest.raises(sqlite3.IntegrityError):
            store.initialize(bridge.home)
        with store.connect(bridge.home) as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == 0
            assert (
                db.execute("SELECT count(*) FROM projects").fetchone()[0] == 0
            )
        with contextlib.closing(sqlite3.connect(legacy)) as db:
            db.execute("DELETE FROM message_recipients WHERE message_id=999")
            db.commit()
        original = hashlib.sha256(legacy.read_bytes()).hexdigest()
    store.initialize(bridge.home)
    store.initialize(bridge.home)
    assert hashlib.sha256(legacy.read_bytes()).hexdigest() == original
    assert mailbox(bridge.home, "/project", "BlueLake")["pending_ack"] == 1
    with store.connect(bridge.home) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("SELECT count(*) FROM messages").fetchone()[0] == 1
        assert (
            db.execute("SELECT token_digest FROM agents LIMIT 1").fetchone()[0]
            is None
        )


def test_locked_store_fails_within_a_bounded_deadline(bridge, actors):
    with store.connect(bridge.home, write=True):
        started = time.monotonic()
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            store.call(bridge.home, actors[0], "send_message", message())
        assert time.monotonic() - started < 2


def test_writer_waits_for_a_short_lived_competing_transaction(
    bridge, actors, monkeypatch
):
    attempted = threading.Event()
    connect = sqlite3.connect

    def traced_connect(*args, **kwargs):
        db = connect(*args, **kwargs)
        db.set_trace_callback(
            lambda statement: (
                attempted.set() if statement == "BEGIN IMMEDIATE" else None
            )
        )
        return db

    with ThreadPoolExecutor(max_workers=1) as pool:
        with store.connect(bridge.home, write=True):
            monkeypatch.setattr(sqlite3, "connect", traced_connect)
            pending = pool.submit(
                store.call, bridge.home, actors[0], "send_message", message()
            )
            assert attempted.wait(timeout=2)
            time.sleep(0.45)
        assert pending.result(timeout=2)["id"] > 0
