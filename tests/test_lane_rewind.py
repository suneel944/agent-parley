"""Rewinding a lane to a recorded checkpoint on a new branch."""

import json
import os
import sys
import time
from pathlib import Path

import pytest

from agent_parley import cli, issues, process, recovery, roster, store
from agent_parley.cli import git
from agent_parley.state import BridgeError, write_json


def commit(lane, name, text):
    """Commits one file in a lane and returns the new HEAD."""
    (lane / name).write_text(text)
    git(lane, "add", name)
    git(lane, "commit", "-m", f"Add {name}")
    return git(lane, "rev-parse", "HEAD")


def capture(directory, head):
    """Captures the claude lane and returns the history entry for a head."""
    recovery.capture(directory, roster.read(directory), "claude")
    return next(
        entry
        for entry in recovery.history(directory, "claude")
        if entry["head"] == head
    )


def lane_actor(bridge, paired, name):
    """Registers one paired lane with the store and returns its identity."""
    token = store.register(
        bridge.home, paired["root"], paired["participants"][name]["display"]
    )["registration_token"]
    actor = store.authenticate(bridge.home, token)
    assert actor is not None
    return actor


def decide(bridge, actor, subject):
    """Records one decision as a lane and returns its identifier."""
    sent = store.call(
        bridge.home,
        actor,
        "send_message",
        {
            "to": [],
            "subject": subject,
            "body_md": f"{subject}, recorded by the lane.",
            "idempotency_key": subject.lower().replace(" ", "-"),
            "decision": True,
        },
    )
    return sent["id"]


def age(directory, identifier, seconds):
    """Moves one recorded checkpoint back in time."""
    path = recovery.history_path(directory, "claude")
    value = json.loads(path.read_text())
    for entry in value["checkpoints"]:
        if entry["id"] == identifier:
            entry["captured_at"] -= seconds
    write_json(path, value)


@pytest.fixture
def worked(bridge, repo, paired):
    """Gives the claude lane two checkpoints with a decision between them."""
    store.initialize(bridge.home)
    actor = lane_actor(bridge, paired, "claude")
    lane_actor(bridge, paired, "codex")
    lane = Path(paired["lanes"]["claude"])
    directory = lane.parent
    bridge.issue(lane, "claim", "42")
    good = commit(lane, "good.txt", "known good\n")
    first = capture(directory, good)
    age(directory, first["id"], 10)
    wrong = decide(bridge, actor, "Rewrite the parser")
    bad = commit(lane, "bad.txt", "wrong path\n")
    second = capture(directory, bad)
    return {
        "lane": lane,
        "directory": directory,
        "actor": actor,
        "good": good,
        "bad": bad,
        "first": first["id"],
        "second": second["id"],
        "decision": wrong,
        "branch": paired["participants"]["claude"]["branch"],
    }


def test_checkpoints_list_commit_time_and_decisions_since(bridge, repo, worked):
    listed = bridge.lane_checkpoints(repo, "claude")

    by_id = {entry["id"]: entry for entry in listed["checkpoints"]}
    assert listed["checkpoints"][0]["id"] == worked["second"]
    assert by_id[worked["first"]]["head"] == worked["good"]
    assert by_id[worked["first"]]["issues"] == ["42"]
    assert [row["id"] for row in by_id[worked["first"]]["decisions"]] == [
        worked["decision"]
    ]
    assert by_id[worked["second"]]["decisions"] == []


def test_rewind_moves_to_a_new_branch_and_keeps_the_old_one(
    bridge, repo, worked
):
    lane = worked["lane"]
    directory = worked["directory"]
    claim = issues.snapshot(directory)["issues"]["42"]

    rewound = bridge.rewind(repo, "claude", worked["first"])

    target = f"{worked['branch']}-rewind-{worked['first']}"
    assert rewound["branch"] == target
    assert git(lane, "branch", "--show-current") == target
    assert git(lane, "rev-parse", "HEAD") == worked["good"]
    assert git(lane, "rev-parse", worked["branch"]) == worked["bad"]
    assert rewound["previous_branch"] == worked["branch"]
    assert roster.read(directory)["participants"]["claude"]["branch"] == (
        target
    )
    after = issues.snapshot(directory)["issues"]["42"]
    assert (after["owner"], after["claim_id"]) == (
        claim["owner"],
        claim["claim_id"],
    )
    inbox = store.call(bridge.home, worked["actor"], "fetch_inbox", {})
    updates = [
        item
        for item in inbox["messages"]
        if item["subject"] == f"Rewound to checkpoint {worked['first']}"
    ]
    assert len(updates) == 1
    assert "update_error" not in rewound


