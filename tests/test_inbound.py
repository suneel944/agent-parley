"""Checks the inbound status reader, decision answers and the passcode gate."""

import json
import threading
from pathlib import Path

import pytest

from agent_parley import decisions, inbound, notify, store

PASSCODE = "correct-horse-battery"
CONFIG = {
    "transports": ["telegram"],
    "telegram": {"token": "t", "chat": "42", "api": "https://api"},
}


@pytest.fixture
def calls(monkeypatch):
    """Records every Bot API call instead of reaching Telegram."""
    recorded: list[tuple[str, dict]] = []

    def call(config, method, fields, timeout=notify.TIMEOUT):
        recorded.append((method, dict(fields)))
        return {"ok": True, "result": []}

    monkeypatch.setattr(notify, "call", call)
    return recorded


@pytest.fixture
def gate():
    """Builds a gate holding the test passcode."""
    return inbound.Gate(inbound.Passcode(PASSCODE))


def message(text, chat=42, identifier=7, number=1):
    """Builds one Bot API update carrying a chat message."""
    return {
        "update_id": number,
        "message": {
            "message_id": identifier,
            "chat": {"id": chat},
            "text": text,
        },
    }


def replies(calls):
    """Returns the text of every message the reader sent."""
    return [
        fields["text"] for method, fields in calls if method == "sendMessage"
    ]


def test_a_message_from_another_chat_is_dropped(bridge, paired, gate, calls):
    verdict = inbound.serve(
        bridge.home, CONFIG, gate, message(f"{PASSCODE} status", chat=99)
    )
    assert verdict == inbound.DROPPED
    assert calls == []


def test_an_update_without_a_message_is_dropped(bridge, gate, calls):
    assert (
        inbound.serve(bridge.home, CONFIG, gate, {"update_id": 5, "poll": {}})
        == inbound.DROPPED
    )
    assert calls == []


def test_the_right_passcode_answers_with_the_status_reading(
    bridge, paired, gate, calls
):
    verdict = inbound.serve(
        bridge.home, CONFIG, gate, message(f"{PASSCODE} status")
    )
    assert verdict == inbound.ACCEPTED
    assert [method for method, _ in calls] == ["deleteMessage", "sendMessage"]
    assert calls[0][1] == {"chat_id": "42", "message_id": 7}
    assert replies(calls)[0].startswith("Server:")
    assert str(bridge.home) in replies(calls)[0]


def test_a_wrong_passcode_gets_no_reply_at_all(bridge, paired, gate, calls):
    verdict = inbound.serve(
        bridge.home, CONFIG, gate, message("guessing status")
    )
    assert verdict == inbound.REFUSED
    assert calls == []


def test_an_empty_message_is_dropped(bridge, gate, calls):
    assert (
        inbound.serve(bridge.home, CONFIG, gate, message("   "))
        == inbound.DROPPED
    )
    assert calls == []


def test_five_wrong_passcodes_lock_the_path_and_notify_once(
    bridge, paired, gate, calls
):
    verdicts = [
        inbound.serve(
            bridge.home, CONFIG, gate, message("guessing status"), now=attempt
        )
        for attempt in range(inbound.FAILURE_LIMIT)
    ]
    assert verdicts[:-1] == [inbound.REFUSED] * (inbound.FAILURE_LIMIT - 1)
    assert verdicts[-1] == inbound.LOCKED
    assert len(replies(calls)) == 1
    assert "Inbound status queries are locked" in replies(calls)[0]
    assert PASSCODE not in replies(calls)[0]


def test_the_lock_refuses_even_the_right_passcode_until_it_expires(
    bridge, paired, gate, calls
):
    for attempt in range(inbound.FAILURE_LIMIT):
        inbound.serve(
            bridge.home, CONFIG, gate, message("guessing status"), now=attempt
        )
    calls.clear()
    held = inbound.serve(
        bridge.home, CONFIG, gate, message(f"{PASSCODE} status"), now=10.0
    )
    assert held == inbound.REFUSED
    assert calls == []
    later = inbound.serve(
        bridge.home,
        CONFIG,
        gate,
        message(f"{PASSCODE} status"),
        now=inbound.LOCK_SECONDS + 10,
    )
    assert later == inbound.ACCEPTED


