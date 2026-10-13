"""Checks durable, bounded recovery of an integration its gate never passed."""

import fcntl
import json
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent_parley import cli, lifecycle, merges, problems
from agent_parley.cli import git
from agent_parley.state import BridgeError

GATE = (
    "import pathlib, sys; "
    "pathlib.Path(sys.argv[1]).open('a').write('run\\n'); "
    "sys.exit(pathlib.Path('broken.txt').exists())"
)


def commit(worktree, message):
    """Records every pending change with a fixed identity."""
    git(worktree, "add", "--all")
    git(
        worktree,
        "-c",
        "user.name=Bridge Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-m",
        message,
    )


def ready(bridge, paired, name, issue, files):
    """Claims one issue, commits the given files and reports the lane ready."""
    lane = Path(paired["lanes"][name])
    bridge.issue(lane, "claim", issue)
    for filename, text in files.items():
        (lane / filename).write_text(text)
    commit(lane, f"{name} work")
    bridge.report(lane, "ready", f"{name} finished", "", "make check")
    return lane


def execution(bridge, repo, issue):
    """Reads one issue's normalized execution state from the ledger."""
    ledger = json.loads((bridge.project(repo)[1] / "issues.json").read_text())
    return lifecycle.state(ledger["issues"][issue])


@pytest.fixture(autouse=True)
def identified(repo):
    """Records the committer identity a merge commit in the base needs."""
    git(repo, "config", "user.name", "Bridge Test")
    git(repo, "config", "user.email", "test@example.com")


@pytest.fixture
def gated(bridge, repo, paired, tmp_path):
    """Records a gate that fails while `broken.txt` exists and counts runs."""
    runs = tmp_path / "gate runs"
    bridge.verification(
        repo, shlex.join([sys.executable, "-c", GATE, str(runs)])
    )
    return runs


@pytest.fixture
def failed(bridge, repo, paired, gated):
    """Integrates a lane whose work fails the post-merge gate."""
    lane = ready(bridge, paired, "claude", "42", {"broken.txt": "bad\n"})
    base = git(repo, "rev-parse", "HEAD")
    with pytest.raises(BridgeError, match="gate failed on attempt 1 of 3"):
        bridge.merge(repo, "claude")
    return {"lane": lane, "base": base}


def test_gate_failure_is_recorded_and_holds_the_base(
    bridge, repo, paired, gated, failed
):
    directory = bridge.project(repo)[1]
    held = merges.integration_record(directory)
    record = json.loads((directory / "issues.json").read_text())["issues"]

    assert held["kind"] == merges.GATE_FAILED
    assert held["lane"] == "claude"
    assert held["issue"] == "42"
    assert held["claim_id"] == record["42"]["claim_id"]
    assert held["base"] == failed["base"]
    assert held["result"] == git(repo, "rev-parse", "HEAD")
    assert held["source_commit"] == git(failed["lane"], "rev-parse", "HEAD")
    assert held["command"][-1] == str(gated)
    assert "exited 1" in held["detail"]
    assert len(held["detail"]) <= merges.MAX_DIAGNOSTIC + 3
    state = execution(bridge, repo, "42")
    assert state["state"] == lifecycle.RECOVERY
    assert state["next_action"] == "repair integration"
    assert (repo / "broken.txt").exists()

    ready(bridge, paired, "codex", "43", {"other.txt": "fine\n"})
    head = git(repo, "rev-parse", "HEAD")
    with pytest.raises(BridgeError, match="no other lane is integrated"):
        bridge.merge(repo, "codex")
    assert git(repo, "rev-parse", "HEAD") == head
    preview = bridge.preview_merge(repo, "codex")
    assert "carries claude's integration" in preview

    rows = problems._integration_rows(directory, str(repo), time.time())
    assert len(rows) == 1
    assert rows[0]["participant"] == "claude"
    assert "agent-parley participant merge claude" in rows[0]["command"]