def test_rewind_marks_later_decisions_superseded(bridge, repo, worked):
    rewound = bridge.rewind(repo, "claude", worked["first"])

    assert [row["id"] for row in rewound["superseded"]] == [worked["decision"]]
    page = store.search_decisions(
        bridge.home,
        roster.read(worked["directory"])["root"],
        worked["actor"]["name"],
    )
    marked = {row["id"]: row for row in page["messages"]}[worked["decision"]]
    assert marked["superseded_reason"] == (
        f"claude was rewound to checkpoint {worked['first']}"
    )
    listed = bridge.lane_checkpoints(repo, "claude")
    first = {entry["id"]: entry for entry in listed["checkpoints"]}[
        worked["first"]
    ]
    assert first["decisions"][0]["superseded_ts"]


def test_rewind_refuses_a_dirty_worktree_with_its_paths(bridge, repo, worked):
    lane = worked["lane"]
    (lane / "draft.txt").write_text("unsaved\n")
    (lane / "good.txt").write_text("edited\n")

    with pytest.raises(BridgeError) as refusal:
        bridge.rewind(repo, "claude", worked["first"])

    assert "draft.txt" in str(refusal.value)
    assert "good.txt" in str(refusal.value)
    assert (lane / "draft.txt").read_text() == "unsaved\n"
    assert (lane / "good.txt").read_text() == "edited\n"
    assert git(lane, "branch", "--show-current") == worked["branch"]
    assert git(lane, "rev-parse", "HEAD") == worked["bad"]
    assert not git(lane, "stash", "list")
    kept = store.lane_decisions(
        bridge.home,
        roster.read(worked["directory"])["root"],
        worked["actor"]["name"],
        0,
    )
    assert kept[0]["superseded_ts"] is None


def published(directory, label):
    """Records a live session for the claude lane with one activity."""
    write_json(
        directory / "claude-activity.json",
        {
            "activity": label,
            "event": "PostToolUse" if label == "working" else "Stop",
            "updated": time.time(),
            "session_id": "claude-session",
            "session_pid": os.getpid(),
            "session_ticks": process.start_ticks(os.getpid()),
        },
    )


def test_rewind_refuses_a_lane_in_a_turn_and_runs_once_idle(
    bridge, repo, worked
):
    lane = worked["lane"]
    published(worked["directory"], "working")

    with pytest.raises(BridgeError) as refusal:
        bridge.rewind(repo, "claude", worked["first"])

    assert "never stops a running turn" in str(refusal.value)
    assert git(lane, "branch", "--show-current") == worked["branch"]
    published(worked["directory"], "idle")
    rewound = bridge.rewind(repo, "claude", worked["first"])
    assert rewound["branch"] != worked["branch"]


def test_rewind_refuses_an_unknown_checkpoint(bridge, repo, worked):
    with pytest.raises(BridgeError) as refusal:
        bridge.rewind(repo, "claude", "999")

    assert "no recorded checkpoint 999" in str(refusal.value)
    assert git(worked["lane"], "rev-parse", "HEAD") == worked["bad"]


def test_a_second_rewind_to_the_same_checkpoint_takes_a_fresh_name(
    bridge, repo, worked
):
    first = bridge.rewind(repo, "claude", worked["first"])
    second = bridge.rewind(repo, "claude", worked["first"])

    assert second["branch"] != first["branch"]
    assert git(worked["lane"], "rev-parse", first["branch"]) == worked["good"]


def test_the_commands_print_checkpoints_and_the_rewind(
    bridge, repo, worked, monkeypatch, capsys
):
    def text(*arguments):
        monkeypatch.setattr(
            sys,
            "argv",
            ["agent-parley", "--home", str(bridge.home), *arguments],
        )
        assert cli.main() == 0
        return capsys.readouterr().out

    listing = text("participant", "checkpoints", "claude", "--repo", str(repo))
    assert f"Checkpoint {worked['first']}  " in listing
    assert worked["good"][:12] in listing
    document = json.loads(
        text(
            "participant",
            "rewind",
            "claude",
            "--to",
            worked["first"],
            "--repo",
            str(repo),
            "--json",
        )
    )
    assert document["kind"] == "participant_rewind"
    assert document["previous_branch"] == worked["branch"]
