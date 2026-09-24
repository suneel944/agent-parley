"""Checks the retirement a lane performs on itself through its tool set."""

import json
import os
import time
from pathlib import Path

import pytest

from agent_parley import (
    cli,
    issues,
    problems,
    retirement,
    roster,
    store,
    supervision,
    tables,
    terminal,
)
from agent_parley.process import start_ticks
from agent_parley.state import BridgeError, write_json


@pytest.fixture(autouse=True)
def quiet_forge(monkeypatch):
    """Keeps the poll off the host forge while these tests run."""
    monkeypatch.setattr(
        supervision.forge, "branch_completion", lambda *args: None
    )


def actor(bridge, root, name):
    """Registers one lane and resolves its own store identity."""
    store.initialize(bridge.home)
    token = store.register(bridge.home, root, name)["registration_token"]
    resolved = store.authenticate(bridge.home, token)
    assert resolved is not None
    return resolved


def alive(directory, name, **extra):
    """Publishes a live, quiet session for one lane."""
    write_json(
        directory / f"{name}-activity.json",
        {
            "session_pid": os.getpid(),
            "session_ticks": start_ticks(os.getpid()),
            "activity": "idle",
            "updated": time.time(),
            **extra,
        },
    )


def retire(bridge, lane):
    """Retires one lane through the served tool path."""
    return store.call(bridge.home, lane, store.RETIRE, {})


def entry(directory, name):
    """Reads one participant entry from the project manifest."""
    return roster.read(directory)["participants"][name]


def test_a_self_retire_releases_its_claims_and_reservations(
    bridge, repo, paired
):
    lane = actor(bridge, paired["root"], "claude")
    peer = actor(bridge, paired["root"], "codex")
    directory = bridge.project(repo)[1]
    bridge.issue(paired["lanes"]["claude"], "claim", "4")
    store.call(
        bridge.home,
        lane,
        "file_reservation_paths",
        {"paths": ["src/engine.py"]},
    )
    queued = store.call(
        bridge.home, peer, "request_reservation", {"paths": ["src/engine.py"]}
    )
    assert queued["granted"] == []

    result = retire(bridge, lane)

    assert result["released"] == ["4"]
    assert result["reservations_released"] == 1
    assert [grant["agent"] for grant in result["granted"]] == ["codex"]
    assert result["worktree"] == retirement.PRUNED
    assert issues.snapshot(directory)["issues"]["4"]["owner"] is None
    assert store.active_reservations(bridge.home, paired["root"]) == {
        "codex": ["src/engine.py"]
    }
    assert entry(directory, "claude")["retired"] == result["retired_at"]
    assert result["credentials_invalidated"] == 1


def test_a_retired_lane_can_no_longer_authenticate(bridge, repo, paired):
    store.initialize(bridge.home)
    token = store.register(bridge.home, paired["root"], "claude")[
        "registration_token"
    ]
    lane = store.authenticate(bridge.home, token)
    assert lane is not None
    retire(bridge, lane)
    assert store.authenticate(bridge.home, token) is None


def test_a_dirty_worktree_is_kept_and_its_changes_reported(
    bridge, repo, paired
):
    lane = actor(bridge, paired["root"], "claude")
    worktree = Path(paired["lanes"]["claude"])
    (worktree / "shared.txt").write_text("lane work in progress\n")
    (worktree / "notes.md").write_text("unfinished\n")

    result = retire(bridge, lane)

    assert result["worktree"] == retirement.KEPT
    assert result["dirty"] == ["notes.md", "shared.txt"]
    assert worktree.exists()


def test_a_handed_off_issue_returns_to_the_pool_and_tells_its_sender(
    bridge, repo, paired
):
    lane = actor(bridge, paired["root"], "claude")
    peer = actor(bridge, paired["root"], "codex")
    directory = bridge.project(repo)[1]
    bridge.issue(paired["lanes"]["codex"], "claim", "7")
    offered = bridge.issue(
        paired["lanes"]["codex"], "offer", "7", to="claude", summary="Parser"
    )
    bridge.issue(
        paired["lanes"]["claude"],
        "accept",
        "7",
        offer_id=offered["offer"]["id"],
    )

    result = retire(bridge, lane)

    assert result["released"] == ["7"]
    assert [notice["agent"] for notice in result["notices"]] == ["codex"]
    assert result["notices"][0]["issues"] == ["7"]
    assert issues.snapshot(directory)["issues"]["7"]["owner"] is None
    delivered = store.call(
        bridge.home, peer, "fetch_inbox", {"include_bodies": True}
    )["messages"]
    assert "#7" in delivered[0]["subject"]
    assert "unclaimed again" in delivered[0]["body_md"]


