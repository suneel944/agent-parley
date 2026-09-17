"""Checks outbound notification selection, formatting and delivery."""

import json
import smtplib
import sys
import urllib.request
from pathlib import Path

import pytest

from agent_parley import checkpoints, cli, notify, supervision
from agent_parley.state import BridgeError, write_json


@pytest.fixture
def fake(monkeypatch):
    """Registers a recording transport and selects it in the environment."""
    sent: list[tuple[str, str]] = []
    monkeypatch.setitem(
        notify.TRANSPORTS,
        "fake",
        lambda config, subject, body: sent.append((subject, body)),
    )
    monkeypatch.setenv("AGENT_PARLEY_NOTIFY", "fake")
    return sent


@pytest.fixture
def refusing(monkeypatch):
    """Registers a transport that always refuses the message."""

    def refuse(config, subject, body):
        raise BridgeError("the bot token is rejected")

    monkeypatch.setitem(notify.TRANSPORTS, "broken", refuse)
    monkeypatch.setenv("AGENT_PARLEY_NOTIFY", "broken")


def offer_context(**extra):
    """Builds the situation a waiting handoff offer produces."""
    return {
        "repo": "demo",
        "provider": "codex",
        "session": "s1",
        "offer": "abc123",
        "issue": "7",
        **extra,
    }


def test_a_waiting_offer_is_the_only_ledger_change_that_notifies():
    assert (
        notify.select("PreToolUse", "coordination_pending", offer_context())
        is notify.Event.HANDOFF_OFFERED
    )
    assert notify.select("PreToolUse", "coordination_pending", {}) is None
    assert notify.select("Stop", "observed", {}) is None


def test_a_blocked_prompt_a_finished_run_and_a_refusal_each_notify():
    assert (
        notify.select("PermissionRequest", "observed", {})
        is notify.Event.PERMISSION_PROMPT
    )
    assert (
        notify.select("SessionEnd", "observed", {}) is notify.Event.RUN_FINISHED
    )
    for reason in ("branch_switch", "branch_drift"):
        assert (
            notify.select("PreToolUse", reason, offer_context())
            is notify.Event.HOOK_REFUSAL
        )


def test_the_offer_reported_is_the_one_naming_this_lane():
    ledger = {
        "revision": 4,
        "issues": {
            "3": {"offer": {"to": "claude", "id": "other"}},
            "7": {"offer": {"to": "codex", "id": "abc123"}},
            "9": {"offer": None},
        },
    }
    assert notify.offered(ledger, "codex") == {
        "issue": "7",
        "offer": "abc123",
    }
    assert notify.offered(ledger, "amp") == {}
    assert notify.offered({}, "codex") == {}


def test_the_body_reports_one_field_a_line_and_skips_empty_ones():
    subject, body = notify.compose(
        notify.Event.HANDOFF_OFFERED,
        {**offer_context(), "lane": "codex", "event": "handoff_offered"},
    )
    assert subject == "Agent Parley: A handoff offer is waiting (codex)"
    assert body.splitlines() == [
        "A handoff offer is waiting",
        "repo: demo",
        "lane: codex",
        "provider: codex",
        "event: handoff_offered",
        "issue: 7",
        "offer: abc123",
    ]


def test_a_long_field_is_clipped_to_the_hook_notice_cap():
    _, body = notify.compose(
        notify.Event.HOOK_REFUSAL, {"repo": "r", "detail": "é" * 4000}
    )
    encoded = body.encode()
    assert len(encoded) <= notify.MAX_MESSAGE_BYTES
    assert len(encoded) > notify.MAX_MESSAGE_BYTES - 4
    assert body.encode().decode() == body


def test_an_unknown_transport_name_is_refused(monkeypatch):
    monkeypatch.setenv("AGENT_PARLEY_NOTIFY", "telegram,carrier-pigeon")
    with pytest.raises(BridgeError, match="carrier-pigeon"):
        notify.settings()


def test_the_tls_mode_decides_the_default_smtp_port():
    assert notify.settings({})["email"]["port"] == notify.SUBMISSION_PORT
    implicit = notify.settings({"AGENT_PARLEY_SMTP_TLS": "implicit"})
    assert implicit["email"]["port"] == notify.IMPLICIT_PORT
    pinned = notify.settings({"AGENT_PARLEY_SMTP_PORT": "2525"})
    assert pinned["email"]["port"] == 2525
    with pytest.raises(BridgeError, match="AGENT_PARLEY_SMTP_TLS"):
        notify.settings({"AGENT_PARLEY_SMTP_TLS": "sometimes"})
    with pytest.raises(BridgeError, match="AGENT_PARLEY_SMTP_PORT"):
        notify.settings({"AGENT_PARLEY_SMTP_PORT": "smtp"})


