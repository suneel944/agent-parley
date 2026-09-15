"""Checks the bounded mail wait against the real store and MCP transport."""

import contextlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from agent_parley import roster, server, store, waits
from agent_parley.state import BridgeError


@pytest.fixture
def lanes(bridge):
    """Registers a sender and a recipient with usable credentials."""
    store.initialize(bridge.home)
    identities = {
        name: store.register(bridge.home, "/project", name)
        for name in ("GreenCastle", "BlueLake")
    }
    return {
        name: {
            "token": identity["registration_token"],
            "actor": store.authenticate(
                bridge.home, identity["registration_token"]
            ),
        }
        for name, identity in identities.items()
    }


def message(**overrides):
    """Builds a valid small message addressed to the waiting lane."""
    return {
        "to": ["BlueLake"],
        "subject": "Contract",
        "body_md": "Changed",
        "idempotency_key": "wait-1",
        **overrides,
    }


@contextlib.contextmanager
def service(bridge):
    """Runs the real service in this process on an ephemeral port."""
    with server.Server(bridge.home, {"token": "health", "port": 0}) as running:
        thread = threading.Thread(target=running.serve_forever, daemon=True)
        thread.start()
        try:
            yield running
        finally:
            running.shutdown()
            thread.join(timeout=5)


def decoded(response):
    """Returns the tool result a served response carries."""
    result = response.json()["result"]
    assert "isError" not in result
    return json.loads(result["content"][0]["text"])


def served(running, token, tool, arguments, timeout=30.0):
    """Calls one tool over the authenticated local transport."""
    response = httpx.post(
        f"http://127.0.0.1:{running.server_port}/mcp/",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        },
        timeout=timeout,
        trust_env=False,
    )
    return response


def test_a_reply_arriving_during_a_wait_is_reported_at_once(bridge, lanes):
    sent = {}

    def reply():
        time.sleep(0.2)
        sent["id"] = store.call(
            bridge.home,
            lanes["GreenCastle"]["actor"],
            "send_message",
            message(),
        )["id"]

    sender = threading.Thread(target=reply)
    started = time.monotonic()
    sender.start()
    try:
        page = waits.wait(
            bridge.home,
            lanes["BlueLake"]["actor"],
            {"timeout_seconds": 10},
        )
    finally:
        sender.join(timeout=5)
    assert time.monotonic() - started < 1
    assert [row["id"] for row in page["messages"]] == [sent["id"]]
    assert page["expired"] is False


def test_a_wait_on_one_thread_ignores_mail_in_another(bridge, lanes):
    other = store.call(
        bridge.home,
        lanes["GreenCastle"]["actor"],
        "send_message",
        message(idempotency_key="wait-other"),
    )
    awaited = store.call(
        bridge.home,
        lanes["GreenCastle"]["actor"],
        "send_message",
        message(idempotency_key="wait-thread", thread_id="release"),
    )
    page = waits.wait(
        bridge.home,
        lanes["BlueLake"]["actor"],
        {"timeout_seconds": 0, "thread_id": "release"},
    )
    assert [row["id"] for row in page["messages"]] == [awaited["id"]]
    assert other["id"] not in [row["id"] for row in page["messages"]]


def test_an_expired_wait_reports_an_empty_page_and_records_nothing(
    bridge, lanes
):
    def events():
        with store.connect(bridge.home) as db:
            return db.execute("SELECT count(*) FROM events").fetchone()[0]

    before = events()
    started = time.monotonic()
    page = waits.wait(
        bridge.home, lanes["BlueLake"]["actor"], {"timeout_seconds": 1}
    )
    assert 1 <= time.monotonic() - started < 5
    assert page == {
        "messages": [],
        "next_after_id": 0,
        "has_more": False,
        "timeout_seconds": 1.0,
        "expired": True,
    }
    assert events() == before


def test_a_requested_wait_is_clamped_and_validated(bridge, lanes):
    assert waits.bounded(10_000) == waits.MAX_SECONDS
    assert waits.bounded(None) == waits.DEFAULT_SECONDS
    assert waits.bounded(10, ceiling=1) == 1
    assert waits.bounded(10, ceiling=0) == 0
    for invalid in ("30", True, -1, [30]):
        with pytest.raises(BridgeError):
            waits.bounded(invalid)