def test_an_offer_awaiting_a_retiring_lane_returns_to_its_owner(
    bridge, repo, paired
):
    lane = actor(bridge, paired["root"], "claude")
    actor(bridge, paired["root"], "codex")
    directory = bridge.project(repo)[1]
    bridge.issue(paired["lanes"]["codex"], "claim", "8")
    bridge.issue(
        paired["lanes"]["codex"], "offer", "8", to="claude", summary="Parser"
    )

    result = retire(bridge, lane)

    assert result["declined"] == ["8"]
    record = issues.snapshot(directory)["issues"]["8"]
    assert record["owner"] == "codex"
    assert record["offer"] is None


def test_a_retired_lane_is_never_woken_and_never_named_for_a_share(
    bridge, repo, paired, monkeypatch
):
    lane = actor(bridge, paired["root"], "claude")
    directory = bridge.project(repo)[1]
    alive(directory, "claude", updated=time.time() - 500)
    bridge.issue(paired["lanes"]["codex"], "claim", "2")
    bridge.issue(paired["lanes"]["codex"], "claim", "3")
    retire(bridge, lane)
    monkeypatch.setattr(
        terminal, "request", lambda *args: pytest.fail("woke a retired lane")
    )

    supervision.poll(bridge.home, directory)

    assert supervision.published_work(directory, "claude")["offer"] is None
    assert not (directory / "claude-wake.json").exists()
    published = supervision.published_work(directory, "codex")
    assert "claude" not in json.dumps(published)


def test_status_reports_the_retirement_and_its_time(
    bridge, repo, paired, monkeypatch, capsys
):
    lane = actor(bridge, paired["root"], "claude")
    retire(bridge, lane)
    monkeypatch.setattr(
        "sys.argv",
        ["agent-parley", "--home", str(bridge.home), "status", "--json"],
    )

    assert cli.main() == 0

    document = json.loads(capsys.readouterr().out)
    row = next(
        record
        for record in document["projects"][0]["participants"]
        if record["participant"] == "claude"
    )
    assert row["retired_at"].endswith("Z")
    assert tables.status_row(row, ())[3].startswith("retired ")


def test_a_retired_lane_reports_only_the_worktree_it_kept(bridge, repo, paired):
    lane = actor(bridge, paired["root"], "claude")
    worktree = Path(paired["lanes"]["claude"])
    (worktree / "draft.txt").write_text("unfinished\n")
    retire(bridge, lane)

    found = [row for row in bridge.problems() if row["participant"] == "claude"]

    assert [row["condition"] for row in found] == [problems.DIRTY]
    assert found[0]["command"] == (
        f"agent-parley participant add claude --repo {paired['root']}"
    )


def test_re_admission_restores_a_retired_lane(
    bridge, repo, paired, monkeypatch
):
    lane = actor(bridge, paired["root"], "claude")
    directory = bridge.project(repo)[1]
    retire(bridge, lane)
    assert not Path(paired["lanes"]["claude"]).exists()

    data = bridge.add_participant(repo, "claude", "claude")

    assert "retired" not in entry(directory, "claude")
    assert Path(data["participants"]["claude"]["lane"]).exists()
    alive(directory, "claude", updated=time.time() - 500)
    bridge.issue(paired["lanes"]["codex"], "claim", "2")
    bridge.issue(paired["lanes"]["codex"], "claim", "3")
    requested = []
    monkeypatch.setattr(
        terminal,
        "request",
        lambda path, name: requested.append(name) or "accepted",
    )

    supervision.poll(bridge.home, directory)

    assert requested == ["claude"]
    assert supervision.published_work(directory, "claude")["offer"]


def test_a_repeated_withdrawal_reports_the_recorded_time(bridge, repo, paired):
    actor(bridge, paired["root"], "claude")
    directory = bridge.project(repo)[1]
    first = retirement.withdraw(directory, "claude")

    repeated = retirement.withdraw(directory, "claude")

    assert repeated["retired_at"] == first["retired_at"]
    assert repeated["released"] == []
    assert entry(directory, "claude")["retired"] == first["retired_at"]


def test_a_retirement_time_must_be_a_time(bridge, repo, paired):
    directory = bridge.project(repo)[1]
    manifest = roster.read(directory)
    manifest["participants"]["claude"]["retired"] = True
    write_json(directory / "project.json", manifest)
    with pytest.raises(BridgeError, match="recorded as a time"):
        roster.read(directory)


def test_a_lane_holding_ready_work_is_refused_before_releasing_any(
    bridge, repo, paired
):
    directory = bridge.project(repo)[1]
    lane = paired["lanes"]["claude"]
    bridge.issue(lane, "claim", "4")
    bridge.issue(lane, "claim", "5")
    bridge.report(lane, "ready", "Done.", "", "make check", issue="5")
    for _ in range(2):
        with pytest.raises(BridgeError, match=r"ready work \(#5\)"):
            retirement.withdraw(directory, "claude")
    ledger = issues.snapshot(directory)["issues"]
    assert [ledger[number]["owner"] for number in ("4", "5")] == [
        "claude",
        "claude",
    ]
    assert not roster.retired(entry(directory, "claude"))