def test_successful_recovery_verifies_the_exact_repaired_result(
    bridge, repo, paired, gated, failed
):
    lane = failed["lane"]
    git(lane, "rm", "--quiet", "broken.txt")
    (lane / "fixed.txt").write_text("repaired\n")
    commit(lane, "claude repair")
    bridge.report(lane, "ready", "claude repaired", "", "make check")

    report = bridge.merge(repo, "claude")

    head = git(repo, "rev-parse", "HEAD")
    assert f"Recovery verified {head[:12]} on attempt 2 of 3." in report
    assert merges.integration_record(bridge.project(repo)[1]) is None
    state = execution(bridge, repo, "42")
    assert state["state"] == lifecycle.COMPLETE
    assert state["integrated_commit"] == head
    assert state["source_commit"] == git(lane, "rev-parse", "HEAD")
    assert not (repo / "broken.txt").exists()

    ready(bridge, paired, "codex", "43", {"other.txt": "fine\n"})
    assert "Merged" in bridge.merge(repo, "codex")


def test_a_conflict_is_recorded_before_the_base_changes(bridge, repo, paired):
    (repo / "shared.txt").write_text("base edit\n")
    commit(repo, "Base edit")
    base = git(repo, "rev-parse", "HEAD")
    ready(bridge, paired, "claude", "42", {"shared.txt": "lane edit\n"})
    ready(bridge, paired, "codex", "43", {"other.txt": "fine\n"})

    with pytest.raises(BridgeError, match="conflict on attempt 1"):
        bridge.merge(repo, "claude")

    directory = bridge.project(repo)[1]
    held = merges.integration_record(directory)
    assert held["kind"] == merges.CONFLICT
    assert held["base"] == base
    assert held["result"] == ""
    assert git(repo, "rev-parse", "HEAD") == base
    assert merges.merging(repo)
    assert execution(bridge, repo, "42")["state"] == lifecycle.RECOVERY
    with pytest.raises(BridgeError, match="no other lane is integrated"):
        bridge.merge(repo, "codex")

    git(repo, "merge", "--abort")

    assert problems._integration_rows(directory, str(repo), time.time()) == []
    assert "Merged" in bridge.merge(repo, "codex")
    assert merges.integration_record(directory) is None


def test_a_crash_between_merge_and_verification_survives_a_restart(
    bridge, repo, paired, gated, monkeypatch
):
    ready(bridge, paired, "claude", "42", {"work.txt": "good\n"})
    real = cli.verify_base

    def crash(root, command, integrated=False):
        if integrated:
            raise KeyboardInterrupt
        real(root, command, integrated)

    monkeypatch.setattr(cli, "verify_base", crash)
    with pytest.raises(KeyboardInterrupt):
        bridge.merge(repo, "claude")
    monkeypatch.setattr(cli, "verify_base", real)

    directory = bridge.project(repo)[1]
    merged = git(repo, "rev-parse", "HEAD")
    held = merges.integration_record(directory)
    assert held["kind"] == merges.INTERRUPTED
    assert held["result"] == merged
    assert execution(bridge, repo, "42")["state"] == lifecycle.READY

    restarted = cli.Bridge(bridge.home)
    report = restarted.merge(repo, "claude")

    assert "already contains" in report
    assert f"Recovery verified {merged[:12]}" in report
    assert git(repo, "rev-parse", "HEAD") == merged
    state = execution(bridge, repo, "42")
    assert state["state"] == lifecycle.COMPLETE
    assert state["integrated_commit"] == merged
    assert merges.integration_record(directory) is None


def test_a_changed_base_is_verified_at_its_exact_commit(
    bridge, repo, paired, gated, failed
):
    git(repo, "rm", "--quiet", "broken.txt")
    commit(repo, "Operator repair on the base")
    repaired = git(repo, "rev-parse", "HEAD")

    report = bridge.merge(repo, "claude")

    assert f"Recovery verified {repaired[:12]}" in report
    assert git(repo, "rev-parse", "HEAD") == repaired
    state = execution(bridge, repo, "42")
    assert state["state"] == lifecycle.COMPLETE
    assert state["integrated_commit"] == repaired


