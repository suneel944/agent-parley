"""Tests transactional coordination, transport isolation and context budgets."""

import asyncio
import contextlib
import hashlib
import json
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest

from agent_parley import server, store
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


def test_inbox_filters_report_receipts_without_mutating_them(bridge, actors):
    ids = []
    for index, ack in enumerate([False, True, True, True]):
        sent = store.call(
            bridge.home,
            actors[0],
            "send_message",
            message(idempotency_key=f"filter-{index}", ack_required=ack),
        )
        ids.append(sent["id"])
    store.call(
        bridge.home, actors[1], "mark_message_read", {"message_id": ids[1]}
    )
    store.call(
        bridge.home, actors[1], "acknowledge_message", {"message_id": ids[2]}
    )
    inbox = store.call(bridge.home, actors[1], "fetch_inbox", {})
    assert inbox["messages"][1]["read_ts"] is not None
    assert inbox["messages"][1]["ack_ts"] is None
    assert inbox["messages"][2]["ack_ts"] is not None
    for filters in (
        {"unread": True},
        {"unacknowledged": True},
        {"unread": True, "unacknowledged": True},
    ):
        expected = [
            row["id"]
            for row in inbox["messages"]
            if (not filters.get("unread") or row["read_ts"] is None)
            and (
                not filters.get("unacknowledged")
                or row["ack_required"]
                and row["ack_ts"] is None
            )
        ]
        result = store.call(bridge.home, actors[1], "fetch_inbox", filters)
        assert [row["id"] for row in result["messages"]] == expected
        first = store.call(
            bridge.home, actors[1], "fetch_inbox", {**filters, "limit": 1}
        )
        rest = store.call(
            bridge.home,
            actors[1],
            "fetch_inbox",
            {**filters, "after_id": first["next_after_id"]},
        )
        assert [
            row["id"] for row in first["messages"] + rest["messages"]
        ] == expected
    assert store.call(bridge.home, actors[1], "fetch_inbox", {}) == inbox
    assert (
        store.call(bridge.home, actors[0], "fetch_inbox", {"unread": True})[
            "messages"
        ]
        == []
    )


@pytest.mark.parametrize(
    "args",
    [
        {"body_offset": -1},
        {"body_offset": True},
        {"unread": 1},
        {"unacknowledged": "true"},
    ],
)
def test_inbox_validates_filters_and_offsets_even_when_empty(
    bridge, actors, args
):
    with pytest.raises(BridgeError):
        store.call(bridge.home, actors[1], "fetch_inbox", args)


@pytest.mark.parametrize(
    "tool", ["fetch_inbox", "read_thread", "search_messages"]
)
def test_mail_budget_includes_paging_metadata(
    bridge, actors, monkeypatch, tool
):
    sent = store.call(
        bridge.home,
        actors[0],
        "send_message",
        message(body_md="budget receipt"),
    )
    args = {
        "read_thread": {"thread_id": sent["thread_id"]},
        "search_messages": {"query": "budget"},
        "fetch_inbox": {},
    }[tool]
    full = store.call(bridge.home, actors[1], tool, args)
    assert len(full["messages"]) == 1
    budget = len(json.dumps(full, ensure_ascii=False).encode()) - 1
    monkeypatch.setattr(store, "MAX_RESULT_BYTES", budget)
    bounded = store.call(bridge.home, actors[1], tool, args)
    assert len(json.dumps(bounded, ensure_ascii=False).encode()) <= budget
    assert bounded["messages"] == []
    assert bounded["has_more"] is True


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


def test_reservations_serialize_conflicts_and_renew(bridge, actors):
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
    assert "stale" not in denied["conflicts"][0]
    other = next(actor for actor in actors if actor is not loser)
    store.call(bridge.home, other, "release_file_reservations", {})
    assert store.call(
        bridge.home, loser, "file_reservation_paths", {"paths": ["src/api.py"]}
    )["granted"]
    assert store.call(
        bridge.home, loser, "file_reservation_paths", {"paths": ["src/api.py"]}
    )["granted"]
    silent = store.call(
        bridge.home, other, "file_reservation_paths", {"paths": ["src/api.py"]}
    )
    assert silent["granted"] == []
    assert "reason" not in silent["conflicts"][0]
    with store.connect(bridge.home) as db:
        assert (
            db.execute(
                "SELECT count(*) FROM file_reservations "
                "WHERE released_ts IS NULL"
            ).fetchone()[0]
            == 1
        )


