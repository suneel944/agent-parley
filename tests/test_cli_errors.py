"""Checks the JSON error document the CLI prints for runtime failures."""

import json
import subprocess
import sys

import pytest

from agent_parley import BridgeError, cli, views

FAILURES = (
    (BridgeError("service refused"), "bridge", "service refused"),
    (OSError("disk unavailable"), "os", "disk unavailable"),
    (ValueError("bad window"), "value", "bad window"),
    (
        subprocess.TimeoutExpired(["gh", "api"], 5),
        "timeout",
        "Command '['gh', 'api']' timed out after 5 seconds",
    ),
)


def fail_up(monkeypatch, tmp_path, exc, *flags, partial=""):
    """Makes ``up`` raise one failure and returns the CLI exit status."""

    def up(self):
        if partial:
            print(partial)
        raise exc

    monkeypatch.setattr(cli.Bridge, "up", up)
    monkeypatch.setattr(
        sys,
        "argv",
        ["agent-parley", "--home", str(tmp_path), "up", *flags],
    )
    return cli.main()


@pytest.mark.parametrize(("exc", "kind", "message"), FAILURES)
def test_json_mode_prints_one_error_document(
    monkeypatch, capsys, tmp_path, exc, kind, message
):
    assert fail_up(monkeypatch, tmp_path, exc, "--json") == 1
    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert len(lines) == 1
    document = json.loads(lines[0])
    assert document["schema"] == views.SCHEMA
    assert document["kind"] == "error"
    assert document["error"] == {"type": kind, "message": message}
    assert captured.err == f"agent-parley: {message}\n"
    assert "Traceback" not in captured.out + captured.err


@pytest.mark.parametrize(("exc", "kind", "message"), FAILURES)
def test_human_mode_keeps_the_plain_error(
    monkeypatch, capsys, tmp_path, exc, kind, message
):
    assert fail_up(monkeypatch, tmp_path, exc) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"agent-parley: {message}\n"


def test_partial_output_is_followed_by_its_own_error_line(
    monkeypatch, capsys, tmp_path
):
    status = fail_up(
        monkeypatch,
        tmp_path,
        BridgeError("stopped midway"),
        "--json",
        partial='{"schema": "partial"',
    )
    assert status == 1
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == '{"schema": "partial"'
    assert json.loads(lines[-1])["error"]["message"] == "stopped midway"


def test_argument_errors_keep_the_argparse_contract(monkeypatch, capsys):
    monkeypatch.setattr(
        sys, "argv", ["agent-parley", "status", "--json", "--bogus"]
    )
    with pytest.raises(SystemExit) as raised:
        cli.main()
    assert raised.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "usage:" in captured.err
