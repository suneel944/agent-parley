"""Checks that local server startup never depends on name resolution."""

import contextlib
import re
import socket
import threading

from agent_parley import server
from agent_parley.server import Server
from agent_parley.state import MAX_LOG_BYTES

STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4} ")


def test_server_binds_without_reverse_dns(tmp_path, monkeypatch):
    def unexpected_lookup(*args):
        raise AssertionError("Loopback startup must not resolve a hostname")

    monkeypatch.setattr(socket, "getfqdn", unexpected_lookup)
    with Server(tmp_path, {"port": 0, "token": "test"}) as instance:
        assert instance.server_name == "127.0.0.1"
        assert instance.server_port > 0


def test_a_start_and_a_stop_each_leave_one_timestamped_line(bridge):
    bridge.up()
    assert bridge.health()["status"] == "ready"
    bridge.down()
    written = (bridge.home / "server.log").read_text()
    lines = written.splitlines()
    bound = [line for line in lines if " bound 127.0.0.1:" in line]
    stopped = [line for line in lines if " stopped " in line]
    assert len(bound) == 1
    assert len(stopped) == 1
    assert all(STAMP.match(line) for line in bound + stopped)
    assert " version " in bound[0]
    assert bridge.config["token"] not in written
    stamped = [line for line in lines if STAMP.match(line)]
    events = [line.split()[1] for line in stamped]
    assert events.index("signalled") < events.index("stopped")
    assert "SIGTERM" in stamped[events.index("signalled")]


def test_the_service_log_stays_under_its_rotation_bound(tmp_path):
    path = tmp_path / "server.log"
    with path.open("a", encoding="utf-8") as stream:
        with contextlib.redirect_stdout(stream):
            for number in range(6000):
                server.log(
                    tmp_path,
                    "refused",
                    f"{number} connection(s) closed unanswered with all "
                    "16 worker slots busy",
                )
    written = path.read_text()
    assert path.stat().st_size < MAX_LOG_BYTES
    assert "5999 connection(s)" in written.splitlines()[-1]


def test_a_refusal_burst_writes_one_line_per_lane_per_window(
    tmp_path, monkeypatch
):
    clock = [1000.0]
    monkeypatch.setattr(server.time, "monotonic", lambda: clock[0])
    path = tmp_path / "server.log"
    lane = ("/work/project", "claude-p2-4")
    with Server(tmp_path, {"port": 0, "token": "test"}) as instance:
        with path.open("a", encoding="utf-8") as stream:
            with contextlib.redirect_stdout(stream):
                for _ in range(5000):
                    instance.coalesce("undecided", lane, "claude-p2-4 held")
                instance.coalesce("undecided", ("/other", "claude"), "other")
                clock[0] += server.REPEAT_SECONDS
                instance.coalesce("undecided", lane, "claude-p2-4 held")
    lines = [
        line for line in path.read_text().splitlines() if " undecided " in line
    ]
    assert len(lines) == 3
    assert lines[0].endswith("undecided claude-p2-4 held")
    assert lines[1].endswith("undecided other")
    assert lines[2].endswith("; 4999 more since the previous entry")


def test_a_stopped_burst_still_writes_its_count(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(server.time, "monotonic", lambda: clock[0])
    path = tmp_path / "server.log"
    first = ("/work/project", "claude-p2-4")
    second = ("/work/other", "codex")
    with Server(tmp_path, {"port": 0, "token": "test"}) as instance:
        with path.open("a", encoding="utf-8") as stream:
            with contextlib.redirect_stdout(stream):
                for _ in range(30):
                    instance.coalesce("undecided", first, "first held")
                for _ in range(7):
                    instance.coalesce("unanswered", second, "second held")
                clock[0] += server.REPEAT_SECONDS
                instance.coalesce("undecided", ("/new", "gemini"), "new")
                instance.coalesce("undecided", ("/new", "gemini"), "new")
                instance.flush_repeats()
                instance.flush_repeats()
    lines = path.read_text().splitlines()
    assert [line.split(" ", 1)[1] for line in lines] == [
        "undecided first held",
        "unanswered second held",
        "undecided first held; 29 more since the previous entry",
        "unanswered second held; 6 more since the previous entry",
        "undecided new",
        "undecided new; 1 more since the previous entry",
    ]


def test_a_rotation_under_concurrent_writes_loses_no_line(tmp_path):
    path = tmp_path / "server.log"
    writers = 8
    count = 400
    pad = "x" * 80

    def write(writer):
        for number in range(count):
            server.log(tmp_path, "refused", f"{writer}:{number} {pad}")

    with path.open("a", encoding="utf-8") as stream:
        with contextlib.redirect_stdout(stream):
            threads = [
                threading.Thread(target=write, args=(writer,))
                for writer in range(writers)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
    rotated = tmp_path / server.ROTATED_NAME
    kept = rotated.read_text().splitlines() + path.read_text().splitlines()
    assert rotated.stat().st_size > 0
    assert path.stat().st_size < MAX_LOG_BYTES
    written = sorted(line.split()[2] for line in kept)
    assert written == sorted(
        f"{writer}:{number}"
        for writer in range(writers)
        for number in range(count)
    )
