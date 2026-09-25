"""Exercises per-claim progress while the lane holding the claims is live."""

import json
import os
import time
from pathlib import Path

import pytest

from agent_parley import issues, lifecycle, process, store, supervision
from agent_parley.state import BridgeError, write_json

WINDOW = 300
IDLE_AFTER = 3600


def registered(bridge, paired):
    """Registers both lanes so fitness checks can read their mail."""
    store.initialize(bridge.home)
    for name in ("claude", "codex"):
        store.register(bridge.home, paired["root"], name)


def live(directory, name, age):
    """Records a live session whose last native checkpoint is `age` old."""
    write_json(
        directory / f"{name}-activity.json",
        {
            "activity": "idle",
            "updated": time.time() - age,
            "session_id": f"{name}-session",
            "session_pid": os.getpid(),
            "session_ticks": process.start_ticks(os.getpid()),
        },
    )


def events(directory, name, *entries):
    """Appends native hook events, each an (event, seconds ago) pair."""
    with (directory / f"{name}-events.jsonl").open("a") as stream:
        for event, ago in entries:
            record = {"ts": time.time() - ago, "event": event}
            stream.write(json.dumps(record) + "\n")


def edit(directory, number, change):
    """Rewrites one ledger record in place, as elapsed time would."""
    ledger = issues.snapshot(directory)
    change(ledger["issues"][number])
    write_json(directory / "issues.json", ledger)


def aged(record: dict, seconds: float) -> None:
    """Moves every recorded transition of one claim `seconds` into the past."""
    for entry in record["history"]:
        entry["at"] = entry["at"] - seconds


def step(bridge, directory):
    """Runs the stuck-claim stage once against fresh presence readings."""
    manifest = json.loads((directory / "project.json").read_text())
    config = {
        **supervision.DEFAULTS,
        "inactive_after": WINDOW,
        "claim_idle_after": IDLE_AFTER,
    }
    observations = {
        name: supervision.presence(directory, name, WINDOW)
        for name in manifest["participants"]
    }
    supervision.overdue_claims(
        bridge.home, directory, manifest, config, observations
    )
    return issues.snapshot(directory)["issues"]


