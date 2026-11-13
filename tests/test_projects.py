"""Exercises recent projects, named workspaces and the commands using them."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_parley import BridgeError, cli, projects, supervision
from agent_parley.cli import Bridge, git
from agent_parley.state import write_json


def checkout(path):
    """Creates one committed repository at ``path``."""
    path.mkdir(parents=True)
    git(path, "init")
    (path / "readme.txt").write_text("first\n")
    git(path, "add", "readme.txt")
    git(path, "commit", "-m", "Initial")
    return path


@pytest.fixture
def pair(bridge, tmp_path):
    """Registers a front-end and a back-end project in one state root."""
    front = checkout(tmp_path / "front")
    back = checkout(tmp_path / "back")
    return (
        bridge.setup(front)["root"],
        bridge.setup(back)["root"],
    )


def invoke(monkeypatch, capsys, bridge, *arguments):
    """Runs one CLI invocation against the bridge's home."""
    monkeypatch.setattr(
        sys, "argv", ["agent-parley", "--home", str(bridge.home), *arguments]
    )
    status = cli.main()
    captured = capsys.readouterr()
    return status, captured.out, captured.err


def test_projects_list_most_recently_used_first(bridge, pair):
    front, back = pair
    projects.touch(bridge.home, front, now=100.0)
    projects.touch(bridge.home, back, now=200.0)
    assert [entry["root"] for entry in projects.listed(bridge.home)] == [
        back,
        front,
    ]
    projects.touch(bridge.home, front, now=300.0)
    listed = projects.listed(bridge.home)
    assert [entry["name"] for entry in listed] == ["front", "back"]
    assert listed[0]["last_used"] == 300.0


def test_records_live_in_the_state_root_not_the_repositories(bridge, pair):
    front, back = pair
    projects.touch(bridge.home, front)
    projects.set_workspace(bridge.home, "stack", ["front", "back"])
    assert (bridge.home / projects.RECENT).exists()
    assert (bridge.home / projects.WORKSPACES).exists()
    for root in pair:
        assert git(root, "status", "--porcelain") == ""


def test_a_project_resolves_by_name_or_root(bridge, pair):
    front, _ = pair
    assert projects.resolve(bridge.home, "front") == front
    assert projects.resolve(bridge.home, front) == front
    with pytest.raises(BridgeError, match="No registered project"):
        projects.resolve(bridge.home, "missing")


def test_two_projects_sharing_a_name_need_their_root(bridge, tmp_path, pair):
    other = checkout(tmp_path / "elsewhere" / "front")
    bridge.setup(other)
    with pytest.raises(BridgeError, match="2 projects are named front"):
        projects.resolve(bridge.home, "front")
    assert projects.resolve(bridge.home, str(other)) == str(other)


def test_workspaces_record_replace_and_forget(bridge, pair):
    front, back = pair
    assert projects.set_workspace(
        bridge.home, "stack", ["front", "back", "front"]
    ) == [front, back]
    assert projects.members(bridge.home, "stack") == [front, back]
    projects.set_workspace(bridge.home, "stack", ["back"])
    assert projects.workspaces(bridge.home) == {"stack": [back]}
    grouped = {
        entry["root"]: entry["workspaces"]
        for entry in projects.listed(bridge.home)
    }
    assert grouped == {front: [], back: ["stack"]}
    projects.remove_workspace(bridge.home, "stack")
    assert projects.workspaces(bridge.home) == {}
    with pytest.raises(BridgeError, match="No workspace"):
        projects.members(bridge.home, "stack")


def test_a_workspace_refuses_a_bad_name_or_no_projects(bridge, pair):
    with pytest.raises(BridgeError, match="invalid"):
        projects.set_workspace(bridge.home, "two words", ["front"])
    with pytest.raises(BridgeError, match="at least one"):
        projects.set_workspace(bridge.home, "stack", [])


def test_projects_command_lists_names_and_roots(
    monkeypatch, capsys, bridge, pair
):
    status, out, _ = invoke(monkeypatch, capsys, bridge, "projects")
    assert status == 0
    for root in pair:
        assert root in out
    status, out, _ = invoke(monkeypatch, capsys, bridge, "projects", "--json")
    document = json.loads(out)
    assert document["kind"] == "projects"
    assert {entry["root"] for entry in document["projects"]} == set(pair)


