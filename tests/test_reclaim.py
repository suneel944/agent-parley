"""Checks that reclaiming a lane removes landed work and nothing else."""

import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from agent_parley import (
    cli,
    dashboard,
    forge,
    issues,
    lanes,
    problems,
    process,
    reclaim,
    recovery,
    retirement,
    roster,
    server,
    store,
    supervision,
    terminal,
)
from agent_parley.cli import git
from agent_parley.state import BridgeError, lock


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


def test_a_merged_lane_holding_ignored_folders_is_reclaimed(
    bridge, repo, landed, monkeypatch
):
    completion(monkeypatch, "MERGED")
    exclude = Path(git(repo, "rev-parse", "--git-common-dir"))
    if not exclude.is_absolute():
        exclude = repo / exclude
    (exclude / "info").mkdir(exist_ok=True)
    (exclude / "info" / "exclude").write_text("__pycache__/\n.venv/\n")
    for folder in ("__pycache__", ".venv"):
        (landed["lane"] / folder).mkdir()
        (landed["lane"] / folder / "cached").write_text("build output\n")

    swept = bridge.reclaim(repo, apply=True)

    assert row_of(swept, "claude")["removed"] is True
    assert not landed["lane"].exists()


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


def test_an_idle_session_names_the_real_hold_and_its_remedy(
    bridge, repo, landed, monkeypatch
):
    completion(monkeypatch, "MERGED")
    manifest = roster.read(landed["directory"])
    registered = reclaim.worktrees(manifest["root"])

    def assessed(idle):
        return reclaim.assess(
            landed["directory"],
            manifest,
            "claude",
            registered=registered,
            claimed=False,
            busy=True,
            idle=idle,
        )

    assert assessed(False)["reason"] == reclaim.SESSION
    kept = assessed(True)
    assert kept["reclaim"] is False
    assert kept["reason"] == reclaim.IDLE
    assert "agent-parley participant stop claude" in kept["remedy"]
    (landed["lane"] / "scratch.txt").write_text("unsaved\n")
    dirty = assessed(True)
    assert dirty["reason"] == reclaim.UNCOMMITTED
    assert "participant stop claude" in reclaim.lines([dirty])[0]


def test_the_supervision_tick_reclaims_a_merged_lane(
    bridge, repo, landed, monkeypatch
):
    store.initialize(bridge.home)
    completion(monkeypatch, "MERGED")

    supervision.poll(bridge.home, landed["directory"])

    assert not landed["lane"].exists()
    assert landed["branch"] not in branches(repo)
    assert not supervision.reclaim_due(landed["directory"], time.time(), 900)


def test_a_reclaimed_lane_moves_its_state_to_reclaimed(
    bridge, repo, landed, monkeypatch
):
    store.initialize(bridge.home)
    completion(monkeypatch, "MERGED")
    root = roster.read(landed["directory"])["root"]
    with store.connect(bridge.home, write=True) as db:
        lanes.transition(db, root, "claude", lanes.STOPPED)

    supervision.poll(bridge.home, landed["directory"])

    with store.connect(bridge.home) as db:
        record = lanes.read(db, root, "claude")
        history = lanes.history(db, root, "claude")
    assert record["state"] == lanes.RECLAIMED
    assert history[-1]["source"] == lanes.STOPPED
    assert history[-1]["target"] == lanes.RECLAIMED


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
    """Leaves the codex lane stopped, empty, clean and long untouched.

    The relocation grace period is lifted, so a worktree a test has just
    made can be moved; the grace period has its own test.
    """
    directory = bridge.project(repo, create=False)[1]
    manifest = roster.read(directory)
    lane = Path(paired["lanes"]["codex"])
    aged(lane)
    completion(monkeypatch, None)
    monkeypatch.setattr(reclaim, "RELOCATE_AFTER", 0)
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


def shared(bridge, paired, count):
    """Sends `count` acknowledged shares from claude to codex."""
    store.initialize(bridge.home)
    tokens = {
        name: store.register(bridge.home, paired["root"], name)
        for name in ("claude", "codex")
    }
    actor = store.authenticate(
        bridge.home, tokens["claude"]["registration_token"]
    )
    for index in range(count):
        store.call(
            bridge.home,
            actor,
            "send_message",
            {
                "to": ["codex"],
                "subject": f"Share {index}",
                "body_md": "Take this",
                "idempotency_key": f"share-{index}",
                "ack_required": True,
                "ack_within": 600,
            },
        )


def test_a_retired_lane_is_one_problem_row_naming_the_retirement(
    bridge, repo, paired, idle
):
    shared(bridge, paired, 3)

    bridge.reclaim(repo, apply=True)
    found = bridge.problems()

    [row] = [row for row in found if row["condition"] == problems.RETIRED]
    assert row["participant"] == "codex"
    assert row["count"] == 3
    assert row["detail"] == "3 shares to codex superseded: codex retired"
    assert "agent-parley" not in row["command"]
    assert not [row for row in found if row["condition"] == problems.BOUNCE]


def test_no_retired_lane_is_no_retirement_row(bridge, repo, paired, idle):
    shared(bridge, paired, 3)

    found = bridge.problems()

    assert not [row for row in found if row["condition"] == problems.RETIRED]


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

    kept = worktree_of(swept, path)
    assert kept["reason"] == reclaim.UNPUSHED
    assert len(kept["paths"]) == 1
    assert path.exists()


def excluded(repo, *patterns):
    """Makes Git ignore the patterns in every worktree of a repository."""
    exclude = Path(git(repo, "rev-parse", "--git-common-dir"))
    if not exclude.is_absolute():
        exclude = repo / exclude
    (exclude / "info").mkdir(exist_ok=True)
    (exclude / "info" / "exclude").write_text(
        "".join(f"{pattern}\n" for pattern in patterns)
    )


def cached(path):
    """Fills a worktree with the output tests and linters leave behind."""
    for name in (".venv", ".pytest_cache", ".ruff_cache", ".mypy_cache"):
        (path / name).mkdir()
        (path / name / "data").write_text("cache\n")
    (path / "__pycache__").mkdir()
    (path / "__pycache__" / "m.pyc").write_bytes(b"\0")
    (path / "pkg.egg-info").mkdir()
    (path / "pkg.egg-info" / "PKG-INFO").write_text("Name: pkg\n")


CACHES = (
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "*.egg-info",
)


def test_an_ignored_file_keeps_a_worktree_a_lane_made(bridge, repo, idle):
    path = made(repo, idle["directory"], "pr-4")
    excluded(repo, ".env")
    (path / ".env").write_text("SECRET=1\n")
    aged(path)

    swept = bridge.reclaim_worktrees(repo, apply=True)

    kept = worktree_of(swept, path)
    assert kept["reason"] == reclaim.IGNORED
    assert kept["paths"] == [".env"]
    assert kept["reclaim"] is False
    assert path.exists()
    assert (path / ".env").exists()


def test_a_worktree_holding_only_caches_is_reclaimed(bridge, repo, idle):
    path = made(repo, idle["directory"], "pr-6")
    excluded(repo, *CACHES)
    cached(path)
    aged(path)

    swept = bridge.reclaim_worktrees(repo, apply=True)

    row = worktree_of(swept, path)
    assert row["reason"] == reclaim.CONTAINED
    assert row["removed"] is True
    assert not path.exists()


def test_caches_beside_user_files_keep_a_worktree(bridge, repo, idle):
    path = made(repo, idle["directory"], "pr-7")
    excluded(repo, *CACHES, ".env", "data/")
    cached(path)
    (path / ".env").write_text("SECRET=1\n")
    (path / "data").mkdir()
    (path / "data" / "rows.csv").write_text("1\n")
    aged(path)

    swept = bridge.reclaim_worktrees(repo, apply=True)

    kept = worktree_of(swept, path)
    assert kept["reason"] == reclaim.IGNORED
    assert kept["paths"] == [".env", "data"]
    assert kept["reclaim"] is False
    assert (path / ".env").exists()
    assert (path / "data" / "rows.csv").exists()
    assert (path / ".venv" / "data").exists()