def test_a_failure_outside_the_window_does_not_count_toward_the_lock(gate):
    spaced = [
        gate.admits("guessing", moment * inbound.FAILURE_WINDOW)
        for moment in range(inbound.FAILURE_LIMIT + 2)
    ]
    assert set(spaced) == {inbound.REFUSED}
    assert not gate.locked(inbound.FAILURE_WINDOW * 10)


def test_the_command_line_filters_parse_through_the_status_parser():
    assert inbound.command(["status"]).filtered() is False
    assert inbound.command(["status", "claude"]).participant == "claude"
    assert inbound.command(["status", "--project", "/tmp/x"]).project == (
        "/tmp/x"
    )
    repeated = inbound.command(
        ["status", "--provider", "codex", "--provider", "claude"]
    )
    assert repeated.providers == ("codex", "claude")
    assert inbound.command(["status", "--outcome", "ready"]).outcome == "ready"
    assert inbound.command(["status", "--drifted"]).drifted is True
    assert inbound.command(["status", "--pending"]).pending is True
    assert inbound.command(["status", "--idle"]).idle is True
    assert inbound.command(["status", "--over-budget"]).over_budget is True
    assert inbound.command(["status", "--since", "45m"]).since == 2700.0
    assert inbound.command(["status", "--issue", "7"]).issue == 7


def test_a_filter_the_status_parser_rejects_answers_one_usage_line(
    bridge, paired, gate, calls
):
    verdict = inbound.serve(
        bridge.home, CONFIG, gate, message(f"{PASSCODE} status --outcome soon")
    )
    assert verdict == inbound.ACCEPTED
    assert replies(calls) == [inbound.USAGE]


def test_a_verb_other_than_status_answers_one_usage_line(
    bridge, paired, gate, calls
):
    for text in ("claim 1", "handoff 1 --to codex", "wake codex", ""):
        calls.clear()
        inbound.serve(
            bridge.home, CONFIG, gate, message(f"{PASSCODE} {text}".strip())
        )
        assert replies(calls) == [inbound.USAGE]


def test_an_unparsable_window_answers_one_usage_line(
    bridge, paired, gate, calls
):
    inbound.serve(
        bridge.home, CONFIG, gate, message(f"{PASSCODE} status --since soon")
    )
    assert replies(calls) == [inbound.USAGE]


def test_a_long_reading_is_truncated_with_the_rows_it_cut():
    reading = "\n".join(f"row {number}" for number in range(2000))
    clipped = inbound.clip(reading)
    assert len(clipped) <= inbound.MAX_REPLY_CHARS
    assert clipped.startswith("row 0\n")
    assert clipped.splitlines()[-1].endswith(
        "not shown; narrow the query with a filter."
    )
    cut = int(clipped.splitlines()[-1].split()[0])
    assert len(clipped.splitlines()) + cut == 2001
    assert inbound.clip("short reading") == "short reading"


def test_the_passcode_is_held_only_as_a_salted_digest():
    held = inbound.Passcode(PASSCODE)
    assert held.matches(PASSCODE)
    assert not held.matches(PASSCODE + "!")
    assert not held.matches("")
    assert PASSCODE not in repr(vars(held))
    assert inbound.Passcode(PASSCODE)._digest != held._digest


def test_an_unset_or_short_passcode_reports_a_configuration_fault():
    asked = {
        "AGENT_PARLEY_INBOUND": "telegram",
        "AGENT_PARLEY_NOTIFY": "telegram",
        "AGENT_PARLEY_TELEGRAM_TOKEN": "t",
        "AGENT_PARLEY_TELEGRAM_CHAT": "42",
    }
    assert "AGENT_PARLEY_INBOUND_PASSCODE" in inbound.fault(asked)
    short = {**asked, "AGENT_PARLEY_INBOUND_PASSCODE": "a" * 11}
    assert "AGENT_PARLEY_INBOUND_PASSCODE" in inbound.fault(short)
    good = {**asked, "AGENT_PARLEY_INBOUND_PASSCODE": "a" * 12}
    assert inbound.fault(good) == ""
    assert inbound.fault({}) == ""
    assert "no such transport" in inbound.fault(
        {**good, "AGENT_PARLEY_INBOUND": "signal"}
    )
    missing = {**good, "AGENT_PARLEY_TELEGRAM_TOKEN": ""}
    assert "AGENT_PARLEY_TELEGRAM_TOKEN" in inbound.fault(missing)


