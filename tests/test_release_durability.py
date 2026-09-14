"""Checks that a committed issue release survives reminder failures."""

import json
import sqlite3
from pathlib import Path

import pytest

from agent_parley import issues, store, supervision
from agent_parley.state import BridgeError


@pytest.fixture
def held(bridge, paired):
    """Claims one issue for the Claude lane and reports that lane path."""
    store.initialize(bridge.home)
    for name in ("claude", "codex"):
        store.register(bridge.home, paired["root"], name)
    lane = Path(paired["lanes"]["claude"])
    bridge.issue(lane, "claim", "1")
    return lane


def diagnostic(directory):
    """Reads any recorded supervision diagnostic."""
    path = directory / issues.SUPERVISION_ERROR
    return json.loads(path.read_text()) if path.exists() else None


@pytest.mark.parametrize(
    "failure",
    [
        BridgeError("issues.lock is held"),
        OSError("issues.json is unwritable"),
        sqlite3.OperationalError("database is locked"),
    ],
)
def test_a_failing_reminder_never_fails_a_committed_release(
    bridge, held, monkeypatch, failure
):
    def explode(*args, **kwargs):
        raise failure

    monkeypatch.setattr(supervision, "reminders", explode)
    record = bridge.issue(held, "release", "1")
    assert record["owner"] is None
    directory = held.parent
    assert issues.snapshot(directory)["issues"]["1"]["owner"] is None
    assert str(failure) in diagnostic(directory)["detail"]


def test_a_failing_reminder_leaves_exactly_one_ownership_transition(
    bridge, held, monkeypatch
):
    monkeypatch.setattr(
        supervision,
        "reminders",
        lambda *args, **kwargs: (_ for _ in ()).throw(BridgeError("locked")),
    )
    bridge.issue(held, "release", "1")
    history = issues.snapshot(held.parent)["issues"]["1"]["history"]
    assert [entry["action"] for entry in history] == ["claim", "release"]


def test_a_failing_reminder_does_not_block_a_later_claim(
    bridge, paired, held, monkeypatch
):
    monkeypatch.setattr(
        supervision,
        "reminders",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("unwritable")),
    )
    bridge.issue(held, "release", "1")
    record = bridge.issue(Path(paired["lanes"]["codex"]), "claim", "1")
    assert record["owner"] == "codex"


def test_a_successful_supervision_poll_clears_the_diagnostic(
    bridge, held, monkeypatch
):
    monkeypatch.setattr(
        supervision,
        "reminders",
        lambda *args, **kwargs: (_ for _ in ()).throw(BridgeError("locked")),
    )
    bridge.issue(held, "release", "1")
    directory = held.parent
    assert diagnostic(directory) is not None
    monkeypatch.undo()
    supervision.poll(bridge.home, directory)
    assert diagnostic(directory) is None


def test_a_failing_ownership_transaction_is_still_reported(bridge, held):
    with pytest.raises(BridgeError, match="has no owner"):
        bridge.issue(held, "release", "2")
    assert diagnostic(held.parent) is None
