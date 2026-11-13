"""Covers one lane working in worktrees of two temporary repositories."""

import json
import time
from pathlib import Path

import pytest

from agent_parley import (
    checkpoints,
    context,
    forge,
    issues,
    roster,
    store,
    supervision,
)
from agent_parley.cli import git
from agent_parley.state import BridgeError, write_json


@pytest.fixture
def sibling(tmp_path):
    """Creates the second committed repository a project links."""
    path = tmp_path / "sibling repo"
    path.mkdir()
    git(path, "init")
    (path / "shared.txt").write_text("sibling\n")
    git(path, "add", "shared.txt")
    git(path, "commit", "-m", "Sibling fixture")
    return path


@pytest.fixture
def actors(bridge, repo, paired, sibling):
    """Links the sibling and registers both lanes with the store."""
    bridge.link(repo, sibling, "web")
    store.initialize(bridge.home)
    return {
        name: store.authenticate(
            bridge.home,
            store.register(bridge.home, paired["root"], name)[
                "registration_token"
            ],
        )
        for name in ("claude", "codex")
    }


def lane(paired, name="claude"):
    """Names one fixture lane's own worktree."""
    return Path(paired["lanes"][name])


def participant(paired, name="claude"):
    """Reads one participant record from the manifest."""
    return roster.read(lane(paired).parent)["participants"][name]


def claim(bridge, paired):
    """Claims issue 1 for the Claude lane with the sibling linked."""
    return Path(
        bridge.issue(lane(paired), "claim", "1", links=["web"])["linked"]["web"]
    )


def reserve(bridge, actor, *paths):
    """Asks the store for advisory reservations on some keys."""
    return store.call(
        bridge.home, actor, "file_reservation_paths", {"paths": list(paths)}
    )["conflicts"]


def test_a_link_is_recorded_outside_both_repositories(
    bridge, repo, actors, sibling
):
    assert "web" in bridge.link(repo)
    assert roster.read(lane_parent(bridge, repo))["links"] == {
        "web": str(sibling)
    }
    assert git(sibling, "status", "--porcelain") == ""
    assert git(repo, "status", "--porcelain") == ""
    with pytest.raises(BridgeError, match="own repository"):
        bridge.link(repo, repo)
    with pytest.raises(BridgeError, match="links no repository named api"):
        bridge.decide(repo, "Use the v2 schema.", link="api")


def lane_parent(bridge, repo):
    """Names the project state directory of the fixture repository."""
    return bridge.project(repo, create=False)[1]


def test_a_claim_adds_a_worktree_on_the_lane_branch(
    bridge, repo, paired, actors, sibling
):
    before = participant(paired)
    worktree = claim(bridge, paired)
    after = participant(paired)
    assert worktree.is_dir()
    assert worktree.parent.parent == lane(paired).parent
    assert not worktree.is_relative_to(repo)
    assert not worktree.is_relative_to(sibling)
    assert git(worktree, "branch", "--show-current") == before["branch"]
    assert after["linked"]["web"] == {
        "lane": str(worktree),
        "branch": before["branch"],
        "base": git(sibling, "rev-parse", "HEAD"),
    }
    assert {k: v for k, v in after.items() if k != "linked"} == before
    assert git(sibling, "status", "--porcelain") == ""
    with pytest.raises(BridgeError, match="links no repository named api"):
        bridge.issue(lane(paired, "codex"), "claim", "2", links=["api"])


def test_reservations_are_per_repository(bridge, paired, actors):
    claim(bridge, paired)
    assert not reserve(bridge, actors["codex"], "shared.txt")
    assert not reserve(bridge, actors["claude"], "web//./shared.txt")
    assert reserve(bridge, actors["claude"], "shared.txt")
    assert reserve(bridge, actors["codex"], "web//*")
    assert not reserve(bridge, actors["codex"], "*.md")


def test_hooks_guard_the_branch_in_each_worktree(bridge, paired, actors):
    worktree = claim(bridge, paired)
    directory = lane(paired).parent
    write_json(directory / "claude-identity.json", {"name": "claude"})
    for cwd, command in (
        (worktree, "git switch main"),
        (lane(paired), f"git -C '{worktree}' switch main"),
    ):
        refused = checkpoints.lane_refusal(
            directory,
            "claude",
            {
                "hook_event_name": "PreToolUse",
                "cwd": str(cwd),
                "tool_input": {"command": command},
            },
        )
        assert refused[1] is checkpoints.Reason.BRANCH_SWITCH
    git(worktree, "switch", "-c", "elsewhere")
    output = checkpoints.checkpoint(
        bridge.home,
        directory,
        "claude",
        {
            "hook_event_name": "PreToolUse",
            "cwd": str(lane(paired)),
            "tool_input": {},
        },
    )
    reason = output["hookSpecificOutput"]["permissionDecisionReason"]
    assert "expected" in reason
    assert f"git -C {worktree} switch" in reason


def unresolved(bridge, paired, monkeypatch, number="1"):
    """Drives the supervisor until the claim reads as unresolved."""
    monkeypatch.setattr(
        supervision.forge,
        "branch_completion",
        lambda *args: ("MERGED", time.time() + 1),
    )
    path = lane(paired).parent / "issues.json"
    supervision.poll(bridge.home, path.parent)
    for _ in range(2):
        ledger = json.loads(path.read_text())
        ledger["issues"][number]["handoff_prompt"]["created"] -= (
            supervision.DEFAULTS["inactive_after"]
        )
        path.write_text(json.dumps(ledger))
        supervision.poll(bridge.home, path.parent)