def test_the_reader_refuses_to_start_without_a_usable_passcode(
    bridge, monkeypatch, calls, capsys
):
    monkeypatch.setenv("AGENT_PARLEY_INBOUND", "telegram")
    monkeypatch.delenv("AGENT_PARLEY_INBOUND_PASSCODE", raising=False)
    inbound.run(bridge.home, threading.Event())
    assert calls == []
    assert "AGENT_PARLEY_INBOUND_PASSCODE" in capsys.readouterr().out


def test_the_reader_stays_off_when_the_environment_never_asked(
    bridge, monkeypatch, calls, capsys
):
    monkeypatch.delenv("AGENT_PARLEY_INBOUND", raising=False)
    inbound.run(bridge.home, threading.Event())
    assert calls == []
    assert capsys.readouterr().out == ""


def test_status_reports_the_inbound_configuration_fault(
    bridge, paired, monkeypatch, capsys
):
    monkeypatch.setenv("AGENT_PARLEY_INBOUND", "telegram")
    monkeypatch.delenv("AGENT_PARLEY_INBOUND_PASSCODE", raising=False)
    bridge.status()
    printed = capsys.readouterr().out
    assert "inbound off: AGENT_PARLEY_INBOUND_PASSCODE must be set" in printed
    monkeypatch.delenv("AGENT_PARLEY_INBOUND")
    bridge.status()
    printed = capsys.readouterr().out
    assert "inbound off: AGENT_PARLEY_INBOUND is not set" in printed


@pytest.fixture
def mail(monkeypatch):
    """Records the supervisor mail an answer hands to its lane."""
    spoken: list[dict] = []

    def speak(home, root, name, subject, body, key, **options):
        spoken.append(
            {"name": name, "subject": subject, "body": body, "key": key}
        )
        return {"id": len(spoken)}

    monkeypatch.setattr(store, "speak", speak)
    return spoken


def waiting(paired, reversibility=decisions.REVERSIBLE):
    """Opens one decision for the codex lane under the bridge's state."""
    return decisions.open_or_refresh(
        Path(paired["lanes"]["codex"]).parent,
        project="/repo",
        lane="codex",
        kind="handoff_offered",
        key="s1",
        question="A handoff offer is waiting",
        options=("accept", "decline"),
        reversibility=reversibility,
    )


def tapping(name, index, chat=42, confirmed=False, markup=None):
    """Builds the callback query one button tap produces."""
    return {
        "update_id": 9,
        "callback_query": {
            "id": "q1",
            "from": {"id": 5},
            "data": decisions.callback(name, index, confirmed=confirmed),
            "message": {
                "message_id": 11,
                "chat": {"id": chat},
                "text": "Agent Parley: 1 decision is waiting",
                "reply_markup": markup or {"inline_keyboard": []},
            },
        },
    }


def test_a_tap_answers_the_decision_and_hands_it_to_the_lane(
    bridge, paired, gate, calls, mail
):
    record = waiting(paired)
    other = [{"text": "go", "callback_data": "d:other:0"}]
    markup = decisions.keyboard([record])
    markup["inline_keyboard"].append(other)
    verdict = inbound.serve(
        bridge.home, CONFIG, gate, tapping(record["id"], 1, markup=markup)
    )
    assert verdict == inbound.ACCEPTED
    stored = decisions.find(bridge.home, record["id"])[1]
    assert stored["state"] == decisions.ANSWERED
    assert stored["answer"] == "decline"
    assert stored["answered_by"] == "telegram:5"
    methods = [method for method, _ in calls]
    assert methods == ["editMessageText", "answerCallbackQuery"]
    edited = calls[0][1]
    assert "answered decline by telegram:5" in edited["text"]
    assert json.loads(edited["reply_markup"]) == {"inline_keyboard": [other]}
    assert mail[0]["name"] == "codex"
    assert mail[0]["key"] == f"decision:{record['id']}"
    assert "decline" in mail[0]["subject"]


