"""Checks that reclaiming a lane removes landed work and nothing else."""

import time
from pathlib import Path

import pytest

from agent_parley import forge, issues, reclaim, roster, store, supervision
from agent_parley.cli import git


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


def completion(monkeypatch, state):
    """Reports one fixed pull-request observation to the sweep."""
    monkeypatch.setattr(
        forge,
        "branch_completion",
        lambda *args: None if state is None else (state, time.time()),
    )


def branches(repo):
    """Lists the branch names the base checkout still carries."""
    return set(
        git(
            repo, "for-each-ref", "--format=%(refname:short)", "refs/heads"
        ).splitlines()
    )


def row_of(rows, name):
    """Returns the assessment recorded for one participant."""
    return next(row for row in rows if row["participant"] == name)


@pytest.fixture
def landed(bridge, repo, paired):
    """Commits work in the Claude lane and merges it into the base."""
    directory = bridge.project(repo, create=False)[1]
    manifest = roster.read(directory)
    lane = Path(paired["lanes"]["claude"])
    (lane / "feature.txt").write_text("lane work\n")
    commit(lane, "Lane work")
    git(
        repo,
        "-c",
        "user.name=Bridge Test",
        "-c",
        "user.email=test@example.com",
        "merge",
        "--no-ff",
        "-m",
        "Merge lane",
        manifest["participants"]["claude"]["branch"],
    )
    return {
        "directory": directory,
        "lane": lane,
        "branch": manifest["participants"]["claude"]["branch"],
        "base": manifest["base"],
    }


@pytest.fixture
def remote(repo, tmp_path):
    """Publishes the base branch to a temporary upstream repository."""
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", str(origin))
    git(repo, "remote", "add", "origin", str(origin))
    return origin


def test_a_merged_lane_is_reclaimed_with_its_worktree_and_branch(
    bridge, repo, landed, monkeypatch
):
    completion(monkeypatch, "MERGED")

    swept = bridge.reclaim(repo, apply=True)

    assert row_of(swept, "claude")["reason"] == reclaim.MERGED
    assert row_of(swept, "claude")["removed"] is True
    assert not landed["lane"].exists()
    assert landed["branch"] not in branches(repo)
    assert "claude" not in roster.read(landed["directory"])["participants"]


def test_a_lane_still_at_the_project_base_is_kept(
    bridge, repo, landed, monkeypatch
):
    completion(monkeypatch, "MERGED")

    swept = bridge.reclaim(repo, apply=True)

    codex = roster.read(landed["directory"])["participants"]["codex"]
    assert row_of(swept, "codex")["reason"] == reclaim.UNUSED
    assert Path(codex["lane"]).exists()


def test_uncommitted_changes_keep_the_lane_and_name_the_files(
    bridge, repo, landed, monkeypatch
):
    (landed["lane"] / "unsaved.txt").write_text("not committed\n")
    completion(monkeypatch, "MERGED")

    swept = bridge.reclaim(repo, apply=True)

    kept = row_of(swept, "claude")
    assert kept["reclaim"] is False
    assert kept["reason"] == reclaim.UNCOMMITTED
    assert kept["paths"] == ["unsaved.txt"]
    assert landed["lane"].exists()
    assert landed["branch"] in branches(repo)


def test_unpushed_commits_keep_the_lane_and_name_them(
    bridge, repo, paired, remote, monkeypatch
):
    directory = bridge.project(repo, create=False)[1]
    manifest = roster.read(directory)
    branch = manifest["participants"]["claude"]["branch"]
    lane = Path(paired["lanes"]["claude"])
    git(repo, "push", "--set-upstream", "origin", branch)
    (lane / "feature.txt").write_text("lane work\n")
    commit(lane, "Lane work")
    git(
        repo,
        "-c",
        "user.name=Bridge Test",
        "-c",
        "user.email=test@example.com",
        "merge",
        "--no-ff",
        "-m",
        "Merge lane",
        branch,
    )
    completion(monkeypatch, "MERGED")

    swept = bridge.reclaim(repo, apply=True)

    kept = row_of(swept, "claude")
    assert kept["reclaim"] is False
    assert kept["reason"] == reclaim.UNPUSHED
    assert kept["paths"] and "Lane work" in kept["paths"][0]
    assert lane.exists()
    assert branch in branches(repo)