def strayed(idle, name):
    """Names where the sweep moves a worktree no lane made."""
    return idle["directory"].resolve() / reclaim.STRAYS / name


def test_an_unattributed_stray_moves_and_is_reclaimed_once_landed(
    bridge, repo, idle, tmp_path
):
    clean = made(repo, tmp_path, "kyc-walk-baseline")
    dirty = made(repo, tmp_path, "fix-1871")
    (dirty / "work.txt").write_text("work\n")
    commit(dirty, "Unpushed work")

    planned = bridge.relocate_worktrees(repo)
    moved = bridge.relocate_worktrees(repo, apply=True)
    assert worktree_of(moved, clean)["moved"] is True, moved
    assert worktree_of(moved, dirty)["moved"] is True, moved
    for name in ("kyc-walk-baseline", "fix-1871"):
        aged(strayed(idle, name))
    swept = bridge.reclaim_worktrees(repo, apply=True)

    plan = worktree_of(planned, clean)
    assert plan["participant"] == ""
    assert plan["reason"] == reclaim.ADRIFT
    assert plan["destination"] == str(strayed(idle, "kyc-walk-baseline"))
    assert worktree_of(planned, dirty)["relocate"] is True
    assert not clean.exists()
    assert not dirty.exists()
    gone = worktree_of(swept, strayed(idle, "kyc-walk-baseline"))
    assert gone["reason"] == reclaim.CONTAINED
    assert gone["removed"] is True
    kept = worktree_of(swept, strayed(idle, "fix-1871"))
    assert kept["reason"] == reclaim.UNPUSHED
    assert not kept.get("removed")
    assert (strayed(idle, "fix-1871") / "work.txt").exists()


def test_a_stray_under_the_repository_claude_worktrees_moves(
    bridge, repo, idle
):
    folder = repo / ".claude" / "worktrees"
    folder.mkdir(parents=True)
    path = made(repo, folder, "pr-kyc")

    moved = bridge.relocate_worktrees(repo, apply=True)

    row = worktree_of(moved, path)
    assert row["moved"] is True
    assert not path.exists()
    assert (strayed(idle, "pr-kyc") / ".git").exists()


def test_a_worktree_in_use_stays_until_its_grace_period_passes(
    bridge, repo, idle, monkeypatch
):
    monkeypatch.setattr(reclaim, "RELOCATE_AFTER", 3600)
    folder = repo / ".claude" / "worktrees"
    folder.mkdir(parents=True)
    path = made(repo, folder, "1015-sweep-record")
    (path / "shared.txt").write_text("in progress\n")
    aged(path)
    gitdir = Path(git(path, "rev-parse", "--absolute-git-dir"))

    fresh = bridge.relocate_worktrees(repo, apply=True)
    aged(gitdir / "index")
    aged(gitdir / "HEAD")
    moved = bridge.relocate_worktrees(repo, apply=True)

    row = worktree_of(fresh, path)
    assert row["reason"] == reclaim.FRESH
    assert row["relocate"] is False
    assert "moved" not in row
    assert worktree_of(moved, path)["moved"] is True
    destination = strayed(idle, "1015-sweep-record")
    assert (destination / "shared.txt").read_text() == "in progress\n"


def test_a_worktree_holding_another_worktree_is_not_moved_over_it(
    bridge, repo, idle, tmp_path
):
    outer = made(repo, tmp_path, "review")
    folder = outer / ".claude" / "worktrees"
    folder.mkdir(parents=True)
    inner = made(repo, folder, "feat")

    first = bridge.relocate_worktrees(repo, apply=True)
    second = bridge.relocate_worktrees(repo, apply=True)

    row = worktree_of(first, outer)
    assert row["reason"] == reclaim.NESTED
    assert row["relocate"] is False
    assert "moved" not in row
    assert worktree_of(first, inner)["moved"] is True
    assert worktree_of(second, outer)["moved"] is True
    moved = strayed(idle, "feat")
    assert git(moved, "rev-parse", "--abbrev-ref", "HEAD") == "feat"


@pytest.fixture
def untemporary(tmp_path, monkeypatch):
    """Points the temporary directories away from the test's own folder."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(reclaim, "TEMPORARY", ())
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    return scratch


def test_an_operator_worktree_outside_scratch_places_is_only_reported(
    bridge, repo, idle, tmp_path, untemporary
):
    service = made(repo, tmp_path, "tm-arena")
    aged(service)
    scratch = made(repo, untemporary, "kyc-in-temp")
    folder = repo / ".claude" / "worktrees"
    folder.mkdir(parents=True)
    inside = made(repo, folder, "pr-kyc")
    lane_made = made(repo, tmp_path, "codex-pr-30")

    moved = bridge.relocate_worktrees(repo, apply=True)
    swept = bridge.reclaim_worktrees(repo, apply=True)

    row = worktree_of(moved, service)
    assert row["reason"] == reclaim.FOREIGN
    assert row["relocate"] is False
    assert "moved" not in row
    kept = worktree_of(swept, service)
    assert kept["reason"] == reclaim.FOREIGN
    assert not kept.get("removed")
    assert (service / ".git").exists()
    assert worktree_of(moved, scratch)["moved"] is True
    assert worktree_of(moved, inside)["moved"] is True
    assert worktree_of(moved, lane_made)["reason"] == reclaim.STRAYED
    assert worktree_of(moved, lane_made)["moved"] is True


def test_a_worktree_nested_under_the_state_directory_moves_to_strays(
    bridge, repo, idle
):
    nested = idle["directory"] / "operator-wt"
    nested.mkdir()
    path = made(repo, nested, "fix/616-c2")

    moved = bridge.relocate_worktrees(repo, apply=True)

    destination = strayed(idle, "616-c2")
    assert worktree_of(moved, path)["destination"] == str(destination)
    assert (destination / ".git").exists()


def test_a_locked_unattributed_stray_is_left_in_place(
    bridge, repo, idle, tmp_path
):
    path = made(repo, tmp_path, "operator-wt")
    git(repo, "worktree", "lock", str(path))

    moved = bridge.relocate_worktrees(repo, apply=True)
    swept = bridge.reclaim_worktrees(repo, apply=True, force=True)

    row = worktree_of(moved, path)
    assert row["reason"] == reclaim.LOCKED
    assert row["relocate"] is False
    assert worktree_of(swept, path)["reason"] == reclaim.LOCKED
    assert path.exists()


@pytest.mark.skipif(not Path("/proc/self/cwd").exists(), reason="needs /proc")
def test_an_unattributed_stray_a_process_works_in_is_left_alone(
    bridge, repo, idle, tmp_path
):
    path = made(repo, tmp_path, "operator-wt")
    aged(path)
    shell = subprocess.Popen(["sleep", "60"], cwd=path)
    try:
        moved = bridge.relocate_worktrees(repo, apply=True)
        swept = bridge.reclaim_worktrees(repo, apply=True)
    finally:
        shell.kill()
        shell.wait()

    assert worktree_of(moved, path)["reason"] == reclaim.SESSION
    assert worktree_of(swept, path)["reason"] == reclaim.SESSION
    assert path.exists()


def test_a_stray_git_refuses_to_move_is_assessed_in_place(
    bridge, repo, idle, tmp_path
):
    path = made(repo, tmp_path, "operator-wt")
    aged(path)
    (idle["directory"] / reclaim.STRAYS).write_text("not a directory\n")

    moved = bridge.relocate_worktrees(repo, apply=True)
    swept = bridge.reclaim_worktrees(repo, apply=True)

    assert worktree_of(moved, path)["reason"] == reclaim.REFUSED
    row = worktree_of(swept, path)
    assert row["reason"] == reclaim.CONTAINED
    assert row["removed"] is True
    assert not path.exists()


@pytest.fixture
def elsewhere(bridge):
    """Makes a directory on a filesystem other than the state directory's."""
    device = os.stat(bridge.home).st_dev
    for candidate in ("/dev/shm", tempfile.gettempdir(), "/tmp", "/var/tmp"):
        try:
            other = os.stat(candidate).st_dev != device
        except OSError:
            continue
        if other and os.access(candidate, os.W_OK):
            folder = Path(tempfile.mkdtemp(dir=candidate))
            yield folder
            shutil.rmtree(folder, ignore_errors=True)
            return
    pytest.skip("needs a second writable filesystem")


