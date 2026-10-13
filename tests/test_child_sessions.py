"""Checks that a native session a lane starts through its shell is seen.

A lane can run a project's own tooling from its shell, and that tooling can
add a further Git worktree and start a native session inside it. Agent
Parley never launches that session and never sees it start, but the
worktree is one Git already registers against the project repository, so it
can be attributed to the lane that made it the same way `reclaim` already
attributes stray worktrees: by path, or by a branch named after the lane.
"""

import json
import re
import time
from pathlib import Path

import pytest

from agent_parley import budgets, problems, reclaim, roster
from agent_parley.cli import git


def made(repo, directory, name):
    """Adds a worktree the way a lane does for a sub-task."""
    path = directory / name
    git(repo, "worktree", "add", "-b", name, str(path))
    return path


def usage_record(identifier, tokens):
    """Builds one assistant transcript record reporting its own usage."""
    return json.dumps(
        {
            "type": "assistant",
            "message": {
                "id": identifier,
                "usage": {
                    "input_tokens": tokens,
                    "cache_creation_input_tokens": tokens * 2,
                    "cache_read_input_tokens": tokens * 3,
                    "output_tokens": tokens * 4,
                },
            },
        }
    )


def claude_transcript(config, lane, lines):
    """Writes a Claude transcript where that client would keep one."""
    directory = (
        Path(config) / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(lane))
    )
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "session.jsonl"
    path.write_text("\n".join(lines) + "\n")
    return path


@pytest.fixture
def served(monkeypatch):
    """Reports the coordination service as up without starting it."""
    from agent_parley import cli

    original = cli.Bridge.status_snapshot

    def ready(self):
        report = original(self)
        report["server"]["ready"] = True
        return report

    monkeypatch.setattr(cli.Bridge, "status_snapshot", ready)


def test_a_worktree_a_lane_made_is_attributed_to_it(bridge, repo, paired):
    directory = bridge.project(repo)[1]
    child = made(repo, directory, "claude-subtask")
    manifest = roster.read(directory)
    assert reclaim.child_worktrees(directory, manifest, "claude") == [child]
    assert reclaim.child_worktrees(directory, manifest, "codex") == []
    assert reclaim.children_by_lane(manifest) == {"claude": [child]}


def test_problems_lists_the_registered_worktrees_once_per_project(
    bridge, repo, paired, served, monkeypatch
):
    directory = bridge.project(repo)[1]
    made(repo, directory, "claude-subtask")
    listed = []
    original = reclaim._entries

    def counted(root):
        listed.append(root)
        return original(root)

    monkeypatch.setattr(reclaim, "_entries", counted)
    bridge.problems()
    assert len(roster.read(directory)["participants"]) == 2
    assert len(listed) == 1


def test_a_session_in_a_worktree_a_lane_made_counts_toward_its_budget(
    bridge, repo, paired, tmp_path
):
    directory = bridge.project(repo)[1]
    bridge.budget(repo, "participant", "claude", {"tokens": 100000})
    child = made(repo, directory, "claude-subtask")
    claude_transcript(
        tmp_path / "claude_config_dir", child, [usage_record("msg_a", 20)]
    )
    data = roster.read(directory)
    reading = budgets.report(bridge.home, directory, data, "claude", {})
    assert reading["used"]["tokens"] == 200


def test_a_child_session_still_active_after_the_claim_ends_is_reported(
    bridge, repo, paired, served, tmp_path
):
    directory = bridge.project(repo)[1]
    child = made(repo, directory, "claude-subtask")
    claude_transcript(
        tmp_path / "claude_config_dir", child, [usage_record("msg_a", 5)]
    )

    found = bridge.problems()

    [row] = [row for row in found if row["condition"] == problems.CHILD_SESSION]
    assert row["participant"] == "claude"
    assert "1 native session" in row["detail"]
    assert row["seconds"] < problems.CHILD_RECENT


def test_a_stale_child_session_is_not_reported(
    bridge, repo, paired, served, tmp_path
):
    directory = bridge.project(repo)[1]
    child = made(repo, directory, "claude-subtask")
    path = claude_transcript(
        tmp_path / "claude_config_dir", child, [usage_record("msg_a", 5)]
    )
    old = time.time() - problems.CHILD_RECENT - 3600
    import os

    os.utime(path, (old, old))

    found = bridge.problems()

    assert not [
        row for row in found if row["condition"] == problems.CHILD_SESSION
    ]