def test_settings_read_recipients_and_never_touch_the_process_environment(
    monkeypatch,
):
    monkeypatch.delenv("AGENT_PARLEY_SMTP_TO", raising=False)
    config = notify.settings(
        {
            "AGENT_PARLEY_NOTIFY": "email",
            "AGENT_PARLEY_SMTP_TO": "a@example.com, b@example.com",
            "AGENT_PARLEY_SMTP_FROM": "bridge@example.com",
            "AGENT_PARLEY_SMTP_HOST": "smtp.example.com",
        }
    )
    assert config["transports"] == ["email"]
    assert config["email"]["recipients"] == ["a@example.com", "b@example.com"]


def test_a_missing_credential_is_reported_rather_than_raised():
    config = notify.settings({"AGENT_PARLEY_NOTIFY": "telegram,email"})
    results = notify.send(config, "subject", "body")
    assert [item["transport"] for item in results] == ["telegram", "email"]
    assert not any(item["ok"] for item in results)
    assert "AGENT_PARLEY_TELEGRAM_TOKEN" in results[0]["error"]
    assert "AGENT_PARLEY_SMTP_HOST" in results[1]["error"]


def test_telegram_posts_one_form_encoded_message(monkeypatch):
    captured: dict = {}

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"ok": true}'

    def urlopen(request, timeout=0):
        captured["url"] = request.full_url
        captured["data"] = request.data.decode()
        captured["method"] = request.get_method()
        return Response()

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    notify.telegram(
        {"telegram": {"token": "t", "chat": "42", "api": "https://api"}},
        "subject",
        "body",
    )
    assert captured["url"] == "https://api/bott/sendMessage"
    assert captured["method"] == "POST"
    assert "chat_id=42" in captured["data"]
    assert "subject" in captured["data"]


def test_email_starts_tls_before_it_authenticates(monkeypatch):
    steps: list[str] = []

    class Session:
        def __init__(self, host, port, timeout=0):
            steps.append(f"connect {host}:{port}")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def starttls(self):
            steps.append("starttls")

        def login(self, user, password):
            steps.append(f"login {user}")

        def send_message(self, message):
            steps.append(f"send {message['Subject']} to {message['To']}")

    monkeypatch.setattr(smtplib, "SMTP", Session)
    notify.email(
        {
            "email": {
                "host": "smtp.example.com",
                "port": 587,
                "user": "bridge",
                "password": "secret",
                "sender": "bridge@example.com",
                "recipients": ["owner@example.com"],
                "tls": "starttls",
            }
        },
        "subject",
        "body",
    )
    assert steps == [
        "connect smtp.example.com:587",
        "starttls",
        "login bridge",
        "send subject to owner@example.com",
    ]


def test_the_same_situation_notifies_once(tmp_path, fake):
    assert notify.deliver(
        tmp_path, "codex", notify.Event.HANDOFF_OFFERED, offer_context()
    )
    assert not notify.deliver(
        tmp_path, "codex", notify.Event.HANDOFF_OFFERED, offer_context()
    )
    assert notify.deliver(
        tmp_path,
        "codex",
        notify.Event.HANDOFF_OFFERED,
        offer_context(offer="def456"),
    )
    notify.drain()
    assert len(fake) == 2
    marker = json.loads((tmp_path / "codex-notify.json").read_text())
    assert set(marker) == {"handoff_offered"}


def test_no_configured_transport_sends_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_PARLEY_NOTIFY", raising=False)
    assert (
        notify.deliver(
            tmp_path, "codex", notify.Event.RUN_FINISHED, {"session": "s1"}
        )
        == ""
    )
    assert not (tmp_path / "codex-notify.json").exists()


def test_a_refused_send_records_one_event_and_moves_on(tmp_path, refusing):
    notify.deliver(
        tmp_path, "codex", notify.Event.RUN_FINISHED, {"session": "s1"}
    )
    notify.drain()
    recorded = [
        json.loads(line)
        for line in (tmp_path / "codex-events.jsonl").read_text().splitlines()
    ]
    assert [entry["reason_class"] for entry in recorded] == [
        "notification_failed"
    ]
    assert "the bot token is rejected" in recorded[0]["cause"]


