"""Checks the prepared spare worktrees a project pool keeps for new lanes."""

import sys
from pathlib import Path

import pytest

from agent_parley import pool, roster, terminal
from agent_parley.state import BridgeError, write_json
from agent_parley.worktrees import git


def prepared(bridge, repo, monkeypatch, tmp_path, size=1):
    """Sets up a project with a counting init and a filled pool."""
    monkeypatch.setattr(pool, "replenish", lambda *args: None)
    counter = tmp_path / "init-runs"
    bridge.setup(repo)
    bridge.initialization(
        repo,
        f"{sys.executable} -c "
        f'"import pathlib; '
        f"pathlib.Path('initialized').write_text('yes'); "
        f"open(r'{counter}', 'a').write('x')\"",
    )
    bridge.pool(repo, size)
    bridge.fill_pool(repo)
    _, directory = bridge.project(repo)
    return directory, counter


def commit(repo, name):
    """Commits one new file to the base checkout."""
    (repo / name).write_text(name)
    git(repo, "add", name)
    git(
        repo,
        "-c",
        "user.name=Bridge Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-m",
        name,
    )
    return git(repo, "rev-parse", "HEAD")


def test_a_spare_is_created_with_init_run_once(
    bridge, repo, monkeypatch, tmp_path
):
    directory, counter = prepared(bridge, repo, monkeypatch, tmp_path)
    recorded = pool.spares(directory)
    assert len(recorded) == 1
    spare = recorded[0]
    assert spare["state"] == pool.READY
    assert Path(spare["path"]).parent == directory
    assert (Path(spare["path"]) / "initialized").read_text() == "yes"
    assert counter.read_text() == "x"
    assert "credential" not in spare
    bridge.fill_pool(repo)
    assert counter.read_text() == "x"
    reading = pool.reading(directory, roster.read(directory))
    assert reading["size"] == 1
    assert [row["state"] for row in reading["spares"]] == [pool.READY]


def test_a_new_lane_takes_the_spare_on_the_current_base(
    bridge, repo, monkeypatch, tmp_path
):
    directory, counter = prepared(bridge, repo, monkeypatch, tmp_path)
    spare = pool.spares(directory)[0]
    manifest = bridge.add_participant(repo, "claude", "claude")
    participant = manifest["participants"]["claude"]
    lane = Path(participant["lane"])
    assert lane == Path(spare["path"])
    assert counter.read_text() == "x"
    branch = git(lane, "symbolic-ref", "--short", "HEAD")
    assert branch == participant["branch"]
    assert git(lane, "rev-parse", "HEAD") == manifest["base"]
    assert not git(repo, "branch", "--list", spare["branch"])
    assert participant["launch"]["init"] < 1
    assert pool.spares(directory) == []
    assert "claude" in terminal.lane_summary(lane)


def test_a_stale_spare_is_never_handed_out(bridge, repo, monkeypatch, tmp_path):
    directory, counter = prepared(bridge, repo, monkeypatch, tmp_path)
    spare = pool.spares(directory)[0]
    data = roster.read(directory)
    data["base"] = commit(repo, "later.txt")
    write_json(directory / "project.json", data)
    assert pool.state(spare, data) == pool.STALE
    manifest = bridge.add_participant(repo, "claude", "claude")
    lane = Path(manifest["participants"]["claude"]["lane"])
    assert lane == directory / "claude"
    assert git(lane, "rev-parse", "HEAD") == data["base"]
    assert counter.read_text() == "xx"
    rows = pool.sweep(repo, directory, roster.read(directory), apply=True)
    assert [(row["reason"], row["removed"]) for row in rows] == [
        ("stale spare", True)
    ]
    assert not Path(spare["path"]).exists()
    assert not git(repo, "branch", "--list", spare["branch"])
    assert pool.spares(directory) == []


def test_a_spare_moved_off_its_base_is_skipped(
    bridge, repo, monkeypatch, tmp_path
):
    directory, _ = prepared(bridge, repo, monkeypatch, tmp_path)
    spare = pool.spares(directory)[0]
    git(Path(spare["path"]), "checkout", "--detach")
    assert pool.take(repo, directory, roster.read(directory)) is None
    assert pool.spares(directory) == []
    assert not Path(spare["path"]).exists()


def test_a_changed_init_command_makes_the_spares_stale(
    bridge, repo, monkeypatch, tmp_path
):
    directory, counter = prepared(bridge, repo, monkeypatch, tmp_path)
    old = pool.spares(directory)[0]
    bridge.initialization(repo, f"{sys.executable} -c pass")
    data = roster.read(directory)
    assert pool.state(old, data) == pool.STALE
    bridge.fill_pool(repo)
    fresh = pool.spares(directory)
    assert len(fresh) == 1
    assert fresh[0]["state"] == pool.READY
    assert not (Path(fresh[0]["path"]) / "initialized").exists()
    assert counter.read_text() == "x"


def test_gc_keeps_a_ready_spare_and_status_counts_it(
    bridge, repo, monkeypatch, tmp_path
):
    directory, _ = prepared(bridge, repo, monkeypatch, tmp_path)
    rows = pool.sweep(repo, directory, roster.read(directory))
    assert [(row["reason"], row["reclaim"]) for row in rows] == [
        ("spare", False)
    ]
    line = pool.summary_line(pool.reading(directory, roster.read(directory)))
    assert line == "Spare worktrees: 1 ready of 1, 0 preparing, 0 stale"


def test_a_pool_set_to_zero_lets_gc_remove_the_spares(
    bridge, repo, monkeypatch, tmp_path
):
    directory, _ = prepared(bridge, repo, monkeypatch, tmp_path)
    spare = pool.spares(directory)[0]
    bridge.pool(repo, 0)
    rows = pool.sweep(repo, directory, roster.read(directory), apply=True)
    assert [row["reason"] for row in rows] == ["stale spare"]
    assert not Path(spare["path"]).exists()


@pytest.mark.parametrize("size", [-1, 9, "two", True])
def test_the_pool_size_is_a_bounded_whole_number(size):
    with pytest.raises(BridgeError):
        roster.pool_size(size)
