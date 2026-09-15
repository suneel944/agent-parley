"""Tests the shared decision log and the privacy of ordinary mail."""

import json
import threading
from pathlib import Path

import httpx
import pytest

from agent_parley import attachments, server, store
from agent_parley.roster import OPERATOR
from agent_parley.state import BridgeError


@pytest.fixture
def lanes(bridge):
    """Registers three participants of one project in a local store."""
    store.initialize(bridge.home)
    return [
        store.authenticate(
            bridge.home,
            store.register(bridge.home, "/project", name)["registration_token"],
        )
        for name in ("GreenCastle", "BlueLake", "RedField")
    ]


def agreed(**overrides):
    """Builds one decision addressed to a single peer."""
    return {
        "to": ["BlueLake"],
        "subject": "Interface agreed",
        "body_md": "Reservations stay advisory between lanes.",
        "idempotency_key": "decision-1",
        "decision": True,
        **overrides,
    }


def test_a_third_lane_finds_a_decision_it_never_received(bridge, lanes):
    recorded = store.call(bridge.home, lanes[0], "send_message", agreed())
    assert recorded["decision"] is True
    found = store.call(
        bridge.home, lanes[2], "search_decisions", {"query": "advisory"}
    )
    assert [row["id"] for row in found["messages"]] == [recorded["id"]]
    assert found["messages"][0]["sender"] == "GreenCastle"
    assert found["index"] in ("fts5", "substring")
    assert (
        store.call(
            bridge.home, lanes[2], "search_messages", {"query": "advisory"}
        )["messages"]
        == []
    )
    outsider = store.authenticate(
        bridge.home,
        store.register(bridge.home, "/other", "GreenCastle")[
            "registration_token"
        ],
    )
    assert (
        store.call(
            bridge.home, outsider, "search_decisions", {"query": "advisory"}
        )["messages"]
        == []
    )


def test_ordinary_mail_stays_out_of_the_decision_log(bridge, lanes):
    sent = store.call(
        bridge.home, lanes[0], "send_message", agreed(decision=False)
    )
    assert "decision" not in sent
    assert (
        store.call(bridge.home, lanes[2], "search_decisions", {})["messages"]
        == []
    )
    assert [
        row["id"]
        for row in store.call(
            bridge.home, lanes[1], "search_messages", {"query": "advisory"}
        )["messages"]
    ] == [sent["id"]]


def test_decision_log_lists_newest_first_within_a_window(bridge, lanes):
    ids = [
        store.call(
            bridge.home,
            lanes[0],
            "send_message",
            agreed(
                body_md=f"Lane order {index}",
                idempotency_key=f"decision-{index}",
            ),
        )["id"]
        for index in range(3)
    ]
    with store.connect(bridge.home, write=True) as db:
        db.execute(
            "UPDATE messages SET created_ts=datetime('now','-2 days') "
            "WHERE id=?",
            (ids[0],),
        )
    page = store.search_decisions(bridge.home, "/project", "RedField")
    assert [row["id"] for row in page["messages"]] == list(reversed(ids))
    assert page["index"] == "recent"
    recent = store.search_decisions(
        bridge.home, "/project", "RedField", since=3600
    )
    assert [row["id"] for row in recent["messages"]] == ids[:0:-1]
    limited = store.search_decisions(
        bridge.home, "/project", "RedField", limit=1
    )
    assert [row["id"] for row in limited["messages"]] == [ids[2]]
    assert limited["has_more"] is True


@pytest.mark.parametrize(
    "args",
    [
        {"decision": "true"},
        {"decision": 1},
        {"to": [], "decision": False},
    ],
)
def test_decision_marking_and_recipients_are_validated(bridge, lanes, args):
    with pytest.raises(BridgeError):
        store.call(bridge.home, lanes[0], "send_message", agreed(**args))


@pytest.mark.parametrize(
    "args", [{"since": -1}, {"since": "7d"}, {"limit": 0}, {"query": 1}]
)
def test_decision_search_validates_its_arguments(bridge, lanes, args):
    with pytest.raises(BridgeError):
        store.call(bridge.home, lanes[2], "search_decisions", args)


def test_decision_respects_the_message_body_cap(bridge, lanes):
    with pytest.raises(BridgeError, match="no attachments"):
        store.call(
            bridge.home,
            lanes[0],
            "send_message",
            agreed(body_md="x" * (store.MAX_BODY_BYTES + 1)),
        )
    with pytest.raises(BridgeError, match="budget"):
        store.call(
            bridge.home,
            lanes[0],
            "send_message",
            agreed(body_md="x" * (attachments.MAX_ATTACHMENT_BYTES + 1)),
        )


def test_upgrade_keeps_stored_mail_out_of_the_decision_log(bridge, lanes):
    sent = store.call(
        bridge.home, lanes[0], "send_message", agreed(decision=False)
    )
    with store.connect(bridge.home, write=True) as db:
        db.execute("DROP INDEX IF EXISTS decisions")
        db.execute("ALTER TABLE messages DROP COLUMN decision")
        db.execute("PRAGMA user_version=8")
    store.initialize(bridge.home)
    with store.connect(bridge.home) as db:
        assert (
            db.execute("PRAGMA user_version").fetchone()[0]
            == store.SCHEMA_VERSION
        )
        assert (
            db.execute(
                "SELECT decision FROM messages WHERE id=?", (sent["id"],)
            ).fetchone()[0]
            == 0
        )
    assert (
        store.call(bridge.home, lanes[2], "search_decisions", {})["messages"]
        == []
    )
    assert [
        row["id"]
        for row in store.call(
            bridge.home, lanes[1], "search_messages", {"query": "advisory"}
        )["messages"]
    ] == [sent["id"]]


def test_served_transport_reports_decisions_to_an_uninvolved_lane(
    bridge, lanes
):
    recorded = store.call(bridge.home, lanes[0], "send_message", agreed())
    reader = store.register(bridge.home, "/project", "GoldRiver")
    with server.Server(bridge.home, {"token": "health", "port": 0}) as service:
        thread = threading.Thread(target=service.serve_forever, daemon=True)
        thread.start()
        try:
            response = httpx.post(
                f"http://127.0.0.1:{service.server_port}/mcp/",
                headers={
                    "Authorization": f"Bearer {reader['registration_token']}"
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "search_decisions",
                        "arguments": {"query": "advisory"},
                    },
                },
                trust_env=False,
            )
        finally:
            service.shutdown()
            thread.join(timeout=2)
    result = response.json()["result"]
    assert not result.get("isError")
    reported = json.loads(result["content"][0]["text"])
    assert [row["id"] for row in reported["messages"]] == [recorded["id"]]


def test_operator_records_a_decision_every_lane_can_read(bridge, repo, paired):
    store.initialize(bridge.home)
    for name in ("claude", "codex"):
        store.register(
            bridge.home,
            paired["root"],
            paired["participants"][name]["display"],
        )
    text = "Ship the lane merge behind a flag."
    recorded = bridge.decide(repo, text)
    assert bridge.decide(repo, text)["duplicate"] is True
    lane = Path(paired["lanes"]["codex"])
    page = bridge.decisions(lane, "lane merge")
    assert [row["id"] for row in page["messages"]] == [recorded["id"]]
    assert page["messages"][0]["sender"] == OPERATOR
    assert page["messages"][0]["subject"] == "Operator decision"
    assert bridge.mail(lane, "search", query="lane merge")["messages"] == []
    assert bridge.decisions(lane, window=3600.0)["messages"]
