"""Checks the project policy that authorizes unattended integration."""

import json
import shlex
import sys
from pathlib import Path

import pytest

from agent_parley import (
    cli,
    issues,
    lifecycle,
    merges,
    metrics,
    roster,
    store,
    unattended,
)
from agent_parley.cli import git
from agent_parley.server import TOOLS
from agent_parley.state import BridgeError, lock, write_json


def commit(worktree: Path, name: str) -> str:
    """Records one file as a commit with a fixed identity."""
    (worktree / name).write_text(f"{name}\n")
    git(worktree, "add", name)
    git(
        worktree,
        "-c",
        "user.name=Bridge Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-m",
        f"Add {name}",
    )
    return git(worktree, "rev-parse", "HEAD")


def gate(tmp_path: Path, status: int = 0) -> str:
    """Writes a verification command that exits with the given status."""
    script = tmp_path / f"gate-{status}.sh"
    script.write_text(f"#!/bin/sh\necho gate ran\nexit {status}\n")
    script.chmod(0o755)
    return shlex.quote(str(script))


def decisions(directory: Path, name: str = "codex") -> list[dict]:
    """Returns the unattended decisions recorded for one lane."""
    return [
        record
        for record in metrics.report_records(directory, name)
        if record.get("kind") == unattended.KIND
    ]


def execution(directory: Path, number: str = "42") -> dict:
    """Reads one issue's normalized execution state."""
    return lifecycle.state(issues.snapshot(directory)["issues"][number])


@pytest.fixture
def ready(bridge, repo, paired, tmp_path, monkeypatch):
    """Leaves one lane claiming issue 42, committed and reported ready."""
    lane = Path(paired["lanes"]["codex"])
    monkeypatch.delenv(unattended.LANE_TOKEN, raising=False)
    monkeypatch.setattr(
        "agent_parley.cli.forge.issue_title", lambda *args: None
    )
    monkeypatch.setattr("agent_parley.cli.forge.assign", lambda *args: True)
    git(repo, "config", "user.name", "Bridge Test")
    git(repo, "config", "user.email", "test@example.com")
    bridge.verification(repo, gate(tmp_path))
    bridge.issue(lane, "claim", "42")
    source = commit(lane, "feature.txt")
    bridge.report(lane, "ready", "Lane result", "", "make check: 3 passed")
    return {
        "lane": lane,
        "directory": lane.parent,
        "source": source,
        "target": git(repo, "branch", "--show-current"),
    }


def authorize(bridge, repo, ready, *numbers):
    """Records the policy for the given issues on the fixture's target."""
    return unattended.configure(
        bridge, repo, ready["target"], list(numbers or ("42",))
    )


def test_without_a_policy_integration_stays_operator_only(bridge, repo, ready):
    directory = ready["directory"]
    assert roster.read(directory)["integration"] == {}
    assert "operator-only" in unattended.describe(bridge, repo)

    with pytest.raises(BridgeError, match="operator-only"):
        unattended.integrate(bridge, repo, "codex")

    assert not (repo / "feature.txt").exists()
    assert execution(directory)["state"] == lifecycle.READY
    [refusal] = decisions(directory)
    assert refusal["outcome"] == unattended.REFUSED
    assert "participant merge" in refusal["reason"]


def test_an_authorized_lane_is_merged_verified_and_completed(
    bridge, repo, ready
):
    directory = ready["directory"]
    authorize(bridge, repo, ready)
    base = git(repo, "rev-parse", "HEAD")

    message = unattended.integrate(bridge, repo, "codex")

    assert "Integrated unattended under integration.unattended" in message
    assert (repo / "feature.txt").exists()
    integrated = git(repo, "rev-parse", "HEAD")
    completed = execution(directory)
    assert completed["state"] == lifecycle.COMPLETE
    assert completed["integrated_commit"] == integrated
    assert completed["source_commit"] == ready["source"]
    [decision] = decisions(directory)
    assert decision["outcome"] == unattended.INTEGRATED
    evidence = decision["evidence"]
    assert evidence["claim_id"] == completed["claim_id"]
    assert evidence["source_commit"] == ready["source"]
    assert evidence["target"] == ready["target"]
    assert evidence["target_commit"] == base
    assert evidence["integrated_commit"] == integrated
    assert evidence["verify"].endswith("gate-0.sh")
    assert evidence["changed_paths"] == 1
    assert evidence["forge"] == "deferred to push"


