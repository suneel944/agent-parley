"""Durable authorized-work lifecycle coverage."""

import json
import shlex
import sys
import time
from pathlib import Path

import pytest

from agent_parley import cli, issues, lifecycle, plan, retries
from agent_parley.cli import git
from agent_parley.state import BridgeError


def claim(directory, number, agent="codex"):
    """Claims one issue through the serialized issue transition."""
    return issues.change(
        directory,
        agent,
        "claim",
        number,
        participants={"codex", "claude"},
    )


def commit(repo: Path, name: str, content: str = "work\n") -> str:
    """Creates one identified test commit and returns its object name."""
    (repo / name).write_text(content)
    git(repo, "add", name)
    git(
        repo,
        "-c",
        "user.name=Bridge Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-m",
        f"Add {name}",
    )
    return git(repo, "rev-parse", "HEAD")


def identify(repo: Path) -> None:
    """Configures the temporary base checkout to create merge commits."""
    git(repo, "config", "user.name", "Bridge Test")
    git(repo, "config", "user.email", "test@example.com")


def test_plan_authorizes_blockers_and_completion_reconciles_dependencies(
    tmp_path,
):
    planned = tmp_path / "work.toml"
    planned.write_text(
        '[plan]\nname = "release"\n[dependencies]\n42 = [17]\n43 = [42]\n'
    )
    plan.apply(tmp_path, planned)
    ledger = issues.snapshot(tmp_path)

    assert lifecycle.actionable(ledger) == ["17"]
    first = claim(tmp_path, "17")
    lifecycle.record_report(
        tmp_path,
        "codex",
        "ready",
        "a" * 40,
        "",
    )
    lifecycle.complete(
        tmp_path,
        "17",
        first["claim_id"],
        "b" * 40,
        ["make", "check"],
    )

    ledger = issues.snapshot(tmp_path)
    completed = ledger["issues"]["17"]
    assert completed["owner"] is None
    assert completed["execution"]["state"] == lifecycle.COMPLETE
    assert completed["execution"]["gate"] == {
        "command": ["make", "check"],
        "status": "passed",
        "commit": "b" * 40,
        "at": completed["execution"]["gate"]["at"],
    }
    assert ledger["issues"]["42"]["blocked_by"] == []
    assert lifecycle.actionable(ledger) == ["42"]


def test_release_requeues_partial_work_but_never_recycles_completion(tmp_path):
    current = claim(tmp_path, "8")
    issues.change(
        tmp_path,
        "codex",
        "release",
        "8",
        participants={"codex"},
    )
    assert lifecycle.actionable(issues.snapshot(tmp_path)) == ["8"]

    current = claim(tmp_path, "8")
    lifecycle.record_report(
        tmp_path,
        "codex",
        "ready",
        "c" * 40,
        "",
    )
    lifecycle.complete(
        tmp_path,
        "8",
        current["claim_id"],
        "d" * 40,
        [],
    )

    assert lifecycle.actionable(issues.snapshot(tmp_path)) == []
    with pytest.raises(BridgeError, match="Verified complete"):
        claim(tmp_path, "8")


def test_release_refuses_ready_work_and_preserves_its_claim(tmp_path):
    current = claim(tmp_path, "9")
    lifecycle.record_report(
        tmp_path,
        "codex",
        "ready",
        "e" * 40,
        "",
    )

    with pytest.raises(BridgeError, match="must remain claimed"):
        issues.change(
            tmp_path,
            "codex",
            "release",
            "9",
            participants={"codex"},
        )

    record = issues.snapshot(tmp_path)["issues"]["9"]
    assert record["owner"] == "codex"
    assert record["claim_id"] == current["claim_id"]
    assert record["execution"]["state"] == lifecycle.READY
    assert record["execution"]["commit"] == "e" * 40


def test_old_generation_and_closed_pull_request_cannot_complete_new_claim(
    tmp_path,
):
    old = claim(tmp_path, "9")
    issues.change(
        tmp_path,
        "codex",
        "release",
        "9",
        participants={"codex"},
    )
    current = claim(tmp_path, "9")
    ledger = issues.snapshot(tmp_path)
    ledger["issues"]["9"]["handoff_prompt"] = {"trigger": "pull request ended"}
    (tmp_path / "issues.json").write_text(json.dumps(ledger))

    assert not issues.released(issues.snapshot(tmp_path)["issues"]["9"])
    with pytest.raises(BridgeError, match="changed ownership generation"):
        lifecycle.complete(
            tmp_path,
            "9",
            old["claim_id"],
            "e" * 40,
            [],
        )
    assert current["claim_id"] != old["claim_id"]


