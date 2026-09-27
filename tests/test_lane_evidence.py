"""Replays hook, dialog and session evidence into the lane state record."""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from agent_parley import checkpoints, lanes, process, store, supervision
from agent_parley.state import write_json


def hooked(bridge, paired, event, session="one"):
    """Runs one lifecycle checkpoint for claude under a session."""
    directory = Path(paired["lanes"]["claude"]).parent
    write_json(directory / "claude-identity.json", {"name": "claude"})
    payload = {
        "hook_event_name": event,
        "session_id": session,
        "cwd": paired["lanes"]["claude"],
        "tool_name": "Bash",
    }
    checkpoints.checkpoint(bridge.home, directory, "claude", payload)
    return directory


def settle(bridge, paired):
    """Applies the queued evidence the way one poll does."""
    directory = Path(paired["lanes"]["claude"]).parent
    supervision.settle_evidence(bridge.home, directory, paired)


def recorded(bridge, paired):
    """Reads claude's record and its transitions."""
    with store.connect(bridge.home) as db:
        return (
            lanes.read(db, paired["root"], "claude"),
            lanes.history(db, paired["root"], "claude", "transition"),
        )


def replay(directory, sequence):
    """Records a sequence of hook events the way the hook logs them."""
    for offset, (event, session) in enumerate(sequence):
        checkpoints.record(
            directory,
            "claude",
            {"hook_event_name": event, "session_id": session},
            checkpoints.Reason.OBSERVED,
            None,
        )
        items = (directory / lanes.SPOOL).read_text().splitlines()
        assert len(items) == offset + 1


@pytest.mark.parametrize(
    ("sequence", "state", "cause"),
    [
        (
            [
                ("SessionStart", "s1"),
                ("UserPromptSubmit", "s1"),
                ("PreToolUse", "s1"),
                ("PostToolUse", "s1"),
                ("Stop", "s1"),
            ],
            lanes.IDLE,
            "",
        ),
        (
            [
                ("SessionStart", "s1"),
                ("PreToolUse", "s1"),
                ("PermissionRequest", "s1"),
            ],
            lanes.BLOCKED,
            lanes.APPROVAL,
        ),
        (
            [
                ("PreToolUse", "s1"),
                ("PermissionRequest", "s1"),
                ("PostToolUse", "s1"),
            ],
            lanes.WORKING,
            "",
        ),
        (
            [("PreToolUse", "s1"), ("Stop", "s1"), ("SessionEnd", "s1")],
            lanes.STOPPED,
            "",
        ),
    ],
)
def test_a_replayed_hook_sequence_ends_in_its_state(
    bridge, paired, sequence, state, cause
):
    store.initialize(bridge.home)
    directory = Path(paired["lanes"]["claude"]).parent
    replay(directory, sequence)
    settle(bridge, paired)
    record, transitions = recorded(bridge, paired)
    assert (record["state"], record["cause"]) == (state, cause)
    assert record["session"] == "s1"
    assert transitions[0]["source"] == ""
    assert not (directory / lanes.SPOOL).exists()
    assert not (directory / lanes.DRAINING).exists()


def test_an_ignored_event_is_no_evidence(tmp_path):
    checkpoints.record(
        tmp_path,
        "claude",
        {"hook_event_name": "PreToolUse", "session_id": "s1"},
        checkpoints.Reason.SUPERSEDED,
        None,
    )
    checkpoints.record(
        tmp_path,
        "claude",
        {"hook_event_name": "Notification", "session_id": "s1"},
        checkpoints.Reason.OBSERVED,
        None,
    )
    assert lanes.pending(tmp_path) == []


def test_a_session_change_is_recorded_with_both_ids(bridge, repo, paired):
    store.initialize(bridge.home)
    hooked(bridge, paired, "PreToolUse", "one")
    hooked(bridge, paired, "SessionStart", "two")
    hooked(bridge, paired, "PreToolUse", "two")
    settle(bridge, paired)
    record, transitions = recorded(bridge, paired)
    assert record["state"] == lanes.WORKING
    assert record["session"] == "two"
    assert "session one -> two" in [item["detail"] for item in transitions]