def test_a_stray_on_another_filesystem_is_rebuilt_in_the_strays_directory(
    bridge, repo, idle, elsewhere
):
    path = made(repo, elsewhere, "kyc-other-device")
    (path / "draft.txt").write_text("unsaved\n")
    aged(path)

    moved = bridge.relocate_worktrees(repo, apply=True)

    row = worktree_of(moved, path)
    assert row["reason"] == reclaim.CROSSDEVICE
    assert row["moved"] is True
    assert not path.exists()
    rebuilt = strayed(idle, "kyc-other-device")
    assert (rebuilt / "draft.txt").read_text() == "unsaved\n"
    assert git(rebuilt, "branch", "--show-current") == "kyc-other-device"


def other_device(monkeypatch, path):
    """Reports one worktree as sitting on a filesystem of its own."""
    real = reclaim._device

    def device(candidate):
        """Gives the worktree a device number nothing else has."""
        if candidate == path.resolve():
            return -1
        return real(candidate)

    monkeypatch.setattr(reclaim, "_device", device)


def test_a_cross_device_stray_keeps_its_staged_unstaged_and_unpushed_work(
    bridge, repo, idle, tmp_path, monkeypatch
):
    path = made(repo, tmp_path, "scratch-x")
    (path / "work.txt").write_text("committed\n")
    commit(path, "Unpushed work")
    head = git(path, "rev-parse", "HEAD")
    (path / "staged.txt").write_text("staged\n")
    git(path, "add", "staged.txt")
    (path / "work.txt").write_text("edited\n")
    (path / "loose.txt").write_text("untracked\n")
    aged(path)
    other_device(monkeypatch, path)

    moved = bridge.relocate_worktrees(repo, apply=True)

    row = worktree_of(moved, path)
    assert row["reason"] == reclaim.CROSSDEVICE
    assert row["moved"] is True
    assert row["checkpoint"].startswith("worktree-")
    assert not path.exists()
    rebuilt = strayed(idle, "scratch-x")
    assert git(rebuilt, "rev-parse", "HEAD") == head
    assert git(rebuilt, "branch", "--show-current") == "scratch-x"
    assert git(rebuilt, "diff", "--cached", "--name-only") == "staged.txt"
    assert (rebuilt / "work.txt").read_text() == "edited\n"
    assert (rebuilt / "loose.txt").read_text() == "untracked\n"
    assert str(path.resolve()) not in git(repo, "worktree", "list")


def test_a_failed_checkpoint_never_removes_a_cross_device_stray(
    bridge, repo, idle, tmp_path, monkeypatch
):
    path = made(repo, tmp_path, "scratch-y")
    (path / "draft.txt").write_text("unsaved\n")
    aged(path)
    other_device(monkeypatch, path)

    def failing(directory, worktree):
        """Fails the checkpoint the way an unreadable worktree does."""
        raise BridgeError("capture failed")

    monkeypatch.setattr(recovery, "preserve", failing)

    moved = bridge.relocate_worktrees(repo, apply=True)

    row = worktree_of(moved, path)
    assert row["reason"] == reclaim.REFUSED
    assert row["moved"] is False
    assert (path / "draft.txt").read_text() == "unsaved\n"
    assert not strayed(idle, "scratch-y").exists()


def test_a_cross_device_stray_holding_ignored_files_is_left_in_place(
    bridge, repo, idle, tmp_path, monkeypatch
):
    path = made(repo, tmp_path, "scratch-z")
    excluded(repo, ".env")
    (path / ".env").write_text("SECRET=1\n")
    aged(path)
    other_device(monkeypatch, path)

    moved = bridge.relocate_worktrees(repo, apply=True)

    row = worktree_of(moved, path)
    assert row["reason"] == reclaim.IGNORED
    assert row["paths"] == [".env"]
    assert (path / ".env").exists()
    assert not list((idle["directory"] / "recovery").glob("worktree-*"))


def test_a_worktree_under_the_pre_rename_state_root_is_reclaimed(
    bridge, repo, idle, tmp_path, monkeypatch
):
    legacy = tmp_path / "agent-bridge" / "projects" / "0123456789abcdef"
    legacy.mkdir(parents=True)
    path = made(repo, legacy, "wt-1092")
    aged(path)

    unrenamed = bridge.reclaim_worktrees(repo)
    monkeypatch.setattr(
        reclaim, "RENAMED_HOMES", {bridge.home.name: "agent-bridge"}
    )
    swept = bridge.reclaim_worktrees(repo, apply=True)

    assert worktree_of(unrenamed, path)["reason"] == reclaim.CONTAINED
    row = worktree_of(swept, path)
    assert row["reason"] == reclaim.CONTAINED
    assert row["removed"] is True
    assert not path.exists()


def test_a_merged_lane_worktree_outside_the_state_directory_is_reclaimed(
    bridge, repo, idle, tmp_path
):
    path = made(repo, tmp_path, "codex-pr-9")
    aged(path)

    swept = bridge.reclaim_worktrees(repo, apply=True)

    row = worktree_of(swept, path)
    assert row["participant"] == "codex"
    assert row["reason"] == reclaim.CONTAINED
    assert row["removed"] is True
    assert not path.exists()


def test_a_worktree_in_the_lane_wt_directory_is_attributed_by_path(
    bridge, repo, idle
):
    folder = reclaim.extras(idle["lane"].resolve())
    folder.mkdir()
    path = made(repo, folder, "kyc-walk-baseline")
    aged(path)
    manifest = roster.read(idle["directory"])

    children = reclaim.child_worktrees(idle["directory"], manifest, "codex")
    swept = bridge.reclaim_worktrees(repo, apply=True)

    assert children == [path.resolve()]
    row = worktree_of(swept, path)
    assert row["participant"] == "codex"
    assert row["reason"] == reclaim.CONTAINED
    assert row["removed"] is True


def test_a_stray_lane_worktree_moves_with_its_unfinished_work(
    bridge, repo, idle, tmp_path
):
    path = made(repo, tmp_path, "codex-pr-20")
    (path / "work.txt").write_text("work\n")
    commit(path, "Unpushed work")
    (path / "draft.txt").write_text("unsaved\n")
    excluded(repo, ".env")
    (path / ".env").write_text("SECRET=1\n")
    destination = reclaim.extras(idle["lane"].resolve()) / "codex-pr-20"

    planned = bridge.relocate_worktrees(repo)
    moved = bridge.relocate_worktrees(repo, apply=True)
    swept = bridge.reclaim_worktrees(repo)

    plan = worktree_of(planned, path)
    assert plan["relocate"] is True
    assert plan["reason"] == reclaim.STRAYED
    assert plan["destination"] == str(destination)
    assert "moved" not in plan
    assert worktree_of(moved, path)["moved"] is True
    assert not path.exists()
    assert (destination / "draft.txt").read_text() == "unsaved\n"
    assert (destination / ".env").read_text() == "SECRET=1\n"
    assert git(destination, "log", "-1", "--format=%s") == "Unpushed work"
    row = worktree_of(swept, destination)
    assert row["participant"] == "codex"
    assert row["reason"] == reclaim.UNCOMMITTED