def test_blocked_and_ready_work_are_not_dispatched_as_continuations(tmp_path):
    claim(tmp_path, "10")
    lifecycle.record_report(
        tmp_path,
        "codex",
        "blocked",
        "f" * 40,
        "approval:merge",
    )
    assert lifecycle.actionable(issues.snapshot(tmp_path), "codex") == []

    lifecycle.record_report(
        tmp_path,
        "codex",
        "ready",
        "f" * 40,
        "",
    )
    ledger = issues.snapshot(tmp_path)
    assert lifecycle.actionable(ledger, "codex") == []
    assert ledger["issues"]["10"]["execution"]["next_action"] == (
        "verify and integrate"
    )


def test_a_held_claim_whose_issue_closed_is_not_a_continuation(tmp_path):
    claim(tmp_path, "11")
    ledger = issues.snapshot(tmp_path)
    assert lifecycle.actionable(ledger, "codex") == ["11"]

    for responded in (None, time.time()):
        ledger["issues"]["11"]["handoff_prompt"] = {
            "trigger": issues.ENDED,
            "holder": "codex",
            "created": time.time(),
            "responded_at": responded,
        }
        assert lifecycle.actionable(ledger, "codex") == []


def test_cli_issue_resume_condition_continues_after_verified_dependency(
    bridge,
    repo,
    paired,
    monkeypatch,
    capsys,
):
    directory = Path(paired["lanes"]["codex"]).parent
    codex = Path(paired["lanes"]["codex"])
    claude = Path(paired["lanes"]["claude"])
    monkeypatch.setattr(
        "agent_parley.cli.forge.issue_title", lambda *args: None
    )
    monkeypatch.setattr("agent_parley.cli.forge.assign", lambda *args: True)
    identify(repo)
    bridge.issue(codex, "claim", "42")
    bridge.issue(claude, "claim", "17")

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agent-parley",
            "--home",
            str(bridge.home),
            "report",
            "--repo",
            str(codex),
            "--state",
            "blocked",
            "--summary",
            "Waiting for prerequisite",
            "--remaining",
            "Issue 17 must integrate",
            "--resume-on",
            "17",
        ],
    )
    assert cli.main() == 0
    capsys.readouterr()
    blocked = issues.snapshot(directory)["issues"]["42"]
    assert blocked["blocked_by"] == ["17"]
    assert blocked["execution"]["resume_when"] == {
        "kind": "issue",
        "issue": "17",
    }

    commit(claude, "dependency.txt")
    bridge.report(claude, "ready", "Dependency done", "", "tests passed")
    bridge.merge(repo, "claude")

    resumed = issues.snapshot(directory)["issues"]["42"]
    assert resumed["blocked_by"] == []
    assert resumed["execution"]["state"] == lifecycle.RUNNING
    assert resumed["execution"]["resume_when"] == ""
    completed = issues.snapshot(directory)["issues"]["17"]["execution"]
    assert completed["source_commit"]
    assert completed["integrated_commit"] == git(repo, "rev-parse", "HEAD")


def test_ready_report_binds_source_commit_and_refuses_later_lane_commit(
    bridge,
    repo,
    paired,
    monkeypatch,
):
    lane = Path(paired["lanes"]["codex"])
    directory = lane.parent
    monkeypatch.setattr(
        "agent_parley.cli.forge.issue_title", lambda *args: None
    )
    monkeypatch.setattr("agent_parley.cli.forge.assign", lambda *args: True)
    bridge.issue(lane, "claim", "51")
    source = commit(lane, "ready.txt")
    bridge.report(lane, "ready", "Ready", "", "tests passed")
    commit(lane, "later.txt")

    with pytest.raises(BridgeError, match="committed since"):
        bridge.merge(repo, "codex")

    execution = issues.snapshot(directory)["issues"]["51"]["execution"]
    assert execution["state"] == lifecycle.READY
    assert execution["source_commit"] == source
    assert not (repo / "later.txt").exists()


