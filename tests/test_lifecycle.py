"""Durable authorized-work lifecycle coverage."""

import json
import shlex
import sys
from pathlib import Path

import pytest

from agent_parley import cli, issues, lifecycle, plan
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
    assert execution["state"] == lifecycle.READY
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
