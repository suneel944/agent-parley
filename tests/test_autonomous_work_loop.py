"""Exercises authorized work across dispatch, reports and real Git merges."""

import json
import os
import shlex
import subprocess
import sys
import time
import types
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


@pytest.mark.parametrize("live_exhaustion", [False, True])
def test_poll_recovers_work_and_offers_it_to_an_eligible_peer(
    bridge, repo, paired, monkeypatch, live_exhaustion
):
    directory, calls = prepare_loop(bridge, repo, paired, monkeypatch)
    source = Path(paired["lanes"]["claude"])
    target = Path(paired["lanes"]["codex"])
    bridge.issue(source, "claim", "17")
    source_head = commit_issue(source, "17")
    (source / "staged.bin").write_bytes(b"\x00staged\xff")
    git(source, "add", "staged.bin")
    (source / "issue-17.txt").write_text("remaining verification\n")
    (source / "untracked.bin").write_bytes(b"\x00untracked\xff")
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    def reject_native_launch(*args, **kwargs):
        pytest.fail("Orphan recovery must not resume the prior native owner")

    monkeypatch.setattr(
        supervision,
        "subprocess",
        types.SimpleNamespace(
            **{**vars(subprocess), "Popen": reject_native_launch}
        ),
    )
    try:
        write_json(
            directory / "claude-activity.json",
            {
                "activity": "working",
                "updated": time.time() - 1200,
                "session_id": "recovery-integration",
                "session_pid": child.pid,
                "session_ticks": process.start_ticks(child.pid),
            },
        )
        if live_exhaustion:
            supervision.record_capacity(
                directory,
                "claude",
                {
                    "state": "exhausted",
                    "observed_at": time.time(),
                    "source": "simulated-provider-fixture",
                    "session_id": "recovery-integration",
                    "observation_id": "integration-exhaustion",
                },
            )
            supervision.poll(bridge.home, directory)
            assert child.poll() is None
            assert supervision.published_stranded_claim(directory, "17")
            bridge.authorize_recovery(repo, "17", "Exercise saved work")
        else:
            child.terminate()
            child.wait(timeout=5)
        calls.clear()
        supervision.poll(bridge.home, directory)
        child.wait(timeout=5)
        assert issues.snapshot(directory)["issues"]["17"]["orphan"]
        assert any(call[1] == "codex" for call in calls)
        taken = bridge.issue(target, "claim", "17", take_orphaned=True)
        assert taken["owner"] == "codex"
        assert git(target, "rev-parse", "HEAD") == source_head
        assert git(target, "diff", "--cached", "--name-only") == "staged.bin"
        assert git(target, "diff", "--name-only") == "issue-17.txt"
        assert git(target, "ls-files", "--others", "--exclude-standard") == (
            "untracked.bin"
        )
        for name in ("staged.bin", "issue-17.txt", "untracked.bin"):
            assert (target / name).read_bytes() == (source / name).read_bytes()
        assert issues.snapshot(directory)["issues"]["42"]["blocked_by"] == [
            "17"
        ]
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