def test_a_base_moved_during_verification_stays_unverified(
    bridge, repo, paired, monkeypatch
):
    ready(bridge, paired, "claude", "42", {"work.txt": "good\n"})

    def moving(root, command, integrated=False):
        if integrated:
            git(root, "commit", "--allow-empty", "-m", "Moved during gate")

    bridge.verification(repo, "true")
    monkeypatch.setattr(cli, "verify_base", moving)

    with pytest.raises(BridgeError, match="interrupted on attempt 1"):
        bridge.merge(repo, "claude")

    held = merges.integration_record(bridge.project(repo)[1])
    assert held["kind"] == merges.INTERRUPTED
    assert held["result"] != git(repo, "rev-parse", "HEAD")
    assert execution(bridge, repo, "42")["state"] == lifecycle.READY


def test_repeated_failure_exhausts_into_one_escalation(
    bridge, repo, paired, gated, failed
):
    for attempt in (2, 3):
        with pytest.raises(BridgeError, match=f"attempt {attempt} of 3"):
            bridge.merge(repo, "claude")
    runs = gated.read_text().count("run")
    head = git(repo, "rev-parse", "HEAD")

    with pytest.raises(BridgeError, match="All 3 of 3 attempts are used"):
        bridge.merge(repo, "claude")

    assert gated.read_text().count("run") == runs
    assert git(repo, "rev-parse", "HEAD") == head
    directory = bridge.project(repo)[1]
    rows = problems._integration_rows(directory, str(repo), time.time())
    assert len(rows) == 1
    assert "--renew-recovery" in rows[0]["command"]
    with pytest.raises(BridgeError, match="in the base checkout"):
        bridge.merge(failed["lane"], "claude", renew=True)

    lane = failed["lane"]
    git(lane, "rm", "--quiet", "broken.txt")
    commit(lane, "claude repair")
    bridge.report(lane, "ready", "claude repaired", "", "make check")
    report = bridge.merge(repo, "claude", renew=True)

    assert "on attempt 4 of 6" in report
    assert merges.integration_record(directory) is None
    assert execution(bridge, repo, "42")["state"] == lifecycle.COMPLETE


def test_repair_follows_the_claim_never_a_bystander(
    bridge, repo, paired, gated, failed
):
    bridge.issue(failed["lane"], "release", "42")
    directory = bridge.project(repo)[1]
    held = merges.integration_record(directory)

    assert merges.integration_owner(directory, held) == ""
    assert "is unheld" in merges.integration_remedy(repo, held, "")
    with pytest.raises(BridgeError, match="no other lane is integrated"):
        bridge.merge(repo, "claude")
    assert execution(bridge, repo, "42")["state"] == lifecycle.QUEUED


def test_duplicate_and_stale_observations_change_nothing(tmp_path):
    entry = {
        "kind": merges.GATE_FAILED,
        "lane": "claude",
        "branch": "bridge/claude",
        "issue": "42",
        "claim_id": "0" * 16,
        "source_commit": "a" * 40,
        "base": "b" * 40,
        "result": "c" * 40,
        "command": ["true"],
        "attempt": 2,
        "limit": 3,
        "detail": "exited 1",
        "recorded_at": 0.0,
    }
    merges.record_integration(tmp_path, entry)
    merges.record_integration(tmp_path, entry)
    stale = {**entry, "attempt": 1, "kind": merges.INTERRUPTED}

    kept = merges.record_integration(tmp_path, stale)

    assert kept["attempt"] == 2
    assert kept["kind"] == merges.GATE_FAILED
    assert not merges.clear_integration(tmp_path, 1, "c" * 40)
    assert not merges.clear_integration(tmp_path, 2, "d" * 40)
    assert merges.integration_record(tmp_path)["attempt"] == 2
    assert merges.clear_integration(tmp_path, 2, "c" * 40)
    assert merges.integration_record(tmp_path) is None


def test_an_unreadable_record_holds_integration(bridge, repo, paired):
    directory = bridge.project(repo)[1]
    (directory / merges.INTEGRATION_RECORD).write_text("{not json")
    ready(bridge, paired, "claude", "42", {"work.txt": "good\n"})

    with pytest.raises(BridgeError, match="cannot be read"):
        bridge.merge(repo, "claude")
    assert "Merge lane branch" not in git(repo, "log", "--pretty=%s")


