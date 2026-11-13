"""Checks that a parked lane resumes once its cause clears."""

import os
import time
from pathlib import Path

from agent_parley import denials, dialogs, lanes, process, supervision
from agent_parley.state import write_json

REFUSAL = {
    "hook_event_name": "PermissionDenied",
    "session_id": "s1",
    "tool_name": "Bash",
    "tool_input": {"command": "git push --force origin main"},
    "reason": "denied. Reason: [CI Bypass]",
}
CAPACITY_BLOCK = {"state": lanes.BLOCKED, "cause": lanes.CAPACITY}


def exhausted(directory: Path, reset_at: float | None) -> None:
    """Records a usage-limit exhaustion as the dialog watcher does."""
    supervision.record_capacity(
        directory,
        "codex",
        {
            "state": "exhausted",
            "observed_at": time.time() - 3600,
            "reset_at": reset_at,
            "source": "native-dialog",
            "session_id": "s1",
            "observation_id": f"dialog:{reset_at}",
        },
    )


def test_a_capacity_block_defers_wakes_only_until_the_reset(tmp_path):
    observed = {"record": CAPACITY_BLOCK, "process_alive": True}
    exhausted(tmp_path, time.time() + 600)

    blocked, due = supervision._wake_block(tmp_path, "codex", observed, {}, 60)

    assert blocked.startswith("its provider capacity is exhausted")
    assert due > time.time()
    exhausted(tmp_path, time.time() - 1)
    assert supervision._wake_block(tmp_path, "codex", observed, {}, 60) == (
        "",
        0.0,
    )


def test_a_capacity_block_stops_holding_claims_after_the_reset(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        supervision, "condition", lambda *args: dict(CAPACITY_BLOCK)
    )
    exhausted(tmp_path, time.time() + 600)

    assert supervision.holder_held(tmp_path, tmp_path, "/repo", "codex") == (
        f"blocked: {lanes.CAPACITY}"
    )
    exhausted(tmp_path, None)
    assert supervision.holder_held(tmp_path, tmp_path, "/repo", "codex")
    exhausted(tmp_path, time.time() - 1)
    assert supervision.holder_held(tmp_path, tmp_path, "/repo", "codex") == ""


def test_other_block_causes_still_defer_wakes(tmp_path):
    observed = {
        "record": {"state": lanes.BLOCKED, "cause": "dialog"},
        "process_alive": True,
    }

    assert supervision._wake_block(tmp_path, "codex", observed, {}, 60) == (
        "blocked: dialog",
        0.0,
    )


def test_a_denial_stops_holding_once_a_later_tool_call_completes(tmp_path):
    found = denials.detect(REFUSAL)
    opened = denials.record(tmp_path, "/repo", "codex", found)
    activity = tmp_path / "codex-activity.json"

    assert denials.waiting(tmp_path, "codex")["id"] == opened["id"]
    write_json(activity, {"progressed": opened["refreshed"] - 1})
    assert denials.waiting(tmp_path, "codex")["id"] == opened["id"]
    write_json(activity, {"progressed": opened["refreshed"] + 1})
    assert denials.waiting(tmp_path, "codex") == {}
    observed = {"record": {}, "process_alive": True}
    assert supervision._wake_block(tmp_path, "codex", observed, {}, 60) == (
        "",
        0.0,
    )
    again = denials.record(
        tmp_path, "/repo", "codex", found, now=opened["refreshed"] + 2
    )
    assert denials.waiting(tmp_path, "codex")["id"] == again["id"]


def test_a_lane_parked_on_an_approval_is_not_reported_active(tmp_path):
    write_json(
        tmp_path / "codex-activity.json",
        {
            "activity": f"{dialogs.APPROVAL}: Bash",
            "event": "PermissionRequest",
            "updated": time.time(),
            "session_pid": os.getpid(),
            "session_ticks": process.start_ticks(os.getpid()),
        },
    )

    reading = supervision.presence(tmp_path, "codex")

    assert reading["activity"] == supervision.WAITING
    assert reading["state"] == supervision.WAITING
    assert reading["state"] not in supervision.TAKES_TURNS