def test_a_relocation_takes_a_free_name_beside_a_taken_one(
    bridge, repo, idle, tmp_path
):
    folder = reclaim.extras(idle["lane"].resolve())
    (folder / "codex-pr-21").mkdir(parents=True)
    path = made(repo, tmp_path, "codex-pr-21")

    moved = bridge.relocate_worktrees(repo, apply=True)

    row = worktree_of(moved, path)
    assert row["destination"] == str(folder / "codex-pr-21-2")
    assert row["moved"] is True
    assert (folder / "codex-pr-21-2" / ".git").exists()


def test_a_locked_stray_worktree_is_left_in_place(bridge, repo, idle, tmp_path):
    path = made(repo, tmp_path, "codex-pr-22")
    git(repo, "worktree", "lock", str(path))

    moved = bridge.relocate_worktrees(repo, apply=True)

    row = worktree_of(moved, path)
    assert row["reason"] == reclaim.LOCKED
    assert row["relocate"] is False
    assert "moved" not in row
    assert path.exists()


def test_a_stray_worktree_of_a_busy_lane_is_left_in_place(
    bridge, repo, idle, tmp_path
):
    path = made(repo, tmp_path, "codex-pr-23")

    with lock(idle["directory"] / "codex.session.lock"):
        moved = bridge.relocate_worktrees(repo, apply=True)

    assert worktree_of(moved, path)["reason"] == reclaim.SESSION
    assert path.exists()


@pytest.mark.skipif(not Path("/proc/self/cwd").exists(), reason="needs /proc")
def test_a_stray_worktree_a_process_works_in_is_left_in_place(
    bridge, repo, idle, tmp_path
):
    path = made(repo, tmp_path, "codex-pr-25")
    shell = subprocess.Popen(["sleep", "60"], cwd=path)
    try:
        moved = bridge.relocate_worktrees(repo, apply=True)
    finally:
        shell.kill()
        shell.wait()

    assert worktree_of(moved, path)["reason"] == reclaim.SESSION
    assert path.exists()


def test_a_move_git_refuses_is_reported_and_moves_nothing(
    bridge, repo, idle, tmp_path
):
    path = made(repo, tmp_path, "codex-pr-24")
    planned = worktree_of(bridge.relocate_worktrees(repo), path)
    Path(planned["destination"]).mkdir(parents=True)

    row = reclaim.relocate(str(repo), idle["directory"], planned)

    assert row["reason"] == reclaim.REFUSED
    assert row["moved"] is False
    assert path.exists()


def test_gc_dry_run_lists_planned_moves_and_apply_performs_them(
    bridge, repo, idle, tmp_path, monkeypatch, capsys
):
    path = made(repo, tmp_path, "codex-pr-25")
    destination = reclaim.extras(idle["lane"].resolve()) / "codex-pr-25"
    stray = made(repo, tmp_path, "kyc-walk-baseline")
    adrift = strayed(idle, "kyc-walk-baseline")
    arguments = ["agent-parley", "--home", str(bridge.home), "gc"]
    arguments += ["--repo", str(repo)]

    monkeypatch.setattr("sys.argv", arguments)
    assert cli.main() == 0
    planned = capsys.readouterr().out
    monkeypatch.setattr("sys.argv", [*arguments, "--apply"])
    assert cli.main() == 0
    applied = capsys.readouterr().out

    assert f"to move to {destination}" in planned
    assert f"worktree {stray.resolve()}: to move to {adrift}" in planned
    assert f"moved to {destination}" in applied
    assert f"moved to {adrift}" in applied
    assert destination.exists() and not path.exists()
    assert adrift.exists() and not stray.exists()


def test_gc_dry_run_lists_every_scattered_stray_and_moves_nothing(
    bridge, repo, idle, tmp_path, tmp_path_factory, monkeypatch, capsys
):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    folder = repo / ".claude" / "worktrees"
    folder.mkdir(parents=True)
    scratch = tmp_path_factory.mktemp("scratch")
    strays = [
        made(repo, home, "kyc-in-home"),
        made(repo, repo.parent, "kyc-beside"),
        made(repo, folder, "kyc-dot-worktrees"),
        made(repo, scratch, "kyc-in-temp"),
    ]
    arguments = ["agent-parley", "--home", str(bridge.home), "gc"]

    monkeypatch.setattr("sys.argv", [*arguments, "--repo", str(repo)])
    assert cli.main() == 0
    planned = capsys.readouterr().out

    for path in strays:
        destination = strayed(idle, path.name)
        assert f"worktree {path.resolve()}: to move to {destination}" in planned
        assert (path / ".git").exists()
        assert not destination.exists()


def test_a_lane_worktree_moved_by_the_operator_is_never_retired(
    bridge, repo, idle, tmp_path
):
    elsewhere = tmp_path / "codex-elsewhere"
    git(repo, "worktree", "move", str(idle["lane"]), str(elsewhere))

    moved = bridge.relocate_worktrees(repo, apply=True)
    made_rows = bridge.reclaim_worktrees(repo, apply=True)
    swept = bridge.reclaim(repo, apply=True)

    kept = row_of(swept, "codex")
    assert kept["reason"] == reclaim.MOVED
    assert kept["reclaim"] is False
    assert kept["paths"] == [str(elsewhere.resolve())]
    assert "git worktree move" in kept["remedy"]
    assert "codex" in roster.read(idle["directory"])["participants"]
    assert elsewhere.exists()
    assert idle["branch"] in branches(repo)
    assert all(row["worktree"] != str(elsewhere.resolve()) for row in moved)
    assert all(
        row.get("worktree") != str(elsewhere.resolve()) for row in made_rows
    )


def test_a_vanished_lane_branch_checked_out_in_the_base_is_not_moved(
    repo, idle
):
    shutil.rmtree(idle["lane"])
    git(repo, "worktree", "prune")
    git(repo, "switch", idle["branch"])

    assert reclaim.relocated(str(repo), idle["lane"], idle["branch"]) == ""


def test_restore_names_where_a_moved_lane_worktree_went(
    bridge, repo, idle, tmp_path
):
    elsewhere = tmp_path / "codex-moved"
    git(repo, "worktree", "move", str(idle["lane"]), str(elsewhere))

    with pytest.raises(BridgeError, match="git worktree move"):
        bridge.restore(repo, "codex")


def test_a_worktree_of_a_retired_lane_is_reclaimed_at_once(
    bridge, repo, idle, tmp_path
):
    path = made(repo, tmp_path, "codex-pr-10")
    retirement.mark(idle["directory"], "codex", time.time())

    swept = bridge.reclaim_worktrees(repo, apply=True)

    assert worktree_of(swept, path)["reason"] == reclaim.ABANDONED
    assert not path.exists()


def test_force_removes_a_dirty_lane_worktree_after_a_checkpoint(
    bridge, repo, idle, tmp_path
):
    path = made(repo, tmp_path, "codex-pr-11")
    (path / "draft.txt").write_text("unsaved\n")
    aged(path)

    unforced = bridge.reclaim_worktrees(repo, apply=True)
    forced = bridge.reclaim_worktrees(repo, apply=True, force=True)

    assert worktree_of(unforced, path)["reason"] == reclaim.UNCOMMITTED
    row = worktree_of(forced, path)
    assert row["removed"] is True
    assert not path.exists()
    folder = idle["directory"] / "recovery"
    record = json.loads((folder / f"{row['checkpoint']}.json").read_text())
    bundle = folder / record["artifact"]["reference"]
    assert bundle.stat().st_size == record["artifact"]["bytes"]
    git(
        repo,
        "fetch",
        "--no-write-fetch-head",
        str(bundle),
        record["worktree_commit"],
    )
    assert "draft.txt" in git(
        repo, "ls-tree", "-r", "--name-only", record["worktree_commit"]
    )


