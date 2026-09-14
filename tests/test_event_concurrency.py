"""Exercises event maintenance against real concurrent hook processes."""

import json
import multiprocessing
import time

import pytest

from agent_parley import checkpoints
from agent_parley.state import BridgeError


def rotate_log(directory, ready, go):
    ready.set()
    assert go.wait(10)
    with checkpoints.event_lock(directory, "lane", exclusive=True):
        (directory / "lane-events.jsonl").replace(
            directory / "lane-events.1.jsonl"
        )


def append_events(directory, ready, go, count):
    ready.set()
    assert go.wait(10)
    for number in range(count):
        checkpoints.record(
            directory,
            "lane",
            {"hook_event_name": "PreToolUse", "tool_name": str(number)},
            checkpoints.Reason.OBSERVED,
            None,
        )


def test_concurrent_rotation_preserves_retained_generation(tmp_path):
    context = multiprocessing.get_context("spawn")
    path = tmp_path / "lane-events.jsonl"
    retained = json.dumps({"ts": time.time(), "padding": "x" * 262144})
    path.write_text(retained + "\n")
    go = context.Event()
    ready = [context.Event() for _ in range(6)]
    children = [
        context.Process(target=append_events, args=(tmp_path, event, go, 20))
        for event in ready
    ]
    try:
        for child in children:
            child.start()
        assert all(event.wait(10) for event in ready)
        go.set()
        for child in children:
            child.join(10)
            assert child.exitcode == 0
        events = checkpoints.read_events(tmp_path, "lane")
        assert len(events) == 121
        assert events[0]["padding"] == "x" * 262144
    finally:
        for child in children:
            if child.is_alive():
                child.kill()
            child.join(10)


def test_prune_excludes_append_until_atomic_replacement(tmp_path):
    context = multiprocessing.get_context("spawn")
    path = tmp_path / "lane-events.jsonl"
    path.write_text(json.dumps({"ts": 1}) + "\n")
    ready, go = context.Event(), context.Event()
    child = context.Process(target=append_events, args=(tmp_path, ready, go, 1))
    try:
        with checkpoints.event_lock(tmp_path, "lane", exclusive=True):
            child.start()
            assert ready.wait(10)
            go.set()
            child.join(0.2)
            assert child.is_alive()
            temporary = path.with_suffix(".tmp")
            temporary.write_text("")
            temporary.replace(path)
        child.join(10)
        assert child.exitcode == 0
        assert len(checkpoints.read_events(tmp_path, "lane")) == 1
    finally:
        if child.is_alive():
            child.kill()
        child.join(10)


def test_a_rotation_cannot_split_an_event_snapshot(tmp_path):
    context = multiprocessing.get_context("spawn")
    (tmp_path / "lane-events.1.jsonl").write_text(json.dumps({"ts": 1}) + "\n")
    (tmp_path / "lane-events.jsonl").write_text(json.dumps({"ts": 2}) + "\n")
    ready, go = context.Event(), context.Event()
    child = context.Process(target=rotate_log, args=(tmp_path, ready, go))
    try:
        with checkpoints.event_lock(tmp_path, "lane"):
            child.start()
            assert ready.wait(10)
            go.set()
            child.join(0.2)
            assert child.is_alive()
            snapshot = checkpoints.read_events(tmp_path, "lane")
        child.join(10)
        assert child.exitcode == 0
        assert [entry["ts"] for entry in snapshot] == [1, 2]
    finally:
        if child.is_alive():
            child.kill()
        child.join(10)


def test_a_held_maintenance_lock_reports_the_log_unavailable(tmp_path):
    (tmp_path / "lane-events.jsonl").write_text(json.dumps({"ts": 1}) + "\n")
    with checkpoints.event_lock(tmp_path, "lane", exclusive=True):
        with pytest.raises(BridgeError, match="stayed locked"):
            checkpoints.read_events(tmp_path, "lane", timeout=0.05)
        summary = checkpoints.event_summary(tmp_path, "lane")
    assert summary == {
        "events": 0,
        "denials": 0,
        "injected_bytes": 0,
        "last_ts": 0.0,
        "last_reason": "unavailable",
    }
    assert checkpoints.read_events(tmp_path, "lane") == [{"ts": 1}]


def test_a_lane_without_a_log_reports_no_events_and_writes_nothing(tmp_path):
    assert checkpoints.read_events(tmp_path / "absent", "lane") == []
    assert checkpoints.read_events(tmp_path, "lane") == []
    assert list(tmp_path.iterdir()) == []


def test_prune_removes_orphans_and_malformed_events(tmp_path):
    path = tmp_path / "lane-events.jsonl"
    path.write_text(
        "\n".join(["[]", '{"ts": "bad"}', '{"ts": null}', '{"ts": 2000000}'])
        + "\n"
    )
    temporary = tmp_path / "lane-events.jsonl.tmp"
    rotated_temporary = tmp_path / "lane-events.1.jsonl.tmp"
    temporary.write_text("interrupted prune")
    rotated_temporary.write_text("orphan without original file")
    assert checkpoints.prune(tmp_path, "lane", now=2000001) == 3
    assert checkpoints.read_events(tmp_path, "lane") == [{"ts": 2000000}]
    assert not temporary.exists()
    assert not rotated_temporary.exists()