def test_a_reservation_without_a_time_to_live_never_reports_stale(
    bridge, actors
):
    store.call(
        bridge.home, actors[0], "file_reservation_paths", {"paths": ["src/a"]}
    )
    with store.connect(bridge.home) as db:
        assert (
            db.execute(
                "SELECT count(*) FROM file_reservations "
                "WHERE expires_ts IS NULL AND released_ts IS NULL"
            ).fetchone()[0]
            == 1
        )
    usage = store.usage(bridge.home, "/project")
    assert usage["GreenCastle"]["leases"] == 1
    assert usage["GreenCastle"]["stale_leases"] == 0
    denied = store.call(
        bridge.home, actors[1], "file_reservation_paths", {"paths": ["src/a"]}
    )
    assert denied["granted"] == []
    assert "stale" not in denied["conflicts"][0]
    mail = mailbox(bridge.home, "/project", "GreenCastle")
    assert mail["reservations"] == 1
    assert mail["stale_reservations"] == 0


def test_a_lease_past_its_time_to_live_reports_stale_and_still_blocks(
    bridge, actors
):
    store.call(
        bridge.home,
        actors[0],
        "file_reservation_paths",
        {"paths": ["src/a"], "ttl_seconds": 3600, "reason": "refactor store"},
    )
    live = store.call(
        bridge.home, actors[1], "file_reservation_paths", {"paths": ["src/a"]}
    )
    assert live["granted"] == []
    assert "stale" not in live["conflicts"][0]
    before = store.usage(bridge.home, "/project")
    assert before["GreenCastle"]["stale_leases"] == 0
    with store.connect(bridge.home, write=True) as db:
        db.execute(
            "UPDATE file_reservations SET expires_ts='2000-01-01' "
            "WHERE agent_id=?",
            (actors[0]["id"],),
        )
    expired = store.call(
        bridge.home, actors[1], "file_reservation_paths", {"paths": ["src/a"]}
    )
    assert expired["granted"] == []
    assert expired["conflicts"][0]["owner"] == "GreenCastle"
    assert expired["conflicts"][0]["reason"] == "refactor store"
    assert expired["conflicts"][0]["stale"] is True
    usage = store.usage(bridge.home, "/project")
    assert usage["GreenCastle"]["leases"] == 1
    assert usage["GreenCastle"]["stale_leases"] == 1
    mail = mailbox(bridge.home, "/project", "GreenCastle")
    assert mail["reservations"] == 1
    assert mail["stale_reservations"] == 1


def test_the_conflict_listing_separates_live_holders_from_stale_ones(
    bridge, actors
):
    third = store.authenticate(
        bridge.home,
        store.register(bridge.home, "/project", "RedRiver")[
            "registration_token"
        ],
    )
    store.call(
        bridge.home,
        actors[0],
        "file_reservation_paths",
        {"paths": ["src/live.py"], "ttl_seconds": 3600},
    )
    store.call(
        bridge.home,
        third,
        "file_reservation_paths",
        {"paths": ["src/dead.py"], "ttl_seconds": 30},
    )
    with store.connect(bridge.home, write=True) as db:
        db.execute(
            "UPDATE file_reservations SET expires_ts='2000-01-01' "
            "WHERE agent_id=?",
            (third["id"],),
        )
    denied = store.call(
        bridge.home,
        actors[1],
        "file_reservation_paths",
        {"paths": ["src/live.py", "src/dead.py"]},
    )
    assert denied["granted"] == []
    assert {
        conflict["owner"]: conflict.get("stale", False)
        for conflict in denied["conflicts"]
    } == {"GreenCastle": False, "RedRiver": True}