def test_a_worktree_whose_commits_landed_elsewhere_is_bundled_then_removed(
    bridge, repo, idle, tmp_path
):
    path = made(repo, tmp_path, "codex-pr-20")
    (path / "landed.txt").write_text("landed\n")
    commit(path, "Sub-task work")
    head = git(path, "rev-parse", "HEAD").strip()
    (repo / "landed.txt").write_text("landed\n")
    commit(repo, "Same work through another branch")
    aged(path)

    swept = bridge.reclaim_worktrees(repo, apply=True)

    row = worktree_of(swept, path)
    assert row["reason"] == reclaim.ABSORBED
    assert row["removed"] is True
    assert not path.exists()
    assert "codex-pr-20" in branches(repo)
    folder = idle["directory"] / "recovery"
    record = json.loads((folder / f"{row['checkpoint']}.json").read_text())
    bundle = folder / record["artifact"]["reference"]
    git(
        repo,
        "fetch",
        "--no-write-fetch-head",
        str(bundle),
        record["worktree_commit"],
    )
    assert git(repo, "cat-file", "-t", head).strip() == "commit"


def test_a_worktree_whose_commits_differ_from_the_base_is_kept(
    bridge, repo, idle, tmp_path
):
    path = made(repo, tmp_path, "codex-pr-21")
    (path / "landed.txt").write_text("mine\n")
    commit(path, "Sub-task work")
    (repo / "landed.txt").write_text("theirs\n")
    commit(repo, "Different work through another branch")
    aged(path)

    swept = bridge.reclaim_worktrees(repo, apply=True)

    assert worktree_of(swept, path)["reason"] == reclaim.UNPUSHED
    assert path.exists()


def test_prune_removes_retired_lane_files_and_stale_wake_logs(
    bridge, repo, idle
):
    directory = idle["directory"]
    retirement.mark(directory, "codex", time.time())
    doomed = [f"codex{end}" for end in reclaim.RETIRED_FILES]
    kept = ["codex-identity.json", "codex-capacity.json", "codex-wake.json"]
    for name in [*doomed, *kept, "claude-wake.log", "claude-work.json"]:
        (directory / name).write_text("{}")
    stale = directory / "old-wake.log"
    stale.write_text("old\n")
    aged(stale, reclaim.WAKE_LOG_RETENTION + 60)
    manifest = roster.read(directory)

    removed = reclaim.prune(directory, manifest, time.time())

    assert sorted(removed) == sorted([*doomed, "old-wake.log"])
    for name in [*kept, "claude-wake.log", "claude-work.json"]:
        assert (directory / name).exists()
    for name in doomed:
        assert not (directory / name).exists()


def test_prune_keeps_the_files_of_a_retired_lane_still_running(
    bridge, repo, idle, monkeypatch
):
    directory = idle["directory"]
    retirement.mark(directory, "codex", time.time())
    (directory / "codex-work.json").write_text("{}")
    monkeypatch.setattr(reclaim, "_busy", lambda directory, name: True)

    removed = reclaim.prune(directory, roster.read(directory), time.time())

    assert removed == []
    assert (directory / "codex-work.json").exists()


def test_reclaim_dry_run_lists_removals_with_sizes(
    bridge, repo, idle, tmp_path, monkeypatch, capsys
):
    merged = made(repo, tmp_path, "codex-pr-12")
    dirty = made(repo, tmp_path, "codex-pr-13")
    (dirty / "draft.txt").write_text("x" * 4096)
    aged(merged)
    aged(dirty)
    monkeypatch.setattr(
        "sys.argv",
        [
            "agent-parley",
            "--home",
            str(bridge.home),
            "reclaim",
            "--dry-run",
            "--repo",
            str(repo),
            "--json",
        ],
    )

    assert cli.main() == 0

    document = json.loads(capsys.readouterr().out)
    rows = document["worktrees"]
    assert worktree_of(rows, merged)["reclaim"] is True
    assert worktree_of(rows, dirty)["reason"] == reclaim.UNCOMMITTED
    assert worktree_of(rows, dirty)["bytes"] >= 4096
    assert merged.exists() and dirty.exists()


def test_status_reports_the_state_size_and_what_reclaim_would_remove(
    bridge, repo, idle, tmp_path
):
    store.initialize(bridge.home)
    dirty = made(repo, tmp_path, "claude-pr-14")
    (dirty / "draft.txt").write_text("unsaved\n")

    supervision.poll(bridge.home, idle["directory"])

    summary = bridge.status_snapshot()["projects"][0]["reclaim"]
    assert summary["state_bytes"] > 0
    assert summary["reclaimable"] == 0
    assert summary["forceable"] == 1
    assert reclaim.summary_line(summary).endswith("1 more with --force")


def test_the_sweep_publishes_the_worktrees_it_reclaimed(bridge, repo, idle):
    store.initialize(bridge.home)
    path = made(repo, idle["directory"], "pr-4")
    aged(path)

    supervision.poll(bridge.home, idle["directory"])

    published = json.loads(
        (idle["directory"] / supervision.RECLAIM_PUBLICATION).read_text()
    )
    assert not path.exists()
    assert not strayed(idle, "pr-4").exists()
    assert [row["worktree"] for row in published["relocations"]] == [
        str(path.resolve())
    ]
    assert [row["worktree"] for row in published["worktrees"]] == [
        str(strayed(idle, "pr-4"))
    ]
    assert row_of(published["lanes"], "codex")["reason"] == reclaim.STOPPED


def test_the_sweep_logs_what_it_moved_and_removed_for_later_reading(
    bridge, repo, idle, capsys
):
    store.initialize(bridge.home)
    path = made(repo, idle["directory"], "pr-5")
    aged(path)

    supervision.poll(bridge.home, idle["directory"])
    first = capsys.readouterr().out
    publication = idle["directory"] / supervision.RECLAIM_PUBLICATION
    published = json.loads(publication.read_text())
    published["swept"] -= supervision.RECLAIM_INTERVAL
    publication.write_text(json.dumps(published))
    supervision.poll(bridge.home, idle["directory"])
    second = capsys.readouterr().out

    entries = [line for line in first.splitlines() if " reclaim " in line]
    name = idle["directory"].name
    assert any(
        f"reclaim {name}: worktree {path.resolve()}: moved to "
        f"{strayed(idle, 'pr-5')}" in line
        for line in entries
    )
    assert any(
        f"worktree {strayed(idle, 'pr-5')}: reclaimed" in line
        for line in entries
    )
    assert any(" codex: reclaimed " in line for line in entries)
    assert " reclaim " not in second
    assert not json.loads(publication.read_text())["relocations"]


def test_a_missing_project_root_is_retired_once_gone_long_enough(
    bridge, repo, paired
):
    store.initialize(bridge.home)
    directory = bridge.project(repo, create=False)[1]
    bridge.issue(Path(paired["lanes"]["claude"]), "claim", "1")
    (saved,) = recovery.capture(directory, roster.read(directory), "claude")
    shutil.rmtree(repo)

    supervision.poll(bridge.home, directory)
    first = supervision.root_retired(directory)
    marker = directory / supervision.ROOT_PUBLICATION
    recorded = json.loads(marker.read_text())
    recorded["since"] -= supervision.DEFAULTS["root_gone_after"]
    marker.write_text(json.dumps(recorded))
    supervision.poll(bridge.home, directory)

    assert first is False
    assert supervision.root_retired(directory)
    participants = roster.read(directory)["participants"]
    assert all(roster.retired(entry) for entry in participants.values())
    assert not issues.snapshot(directory)["issues"]["1"].get("owner")
    published = json.loads(marker.read_text())
    assert published["state_directory"] == str(directory)
    released = next(
        lane for lane in published["lanes"] if lane["participant"] == "claude"
    )
    assert released["checkpoints"] == {"1": saved["id"]}
    assert bridge.status_snapshot()["projects"] == []
    assert dashboard.collect(bridge.home, False, {})["projects"] == []


