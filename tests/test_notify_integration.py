"""Checks that a real handoff offer notifies the lane it names."""

import asyncio
import json
import threading
from pathlib import Path

import pytest

from agent_parley import checkpoints, notify, server, store


@pytest.fixture
def service(bridge):
    """Serves the bridge's configured loopback port on a thread."""
    with server.Server(bridge.home, bridge.config) as instance:
        thread = threading.Thread(target=instance.serve_forever, daemon=True)
        thread.start()
        try:
            yield instance
        finally:
            instance.shutdown()
            thread.join(timeout=2)


@pytest.fixture
def registered(bridge, paired):
    """Writes both lanes' identities, as the launcher does at a start."""
    store.initialize(bridge.home)
    for name in ("claude", "codex"):
        asyncio.run(bridge.identity(name, paired))
    return paired


@pytest.fixture
def fake(monkeypatch):
    """Registers a recording transport and selects it in the environment."""
    sent: list[tuple[str, str]] = []
    monkeypatch.setitem(
        notify.TRANSPORTS,
        "fake",
        lambda config, subject, body: sent.append((subject, body)),
    )
    monkeypatch.setenv("AGENT_PARLEY_NOTIFY", "fake")
    return sent


def checkpoint(bridge, directory, lane, payload):
    """Asks the service's own handler for one checkpoint decision."""
    return checkpoints.serve(
        bridge.home,
        {
            "directory": str(directory),
            "participant": "codex",
            "payload": {**payload, "cwd": str(lane), "session_id": "s1"},
        },
    )


def test_a_real_offer_notifies_the_lane_it_names(
    bridge, repo, registered, service, fake
):
    lane = Path(registered["lanes"]["claude"])
    directory = lane.parent
    bridge.issue(lane, "claim", "1")
    bridge.issue(lane, "offer", "1", to="codex", summary="Take the parser")
    answer = checkpoint(
        bridge,
        directory,
        Path(registered["lanes"]["codex"]),
        {"hook_event_name": "SessionStart"},
    )
    assert answer["status"] == 0
    notify.drain()
    assert len(fake) == 1
    subject, body = fake[0]
    assert subject == "Agent Parley: A handoff offer is waiting (codex)"
    assert "lane: codex" in body
    assert "issue: 1" in body
    record = json.loads((directory / "issues.json").read_text())
    assert f"offer: {record['issues']['1']['offer']['id']}" in body
    assert len(body.encode()) <= notify.MAX_MESSAGE_BYTES


def test_an_unchanged_offer_notifies_no_further(
    bridge, repo, registered, service, fake
):
    lane = Path(registered["lanes"]["claude"])
    directory = lane.parent
    bridge.issue(lane, "claim", "1")
    bridge.issue(lane, "offer", "1", to="codex", summary="Take the parser")
    peer = Path(registered["lanes"]["codex"])
    for event in ("SessionStart", "UserPromptSubmit", "PreToolUse"):
        checkpoint(bridge, directory, peer, {"hook_event_name": event})
    notify.drain()
    assert len(fake) == 1


def test_no_offer_for_this_lane_notifies_nothing(
    bridge, repo, registered, service, fake
):
    lane = Path(registered["lanes"]["claude"])
    bridge.issue(lane, "claim", "1")
    checkpoint(
        bridge,
        lane.parent,
        Path(registered["lanes"]["codex"]),
        {"hook_event_name": "SessionStart"},
    )
    notify.drain()
    assert fake == []
