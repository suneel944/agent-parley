"""Checks that the forge is selected per project and every choice holds."""

import os
import sys

import pytest

from agent_parley import cli, forge, roster
from agent_parley.state import BridgeError

BD_STUB = """#!/bin/sh
for argument in "$@"; do
  printf '%s\\0' "$argument" >> "$BD_CALLS"
done
if [ -n "$BD_FAILS" ]; then
  echo "the ledger refused" >&2
  exit 1
fi
if [ "$1" = "show" ]; then
  echo '{"id": "42", "title": "Beads title", "status": "open"}'
fi
exit 0
"""


def stub_beads_cli(tmp_path, monkeypatch):
    """Puts a recording ``bd`` client first on PATH."""
    binaries = tmp_path / "bin"
    binaries.mkdir()
    executable = binaries / "bd"
    executable.write_text(BD_STUB)
    executable.chmod(0o755)
    calls = tmp_path / "bd-calls"
    monkeypatch.setenv("PATH", f"{binaries}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("BD_CALLS", str(calls))
    monkeypatch.delenv("BD_FAILS", raising=False)
    return calls


def test_github_is_the_default_and_is_recorded_at_register(
    bridge, repo, paired
):
    assert forge.select(repo) == "github"
    directory = bridge.project(repo)[1]
    assert roster.read(directory)["forge"] == "github"
    assert "github forge (recorded)" in bridge.tracker(repo)


def test_a_beads_ledger_is_detected(bridge, repo):
    (repo / ".beads").mkdir()
    assert forge.select(repo) == "beads"
    bridge.add_participant(repo, "claude", "claude")
    directory = bridge.project(repo)[1]
    assert roster.read(directory)["forge"] == "beads"
    assert "beads forge (recorded)" in bridge.tracker(repo)


def test_an_explicit_manifest_value_wins_over_detection(bridge, repo, paired):
    (repo / ".beads").mkdir()
    assert forge.select(repo, {"forge": "null"}) == "null"
    assert forge.select(repo, {"forge": "github", "root": str(repo)}) == (
        "github"
    )
    assert forge.select(repo, {"forge": None, "root": str(repo)}) == "beads"
    assert "null forge (recorded)" in bridge.tracker(repo, "null")
    assert roster.read(bridge.project(repo)[1])["forge"] == "null"
    with pytest.raises(BridgeError, match="must be one of"):
        bridge.tracker(repo, "jira")
    with pytest.raises(BridgeError, match="must be one of"):
        roster.normalize(
            {
                "version": 2,
                "root": str(repo),
                "base": "x",
                "forge": "jira",
                "participants": {},
            }
        )
    assert roster.forge_choice(None) is None


def test_the_null_forge_keeps_numbers_bare_and_calls_nothing(repo, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("the null forge must never run a client")

    monkeypatch.setattr(forge.subprocess, "run", explode)
    monkeypatch.setattr(forge.shutil, "which", explode)
    assert forge.select(repo, {"forge": "null"}) == "null"
    assert forge.issue_title(repo, "42") is None
    assert forge.issue_pull_request_paths(repo, "42") == []
    assert forge.branch_completion(repo, "parley/x/lane-1") is None
    assert forge.assign(repo, "42") is False
    assert forge.unassign(repo, "42") is False
    assert forge.comment(repo, "42", "Lane account") is False


def test_beads_speaks_through_bd_and_absorbs_failure(
    repo, tmp_path, monkeypatch
):
    calls = stub_beads_cli(tmp_path, monkeypatch)
    (repo / ".beads").mkdir()
    assert forge.select(repo) == "beads"
    assert forge.issue_title(repo, "42") == "Beads title"
    assert forge.assign(repo, "42") is True
    assert forge.unassign(repo, "42") is True
    assert forge.comment(repo, "42", "Lane account") is True
    assert forge.issue_pull_request_paths(repo, "42") == []
    assert forge.branch_completion(repo, "parley/x/lane-1") is None
    recorded = calls.read_text().split("\0")[:-1]
    assert recorded[:3] == ["show", "42", "--json"]
    assert recorded[3:6] == ["update", "42", "--assignee"]
    assert recorded[6] and recorded[7:10] == ["update", "42", "--assignee"]
    assert recorded[10] == ""
    assert recorded[11:] == ["comment", "42", "Lane account"]
    monkeypatch.setenv("BD_FAILS", "1")
    assert forge.issue_title(repo, "42") is None
    assert forge.assign(repo, "42") is False
    assert forge.unassign(repo, "42") is False
    assert forge.comment(repo, "42", "Lane account") is False
    monkeypatch.setattr(forge.shutil, "which", lambda command: None)
    assert forge.issue_title(repo, "42") is None
    assert forge.assign(repo, "42") is False
    assert forge.comment(repo, "42", "Lane account") is False


def test_a_claim_under_beads_mirrors_through_bd_not_gh(
    bridge, repo, paired, tmp_path, monkeypatch
):
    calls = stub_beads_cli(tmp_path, monkeypatch)
    bridge.tracker(repo, "beads")
    monkeypatch.setattr(
        forge, "slug", lambda directory: pytest.fail("gh must not be asked")
    )
    lane = paired["lanes"]["claude"]
    record = bridge.issue(lane, "claim", "42")
    assert record["title"] == "Beads title"
    recorded = calls.read_text().split("\0")[:-1]
    assert recorded[:3] == ["show", "42", "--json"]
    assert recorded[3:6] == ["update", "42", "--assignee"]


def test_participant_pr_refuses_under_a_forge_without_pull_requests(
    bridge, repo, paired, monkeypatch, capsys
):
    bridge.tracker(repo, "null")
    with pytest.raises(BridgeError, match="opens no pull requests"):
        bridge.pull_request(repo, "claude")
    bridge.tracker(repo, "beads")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agent-parley",
            "--home",
            str(bridge.home),
            "participant",
            "pr",
            "claude",
            "--repo",
            str(repo),
        ],
    )
    assert cli.main() == 1
    printed = capsys.readouterr().err.strip()
    assert "beads forge, which opens no pull requests" in printed
    assert "\n" not in printed and printed.endswith("nothing was pushed.")


def test_the_forge_command_shows_and_sets_the_choice(
    bridge, repo, paired, monkeypatch, capsys
):
    for arguments in (["show"], ["set", "null"], ["show"]):
        monkeypatch.setattr(
            sys,
            "argv",
            ["agent-parley", "--home", str(bridge.home), "forge"]
            + arguments
            + ["--repo", str(repo)],
        )
        assert cli.main() == 0
    printed = capsys.readouterr().out
    assert "github forge (recorded), which opens pull requests" in printed
    assert "null forge (recorded), which opens no pull requests" in printed