def test_prune_removes_a_bundle_no_live_claim_can_still_read(
    bridge, repo, paired
):
    directory = bridge.project(repo, create=False)[1]
    lane = Path(paired["lanes"]["claude"])
    bridge.issue(lane, "claim", "1")
    (saved,) = recovery.capture(directory, roster.read(directory), "claude")
    folder = directory / "recovery"
    bundle = folder / saved["artifact"]["reference"]
    record = folder / f"{saved['id']}.json"
    assert bundle.exists() and record.exists()

    bridge.issue(lane, "release", "1")
    removed = recovery.prune(directory, issues.snapshot(directory))

    assert removed == [saved["id"]]
    assert not bundle.exists()
    assert not record.exists()


def test_prune_keeps_a_bundle_its_claim_still_owns(bridge, repo, paired):
    directory = bridge.project(repo, create=False)[1]
    lane = Path(paired["lanes"]["claude"])
    bridge.issue(lane, "claim", "1")
    (saved,) = recovery.capture(directory, roster.read(directory), "claude")
    folder = directory / "recovery"

    removed = recovery.prune(directory, issues.snapshot(directory))

    assert removed == []
    assert (folder / saved["artifact"]["reference"]).exists()


def test_prune_drops_only_preserved_worktrees_past_their_retention(
    bridge, repo, idle, tmp_path
):
    old = made(repo, tmp_path, "pr-old")
    new = made(repo, tmp_path, "pr-new")
    (old / "a.txt").write_text("a\n")
    (new / "b.txt").write_text("b\n")
    expired = recovery.preserve(idle["directory"], old)
    kept = recovery.preserve(idle["directory"], new)
    folder = idle["directory"] / "recovery"
    record = folder / f"{expired['id']}.json"
    aged_record = json.loads(record.read_text())
    aged_record["captured_at"] -= recovery.PRESERVED_RETENTION + 1
    record.write_text(json.dumps(aged_record))

    removed = recovery.prune(
        idle["directory"], issues.snapshot(idle["directory"])
    )

    assert removed == [expired["id"]]
    assert not record.exists()
    assert not (folder / f"{expired['id']}.bundle").exists()
    assert [saved["id"] for saved in recovery.preserved(idle["directory"])] == [
        kept["id"]
    ]


def test_gc_lists_a_forced_removal_and_restores_it_with_the_printed_command(
    bridge, repo, idle, monkeypatch, capsys
):
    path = made(repo, idle["directory"], "pr-31")
    (path / "work.txt").write_text("committed\n")
    commit(path, "Unpushed work")
    head = git(path, "rev-parse", "HEAD")
    (path / "draft.txt").write_text("unsaved\n")
    git(path, "add", "draft.txt")
    (path / "work.txt").write_text("edited\n")
    aged(path)
    arguments = ["agent-parley", "--home", str(bridge.home), "gc"]
    arguments += ["--repo", str(repo)]

    monkeypatch.setattr("sys.argv", [*arguments, "--apply", "--force"])
    assert cli.main() == 0
    swept = capsys.readouterr().out
    monkeypatch.setattr("sys.argv", [*arguments, "--preserved"])
    assert cli.main() == 0
    listed = capsys.readouterr().out
    (identifier,) = [
        saved["id"] for saved in recovery.preserved(idle["directory"])
    ]
    removed = strayed(idle, "pr-31")
    command = reclaim.restore_command(identifier, str(removed))
    monkeypatch.setattr(
        "sys.argv", [*arguments, "--restore", identifier, "--to", str(path)]
    )
    assert cli.main() == 0

    assert not removed.exists()
    assert command in swept
    assert f"{identifier}: {removed} (pr-31)" in listed
    assert git(path, "rev-parse", "HEAD") == head
    assert git(path, "branch", "--show-current") == "pr-31"
    assert git(path, "diff", "--cached", "--name-only") == "draft.txt"
    assert (path / "work.txt").read_text() == "edited\n"


def test_a_repeated_refusal_is_logged_once(
    bridge, repo, idle, tmp_path, monkeypatch
):
    path = made(repo, tmp_path, "operator-wt")
    (path / "draft.txt").write_text("unsaved\n")
    aged(path)
    (idle["directory"] / reclaim.STRAYS).write_text("not a directory\n")
    logged = []
    monkeypatch.setattr(
        server, "log", lambda home, kind, line: logged.append(line)
    )
    manifest = roster.read(idle["directory"])
    publication = idle["directory"] / supervision.RECLAIM_PUBLICATION

    supervision.reclaim_lanes(bridge.home, idle["directory"], manifest)
    published = json.loads(publication.read_text())
    publication.write_text(json.dumps({**published, "swept": 0}))
    supervision.reclaim_lanes(bridge.home, idle["directory"], manifest)

    assert json.loads(publication.read_text())["swept"] > 0
    refusals = [line for line in logged if reclaim.REFUSED in line]
    assert len(refusals) == 1
    assert str(path.resolve()) in refusals[0]
    assert path.exists()


def test_a_missing_root_never_retires_a_live_lane(bridge, repo, paired):
    store.initialize(bridge.home)
    directory = bridge.project(repo, create=False)[1]
    bridge.issue(Path(paired["lanes"]["claude"]), "claim", "1")
    (directory / "claude-activity.json").write_text(
        json.dumps(
            {
                "activity": "working",
                "updated": time.time(),
                "session_id": "claude-session",
                "session_pid": os.getpid(),
                "session_ticks": process.start_ticks(os.getpid()),
            }
        )
    )
    shutil.rmtree(repo)

    supervision.poll(bridge.home, directory)
    marker = directory / supervision.ROOT_PUBLICATION
    recorded = json.loads(marker.read_text())
    recorded["since"] -= supervision.DEFAULTS["root_gone_after"]
    marker.write_text(json.dumps(recorded))
    supervision.poll(bridge.home, directory)

    assert not supervision.root_retired(directory)
    participants = roster.read(directory)["participants"]
    assert not roster.retired(participants["claude"])
    assert roster.retired(participants["codex"])
    assert issues.snapshot(directory)["issues"]["1"]["owner"] == "claude"
    published = json.loads(marker.read_text())
    assert published["live"] == ["claude"]
    assert [lane["participant"] for lane in published["lanes"]] == ["codex"]
    [row] = [
        row for row in bridge.problems() if row["condition"] == problems.ROOT
    ]
    assert row["detail"].endswith("kept from retirement: claude")


def test_a_missing_root_never_retires_an_unreadable_lane(bridge, repo, paired):
    store.initialize(bridge.home)
    directory = bridge.project(repo, create=False)[1]
    bridge.issue(Path(paired["lanes"]["claude"]), "claim", "1")
    (directory / "claude-activity.json").write_text("{")
    shutil.rmtree(repo)

    supervision.poll(bridge.home, directory)
    marker = directory / supervision.ROOT_PUBLICATION
    recorded = json.loads(marker.read_text())
    recorded["since"] -= supervision.DEFAULTS["root_gone_after"]
    marker.write_text(json.dumps(recorded))
    supervision.poll(bridge.home, directory)

    assert not supervision.root_retired(directory)
    participants = roster.read(directory)["participants"]
    assert not roster.retired(participants["claude"])
    assert issues.snapshot(directory)["issues"]["1"]["owner"] == "claude"
    published = json.loads(marker.read_text())
    assert published["live"] == ["claude"]


