"""Checks child lanes against the real store, MCP transport and Git."""

import json
from pathlib import Path

import httpx
import pytest

from agent_parley import children, roster, store, supervision
from agent_parley.cli import git
from agent_parley.state import BridgeError


def served(service, token, tool, arguments):
    """Calls one tool over the authenticated local transport."""
    response = httpx.post(
        f"http://127.0.0.1:{service.server_port}/mcp/",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        },
        timeout=30,
        trust_env=False,
    )
    assert response.status_code == 200
    return response.json()["result"]


def decoded(result):
    """Returns the payload of a tool result that succeeded."""
    assert "isError" not in result, result
    return json.loads(result["content"][0]["text"])


@pytest.fixture
def launched(monkeypatch):
    """Records each child launch instead of starting a native CLI."""
    calls = []
    monkeypatch.setattr(
        children,
        "launch",
        lambda home, directory, root, name, task: calls.append(
            {"name": name, "root": root, "task": task}
        ),
    )
    return calls


@pytest.fixture
def parent(bridge, repo):
    """Registers one parent lane with the store and returns its token."""
    data = bridge.add_participant(repo, "claude", "claude")
    store.initialize(bridge.home)
    token = store.register(bridge.home, data["root"], "claude")[
        "registration_token"
    ]
    return {
        "root": data["root"],
        "token": token,
        "directory": roster.locate(bridge.home, data["root"]),
    }


def commit(lane, name, text):
    """Commits one file in a lane's worktree."""
    (lane / name).parent.mkdir(parents=True, exist_ok=True)
    (lane / name).write_text(text)
    git(lane, "add", name)
    git(lane, "commit", "-m", f"Add {name}")


def test_a_parent_spawns_two_children_and_receives_two_capped_reports(
    bridge, repo, parent, service, launched
):
    spawned = [
        decoded(
            served(
                service,
                parent["token"],
                "spawn_child",
                {"task": f"Write part {index}", "paths": [f"part{index}/"]},
            )
        )
        for index in (1, 2)
    ]
    manifest = roster.read(parent["directory"])
    source = manifest["participants"]["claude"]
    assert [child["child"] for child in spawned] == ["claude-c1", "claude-c2"]
    assert [call["name"] for call in launched] == ["claude-c1", "claude-c2"]
    for index, child in enumerate(spawned, start=1):
        entry = manifest["participants"][child["child"]]
        assert entry["provider"] == source["provider"]
        assert entry["credential"] == source["credential"]
        assert entry["parent"] == "claude"
        assert entry["paths"] == [f"part{index}"]
        assert child["branch"] == f"{source['branch']}-c{index}"
        lane = Path(child["worktree"])
        assert git(lane, "rev-parse", "HEAD") == git(
            Path(source["lane"]), "rev-parse", "HEAD"
        )
        assert f"part{index}" in launched[index - 1]["task"]
        assert "agent-parley report" in launched[index - 1]["task"]
    tokens = {
        child["child"]: store.register(
            bridge.home, parent["root"], child["child"]
        )["registration_token"]
        for child in spawned
    }
    wide = served(
        service,
        tokens["claude-c1"],
        "file_reservation_paths",
        {"paths": ["part2/x.txt"]},
    )
    assert wide.get("isError") is True
    assert "limited to part1" in wide["content"][0]["text"]
    assert decoded(
        served(
            service,
            tokens["claude-c1"],
            "file_reservation_paths",
            {"paths": ["part1/x.txt"]},
        )
    )["granted"]
    transcript = "TRANSCRIPT-ONLY " * 2000
    for index, child in enumerate(spawned, start=1):
        lane = Path(child["worktree"])
        commit(lane, f"part{index}/x.txt", transcript)
        bridge.report(
            lane, "ready", f"Wrote part {index}", "", "pytest: 3 passed"
        )
        bridge.report(lane, "ready", "Second report", "", "again")
    inbox = decoded(
        served(
            service,
            parent["token"],
            "fetch_inbox",
            {"include_bodies": True},
        )
    )
    results = [
        row for row in inbox["messages"] if row["subject"].startswith("Child ")
    ]
    assert sorted(row["subject"] for row in results) == [
        "Child claude-c1 result: ready",
        "Child claude-c2 result: ready",
    ]
    for row in results:
        assert len(row["body_md"].encode()) <= children.REPORT_BYTES
        assert "TRANSCRIPT-ONLY" not in row["body_md"]
        assert "pytest: 3 passed" in row["body_md"]
        assert "git merge --no-ff" in row["body_md"]
        assert "Second report" not in row["body_md"]
    views = children.of(roster.read(parent["directory"]), "claude")
    assert [view["state"] for view in views] == ["ready", "ready"]


def test_a_spawn_past_the_per_parent_limit_names_the_limit(
    bridge, parent, service, launched, monkeypatch
):
    monkeypatch.setattr(children, "MAX_PER_PARENT", 1)
    arguments = {"task": "Do it", "paths": ["docs"]}
    decoded(served(service, parent["token"], "spawn_child", arguments))
    refused = served(service, parent["token"], "spawn_child", arguments)
    assert refused.get("isError") is True
    assert "per-parent limit of 1" in refused["content"][0]["text"]
    assert len(launched) == 1