def test_a_replay_merges_and_completes_nothing_again(bridge, repo, ready):
    directory = ready["directory"]
    authorize(bridge, repo, ready)
    unattended.integrate(bridge, repo, "codex")
    integrated = git(repo, "rev-parse", "HEAD")
    history = issues.snapshot(directory)["issues"]["42"]["history"]

    with pytest.raises(BridgeError):
        unattended.integrate(bridge, repo, "codex")

    assert git(repo, "rev-parse", "HEAD") == integrated
    assert issues.snapshot(directory)["issues"]["42"]["history"] == history
    assert [record["outcome"] for record in decisions(directory)] == [
        unattended.INTEGRATED,
        unattended.REFUSED,
    ]


def test_a_lane_holding_two_claims_integrates_its_ready_one(
    bridge, repo, ready
):
    directory = ready["directory"]
    authorize(bridge, repo, ready)
    bridge.issue(ready["lane"], "claim", "43")

    unattended.integrate(bridge, repo, "codex")

    assert (repo / "feature.txt").exists()
    assert execution(directory)["state"] == lifecycle.COMPLETE
    assert issues.snapshot(directory)["issues"]["43"]["owner"] == "codex"
    assert execution(directory, "43")["state"] != lifecycle.COMPLETE


def test_an_operator_merge_takes_the_only_ready_claim(bridge, repo, ready):
    directory = ready["directory"]
    bridge.issue(ready["lane"], "claim", "43")

    bridge.merge(repo, "codex")

    assert (repo / "feature.txt").exists()
    assert execution(directory)["state"] == lifecycle.COMPLETE
    assert execution(directory, "43")["state"] != lifecycle.COMPLETE


def test_two_ready_claims_need_an_explicit_issue(bridge, repo, ready):
    directory = ready["directory"]
    bridge.issue(ready["lane"], "claim", "43")
    commit(ready["lane"], "second.txt")
    bridge.report(
        ready["lane"], "ready", "Second", "", "make check: ok", issue="43"
    )

    with pytest.raises(BridgeError, match="#42, #43 are ready"):
        bridge.merge(repo, "codex")
    assert not (repo / "feature.txt").exists()

    bridge.merge(repo, "codex", issue="43")

    assert (repo / "second.txt").exists()
    assert execution(directory, "43")["state"] == lifecycle.COMPLETE
    assert execution(directory)["state"] == lifecycle.READY


def test_an_old_integration_never_stands_in_for_a_new_claim(
    bridge, repo, ready
):
    directory = ready["directory"]
    authorize(bridge, repo, ready)
    unattended.integrate(bridge, repo, "codex")
    unattended.configure(bridge, repo, ready["target"], [])
    bridge.issue(ready["lane"], "claim", "43")
    commit(ready["lane"], "second.txt")
    bridge.report(ready["lane"], "ready", "Second", "", "make check: ok")
    integrated = git(repo, "rev-parse", "HEAD")

    with pytest.raises(BridgeError, match="operator-only"):
        unattended.integrate(bridge, repo, "codex")

    assert git(repo, "rev-parse", "HEAD") == integrated
    assert not (repo / "second.txt").exists()
    assert execution(directory, "43")["state"] == lifecycle.READY
    last = decisions(directory)[-1]
    assert last["outcome"] == unattended.REFUSED
    assert last["evidence"]["forge"] == unattended.FORGE