@pytest.mark.parametrize("interrupted", [False, True])
def test_schema_upgrade_makes_a_lease_time_to_live_optional(
    bridge, actors, interrupted
):
    store.call(
        bridge.home,
        actors[0],
        "file_reservation_paths",
        {"paths": ["src/a"], "ttl_seconds": 3600},
    )
    with store.connect(bridge.home, write=True) as db:
        deadline = db.execute(
            "SELECT expires_ts FROM file_reservations"
        ).fetchone()[0]
        db.execute("ALTER TABLE file_reservations RENAME TO retired_leases")
        db.execute(
            "CREATE TABLE file_reservations (id INTEGER PRIMARY KEY,"
            " project_id INTEGER NOT NULL REFERENCES projects(id),"
            " agent_id INTEGER NOT NULL REFERENCES agents(id),"
            " path_pattern TEXT NOT NULL, exclusive INTEGER NOT NULL,"
            " reason TEXT DEFAULT '',"
            " created_ts TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,"
            " expires_ts TEXT NOT NULL, released_ts TEXT)"
        )
        db.execute(
            "INSERT INTO file_reservations SELECT id,project_id,agent_id,"
            "path_pattern,exclusive,reason,created_ts,expires_ts,released_ts "
            "FROM retired_leases"
        )
        db.execute("DROP TABLE retired_leases")
        db.execute("PRAGMA user_version=2")
    if interrupted:
        crash = subprocess.run(
            [
                sys.executable,
                "-c",
                """
import os
import sqlite3
import sys
from pathlib import Path
from agent_parley import store

original = sqlite3.connect
class Interrupted(sqlite3.Connection):
    def execute(self, sql, *args):
        if sql.startswith('ALTER TABLE rebuilt_reservations'):
            os._exit(77)
        return super().execute(sql, *args)

store.sqlite3.connect = lambda *a, **kw: original(
    *a, **kw, factory=Interrupted
)
store.initialize(Path(sys.argv[1]))
""",
                str(bridge.home),
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert crash.returncode == 77, crash.stderr
        with store.connect(bridge.home) as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == 2
            assert db.execute(
                "SELECT path_pattern,expires_ts FROM file_reservations"
            ).fetchone()[:] == ("src/a", deadline)
            assert not db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='rebuilt_reservations'"
            ).fetchone()
    store.initialize(bridge.home)
    store.call(
        bridge.home, actors[1], "file_reservation_paths", {"paths": ["docs/b"]}
    )
    with store.connect(bridge.home) as db:
        assert (
            db.execute("PRAGMA user_version").fetchone()[0]
            == store.SCHEMA_VERSION
        )
        assert [
            row[0]
            for row in db.execute(
                "SELECT expires_ts FROM file_reservations ORDER BY id"
            )
        ] == [deadline, None]
    usage = store.usage(bridge.home, "/project")
    assert usage["GreenCastle"]["leases"] == 1
    assert usage["BlueLake"]["leases"] == 1


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
    assert len(json.dumps(TOOLS).encode()) < 6100


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
        assert (
            db.execute("PRAGMA user_version").fetchone()[0]
            == store.SCHEMA_VERSION
        )
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


def test_replies_join_a_thread_and_other_sends_open_their_own(bridge, actors):
    opened = store.call(bridge.home, actors[0], "send_message", message())
    assert opened["thread_id"]
    answer = message(
        to=["GreenCastle"],
        subject="Re: Contract",
        body_md="Agreed",
        idempotency_key="contract-answer",
        reply_to=opened["id"],
    )
    inbox = store.call(bridge.home, actors[1], "fetch_inbox", {})
    assert inbox["messages"][0]["thread_id"] == opened["thread_id"]
    answered = store.call(bridge.home, actors[1], "send_message", answer)
    assert answered["thread_id"] == opened["thread_id"]
    retried = store.call(bridge.home, actors[1], "send_message", answer)
    assert retried["duplicate"]
    assert retried["thread_id"] == opened["thread_id"]
    separate = store.call(
        bridge.home,
        actors[0],
        "send_message",
        message(idempotency_key="contract-2"),
    )
    assert separate["thread_id"] != opened["thread_id"]
    with pytest.raises(BridgeError, match="reply_to"):
        store.call(
            bridge.home,
            actors[0],
            "send_message",
            message(idempotency_key="stray", reply_to=opened["id"] + 999),
        )
    with pytest.raises(BridgeError, match="not both"):
        store.call(
            bridge.home,
            actors[0],
            "send_message",
            message(
                idempotency_key="ambiguous",
                reply_to=opened["id"],
                thread_id="chosen",
            ),
        )


def test_thread_reads_in_send_order_for_the_calling_participant(bridge, actors):
    first = store.call(bridge.home, actors[0], "send_message", message())
    thread = first["thread_id"]
    for index in range(2):
        store.call(
            bridge.home,
            actors[1],
            "send_message",
            message(
                to=["GreenCastle"],
                body_md=f"Answer {index}",
                idempotency_key=f"answer-{index}",
                reply_to=first["id"],
            ),
        )
    page = store.call(
        bridge.home, actors[0], "read_thread", {"thread_id": thread}
    )
    assert [row["sender"] for row in page["messages"]] == [
        "GreenCastle",
        "BlueLake",
        "BlueLake",
    ]
    identifiers = [row["id"] for row in page["messages"]]
    assert identifiers == sorted(identifiers) and not page["has_more"]
    assert (
        store.read_thread(bridge.home, "/project", "GreenCastle", thread)[
            "messages"
        ]
        == page["messages"]
    )
    walked = store.call(
        bridge.home,
        actors[0],
        "read_thread",
        {"thread_id": thread, "limit": 1},
    )
    assert walked["has_more"] and walked["next_after_id"] == first["id"]
    rest = store.call(
        bridge.home,
        actors[0],
        "read_thread",
        {"thread_id": thread, "after_id": walked["next_after_id"]},
    )
    assert [row["id"] for row in rest["messages"]] == identifiers[1:]
    absent = store.call(
        bridge.home, actors[0], "read_thread", {"thread_id": "absent"}
    )
    assert absent["messages"] == [] and not absent["has_more"]


def test_search_reports_own_mail_hits_and_ignores_other_projects(
    bridge, actors
):
    sent = store.call(
        bridge.home,
        actors[0],
        "send_message",
        message(body_md="Reservation conflict on src/engine.py"),
    )
    with store.connect(bridge.home) as db:
        indexed = bool(
            db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='message_search'"
            ).fetchone()
        )
    hit = store.call(
        bridge.home, actors[1], "search_messages", {"query": "reservation"}
    )
    assert hit["index"] == ("fts5" if indexed else "substring")
    assert [row["id"] for row in hit["messages"]] == [sent["id"]]
    assert hit["messages"][0]["thread_id"] == sent["thread_id"]
    miss = store.call(
        bridge.home, actors[1], "search_messages", {"query": "unrelated"}
    )
    assert miss["messages"] == [] and not miss["has_more"]
    outsider = store.authenticate(
        bridge.home,
        store.register(bridge.home, "/other", "GreenCastle")[
            "registration_token"
        ],
    )
    assert (
        store.call(
            bridge.home, outsider, "search_messages", {"query": "reservation"}
        )["messages"]
        == []
    )
    assert [
        row["id"]
        for row in store.search_messages(
            bridge.home, "/project", "BlueLake", "conflict"
        )["messages"]
    ] == [sent["id"]]