@pytest.mark.parametrize(
    ("mutation", "dirty"),
    (
        ("printf changed > shared.txt", "shared.txt"),
        ("printf generated > generated.py", "generated.py"),
    ),
)
def test_passing_gate_that_changes_repository_cannot_complete(
    bridge,
    repo,
    paired,
    monkeypatch,
    tmp_path,
    mutation,
    dirty,
):
    lane = Path(paired["lanes"]["codex"])
    directory = lane.parent
    monkeypatch.setattr(
        "agent_parley.cli.forge.issue_title", lambda *args: None
    )
    monkeypatch.setattr("agent_parley.cli.forge.assign", lambda *args: True)
    identify(repo)
    bridge.issue(lane, "claim", "52")
    commit(lane, "gated.txt")
    bridge.report(lane, "ready", "Ready", "", "tests passed")
    marker = tmp_path / "gate-ran"
    script = tmp_path / "mutating-gate.sh"
    script.write_text(
        "#!/bin/sh\n"
        f"if test -e {shlex.quote(str(marker))}; then\n"
        f"  {mutation}\n"
        "else\n"
        f"  touch {shlex.quote(str(marker))}\n"
        "fi\n"
    )
    script.chmod(0o700)
    bridge.verification(repo, shlex.quote(str(script)))

    with pytest.raises(BridgeError, match="changed repository"):
        bridge.merge(repo, "codex")

    execution = issues.snapshot(directory)["issues"]["52"]["execution"]
    assert execution["state"] == lifecycle.RECOVERY
    assert execution["next_action"] == "repair integration"
    assert dirty in git(repo, "status", "--porcelain")


def test_premerge_gate_that_moves_base_head_cannot_integrate(
    bridge,
    repo,
    paired,
    monkeypatch,
    tmp_path,
):
    lane = Path(paired["lanes"]["codex"])
    directory = lane.parent
    monkeypatch.setattr(
        "agent_parley.cli.forge.issue_title", lambda *args: None
    )
    monkeypatch.setattr("agent_parley.cli.forge.assign", lambda *args: True)
    identify(repo)
    bridge.issue(lane, "claim", "57")
    commit(lane, "pre-gate.txt")
    bridge.report(lane, "ready", "Ready", "", "tests passed")
    original_base = git(repo, "rev-parse", "HEAD")
    marker = tmp_path / "committing-gate-ran"
    script = tmp_path / "committing-gate.sh"
    script.write_text(
        "#!/bin/sh\n"
        f"if ! test -e {shlex.quote(str(marker))}; then\n"
        "  git -c user.name='Gate Test' -c user.email=gate@example.com "
        "commit --allow-empty -m 'Gate commit'\n"
        f"  touch {shlex.quote(str(marker))}\n"
        "fi\n"
    )
    script.chmod(0o700)
    bridge.verification(repo, shlex.quote(str(script)))

    with pytest.raises(BridgeError, match="Pre-merge verification changed"):
        bridge.merge(repo, "codex")

    assert git(repo, "rev-parse", "HEAD") != original_base
    assert not (repo / "pre-gate.txt").exists()
    execution = issues.snapshot(directory)["issues"]["57"]["execution"]
    assert execution["state"] == lifecycle.READY


def test_merge_gate_leaves_the_setup_lock_to_other_operations(
    bridge,
    repo,
    paired,
    monkeypatch,
):
    lane = Path(paired["lanes"]["codex"])
    directory = lane.parent
    monkeypatch.setattr(
        "agent_parley.cli.forge.issue_title", lambda *args: None
    )
    monkeypatch.setattr("agent_parley.cli.forge.assign", lambda *args: True)
    identify(repo)
    bridge.issue(lane, "claim", "58")
    commit(lane, "gated.txt")
    bridge.report(lane, "ready", "Ready", "", "tests passed")
    bridge.verification(repo, "true")
    original = cli.verify_base
    seen = []

    def gate(*args, **kwargs):
        with cli.lock(directory / "setup.lock"):
            seen.append("setup")
        with pytest.raises(BridgeError, match="Another merge"):
            bridge.merge(repo, "claude")
        return original(*args, **kwargs)

    monkeypatch.setattr("agent_parley.cli.verify_base", gate)

    bridge.merge(repo, "codex")

    assert seen == ["setup", "setup"]
    assert (repo / "gated.txt").exists()


