"""Checks the read-only loopback web view of the dashboard over real HTTP."""

import http.client
import json
import re
import sys
import threading

import pytest

from agent_parley import cli, dashboard, webview

SAMPLE = re.compile(
    r"^[a-zA-Z_:][a-zA-Z0-9_:]*(\{.*\})? -?[0-9.eE+-]+(?: [0-9]+)?$"
)


@pytest.fixture
def reads(bridge, paired):
    """Counts the dashboard readings a served view makes."""
    calls = []

    def read():
        calls.append(1)
        return dashboard.select(dashboard.collect(bridge.home, False, {}))

    return read, calls


def start(read):
    """Starts a view on an ephemeral loopback port in a daemon thread."""
    server = webview.Server(0, read, 30.0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


@pytest.fixture
def served(reads):
    """Serves the paired roster and stops the server afterwards."""
    server, thread = start(reads[0])
    yield server
    server.shutdown()
    server.server_close()
    thread.join()


def fetch(server, path, method="GET", host=None):
    """Sends one request and returns its status and decoded body."""
    connection = http.client.HTTPConnection(
        "127.0.0.1", server.server_port, timeout=10
    )
    try:
        headers = {} if host is None else {"Host": host}
        connection.request(method, path, headers=headers)
        response = connection.getresponse()
        return response.status, response.read().decode()
    finally:
        connection.close()


def test_view_binds_loopback_and_prints_a_tokened_url(served):
    assert served.server_address[0] == "127.0.0.1"
    assert served.url == (
        f"http://127.0.0.1:{served.server_port}/{served.token}/"
    )
    assert len(served.token) >= 16


def test_page_lists_every_lane_and_refreshes(served):
    status, body = fetch(served, f"/{served.token}/")
    assert status == 200
    assert "<td>claude</td>" in body
    assert "<td>codex</td>" in body
    assert "<th>STATE</th>" in body
    assert "<th>ISSUES</th>" in body
    assert "<th>IDLE</th>" in body
    assert "<th>PROBLEMS</th>" in body
    assert '<meta http-equiv="refresh" content="30">' in body
    assert "<script" not in body


def test_requests_without_the_token_are_not_found(served):
    for path in (
        "/",
        "/metrics",
        "/wrong/",
        f"/{served.token}",
        f"/{served.token}x/",
        f"/{served.token}/state",
    ):
        assert fetch(served, path)[0] == 404, path


def test_foreign_host_is_forbidden(served):
    for host in (
        f"attacker.example:{served.server_port}",
        "127.0.0.1:1",
        "127.0.0.1",
    ):
        assert fetch(served, f"/{served.token}/", host=host)[0] == 403
    assert (
        fetch(
            served,
            f"/{served.token}/",
            host=f"localhost:{served.server_port}",
        )[0]
        == 200
    )


def test_methods_other_than_get_are_refused(served):
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        assert fetch(served, f"/{served.token}/", method=method)[0] == 405


def test_page_escapes_lane_values(bridge, paired):
    hostile = "<script>alert(1)</script>"

    def read():
        view = dashboard.collect(bridge.home, False, {})
        view["projects"][0]["rows"][0]["participant"] = hostile
        view["projects"][0]["rows"][0]["prompt"] = hostile
        return view

    server, thread = start(read)
    try:
        status, body = fetch(server, f"/{server.token}/")
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert status == 200
    assert hostile not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body


def test_frame_matches_top_json(served, bridge, monkeypatch, capsys):
    status, body = fetch(served, f"/{served.token}/frame.json")
    assert status == 200
    frame = json.loads(body)
    monkeypatch.setattr(
        sys,
        "argv",
        ["agent-parley", "--home", str(bridge.home), "top", "--json"],
    )
    assert cli.main() == 0
    printed = json.loads(capsys.readouterr().out)
    assert frame.keys() == printed.keys()
    assert frame["kind"] == "top"
    assert [
        sorted(row["participant"] for row in project["participants"])
        for project in frame["projects"]
    ] == [
        sorted(row["participant"] for row in project["participants"])
        for project in printed["projects"]
    ]
    assert frame["projects"][0]["participants"][0].keys() == (
        printed["projects"][0]["participants"][0].keys()
    )


def test_metrics_are_exposition_text(served):
    status, body = fetch(served, f"/{served.token}/metrics")
    assert status == 200
    lines = [line for line in body.splitlines() if line]
    assert any(not line.startswith("#") for line in lines)
    for line in lines:
        assert line.startswith("# ") or SAMPLE.match(line), line


def test_readings_are_cached_for_the_interval(served, reads):
    for route in ("", "frame.json", "metrics", ""):
        assert fetch(served, f"/{served.token}/{route}")[0] == 200
    assert len(reads[1]) == 1


def test_serve_refuses_json_and_once(bridge, monkeypatch, capsys):
    for flag in ("--json", "--once"):
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "agent-parley",
                "--home",
                str(bridge.home),
                "top",
                "--serve",
                flag,
            ],
        )
        with pytest.raises(SystemExit) as raised:
            cli.main()
        assert raised.value.code == 2
    assert "--serve cannot be combined" in capsys.readouterr().err


def test_top_serve_prints_the_loopback_url(bridge, paired, monkeypatch, capsys):
    monkeypatch.setattr(
        webview.Server, "serve_forever", lambda self, *args: None
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["agent-parley", "--home", str(bridge.home), "top", "--serve"],
    )
    assert cli.main() == 0
    printed = capsys.readouterr().out
    assert re.search(r"http://127\.0\.0\.1:[0-9]+/[\w-]{16,}/", printed)
