"""Checks that lanes on a replaced plugin are told and restarted."""

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent_parley import (
    checkpoints,
    dashboard,
    plugins,
    process,
    protocol,
    records,
    store,
    supervision,
)
from agent_parley.state import write_json


@pytest.fixture
def lane(bridge, paired):
    """Registers both lanes and names the codex lane under test."""
    path = Path(paired["lanes"]["codex"])
    store.initialize(bridge.home)
    for name in ("claude", "codex"):
        store.register(bridge.home, paired["root"], name)
    write_json(path.parent / "codex-identity.json", {"name": "codex"})
    return {"lane": path, "directory": path.parent, "root": paired["root"]}


@pytest.fixture
def session():
    """Runs a process standing in for the lane's launcher."""
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"]
    )
    yield child
    child.kill()
    child.wait()


def recorded(lane, **fields):
    """Writes the codex lane's activity record."""
    write_json(lane["directory"] / "codex-activity.json", fields)


def activity(lane):
    """Reads the codex lane's activity record."""
    return json.loads((lane["directory"] / "codex-activity.json").read_text())


def hook(bridge, lane, event, **extra):
    """Runs one codex checkpoint decision and returns its output."""
    payload = {
        "hook_event_name": event,
        "cwd": str(lane["lane"]),
        "session_id": "s1",
        **extra,
    }
    return checkpoints.checkpoint(
        bridge.home, lane["directory"], "codex", payload
    )


def text(output):
    """Returns the context or block reason a hook output injects."""
    details = output.get("hookSpecificOutput") or {}
    return str(output.get("reason") or details.get("additionalContext", ""))


def test_a_lane_on_an_older_plugin_is_told_once_and_stop_blocks_once(
    bridge, lane
):
    recorded(lane, plugin_version="0.13.0")

    first = hook(bridge, lane, "Stop")

    assert first["decision"] == "block"
    assert "Plugin stale" in first["reason"]
    assert "plugin 0.13.0" in first["reason"]
    assert "agent-parley run codex --resume" in first["reason"]
    assert activity(lane)["plugin_notified"] == "0.13.0"
    assert hook(bridge, lane, "Stop") == {}
    later = hook(bridge, lane, "PreToolUse", tool_name="Read", tool_input={})
    assert "Plugin stale" not in text(later)
    [entry] = [
        record
        for record in bridge.status_snapshot()["projects"][0]["participants"]
        if record["participant"] == "codex"
    ]
    assert (entry["plugin_version"], entry["plugin_stale"]) == (
        "0.13.0",
        True,
    )
    [row] = [
        row
        for row in dashboard.collect(bridge.home, False, {})["projects"][0][
            "rows"
        ]
        if row["participant"] == "codex"
    ]
    assert "plugin 0.13.0 stale; agent-parley run codex --resume" in (
        dashboard.notes(row)
    )


def test_a_notice_the_hook_withheld_is_not_marked_delivered(
    bridge, lane, monkeypatch
):
    recorded(lane, plugin_version="0.13.0")
    monkeypatch.setattr(
        checkpoints, "continuation_refused", lambda *args: "run budget"
    )

    assert hook(bridge, lane, "Stop") == {}
    assert "plugin_notified" not in activity(lane)
    monkeypatch.undo()
    assert "Plugin stale" in hook(bridge, lane, "Stop")["reason"]
    assert activity(lane)["plugin_notified"] == "0.13.0"


def test_a_record_without_a_version_is_unknown_and_never_stale(bridge, lane):
    recorded(lane)

    assert "Plugin stale" not in text(hook(bridge, lane, "Stop"))
    [entry] = [
        record
        for record in bridge.status_snapshot()["projects"][0]["participants"]
        if record["participant"] == "codex"
    ]
    assert (entry["plugin_version"], entry["plugin_stale"]) == (
        "unknown",
        False,
    )
    current = protocol.launcher_version()
    assert protocol.lane_plugin({"plugin_version": current}) == (
        current,
        False,
    )


