"""Checks that a closed claim retires the mail that claim left behind."""

import json
import os
import time
from pathlib import Path

import pytest

from agent_parley import checkpoints, process, store, supervision, terminal
from agent_parley.state import write_json


def mail(bridge, paired, name):
    """Reads one lane's mailbox exactly as a checkpoint would."""
    return checkpoints.mailbox(bridge.home, paired["root"], name)


def lane_record(snapshot, name):
    """Returns one participant's record from a status reading."""
    return [
        record
        for project in snapshot["projects"]
        for record in project["participants"]
        if record["participant"] == name
    ][0]


def send(bridge, actor, key, **extra):
    """Sends one message from an authenticated lane to the codex lane."""
    return store.call(
        bridge.home,
        actor,
        "send_message",
        {
            "to": ["codex"],
            "subject": f"Parser {key}",
            "body_md": "Work in progress",
            "idempotency_key": key,
            **extra,
        },
    )


@pytest.fixture
def correlated(bridge, paired):
    """Mails codex twice from claude while claude holds a claim."""
    store.initialize(bridge.home)
    registered = {
        name: store.register(bridge.home, paired["root"], name)
        for name in ("claude", "codex")
    }
    actor = store.authenticate(
        bridge.home, registered["claude"]["registration_token"]
    )
    lane = Path(paired["lanes"]["claude"])
    bridge.issue(lane, "claim", "1")
    bound = [
        send(bridge, actor, f"bound-{index}", ack_required=True)["id"]
        for index in (1, 2)
    ]
    return {
        "directory": lane.parent,
        "lane": lane,
        "actor": actor,
        "bound": bound,
    }


def test_closing_a_claim_supersedes_the_mail_it_sent(
    bridge, paired, correlated
):
    before = mail(bridge, paired, "codex")
    assert (before["unread"], before["pending_ack"]) == (2, 2)

    bridge.issue(correlated["lane"], "release", "1")

    after = mail(bridge, paired, "codex")
    assert after["unread"] == 0
    assert after["superseded"] == 2
    assert after["pending_ack"] == 0
    assert after["outstanding_ack"] == []
    assert after["messages"] == []
    with store.connect(bridge.home) as db:
        reasons = db.execute(
            "SELECT superseded_reason AS reason FROM message_recipients "
            "WHERE message_id IN (?,?) ORDER BY message_id",
            tuple(correlated["bound"]),
        ).fetchall()
    assert [row["reason"] for row in reasons] == ["issue #1 released"] * 2


def test_superseded_mail_never_names_a_lane_as_stalled(
    bridge, paired, correlated
):
    with store.connect(bridge.home, write=True) as db:
        db.execute(
            "UPDATE messages SET created_ts=datetime('now','-1800 seconds')"
        )
    waiting = store.waiting(bridge.home, paired["root"], "codex")
    assert waiting["kind"] == "acknowledgement"

    bridge.issue(correlated["lane"], "release", "1")

    assert store.waiting(bridge.home, paired["root"], "codex")["kind"] is None


def test_a_wake_after_a_closed_claim_carries_only_live_threads(
    bridge, paired, correlated, monkeypatch
):
    directory = correlated["directory"]
    bridge.issue(correlated["lane"], "release", "1")
    opener = send(bridge, correlated["actor"], "live-1")["id"]
    latest = send(bridge, correlated["actor"], "live-2", reply_to=opener)["id"]
    write_json(
        directory / "codex-activity.json",
        {
            "activity": "idle",
            "updated": time.time() - 500,
            "session_pid": os.getpid(),
            "session_ticks": process.start_ticks(os.getpid()),
        },
    )
    woken: list = []
    monkeypatch.setattr(
        terminal,
        "request",
        lambda directory, name: woken.append(name) or "wake requested",
    )

    supervision.poll(bridge.home, directory)

    assert "codex" in woken
    record = json.loads((directory / "codex-wake.json").read_text())
    mailed = [item for item in record["backlog"] if item.isdigit()]
    assert mailed == [str(latest)]
    assert record["superseded"] == 2
    assert str(opener) not in record["backlog"]
    for number in correlated["bound"]:
        assert str(number) not in record["backlog"]


def test_a_wake_digest_is_bounded_to_the_newest_threads():
    rows = [
        {"id": index, "thread_id": f"thread-{index}"}
        for index in range(1, supervision.WAKE_DIGEST_THREADS + 4)
    ]
    digest = supervision._mail_digest(rows)
    assert len(digest) == supervision.WAKE_DIGEST_THREADS
    assert digest[-1] == str(rows[-1]["id"])
    assert digest == sorted(digest, key=int)


def test_status_separates_live_unread_from_superseded(
    bridge, repo, paired, correlated
):
    bridge.say(repo, "codex", "Operator question")
    bridge.issue(correlated["lane"], "release", "1")

    record = lane_record(bridge.status_snapshot(), "codex")

    assert record["mail"]["unread"] == 1
    assert record["mail"]["superseded"] == 2
