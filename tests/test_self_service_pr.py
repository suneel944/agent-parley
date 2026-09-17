"""Checks the repository policy that lets a lane open its own request."""

import json
import os
import shlex
from pathlib import Path

import pytest

from agent_parley import evidence, forge, metrics, roster, store
from agent_parley.cli import git
from agent_parley.state import BridgeError, lock, write_json

GH_STUB = """#!/bin/sh
if [ "$1" = "issue" ]; then
  cat "$GH_ISSUE"
  exit 0
fi
if [ "$2" = "list" ]; then
  echo '[]'
  exit 0
fi
: > "$GH_CREATE"
for argument in "$@"; do
  printf '%s\\0' "$argument" >> "$GH_CREATE"
done
echo "https://github.com/example/agent-parley/pull/7"
"""


def identify(worktree):
    """Gives a fixture repository the identity a commit needs."""
    git(worktree, "config", "user.name", "Bridge Test")
    git(worktree, "config", "user.email", "test@example.com")


def commit(worktree, message):
    """Records every pending change with a fixed identity."""
    git(worktree, "add", "--all")
    git(
        worktree,
        "-c",
        "user.name=Bridge Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-m",
        message,
    )


def stub_github_cli(tmp_path, monkeypatch):
    """Puts a recording GitHub CLI first on PATH so no request leaves."""
    binaries = tmp_path / "bin"
    binaries.mkdir()
    executable = binaries / "gh"
    executable.write_text(GH_STUB)
    executable.chmod(0o755)
    created = tmp_path / "created-arguments"
    issue = tmp_path / "issue-metadata.json"
    issue.write_text(
        json.dumps({"labels": [{"name": "enhancement"}], "milestone": None})
    )
    monkeypatch.setenv("PATH", f"{binaries}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("GH_CREATE", str(created))
    monkeypatch.setenv("GH_ISSUE", str(issue))
    monkeypatch.setattr(forge, "slug", lambda repo: "example/agent-parley")
    return created


def created_options(created):
    """Reads the flags the stubbed GitHub CLI was asked to create with."""
    arguments = created.read_text().split("\0")[:-1]
    return dict(zip(arguments[2::2], arguments[3::2], strict=True))


def green_gate(tmp_path, marker):
    """Writes a verification command that passes and records where it ran."""
    script = tmp_path / "green.sh"
    script.write_text(f"#!/bin/sh\npwd -P > {shlex.quote(str(marker))}\n")
    script.chmod(0o755)
    return shlex.quote(str(script))


def allow_self_service(directory, permitted=True):
    """Turns the repository's self-service pull-request policy on or off."""
    manifest = roster.read(directory)
    manifest["pull_request"] = {"self_service": permitted}
    write_json(directory / "project.json", manifest)


def opened_records(directory, name):
    """Returns the recorded pull requests one lane opened."""
    return [
        record
        for record in metrics.report_records(directory, name)
        if record.get("action") == "pull_request"
    ]


@pytest.fixture
def ready(bridge, repo, paired, tmp_path, monkeypatch):
    """Leaves one lane committed, reported ready and claiming an issue."""
    lane = Path(paired["lanes"]["codex"])
    identify(repo)
    remote = tmp_path / "origin.git"
    git(repo, "init", "--bare", str(remote))
    git(repo, "remote", "add", "origin", str(remote))
    created = stub_github_cli(tmp_path, monkeypatch)
    marker = tmp_path / "gate-ran.txt"
    bridge.verification(repo, green_gate(tmp_path, marker))
    (lane / "feature.txt").write_text("lane work\n")
    commit(lane, "feat: add the lane feature")
    bridge.report(lane, "ready", "Lane result", "", "make check: 12 passed")
    bridge.issue(lane, "claim", "42")
    return {
        "lane": lane,
        "branch": paired["branches"]["codex"],
        "directory": lane.parent,
        "remote": remote,
        "created": created,
        "marker": marker,
    }


def test_the_policy_is_off_until_a_repository_turns_it_on(bridge, repo, ready):
    directory = ready["directory"]
    with lock(directory / "codex.session.lock"):
        with pytest.raises(BridgeError, match="session owns"):
            bridge.pull_request(ready["lane"], "codex")
    assert git(repo, "branch", "--remotes") == ""

    message = bridge.pull_request(repo, "codex")
    assert message == (
        f"Pushed {ready['branch']} and opened "
        "https://github.com/example/agent-parley/pull/7"
    )
    body = created_options(ready["created"])["--body"]
    assert "self-service" not in body
    assert evidence.SECTION_START in body
    assert "authorization" not in opened_records(directory, "codex")[-1]


def test_a_lane_opens_its_own_request_while_its_session_is_live(
    bridge, repo, ready
):
    directory = ready["directory"]
    allow_self_service(directory)

    with lock(directory / "codex.session.lock"):
        message = bridge.pull_request(ready["lane"], "codex")

    assert "https://github.com/example/agent-parley/pull/7" in message
    assert "self-service policy" in message
    assert git(
        ready["remote"], "log", "-1", "--pretty=%s", ready["branch"]
    ) == ("feat: add the lane feature")
    assert ready["marker"].exists()

    body = created_options(ready["created"])["--body"]
    assert "Opened by codex itself" in body
    assert "pull_request.self_service" in body
    assert "clear of the reservations held by no peer" in body

    recorded = opened_records(directory, "codex")[-1]["authorization"]
    assert recorded["policy"] == "pull_request.self_service"
    assert recorded["branch"] == ready["branch"]
    assert recorded["changed_paths"] == 1
    assert recorded["gate"].endswith("green.sh")


def test_an_operator_outside_the_lane_keeps_the_session_exclusion(
    bridge, repo, ready
):
    allow_self_service(ready["directory"])
    with lock(ready["directory"] / "codex.session.lock"):
        with pytest.raises(BridgeError, match="session owns"):
            bridge.pull_request(repo, "codex")
    assert git(repo, "branch", "--remotes") == ""


def test_each_unmet_condition_refuses_the_lane_by_name(
    bridge, repo, paired, ready
):
    lane = ready["lane"]
    directory = ready["directory"]
    allow_self_service(directory)

    bridge.verification(repo, "")
    with pytest.raises(BridgeError, match="no verification command"):
        bridge.pull_request(lane, "codex")
    bridge.verification(repo, green_gate(directory, ready["marker"]))

    bridge.report(lane, "partial", "Half done", "the rest", "")
    with pytest.raises(BridgeError, match="reported partial, not ready"):
        bridge.pull_request(lane, "codex")
    bridge.report(lane, "ready", "Lane result", "", "make check: 12 passed")

    git(lane, "checkout", "-b", "sidetrack")
    with pytest.raises(BridgeError, match="expected"):
        bridge.pull_request(lane, "codex")
    git(lane, "checkout", ready["branch"])

    root, _ = bridge.project(repo)
    store.initialize(bridge.home)
    token = store.register(bridge.home, str(root), "claude")
    peer = store.authenticate(bridge.home, token["registration_token"])
    assert peer is not None
    store.call(
        bridge.home,
        peer,
        "file_reservation_paths",
        {"paths": ["feature.txt"]},
    )
    with pytest.raises(BridgeError, match="peer reservation covers") as held:
        bridge.pull_request(lane, "codex")
    assert "claude reserves 'feature.txt'" in str(held.value)

    assert git(repo, "branch", "--remotes") == ""
    assert not ready["created"].exists()
    assert opened_records(directory, "codex") == []