def evidence(monkeypatch, states):
    """Answers forge evidence per repository from a mutable state map."""
    monkeypatch.setattr(
        forge,
        "branch_evidence",
        lambda root, branch: (
            None
            if states[str(root)] is None
            else {
                "branch": branch,
                "state": states[str(root)],
                "created_at": time.time(),
                "commit": "abcdef1234567",
                "pull_request": 7,
                "url": "https://example.invalid/pull/7",
            }
        ),
    )


def test_resolve_requires_each_changed_repository(
    bridge, repo, paired, actors, sibling, monkeypatch
):
    worktree = claim(bridge, paired)
    (worktree / "schema.txt").write_text("v2\n")
    git(worktree, "add", "schema.txt")
    git(worktree, "commit", "-m", "Schema v2")
    unresolved(bridge, paired, monkeypatch)
    states = {str(repo): "MERGED", str(sibling): None}
    evidence(monkeypatch, states)
    with pytest.raises(BridgeError, match="claude in web"):
        bridge.issue_resolve(repo, "1")
    states[str(sibling)] = "CLOSED"
    with pytest.raises(BridgeError, match="--release"):
        bridge.issue_resolve(repo, "1")
    states[str(sibling)] = "MERGED"
    result = bridge.issue_resolve(repo, "1")
    assert result["outcome"] == "complete"
    assert result["evidence"]["repositories"][0]["repository"] == "web"
    assert result["evidence"]["state"] == "MERGED"


def test_release_keeps_the_worktree_and_views_list_it(
    bridge, repo, paired, actors, sibling, capsys
):
    worktree = claim(bridge, paired)
    assert not reserve(bridge, actors["claude"], "web//a")
    bridge.issue(lane(paired), "release", "1")
    ledger = issues.snapshot(lane(paired).parent)["issues"]["1"]
    assert ledger.get("owner") is None
    assert worktree.is_dir()
    location = context.lane_location(participant(paired))
    assert location["linked_worktrees"] == {"web": str(worktree)}
    assert f"web:{worktree}" in context.location_cell(location)
    assert "linked_worktrees" not in context.lane_location(
        participant(paired, "codex")
    )
    bridge.status()
    assert "claude worktrees: " in capsys.readouterr().out
    branch = participant(paired)["branch"]
    assert "no longer links web" in bridge.unlink(repo, "web")
    assert not worktree.exists()
    assert "linked" not in participant(paired)
    assert git(sibling, "branch", "--list", branch)


def test_retire_removes_the_linked_worktrees(
    bridge, repo, paired, actors, sibling
):
    worktree = claim(bridge, paired)
    bridge.retire(repo, "claude")
    assert not worktree.exists()
    assert str(worktree) not in git(sibling, "worktree", "list")


@pytest.mark.parametrize("removal", ["retire", "unlink"])
def test_linked_work_refuses_its_removal(
    bridge, repo, paired, actors, sibling, removal
):
    worktree = claim(bridge, paired)

    def remove():
        """Runs the removal under test."""
        if removal == "retire":
            return bridge.retire(repo, "claude")
        return bridge.unlink(repo, "web")

    (worktree / "draft.txt").write_text("draft\n")
    with pytest.raises(BridgeError, match="claude in web has uncommitted"):
        remove()
    (worktree / "draft.txt").unlink()
    (sibling / ".git" / "info" / "exclude").write_text("*.env\n")
    (worktree / "local.env").write_text("TOKEN=1\n")
    with pytest.raises(BridgeError, match=r"ignored files .*local\.env"):
        remove()
    assert (worktree / "local.env").is_file()
    assert lane(paired).is_dir()
    assert participant(paired)["linked"]["web"]["lane"] == str(worktree)


def test_retire_deletes_linked_ignored_files_it_was_told_to(
    bridge, repo, paired, actors, sibling
):
    worktree = claim(bridge, paired)
    (sibling / ".git" / "info" / "exclude").write_text("*.env\n")
    (worktree / "local.env").write_text("TOKEN=1\n")
    bridge.retire(repo, "claude", discard=["web//local.env"])
    assert not worktree.exists()


def test_a_later_claim_moves_each_linked_base(
    bridge, repo, paired, actors, sibling, monkeypatch
):
    worktree = claim(bridge, paired)
    (worktree / "schema.txt").write_text("v2\n")
    git(worktree, "add", "schema.txt")
    git(worktree, "commit", "-m", "Schema v2")
    bridge.issue(lane(paired), "release", "1")
    record = bridge.issue(lane(paired), "claim", "2")
    assert record["linked"] == {"web": str(worktree)}
    assert participant(paired)["linked"]["web"]["base"] == git(
        worktree, "rev-parse", "HEAD"
    )
    unresolved(bridge, paired, monkeypatch, "2")
    evidence(monkeypatch, {str(repo): "MERGED", str(sibling): None})
    result = bridge.issue_resolve(repo, "2")
    assert result["outcome"] == "complete"
    assert "repositories" not in result["evidence"]


def test_a_write_in_a_linked_tree_meets_a_peer_reservation(
    bridge, paired, actors
):
    worktree = claim(bridge, paired)
    directory = lane(paired).parent
    write_json(directory / "claude-identity.json", {"name": "claude"})
    assert not reserve(bridge, actors["codex"], "web//shared.txt")
    refused = checkpoints.lane_refusal(
        directory,
        "claude",
        {
            "hook_event_name": "PreToolUse",
            "cwd": str(worktree),
            "tool_name": "Write",
            "tool_input": {"file_path": str(worktree / "shared.txt")},
        },
        bridge.home,
    )
    assert refused[1] is checkpoints.Reason.RESERVED_PATH
    assert "web//shared.txt" in json.dumps(refused[0])


def test_an_unknown_link_prefix_is_refused(bridge, paired, actors):
    with pytest.raises(BridgeError, match="wbe// names no repository"):
        reserve(bridge, actors["claude"], "wbe//shared.txt")
