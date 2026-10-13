"""Checks the watchdog for a headless lane held by a native prompt."""

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent_parley import (
    decisions,
    dialogs,
    issues,
    notify,
    process,
    store,
    supervision,
    timeouts,
)
from agent_parley.state import write_json

COMMAND = "P=python3; cd lane && $P -m agent_parley.cli issue list"
PROMPT = (
    "\x1b[2J ╭──────────╮\r\n│ Bash command │\r\n│   "
    f"{COMMAND}\r\n│   Contains simple_expansion\r\n│\r\n"
    "│ Do you want to proceed?\r\n│ ❯ 1. Yes\r\n"
    "│   2. Yes, and switch to auto mode\r\n│   3. No\r\n"
    "Esc to cancel · Tab to amend"
)


@pytest.fixture
def quiet(monkeypatch):
    """Keeps dialog notifications off any transport."""
    monkeypatch.setattr(notify, "deliver", lambda *args, **kwargs: None)


@pytest.fixture
def session():
    """Runs a process standing in for the lane's headless launcher."""
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"]
    )
    yield child
    child.kill()
    child.wait()


def actors(bridge, paired):
    """Registers both lanes and resolves their store identities."""
    store.initialize(bridge.home)
    return {
        name: store.authenticate(
            bridge.home,
            store.register(bridge.home, paired["root"], name)[
                "registration_token"
            ],
        )
        for name in ("claude", "codex")
    }


def held(directory, child, attached=False):
    """Records a launched lane and drives its watcher onto the prompt."""
    write_json(
        directory / "claude-activity.json",
        {
            "activity": "working",
            "attached": attached,
            "session_id": "native-1",
            "session_pid": child.pid,
            "session_ticks": process.start_ticks(child.pid),
            "updated": time.time(),
        },
    )
    watch = dialogs.Watch(directory, "claude")
    watch.advance(PROMPT.encode(), 0.0)
    watch.advance(b"", 0.5)
    return watch


def elapse(directory, seconds):
    """Moves the published prompt back in time, as waiting would."""
    path = directory / "claude-activity.json"
    state = json.loads(path.read_text())
    state["dialog"]["at"] -= seconds
    write_json(path, state)


def sweep(bridge, directory):
    """Runs the headless prompt stage once with default settings."""
    return supervision.headless_prompts(
        bridge.home,
        directory,
        supervision.roster.read(directory),
        supervision.settings({}),
    )


def inbox(bridge, lane):
    """Reads one lane's own mail with bodies."""
    return store.call(
        bridge.home, lane, "fetch_inbox", {"include_bodies": True}
    )["messages"]


def test_a_headless_prompt_raises_a_decision_carrying_its_command(
    bridge, repo, paired, quiet, session
):
    directory = Path(paired["lanes"]["claude"]).parent
    held(directory, session)

    [record] = decisions.list_open(directory)

    assert record["kind"] == dialogs.DECISION_KIND
    assert record["lane"] == "claude"
    assert "simple_expansion" in record["detail"]
    assert "agent_parley.cli issue list" in record["detail"]
    assert sweep(bridge, directory) == []
    assert session.poll() is None


def test_a_headless_prompt_past_the_deadline_ends_and_returns_the_lane(
    bridge, repo, paired, quiet, session
):
    lane = actors(bridge, paired)
    directory = Path(paired["lanes"]["claude"]).parent
    bridge.issue(Path(paired["lanes"]["claude"]), "claim", "7")
    store.call(
        bridge.home,
        lane["codex"],
        "send_message",
        {
            "to": ["claude"],
            "subject": "Interface change",
            "body_md": "Response now includes session_id.",
            "idempotency_key": "headless-1",
        },
    )
    held(directory, session)
    [decision] = decisions.list_open(directory)
    elapse(directory, timeouts.TIMEOUT_SECONDS + 1)

    assert sweep(bridge, directory) == ["claude"]

    assert session.wait(timeout=10) is not None
    state = json.loads((directory / "claude-activity.json").read_text())
    assert state["activity"] == supervision.STOPPED
    assert "dialog" not in state
    assert not state.get("operator_stopped")
    result = supervision.wake_record(
        bridge.home, paired["root"], "claude", directory
    )["result"]
    assert result.startswith("ended: claude was resumed without a terminal")
    assert "agent_parley.cli issue list" in result
    assert decisions.get(directory, decision["id"])["state"] == decisions.STALE
    assert issues.snapshot(directory)["issues"]["7"]["owner"] is None
    mail = {
        message["subject"]: message["body_md"]
        for message in inbox(bridge, lane["codex"])
    }
    assert "Interface change" in mail["Mail returned: claude ended at a prompt"]
    notice = mail["claude ended at a prompt; its work returned"]
    assert "Claims released to the pool: #7." in notice
    assert store.waiting(bridge.home, paired["root"], "claude")["kind"] is None


def unthrottle(home, directory, root, name):
    """Ages a lane's recorded wake spacing so the next poll is admitted."""
    record = supervision.wake_record(home, root, name)
    supervision.store_wake(home, directory, root, name, {**record, "at": 0})


def test_a_wake_after_a_headless_stop_names_its_command(
    bridge, repo, paired, quiet, session, monkeypatch
):
    lane = actors(bridge, paired)
    directory = Path(paired["lanes"]["claude"]).parent
    with store.connect(bridge.home, write=True) as db:
        supervision.lanes.transition(
            db,
            paired["root"],
            "claude",
            supervision.lanes.WORKING,
            session="native-1",
        )
    held(directory, session)
    elapse(directory, timeouts.TIMEOUT_SECONDS + 1)

    assert sweep(bridge, directory) == ["claude"]

    store.call(
        bridge.home,
        lane["codex"],
        "send_message",
        {
            "to": ["claude"],
            "subject": "New work",
            "body_md": "Please pick this up.",
            "idempotency_key": "headless-note-1",
        },
    )
    manifest = supervision.roster.read(directory)
    manifest["participants"]["claude"]["approve_bridge_tools"] = True
    config = supervision.settings({})
    observed = supervision.presence(
        directory, "claude", config["inactive_after"]
    )
    assert observed["process_alive"] is False

    launched = []

    class Child:
        pid = 4321

    monkeypatch.setattr(
        supervision.subprocess,
        "Popen",
        lambda command, **kwargs: launched.append(command) or Child(),
    )
    monkeypatch.setattr(supervision, "track_launcher", lambda child: None)

    supervision.wake(
        bridge.home, directory, manifest, "claude", observed, config
    )

    assert launched
    first = launched[0][launched[0].index("--task") + 1]
    assert COMMAND in first
    assert "no rule granted" in first

    launched.clear()
    unthrottle(bridge.home, directory, paired["root"], "claude")

    supervision.wake(
        bridge.home, directory, manifest, "claude", observed, config
    )

    assert launched
    second = launched[0][launched[0].index("--task") + 1]
    assert COMMAND not in second


def test_a_prompt_in_a_terminal_is_left_for_its_operator(
    bridge, repo, paired, quiet, session
):
    directory = Path(paired["lanes"]["claude"]).parent
    held(directory, session, attached=True)
    elapse(directory, timeouts.TIMEOUT_SECONDS + 1)

    assert sweep(bridge, directory) == []
    assert session.poll() is None
    assert decisions.list_open(directory)
