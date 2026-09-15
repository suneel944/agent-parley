"""Checks the lane branch reader against its recorded and fallback paths."""

import subprocess
from pathlib import Path

import pytest

from agent_parley import checkpoints, cli


@pytest.fixture
def lane(paired):
    """Returns one prepared lane worktree."""
    return Path(paired["lanes"]["claude"])


def refuse_git(monkeypatch):
    """Fails the test if any code under check spawns a process."""

    def spawned(*args, **kwargs):
        raise AssertionError(f"Unexpected process spawn: {args}")

    monkeypatch.setattr(subprocess, "run", spawned)


def test_a_healthy_lane_reports_its_branch_without_spawning_git(
    lane, monkeypatch
):
    cli.git(lane, "checkout", "-b", "elsewhere")
    refuse_git(monkeypatch)
    assert checkpoints.lane_branch(lane) == "elsewhere"


def test_a_detached_lane_reports_the_marker_without_spawning_git(
    lane, monkeypatch
):
    cli.git(lane, "checkout", "--detach")
    refuse_git(monkeypatch)
    assert checkpoints.lane_branch(lane) == "<detached HEAD>"


def test_an_unreadable_worktree_reports_the_unavailable_marker(lane):
    (lane / ".git").write_text("gitdir: /nonexistent/worktrees/gone\n")
    assert checkpoints.recorded_branch(lane) == ""
    assert checkpoints.lane_branch(lane) == "an unavailable worktree"


def test_a_missing_worktree_reports_the_unavailable_marker(lane):
    assert checkpoints.lane_branch(lane / "absent") == "an unavailable worktree"


def test_an_unrecognised_head_defers_to_git(lane):
    marker = (lane / ".git").read_text().strip()
    directory = marker.removeprefix("gitdir:").strip()
    (Path(directory) / "HEAD").write_text("ref: refs/remotes/origin/main\n")
    assert checkpoints.recorded_branch(lane) == ""
