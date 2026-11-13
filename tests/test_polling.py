"""Checks that a lane polling coordination state is flagged, told, parked."""

import subprocess
import time
from pathlib import Path

from agent_parley import polling, quota, store, supervision


def lane_of(bridge, repo, paired):
    """Registers the claude lane in the store and returns its records."""
    store.initialize(bridge.home)
    registered = store.register(bridge.home, paired["root"], "claude")
    directory = bridge.project(repo)[1]
    return directory, registered["registration_token"]


def poll_status(bridge, monkeypatch, token, times, tool="cli:status"):
    """Records the lane's command-line reads as the lane itself."""
    monkeypatch.setenv(polling.TOKEN_ENV, token)
    for _ in range(times):
        polling.record(bridge.home, tool)
    monkeypatch.delenv(polling.TOKEN_ENV)


def settle(bridge, directory, paired):
    """Runs the polling and quota steps of one supervision poll."""
    parked = polling.observe(bridge.home, directory, paired)
    quota.account(
        bridge.home,
        directory,
        paired,
        supervision.published_capacity,
        parked,
    )
    return parked


def notices(bridge, paired):
    """Counts the polling notices delivered to the claude lane."""
    with store.connect(bridge.home) as db:
        return db.execute(
            "SELECT COUNT(*) FROM messages WHERE dedup_key LIKE ?",
            (f"{polling.NOTICE}%",),
        ).fetchone()[0]


def commit(lane):
    """Commits one file on the lane's branch."""
    (lane / "work.txt").write_text(f"{time.time()}\n")
    for command in (
        ["add", "work.txt"],
        [
            "-c",
            "user.name=Lane",
            "-c",
            "user.email=lane@example.com",
            "commit",
            "-m",
            "Move the work",
        ],
    ):
        subprocess.run(
            ["git", "-C", str(lane), *command], check=True, capture_output=True
        )


def flagged_and_parked(bridge, repo, paired, monkeypatch):
    """Drives the claude lane through flag, one notice and a park."""
    directory, token = lane_of(bridge, repo, paired)
    settle(bridge, directory, paired)
    poll_status(bridge, monkeypatch, token, 40)
    assert settle(bridge, directory, paired) == {}
    assert "lane told" in polling.marker(directory, "claude")
    assert quota.held(directory, "claude") is None
    assert notices(bridge, paired) == 1
    poll_status(bridge, monkeypatch, token, 40)
    parked = settle(bridge, directory, paired)
    assert "claude" in parked
    assert notices(bridge, paired) == 1
    return directory, token


def test_forty_status_reads_flag_wake_once_then_park(
    bridge, repo, paired, monkeypatch
):
    directory, _ = flagged_and_parked(bridge, repo, paired, monkeypatch)
    held = quota.held(directory, "claude")
    assert held is not None
    assert held["cause"].startswith("parked for polling: ")
    marker = supervision.stall_marker(
        supervision.stall(bridge.home, directory, paired, "claude", 600)
    )
    assert marker.startswith("polling: ")
    assert "parked until progress" in marker
    settle(bridge, directory, paired)
    assert notices(bridge, paired) == 1
    assert quota.held(directory, "claude") is not None


def test_waiting_for_mail_is_never_polling(bridge, repo, paired, monkeypatch):
    directory, token = lane_of(bridge, repo, paired)
    poll_status(bridge, monkeypatch, token, 80, tool="wait_for_message")
    assert settle(bridge, directory, paired) == {}
    assert polling.marker(directory, "claude") == ""
    assert notices(bridge, paired) == 0


def test_a_commit_on_the_lane_branch_clears_the_park(
    bridge, repo, paired, monkeypatch
):
    directory, token = flagged_and_parked(bridge, repo, paired, monkeypatch)
    commit(Path(paired["participants"]["claude"]["lane"]))
    assert settle(bridge, directory, paired) == {}
    assert quota.held(directory, "claude") is None
    assert polling.marker(directory, "claude") == ""
    poll_status(bridge, monkeypatch, token, 5)
    assert settle(bridge, directory, paired) == {}
    assert polling.marker(directory, "claude") == ""


def test_an_operator_message_clears_the_park(bridge, repo, paired, monkeypatch):
    directory, _ = flagged_and_parked(bridge, repo, paired, monkeypatch)
    bridge.say(repo, "claude", "Look at issue 7 next")
    assert settle(bridge, directory, paired) == {}
    assert quota.held(directory, "claude") is None
    assert polling.marker(directory, "claude") == ""


def test_an_operator_command_records_no_read(bridge, repo, paired, monkeypatch):
    lane_of(bridge, repo, paired)
    monkeypatch.delenv(polling.TOKEN_ENV, raising=False)
    polling.record(bridge.home, "cli:status")
    with store.connect(bridge.home) as db:
        count = db.execute(
            "SELECT COUNT(*) FROM events WHERE tool='cli:status'"
        ).fetchone()[0]
    assert count == 0