def backdate(directory, seconds):
    """Moves the recorded start of a root's absence `seconds` earlier."""
    marker = directory / supervision.ROOT_PUBLICATION
    recorded = json.loads(marker.read_text())
    recorded["since"] -= seconds
    marker.write_text(json.dumps(recorded))


def test_a_root_gone_past_one_interval_and_back_retires_nothing(
    bridge, repo, paired
):
    store.initialize(bridge.home)
    directory = bridge.project(repo, create=False)[1]
    bridge.issue(Path(paired["lanes"]["claude"]), "claim", "1")
    away = repo.with_name("moved away")
    repo.rename(away)

    supervision.poll(bridge.home, directory)
    backdate(directory, supervision.DEFAULTS["interval"] + 30)
    supervision.poll(bridge.home, directory)
    away.rename(repo)
    supervision.poll(bridge.home, directory)

    participants = roster.read(directory)["participants"]
    assert not any(roster.retired(entry) for entry in participants.values())
    assert issues.snapshot(directory)["issues"]["1"]["owner"] == "claude"
    assert not (directory / supervision.ROOT_PUBLICATION).exists()
    assert not (directory / supervision.ROOT_RETURNED).exists()


def test_a_root_whose_parent_cannot_be_listed_never_retires(
    bridge, repo, paired, tmp_path
):
    store.initialize(bridge.home)
    directory = bridge.project(repo, create=False)[1]
    bridge.issue(Path(paired["lanes"]["claude"]), "claim", "1")
    manifest = directory / "project.json"
    data = json.loads(manifest.read_text())
    data["root"] = str(tmp_path / "unmounted volume" / "project")
    manifest.write_text(json.dumps(data))

    supervision.poll(bridge.home, directory)
    backdate(directory, supervision.DEFAULTS["root_gone_after"] + 1)
    supervision.poll(bridge.home, directory)

    published = json.loads(
        (directory / supervision.ROOT_PUBLICATION).read_text()
    )
    assert published["unreachable"] is True
    assert not published["retired"]
    participants = roster.read(directory)["participants"]
    assert not any(roster.retired(entry) for entry in participants.values())
    assert issues.snapshot(directory)["issues"]["1"]["owner"] == "claude"


def test_a_returned_root_keeps_the_record_of_the_lanes_it_retired(
    bridge, repo, paired
):
    store.initialize(bridge.home)
    directory = bridge.project(repo, create=False)[1]
    bridge.issue(Path(paired["lanes"]["claude"]), "claim", "1")
    away = repo.with_name("moved away")
    repo.rename(away)

    supervision.poll(bridge.home, directory)
    backdate(directory, supervision.DEFAULTS["root_gone_after"])
    supervision.poll(bridge.home, directory)
    away.rename(repo)
    supervision.poll(bridge.home, directory)

    assert not (directory / supervision.ROOT_PUBLICATION).exists()
    kept = json.loads((directory / supervision.ROOT_RETURNED).read_text())
    assert kept["returned_at"] >= kept["since"]
    assert {lane["participant"] for lane in kept["lanes"]} == {
        "claude",
        "codex",
    }
    [row] = [
        row
        for row in bridge.problems()
        if row["condition"] == problems.ROOT_RETURNED
    ]
    assert row["count"] == 2
    assert "agent-parley participant add claude --repo " in row["command"]
    assert "agent-parley participant add codex --repo " in row["command"]


def test_a_returned_root_record_clears_once_its_lanes_are_readmitted(
    bridge, repo, paired
):
    store.initialize(bridge.home)
    directory = bridge.project(repo, create=False)[1]
    kept = directory / supervision.ROOT_RETURNED
    kept.write_text(
        json.dumps(
            {
                "since": time.time() - 7200,
                "returned_at": time.time(),
                "lanes": [{"participant": "claude", "released": []}],
            }
        )
    )

    supervision.poll(bridge.home, directory)

    assert not kept.exists()


def test_a_returned_root_row_names_only_lanes_still_retired(
    bridge, repo, paired
):
    store.initialize(bridge.home)
    directory = bridge.project(repo, create=False)[1]
    retirement.mark(directory, "codex", time.time())
    (directory / supervision.ROOT_RETURNED).write_text(
        json.dumps(
            {
                "since": time.time() - 7200,
                "returned_at": time.time(),
                "lanes": [
                    {"participant": "claude", "released": []},
                    {"participant": "codex", "released": []},
                ],
            }
        )
    )

    [row] = [
        row
        for row in bridge.problems()
        if row["condition"] == problems.ROOT_RETURNED
    ]

    assert row["count"] == 1
    assert "agent-parley participant add codex --repo " in row["command"]
    assert "participant add claude" not in row["command"]
    assert "claude" not in row["detail"]


def test_a_second_root_return_keeps_lanes_retired_by_the_first(
    bridge, repo, paired
):
    store.initialize(bridge.home)
    directory = bridge.project(repo, create=False)[1]
    retirement.mark(directory, "claude", time.time())
    retirement.mark(directory, "codex", time.time())
    kept = directory / supervision.ROOT_RETURNED
    kept.write_text(
        json.dumps(
            {
                "since": time.time() - 7200,
                "returned_at": time.time() - 3600,
                "lanes": [
                    {"participant": "claude", "released": ["1"]},
                    {"participant": "codex", "released": ["2"]},
                ],
            }
        )
    )
    (directory / supervision.ROOT_PUBLICATION).write_text(
        json.dumps(
            {
                "since": time.time() - 600,
                "lanes": [{"participant": "codex", "released": []}],
            }
        )
    )

    supervision.root_returned(directory, roster.read(directory))

    recorded = json.loads(kept.read_text())
    assert recorded["lanes"] == [
        {"participant": "codex", "released": []},
        {"participant": "claude", "released": ["1"]},
    ]


def test_service_start_removes_wake_sockets_nobody_listens_on():
    with tempfile.TemporaryDirectory(prefix="wake-") as temporary:
        home = Path(temporary)
        stale = home / "wake-stale.sock"
        live = home / "wake-live.sock"
        with socket.socket(socket.AF_UNIX) as dead:
            dead.bind(str(stale))
        aged = time.time() - terminal.SOCKET_GRACE - 1
        os.utime(stale, (aged, aged))
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(live))
            listener.listen(1)

            removed = terminal.sweep_sockets(home)

            assert live.exists()
        assert removed == ["wake-stale.sock"]
        assert not stale.exists()


def test_a_sweep_keeps_a_wake_socket_bound_but_not_yet_listening():
    with tempfile.TemporaryDirectory(prefix="wake-") as temporary:
        path = Path(temporary) / "wake-x.sock"
        with (
            socket.socket(socket.AF_UNIX) as server,
            socket.socket(socket.AF_UNIX) as client,
        ):
            server.bind(str(path))

            removed = terminal.sweep_sockets(Path(temporary))

            server.listen(1)
            client.connect(str(path))
        assert removed == []


def test_a_lane_orphaned_past_the_ceiling_is_ready_to_retire():
    record = {
        "claims": [
            {"issue": 7, "orphaned": True, "orphan_recorded_seconds": 7200}
        ],
        "availability": {"process_alive": False},
    }
    live = {**record, "availability": {"process_alive": True}}

    rows = problems._retire_rows(record, "claude", "--repo /r", "/r", 3600)
    young = problems._retire_rows(record, "claude", "--repo /r", "/r", 9000)
    assert problems._retire_rows(live, "claude", "--repo /r", "/r", 3600) == []

    assert [row["condition"] for row in rows] == [problems.READY]
    assert rows[0]["command"] == (
        "agent-parley participant retire claude --repo /r"
    )
    assert young == []


