"""Checks that reclaiming a lane removes landed work and nothing else."""

import json
import os
import shutil
import socket
import time
from pathlib import Path

import pytest

from agent_parley import (
    forge,
    issues,
    problems,
    reclaim,
    retirement,
    roster,
    store,
    supervision,
    terminal,
)
from agent_parley.cli import git
from agent_parley.state import lock


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


def aged(path, seconds=3600):
    """Moves a directory's change time into the past."""
    past = time.time() - seconds
    os.utime(path, (past, past))


@pytest.fixture
def idle(bridge, repo, paired, monkeypatch):
    """Leaves the codex lane stopped, empty, clean and long untouched."""
    directory = bridge.project(repo, create=False)[1]
    manifest = roster.read(directory)
    lane = Path(paired["lanes"]["codex"])
    aged(lane)
    completion(monkeypatch, None)
    return {
        "directory": directory,
        "lane": lane,
        "branch": manifest["participants"]["codex"]["branch"],
    }


def test_a_stopped_empty_clean_lane_is_retired(bridge, repo, idle):
    swept = bridge.reclaim(repo, apply=True)

    retired = row_of(swept, "codex")
    assert retired["reason"] == reclaim.STOPPED
    assert retired["removed"] is True
    assert not idle["lane"].exists()
    assert idle["branch"] not in branches(repo)
    assert "codex" not in roster.read(idle["directory"])["participants"]


def test_a_recently_created_lane_is_not_retired(bridge, repo, idle):
    os.utime(idle["lane"])

    swept = bridge.reclaim(repo, apply=True)

    assert row_of(swept, "codex")["reason"] == reclaim.UNUSED
    assert idle["lane"].exists()


def test_a_live_session_keeps_the_stopped_lane(bridge, repo, idle):
    with lock(idle["directory"] / "codex.session.lock"):
        swept = bridge.reclaim(repo, apply=True)

    assert row_of(swept, "codex")["reason"] == reclaim.SESSION
    assert idle["lane"].exists()


def test_a_claim_keeps_the_stopped_lane(bridge, repo, idle):
    bridge.issue(idle["lane"], "claim", "1")
    aged(idle["lane"])

    swept = bridge.reclaim(repo, apply=True)

    assert row_of(swept, "codex")["reason"] == reclaim.CLAIMED
    assert idle["lane"].exists()


def test_a_reservation_keeps_the_stopped_lane(bridge, repo, idle, monkeypatch):
    monkeypatch.setattr(
        store, "active_reservations", lambda *args: {"codex": ["src/*"]}
    )

    swept = bridge.reclaim(repo, apply=True)

    assert row_of(swept, "codex")["reason"] == reclaim.LEASED
    assert idle["lane"].exists()


def test_one_untracked_file_keeps_the_stopped_lane(bridge, repo, idle):
    (idle["lane"] / "notes.txt").write_text("keep me\n")
    aged(idle["lane"])

    swept = bridge.reclaim(repo, apply=True)

    kept = row_of(swept, "codex")
    assert kept["reason"] == reclaim.UNCOMMITTED
    assert kept["paths"] == ["notes.txt"]
    assert (idle["lane"] / "notes.txt").exists()


def test_a_retired_lane_whose_worktree_is_gone_leaves_status(
    bridge, repo, idle
):
    retirement.mark(idle["directory"], "codex", time.time())
    git(repo, "worktree", "remove", str(idle["lane"]))

    swept = bridge.reclaim(repo, apply=True)
    listed = [
        record["participant"]
        for project in bridge.status_snapshot()["projects"]
        for record in project["participants"]
    ]

    assert "codex" not in listed
    assert "claude" in listed
    assert row_of(swept, "codex")["reason"] == reclaim.VANISHED
    assert "codex" not in roster.read(idle["directory"])["participants"]


def test_mail_to_a_retired_lane_is_superseded(bridge, repo, paired, idle):
    store.initialize(bridge.home)
    tokens = {
        name: store.register(bridge.home, paired["root"], name)
        for name in ("claude", "codex")
    }
    actor = store.authenticate(
        bridge.home, tokens["claude"]["registration_token"]
    )
    store.call(
        bridge.home,
        actor,
        "send_message",
        {
            "to": ["codex"],
            "subject": "Share",
            "body_md": "Take this",
            "idempotency_key": "share-1",
            "ack_required": True,
        },
    )

    bridge.reclaim(repo, apply=True)

    with store.connect(bridge.home) as db:
        rows = db.execute(
            "SELECT superseded_reason FROM message_recipients"
        ).fetchall()
    assert [row["superseded_reason"] for row in rows] == ["codex retired"]
    assert not supervision.bounced_shares(
        bridge.home,
        idle["directory"],
        roster.read(idle["directory"]),
        {"claude": {"state": supervision.ACTIVE}},
    )


