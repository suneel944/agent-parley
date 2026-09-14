"""Verifies the recorded approval integration commands can require."""

import json
from pathlib import Path

import pytest

from agent_parley import approvals, dashboard, history, metrics, roster
from agent_parley.checkpoints import branch_head
from agent_parley.cli import git
from agent_parley.state import BridgeError


def identify(worktree):
    """Gives a fixture repository the identity a merge commit needs."""
    git(worktree, "config", "user.name", "Bridge Test")
    git(worktree, "config", "user.email", "test@example.com")


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


def reported(directory, name):
    """Returns the identifier of a lane's latest ready report."""
    return [
        record["id"]
        for record in metrics.report_records(directory, name)
        if record.get("kind") == "report" and record.get("state") == "ready"
    ][-1]


@pytest.fixture
def awaiting(bridge, repo, paired):
    """Leaves one lane ready to integrate and one decision short of it."""
    identify(repo)
    lane = Path(paired["lanes"]["codex"])
    bridge.approval_policy(repo, ["merge", "pr"])
    (lane / "feature.txt").write_text("lane work\n")
    commit(lane, "Lane work")
    bridge.report(lane, "ready", "Engine built", "", "12 tests passed")
    return {"lane": lane, "directory": lane.parent}


def test_merge_is_refused_until_the_operator_records_an_approval(
    bridge, repo, paired, awaiting
):
    with pytest.raises(BridgeError) as refused:
        bridge.merge(repo, "codex")
    message = str(refused.value)
    assert reported(awaiting["directory"], "codex") in message
    assert "agent-parley approve codex" in message
    assert "participant merge" in message

    with pytest.raises(BridgeError, match="participant pr"):
        bridge.pull_request(repo, "codex")

    granted = bridge.approve(repo, "codex")
    assert "not a verification of the code" in granted
    assert "Merged" in bridge.merge(repo, "codex")


def test_a_lane_cannot_record_the_approval_of_its_own_work(
    bridge, repo, paired, awaiting
):
    with pytest.raises(BridgeError, match="base checkout"):
        bridge.approve(awaiting["lane"], "codex")
    with pytest.raises(BridgeError, match="approve codex"):
        bridge.merge(repo, "codex")


def test_an_approval_of_another_lane_or_branch_merges_nothing(
    bridge, repo, paired, awaiting
):
    claude = Path(paired["lanes"]["claude"])
    (claude / "other.txt").write_text("other work\n")
    commit(claude, "Other work")
    bridge.report(claude, "ready", "Other built", "", "4 tests passed")
    bridge.approve(repo, "claude")

    with pytest.raises(BridgeError, match="approve codex"):
        bridge.merge(repo, "codex")

    directory = awaiting["directory"]
    data = roster.read(directory)
    head = branch_head(
        Path(data["root"]), data["participants"]["codex"]["branch"]
    )
    binding = approvals.binding(
        data, "codex", head, reported(directory, "codex")
    )
    metrics.record_report(
        directory,
        "codex",
        {
            "kind": "approval",
            "decision": "approved",
            "operator": "someone",
            "reason": "",
            "binding": {**binding, "branch": "refs/heads/somewhere-else"},
        },
    )
    with pytest.raises(BridgeError, match="targets a different branch"):
        bridge.merge(repo, "codex")


def test_new_commits_and_changed_policy_each_need_a_new_approval(
    bridge, repo, paired, awaiting
):
    bridge.approve(repo, "codex")
    (awaiting["lane"] / "more.txt").write_text("later work\n")
    commit(awaiting["lane"], "Later work")
    with pytest.raises(BridgeError, match="committed since"):
        bridge.merge(repo, "codex")

    bridge.approve(repo, "codex")
    bridge.verification(repo, "true")
    with pytest.raises(BridgeError, match="verification command"):
        bridge.merge(repo, "codex")

    bridge.approve(repo, "codex")
    assert "Merged" in bridge.merge(repo, "codex")


def test_a_further_report_invalidates_the_approval_it_was_given_for(
    bridge, repo, paired, awaiting
):
    bridge.approve(repo, "codex")
    bridge.report(
        awaiting["lane"], "ready", "Engine rebuilt", "", "13 tests passed"
    )
    with pytest.raises(BridgeError, match="earlier report"):
        bridge.merge(repo, "codex")


def test_a_rejection_refuses_integration_and_tells_the_lane_why(
    bridge, repo, paired, awaiting
):
    assert "Rejected" in bridge.reject(repo, "codex", "Needs a test")
    with pytest.raises(BridgeError, match="Needs a test"):
        bridge.merge(repo, "codex")
    chain = history.records(
        bridge.home,
        awaiting["directory"],
        roster.read(awaiting["directory"]),
        kinds=("approval",),
    )
    assert chain[0]["action"] == "rejected"
    assert "Needs a test" in chain[0]["detail"]


def test_an_unreadable_decision_log_refuses_rather_than_permits(
    bridge, repo, paired, awaiting
):
    bridge.approve(repo, "codex")
    path = metrics.report_path(awaiting["directory"], "codex")
    with path.open("a", encoding="utf-8") as stream:
        stream.write("{ this record is damaged\n")
    with pytest.raises(BridgeError, match="cannot be read"):
        bridge.merge(repo, "codex")
    with pytest.raises(BridgeError, match="cannot be read"):
        bridge.pull_request(repo, "codex")


def test_approval_state_is_visible_in_status_and_the_live_view(
    bridge, repo, paired, awaiting
):
    lanes = {
        record["participant"]: record
        for record in bridge.status_snapshot()["projects"][0]["participants"]
    }
    assert lanes["codex"]["approval"]["state"] == "awaiting approval"
    assert lanes["codex"]["approval"]["report"] == reported(
        awaiting["directory"], "codex"
    )
    view = dashboard.collect(bridge.home, False, {})
    assert view["totals"]["awaiting_approval"] == 1
    assert any("awaiting approval 1" in line for line in dashboard.render(view))

    bridge.approve(repo, "codex")
    lanes = {
        record["participant"]: record
        for record in bridge.status_snapshot()["projects"][0]["participants"]
    }
    assert lanes["codex"]["approval"]["state"] == "approved"
    assert (
        dashboard.collect(bridge.home, False, {})["totals"]["awaiting_approval"]
        == 0
    )


def test_a_project_without_the_requirement_integrates_as_before(
    bridge, repo, paired
):
    identify(repo)
    lane = Path(paired["lanes"]["codex"])
    (lane / "feature.txt").write_text("lane work\n")
    commit(lane, "Lane work")
    assert roster.read(lane.parent)["approval"] == []
    assert "Merged" in bridge.merge(repo, "codex")


def test_the_requirement_is_reported_and_validated(bridge, repo, paired):
    assert "no recorded operator approval" in bridge.approval_policy(repo)
    assert "participant merge" in bridge.approval_policy(repo, ["merge"])
    with pytest.raises(BridgeError, match="Approval steps"):
        bridge.approval_policy(repo, ["release"])
    stored = json.loads(
        (Path(paired["lanes"]["codex"]).parent / "project.json").read_text()
    )
    assert stored["approval"] == ["merge"]
