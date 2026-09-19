"""Exercises authorized work across dispatch, reports and real Git merges."""

import json
import os
import shlex
import sys
import time
from pathlib import Path

import pytest

from agent_parley import issues, process, store, supervision, terminal
from agent_parley.cli import git
from agent_parley.state import BridgeError, write_json


def prepare_loop(bridge, repo, paired, monkeypatch):
    """Registers idle lanes and records an approved dependency chain."""
    store.initialize(bridge.home)
    directory = Path(paired["lanes"]["codex"]).parent
    for name, participant in paired["participants"].items():
        store.register(bridge.home, paired["root"], participant["display"])
        idle(directory, name)
    git(repo, "config", "user.name", "Bridge Test")
    git(repo, "config", "user.email", "test@example.com")
    plan = repo.parent / "approved-work.toml"
    plan.write_text(
        '[plan]\nname = "Approved completion trial"\n'
        '[dependencies]\n"42" = ["17"]\n"43" = ["42"]\n'
    )
    bridge.work_plan(repo, "apply", plan)
    monkeypatch.setattr(supervision.forge, "branch_completion", lambda *a: None)
    calls = []
    monkeypatch.setattr(
        terminal, "request", lambda *a: calls.append(a) or "accepted"
    )
    return directory, calls


def idle(directory, name):
    """Ends a simulated turn without erasing durable report or wake state."""
    path = directory / f"{name}-activity.json"
    state = json.loads(path.read_text()) if path.exists() else {}
    state.update(
        activity="idle",
        updated=time.time() - 1200,
        session_pid=os.getpid(),
        session_ticks=process.start_ticks(os.getpid()),
    )
    write_json(path, state)


def commit_issue(lane, number):
    """Commits deterministic work that the configured verification checks."""
    (lane / f"issue-{number}.txt").write_text("verified\n")
    git(lane, "add", f"issue-{number}.txt")
    git(
        lane,
        "-c",
        "user.name=Bridge Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-m",
        f"Complete fixture issue {number}",
    )
    return git(lane, "rev-parse", "HEAD")


def test_three_authorized_issues_dispatch_and_complete_in_dependency_order(
    bridge, repo, paired, monkeypatch
):
    directory, calls = prepare_loop(bridge, repo, paired, monkeypatch)
    lane = Path(paired["lanes"]["codex"])
    command = f"{shlex.quote(sys.executable)} -c " + shlex.quote(
        "from pathlib import Path; "
        "files = list(Path('.').glob('issue-*.txt')); "
        "assert all(p.read_text() == 'verified\\n' "
        "for p in files)"
    )
    bridge.verification(repo, command)
    bridge.approval_policy(repo, ["merge"])
    for number, next_number in (("17", "42"), ("42", "43"), ("43", None)):
        idle(directory, "codex")
        supervision.poll(bridge.home, directory)
        assert issues.unclaimed(issues.snapshot(directory)) == [number]
        offer = supervision.published_work(directory, "codex")["offer"]
        assert offer and f"#{number}" in offer["text"]
        assert any(call[1] == "codex" for call in calls)
        calls.clear()
        bridge.issue(lane, "claim", number)
        head = commit_issue(lane, number)
        bridge.report(lane, "ready", f"Issue {number} ready", "", "Run gate")
        assert issues.snapshot(directory)["issues"][number]["owner"] == "codex"
        with pytest.raises(BridgeError, match="approve codex"):
            bridge.merge(repo, "codex")
        bridge.approve(repo, "codex")
        bridge.merge(repo, "codex")
        ledger = issues.snapshot(directory)
        completed = ledger["issues"][number]
        assert completed["execution"]["state"] == "complete"
        assert completed["execution"]["gate"]["command"] == shlex.split(command)
        assert completed["execution"]["gate"]["commit"] == git(
            repo, "rev-parse", "HEAD"
        )
        assert completed["owner"] is None
        assert git(repo, "merge-base", "--is-ancestor", head, "HEAD") == ""
        assert issues.unclaimed(ledger) == (
            [next_number] if next_number else []
        )
    supervision.poll(bridge.home, directory)
    assert supervision.published_work(directory, "codex")["offer"] is None


def test_closed_unmerged_pull_request_does_not_complete_or_unblock_work(
    bridge, repo, paired, monkeypatch
):
    directory, _ = prepare_loop(bridge, repo, paired, monkeypatch)
    lane = Path(paired["lanes"]["codex"])
    bridge.issue(lane, "claim", "17")
    commit_issue(lane, "17")
    bridge.report(lane, "ready", "Ready for review", "", "Reported checks")
    monkeypatch.setattr(
        supervision.forge,
        "branch_completion",
        lambda *a: ("CLOSED", time.time()),
    )
    idle(directory, "codex")
    supervision.poll(bridge.home, directory)
    ledger = issues.snapshot(directory)
    assert ledger["issues"]["17"]["execution"]["state"] != "complete"
    assert ledger["issues"]["17"]["owner"] == "codex"
    assert ledger["issues"]["42"]["blocked_by"] == ["17"]
    assert issues.unclaimed(ledger) == []