def test_a_session_the_lane_did_not_adopt_never_moves_its_state(
    bridge, repo, paired
):
    store.initialize(bridge.home)
    hooked(bridge, paired, "PreToolUse", "one")
    hooked(bridge, paired, "Stop", "one")
    hooked(bridge, paired, "PreToolUse", "foreign")
    settle(bridge, paired)
    record, transitions = recorded(bridge, paired)
    assert record["state"] == lanes.IDLE
    assert record["session"] == "one"
    assert all("foreign" not in item["detail"] for item in transitions)


def test_an_adopted_session_reaches_the_record_with_both_ids(
    bridge, repo, paired
):
    store.initialize(bridge.home)
    directory = Path(paired["lanes"]["claude"]).parent
    write_json(directory / "claude-identity.json", {"name": "claude"})
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    gone = process.ServerProcess(child.pid, process.start_ticks(child.pid))
    child.kill()
    child.wait()
    live = process.ServerProcess(os.getpid(), process.start_ticks(os.getpid()))
    for session, native in (("one", gone), ("two", live)):
        payload = {
            "hook_event_name": "PreToolUse",
            "session_id": session,
            "cwd": paired["lanes"]["claude"],
            "tool_name": "Bash",
        }
        checkpoints.checkpoint(
            bridge.home, directory, "claude", payload, session_process=native
        )
    assert checkpoints.activity(directory, "claude")["session_id"] == "two"
    settle(bridge, paired)
    record, transitions = recorded(bridge, paired)
    assert record["state"] == lanes.WORKING
    assert record["session"] == "two"
    assert "session one -> two" in [item["detail"] for item in transitions]


def test_evidence_waits_in_order_while_the_store_is_busy(
    bridge, paired, monkeypatch
):
    store.initialize(bridge.home)
    directory = Path(paired["lanes"]["claude"]).parent
    replay(directory, [("PreToolUse", "s1"), ("PermissionRequest", "s1")])
    connect = store.connect

    def busy(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "connect", busy)
    with pytest.raises(sqlite3.OperationalError):
        settle(bridge, paired)
    assert (directory / lanes.DRAINING).exists()
    checkpoints.record(
        directory,
        "claude",
        {"hook_event_name": "Stop", "session_id": "s1"},
        checkpoints.Reason.OBSERVED,
        None,
    )
    monkeypatch.setattr(store, "connect", connect)
    settle(bridge, paired)
    record, transitions = recorded(bridge, paired)
    assert [item["target"] for item in transitions] == [
        lanes.WORKING,
        lanes.BLOCKED,
    ]
    assert record["state"] == lanes.BLOCKED
    settle(bridge, paired)
    record, transitions = recorded(bridge, paired)
    assert [item["target"] for item in transitions][-1] == lanes.IDLE
    assert record["state"] == lanes.IDLE
    assert lanes.pending(directory) == []


def test_a_hook_from_a_dead_lane_starts_it_before_it_works(tmp_path):
    home = tmp_path / "state"
    home.mkdir()
    store.initialize(home)
    with store.connect(home, write=True) as db:
        lanes.transition(db, "/r", "a", lanes.DEAD, now=10.0)
        lanes.apply(
            db,
            "/r",
            [
                {
                    "ts": 5.0,
                    "lane": "a",
                    "source": "PreToolUse",
                    "state": "working",
                }
            ],
        )
        record = lanes.read(db, "/r", "a")
        moves = [item["target"] for item in lanes.history(db, "/r", "a")]
    assert record["state"] == lanes.WORKING
    assert record["since"] == 10.0
    assert moves == [lanes.DEAD, lanes.STARTING, lanes.WORKING]


def test_malformed_evidence_is_skipped(tmp_path):
    (tmp_path / lanes.SPOOL).write_text(
        '{"lane": "a", "state": "blocked"}\nnot json\n[1]\n'
    )
    items = lanes.pending(tmp_path)
    home = tmp_path / "state"
    home.mkdir()
    store.initialize(home)
    with store.connect(home, write=True) as db:
        assert lanes.apply(db, "/r", items) == 0
        assert lanes.read(db, "/r", "a") is None