def test_a_claim_changed_after_evaluation_merges_nothing(
    bridge, repo, ready, monkeypatch
):
    directory = ready["directory"]
    authorize(bridge, repo, ready)
    evaluate = unattended._evaluate

    def then_supersede(*arguments):
        outcome = evaluate(*arguments)
        monkeypatch.setattr(
            "agent_parley.cli.merge_claim",
            lambda directory, name, issue="": {
                "issue": 42,
                "claim_id": "superseded",
            },
        )
        return outcome

    monkeypatch.setattr(unattended, "_evaluate", then_supersede)

    with pytest.raises(BridgeError, match="changed after the integration"):
        unattended.integrate(bridge, repo, "codex")

    assert not (repo / "feature.txt").exists()
    assert execution(directory)["state"] == lifecycle.READY
    [failure] = decisions(directory)
    assert failure["outcome"] == unattended.FAILED


def test_an_unreadable_input_is_refused_and_recorded(
    bridge, repo, ready, monkeypatch
):
    directory = ready["directory"]
    authorize(bridge, repo, ready)

    def unreadable(*arguments):
        raise BridgeError("git rev-parse failed")

    monkeypatch.setattr("agent_parley.cli.current_branch", unreadable)

    with pytest.raises(BridgeError, match="could not be read"):
        unattended.integrate(bridge, repo, "codex")

    assert not (repo / "feature.txt").exists()
    [refusal] = decisions(directory)
    assert refusal["outcome"] == unattended.REFUSED
    assert "git rev-parse failed" in refusal["reason"]


def test_an_unlisted_issue_is_refused(bridge, repo, ready):
    authorize(bridge, repo, ready, "7")
    with pytest.raises(BridgeError, match="#42 is not listed"):
        unattended.integrate(bridge, repo, "codex")
    assert not (repo / "feature.txt").exists()


def test_a_commit_after_the_ready_report_is_refused(bridge, repo, ready):
    authorize(bridge, repo, ready)
    commit(ready["lane"], "later.txt")

    with pytest.raises(BridgeError, match="committed since"):
        unattended.integrate(bridge, repo, "codex")

    assert not (repo / "feature.txt").exists()
    [refusal] = decisions(ready["directory"])
    assert refusal["evidence"]["source_commit"] == ready["source"]


def test_a_base_checkout_off_the_target_branch_is_refused(bridge, repo, ready):
    authorize(bridge, repo, ready)
    git(repo, "checkout", "-b", "elsewhere")

    with pytest.raises(BridgeError, match="not the policy target"):
        unattended.integrate(bridge, repo, "codex")

    assert not (repo / "feature.txt").exists()


def test_a_moved_target_is_evaluated_afresh(bridge, repo, ready):
    directory = ready["directory"]
    authorize(bridge, repo, ready, "7")
    with pytest.raises(BridgeError, match="not listed"):
        unattended.integrate(bridge, repo, "codex")
    moved = commit(repo, "base.txt")
    authorize(bridge, repo, ready)

    unattended.integrate(bridge, repo, "codex")

    refusal, decision = decisions(directory)
    assert refusal["outcome"] == unattended.REFUSED
    assert decision["evidence"]["target_commit"] == moved
    assert git(repo, "rev-parse", "HEAD^1") == moved


def test_a_changed_claim_generation_is_refused(
    bridge, repo, ready, monkeypatch
):
    authorize(bridge, repo, ready)
    monkeypatch.setattr(
        "agent_parley.cli.merge_claim",
        lambda directory, name, issue="": {
            "issue": 42,
            "claim_id": "superseded",
        },
    )

    with pytest.raises(BridgeError, match="changed claim generation"):
        unattended.integrate(bridge, repo, "codex")

    assert not (repo / "feature.txt").exists()
    assert execution(ready["directory"])["state"] == lifecycle.READY


def test_an_incomplete_dependency_is_refused(bridge, repo, ready):
    directory = ready["directory"]
    authorize(bridge, repo, ready)
    ledger = issues.snapshot(directory)
    ledger["issues"]["42"]["blocked_by"] = ["17"]
    write_json(directory / "issues.json", ledger)

    with pytest.raises(BridgeError, match="still waits on #17"):
        unattended.integrate(bridge, repo, "codex")

    assert not (repo / "feature.txt").exists()