def test_a_spawn_past_the_project_limit_names_the_limit(
    bridge, repo, parent, launched, monkeypatch
):
    monkeypatch.setattr(children, "MAX_PER_PROJECT", 1)
    bridge.add_participant(repo, "codex", "codex")
    store.register(bridge.home, parent["root"], "codex")
    actor = {"project": parent["root"], "name": "claude"}
    other = {"project": parent["root"], "name": "codex"}
    children.spawn(bridge.home, actor, {"task": "a", "paths": ["a"]})
    with pytest.raises(BridgeError, match="per-project limit of 1"):
        children.spawn(bridge.home, other, {"task": "b", "paths": ["b"]})


def test_a_child_cannot_spawn_children(bridge, parent, launched):
    actor = {"project": parent["root"], "name": "claude"}
    child = children.spawn(bridge.home, actor, {"task": "a", "paths": ["a"]})
    with pytest.raises(BridgeError, match="itself a child lane"):
        children.spawn(
            bridge.home,
            {"project": parent["root"], "name": child["child"]},
            {"task": "b", "paths": ["a"]},
        )


def test_a_parent_over_its_budget_is_refused(
    bridge, parent, launched, monkeypatch
):
    from agent_parley import budgets

    monkeypatch.setattr(
        budgets,
        "standing",
        lambda home, directory, manifest, name: {
            "over": True,
            "crossed": ["turns"],
        },
    )
    actor = {"project": parent["root"], "name": "claude"}
    with pytest.raises(BridgeError, match=r"over its budget \(turns\)"):
        children.spawn(bridge.home, actor, {"task": "a", "paths": ["a"]})
    assert launched == []


@pytest.mark.parametrize(
    "paths",
    [[], ["/etc"], ["../up"], ["a/../../b"], ["port:5432"], [""], [3]],
)
def test_invalid_paths_are_refused(bridge, parent, launched, paths):
    actor = {"project": parent["root"], "name": "claude"}
    with pytest.raises(BridgeError, match="spawn_child"):
        children.spawn(bridge.home, actor, {"task": "a", "paths": paths})


def test_an_oversized_task_is_refused(bridge, parent, launched):
    actor = {"project": parent["root"], "name": "claude"}
    task = "x" * (children.TASK_BYTES + 1)
    with pytest.raises(BridgeError, match="byte budget"):
        children.spawn(bridge.home, actor, {"task": task, "paths": ["a"]})


def test_outside_keeps_only_keys_beyond_the_scope():
    scope = ["src/app", "docs/*.md"]
    keys = ["src/app", "src/app/x.py", "src/apple", "docs/a.md", "port:1"]
    assert children.outside(scope, keys) == ["src/apple", "port:1"]


def test_of_lists_children_with_their_state():
    manifest = {
        "participants": {
            "lead": {"branch": "b"},
            "lead-c2": {
                "parent": "lead",
                "branch": "b-c2",
                "lane": "/l2",
                "paths": ["x"],
                "retired": {"at": 1},
            },
            "lead-c1": {
                "parent": "lead",
                "branch": "b-c1",
                "lane": "/l1",
                "paths": ["y"],
                "result": {"outcome": "blocked"},
            },
            "lead-c3": {"parent": "lead", "branch": "b-c3", "paths": []},
            "other-c1": {"parent": "other", "branch": "o"},
        }
    }
    views = children.of(manifest, "lead")
    assert [(view["name"], view["state"]) for view in views] == [
        ("lead-c1", "blocked"),
        ("lead-c2", "retired"),
        ("lead-c3", "running"),
    ]
    assert children.live(manifest) == ["lead-c3", "other-c1"]


def test_message_is_capped_and_names_the_merge():
    body = children.message(
        "lead-c1",
        "b-c1",
        {
            "state": "blocked",
            "commit": "a" * 40,
            "summary": "s" * 5000,
            "evidence": "e" * 5000,
            "remaining": "r" * 5000,
        },
    )
    assert len(body.encode()) <= children.REPORT_BYTES
    assert "Branch: b-c1 at aaaaaaaaaaaa" in body
    assert body.endswith("git merge --no-ff b-c1")


def test_the_launcher_runs_the_package_without_bypass_flags(
    tmp_path, monkeypatch
):
    seen = {}

    class Child:
        pid = 1

    def popen(command, **options):
        seen["command"] = command
        seen["options"] = options
        return Child()

    monkeypatch.setattr(children.subprocess, "Popen", popen)
    monkeypatch.setattr(supervision, "track_launcher", lambda child: None)
    children.launch(tmp_path, tmp_path, "/repo", "lead-c1", "task text")
    command = seen["command"]
    assert command[1:7] == [
        "-m",
        "agent_parley.cli",
        "--home",
        str(tmp_path),
        "run",
        "lead-c1",
    ]
    assert command[-2:] == ["--task", "task text"]
    assert not [
        part
        for part in command
        if "dangerous" in part or "bypass" in part or "yolo" in part
    ]
    assert seen["options"]["start_new_session"] is True
    assert (tmp_path / "lead-c1-wake.log").exists()