def test_workspace_command_records_and_lists(monkeypatch, capsys, bridge, pair):
    front, back = pair
    status, out, _ = invoke(
        monkeypatch, capsys, bridge, "workspace", "set", "stack", "front", back
    )
    assert status == 0
    assert out.strip() == f"stack: {front}, {back}"
    _, out, _ = invoke(monkeypatch, capsys, bridge, "workspace", "list")
    assert out.strip() == f"stack: {front}, {back}"
    _, out, _ = invoke(
        monkeypatch, capsys, bridge, "workspace", "remove", "stack"
    )
    assert "No workspaces" in out


def test_run_project_launches_in_that_project(
    monkeypatch, capsys, bridge, pair
):
    _, back = pair
    launched = []

    def launch(self, agent, repo, *args, **kwargs):
        launched.append((agent, str(repo)))
        return 0

    monkeypatch.setattr(Bridge, "launch", launch)
    status, _, _ = invoke(
        monkeypatch, capsys, bridge, "run", "claude", "--project", "back"
    )
    assert status == 0
    assert launched == [("claude", back)]
    assert projects.listed(bridge.home)[0]["root"] == back


def test_run_refuses_both_repo_and_project(monkeypatch, capsys, bridge, pair):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agent-parley",
            "--home",
            str(bridge.home),
            "run",
            "claude",
            "--repo",
            pair[0],
            "--project",
            "back",
        ],
    )
    with pytest.raises(SystemExit):
        cli.main()
    assert "not allowed with" in capsys.readouterr().err


def test_status_workspace_reports_only_its_projects(
    monkeypatch, capsys, bridge, pair, tmp_path
):
    front, back = pair
    third = bridge.setup(checkout(tmp_path / "third"))["root"]
    projects.set_workspace(bridge.home, "stack", ["front", "back"])
    _, out, _ = invoke(
        monkeypatch, capsys, bridge, "status", "--workspace", "stack", "--json"
    )
    shown = {project["root"] for project in json.loads(out)["projects"]}
    assert shown == {front, back}
    assert third not in shown


def test_start_lanes_resumes_only_stopped_recorded_sessions(
    monkeypatch, bridge, pair
):
    front, _ = pair
    bridge.add_participant(front, "claude", "claude")
    bridge.add_participant(front, "codex", "codex")
    directory = bridge.project(Path(front), create=False)[1]
    write_json(
        directory / "claude-activity.json",
        {"session_id": "native-1", "session_pid": 2**22, "session_ticks": "1"},
    )
    resumed = []

    def resume(home, place, manifest, name, task=""):
        resumed.append((place, manifest["root"], name))
        return SimpleNamespace(pid=4321)

    monkeypatch.setattr(supervision, "_resume", resume)
    monkeypatch.setattr(supervision, "opt_in_missing", lambda *args: False)
    outcomes = projects.start_lanes(bridge.home, front)
    assert resumed == [(directory, front, "claude")]
    assert outcomes["claude"].startswith("resuming (launcher 4321")
    assert outcomes["codex"].startswith("skipped: no recorded session")


def test_up_workspace_starts_every_member_projects_lanes(
    monkeypatch, capsys, bridge, pair
):
    front, back = pair
    projects.set_workspace(bridge.home, "stack", ["front", "back"])
    started = []
    monkeypatch.setattr(Bridge, "up", lambda self: None)
    monkeypatch.setattr(
        projects,
        "start_lanes",
        lambda home, root: started.append(root) or {"claude": "resumed"},
    )
    status, out, _ = invoke(
        monkeypatch, capsys, bridge, "up", "--workspace", "stack"
    )
    assert status == 0
    assert started == [front, back]
    assert f"{back}\n  claude: resumed" in out


def test_up_with_an_unknown_workspace_starts_nothing(
    monkeypatch, capsys, bridge, pair
):
    calls = []
    monkeypatch.setattr(Bridge, "up", lambda self: calls.append("up"))
    status, _, err = invoke(
        monkeypatch, capsys, bridge, "up", "--workspace", "missing"
    )
    assert status == 1
    assert calls == []
    assert "No workspace is named missing" in err