def test_a_peer_reservation_over_the_change_is_refused(bridge, repo, ready):
    authorize(bridge, repo, ready)
    root, _ = bridge.project(repo)
    store.initialize(bridge.home)
    token = store.register(bridge.home, str(root), "claude")
    peer = store.authenticate(bridge.home, token["registration_token"])
    assert peer is not None
    store.call(
        bridge.home, peer, "file_reservation_paths", {"paths": ["feature.txt"]}
    )

    with pytest.raises(BridgeError, match="peer reservation covers"):
        unattended.integrate(bridge, repo, "codex")

    assert not (repo / "feature.txt").exists()


def test_a_held_merge_lock_serializes_the_attempt(bridge, repo, ready):
    directory = ready["directory"]
    authorize(bridge, repo, ready)

    with lock(directory / "merge.lock"):
        with pytest.raises(BridgeError, match="Another merge"):
            unattended.integrate(bridge, repo, "codex")

    assert not (repo / "feature.txt").exists()
    assert execution(directory)["state"] == lifecycle.READY
    unattended.integrate(bridge, repo, "codex")
    assert execution(directory)["state"] == lifecycle.COMPLETE


def test_a_failing_gate_records_a_durable_failure(
    bridge, repo, ready, tmp_path
):
    directory = ready["directory"]
    authorize(bridge, repo, ready)
    bridge.verification(repo, gate(tmp_path, 3))

    with pytest.raises(BridgeError, match="recorded for recovery"):
        unattended.integrate(bridge, repo, "codex")

    assert not (repo / "feature.txt").exists()
    assert execution(directory)["state"] == lifecycle.READY
    [failure] = decisions(directory)
    assert failure["outcome"] == unattended.FAILED
    assert failure["evidence"]["source_commit"] == ready["source"]
    assert failure["reason"]


def test_an_unverified_base_is_refused_and_never_repaired(bridge, repo, ready):
    directory = ready["directory"]
    authorize(bridge, repo, ready)
    base = git(repo, "rev-parse", "HEAD")
    merges.record_integration(
        directory,
        {
            "lane": "claude",
            "branch": "bridge/claude",
            "issue": "",
            "claim_id": "",
            "source": base,
            "base": base,
            "result": base,
            "gate": "make check",
            "attempt": 1,
            "limit": merges.REPAIR_ATTEMPTS,
            "kind": merges.GATE_FAILED,
            "detail": "gate failed",
        },
    )

    with pytest.raises(BridgeError, match="never repairs"):
        unattended.integrate(bridge, repo, "codex")

    assert git(repo, "rev-parse", "HEAD") == base
    assert execution(directory)["state"] == lifecycle.READY
    assert merges.integration_record(directory)["attempt"] == 1
    [refusal] = decisions(directory)
    assert refusal["outcome"] == unattended.REFUSED
    assert "claude's integration" in refusal["reason"]


def test_an_unconfigured_gate_is_refused(bridge, repo, ready):
    authorize(bridge, repo, ready)
    bridge.verification(repo, "")

    with pytest.raises(BridgeError, match="no verification command"):
        unattended.integrate(bridge, repo, "codex")

    assert not (repo / "feature.txt").exists()


def test_a_session_lock_still_excludes_a_live_lane(bridge, repo, ready):
    directory = ready["directory"]
    authorize(bridge, repo, ready)

    with lock(directory / "codex.session.lock"):
        with pytest.raises(BridgeError, match="recorded for recovery"):
            unattended.integrate(bridge, repo, "codex")

    assert not (repo / "feature.txt").exists()
    assert decisions(directory)[-1]["outcome"] == unattended.FAILED


