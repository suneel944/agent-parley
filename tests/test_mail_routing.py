"""Checks mail routing by relevance and the expiry of acknowledgement debt."""

import asyncio
import time
from pathlib import Path

import pytest

from agent_parley import checkpoints, roster, store, supervision
from agent_parley import lanes as states
from agent_parley.state import write_json


@pytest.fixture
def lanes(bridge, paired):
    """Registers claude and codex and authenticates both."""
    store.initialize(bridge.home)
    registered = {
        name: store.register(bridge.home, paired["root"], name)
        for name in ("claude", "codex")
    }
    lane = Path(paired["lanes"]["claude"])
    return {
        name: store.authenticate(
            bridge.home, registered[name]["registration_token"]
        )
        for name in registered
    } | {"lane": lane, "directory": lane.parent, "root": paired["root"]}


def send(bridge, lanes, key, subject, **extra):
    """Mails codex from claude."""
    return store.call(
        bridge.home,
        lanes["claude"],
        "send_message",
        {
            "to": ["codex"],
            "subject": subject,
            "body_md": "See the log.",
            "idempotency_key": key,
            **extra,
        },
    )


def mail(bridge, lanes, name="codex"):
    """Reads one lane's mailbox as a checkpoint would."""
    return checkpoints.mailbox(bridge.home, lanes["root"], name)


def test_a_merge_note_reaches_the_feed_and_no_mailbox(bridge, paired, lanes):
    sent = send(bridge, lanes, "merge", "Merged #12 into main")

    assert sent["feed"] is True
    assert sent["withheld"] == ["codex"]
    assert mail(bridge, lanes)["unread"] == 0
    items = store.feed(bridge.home, lanes["root"])["items"]
    assert [item["subject"] for item in items] == ["Merged #12 into main"]


def test_a_broadcast_reaches_only_the_lanes_it_concerns(
    bridge, repo, paired, lanes
):
    for name in ("kimi-1", "kimi-2"):
        data = bridge.add_participant(repo, name, "kimi")
        asyncio.run(bridge.identity(name, data))
    sent = send(
        bridge,
        lanes,
        "broadcast",
        "Schema moved",
        to=["codex", "kimi-1", "kimi-2"],
        body_md="codex: the parser schema moved.",
    )
    explicit = send(bridge, lanes, "pair", "Pair on it", to=["kimi-1"])

    assert sent["withheld"] == ["kimi-1", "kimi-2"]
    assert mail(bridge, lanes)["unread"] == 1
    assert [
        item["id"] for item in mail(bridge, lanes, "kimi-1")["messages"]
    ] == [explicit["id"]]
    news = store.feed(bridge.home, lanes["root"])
    assert [item["id"] for item in news["items"]] == [sent["id"]]


def test_an_explicit_address_to_every_peer_of_two_is_delivered(
    bridge, paired, lanes
):
    sent = send(bridge, lanes, "both", "Schema moved")

    assert "withheld" not in sent
    assert mail(bridge, lanes)["unread"] == 1


def test_a_second_main_is_note_replaces_the_first(bridge, paired, lanes):
    send(bridge, lanes, "one", "main is at abc123")
    send(bridge, lanes, "two", "main is at def456")

    news = store.feed(bridge.home, lanes["root"])

    assert [item["subject"] for item in news["items"]] == ["main is at def456"]
    assert news["superseded"] == 1


def test_a_newer_note_on_a_topic_supersedes_the_unread_one(
    bridge, paired, lanes
):
    send(bridge, lanes, "one", "Parser at 40%", topic="parser")
    send(bridge, lanes, "two", "Parser at 80%", topic="parser")

    box = mail(bridge, lanes)

    assert (box["unread"], box["superseded"]) == (1, 1)
    assert box["unread_topics"] == {"parser": 1}
    assert [item["subject"] for item in box["messages"]] == ["Parser at 80%"]


def test_a_superseded_request_owes_no_acknowledgement(bridge, paired, lanes):
    send(bridge, lanes, "one", "Review A", topic="review", ack_required=True)
    second = send(
        bridge, lanes, "two", "Review B", topic="review", ack_required=True
    )

    owed = store.pending_acknowledgements(bridge.home, lanes["root"])

    assert [item["message_id"] for item in owed] == [second["id"]]
    assert mail(bridge, lanes)["pending_ack"] == 1


def test_debt_on_a_closed_claim_is_zero(bridge, paired, lanes):
    bridge.issue(lanes["lane"], "claim", "1")
    send(bridge, lanes, "bound", "Contract", ack_required=True)
    assert mail(bridge, lanes)["pending_ack"] == 1

    bridge.issue(lanes["lane"], "release", "1")

    assert mail(bridge, lanes)["pending_ack"] == 0
    assert store.pending_acknowledgements(bridge.home, lanes["root"]) == []


def test_a_stale_lanes_debt_does_not_lower_its_fit(bridge, paired, lanes):
    send(bridge, lanes, "owed", "Contract", ack_required=True)
    manifest = roster.read(lanes["directory"])
    activity = lanes["directory"] / "codex-activity.json"

    write_json(activity, {"updated": time.time() - 3600})
    with store.connect(bridge.home, write=True) as db:
        states.transition(db, manifest["root"], "codex", states.WORKING)
    fresh = supervision.fit(
        bridge.home, lanes["directory"], manifest, "codex", 0, 300
    )
    write_json(activity, {"updated": time.time()})
    with store.connect(bridge.home, write=True) as db:
        states.transition(
            db, manifest["root"], "codex", states.IDLE, now=time.time() - 3600
        )
    stale = supervision.fit(
        bridge.home, lanes["directory"], manifest, "codex", 0, 300
    )

    assert fresh["checks"]["mail"] is False
    assert stale["checks"]["mail"] is True
