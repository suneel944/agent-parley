"""Checks that an operator answer is carried out rather than only mailed."""

import pytest

from agent_parley import (
    budgets,
    decisions,
    dialogs,
    inbound,
    notify,
    roster,
    store,
    supervision,
)
from agent_parley.state import BridgeError


@pytest.fixture
def directory(bridge, paired):
    """Returns the paired project's private state directory."""
    return roster.locate(bridge.home, paired["root"])


@pytest.fixture
def mailed(monkeypatch):
    """Records every message the service sends instead of storing it."""
    sent = []

    def speak(home, root, display, subject, body, key):
        sent.append({"to": display, "subject": subject, "body": body})

    monkeypatch.setattr(store, "speak", speak)
    return sent


def asked(directory, event, lane="codex", **extra):
    """Opens one decision of a notify event with its shipped options."""
    options, reversibility = notify.ANSWERS[event]
    return decisions.open_or_refresh(
        directory,
        project="/repo",
        lane=lane,
        kind=event.value,
        key=f"{event.value}:{lane}",
        question=event.value,
        options=options,
        reversibility=reversibility,
        **extra,
    )


def shown(directory, lane="codex"):
    """Opens the relayed native dialog a lane shows for a tool call."""
    return decisions.open_or_refresh(
        directory,
        project="/repo",
        lane=lane,
        kind=dialogs.DECISION_KIND,
        key=f"dialog:{lane}",
        question="Run Bash?",
        options=("Yes", "Yes, and don't ask again", "No"),
        confirm=("Yes, and don't ask again",),
    )


@pytest.mark.parametrize(
    ("option", "pressed"),
    [(notify.PERMISSION_DENY, "No"), (notify.PERMISSION_ALLOW, "Yes")],
)
def test_a_permission_answer_presses_the_lane_dialog(
    bridge, directory, mailed, option, pressed
):
    dialog = shown(directory)
    record = asked(directory, notify.Event.PERMISSION_PROMPT)
    state, answered = inbound.decide(
        bridge.home, record["id"], option, "cli", confirmed=True
    )
    assert state == inbound.ANSWERED
    assert answered["answer"] == option
    assert decisions.get(directory, dialog["id"])["answer"] == pressed
    assert decisions.get(directory, record["id"])["applied"]
    assert mailed == []


def test_a_permission_answer_without_a_dialog_stays_open(
    bridge, directory, mailed
):
    record = asked(directory, notify.Event.PERMISSION_PROMPT)
    with pytest.raises(BridgeError, match="no relayed permission dialog"):
        inbound.decide(
            bridge.home,
            record["id"],
            notify.PERMISSION_DENY,
            "cli",
            confirmed=True,
        )
    assert decisions.get(directory, record["id"])["state"] == decisions.OPEN


def test_raising_the_run_budget_resumes_the_run(
    bridge, directory, mailed, monkeypatch
):
    resumed = []
    monkeypatch.setattr(
        budgets, "resume", lambda *arguments: resumed.append(arguments)
    )
    record = asked(
        directory, notify.Event.RUN_BUDGET_EXHAUSTED, lane="run-budget"
    )
    inbound.decide(bridge.home, record["id"], notify.BUDGET_RAISE, "cli")
    assert len(resumed) == 1
    assert decisions.get(directory, record["id"])["applied"]
    assert mailed == []


def test_a_refused_budget_raise_leaves_the_decision_open(
    bridge, directory, monkeypatch
):
    def refuse(*arguments):
        raise BridgeError("The run is still over its limit.")

    monkeypatch.setattr(budgets, "resume", refuse)
    record = asked(
        directory, notify.Event.RUN_BUDGET_EXHAUSTED, lane="run-budget"
    )
    with pytest.raises(BridgeError, match="still over its limit"):
        inbound.decide(bridge.home, record["id"], notify.BUDGET_RAISE, "cli")
    assert decisions.get(directory, record["id"])["state"] == decisions.OPEN


def test_giving_orphaned_work_mails_the_live_peer_not_the_dead_lane(
    bridge, directory, mailed, monkeypatch
):
    monkeypatch.setattr(supervision, "orphan_peer", lambda *a: "claude")
    record = asked(directory, notify.Event.ORPHAN_DECISION, issue="7")
    inbound.decide(bridge.home, record["id"], notify.ORPHAN_GIVE, "cli")
    manifest = roster.read(directory)
    assert [item["to"] for item in mailed] == [
        manifest["participants"]["claude"]["display"]
    ]
    assert "--take-orphaned" in mailed[0]["body"]


def test_giving_orphaned_work_with_no_peer_stays_open(
    bridge, directory, mailed, monkeypatch
):
    monkeypatch.setattr(supervision, "orphan_peer", lambda *a: None)
    record = asked(directory, notify.Event.ORPHAN_DECISION, issue="7")
    with pytest.raises(BridgeError, match="No live peer"):
        inbound.decide(bridge.home, record["id"], notify.ORPHAN_GIVE, "cli")
    assert decisions.get(directory, record["id"])["state"] == decisions.OPEN
    assert mailed == []


@pytest.mark.parametrize(
    ("option", "returned"),
    [(notify.ORPHAN_RETURN, ["codex"]), (notify.ORPHAN_WAIT, [])],
)
def test_returning_or_waiting_on_orphaned_work(
    bridge, directory, mailed, monkeypatch, option, returned
):
    calls = []
    monkeypatch.setattr(
        supervision,
        "return_orphaned",
        lambda home, where, name: calls.append(name),
    )
    record = asked(directory, notify.Event.ORPHAN_DECISION, issue="7")
    inbound.decide(bridge.home, record["id"], option, "cli")
    assert calls == returned
    assert mailed == []
