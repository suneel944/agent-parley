"""Checks durable operator decisions: one per situation, sent as a digest."""

import json

import pytest

from agent_parley import decisions, notify
from agent_parley.state import BridgeError


def opened(directory, key="s1", lane="codex", now=100.0, **extra):
    """Opens one decision with the fields most checks share."""
    return decisions.open_or_refresh(
        directory,
        project="/repo",
        lane=lane,
        kind="handoff_offered",
        key=key,
        question="A handoff offer is waiting",
        options=("accept", "decline"),
        issue="7",
        now=now,
        **extra,
    )


@pytest.fixture
def sent(monkeypatch):
    """Records every message and the keyboard it carried."""
    messages: list[dict] = []

    def fake(config, subject, body):
        messages.append(
            {"subject": subject, "body": body, "markup": config.get("markup")}
        )

    monkeypatch.setitem(notify.TRANSPORTS, "fake", fake)
    return messages


def test_a_repeat_observation_refreshes_the_one_open_decision(tmp_path):
    first = opened(tmp_path, now=100.0)
    again = opened(tmp_path, now=200.0)
    assert again["id"] == first["id"]
    assert again["refreshed"] == 200.0
    assert again["expires"] == 200.0 + decisions.DEFAULT_TTL
    assert [
        record["id"] for record in decisions.list_open(tmp_path, 200.0)
    ] == [first["id"]]
    other = opened(tmp_path, key="s2", now=200.0)
    assert other["id"] != first["id"]
    assert len(decisions.list_open(tmp_path, 200.0)) == 2


def test_the_first_option_is_recommended_unless_one_is_named(tmp_path):
    assert opened(tmp_path)["recommended"] == "accept"
    with pytest.raises(BridgeError, match="not offered"):
        opened(tmp_path, key="s3", recommended="maybe")
    with pytest.raises(BridgeError, match="reversibility"):
        opened(tmp_path, key="s4", reversibility="sometimes")


def test_an_answer_settles_the_decision_and_a_new_one_opens_after(tmp_path):
    record = opened(tmp_path)
    with pytest.raises(BridgeError, match="offers accept, decline"):
        decisions.answer(tmp_path, record["id"], "maybe", "cli", now=150.0)
    answered = decisions.answer(
        tmp_path, record["id"], "decline", "telegram", now=150.0
    )
    assert answered["state"] == decisions.ANSWERED
    assert answered["answer"] == "decline"
    assert answered["answered_by"] == "telegram"
    with pytest.raises(BridgeError, match="already answered"):
        decisions.answer(tmp_path, record["id"], "accept", "cli", now=160.0)
    assert decisions.list_open(tmp_path, 160.0) == []
    reopened = opened(tmp_path, now=170.0)
    assert reopened["state"] == decisions.OPEN
    assert reopened["created"] == 170.0


def test_an_unobserved_decision_expires_and_a_closed_one_leaves(tmp_path):
    record = opened(tmp_path, ttl=10.0, now=100.0)
    assert decisions.list_open(tmp_path, 105.0)
    assert decisions.list_open(tmp_path, 111.0) == []
    other = opened(tmp_path, key="s2", now=100.0)
    assert decisions.close(tmp_path, other["id"], now=101.0)
    assert not decisions.close(tmp_path, other["id"], now=102.0)
    assert decisions.get(tmp_path, other["id"])["state"] == decisions.CLOSED
    with pytest.raises(BridgeError, match="already expired"):
        decisions.answer(tmp_path, record["id"], "accept", "cli", now=120.0)


