"""Checks that local server startup never depends on name resolution."""

import contextlib
import re
import socket

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