def test_upgrade_backfills_threads_and_indexes_stored_messages(bridge, actors):
    for index in range(2):
        store.call(
            bridge.home,
            actors[0],
            "send_message",
            message(
                body_md=f"Reservation conflict {index}",
                idempotency_key=f"stored-{index}",
            ),
        )
    with store.connect(bridge.home, write=True) as db:
        db.execute("UPDATE messages SET thread_id=''")
        db.execute("DROP TABLE IF EXISTS message_search")
        db.execute("DROP TRIGGER IF EXISTS message_indexed")
        db.execute("DROP TRIGGER IF EXISTS message_unindexed")
        db.execute("DROP INDEX IF EXISTS threads")
        db.execute("PRAGMA user_version=2")
    store.initialize(bridge.home)
    with store.connect(bridge.home) as db:
        assert (
            db.execute("PRAGMA user_version").fetchone()[0]
            == store.SCHEMA_VERSION
        )
        threads = [
            row[0] for row in db.execute("SELECT thread_id FROM messages")
        ]
    assert len(threads) == 2 and len(set(threads)) == 2 and all(threads)
    found = store.call(
        bridge.home, actors[1], "search_messages", {"query": "reservation"}
    )
    assert len(found["messages"]) == 2
    page = store.call(
        bridge.home, actors[1], "read_thread", {"thread_id": threads[0]}
    )
    assert len(page["messages"]) == 1


def test_search_degrades_to_substring_where_sqlite_lacks_fts5(
    tmp_path, monkeypatch
):
    home = tmp_path / "without-fts5"
    home.mkdir(parents=True)
    monkeypatch.setattr(store, "_fts_available", lambda db: False)
    store.initialize(home)
    identities = [
        store.authenticate(
            home,
            store.register(home, "/project", name)["registration_token"],
        )
        for name in ("GreenCastle", "BlueLake")
    ]
    sent = store.call(
        home,
        identities[0],
        "send_message",
        message(body_md="Lease overlap in src/engine.py"),
    )
    with store.connect(home) as db:
        assert not db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='message_search'"
        ).fetchone()
    found = store.call(
        home, identities[1], "search_messages", {"query": "overlap"}
    )
    assert found["index"] == "substring"
    assert [row["id"] for row in found["messages"]] == [sent["id"]]
    assert (
        store.call(
            home, identities[1], "search_messages", {"query": "unrelated"}
        )["messages"]
        == []
    )
    assert (
        store.call(
            home,
            identities[1],
            "search_messages",
            {"query": "100% _absent_"},
        )["messages"]
        == []
    )
    assert store.call(
        home, identities[1], "read_thread", {"thread_id": sent["thread_id"]}
    )["messages"]


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
        assert time.monotonic() - started < store.BUSY_TIMEOUT + 1


