"""Tests that first read and first acknowledgement receipts survive retries."""

import pytest

from agent_parley import store
from agent_parley.state import BridgeError

EARLIER = "2001-02-03 04:05:06"
STATEMENTS = {
    "read_ts": "UPDATE message_recipients SET read_ts=? WHERE message_id=?",
    "ack_ts": "UPDATE message_recipients SET ack_ts=? WHERE message_id=?",
}


@pytest.fixture
def actors(bridge):
    """Registers a sender and a recipient in an initialized local database."""
    store.initialize(bridge.home)
    identities = [
        store.register(bridge.home, "/project", name)
        for name in ("GreenCastle", "BlueLake")
    ]
    return [
        store.authenticate(bridge.home, row["registration_token"])
        for row in identities
    ]


def deliver(bridge, actors, key):
    """Sends one acknowledgeable message and reports its identifier."""
    return store.call(
        bridge.home,
        actors[0],
        "send_message",
        {
            "to": ["BlueLake"],
            "subject": "Contract",
            "body_md": "Changed",
            "idempotency_key": key,
            "ack_required": True,
        },
    )["id"]


def backdate(bridge, message_id, column):
    """Rewrites one stored receipt so a later stamp would be observable."""
    with store.connect(bridge.home, write=True) as db:
        db.execute(STATEMENTS[column], (EARLIER, message_id))


def receipt(bridge, actors, message_id):
    """Reports the recipient's stored receipts for one message."""
    inbox = store.call(bridge.home, actors[1], "fetch_inbox", {})
    row = next(item for item in inbox["messages"] if item["id"] == message_id)
    return row["read_ts"], row["ack_ts"]


def test_repeated_read_keeps_the_first_read_timestamp(bridge, actors):
    message_id = deliver(bridge, actors, "retry-read")
    store.call(
        bridge.home, actors[1], "mark_message_read", {"message_id": message_id}
    )
    backdate(bridge, message_id, "read_ts")
    result = store.call(
        bridge.home, actors[1], "mark_message_read", {"message_id": message_id}
    )
    assert result == {"id": message_id, "acknowledged": False}
    assert receipt(bridge, actors, message_id) == (EARLIER, None)


def test_acknowledgement_keeps_an_earlier_read_timestamp(bridge, actors):
    message_id = deliver(bridge, actors, "ack-after-read")
    store.call(
        bridge.home, actors[1], "mark_message_read", {"message_id": message_id}
    )
    backdate(bridge, message_id, "read_ts")
    result = store.call(
        bridge.home,
        actors[1],
        "acknowledge_message",
        {"message_id": message_id},
    )
    read_ts, ack_ts = receipt(bridge, actors, message_id)
    assert result == {"id": message_id, "acknowledged": True}
    assert read_ts == EARLIER
    assert ack_ts is not None and ack_ts != EARLIER


def test_repeated_acknowledgement_keeps_both_first_timestamps(bridge, actors):
    message_id = deliver(bridge, actors, "retry-ack")
    store.call(
        bridge.home,
        actors[1],
        "acknowledge_message",
        {"message_id": message_id},
    )
    backdate(bridge, message_id, "read_ts")
    backdate(bridge, message_id, "ack_ts")
    result = store.call(
        bridge.home,
        actors[1],
        "acknowledge_message",
        {"message_id": message_id},
    )
    assert result == {"id": message_id, "acknowledged": True}
    assert receipt(bridge, actors, message_id) == (EARLIER, EARLIER)


def test_marking_read_never_disturbs_an_acknowledgement(bridge, actors):
    message_id = deliver(bridge, actors, "read-after-ack")
    store.call(
        bridge.home,
        actors[1],
        "acknowledge_message",
        {"message_id": message_id},
    )
    backdate(bridge, message_id, "ack_ts")
    first_read = receipt(bridge, actors, message_id)[0]
    store.call(
        bridge.home, actors[1], "mark_message_read", {"message_id": message_id}
    )
    assert first_read is not None
    assert receipt(bridge, actors, message_id) == (first_read, EARLIER)


def test_retried_receipts_keep_rejecting_mail_outside_your_inbox(
    bridge, actors
):
    message_id = deliver(bridge, actors, "authorization")
    for tool in ("mark_message_read", "acknowledge_message"):
        for _ in range(2):
            with pytest.raises(BridgeError):
                store.call(
                    bridge.home, actors[0], tool, {"message_id": message_id}
                )
    assert receipt(bridge, actors, message_id) == (None, None)