def test_size_counts_a_hard_linked_file_once(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a" / "object").write_bytes(b"x" * 4096)
    os.link(tmp_path / "a" / "object", tmp_path / "b" / "object")
    (tmp_path / "b" / "own").write_bytes(b"y" * 100)

    assert reclaim.size(tmp_path) == 4196


def gone_root(bridge, repo, paired, interval=True):
    """Removes the root of the paired project and lets supervision retire it.

    Returns:
        The project state directory and the checkpoint captured first.
    """
    store.initialize(bridge.home)
    directory = bridge.project(repo, create=False)[1]
    bridge.issue(Path(paired["lanes"]["claude"]), "claim", "1")
    (saved,) = recovery.capture(directory, roster.read(directory), "claude")
    shutil.rmtree(repo)
    supervision.poll(bridge.home, directory)
    if interval:
        marker = directory / supervision.ROOT_PUBLICATION
        recorded = json.loads(marker.read_text())
        recorded["since"] -= supervision.DEFAULTS["root_gone_after"]
        marker.write_text(json.dumps(recorded))
        supervision.poll(bridge.home, directory)
    return directory, saved


def test_gc_project_removes_a_gone_root_and_keeps_its_checkpoints(
    bridge, repo, paired, monkeypatch, capsys
):
    directory, saved = gone_root(bridge, repo, paired)
    command = ["agent-parley", "--home", str(bridge.home), "gc"]
    command += ["--project", str(repo)]
    monkeypatch.setattr("sys.argv", command)

    assert cli.main() == 0
    planned = capsys.readouterr().out
    assert directory.exists()
    monkeypatch.setattr("sys.argv", [*command, "--apply"])
    assert cli.main() == 0
    printed = capsys.readouterr().out

    kept = bridge.home / supervision.RECOVERED_FOLDER / directory.name
    assert f"Would remove {directory}, the state of {repo}." in planned
    assert "Add --apply to remove it." in planned
    assert f"Removed {directory}, the state of {repo}." in printed
    assert f"Kept its recovery checkpoints in {kept}." in printed
    assert not directory.exists()
    assert any(kept.rglob(f"*{saved['id']}*"))
    assert not list((bridge.home / "projects").glob("*/project.json"))
    named = {
        entry["component"]: entry for entry in bridge.doctor()["components"]
    }
    assert named["projects"]["state"] == "ok"


def test_gc_project_yes_deletes_the_checkpoints_too(bridge, repo, paired):
    directory, _ = gone_root(bridge, repo, paired)

    result = supervision.forget(
        bridge.home, str(repo), apply=True, discard=True
    )

    assert result["checkpoints"] == ""
    assert result["checkpoints_deleted"] is True
    assert not directory.exists()
    assert not (bridge.home / supervision.RECOVERED_FOLDER).exists()


def test_gc_project_refuses_a_root_that_exists(bridge, repo, paired):
    directory = bridge.project(repo, create=False)[1]

    with pytest.raises(cli.BridgeError, match="still exists"):
        supervision.forget(bridge.home, str(repo), apply=True)

    assert (directory / "project.json").exists()


def test_gc_project_refuses_a_lane_that_may_still_run(bridge, repo, paired):
    store.initialize(bridge.home)
    directory = bridge.project(repo, create=False)[1]
    (directory / "claude-activity.json").write_text("{")
    shutil.rmtree(repo)

    with pytest.raises(cli.BridgeError, match="still be running: claude"):
        supervision.forget(bridge.home, str(repo), apply=True)

    assert (directory / "project.json").exists()


def test_gc_project_waits_for_the_service_to_retire_the_root(
    bridge, repo, paired
):
    directory, _ = gone_root(bridge, repo, paired, interval=False)

    with pytest.raises(cli.BridgeError, match="not retired"):
        supervision.forget(bridge.home, str(repo), apply=True)

    assert (directory / "project.json").exists()


def test_a_refused_retirement_keeps_the_gone_root_unretired_and_named(
    bridge, repo, paired, monkeypatch
):
    withdraw = retirement.withdraw
    refusal = "claude holds ready or operator-blocked work (#1)"

    def refusing(directory, name):
        """Refuses the claude lane's retirement as ready work would."""
        if name == "claude":
            raise BridgeError(refusal)
        return withdraw(directory, name)

    monkeypatch.setattr(retirement, "withdraw", refusing)
    directory, _ = gone_root(bridge, repo, paired)

    published = json.loads(
        (directory / supervision.ROOT_PUBLICATION).read_text()
    )
    assert published["retired"] is None
    assert not supervision.root_retired(directory)
    [entry] = published["refused"]
    assert entry["participant"] == "claude"
    assert entry["refused"] == refusal
    assert set(entry["checkpoints"]) == {"1"}
    assert [lane["participant"] for lane in published["lanes"]] == ["codex"]
    assert issues.snapshot(directory)["issues"]["1"]["owner"] == "claude"
    assert supervision.root_refused(directory) == [entry]
    with pytest.raises(cli.BridgeError, match=r"claude: claude holds ready"):
        supervision.forget(bridge.home, str(repo), apply=True)
    assert (directory / "project.json").exists()

    result = supervision.forget(
        bridge.home, str(repo), apply=True, discard=True
    )

    assert result["removed"] is True
    assert not directory.exists()


def test_a_root_publication_without_refused_reads_as_refusing_nothing(
    tmp_path,
):
    (tmp_path / supervision.ROOT_PUBLICATION).write_text(
        json.dumps({"since": 1, "retired": 2, "lanes": [], "live": []})
    )

    assert supervision.root_refused(tmp_path) == []


def test_one_refused_reclaim_step_never_skips_the_others(
    bridge, repo, idle, monkeypatch
):
    ran = []
    reclaimed = cli.Bridge.reclaim
    pruned = reclaim.prune

    def refusing(self, root, *, apply=False):
        """Refuses every worktree move."""
        raise BridgeError("relocation refused")

    def reclaiming(self, root, *, apply=False):
        """Records that the lane reclaim ran."""
        ran.append("reclaim")
        return reclaimed(self, root, apply=apply)

    def pruning(directory, manifest, now):
        """Records that the file prune ran."""
        ran.append("prune")
        return pruned(directory, manifest, now)

    logged = []
    monkeypatch.setattr(cli.Bridge, "relocate_worktrees", refusing)
    monkeypatch.setattr(cli.Bridge, "reclaim", reclaiming)
    monkeypatch.setattr(reclaim, "prune", pruning)
    monkeypatch.setattr(
        server, "log", lambda home, kind, line: logged.append((kind, line))
    )
    manifest = roster.read(idle["directory"])

    with pytest.raises(BridgeError, match="relocate: relocation refused"):
        supervision.reclaim_lanes(bridge.home, idle["directory"], manifest)

    assert ran == ["reclaim", "prune"]
    published = json.loads(
        (idle["directory"] / supervision.RECLAIM_PUBLICATION).read_text()
    )
    assert published["errors"] == [
        {"step": "relocate", "error": "relocation refused"}
    ]
    assert published["swept"] > 0
    assert (
        "reclaim",
        f"{idle['directory'].name}: relocate refused: relocation refused",
    ) in logged
    summary = supervision.reclaim_summary(idle["directory"])
    assert reclaim.summary_line(summary).endswith(
        "; sweep steps refused: 1, first relocate: relocation refused"
    )


def test_a_refused_reclaim_step_reaches_the_supervision_error(
    bridge, repo, idle, monkeypatch
):
    store.initialize(bridge.home)

    def refusing(self, root, *, apply=False):
        """Refuses every worktree move."""
        raise BridgeError("relocation refused")

    monkeypatch.setattr(cli.Bridge, "relocate_worktrees", refusing)

    supervision.poll(bridge.home, idle["directory"])

    error = issues.supervision_error(idle["directory"])
    assert error is not None
    assert "relocate: relocation refused" in error["detail"]
