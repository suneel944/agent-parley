"""Long waits handed to the service instead of held open by a lane."""

import contextlib
import json
import os
import threading
import time
from pathlib import Path

import httpx
import pytest

from agent_parley import (
    lanes,
    process,
    server,
    store,
    supervision,
    terminal,
    waits,
)
from agent_parley.state import BridgeError, write_json


def tokens(bridge, root):
    """Registers both paired lanes and returns their bearer tokens."""
    store.initialize(bridge.home)
    return {
        name: store.register(bridge.home, root, name)["registration_token"]
        for name in ("claude", "codex")
    }


@contextlib.contextmanager
def service(bridge):
    """Runs the real service in this process on an ephemeral port."""
    with server.Server(bridge.home, {"token": "health", "port": 0}) as running:
        thread = threading.Thread(target=running.serve_forever, daemon=True)
        thread.start()
        try:
            yield running
        finally:
            running.shutdown()
            thread.join(timeout=5)


def served(running, token, tool, arguments):
    """Calls one tool over the local transport and decodes its result."""
    response = httpx.post(
        f"http://127.0.0.1:{running.server_port}/mcp/",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        },
        timeout=30,
        trust_env=False,
    )
    result = response.json()["result"]
    assert "isError" not in result, result
    return json.loads(result["content"][0]["text"])


def handed(bridge, root, name):
    """Reads one lane's handed-off wait the way `status` would."""
    with store.connect(bridge.home) as db:
        return waits.pending(db, root, name)


def idle(bridge, paired, name):
    """Records a live lane quiet past the inactivity window."""
    directory = Path(paired["lanes"][name]).parent
    write_json(
        directory / f"{name}-activity.json",
        {
            "activity": "idle",
            "updated": time.time() - 500,
            "session_pid": os.getpid(),
            "session_ticks": process.start_ticks(os.getpid()),
        },
    )
    observed = supervision.presence(directory, name, 1)
    with store.connect(bridge.home, write=True) as db:
        lanes.sample(
            db,
            paired["root"],
            name,
            observed,
            dead_after=supervision.DEFAULTS["stalled_after"],
        )
    return directory, observed


def test_a_handed_off_wait_costs_one_turn_to_start_and_one_to_resume(
    bridge, paired, monkeypatch
):
    token = tokens(bridge, paired["root"])
    requests = []
    monkeypatch.setattr(
        terminal, "request", lambda *args: requests.append(args) or "accepted"
    )
    config = {**supervision.DEFAULTS, "inactive_after": 1}
    with service(bridge) as running:
        started = served(
            running, token["codex"], waits.TOOL, {"hand_off": "handoff"}
        )
        assert started["end_turn"] is True
        assert started["messages"] == []
        record = started["hand_off"]
        assert record["kind"] == "handoff"
        assert record["wake"] == waits.SERVICE
        assert record["woken_at"] is None
        again = served(
            running, token["codex"], waits.TOOL, {"hand_off": "handoff"}
        )
        assert again["hand_off"]["since"] == record["since"]
        directory, observed = idle(bridge, paired, "codex")
        for _ in range(15):
            supervision.wake(
                bridge.home, directory, paired, "codex", observed, config
            )
        assert requests == []
        assert handed(bridge, paired["root"], "codex")["woken_at"] is None
        served(
            running,
            token["claude"],
            "send_message",
            {
                "to": ["codex"],
                "subject": "Handoff",
                "body_md": "Issue #1 is yours now.",
                "idempotency_key": "handoff-1",
            },
        )
        for _ in range(3):
            supervision.wake(
                bridge.home, directory, paired, "codex", observed, config
            )
        assert len(requests) == 1
        woken = handed(bridge, paired["root"], "codex")
        assert woken["kind"] == "handoff" and woken["woken_at"]
        served(running, token["codex"], "fetch_inbox", {})
    assert handed(bridge, paired["root"], "codex") is None


def test_mail_already_waiting_is_returned_and_nothing_is_handed_off(
    bridge, paired
):
    token = tokens(bridge, paired["root"])
    with service(bridge) as running:
        sent = served(
            running,
            token["claude"],
            "send_message",
            {
                "to": ["codex"],
                "subject": "Checks",
                "body_md": "Green.",
                "idempotency_key": "early-1",
                "thread_id": "pr-7",
            },
        )
        page = served(
            running,
            token["codex"],
            waits.TOOL,
            {"hand_off": "check", "thread_id": "pr-7"},
        )
    assert [row["id"] for row in page["messages"]] == [sent["id"]]
    assert page["hand_off"] is None and page["end_turn"] is False
    assert handed(bridge, paired["root"], "codex") is None


def test_a_lane_the_service_cannot_wake_keeps_polling_and_is_marked(
    bridge, paired
):
    token = tokens(bridge, paired["root"])
    project = Path(paired["lanes"]["codex"]).parent / "project.json"
    manifest = json.loads(project.read_text())
    manifest["participants"]["codex"]["wake"] = False
    write_json(project, manifest)
    with service(bridge) as running:
        started = time.monotonic()
        page = served(
            running,
            token["codex"],
            waits.TOOL,
            {"hand_off": "mail", "thread_id": "ask", "timeout_seconds": 1},
        )
    assert 1 <= time.monotonic() - started < 10
    assert page["end_turn"] is False and page["expired"] is True
    record = handed(bridge, paired["root"], "codex")
    assert record["wake"] == waits.POLLING
    assert record["reason"] == "wake is off for this lane"
    assert record["thread_id"] == "ask"


def test_a_hand_off_must_name_what_it_waits_for(bridge, paired):
    store.initialize(bridge.home)
    actor = store.authenticate(
        bridge.home,
        store.register(bridge.home, paired["root"], "codex")[
            "registration_token"
        ],
    )
    with pytest.raises(BridgeError, match="hand_off must be one of"):
        waits.hand_off(bridge.home, actor, {"hand_off": "forever"})
    assert handed(bridge, paired["root"], "codex") is None
