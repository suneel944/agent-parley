"""Checks a fake Telegram update answered by the real local service."""

import sys
import threading

import pytest

from agent_parley import cli, inbound, notify, server

PASSCODE = "correct-horse-battery"


@pytest.fixture
def service(bridge):
    """Serves the bridge's configured loopback port on a thread."""
    with server.Server(bridge.home, bridge.config) as instance:
        thread = threading.Thread(target=instance.serve_forever, daemon=True)
        thread.start()
        try:
            yield instance
        finally:
            instance.shutdown()
            thread.join(timeout=2)


@pytest.fixture
def configured(monkeypatch):
    """Points every transport variable at the test bot."""
    for variable, value in (
        ("AGENT_PARLEY_INBOUND", "telegram"),
        ("AGENT_PARLEY_INBOUND_PASSCODE", PASSCODE),
        ("AGENT_PARLEY_NOTIFY", "telegram"),
        ("AGENT_PARLEY_TELEGRAM_TOKEN", "t"),
        ("AGENT_PARLEY_TELEGRAM_CHAT", "42"),
        ("AGENT_PARLEY_TELEGRAM_API", "https://api"),
    ):
        monkeypatch.setenv(variable, value)


def polled(monkeypatch, stopped, text, chat=42):
    """Serves one fake `getUpdates` batch and then stops the reader."""
    sent: list[tuple[str, dict]] = []
    batches = [
        {
            "result": [
                {
                    "update_id": 11,
                    "message": {
                        "message_id": 3,
                        "chat": {"id": chat},
                        "text": text,
                    },
                }
            ]
        }
    ]

    def call(config, method, fields, timeout=notify.TIMEOUT):
        if method != "getUpdates":
            sent.append((method, dict(fields)))
            return {"ok": True}
        if batches:
            return batches.pop(0)
        stopped.set()
        return {"result": []}

    monkeypatch.setattr(notify, "call", call)
    return sent


def replies(sent):
    """Returns the text of every message the reader sent."""
    return [
        fields["text"] for method, fields in sent if method == "sendMessage"
    ]


def test_a_fake_update_answers_the_command_line_reading(
    bridge, repo, paired, service, configured, monkeypatch, capsys
):
    stopped = threading.Event()
    sent = polled(monkeypatch, stopped, f"{PASSCODE} status --provider claude")
    inbound.run(bridge.home, stopped)
    capsys.readouterr()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agent-parley",
            "--home",
            str(bridge.home),
            "status",
            "--provider",
            "claude",
        ],
    )
    assert cli.main() == 0
    printed = capsys.readouterr().out
    assert [method for method, _ in sent] == ["deleteMessage", "sendMessage"]
    assert replies(sent) == [printed]
    assert "claude" in printed
    assert PASSCODE not in printed


def test_a_fake_update_from_another_chat_is_never_answered(
    bridge, repo, paired, service, configured, monkeypatch
):
    stopped = threading.Event()
    sent = polled(monkeypatch, stopped, f"{PASSCODE} status", chat=99)
    inbound.run(bridge.home, stopped)
    assert sent == []


def test_a_fake_update_with_a_wrong_passcode_is_never_answered(
    bridge, repo, paired, service, configured, monkeypatch
):
    stopped = threading.Event()
    sent = polled(monkeypatch, stopped, "guessing status")
    inbound.run(bridge.home, stopped)
    assert sent == []
