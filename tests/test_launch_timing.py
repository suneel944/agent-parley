"""Checks the launch timing each lane keeps for participant show."""

import os
import sys

from agent_parley import roster, views
from agent_parley.worktrees import launch_line, launched, preparation


def native(bridge, monkeypatch, tmp_path):
    """Puts a native claude CLI that exits at once ahead on the path."""
    binary = tmp_path / "bin"
    binary.mkdir()
    executable = binary / "claude"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(binary) + os.pathsep + os.environ["PATH"])
    monkeypatch.setattr(bridge, "up", lambda: None)

    async def identity(*args):
        return {"registration_token": "test-scoped-credential"}

    monkeypatch.setattr(bridge, "identity", identity)


def test_a_new_lane_records_the_split_to_its_cli_start(
    bridge, repo, monkeypatch, tmp_path
):
    native(bridge, monkeypatch, tmp_path)
    bridge.setup(repo)
    bridge.initialization(repo, f"{sys.executable} -c pass")
    assert bridge.launch("claude", repo, "Coordinate") == 0
    _, directory = bridge.project(repo)
    timing = roster.read(directory)["participants"]["claude"]["launch"]
    assert timing["worktree"] > 0
    assert timing["init"] > 0
    assert timing["start"] >= 0
    assert timing["total"] >= timing["worktree"] + timing["init"]
    assert launch_line(timing).startswith("Last launch: ")
    participant = roster.read(directory)["participants"]["claude"]
    assert views.participant_detail({}, participant)["launch_timing"] == (
        timing
    )


def test_a_later_launch_of_the_same_lane_pays_neither_step(
    bridge, repo, monkeypatch, tmp_path
):
    native(bridge, monkeypatch, tmp_path)
    bridge.setup(repo)
    bridge.initialization(repo, f"{sys.executable} -c pass")
    bridge.launch("claude", repo, "Coordinate")
    bridge.launch("claude", repo, "Coordinate")
    _, directory = bridge.project(repo)
    timing = roster.read(directory)["participants"]["claude"]["launch"]
    assert timing["worktree"] == 0
    assert timing["init"] == 0
    assert timing["start"] == timing["total"]


def test_only_an_uncompleted_preparation_counts_toward_a_launch():
    prepared = preparation(0.25, 2.0)
    assert prepared["start"] is None
    first = launched(prepared, 3.0)
    assert (first["worktree"], first["init"], first["start"]) == (
        0.25,
        2.0,
        0.75,
    )
    again = launched(first, 0.5)
    assert (again["worktree"], again["init"], again["start"]) == (
        0.0,
        0.0,
        0.5,
    )
    assert launched(None, 0.5)["start"] == 0.5


def test_no_line_is_shown_before_any_launch_completes():
    assert launch_line(None) == ""
    assert launch_line(preparation(0.1, 0.2)) == ""
