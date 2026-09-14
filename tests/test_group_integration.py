"""Checks ordered integration of ready lanes and of one plan group."""

from pathlib import Path

import pytest

from agent_parley import plan
from agent_parley.cli import git
from agent_parley.state import BridgeError

PLAN = """
[plan]
name = "Parser rewrite"

[dependencies]
"43" = ["42"]

[groups]
rewrite = ["42", "43"]
"""


def written(directory, text=PLAN, name="group-plan.toml"):
    """Writes one plan file and returns its path."""
    path = directory / name
    path.write_text(text)
    return path


def identify(worktree):
    """Records a fixed committer identity in one checkout."""
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


def worked(bridge, paired, name, issue, filename):
    """Claims one issue, commits lane work and reports the lane ready."""
    lane = Path(paired["lanes"][name])
    bridge.issue(lane, "claim", issue)
    (lane / filename).write_text(f"{name} work\n")
    commit(lane, f"{name} work")
    bridge.report(lane, "ready", f"{name} finished", "", "make check")
    return lane


@pytest.fixture
def waiting(bridge, repo, paired):
    """Prepares two ready lanes whose issues record one dependency edge."""
    identify(repo)
    worked(bridge, paired, "claude", "42", "first.txt")
    codex = worked(bridge, paired, "codex", "43", "second.txt")
    bridge.issue(codex, "block", "43", on="42")
    return paired


def test_ready_lanes_integrate_in_dependency_order(bridge, repo, waiting):
    report = bridge.integrate(repo)

    assert "claude, codex" in report.splitlines()[0]
    assert "Integrated 2 of 2 lanes: claude, codex." in report
    assert (repo / "first.txt").exists()
    assert (repo / "second.txt").exists()
    subjects = git(repo, "log", "--pretty=%s").splitlines()
    first = subjects.index(f"Merge lane branch {waiting['branches']['claude']}")
    second = subjects.index(f"Merge lane branch {waiting['branches']['codex']}")
    assert second < first


def test_a_dependency_cycle_is_refused_and_named(bridge, repo, waiting):
    bridge.issue(Path(waiting["lanes"]["claude"]), "block", "42", on="43")

    with pytest.raises(BridgeError, match="form a cycle") as refusal:
        bridge.integrate(repo)
    assert "claude" in str(refusal.value)
    assert "codex" in str(refusal.value)
    assert git(repo, "log", "--pretty=%s").splitlines() == ["Initial fixture"]


def test_an_ordered_run_stops_at_the_first_refusal(bridge, repo, waiting):
    lane = Path(waiting["lanes"]["claude"])
    (lane / "draft.txt").write_text("unsaved\n")

    with pytest.raises(BridgeError, match="refused") as stop:
        bridge.integrate(repo)
    report = str(stop.value)
    assert "uncommitted changes" in report
    assert "codex: not attempted; it waits on claude." in report
    assert "Integrated 0 of 2 lanes: none." in report
    assert not (repo / "second.txt").exists()


def test_a_group_preflight_refuses_every_member_or_none(bridge, repo, waiting):
    bridge.work_plan(repo, "apply", written(repo.parent))
    lane = Path(waiting["lanes"]["codex"])
    (lane / "draft.txt").write_text("unsaved\n")

    with pytest.raises(BridgeError, match="refused as a whole") as refusal:
        bridge.integrate(repo, group="rewrite")
    report = str(refusal.value)
    assert "codex: " in report
    assert "admits every member or none" in report
    assert not (repo / "first.txt").exists()
    assert git(repo, "log", "--pretty=%s").splitlines() == ["Initial fixture"]


def test_a_group_integrates_its_members_in_dependency_order(
    bridge, repo, waiting
):
    bridge.work_plan(repo, "apply", written(repo.parent))

    report = bridge.integrate(repo, group="rewrite")

    assert report.splitlines()[0].startswith("Group rewrite: 2 lanes")
    assert "Integrated 2 of 2 lanes: claude, codex." in report
    assert (repo / "first.txt").exists()
    assert (repo / "second.txt").exists()


def test_a_group_preview_merges_nothing(bridge, repo, waiting):
    bridge.work_plan(repo, "apply", written(repo.parent))

    report = bridge.integrate(repo, group="rewrite", preview=True)

    assert "Preview only: nothing is merged" in report
    assert not (repo / "first.txt").exists()


def test_an_unclaimed_member_refuses_the_group(bridge, repo, paired):
    bridge.work_plan(repo, "apply", written(repo.parent))

    with pytest.raises(BridgeError, match="#42 is unclaimed"):
        bridge.integrate(repo, group="rewrite")


def test_a_ready_group_is_visible_before_anyone_merges(bridge, repo, waiting):
    bridge.work_plan(repo, "apply", written(repo.parent))
    directory = bridge.project(repo)[1]

    applied = bridge.work_plan(repo, "show")

    assert applied["ready_groups"] == ["rewrite"]
    assert "(every member reported ready)" in plan.render(applied)
    project = bridge.status_snapshot()["projects"][0]
    assert project["ready_groups"] == ["rewrite"]
    assert plan.groups(directory) == {"rewrite": ["42", "43"]}