def busy_with_one_idle_claim(bridge, paired):
    """Leaves a live claude working #7 while #8 has had no progress."""
    registered(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    directory = lane.parent
    bridge.issue(lane, "claim", "7")
    bridge.issue(lane, "claim", "8")
    for number in ("7", "8"):
        edit(directory, number, lambda record: aged(record, 2 * IDLE_AFTER))
    lifecycle.record_report(
        directory, "claude", "partial", "", "tests remain", issue="7"
    )
    live(directory, "claude", 5)
    events(directory, "claude", ("PostToolUse", 5))
    live(directory, "codex", 5)
    events(directory, "codex", ("PostToolUse", 5))
    return directory


def test_an_idle_claim_of_a_live_lane_is_offered_and_its_peer_kept(
    bridge, paired
):
    directory = busy_with_one_idle_claim(bridge, paired)

    woken = step(bridge, directory)
    assert woken["8"]["overdue_recovery"]["step"] == "wake"
    assert woken["8"]["attempts"] == 1
    notice = woken["8"]["deadline_notice"]
    assert notice["holder"] == "claude"
    assert "has recorded no progress" in notice["text"]
    assert "overdue_recovery" not in woken["7"]

    edit(
        directory,
        "8",
        lambda record: record["overdue_recovery"].update(
            at=time.time() - WINDOW - 1
        ),
    )
    offered = step(bridge, directory)
    assert offered["8"]["owner"] == "claude"
    assert offered["8"]["offer"]["to"] == "codex"
    assert "has recorded no progress" in offered["8"]["offer"]["summary"]
    assert offered["8"]["history"][-1]["action"] == "overdue-offer"
    assert offered["7"]["owner"] == "claude"
    assert offered["7"]["offer"] is None
    assert "overdue_recovery" not in offered["7"]


def test_progress_on_the_idle_claim_drops_its_recovery(bridge, paired):
    directory = busy_with_one_idle_claim(bridge, paired)
    assert step(bridge, directory)["8"]["overdue_recovery"]["step"] == "wake"
    lifecycle.record_report(
        directory, "claude", "partial", "", "still going", issue="8"
    )
    kept = step(bridge, directory)["8"]
    assert kept["offer"] is None
    assert "overdue_recovery" not in kept
    assert "deadline_notice" not in kept


def test_the_listing_shows_each_claims_own_progress(bridge, paired):
    directory = busy_with_one_idle_claim(bridge, paired)
    text = issues.describe(issues.snapshot(directory))
    lines = text.splitlines()
    assert "last progress 0s ago" in lines[0] or "last progress 1s" in lines[0]
    assert f"last progress {2 * IDLE_AFTER}s ago" in lines[1]


def test_a_peer_takeover_request_is_granted_after_the_grace_window(
    bridge, paired
):
    directory = busy_with_one_idle_claim(bridge, paired)
    codex = Path(paired["lanes"]["codex"])
    requested = bridge.issue(
        codex, "request", "8", summary="claude is not working #8"
    )
    assert requested["request"]["to"] == "codex"
    assert issues.grant_requests(directory, WINDOW, {}) == []

    edit(
        directory,
        "8",
        lambda record: record["request"].update(
            created=time.time() - WINDOW - 1
        ),
    )
    assert issues.grant_requests(directory, WINDOW, {}) == ["8"]
    record = issues.snapshot(directory)["issues"]["8"]
    assert record["request"] is None
    assert record["offer"]["to"] == "codex"
    assert record["history"][-1]["action"] == "grant"

    moved = bridge.issue(codex, "accept", "8", offer_id=record["offer"]["id"])
    assert moved["owner"] == "codex"


def test_a_request_on_a_progressing_claim_waits_for_its_holder(bridge, paired):
    directory = busy_with_one_idle_claim(bridge, paired)
    codex = Path(paired["lanes"]["codex"])
    bridge.issue(codex, "request", "7")
    edit(
        directory,
        "7",
        lambda record: record["request"].update(
            created=time.time() - WINDOW - 1
        ),
    )
    lifecycle.record_report(
        directory, "claude", "partial", "", "tests remain", issue="7"
    )
    assert issues.grant_requests(directory, WINDOW, {}) == []
    assert issues.snapshot(directory)["issues"]["7"]["request"]


def test_a_claim_past_the_cap_is_refused_naming_the_held_claims(bridge, paired):
    directory = busy_with_one_idle_claim(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    with pytest.raises(BridgeError) as refused:
        bridge.issue(lane, "claim", "9")
    message = str(refused.value)
    assert "max_claims_per_lane is 2" in message
    assert f"#8 (no progress {2 * IDLE_AFTER}s)" in message
    assert "9" not in issues.snapshot(directory)["issues"]
    assert bridge.issue(lane, "claim", "8")["owner"] == "claude"


def test_a_project_may_raise_the_claim_cap(bridge, paired):
    registered(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    directory = lane.parent
    manifest = json.loads((directory / "project.json").read_text())
    manifest["supervision"] = {"max_claims_per_lane": 3}
    write_json(directory / "project.json", manifest)
    for number in ("7", "8", "9"):
        assert bridge.issue(lane, "claim", number)["owner"] == "claude"
    with pytest.raises(BridgeError, match="max_claims_per_lane is 3"):
        bridge.issue(lane, "claim", "10")


def blocking_an_idle_claim(bridge, paired, monkeypatch):
    """Parks codex's #9 on claude's idle #8 and records every notice sent."""
    directory = busy_with_one_idle_claim(bridge, paired)
    codex = Path(paired["lanes"]["codex"])
    bridge.issue(codex, "claim", "9")
    bridge.issue(codex, "block", "9", on="8")
    sent = []
    monkeypatch.setattr(
        supervision.notify,
        "deliver",
        lambda directory, agent, event, fields: sent.append(
            (agent, event, dict(fields))
        ),
    )
    return directory, sent


def test_a_blocking_idle_claim_notifies_the_operator_once(
    bridge, paired, monkeypatch
):
    directory, sent = blocking_an_idle_claim(bridge, paired, monkeypatch)
    for _ in range(3):
        record = step(bridge, directory)["8"]
    assert record["idle_blocker"]["waiting"] == ["9"]
    assert len(sent) == 1
    agent, event, fields = sent[0]
    assert (agent, event) == ("claude", supervision.notify.Event.IDLE_BLOCKER)
    assert fields["issue"] == "8"
    assert fields["claim"] == record["claim_id"]
    assert fields["detail"].endswith("; #9 waiting on it")

    lifecycle.record_report(
        directory, "claude", "partial", "", "still going", issue="8"
    )
    assert step(bridge, directory)["8"]["idle_blocker"]["waiting"] == []
    edit(
        directory,
        "8",
        lambda record: record["execution"]["progress"].update(
            at=time.time() - 2 * IDLE_AFTER
        ),
    )
    assert step(bridge, directory)["8"]["idle_blocker"]["waiting"] == ["9"]
    assert len(sent) == 1


def test_status_shows_a_row_for_a_blocking_claim_without_progress(
    bridge, paired, monkeypatch, capsys
):
    directory, _ = blocking_an_idle_claim(bridge, paired, monkeypatch)
    bridge.status()
    assert "Blocking claim" not in capsys.readouterr().out

    step(bridge, directory)
    bridge.status()
    rows = [
        line
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("Blocking claim")
    ]
    assert len(rows) == 1
    assert rows[0].startswith("Blocking claim #8 (claude): no progress for ")
    assert rows[0].endswith("s; #9 waiting on it")
    assert int(rows[0].split("for ")[1].split("s;")[0]) >= 2 * IDLE_AFTER

    lifecycle.record_report(
        directory, "claude", "partial", "", "still going", issue="8"
    )
    step(bridge, directory)
    bridge.status()
    assert "Blocking claim" not in capsys.readouterr().out
