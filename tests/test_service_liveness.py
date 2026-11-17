"""Tests for a service that stays supervised after a failure of its own.

Each test fails one part of the detached service the way a real estate did:
a background loop that raises, a project listing or a poll record that
cannot be read, a forge call cleared by another thread's success, and a
re-execution the kernel refuses.
"""

import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from agent_parley import forge, hook, issues, protocol, server, supervision
from agent_parley.state import write_json

RATE = "HTTP 403: API rate limit exceeded"


def settled(check, timeout=5.0):
    """Waits until ``check`` holds, and reports whether it did."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.01)
    return bool(check())


@pytest.fixture
def background(bridge, service):
    """Attaches a loop runner to the served bridge, stopped at teardown."""
    stopped = threading.Event()
    runner = server.Background(bridge.home, stopped)
    service.background = runner
    yield runner
    stopped.set()
    runner.join(time.monotonic() + 5)


def test_a_failed_loop_reads_as_degraded_until_it_restarts(
    bridge, repo, paired, service, background, monkeypatch, capsys
):
    monkeypatch.setattr(server, "LOOP_RESTART_SECONDS", 60.0)

    def broken(home, stopped):
        raise RuntimeError("no projects")

    background.start("supervision", broken)
    assert settled(background.faults)
    reading = service.health()
    assert reading["status"] == protocol.DEGRADED
    assert reading["detail"] == "supervision: RuntimeError: no projects"
    assert bridge.ready() is False
    assert bridge.status_snapshot()["server"] == {
        "ready": False,
        "state": protocol.DEGRADED,
    }
    bridge.status()
    printed = capsys.readouterr().out
    assert f"Background: {protocol.DEGRADED}" in printed
    reported = bridge.doctor()
    named = {entry["component"]: entry for entry in reported["components"]}
    assert named["service"]["state"] == protocol.DEGRADED
    assert named["service"]["remedy"] == protocol.RECOVERING
    assert named["service"]["compatible"] is False
    assert "background supervision failed; restarting in 60 s" in printed
    assert "RuntimeError: no projects" in printed


def test_a_failed_loop_is_started_again_and_health_recovers(
    bridge, service, background, monkeypatch
):
    monkeypatch.setattr(server, "LOOP_RESTART_SECONDS", 0.01)
    runs = []
    resumed = threading.Event()

    def flaky(home, stopped):
        runs.append(home)
        if len(runs) == 1:
            raise KeyError("first")
        resumed.set()
        stopped.wait()

    background.start("supervision", flaky)
    assert resumed.wait(5)
    assert runs == [bridge.home, bridge.home]
    assert settled(lambda: not background.faults())
    assert service.health()["status"] == "ready"


def test_a_loop_that_returns_is_not_started_again(
    bridge, service, background, monkeypatch
):
    monkeypatch.setattr(server, "LOOP_RESTART_SECONDS", 0.01)
    runs = []
    background.start("inbound", lambda home, stopped: runs.append(home))
    background.threads[0].join(5)
    assert not background.threads[0].is_alive()
    assert runs == [bridge.home]
    assert background.faults() == {}
    assert service.health()["status"] == "ready"


def quiet(monkeypatch, stopped, passes):
    """Runs supervision without polling and stops it after two passes."""

    def reap():
        passes.append(1)
        if len(passes) > 1:
            stopped.set()

    monkeypatch.setattr(supervision, "poll", lambda home, path: None)
    monkeypatch.setattr(supervision, "reap_launchers", reap)
    monkeypatch.setattr(
        supervision,
        "configuration",
        lambda home, manifest: {**supervision.DEFAULTS, "interval": 0},
    )


def test_supervision_survives_a_project_listing_it_cannot_read(
    bridge, paired, monkeypatch, capsys
):
    stopped = threading.Event()
    passes = []
    quiet(monkeypatch, stopped, passes)
    listing = Path.glob

    def unreadable(self, pattern, **options):
        if pattern == "*/project.json" and not passes:
            raise PermissionError("projects")
        return listing(self, pattern, **options)

    monkeypatch.setattr(Path, "glob", unreadable)
    supervision.run(bridge.home, stopped)
    assert len(passes) == 2
    logged = capsys.readouterr().out
    assert "list projects: PermissionError: projects" in logged


def test_supervision_survives_a_poll_record_it_cannot_read(
    bridge, paired, monkeypatch, capsys
):
    stopped = threading.Event()
    passes = []
    quiet(monkeypatch, stopped, passes)
    reads = []

    def unreadable(directory):
        reads.append(directory)
        if len(reads) == 1:
            raise ValueError("record")

    monkeypatch.setattr(issues, "supervision_error", unreadable)
    supervision.run(bridge.home, stopped)
    assert len(passes) == 2
    assert len(reads) == 2
    logged = capsys.readouterr().out
    assert "record poll failure: ValueError: record" in logged


def forge_client(monkeypatch, entered, release):
    """Answers ``slow`` after ``release`` and refuses anything else."""

    def answer(args, **options):
        if args[0] == "slow":
            entered.set()
            release.wait(5)
            return subprocess.CompletedProcess(args, 0, "ok", "")
        if args[0] == "fine":
            return subprocess.CompletedProcess(args, 0, "ok", "")
        return subprocess.CompletedProcess(args, 1, "", RATE)

    monkeypatch.setattr(forge.subprocess, "run", answer)
    forge._failure.clear()


def test_a_success_already_in_flight_keeps_a_later_forge_failure(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    forge_client(monkeypatch, entered, release)
    slow = threading.Thread(target=forge._run, args=(["slow"], 5))
    slow.start()
    assert entered.wait(5)
    assert forge._run(["refused"], 5) is None
    release.set()
    slow.join(5)
    assert forge.last_failure()["kind"] == "rate"
    assert forge._run(["fine"], 5) == "ok"
    assert forge.last_failure() is None


def test_a_caller_reads_its_own_forge_failure_after_another_succeeds(
    monkeypatch,
):
    forge_client(monkeypatch, threading.Event(), threading.Event())
    assert forge._run(["refused"], 5) is None
    other = threading.Thread(target=forge._run, args=(["fine"], 5))
    other.start()
    other.join(5)
    assert forge.last_failure() is None
    assert forge.last_failure(own=True)["kind"] == "rate"
    assert forge._run(["fine"], 5) == "ok"
    assert forge.last_failure(own=True) is None


def test_a_refused_re_execution_is_retried_then_exits_as_a_failure(
    bridge, monkeypatch, capsys
):
    attempts = []

    def refused(path, arguments):
        attempts.append(arguments)
        raise OSError("interpreter gone")

    monkeypatch.setattr(server.os, "execv", refused)
    monkeypatch.setattr(server, "EXEC_RETRY_SECONDS", 0.0)
    with pytest.raises(SystemExit) as exit:
        server.replace(bridge.home, "0.0.0")
    assert exit.value.code == 1
    assert len(attempts) == server.EXEC_ATTEMPTS
    logged = capsys.readouterr().out
    last = f"attempt {server.EXEC_ATTEMPTS} of {server.EXEC_ATTEMPTS}"
    assert f"failed, {last}: interpreter gone" in logged


def test_an_idle_launcher_asks_for_the_service_once_an_interval():
    assert hook.RELAUNCH_INTERVAL > 5
    with tempfile.TemporaryDirectory(prefix="live-") as temporary:
        directory = Path(temporary)
        lane = directory / "lane"
        lane.mkdir()
        home = directory / "home"
        home.mkdir()
        write_json(directory / "lane-activity.json", {})
        harness = (
            "import os, sys\nfrom pathlib import Path\n"
            "from agent_parley import hook, terminal\n"
            "def asked(home):\n"
            "    with open(os.path.join(home, 'asked'), 'a') as stream:\n"
            "        stream.write('asked\\n')\n"
            "hook.relaunch = asked\n"
            "raise SystemExit(terminal.run("
            "[sys.executable, '-c', 'import time; time.sleep(2.5)'], "
            "Path(sys.argv[1]), dict(os.environ), 'lane', attached=False, "
            "home=Path(sys.argv[2])))"
        )
        done = subprocess.run(
            [sys.executable, "-c", harness, str(lane), str(home)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=30,
            start_new_session=True,
            check=False,
        )
        assert done.returncode == 0, done.stderr
        assert (home / "asked").read_text() == "asked\n"