def test_a_second_tap_is_refused_with_who_answered_first(
    bridge, paired, gate, calls, mail
):
    record = waiting(paired)
    inbound.serve(bridge.home, CONFIG, gate, tapping(record["id"], 0))
    calls.clear()
    verdict = inbound.serve(bridge.home, CONFIG, gate, tapping(record["id"], 1))
    assert verdict == inbound.REFUSED
    assert calls[-1][0] == "answerCallbackQuery"
    assert "already answered accept" in calls[-1][1]["text"]
    assert len(mail) == 1


def test_a_tap_from_another_chat_or_an_unknown_decision_is_dropped(
    bridge, paired, gate, calls, mail
):
    record = waiting(paired)
    assert (
        inbound.serve(bridge.home, CONFIG, gate, tapping(record["id"], 0, 99))
        == inbound.DROPPED
    )
    assert calls == []
    verdict = inbound.serve(bridge.home, CONFIG, gate, tapping("nope", 0))
    assert verdict == inbound.REFUSED
    assert "No decision nope" in calls[-1][1]["text"]
    stored = decisions.find(bridge.home, record["id"])[1]
    assert stored["state"] == decisions.OPEN


def test_an_irreversible_option_waits_for_the_confirming_tap(
    bridge, paired, gate, calls, mail
):
    record = waiting(paired, decisions.IRREVERSIBLE)
    first = inbound.serve(bridge.home, CONFIG, gate, tapping(record["id"], 0))
    assert first == inbound.ACCEPTED
    stored = decisions.find(bridge.home, record["id"])[1]
    assert stored["state"] == decisions.OPEN
    button = json.loads(calls[0][1]["reply_markup"])["inline_keyboard"][0][0]
    assert button["callback_data"] == decisions.callback(
        record["id"], 0, confirmed=True
    )
    assert mail == []
    inbound.serve(
        bridge.home, CONFIG, gate, tapping(record["id"], 0, confirmed=True)
    )
    assert decisions.find(bridge.home, record["id"])[1]["answer"] == "accept"
    assert len(mail) == 1


def test_a_typed_answer_carries_its_note_quoted_to_the_lane(
    bridge, paired, gate, calls, mail
):
    record = waiting(paired)
    text = f"{PASSCODE} decide {record['id']} accept take it after lunch"
    verdict = inbound.serve(bridge.home, CONFIG, gate, message(text))
    assert verdict == inbound.ACCEPTED
    assert replies(calls)[-1].startswith(f"Decision {record['id']} answered")
    stored = decisions.find(bridge.home, record["id"])[1]
    assert stored["note"] == "take it after lunch"
    assert "> take it after lunch" in mail[0]["body"]
    assert "not an instruction" in mail[0]["body"]


def test_a_reply_to_a_one_decision_digest_answers_it(
    bridge, paired, gate, calls, mail
):
    record = waiting(paired)
    update = message(f"{PASSCODE} decline not today")
    update["message"]["reply_to_message"] = {
        "text": decisions.compose([record])[1]
    }
    inbound.serve(bridge.home, CONFIG, gate, update)
    stored = decisions.find(bridge.home, record["id"])[1]
    assert stored["answer"] == "decline"
    assert stored["note"] == "not today"


def test_a_malformed_or_closed_typed_answer_is_refused_in_the_reply(
    bridge, paired, gate, calls, mail
):
    record = waiting(paired)
    inbound.serve(bridge.home, CONFIG, gate, message(f"{PASSCODE} decide x"))
    assert replies(calls)[-1] == inbound.DECIDE_USAGE
    decisions.close(Path(paired["lanes"]["codex"]).parent, record["id"])
    inbound.serve(
        bridge.home,
        CONFIG,
        gate,
        message(f"{PASSCODE} decide {record['id']} accept"),
    )
    assert "already closed" in replies(calls)[-1]
    assert mail == []