def repair_base(repo):
    """Commits an operator fix on the base that makes the gate pass."""
    git(repo, "rm", "--quiet", "broken.txt")
    commit(repo, "Operator repair on the base")
    return git(repo, "rev-parse", "HEAD")


def test_a_completed_issue_releases_only_through_the_operator(
    bridge, repo, paired, gated, failed
):
    directory = bridge.project(repo)[1]
    held = merges.integration_record(directory)
    lifecycle.complete(
        directory,
        "42",
        held["claim_id"],
        held["result"],
        held["command"],
        held["source_commit"],
    )

    assert merges.integration_owner(directory, held) == ""
    remedy = merges.integration_remedy(repo, held, "")
    assert "report --state ready" in remedy
    assert merges.VERIFY_RECOVERY in remedy
    rows = problems._integration_rows(directory, str(repo), time.time())
    assert merges.VERIFY_RECOVERY in rows[0]["command"]

    repair_base(repo)
    report = bridge.verify_recovery(repo)

    assert "cleared claude's gate failed record" in report
    assert merges.integration_record(directory) is None
    ready(bridge, paired, "codex", "43", {"other.txt": "fine\n"})
    assert "Merged" in bridge.merge(repo, "codex")


def test_a_retired_lane_on_a_record_without_an_issue(
    bridge, repo, paired, gated
):
    directory = bridge.project(repo)[1]
    head = git(repo, "rev-parse", "HEAD")
    merges.record_integration(
        directory,
        {
            "kind": merges.INTERRUPTED,
            "lane": "retired-lane",
            "branch": "bridge/retired-lane",
            "issue": None,
            "claim_id": "",
            "source_commit": head,
            "base": head,
            "result": head,
            "command": [sys.executable, "-c", GATE, str(gated)],
            "attempt": 1,
            "limit": 3,
            "detail": "",
            "recorded_at": time.time(),
        },
    )
    held = merges.integration_record(directory)

    assert merges.integration_owner(directory, held) == ""
    assert "no longer an active participant" in merges.integration_hold(
        repo, directory, held
    )
    assert merges.VERIFY_RECOVERY in merges.integration_hold(
        repo, directory, held
    )
    ready(bridge, paired, "codex", "43", {"other.txt": "fine\n"})
    with pytest.raises(BridgeError, match="no other lane is integrated"):
        bridge.merge(repo, "codex")

    assert "cleared retired-lane's" in bridge.verify_recovery(repo)
    assert "Merged" in bridge.merge(repo, "codex")


def test_the_operator_path_clears_only_on_a_passing_gate(
    bridge, repo, paired, gated, failed
):
    directory = bridge.project(repo)[1]
    with pytest.raises(BridgeError, match="in the base checkout"):
        bridge.verify_recovery(failed["lane"])
    with pytest.raises(BridgeError, match="record stands"):
        bridge.verify_recovery(repo)
    assert merges.integration_record(directory)["attempt"] == 1

    (repo / "scratch.txt").write_text("uncommitted\n")
    with pytest.raises(BridgeError, match="uncommitted changes"):
        bridge.verify_recovery(repo)
    (repo / "scratch.txt").unlink()
    assert merges.integration_record(directory) is not None

    repaired = repair_base(repo)
    report = bridge.verify_recovery(repo)

    assert f"Verified the base at {repaired[:12]}" in report
    assert merges.integration_record(directory) is None
    assert git(repo, "rev-parse", "HEAD") == repaired
    assert "carries no unverified" in bridge.verify_recovery(repo)


def test_a_busy_issue_ledger_never_masks_the_failure(
    bridge, repo, paired, gated, monkeypatch
):
    def busy(*args):
        raise BridgeError("issues.lock is busy")

    monkeypatch.setattr(lifecycle, "integration_failed", busy)
    ready(bridge, paired, "claude", "42", {"broken.txt": "bad\n"})

    with pytest.raises(BridgeError, match="gate failed on attempt 1 of 3"):
        bridge.merge(repo, "claude")

    held = merges.integration_record(bridge.project(repo)[1])
    assert held["kind"] == merges.GATE_FAILED
    assert execution(bridge, repo, "42")["state"] == lifecycle.READY


