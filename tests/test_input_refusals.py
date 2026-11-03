"""Invalid command input is refused before it reaches any state."""

import sys
from pathlib import Path

import pytest

from agent_parley import BridgeError, cli, start, store

DEFAULT = Path("~/.local/state/agent-parley")


def test_an_empty_home_variable_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("AGENT_PARLEY_HOME", "")
    parser, _ = cli.root_parser(None)
    assert parser.parse_args(["status"]).home == DEFAULT


def test_bare_status_reads_the_default_home_when_the_variable_is_empty(
    monkeypatch,
):
    seen = []

    def refuse(home):
        seen.append(home)
        raise BridgeError("stopped")

    monkeypatch.setenv("AGENT_PARLEY_HOME", "")
    monkeypatch.setattr(cli, "Bridge", refuse)
    assert cli._plain_status() == 1
    assert seen == [DEFAULT]


def test_the_start_screen_reads_the_default_home_when_the_variable_is_empty(
    monkeypatch, capsys
):
    seen = []
    monkeypatch.setenv("AGENT_PARLEY_HOME", "")
    monkeypatch.setattr(
        start, "look", lambda home, directory: seen.append(home)
    )
    monkeypatch.setattr(start, "render", lambda view: "")
    assert start.show() == 0
    assert seen == [DEFAULT.expanduser()]


@pytest.mark.parametrize(
    "arguments",
    (
        ("metrics", "--every", "0"),
        ("metrics", "--every", "-1"),
        ("watch", "claude", "--interval", "0"),
        ("watch", "claude", "--interval", "-1"),
        ("top", "--interval", "0"),
        ("top", "--interval", "inf"),
    ),
)
def test_a_polling_interval_must_be_positive_and_finite(arguments, capsys):
    parser, _ = cli.root_parser(None)
    with pytest.raises(SystemExit):
        parser.parse_args(list(arguments))
    assert "invalid duration value" in capsys.readouterr().err


def test_a_polling_interval_still_accepts_fractional_seconds():
    parser, _ = cli.root_parser(None)
    watched = parser.parse_args(["watch", "claude", "--interval", "0.5"])
    assert watched.interval == 0.5
    assert parser.parse_args(["metrics", "--every", "5"]).every == 5.0


def test_top_reports_an_argument_error_with_its_own_usage(
    bridge, monkeypatch, capsys
):
    monkeypatch.setattr(
        sys,
        "argv",
        ["agent-parley", "--home", str(bridge.home), "top", "--sort", "nope"],
    )
    with pytest.raises(SystemExit):
        cli.main()
    error = capsys.readouterr().err
    assert error.startswith("usage: agent-parley top")
    assert "Unknown sort column nope" in error


def test_participant_list_never_creates_an_unknown_project(
    bridge, repo, monkeypatch, capsys
):
    _, directory = bridge.project(repo, create=False)
    monkeypatch.chdir(repo)
    monkeypatch.setattr(
        sys,
        "argv",
        ["agent-parley", "--home", str(bridge.home), "participant", "list"],
    )
    assert cli.main() == 1
    assert "no bridge project" in capsys.readouterr().err
    assert not directory.exists()


def test_a_store_write_before_up_names_up(tmp_path):
    with pytest.raises(BridgeError) as refused:
        store.decide(tmp_path, "/absent", "Subject", "Body", "k1")
    assert refused.value.next_command == "agent-parley up"
    assert "agent-parley up" in str(refused.value)