def idle(bridge, lane, child, attached):
    """Records a live idle codex session on a plugin the service refreshed."""
    recorded(
        lane,
        activity="idle",
        attached=attached,
        session_id="native-1",
        session_pid=child.pid,
        session_ticks=process.start_ticks(child.pid),
        plugin_version="0.13.0",
        updated=time.time(),
    )
    with store.connect(bridge.home, write=True) as db:
        supervision.lanes.transition(
            db,
            lane["root"],
            "codex",
            supervision.lanes.IDLE,
            session="native-1",
        )
    config = Path(records.DEFAULT_HOMES["codex"]).expanduser()
    write_json(
        bridge.home / plugins.REPAIR_RECORD,
        {
            f"codex:{config}": {
                "client": "codex",
                "directory": str(config),
                "launcher": protocol.launcher_version(),
                "state": plugins.REPAIRED,
                "at": time.time(),
            }
        },
    )


def restart(bridge, lane, monkeypatch):
    """Runs the stale plugin stage once and returns the launches it made."""
    launched = []

    class Child:
        pid = 4321

    monkeypatch.setattr(
        supervision.subprocess,
        "Popen",
        lambda command, **kwargs: launched.append(command) or Child(),
    )
    monkeypatch.setattr(supervision, "track_launcher", lambda child: None)
    manifest = supervision.roster.read(lane["directory"])
    manifest["participants"]["codex"]["approve_bridge_tools"] = True
    ended = supervision.stale_plugins(
        bridge.home, lane["directory"], manifest, supervision.settings({})
    )
    return ended, launched


def test_an_unattended_idle_lane_is_restarted_once_with_resume(
    bridge, lane, session, monkeypatch
):
    idle(bridge, lane, session, attached=False)

    ended, launched = restart(bridge, lane, monkeypatch)

    assert ended == ["codex"]
    assert session.wait(timeout=10) is not None
    [command] = launched
    assert command[command.index("run") + 1] == "codex"
    assert "--resume" in command
    assert "--task" not in command
    assert activity(lane)["plugin_restart"] == protocol.launcher_version()
    assert restart(bridge, lane, monkeypatch) == ([], [])


def test_an_attended_lane_is_left_alone(bridge, lane, session, monkeypatch):
    idle(bridge, lane, session, attached=True)

    assert restart(bridge, lane, monkeypatch) == ([], [])
    assert session.poll() is None


def test_a_lane_resume_could_not_bring_back_is_not_stopped(
    bridge, lane, session, monkeypatch
):
    idle(bridge, lane, session, attached=False)
    provider = supervision.roster.provider
    monkeypatch.setattr(
        supervision.roster,
        "provider",
        lambda home, name: {**provider(home, name), "require_env": ["TOKEN"]},
    )

    assert restart(bridge, lane, monkeypatch) == ([], [])
    monkeypatch.setattr(supervision.roster, "provider", provider)
    monkeypatch.setattr(supervision, "_fenced", lambda *args: True)
    assert restart(bridge, lane, monkeypatch) == ([], [])
    assert session.poll() is None
    assert "plugin_restart" not in activity(lane)


def test_a_session_replaced_after_the_read_is_not_stopped(
    bridge, lane, session, monkeypatch
):
    idle(bridge, lane, session, attached=False)
    held = supervision.lock

    def replaced(path, **kwargs):
        state = activity(lane)
        state["session_pid"] = state["session_pid"] + 1
        recorded(lane, **state)
        return held(path, **kwargs)

    monkeypatch.setattr(supervision, "lock", replaced)

    assert restart(bridge, lane, monkeypatch) == ([], [])
    assert session.poll() is None
    assert "plugin_restart" not in activity(lane)


def test_an_unattended_lane_waits_for_a_successful_repair(
    bridge, lane, session, monkeypatch
):
    idle(bridge, lane, session, attached=False)
    path = bridge.home / plugins.REPAIR_RECORD
    entries = json.loads(path.read_text())
    for entry in entries.values():
        entry["state"] = plugins.FAILED
    write_json(path, entries)

    assert restart(bridge, lane, monkeypatch) == ([], [])
    assert session.poll() is None