def test_a_closed_unmerged_pull_request_deletes_nothing(
    bridge, repo, landed, monkeypatch
):
    completion(monkeypatch, "CLOSED")

    swept = bridge.reclaim(repo, apply=True)

    kept = row_of(swept, "claude")
    assert kept["reclaim"] is False
    assert kept["reason"] == reclaim.PULL_CLOSED
    assert landed["lane"].exists()
    assert landed["branch"] in branches(repo)
    assert "claude" in roster.read(landed["directory"])["participants"]


def test_an_open_pull_request_deletes_nothing(
    bridge, repo, landed, monkeypatch
):
    completion(monkeypatch, "OPEN")

    swept = bridge.reclaim(repo, apply=True)

    assert row_of(swept, "claude")["reason"] == reclaim.PULL_OPEN
    assert landed["lane"].exists()


def test_an_unreachable_forge_deletes_nothing(
    bridge, repo, landed, monkeypatch
):
    completion(monkeypatch, None)

    swept = bridge.reclaim(repo, apply=True)

    assert row_of(swept, "claude")["reason"] == reclaim.UNLANDED
    assert landed["lane"].exists()
    assert landed["branch"] in branches(repo)


def test_a_branch_gone_from_its_upstream_is_reclaimed(
    bridge, repo, landed, remote, monkeypatch
):
    git(repo, "push", "--set-upstream", "origin", landed["branch"])
    git(remote, "update-ref", "-d", f"refs/heads/{landed['branch']}")
    completion(monkeypatch, None)

    swept = bridge.reclaim(repo, apply=True)

    assert row_of(swept, "claude")["reason"] == reclaim.GONE
    assert not landed["lane"].exists()
    assert landed["branch"] not in branches(repo)


def test_an_upstream_that_still_carries_the_branch_deletes_nothing(
    bridge, repo, landed, remote, monkeypatch
):
    git(repo, "push", "--set-upstream", "origin", landed["branch"])
    completion(monkeypatch, None)

    swept = bridge.reclaim(repo, apply=True)

    assert row_of(swept, "claude")["reason"] == reclaim.PUBLISHED
    assert landed["lane"].exists()


def test_a_held_claim_keeps_the_lane(bridge, repo, landed, monkeypatch):
    bridge.issue(landed["lane"], "claim", "1")
    completion(monkeypatch, "MERGED")

    swept = bridge.reclaim(repo, apply=True)

    assert row_of(swept, "claude")["reason"] == reclaim.CLAIMED
    assert landed["lane"].exists()
    assert issues.snapshot(landed["directory"])["issues"]["1"]["owner"] == (
        "claude"
    )


def test_a_report_without_apply_removes_nothing(
    bridge, repo, landed, monkeypatch
):
    completion(monkeypatch, "MERGED")

    swept = bridge.reclaim(repo)

    assert row_of(swept, "claude")["reclaim"] is True
    assert "removed" not in row_of(swept, "claude")
    assert landed["lane"].exists()
    assert landed["branch"] in branches(repo)


def test_a_lane_outside_the_state_directory_is_never_touched(
    bridge, repo, landed, tmp_path, monkeypatch
):
    outside = tmp_path / "elsewhere"
    manifest = roster.read(landed["directory"])
    manifest["participants"]["claude"]["lane"] = str(outside)
    completion(monkeypatch, "MERGED")

    assessed = reclaim.assess(
        landed["directory"],
        manifest,
        "claude",
        registered=set(),
        claimed=False,
        busy=False,
    )

    assert assessed["reclaim"] is False
    assert assessed["reason"] == reclaim.OUTSIDE


def test_the_supervision_tick_reclaims_a_merged_lane(
    bridge, repo, landed, monkeypatch
):
    store.initialize(bridge.home)
    completion(monkeypatch, "MERGED")

    supervision.poll(bridge.home, landed["directory"])

    assert not landed["lane"].exists()
    assert landed["branch"] not in branches(repo)
    assert not supervision.reclaim_due(landed["directory"], time.time(), 900)


def test_the_supervision_tick_sweeps_once_an_interval(
    bridge, repo, landed, monkeypatch
):
    store.initialize(bridge.home)
    completion(monkeypatch, "MERGED")
    supervision.poll(bridge.home, landed["directory"])
    sweeps = []

    def counted(*args):
        """Records that a second sweep planned anything at all."""
        sweeps.append(1)
        return []

    monkeypatch.setattr(reclaim, "plan", counted)

    supervision.poll(bridge.home, landed["directory"])

    assert sweeps == []