def test_keyed_report_retry_repairs_interrupted_lifecycle_transition(
    bridge,
    paired,
    monkeypatch,
):
    lane = Path(paired["lanes"]["codex"])
    directory = lane.parent
    monkeypatch.setattr(
        "agent_parley.cli.forge.issue_title", lambda *args: None
    )
    monkeypatch.setattr("agent_parley.cli.forge.assign", lambda *args: True)
    bridge.issue(lane, "claim", "53")
    actual = lifecycle.record_report
    calls = 0

    def interrupted(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("interrupted after checkpoint")
        return actual(*args, **kwargs)

    monkeypatch.setattr(lifecycle, "record_report", interrupted)
    with pytest.raises(OSError, match="interrupted"):
        bridge.report(
            lane,
            "partial",
            "In progress",
            "More work",
            "",
            key="repair",
        )
    bridge.report(
        lane,
        "partial",
        "In progress",
        "More work",
        "",
        key="repair",
    )

    execution = issues.snapshot(directory)["issues"]["53"]["execution"]
    assert calls == 2
    assert execution["state"] == lifecycle.RUNNING
    assert execution["progress"]["token"] == git(lane, "rev-parse", "HEAD")


def test_report_does_not_apply_to_claim_acquired_after_selection(
    bridge,
    paired,
    monkeypatch,
):
    lane = Path(paired["lanes"]["codex"])
    directory = lane.parent
    monkeypatch.setattr(
        "agent_parley.cli.forge.issue_title", lambda *args: None
    )
    monkeypatch.setattr("agent_parley.cli.forge.assign", lambda *args: True)
    bridge.issue(lane, "claim", "54")
    actual = lifecycle.record_report
    acquired = False

    def concurrent_claim(*args, **kwargs):
        nonlocal acquired
        if not acquired:
            acquired = True
            bridge.issue(lane, "claim", "55")
        return actual(*args, **kwargs)

    monkeypatch.setattr(lifecycle, "record_report", concurrent_claim)
    bridge.report(lane, "ready", "Issue 54 done", "", "tests passed")

    ledger = issues.snapshot(directory)["issues"]
    assert ledger["54"]["execution"]["state"] == lifecycle.READY
    assert ledger["55"]["execution"]["state"] == lifecycle.RUNNING


def test_unknown_resume_issue_adds_no_authority_or_blocked_activity(
    bridge,
    paired,
    monkeypatch,
):
    lane = Path(paired["lanes"]["codex"])
    directory = lane.parent
    monkeypatch.setattr(
        "agent_parley.cli.forge.issue_title", lambda *args: None
    )
    monkeypatch.setattr("agent_parley.cli.forge.assign", lambda *args: True)
    bridge.issue(lane, "claim", "56")

    with pytest.raises(BridgeError, match="not authorized work"):
        bridge.report(
            lane,
            "blocked",
            "Waiting",
            "Unknown dependency",
            "",
            resume_on="99",
        )

    ledger = issues.snapshot(directory)["issues"]
    assert "99" not in ledger
    assert ledger["56"]["execution"]["state"] == lifecycle.RUNNING
    activity = directory / "codex-activity.json"
    assert (
        not activity.exists()
        or json.loads(activity.read_text()).get("outcome") != "blocked"
    )


def planned(directory: Path, text: str) -> None:
    """Applies one plan file written into the state directory."""
    path = directory / "work.toml"
    path.write_text(text)
    plan.apply(directory, path)


def rewrite(directory: Path, change) -> None:
    """Edits the issue ledger in place, bypassing every transition."""
    path = directory / "issues.json"
    ledger = json.loads(path.read_text())
    change(ledger["issues"])
    path.write_text(json.dumps(ledger))


def completed(records: dict, number: str) -> None:
    """Marks one ledger record complete without reconciling dependents."""
    records[number].setdefault("execution", {})["state"] = lifecycle.COMPLETE


def block(directory: Path, number: str, blocker: str) -> dict:
    """Records one dependency edge as the codex lane."""
    return issues.change(
        directory,
        "codex",
        "block",
        number,
        participants={"codex", "claude"},
        on=blocker,
    )


def test_blocking_on_unrecorded_or_complete_work_adds_no_edge(tmp_path):
    planned(tmp_path, "[dependencies]\n42 = [17]\n")
    claim(tmp_path, "50")
    with pytest.raises(BridgeError, match="not in the issue ledger"):
        block(tmp_path, "50", "99")
    rewrite(tmp_path, lambda records: completed(records, "17"))

    assert block(tmp_path, "50", "17").get("blocked_by", []) == []
    waiting = issues.snapshot(tmp_path)["issues"]["50"]
    assert waiting.get("blocked_by", []) == []


def test_a_block_that_closes_a_cycle_is_refused(tmp_path):
    planned(tmp_path, "[dependencies]\n42 = [17]\n43 = [42]\n")
    claim(tmp_path, "17")

    with pytest.raises(BridgeError, match="dependency cycle"):
        block(tmp_path, "17", "43")
    waiting = issues.snapshot(tmp_path)["issues"]["17"]
    assert waiting.get("blocked_by", []) == []


def test_the_supervisor_drops_edges_no_completion_will_clear(tmp_path):
    planned(tmp_path, "[dependencies]\n42 = [17]\n43 = [99]\n")

    def stale(records: dict) -> None:
        """Completes #17 unreconciled and forgets #99 entirely."""
        completed(records, "17")
        del records["99"]

    rewrite(tmp_path, stale)

    assert lifecycle.settle_dependencies(tmp_path) == [
        ("42", "17"),
        ("43", "99"),
    ]
    ledger = issues.snapshot(tmp_path)["issues"]
    assert ledger["42"]["blocked_by"] == []
    assert ledger["43"]["blocked_by"] == []
    assert lifecycle.settle_dependencies(tmp_path) == []


def test_the_operator_unblocks_an_unowned_issue_from_the_base_checkout(
    bridge, repo, paired
):
    directory = bridge.project(repo)[1]
    lane = Path(paired["lanes"]["codex"])
    planned(directory, "[dependencies]\n42 = [17]\n")

    with pytest.raises(BridgeError, match="has no owner"):
        bridge.issue(lane, "unblock", "42", on="17")
    bridge.issue(repo, "unblock", "42", on="17")
    assert issues.snapshot(directory)["issues"]["42"]["blocked_by"] == []


def test_history_is_capped_but_keeps_the_claim_marker():
    record = {"history": []}
    lifecycle.append_history(record, {"action": "claim", "at": 0})
    last = 0
    for last in range(1, lifecycle.MAX_HISTORY + 20):
        lifecycle.append_history(record, {"action": "note", "at": last})
    history = record["history"]
    assert len(history) == lifecycle.MAX_HISTORY + 1
    assert history[0] == {"action": "claim", "at": 0}
    assert history[-1] == {"action": "note", "at": last}
    assert history[1]["at"] == last - lifecycle.MAX_HISTORY + 1


def test_repeated_claim_release_cycles_never_grow_past_the_cap(tmp_path):
    """A lane cycling one issue keeps its ledger entry bounded.

    At most three retained marker entries (claim, take and complete) can
    sit outside the capped recent window, so the total never exceeds the
    cap by more than that fixed amount, however many cycles run.
    """
    for _ in range(lifecycle.MAX_HISTORY + 20):
        claim(tmp_path, "42")
        issues.change(
            tmp_path,
            "codex",
            "release",
            "42",
            participants={"codex", "claude"},
        )
    record = issues.snapshot(tmp_path)["issues"]["42"]
    assert len(record["history"]) <= lifecycle.MAX_HISTORY + 3


def test_a_replayed_claim_omits_history_from_the_retained_result(tmp_path):
    first = issues.change(
        tmp_path,
        "codex",
        "claim",
        "42",
        participants={"codex", "claude"},
        key="claim-42",
    )
    assert first["history"]
    scoped = retries.scope("codex", "claim", "claim-42")
    stored = issues.snapshot(tmp_path)["retries"][scoped]["result"]
    assert "history" not in stored

    replayed = issues.change(
        tmp_path,
        "codex",
        "claim",
        "42",
        participants={"codex", "claude"},
        key="claim-42",
    )
    assert "history" not in replayed
    assert replayed["replayed"] is True