def made(repo, directory, name):
    """Adds a worktree the way a lane does for a sub-task."""
    path = directory / name
    git(repo, "worktree", "add", "-b", name, str(path))
    return path


def worktree_of(rows, path):
    """Returns the assessment recorded for one worktree."""
    return next(row for row in rows if row["worktree"] == str(path.resolve()))


def test_a_merged_worktree_a_lane_made_is_reclaimed(bridge, repo, idle):
    path = made(repo, idle["directory"], "pr-1")
    aged(path)

    swept = bridge.reclaim_worktrees(repo, apply=True)

    row = worktree_of(swept, path)
    assert row["reason"] == reclaim.CONTAINED
    assert row["removed"] is True
    assert not path.exists()
    assert "pr-1" in branches(repo)


def test_a_dirty_worktree_a_lane_made_is_reported_with_its_size(
    bridge, repo, idle
):
    path = made(repo, idle["directory"], "pr-2")
    (path / "draft.txt").write_text("x" * 2048)
    aged(path)

    reported = bridge.reclaim_worktrees(repo, sizes=True)
    swept = bridge.reclaim_worktrees(repo, apply=True)

    row = worktree_of(reported, path)
    assert row["reason"] == reclaim.UNCOMMITTED
    assert row["paths"] == ["draft.txt"]
    assert row["bytes"] >= 2048
    assert worktree_of(swept, path)["reclaim"] is False
    assert (path / "draft.txt").exists()


def test_a_worktree_with_unmerged_commits_is_kept(bridge, repo, idle):
    path = made(repo, idle["directory"], "pr-3")
    (path / "work.txt").write_text("work\n")
    commit(path, "Sub-task work")
    aged(path)

    swept = bridge.reclaim_worktrees(repo, apply=True)

    assert worktree_of(swept, path)["reason"] == reclaim.DETACHED
    assert path.exists()


def test_a_worktree_outside_the_state_directory_is_never_touched(
    bridge, repo, idle, tmp_path
):
    path = made(repo, tmp_path, "operator-wt")
    aged(path)

    swept = bridge.reclaim_worktrees(repo, apply=True)

    assert worktree_of(swept, path)["reason"] == reclaim.OUTSIDE
    assert path.exists()


def test_the_sweep_publishes_the_worktrees_it_reclaimed(bridge, repo, idle):
    store.initialize(bridge.home)
    path = made(repo, idle["directory"], "pr-4")
    aged(path)

    supervision.poll(bridge.home, idle["directory"])

    published = json.loads(
        (idle["directory"] / supervision.RECLAIM_PUBLICATION).read_text()
    )
    assert not path.exists()
    assert [row["worktree"] for row in published["worktrees"]] == [
        str(path.resolve())
    ]
    assert row_of(published["lanes"], "codex")["reason"] == reclaim.STOPPED


def test_a_missing_project_root_is_retired_within_one_interval(
    bridge, repo, paired
):
    store.initialize(bridge.home)
    directory = bridge.project(repo, create=False)[1]
    bridge.issue(Path(paired["lanes"]["claude"]), "claim", "1")
    shutil.rmtree(repo)

    supervision.poll(bridge.home, directory)
    first = supervision.root_retired(directory)
    marker = directory / supervision.ROOT_PUBLICATION
    recorded = json.loads(marker.read_text())
    recorded["since"] -= supervision.DEFAULTS["interval"]
    marker.write_text(json.dumps(recorded))
    supervision.poll(bridge.home, directory)

    assert first is False
    assert supervision.root_retired(directory)
    participants = roster.read(directory)["participants"]
    assert all(roster.retired(entry) for entry in participants.values())
    assert not issues.snapshot(directory)["issues"]["1"].get("owner")
    assert json.loads(marker.read_text())["state_directory"] == str(directory)
    assert bridge.status_snapshot()["projects"] == []


def test_service_start_removes_wake_sockets_nobody_listens_on(tmp_path):
    home = tmp_path / "state"
    home.mkdir()
    stale = home / "wake-stale.sock"
    live = home / "wake-live.sock"
    with socket.socket(socket.AF_UNIX) as dead:
        dead.bind(str(stale))
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(str(live))
        listener.listen(1)

        removed = terminal.sweep_sockets(home)

        assert live.exists()
    assert removed == ["wake-stale.sock"]
    assert not stale.exists()


def test_a_lane_orphaned_past_the_ceiling_is_ready_to_retire():
    record = {
        "claims": [
            {"issue": 7, "orphaned": True, "orphan_recorded_seconds": 7200}
        ]
    }

    rows = problems._retire_rows(record, "claude", "--repo /r", "/r", 3600)
    young = problems._retire_rows(record, "claude", "--repo /r", "/r", 9000)

    assert [row["condition"] for row in rows] == [problems.READY]
    assert rows[0]["command"] == (
        "agent-parley participant retire claude --repo /r"
    )
    assert young == []