def test_a_busy_ledger_at_completion_keeps_the_record_for_a_retry(
    bridge, repo, paired, gated, monkeypatch
):
    ready(bridge, paired, "claude", "42", {"work.txt": "good\n"})
    directory = bridge.project(repo)[1]
    real = lifecycle.complete

    def contended(*args):
        with (directory / "issues.lock").open("a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            return real(*args)

    monkeypatch.setattr(lifecycle, "complete", contended)
    with pytest.raises(BridgeError, match="could not be recorded complete"):
        bridge.merge(repo, "claude")
    monkeypatch.setattr(lifecycle, "complete", real)

    merged = git(repo, "rev-parse", "HEAD")
    held = merges.integration_record(directory)
    assert held["result"] == merged
    assert "not recorded complete" in held["detail"]
    assert execution(bridge, repo, "42")["state"] == lifecycle.READY

    report = bridge.merge(repo, "claude")

    assert f"Recovery verified {merged[:12]}" in report
    assert git(repo, "rev-parse", "HEAD") == merged
    state = execution(bridge, repo, "42")
    assert state["state"] == lifecycle.COMPLETE
    assert state["integrated_commit"] == merged
    assert merges.integration_record(directory) is None


def test_a_merge_timeout_before_any_change_burns_no_attempt(
    bridge, repo, paired, gated, failed, monkeypatch
):
    directory = bridge.project(repo)[1]
    before = merges.integration_record(directory)

    def slow(*args):
        raise subprocess.TimeoutExpired(["git", "merge"], 1)

    monkeypatch.setattr(cli, "merge_branch", slow)
    with pytest.raises(subprocess.TimeoutExpired):
        bridge.merge(repo, "claude")

    after = merges.integration_record(directory)
    assert after["attempt"] == before["attempt"] == 1
    assert after["kind"] == merges.GATE_FAILED
    assert after["result"] == before["result"]


def test_a_gate_timeout_after_the_merge_is_recorded_unverified(
    bridge, repo, paired, monkeypatch
):
    hang = (
        "import pathlib, time; "
        "pathlib.Path('slow.txt').exists() and time.sleep(30)"
    )
    bridge.verification(repo, shlex.join([sys.executable, "-c", hang]))
    ready(bridge, paired, "claude", "42", {"slow.txt": "slow\n"})
    monkeypatch.setattr("agent_parley.worktrees.VERIFY_TIMEOUT", 1)

    with pytest.raises(BridgeError, match="timed out"):
        bridge.merge(repo, "claude")

    held = merges.integration_record(bridge.project(repo)[1])
    assert held["kind"] == merges.GATE_FAILED
    assert held["result"] == git(repo, "rev-parse", "HEAD")
    assert execution(bridge, repo, "42")["state"] == lifecycle.RECOVERY


def test_a_first_merge_timeout_leaves_no_record(
    bridge, repo, paired, monkeypatch
):
    ready(bridge, paired, "claude", "42", {"work.txt": "good\n"})

    def slow(*args):
        raise subprocess.TimeoutExpired(["git", "merge"], 1)

    monkeypatch.setattr(cli, "merge_branch", slow)
    with pytest.raises(subprocess.TimeoutExpired):
        bridge.merge(repo, "claude")

    assert merges.integration_record(bridge.project(repo)[1]) is None


def test_merge_head_without_unmerged_paths_is_an_interruption(
    bridge, repo, paired, monkeypatch
):
    ready(bridge, paired, "claude", "42", {"work.txt": "good\n"})

    def killed(root, lane, name, branch, source):
        git(root, "merge", "--no-ff", "--no-commit", branch)
        raise subprocess.TimeoutExpired(["git", "merge"], 1)

    monkeypatch.setattr(cli, "merge_branch", killed)
    with pytest.raises(BridgeError, match="interrupted on attempt 1"):
        bridge.merge(repo, "claude")

    held = merges.integration_record(bridge.project(repo)[1])
    assert merges.merging(repo)
    assert not merges.unmerged(repo)
    assert held["kind"] == merges.INTERRUPTED
    assert held["result"] == ""
    assert "merge --abort" in merges.integration_remedy(repo, held)
    assert execution(bridge, repo, "42")["state"] == lifecycle.READY
