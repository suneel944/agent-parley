"""Exercises event maintenance against real concurrent hook processes."""

import json
import multiprocessing
import time

from agent_parley import checkpoints


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