def test_the_test_message_reports_each_transport(tmp_path, fake):
    report = notify.probe("demo")
    assert report["root"] == "demo"
    assert report["results"] == [{"transport": "fake", "ok": True, "error": ""}]
    assert fake[0][1].splitlines()[1] == "repo: demo"


def test_the_test_message_needs_a_configured_transport(monkeypatch):
    monkeypatch.delenv("AGENT_PARLEY_NOTIFY", raising=False)
    with pytest.raises(BridgeError, match="AGENT_PARLEY_NOTIFY"):
        notify.probe("demo")


def invoke(monkeypatch, *arguments):
    """Runs one command line and returns its exit status."""
    monkeypatch.setattr(sys, "argv", ["agent-parley", *arguments])
    return cli.main()


def test_the_command_reports_a_refusal_and_exits_one(
    tmp_path, refusing, monkeypatch, capsys
):
    status = invoke(monkeypatch, "notify", "test", "--repo", str(tmp_path))
    assert status == 1
    printed = capsys.readouterr().out
    assert "broken: failed:" in printed
    assert "the bot token is rejected" in printed


def test_the_command_reports_a_sent_message_as_json(
    tmp_path, fake, monkeypatch, capsys
):
    status = invoke(
        monkeypatch, "notify", "test", "--repo", str(tmp_path), "--json"
    )
    assert status == 0
    document = json.loads(capsys.readouterr().out)
    assert document["kind"] == "notify"
    assert document["results"] == [
        {"transport": "fake", "ok": True, "error": ""}
    ]


def test_an_idle_lane_past_the_grace_period_notifies_once(
    tmp_path, fake, monkeypatch
):
    directory = tmp_path / "state"
    directory.mkdir()
    manifest = {
        "root": "demo",
        "participants": {"codex": {"provider": "codex"}},
    }
    write_json(directory / "codex-activity.json", {"updated": 1000.0})
    started = supervision.announce_idle(
        directory, manifest, ["codex"], {"codex": 900}, 600
    )
    assert started == ["codex"]
    assert (
        supervision.announce_idle(
            directory, manifest, ["codex"], {"codex": 930}, 600
        )
        == []
    )
    write_json(directory / "codex-activity.json", {"updated": 2000.0})
    assert supervision.announce_idle(
        directory, manifest, ["codex"], {"codex": 900}, 600
    ) == ["codex"]
    notify.drain()
    assert len(fake) == 2
    assert "A lane is idle with no claim" in fake[0][0]
    assert "idle 900s with no claim" in fake[0][1]


def test_a_broken_transport_leaves_the_sweep_running(tmp_path, monkeypatch):
    directory = tmp_path / "state"
    directory.mkdir()
    monkeypatch.setenv("AGENT_PARLEY_NOTIFY", "smoke-signal")
    manifest = {
        "root": "demo",
        "participants": {"codex": {"provider": "codex"}},
    }
    assert (
        supervision.announce_idle(
            directory, manifest, ["codex"], {"codex": 900}, 600
        )
        == []
    )
    recorded = json.loads((directory / "supervision-error.json").read_text())
    assert "smoke-signal" in recorded["detail"]


def test_a_recorded_decision_reaches_the_notifier(tmp_path, fake):
    manifest = {"root": "demo", "participants": {}}
    checkpoints.announce(
        tmp_path,
        "codex",
        manifest,
        {"provider": "codex"},
        {"hook_event_name": "SessionEnd", "session_id": "s1"},
        checkpoints.Reason.OBSERVED,
    )
    notify.drain()
    assert len(fake) == 1
    assert "A lane run finished" in fake[0][0]
    assert "provider: codex" in fake[0][1]


def test_a_lane_without_transports_skips_the_notifier(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_PARLEY_NOTIFY", raising=False)
    assert (
        checkpoints.announce(
            tmp_path,
            "codex",
            {"root": "demo", "participants": {}},
            {"provider": "codex"},
            {"hook_event_name": "SessionEnd", "session_id": "s1"},
            checkpoints.Reason.OBSERVED,
        )
        == ""
    )
    assert not Path(tmp_path / "codex-notify.json").exists()
