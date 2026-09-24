"""Checks that a pending handoff offer wakes its named recipient."""

import json
import os
import time
import types
from pathlib import Path

import pytest

from agent_parley import issues, process, store, supervision, terminal
from agent_parley.state import write_json


@pytest.fixture
def offered(bridge, paired, monkeypatch):
    """Offers a claimed issue to an idle Codex lane holding no mail."""
    monkeypatch.setattr(
        supervision,
        "subprocess",
        types.SimpleNamespace(
            **{
                **vars(supervision.subprocess),
                "Popen": lambda *args, **kwargs: pytest.fail(
                    "started a native launcher"
                ),
            }
        ),
    )
    store.initialize(bridge.home)
    for name in ("claude", "codex"):
        store.register(bridge.home, paired["root"], name)
    lane = Path(paired["lanes"]["claude"])
    directory = lane.parent
    bridge.issue(lane, "claim", "1")
    bridge.issue(lane, "offer", "1", to="codex", summary="Take the parser")
    write_json(
        directory / "codex-activity.json",
        {
            "activity": "idle",
            "updated": time.time() - 500,
            "session_pid": os.getpid(),
            "session_ticks": process.start_ticks(os.getpid()),
        },
    )
    return directory


def woken(directory):
    """Reads the recorded wake decision for the Codex lane."""
    path = directory / "codex-wake.json"
    return json.loads(path.read_text()) if path.exists() else None


def requests(monkeypatch):
    """Captures terminal wake requests without touching a native session."""
    captured: list = []
    monkeypatch.setattr(
        terminal,
        "request",
        lambda directory, name: captured.append(name) or "wake requested",
    )
    return captured


def test_a_pending_offer_alone_wakes_its_recipient(
    bridge, offered, monkeypatch
):
    captured = requests(monkeypatch)
    supervision.poll(bridge.home, offered)
    assert captured == ["codex"]
    record = issues.snapshot(offered)["issues"]["1"]
    assert record["owner"] == "claude"
    assert record["offer"]["to"] == "codex"
    assert woken(offered)["backlog"] == [record["offer"]["id"]]


def test_a_cancelled_offer_stops_waking_the_recipient(
    bridge, paired, offered, monkeypatch
):
    bridge.issue(Path(paired["lanes"]["claude"]), "cancel", "1")
    monkeypatch.setattr(
        terminal,
        "request",
        lambda *args: pytest.fail("woke a cancelled offer"),
    )
    supervision.poll(bridge.home, offered)
    assert woken(offered) is None


def test_an_accepted_offer_wakes_the_recipient_for_owned_work(
    bridge, paired, offered, monkeypatch
):
    peer = Path(paired["lanes"]["codex"])
    record = issues.snapshot(offered)["issues"]["1"]
    accepted_offer = record["offer"]["id"]
    bridge.issue(peer, "accept", "1", offer_id=accepted_offer)
    captured = requests(monkeypatch)

    supervision.poll(bridge.home, offered)

    assert captured == ["codex"]
    wake = woken(offered)
    assert accepted_offer not in wake["backlog"]
    assert wake["backlog"][0].startswith("work:")
    assert issues.snapshot(offered)["issues"]["1"]["owner"] == "codex"
    assert supervision.published_work(offered, "codex")["offer"]["kind"] == (
        "continue"
    )


def test_a_repeated_poll_does_not_wake_the_recipient_again(
    bridge, offered, monkeypatch
):
    captured = requests(monkeypatch)
    supervision.poll(bridge.home, offered)
    supervision.poll(bridge.home, offered)
    assert captured == ["codex"]
    assert woken(offered)["attempts"] == 1


def test_a_lane_wake_opt_out_survives_a_pending_offer(
    bridge, paired, offered, monkeypatch
):
    manifest = json.loads((offered / "project.json").read_text())
    manifest["participants"]["codex"]["wake"] = False
    write_json(offered / "project.json", manifest)
    monkeypatch.setattr(
        terminal, "request", lambda *args: pytest.fail("woke an opted-out lane")
    )
    supervision.poll(bridge.home, offered)
    assert woken(offered) is None


def test_a_lane_waiting_for_approval_is_never_woken_by_an_offer(
    bridge, offered, monkeypatch
):
    write_json(
        offered / "codex-activity.json",
        {
            "activity": "waiting for approval: Bash",
            "updated": time.time() - 500,
            "session_pid": os.getpid(),
            "session_ticks": process.start_ticks(os.getpid()),
        },
    )
    monkeypatch.setattr(
        terminal, "request", lambda *args: pytest.fail("woke an approval")
    )
    supervision.poll(bridge.home, offered)
    assert woken(offered) is None