def test_mcp_reads_return_while_a_writer_holds_the_store(bridge, actors):
    sent = store.call(bridge.home, actors[0], "send_message", message())
    reads = [
        ("fetch_inbox", {}),
        ("list_participants", {}),
        ("read_thread", {"thread_id": sent["thread_id"]}),
        ("search_messages", {"query": "Contract"}),
    ]
    with store.connect(bridge.home, write=True) as db:
        for tool, args in reads:
            started = time.monotonic()
            result = store.call(bridge.home, actors[1], tool, args)
            assert time.monotonic() - started < 0.5
            assert result
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 1


def test_roster_and_delivery_exclude_uncredentialed_recipients(bridge, actors):
    store.speak(bridge.home, "/project", "BlueLake", "Steer", "Work", "s1")
    store.revoke(bridge.home, "/project", "BlueLake")
    for index in range(31):
        store.register(bridge.home, "/project", f"lane-{index:02}")
    roster = store.call(bridge.home, actors[0], "list_participants", {})
    names = {row["name"] for row in roster["participants"]}
    assert len(names) == 32
    assert "lane-30" in names
    assert "operator" not in names
    assert "BlueLake" not in names
    for name in ("operator", "BlueLake"):
        with pytest.raises(BridgeError, match="no active credential"):
            store.call(
                bridge.home, actors[0], "send_message", message(to=[name])
            )
    with store.connect(bridge.home) as db:
        assert db.execute("SELECT count(*) FROM messages").fetchone()[0] == 1
    store.register(bridge.home, "/project", "BlueLake")
    assert store.call(bridge.home, actors[0], "send_message", message())["id"]


def test_startup_disables_old_fts_triggers_and_restores_the_index(
    bridge, actors, monkeypatch
):
    store.call(bridge.home, actors[0], "send_message", message())
    with monkeypatch.context() as patch:
        patch.setattr(store, "_fts_available", lambda db: False)
        store.initialize(bridge.home)
        with store.connect(bridge.home) as db:
            assert not db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='trigger'"
            ).fetchone()
        sent = store.call(
            bridge.home,
            actors[0],
            "send_message",
            message(subject="Offline indexing", idempotency_key="offline"),
        )
        result = store.call(
            bridge.home, actors[1], "search_messages", {"query": "Offline"}
        )
        assert result["index"] == "substring"
    store.initialize(bridge.home)
    result = store.call(
        bridge.home, actors[1], "search_messages", {"query": "Offline"}
    )
    assert result["index"] == "fts5"
    assert [item["id"] for item in result["messages"]] == [sent["id"]]


@pytest.mark.parametrize(
    "code,detail,retryable",
    [
        (sqlite3.SQLITE_BUSY, "database is locked", True),
        (sqlite3.SQLITE_LOCKED, "database table is locked", True),
        (sqlite3.SQLITE_BUSY_SNAPSHOT, "snapshot is busy", True),
        (sqlite3.SQLITE_ERROR, "no such table: messages", False),
        (
            sqlite3.SQLITE_READONLY,
            "attempt to write a readonly database",
            False,
        ),
        (sqlite3.SQLITE_IOERR, "disk I/O error", False),
    ],
)
def test_transport_classifies_and_records_operational_errors(
    bridge, actors, monkeypatch, code, detail, retryable
):
    def fail(*args):
        error = sqlite3.OperationalError(detail)
        error.sqlite_errorcode = code
        raise error

    monkeypatch.setattr(store, "_serve", fail)
    identity = store.register(bridge.home, "/project", "reader")
    with server.Server(bridge.home, {"token": "health", "port": 0}) as service:
        thread = threading.Thread(target=service.serve_forever, daemon=True)
        thread.start()
        try:
            response = httpx.post(
                f"http://127.0.0.1:{service.server_port}/mcp/",
                headers={
                    "Authorization": f"Bearer {identity['registration_token']}"
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "fetch_inbox", "arguments": {}},
                },
                trust_env=False,
            )
        finally:
            service.shutdown()
            thread.join(timeout=2)
    result = response.json()["result"]
    assert result["isError"]
    text = result["content"][0]["text"]
    assert ("retry later" in text) is retryable
    if not retryable:
        assert text == f"Store error: {detail}"
    with store.connect(bridge.home) as db:
        assert db.execute("SELECT outcome FROM events").fetchone()[0] == "error"


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
            assert attempted.wait(timeout=store.BUSY_TIMEOUT)
            time.sleep(1.2)
        assert pending.result(timeout=store.BUSY_TIMEOUT)["id"] > 0