def test_the_service_ceiling_clamps_an_over_long_caller_timeout(
    bridge, lanes, monkeypatch
):
    monkeypatch.setattr(waits, "MAX_SECONDS", 0.5)
    with service(bridge) as running:
        started = time.monotonic()
        response = served(
            running,
            lanes["BlueLake"]["token"],
            waits.TOOL,
            {"timeout_seconds": 600},
        )
    elapsed = time.monotonic() - started
    assert response.status_code == 200
    assert decoded(response) == {
        "messages": [],
        "next_after_id": 0,
        "has_more": False,
        "timeout_seconds": 0.5,
        "expired": True,
    }
    assert elapsed < 30


def test_wait_filters_report_exactly_what_fetch_inbox_reports(bridge, lanes):
    for index in range(4):
        store.call(
            bridge.home,
            lanes["GreenCastle"]["actor"],
            "send_message",
            message(
                idempotency_key=f"filter-{index}",
                ack_required=index % 2 == 1,
                thread_id="review" if index == 3 else "",
            ),
        )
    inbox = store.call(
        bridge.home, lanes["BlueLake"]["actor"], "fetch_inbox", {}
    )
    store.call(
        bridge.home,
        lanes["BlueLake"]["actor"],
        "mark_message_read",
        {"message_id": inbox["messages"][0]["id"]},
    )
    for filters in (
        {},
        {"unread": True},
        {"unacknowledged": True},
        {"limit": 1},
        {"after_id": inbox["messages"][0]["id"]},
        {"thread_id": "review"},
        {"unread": True, "include_bodies": True},
    ):
        expected = store.call(
            bridge.home, lanes["BlueLake"]["actor"], "fetch_inbox", filters
        )
        page = waits.wait(
            bridge.home,
            lanes["BlueLake"]["actor"],
            {**filters, "timeout_seconds": 0},
        )
        assert page == {
            **expected,
            "timeout_seconds": 0.0,
            "expired": not expected["messages"],
        }


def test_a_revoked_registration_ends_a_wait_without_an_error(bridge, lanes):
    store.call(
        bridge.home, lanes["GreenCastle"]["actor"], "send_message", message()
    )
    assert store.revoke(bridge.home, "/project", "BlueLake") == 1
    page = waits.wait(
        bridge.home, lanes["BlueLake"]["actor"], {"timeout_seconds": 5}
    )
    assert page["messages"] == []
    assert page["expired"] is True


def test_a_stopping_service_ends_a_wait_without_an_error(bridge, lanes):
    stopping = threading.Event()
    stopping.set()
    started = time.monotonic()
    page = waits.wait(
        bridge.home,
        lanes["BlueLake"]["actor"],
        {"timeout_seconds": 30},
        stopping,
    )
    assert time.monotonic() - started < 5
    assert page["messages"] == []
    assert page["expired"] is True


def test_a_paused_lane_is_never_handed_its_mail_by_a_wait(bridge, repo, paired):
    store.initialize(bridge.home)
    sender = store.authenticate(
        bridge.home,
        store.register(bridge.home, paired["root"], "codex")[
            "registration_token"
        ],
    )
    recipient = store.authenticate(
        bridge.home,
        store.register(bridge.home, paired["root"], "claude")[
            "registration_token"
        ],
    )
    store.call(
        bridge.home,
        sender,
        "send_message",
        message(to=["claude"], idempotency_key="paused-1"),
    )
    bridge.pause(repo, "claude")
    assert roster.paused(bridge.home, paired["root"], "claude")
    with pytest.raises(BridgeError, match="paused"):
        waits.wait(bridge.home, recipient, {"timeout_seconds": 0})


def test_more_waiters_than_workers_are_all_answered(bridge, lanes):
    waiting = server.WORKERS + 4
    with service(bridge) as running:
        with ThreadPoolExecutor(max_workers=waiting + 1) as pool:
            futures = []
            for _ in range(waiting):
                futures.append(
                    pool.submit(
                        served,
                        running,
                        lanes["BlueLake"]["token"],
                        waits.TOOL,
                        {"timeout_seconds": 20},
                    )
                )
                time.sleep(0.05)
            roster_call = pool.submit(
                served,
                running,
                lanes["GreenCastle"]["token"],
                "list_participants",
                {},
            )
            assert roster_call.result().status_code == 200
            delivery = served(
                running,
                lanes["GreenCastle"]["token"],
                "send_message",
                message(),
            )
            assert delivery.status_code == 200
            started = time.monotonic()
            pages = [future.result() for future in futures]
    assert time.monotonic() - started < 10
    assert [page.status_code for page in pages] == [200] * waiting
    for page in pages:
        assert decoded(page)["expired"] is False