@pytest.mark.parametrize(
    "recorded",
    (
        {"unattended": {"target": "main", "issues": [42]}},
        {"unattended": {"target": "main", "issues": []}},
        {"unattended": {"target": "main"}},
        {"unattended": {"target": "../main", "issues": ["42"]}},
        {"unattended": {"target": "main", "issues": ["42", "42"]}},
        {"unattended": True},
        {"widen": {}},
        ["42"],
    ),
)
def test_an_invalid_policy_is_refused_not_read_as_consent(
    bridge, repo, ready, recorded
):
    directory = ready["directory"]
    manifest = roster.read(directory)
    manifest["integration"] = recorded
    write_json(directory / "project.json", manifest)

    assert roster.read(directory)["participants"]
    with pytest.raises(BridgeError, match="Invalid integration policy"):
        unattended.integrate(bridge, repo, "codex")
    with pytest.raises(BridgeError, match="Invalid integration policy"):
        unattended.describe(bridge, repo)
    assert not (repo / "feature.txt").exists()


def test_a_lane_cannot_set_or_run_the_policy(bridge, repo, ready):
    lane = ready["lane"]
    with pytest.raises(BridgeError, match="never from an assigned worktree"):
        unattended.configure(bridge, lane, ready["target"], ["42"])
    authorize(bridge, repo, ready)
    with pytest.raises(BridgeError, match="never from an assigned worktree"):
        unattended.configure(bridge, lane, ready["target"], ["42", "43"])
    with pytest.raises(BridgeError, match="never from an assigned worktree"):
        unattended.integrate(bridge, lane, "codex")
    assert unattended.policy(roster.read(ready["directory"])) == {
        "target": ready["target"],
        "issues": ["42"],
    }


def test_a_lane_token_in_the_base_checkout_is_refused(
    bridge, repo, ready, monkeypatch
):
    authorize(bridge, repo, ready)
    monkeypatch.setenv(unattended.LANE_TOKEN, "lane-token")

    with pytest.raises(BridgeError, match="AGENT_PARLEY_TOKEN"):
        unattended.configure(bridge, repo, ready["target"], ["42", "43"])
    with pytest.raises(BridgeError, match="AGENT_PARLEY_TOKEN"):
        unattended.integrate(bridge, repo, "codex")

    assert not (repo / "feature.txt").exists()
    assert unattended.policy(roster.read(ready["directory"]))["issues"] == [
        "42"
    ]


def test_served_tools_cannot_alter_the_policy(bridge, repo, ready):
    for tool in TOOLS:
        assert "unattended" not in json.dumps(tool).lower()
        named = {tool["name"], *tool["inputSchema"]["properties"]}
        assert not named & {"integration", "target", "issues", "policy"}
    directory = ready["directory"]
    authorize(bridge, repo, ready)
    before = (directory / "project.json").read_text()
    root, _ = bridge.project(repo)
    store.initialize(bridge.home)
    token = store.register(bridge.home, str(root), "codex")
    lane = store.authenticate(bridge.home, token["registration_token"])
    assert lane is not None
    for name, arguments in (
        ("file_reservation_paths", {"paths": ["project.json"]}),
        ("list_participants", {}),
        ("next_issues", {}),
        ("release_file_reservations", {}),
    ):
        store.call(bridge.home, lane, name, arguments)
    assert (directory / "project.json").read_text() == before


def test_the_command_line_sets_shows_and_clears_the_policy(
    bridge, repo, ready, monkeypatch, capsys
):
    def run(*arguments):
        monkeypatch.setattr(
            sys,
            "argv",
            ["agent-parley", "--home", str(bridge.home), *arguments],
        )
        assert cli.main() == 0
        return capsys.readouterr().out

    shown = run(
        "unattended",
        "set",
        "#42",
        "9",
        "--target",
        ready["target"],
        "--repo",
        str(repo),
    )
    assert f"into {ready['target']} for issues #9, #42" in shown
    assert "#9, #42" in run("unattended", "show", "--repo", str(repo))
    assert "Integrated unattended" in run(
        "unattended", "run", "codex", "--issue", "42", "--repo", str(repo)
    )
    assert "operator-only" in run("unattended", "set", "--repo", str(repo))
    assert roster.read(ready["directory"])["integration"] == {}
