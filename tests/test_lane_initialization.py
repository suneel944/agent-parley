"""Checks the recorded command every new lane runs before its agent starts."""

import json
import os
import sys
from pathlib import Path

import pytest

from agent_parley import roster
from agent_parley.state import BridgeError

RECORDER = (
    "import os, pathlib, sys;"
    "pathlib.Path(os.environ['INIT_RECORD']).write_text("
    "os.getcwd() + '\\n' + os.environ.get('AGENT_PARLEY_BASE', '') + '\\n'"
    "+ ' '.join(sys.argv[1:]))"
)
FAILING = (
    "import sys;"
    "print('preparing the lane');"
    "print('missing dependency', file=sys.stderr);"
    "sys.exit(3)"
)


def configured(bridge, repo, script, monkeypatch, tmp_path):
    """Records a Python initialization command and reports its receipt."""
    record = tmp_path / "initialization.txt"
    monkeypatch.setenv("INIT_RECORD", str(record))
    bridge.initialization(repo, f"{sys.executable} -c {script!r} alpha")
    return record


def test_init_show_reports_no_command_until_one_is_set(bridge, repo):
    bridge.setup(repo)
    assert "no lane initialization command" in bridge.initialization(repo)
    message = bridge.initialization(repo, "true")
    assert "runs `true` in every new lane" in message
    assert "AGENT_PARLEY_BASE" in message
    assert bridge.initialization(repo) == message
    assert "no lane initialization command" in bridge.initialization(repo, "")


def test_a_recorded_command_runs_in_each_new_lane_worktree(
    bridge, repo, monkeypatch, tmp_path
):
    bridge.setup(repo)
    record = configured(bridge, repo, RECORDER, monkeypatch, tmp_path)
    manifest = bridge.add_participant(repo, "claude", "claude")
    lane = Path(manifest["lanes"]["claude"])
    directory, base, arguments = record.read_text().splitlines()
    assert Path(directory).resolve() == lane.resolve()
    assert Path(base).resolve() == repo.resolve()
    assert arguments == "alpha"


def test_a_failing_command_refuses_the_lane_and_keeps_the_worktree(
    bridge, repo, monkeypatch, tmp_path
):
    bridge.setup(repo)
    configured(bridge, repo, FAILING, monkeypatch, tmp_path)
    with pytest.raises(BridgeError) as refusal:
        bridge.add_participant(repo, "claude", "claude")
    message = str(refusal.value)
    assert "exited 3" in message
    assert "left in place for inspection" in message
    assert "missing dependency" in message
    _, directory = bridge.project(repo)
    assert (directory / "claude").is_dir()
    assert "claude" not in roster.read(directory)["participants"]


def test_an_unrunnable_command_reports_how_to_correct_it(bridge, repo):
    bridge.setup(repo)
    bridge.initialization(repo, "agent-parley-no-such-executable")
    with pytest.raises(BridgeError, match="could not run"):
        bridge.add_participant(repo, "claude", "claude")


def test_the_command_is_stored_outside_the_target_repository(
    bridge, repo, monkeypatch, tmp_path
):
    bridge.setup(repo)
    configured(bridge, repo, RECORDER, monkeypatch, tmp_path)
    _, directory = bridge.project(repo)
    manifest = json.loads((directory / "project.json").read_text())
    assert manifest["initialize"][0] == sys.executable
    assert not list(repo.glob(".agent-parley*"))
    assert "initialize" not in os.listdir(repo)


@pytest.mark.parametrize(
    "command", ["'unterminated", "runner " + "argument " * 200]
)
def test_an_unusable_command_is_refused_by_name(bridge, repo, command):
    bridge.setup(repo)
    with pytest.raises(BridgeError, match="Lane initialization command"):
        bridge.initialization(repo, command)


@pytest.mark.parametrize("command", ["", "   "])
def test_an_empty_command_removes_the_setting(bridge, repo, command):
    bridge.setup(repo)
    bridge.initialization(repo, "true")
    assert "no lane initialization" in bridge.initialization(repo, command)