def test_due_decisions_go_out_as_one_digest_with_a_row_each(tmp_path, sent):
    first = opened(tmp_path, now=100.0)
    second = opened(tmp_path, key="s2", lane="claude", now=101.0)
    report = decisions.flush(tmp_path, {"transports": ["fake"]}, now=102.0)
    assert report["sent"] == [first["id"], second["id"]]
    assert len(sent) == 1
    assert sent[0]["subject"] == "Agent Parley: 2 decisions are waiting"
    assert f"reply: decide {first['id']} <option>" in sent[0]["body"]
    assert "accept (recommended)" in sent[0]["body"]
    rows = sent[0]["markup"]["inline_keyboard"]
    assert [button["callback_data"] for button in rows[0]] == [
        f"d:{first['id']}:0",
        f"d:{first['id']}:1",
    ]
    assert rows[0][0]["text"] == "accept *"
    assert all(
        len(button["callback_data"].encode()) <= 64
        for row in rows
        for button in row
    )
    assert decisions.flush(tmp_path, {"transports": ["fake"]}, now=103.0) == {
        "sent": [],
        "results": [],
    }
    assert opened(tmp_path, now=104.0)["sent"] == 102.0
    assert len(sent) == 1


def test_a_refused_digest_is_retried_with_a_growing_backoff(
    tmp_path, monkeypatch
):
    def refuse(config, subject, body):
        raise BridgeError("the bot token is rejected")

    monkeypatch.setitem(notify.TRANSPORTS, "broken", refuse)
    config = {"transports": ["broken"]}
    record = opened(tmp_path, now=100.0)
    assert decisions.flush(tmp_path, config, now=100.0)["sent"] == []
    stored = decisions.get(tmp_path, record["id"])
    assert stored["attempts"] == 1
    assert stored["retry_at"] == 100.0 + decisions.BACKOFF_FIRST
    assert "the bot token is rejected" in stored["error"]
    assert decisions.flush(tmp_path, config, now=110.0)["results"] == []
    decisions.flush(tmp_path, config, now=131.0)
    stored = decisions.get(tmp_path, record["id"])
    assert stored["attempts"] == 2
    assert stored["retry_at"] == 131.0 + 2 * decisions.BACKOFF_FIRST


def test_a_decision_leased_by_one_sender_is_not_sent_by_another(tmp_path):
    opened(tmp_path, now=100.0)
    assert decisions._lease(tmp_path, 100.0)
    assert decisions._lease(tmp_path, 101.0) == []
    assert decisions._lease(tmp_path, 100.0 + decisions.LEASE_SECONDS)


def test_a_hook_that_exits_before_sending_is_delivered_by_the_poll(
    tmp_path, monkeypatch, sent
):
    monkeypatch.setenv("AGENT_PARLEY_NOTIFY", "fake")
    monkeypatch.setattr(notify, "_SENDING", set())
    monkeypatch.setattr(notify, "_THREADS", [])
    monkeypatch.setattr(notify.threading.Thread, "start", lambda self: None)
    assert notify.deliver(
        tmp_path,
        "codex",
        notify.Event.HANDOFF_OFFERED,
        {"repo": "/repo", "offer": "abc", "issue": "7"},
    )
    assert sent == []
    waiting = decisions.list_open(tmp_path)
    assert [record["kind"] for record in waiting] == ["handoff_offered"]
    assert waiting[0]["options"] == ["accept", "decline"]
    assert waiting[0]["issue"] == "7"
    report = notify.flush_decisions(tmp_path)
    assert report["sent"] == [waiting[0]["id"]]
    assert sent[0]["subject"] == (
        "Agent Parley: A handoff offer is waiting (codex)"
    )
    assert sent[0]["body"].splitlines()[1] == "repo: /repo"
    assert sent[0]["markup"]["inline_keyboard"][0][0]["text"] == "accept *"


def test_no_credential_reaches_a_decision_record(tmp_path, monkeypatch, sent):
    monkeypatch.setenv("AGENT_PARLEY_NOTIFY", "fake")
    monkeypatch.setenv("AGENT_PARLEY_TELEGRAM_TOKEN", "123:secret-token")
    notify.deliver(
        tmp_path,
        "codex",
        notify.Event.PERMISSION_PROMPT,
        {"repo": "/repo", "session": "s1", "tool": "Bash"},
    )
    notify.drain()
    text = (tmp_path / decisions.RECORD_NAME).read_text()
    assert "secret-token" not in text
    record = next(iter(json.loads(text).values()))
    assert record["reversibility"] == decisions.IRREVERSIBLE
    assert record["recommended"] == "deny"
    assert record["sent"]
