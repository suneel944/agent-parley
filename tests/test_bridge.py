import asyncio
import contextlib
import json
import os
import shlex
import socket
import subprocess
import sys
import tarfile
import time
import tomllib
import zipfile
from pathlib import Path

import httpx
import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from agent_parley import dashboard, forge, roster, store
from agent_parley.checkpoints import (
    MAX_EVENT_LOG_AGE,
    MAX_EVENT_LOG_BYTES,
    branch_guard,
    checkpoint,
    event_summary,
    mailbox,
    prune,
)
from agent_parley.cli import Bridge, BridgeError, git, lock, write_json
from agent_parley.issues import MAX_BLOCKERS, describe
from agent_parley.process import start_ticks


@contextlib.asynccontextmanager
async def client_session(url, auth):
    """Connects the independent official SDK to the real HTTP service."""
    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {auth}"}, trust_env=False
    ) as http:
        async with streamable_http_client(url, http_client=http) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                yield session


def test_isolation_identity_and_idempotent_setup(bridge, repo, paired):
    data = paired
    claude, codex = (Path(data["lanes"][name]) for name in ("claude", "codex"))
    (claude / "shared.txt").write_text("Claude change\n")
    assert (codex / "shared.txt").read_text() == "original\n"
    assert (repo / "shared.txt").read_text() == "original\n"
    assert bridge.setup(codex) == data
    assert bridge.project(claude) == bridge.project(repo)
    assert (
        len(git(repo, "worktree", "list", "--porcelain").split("worktree "))
        == 4
    )


def test_dirty_source_is_preserved_as_a_stash_entry(bridge, repo, capsys):
    (repo / "shared.txt").write_text("uncommitted work\n")
    (repo / "untracked.txt").write_text("untracked work\n")
    data = bridge.setup(repo)
    assert data["base"] == git(repo, "rev-parse", "--verify", "HEAD")
    assert not git(repo, "status", "--porcelain")
    assert (repo / "shared.txt").read_text() == "original\n"
    assert not (repo / "untracked.txt").exists()
    entry = git(repo, "rev-parse", "--short", "refs/stash")
    assert f"stash apply {entry}" in capsys.readouterr().err
    git(repo, "stash", "apply", entry)
    assert (repo / "shared.txt").read_text() == "uncommitted work\n"
    assert (repo / "untracked.txt").read_text() == "untracked work\n"


def test_merge_still_refuses_a_dirty_base_checkout(bridge, repo, paired):
    (repo / "shared.txt").write_text("uncommitted work\n")
    with pytest.raises(BridgeError, match="uncommitted changes"):
        bridge.merge(repo, "claude")
    assert (repo / "shared.txt").read_text() == "uncommitted work\n"


def test_missing_commit_and_existing_branch_preserved(bridge, repo, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    git(empty, "init")
    with pytest.raises(BridgeError):
        bridge.setup(empty)
    _, directory = bridge.project(repo)
    branch = f"parley/{directory.name}/codex"
    git(repo, "branch", branch)
    with pytest.raises(BridgeError, match="Existing lane"):
        bridge.add_participant(repo, "codex", "codex")
    assert not (directory / "codex").exists()
    assert git(repo, "rev-parse", branch) == git(repo, "rev-parse", "HEAD")


def test_lock_rejects_second_session(bridge):
    path = bridge.home / "session.lock"
    with lock(path), pytest.raises(BridgeError, match="owns"):
        with lock(path):
            pass


def test_public_state_directory_rejected(tmp_path):
    public = tmp_path / "public"
    public.mkdir(mode=0o755)
    with pytest.raises(BridgeError, match="private"):
        Bridge(public)


def test_occupied_port_fails_without_killing_owner(bridge):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", bridge.config["port"]))
        sock.listen()
        with pytest.raises(BridgeError, match="occupied"):
            bridge.up()
        assert sock.getsockname()[1] == bridge.config["port"]


def test_stale_pid_record_cannot_stop_an_unrelated_process(bridge):
    pid = os.getpid()
    write_json(
        bridge.home / "server.json",
        {
            "pid": pid,
            "start_ticks": start_ticks(pid),
        },
    )
    assert bridge.server_process() is None
    bridge.down()
    os.kill(pid, 0)


def test_changed_lane_branch_is_rejected_without_resetting(
    bridge, repo, paired
):
    lane = Path(paired["lanes"]["codex"])
    git(lane, "switch", "-c", "personal-work")
    with pytest.raises(BridgeError, match="expected"):
        bridge.setup(repo)
    assert git(lane, "branch", "--show-current") == "personal-work"


@pytest.mark.parametrize(
    "command",
    [
        "git switch -c personal-work",
        "rtk git checkout personal-work",
        "git branch -m personal-work",
        "git symbolic-ref HEAD refs/heads/personal-work",
        "gh pr checkout 42",
    ],
)
def test_native_hook_blocks_bridge_lane_branch_changes(
    bridge, repo, paired, command
):
    data = paired
    lane = Path(data["lanes"]["codex"])
    payload = {
        "hook_event_name": "PreToolUse",
        "cwd": str(lane),
        "tool_name": "Bash",
        "tool_input": {"command": command},
    }
    result = checkpoint(bridge.home, lane.parent, "codex", payload)
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert git(lane, "branch", "--show-current") == data["branches"]["codex"]


def test_native_hook_blocks_drift_until_exact_restore(bridge, repo, paired):
    data = paired
    lane = Path(data["lanes"]["codex"])
    expected = data["branches"]["codex"]
    git(lane, "switch", "-c", "personal-work")
    payload = {
        "hook_event_name": "PreToolUse",
        "cwd": str(lane),
        "tool_name": "Bash",
        "tool_input": {"command": "git status"},
    }
    denied, reason = branch_guard("PreToolUse", payload, lane, expected)
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert reason == "branch_drift"
    blocked, reason = branch_guard("Stop", payload, lane, expected)
    assert blocked["decision"] == "block"
    assert reason == "branch_drift"
    payload["tool_input"]["command"] = f"git switch {expected}"
    assert branch_guard("PreToolUse", payload, lane, expected) == (
        None,
        "branch_restore",
    )


def test_native_hook_allows_branch_work_in_separate_worktree(
    bridge, repo, paired
):
    data = paired
    lane = Path(data["lanes"]["codex"])
    payload = {
        "hook_event_name": "PreToolUse",
        "cwd": str(lane),
        "tool_name": "Bash",
        "tool_input": {"command": f"git -C {repo} switch main"},
    }
    assert branch_guard(
        "PreToolUse", payload, lane, data["branches"]["codex"]
    ) == (None, "branch_ok")


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_native_launch_preserves_task_and_passes_shared_configuration(
    bridge, repo, monkeypatch, tmp_path, agent
):
    """Exercises native argv, cwd and environment without a model call."""
    binary = tmp_path / "bin"
    binary.mkdir()
    executable = binary / agent
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "with open(os.environ['CAPTURE'], 'w') as f:\n"
        " json.dump({'argv': sys.argv[1:], 'cwd': os.getcwd(), "
        "'has_token': bool(os.environ.get('AGENT_PARLEY_TOKEN'))}, f)\n"
    )
    executable.chmod(0o755)
    capture = tmp_path / "capture.json"
    monkeypatch.setenv("CAPTURE", str(capture))
    monkeypatch.setenv("PATH", str(binary) + os.pathsep + os.environ["PATH"])
    monkeypatch.setattr(bridge, "up", lambda: None)

    async def fake_identity(*args):
        return {"registration_token": "test-scoped-credential"}

    monkeypatch.setattr(bridge, "identity", fake_identity)
    task = 'Fix "login"; $(do-not-execute)\nPreserve this newline.'
    assert bridge.launch(agent, repo, task) == 0
    result = json.loads(capture.read_text())
    assert result["cwd"] == bridge.setup(repo)["lanes"][agent]
    assert result["has_token"]
    assert task in result["argv"][-1]
    assert bridge.config["token"] not in json.dumps(result)
    assert str(repo) in " ".join(result["argv"])
    if agent == "claude":
        config = json.loads(Path(result["argv"][1]).read_text())
        assert (
            config["mcpServers"]["agent_parley"]["url"] == bridge.url + "/mcp/"
        )
        settings = json.loads(
            result["argv"][result["argv"].index("--settings") + 1]
        )
        assert "PreToolUse" in settings["hooks"]
        assert "Stop" in settings["hooks"]
    else:
        assert (
            'mcp_servers.agent_parley.bearer_token_env_var="AGENT_PARLEY_TOKEN"'
            in result["argv"]
        )
        overrides = [arg for arg in result["argv"] if arg.startswith("hooks.")]
        parsed = tomllib.loads("\n".join(overrides))
        assert "PreToolUse" in parsed["hooks"]
        assert "SessionEnd" in parsed["hooks"]
    assert "bypass" not in " ".join(result["argv"])


def test_mcp_two_clients_conflict_handoff_auth_and_restart(
    bridge, repo, paired
):
    data = paired
    bridge.up()
    pid = bridge.server_process().pid
    bridge.up()
    assert bridge.server_process().pid == pid
    response = httpx.post(
        bridge.url + "/mcp/",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/list",
            "params": {},
        },
        trust_env=False,
    )
    assert response.status_code == 401

    async def exercise():
        tokens = {}
        for name in ("claude", "codex"):
            identity = await bridge.identity(name, data)
            tokens[name] = identity["registration_token"]

        async def call(client, tool, arguments):
            arguments.pop("project_key", None)
            arguments.pop("agent_name", None)
            arguments.pop("sender_name", None)
            if tool == "send_message":
                arguments["idempotency_key"] = arguments["subject"]
            result = await client.call_tool(tool, arguments)
            assert not result.is_error, result.content
            return result

        async with client_session(
            bridge.url + "/mcp/", auth=tokens["claude"]
        ) as claude:
            async with client_session(
                bridge.url + "/mcp/", auth=tokens["codex"]
            ) as codex:
                root = data["root"]
                assert len((await claude.list_tools()).tools) == 7
                roster_view = await call(claude, "list_participants", {})
                assert sorted(
                    entry["name"]
                    for entry in json.loads(roster_view.content[0].text)[
                        "participants"
                    ]
                ) == ["claude", "codex"]
                granted = await call(
                    claude,
                    "file_reservation_paths",
                    {
                        "project_key": root,
                        "agent_name": "GreenCastle",
                        "paths": ["shared.txt"],
                        "ttl_seconds": 300,
                        "exclusive": True,
                    },
                )
                assert not json.loads(granted.content[0].text)["conflicts"]
                conflict = await call(
                    codex,
                    "file_reservation_paths",
                    {
                        "project_key": root,
                        "agent_name": "BlueLake",
                        "paths": ["shared.txt"],
                        "ttl_seconds": 300,
                        "exclusive": True,
                    },
                )
                assert json.loads(conflict.content[0].text)["conflicts"]
                await call(
                    codex,
                    "release_file_reservations",
                    {
                        "project_key": root,
                        "agent_name": "BlueLake",
                    },
                )
                await call(
                    claude,
                    "send_message",
                    {
                        "project_key": root,
                        "sender_name": "GreenCastle",
                        "to": ["codex"],
                        "subject": "Login contract",
                        "body_md": "Response now includes session_id.",
                        "thread_id": "login-task",
                        "ack_required": True,
                    },
                )
                inbox = await call(
                    codex,
                    "fetch_inbox",
                    {
                        "project_key": root,
                        "agent_name": "BlueLake",
                        "include_bodies": True,
                    },
                )
                messages = json.loads(inbox.content[0].text)["messages"]
                message = next(
                    item
                    for item in messages
                    if item["subject"] == "Login contract"
                )
                assert "session_id" in message["body_md"]
                directory = Path(data["lanes"]["codex"]).parent
                payload = {
                    "hook_event_name": "PreToolUse",
                    "session_id": "codex-test",
                    "cwd": data["lanes"]["codex"],
                    "tool_name": "apply_patch",
                }
                hook = bridge.hooks("codex", directory)["PreToolUse"][0][
                    "hooks"
                ][0]
                result = subprocess.run(
                    shlex.split(hook["command"]),
                    input=json.dumps(payload),
                    capture_output=True,
                    text=True,
                    timeout=3,
                    check=False,
                )
                assert result.returncode == 0, result.stderr
                delivered = json.loads(result.stdout)["hookSpecificOutput"]
                assert delivered["permissionDecision"] == "deny"
                assert "session_id" in delivered["additionalContext"]
                assert (
                    checkpoint(bridge.home, directory, "codex", payload) == {}
                )
                assert mailbox(bridge.home, root, "codex")["pending_ack"] == 1
                await call(
                    codex,
                    "acknowledge_message",
                    {
                        "project_key": root,
                        "agent_name": "BlueLake",
                        "message_id": message["id"],
                    },
                )
                assert mailbox(bridge.home, root, "codex")["pending_ack"] == 0
                await call(
                    claude,
                    "release_file_reservations",
                    {
                        "project_key": root,
                        "agent_name": "GreenCastle",
                    },
                )
                acquired = await call(
                    codex,
                    "file_reservation_paths",
                    {
                        "project_key": root,
                        "agent_name": "BlueLake",
                        "paths": ["shared.txt"],
                        "ttl_seconds": 300,
                        "exclusive": True,
                    },
                )
                assert not json.loads(acquired.content[0].text)["conflicts"]
                await call(
                    codex,
                    "send_message",
                    {
                        "project_key": root,
                        "sender_name": "BlueLake",
                        "to": ["claude"],
                        "subject": "Handoff",
                        "body_md": (
                            f"Reviewed commit {data['base']}; "
                            "shared.txt unchanged."
                        ),
                        "thread_id": "login-task",
                    },
                )
                handoff = await call(
                    claude,
                    "fetch_inbox",
                    {
                        "project_key": root,
                        "agent_name": "GreenCastle",
                        "include_bodies": True,
                    },
                )
                assert data["base"] in handoff.content[0].text
                stop = {
                    "hook_event_name": "Stop",
                    "session_id": "claude-test",
                    "cwd": data["lanes"]["claude"],
                }
                assert (
                    checkpoint(bridge.home, directory, "claude", stop)[
                        "decision"
                    ]
                    == "block"
                )
                assert (
                    checkpoint(
                        bridge.home,
                        directory,
                        "claude",
                        {
                            **stop,
                            "stop_hook_active": True,
                        },
                    )
                    == {}
                )
                state = json.loads(
                    (directory / "claude-activity.json").read_text()
                )
                assert state["activity"] == "idle"
                assert "outcome" not in state

    asyncio.run(exercise())
    bridge.down()
    assert bridge.server_process() is None
    bridge.up()

    async def persisted():
        identity = await bridge.identity("codex", data)
        async with client_session(
            bridge.url + "/mcp/", auth=identity["registration_token"]
        ) as client:
            result = await client.call_tool(
                "fetch_inbox",
                {
                    "include_bodies": True,
                },
            )
            assert "session_id" in result.content[0].text

    asyncio.run(persisted())
    assert (Path(data["lanes"]["claude"]) / "shared.txt").exists()


def test_reports_require_remaining_work_or_verification(bridge, repo, paired):
    lane = Path(paired["lanes"]["claude"])
    with pytest.raises(BridgeError, match="remaining"):
        bridge.report(lane, "partial", "Engine built", "", "")
    with pytest.raises(BridgeError, match="evidence"):
        bridge.report(lane, "ready", "Engine built", "", "")
    with pytest.raises(BridgeError, match="worktree"):
        bridge.report(repo, "ready", "Engine built", "", "pytest: 12 passed")
    bridge.report(
        lane,
        "partial",
        "Engine built",
        "CLI integration missing",
        "12 tests passed",
    )
    state = json.loads((lane.parent / "claude-activity.json").read_text())
    assert state["outcome"] == "partial"
    assert state["remaining"] == "CLI integration missing"


def test_liveness_follows_the_session_process_not_the_session_lock(
    bridge, repo, paired, capsys
):
    directory = Path(paired["lanes"]["claude"]).parent
    running = {
        "session_pid": os.getpid(),
        "session_ticks": start_ticks(os.getpid()),
    }
    write_json(directory / "claude-activity.json", running)
    with lock(directory / "claude.session.lock"):
        bridge.status()
    output = capsys.readouterr().out
    assert "running; checkpoints unavailable (relaunch)" in output
    assert "Reported outcome: unknown" in output
    write_json(
        directory / "claude-activity.json",
        {**running, "session_ticks": "0", "activity": "working"},
    )
    with lock(directory / "claude.session.lock"):
        bridge.status()
    assert "claude (claude): stopped" in capsys.readouterr().out


def test_hook_failure_pauses_tools_and_foreign_worktree_is_rejected(
    bridge, repo, paired
):
    lane = Path(paired["lanes"]["claude"])
    write_json(lane.parent / "claude-identity.json", {"name": "GreenCastle"})
    payload = {
        "hook_event_name": "PreToolUse",
        "cwd": str(lane),
        "session_id": "test",
    }
    result = checkpoint(bridge.home, lane.parent, "claude", payload)
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    with pytest.raises(BridgeError, match="cwd"):
        checkpoint(
            bridge.home, lane.parent, "claude", {**payload, "cwd": str(repo)}
        )
    assert (
        checkpoint(
            bridge.home, lane.parent, "claude", {**payload, "agent_id": "child"}
        )
        == {}
    )


def test_top_reports_every_participant_and_writes_no_state(
    bridge, repo, paired, capsys
):
    directory = Path(paired["lanes"]["claude"]).parent
    write_json(
        directory / "claude-activity.json",
        {
            "activity": "working",
            "session_pid": os.getpid(),
            "session_ticks": start_ticks(os.getpid()),
            "last_prompt": "Wire the dashboard",
        },
    )
    bridge.issue(Path(paired["lanes"]["codex"]), "claim", "77")
    before = {
        path.name: path.stat().st_mtime_ns for path in directory.iterdir()
    }
    dashboard.run(bridge.home, lambda: False, once=True)
    output = capsys.readouterr().out
    assert "agent-parley top  server: not running" in output
    assert "denials 0 (0%)" in output
    assert "#77" in output
    assert "Wire the dashboard" in output
    assert "working" in output and "stopped" in output
    assert {
        path.name: path.stat().st_mtime_ns for path in directory.iterdir()
    } == before


def test_top_reports_only_the_selected_providers(bridge, repo, paired, capsys):
    dashboard.run(bridge.home, lambda: False, once=True, providers=("codex",))
    selected = capsys.readouterr().out
    assert "codex/default" in selected
    assert "claude/default" not in selected
    assert "participants 1" in selected
    assert "provider codex" in selected

    dashboard.run(
        bridge.home, lambda: False, once=True, providers=("claude", "codex")
    )
    both = capsys.readouterr().out
    assert "claude/default" in both and "codex/default" in both
    assert "participants 2" in both

    dashboard.run(bridge.home, lambda: False, once=True, providers=("kimi",))
    none = capsys.readouterr().out
    assert "no participants for the selected provider" in none
    assert "participants 0" in none

    dashboard.run(bridge.home, lambda: False, once=True)
    unfiltered = capsys.readouterr().out
    assert "participants 2" in unfiltered
    assert "provider " not in unfiltered


def test_checkpoint_records_every_decision_in_a_rotating_event_log(
    bridge, repo, paired, monkeypatch
):
    monkeypatch.setattr(
        "agent_parley.checkpoints.mailbox",
        lambda *args: {"pending_ack": 0, "messages": []},
    )
    lane = Path(paired["lanes"]["claude"])
    directory = lane.parent
    write_json(directory / "claude-identity.json", {"name": "claude"})
    log = directory / "claude-events.jsonl"
    payload = {
        "hook_event_name": "PreToolUse",
        "cwd": str(lane),
        "session_id": "test",
        "tool_name": "Bash",
        "tool_input": {"command": "pytest -q"},
    }
    checkpoint(bridge.home, directory, "claude", payload)
    checkpoint(
        bridge.home, directory, "claude", {**payload, "agent_id": "child"}
    )
    git(lane, "switch", "-c", "personal-work")
    denied = checkpoint(bridge.home, directory, "claude", payload)
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    entries = [json.loads(line) for line in log.read_text().splitlines()]
    assert [entry["reason_class"] for entry in entries] == [
        "coordination_pending",
        "ignored_event",
        "branch_drift",
    ]
    assert [entry["decision"] for entry in entries] == [
        "allow",
        "allow",
        "deny",
    ]
    assert entries[0]["activity"] == "testing (command observed)"
    assert entries[0]["tool_name"] == "Bash"
    assert entries[0]["injected_bytes"] > 0
    assert entries[1]["injected_bytes"] == 0
    log.write_text("x" * MAX_EVENT_LOG_BYTES)
    checkpoint(bridge.home, directory, "claude", payload)
    assert (directory / "claude-events.1.jsonl").exists()
    assert 0 < len(log.read_bytes()) < MAX_EVENT_LOG_BYTES
    (directory / "claude-events.1.jsonl").write_text(
        json.dumps(
            {
                "ts": 1.0,
                "decision": "deny",
                "injected_bytes": 5,
                "reason_class": "branch_drift",
            }
        )
        + "\n"
    )
    log.write_text(
        json.dumps(
            {
                "ts": 2.0,
                "decision": "allow",
                "injected_bytes": 7,
                "reason_class": "coordination_pending",
            }
        )
        + "\n"
    )
    summary = event_summary(directory, "claude")
    assert summary["events"] == 2
    assert summary["denials"] == 1
    assert summary["injected_bytes"] == 12
    assert summary["last_ts"] == 2.0
    assert summary["last_reason"] == "coordination_pending"


def test_issue_claim_race_persistence_and_explicit_handoff(
    bridge, repo, paired, tmp_path
):
    lanes = {name: Path(path) for name, path in paired["lanes"].items()}
    commands = [
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "agent_parley.cli",
                "--home",
                str(bridge.home),
                "issue",
                "claim",
                "432",
            ],
            cwd=lane,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for lane in lanes.values()
    ]
    results = [command.communicate(timeout=10) for command in commands]
    assert sorted(command.returncode for command in commands) == [0, 1], results
    restarted = Bridge(bridge.home)
    record = restarted.issue(repo, "list")["issues"]["432"]
    owner = record["owner"]
    peer = "codex" if owner == "claude" else "claude"
    with pytest.raises(BridgeError, match="owned"):
        bridge.issue(lanes[peer], "claim", "#432")
    with pytest.raises(BridgeError, match="Only"):
        bridge.issue(lanes[peer], "release", "432")
    offered = bridge.issue(
        lanes[owner], "offer", "432", to=peer, summary="Review commit abc"
    )
    offer_id = offered["offer"]["id"]
    assert offered["owner"] == owner
    with pytest.raises(BridgeError, match="recipient"):
        bridge.issue(lanes[owner], "accept", "432", offer_id=offer_id)
    bridge.issue(lanes[owner], "cancel", "432")
    replacement = bridge.issue(
        lanes[owner], "offer", "432", to=peer, summary="Updated handoff"
    )
    with pytest.raises(BridgeError, match="changed"):
        bridge.issue(lanes[peer], "accept", "432", offer_id=offer_id)
    accepted = restarted.issue(
        lanes[peer], "accept", "432", offer_id=replacement["offer"]["id"]
    )
    assert accepted["owner"] == peer
    assert accepted["offer"] is None
    with pytest.raises(BridgeError, match="Only"):
        bridge.issue(lanes[owner], "release", "432")
    bridge.issue(lanes[peer], "release", "432")
    assert bridge.issue(lanes[owner], "claim", "432")["owner"] == owner
    assert len(bridge.issue(repo, "list")["issues"]["432"]["history"]) == 7


def test_issue_decline_no_timeout_and_worktree_authority(
    bridge, repo, paired, monkeypatch
):
    claude, codex = (
        Path(paired["lanes"][name]) for name in ("claude", "codex")
    )
    with pytest.raises(BridgeError, match="worktree"):
        bridge.issue(repo, "claim", "432")
    for number in (
        "0",
        "-1",
        "../432",
        "0432",
        "https://github.com/a/b/issues/432",
    ):
        with pytest.raises(BridgeError, match="positive"):
            bridge.issue(claude, "claim", number)
    bridge.issue(claude, "claim", "432")
    offer = bridge.issue(
        claude, "offer", "432", to="codex", summary="Waiting for review"
    )["offer"]
    monkeypatch.setattr(
        "agent_parley.issues.time.time", lambda: offer["created"] + 86400
    )
    assert bridge.issue(repo, "list")["issues"]["432"]["owner"] == "claude"
    declined = bridge.issue(codex, "decline", "432", offer_id=offer["id"])
    assert declined["owner"] == "claude"
    assert declined["offer"] is None


def test_issue_crash_releases_operation_lock_but_preserves_owner(
    bridge, repo, paired
):
    claude, codex = (
        Path(paired["lanes"][name]) for name in ("claude", "codex")
    )
    bridge.issue(claude, "claim", "432")
    crashed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import os, sys\nfrom pathlib import Path\n"
            "from agent_parley.state import lock\n"
            "with lock(Path(sys.argv[1])): os._exit(7)",
            str(claude.parent / "issues.lock"),
        ],
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert crashed.returncode == 7
    with pytest.raises(BridgeError, match="owned by claude"):
        Bridge(bridge.home).issue(codex, "claim", "432")
    bridge.issue(claude, "release", "432")
    assert bridge.issue(codex, "claim", "432")["owner"] == "codex"


def test_issue_dependencies_are_owner_only_and_survive_a_release(
    bridge, repo, paired
):
    claude, codex = (
        Path(paired["lanes"][name]) for name in ("claude", "codex")
    )
    bridge.issue(claude, "claim", "432")
    bridge.issue(codex, "claim", "77")
    with pytest.raises(BridgeError, match="Only claude"):
        bridge.issue(codex, "block", "432", on="77")
    with pytest.raises(BridgeError, match="wait on itself"):
        bridge.issue(claude, "block", "432", on="432")
    with pytest.raises(BridgeError, match="Blocker must be"):
        bridge.issue(claude, "block", "432", on="0")
    blocked = bridge.issue(claude, "block", "432", on="#77")
    assert blocked["blocked_by"] == ["77"]
    revision = bridge.issue(repo, "list")["revision"]
    assert bridge.issue(claude, "block", "432", on="77")["blocked_by"] == ["77"]
    assert bridge.issue(repo, "list")["revision"] == revision
    for extra in range(100, 99 + MAX_BLOCKERS):
        bridge.issue(claude, "block", "432", on=str(extra))
    with pytest.raises(BridgeError, match="drop one with issue unblock"):
        bridge.issue(claude, "block", "432", on="999")
    bridge.issue(claude, "release", "432")
    assert bridge.issue(claude, "claim", "432")["blocked_by"] == [
        "77",
        *(str(extra) for extra in range(100, 99 + MAX_BLOCKERS)),
    ]
    assert "#432: claude; waits on #77 (codex)" in describe(
        bridge.issue(repo, "list")
    )
    with pytest.raises(BridgeError, match="does not wait on #555"):
        bridge.issue(claude, "unblock", "432", on="555")
    assert (
        "77"
        not in bridge.issue(claude, "unblock", "432", on="77")["blocked_by"]
    )


def test_forge_slug_reads_github_remotes_and_ignores_everything_else(repo):
    assert forge.slug(repo) is None
    git(repo, "remote", "add", "origin", "https://github.com/owner/name.git")
    assert forge.slug(repo) == "owner/name"
    git(repo, "remote", "set-url", "origin", "git@github.com:owner/name.git")
    assert forge.slug(repo) == "owner/name"
    git(repo, "remote", "set-url", "origin", "https://github.com/owner/name")
    assert forge.slug(repo) == "owner/name"
    git(repo, "remote", "set-url", "origin", "git@example.com:owner/name.git")
    assert forge.slug(repo) is None


def test_forge_title_is_skipped_without_the_gh_client(repo, monkeypatch):
    git(repo, "remote", "add", "origin", "https://github.com/owner/name.git")
    assert forge.slug(repo) == "owner/name"
    monkeypatch.setattr(forge.shutil, "which", lambda command: None)
    executed = []
    real_run = subprocess.run

    def record(command, **kwargs):
        executed.append(command[0])
        return real_run(command, **kwargs)

    monkeypatch.setattr(forge.subprocess, "run", record)
    assert forge.issue_title(repo, "42") is None
    assert executed == ["git"]


def test_forge_title_absorbs_failed_and_unusable_gh_output(repo, monkeypatch):
    monkeypatch.setattr(forge, "slug", lambda directory: "owner/name")
    monkeypatch.setattr(forge.shutil, "which", lambda command: "/usr/bin/gh")
    replies = [
        subprocess.CompletedProcess([], 1, "", "gh: could not authenticate"),
        subprocess.CompletedProcess([], 0, "not json at all", ""),
        subprocess.CompletedProcess([], 0, '{"body": "no title key"}', ""),
        subprocess.CompletedProcess([], 0, '{"title": null}', ""),
    ]
    monkeypatch.setattr(
        forge.subprocess, "run", lambda *args, **kwargs: replies.pop(0)
    )
    resolved = [forge.issue_title(repo, "42") for _ in range(len(replies))]
    assert resolved == [None, None, None, None]


def test_forge_title_is_clipped_to_the_display_limit(repo, monkeypatch):
    monkeypatch.setattr(forge, "slug", lambda directory: "owner/name")
    monkeypatch.setattr(forge.shutil, "which", lambda command: "/usr/bin/gh")
    payload = json.dumps({"title": "t" * 500})
    reply = subprocess.CompletedProcess([], 0, payload, "")
    monkeypatch.setattr(forge.subprocess, "run", lambda *args, **kwargs: reply)
    assert forge.issue_title(repo, "42") == "t" * 200


def test_forge_mirrors_use_the_operator_account_and_absorb_refusal(
    repo, monkeypatch
):
    monkeypatch.setattr(forge, "slug", lambda directory: "owner/name")
    monkeypatch.setattr(forge.shutil, "which", lambda command: "/usr/bin/gh")
    issued = []
    accepted = subprocess.CompletedProcess([], 0, "", "")

    def record(command, **kwargs):
        issued.append(command)
        return accepted

    monkeypatch.setattr(forge.subprocess, "run", record)
    assert forge.assign(repo, "42") is True
    assert forge.unassign(repo, "42") is True
    assert forge.comment(repo, "42", "Lane account") is True
    assert issued[0][:6] == [
        "gh",
        "issue",
        "edit",
        "42",
        "--repo",
        "owner/name",
    ]
    assert issued[0][6:] == ["--add-assignee", "@me"]
    assert issued[1][6:] == ["--remove-assignee", "@me"]
    assert issued[2][1:4] == ["issue", "comment", "42"]
    assert issued[2][6:] == ["--body", "Lane account"]
    assert not any("--force" in command for command in issued)

    refused = subprocess.CompletedProcess([], 1, "", "gh: no write access")
    monkeypatch.setattr(forge.subprocess, "run", lambda *a, **k: refused)
    assert forge.assign(repo, "42") is False
    assert forge.unassign(repo, "42") is False
    assert forge.comment(repo, "42", "Lane account") is False
    monkeypatch.setattr(forge.shutil, "which", lambda command: None)
    assert forge.assign(repo, "42") is False


def test_claim_and_release_mirror_onto_the_forge_after_the_ledger(
    bridge, repo, paired, monkeypatch
):
    claude = Path(paired["lanes"]["claude"])
    mirrored = []
    monkeypatch.setattr(
        "agent_parley.cli.forge.issue_title", lambda directory, number: None
    )
    monkeypatch.setattr(
        "agent_parley.cli.forge.assign",
        lambda directory, number: mirrored.append(("assign", number)),
    )
    monkeypatch.setattr(
        "agent_parley.cli.forge.unassign",
        lambda directory, number: mirrored.append(("unassign", number)),
    )
    bridge.issue(claude, "claim", "#432")
    bridge.issue(claude, "block", "432", on="77")
    bridge.issue(claude, "release", "432")
    assert mirrored == [("assign", "432"), ("unassign", "432")]

    def refuse(directory, number):
        raise AssertionError("a mirror must never decide the ledger")

    monkeypatch.setattr("agent_parley.cli.forge.assign", refuse)
    with pytest.raises(AssertionError):
        bridge.issue(claude, "claim", "432")
    assert bridge.issue(repo, "list")["issues"]["432"]["owner"] == "claude"


def test_a_ready_report_comments_once_on_every_claimed_issue(
    bridge, repo, paired, monkeypatch
):
    claude = Path(paired["lanes"]["claude"])
    posted = []
    monkeypatch.setattr(
        "agent_parley.cli.forge.issue_title", lambda directory, number: None
    )
    monkeypatch.setattr(
        "agent_parley.cli.forge.assign", lambda directory, number: True
    )
    monkeypatch.setattr(
        "agent_parley.cli.forge.comment",
        lambda directory, number, body: posted.append((number, body)),
    )
    bridge.issue(claude, "claim", "432")
    bridge.issue(claude, "claim", "433")

    bridge.report(claude, "partial", "Halfway", "More to do", "")
    assert posted == []

    bridge.report(claude, "ready", "Lane result", "", "make check passed")
    assert [number for number, _ in posted] == ["432", "433"]
    assert "Lane `claude` reports ready for review." in posted[0][1]
    assert "make check passed" in posted[0][1]
    assert "neither review nor independent verification" in posted[0][1]

    bridge.report(claude, "ready", "Lane result", "", "make check passed")
    assert len(posted) == 2


def test_issue_claim_records_and_renders_the_forge_title(
    bridge, repo, paired, monkeypatch
):
    claude = Path(paired["lanes"]["claude"])
    monkeypatch.setattr(
        "agent_parley.cli.forge.issue_title",
        lambda directory, number: f"Title for issue {number}",
    )
    assert bridge.issue(claude, "claim", "#432")["title"] == (
        "Title for issue 432"
    )
    assert (
        describe(bridge.issue(repo, "list"))
        == "#432: claude — Title for issue 432"
    )


def test_issue_claim_survives_an_unavailable_forge(
    bridge, repo, paired, monkeypatch
):
    claude = Path(paired["lanes"]["claude"])
    monkeypatch.setattr(
        "agent_parley.cli.forge.issue_title", lambda directory, number: None
    )
    assert "title" not in bridge.issue(claude, "claim", "432")
    monkeypatch.setattr(
        "agent_parley.cli.forge.issue_title", lambda directory, number: ""
    )
    assert "title" not in bridge.issue(claude, "claim", "433")
    assert describe(bridge.issue(repo, "list")) == (
        "#432: claude\n#433: claude"
    )


def test_recorded_issue_title_survives_later_transitions(
    bridge, repo, paired, monkeypatch
):
    claude, codex = (
        Path(paired["lanes"][name]) for name in ("claude", "codex")
    )
    monkeypatch.setattr(
        "agent_parley.cli.forge.issue_title",
        lambda directory, number: "Resolved from the forge",
    )
    bridge.issue(claude, "claim", "432")
    blocked = bridge.issue(claude, "block", "432", on="77")
    assert blocked["title"] == "Resolved from the forge"
    bridge.issue(claude, "release", "432")
    monkeypatch.setattr(
        "agent_parley.cli.forge.issue_title", lambda directory, number: None
    )
    reclaimed = bridge.issue(codex, "claim", "432")
    assert reclaimed["title"] == "Resolved from the forge"
    assert "#432: codex — Resolved from the forge; waits on #77" in describe(
        bridge.issue(repo, "list")
    )


def test_issue_notifications_are_once_per_change_without_empty_reminders(
    bridge, repo, paired, monkeypatch
):
    claude, codex = (
        Path(paired["lanes"][name]) for name in ("claude", "codex")
    )
    directory = claude.parent
    write_json(directory / "codex-identity.json", {"name": "codex"})
    monkeypatch.setattr(
        "agent_parley.checkpoints.mailbox",
        lambda *args: {"pending_ack": 0, "messages": []},
    )
    bridge.issue(claude, "claim", "432")
    bridge.issue(
        claude,
        "offer",
        "432",
        to="codex",
        summary="Read tests before accepting",
    )
    payload = {
        "hook_event_name": "PreToolUse",
        "session_id": "test",
        "cwd": str(codex),
    }
    notice = checkpoint(bridge.home, directory, "codex", payload)[
        "hookSpecificOutput"
    ]
    assert notice["permissionDecision"] == "deny"
    assert "handoff to codex" in notice["additionalContext"]
    assert checkpoint(bridge.home, directory, "codex", payload) == {}
    reminder = checkpoint(
        bridge.home,
        directory,
        "codex",
        {**payload, "hook_event_name": "UserPromptSubmit"},
    )
    assert reminder == {}
    bridge.issue(claude, "cancel", "432")
    stop = {**payload, "hook_event_name": "Stop", "stop_hook_active": True}
    assert checkpoint(bridge.home, directory, "codex", stop) == {}
    assert (
        checkpoint(bridge.home, directory, "codex", payload)[
            "hookSpecificOutput"
        ]["permissionDecision"]
        == "deny"
    )


def test_built_wheel_installs_and_coordinates_outside_checkout(tmp_path, repo):
    project = Path(__file__).resolve().parents[1]
    metadata = tomllib.loads((project / "pyproject.toml").read_text())
    version = metadata["project"]["version"]
    stem = metadata["project"]["name"].replace("-", "_")
    wheel = project / "dist" / f"{stem}-{version}-py3-none-any.whl"
    assert wheel.exists(), "Run make build before the installed-package test."
    with zipfile.ZipFile(wheel) as archive:
        assert "agent_parley/__main__.py" in archive.namelist()
        package_metadata = archive.read(
            f"{stem}-{version}.dist-info/METADATA"
        ).decode()
        assert "Requires-Dist:" not in package_metadata
        assert not any(name.startswith("src/") for name in archive.namelist())
    with tarfile.open(project / "dist" / f"{stem}-{version}.tar.gz") as archive:
        root = f"{stem}-{version}"
        for client in ("codex", "claude"):
            manifest_path = (
                f"{root}/plugins/agent-parley/.{client}-plugin/plugin.json"
            )
            with archive.extractfile(manifest_path) as stream:
                manifest = json.load(stream)
            assert manifest["name"] == "agent-parley"
            assert manifest["version"] == version
        assert archive.getmember(
            f"{root}/plugins/agent-parley/skills/coordinate/SKILL.md"
        ).isfile()
        assert archive.getmember(
            f"{root}/.agents/plugins/marketplace.json"
        ).isfile()
        assert archive.getmember(
            f"{root}/.claude-plugin/marketplace.json"
        ).isfile()
    requirements = tmp_path / "requirements.txt"
    subprocess.run(
        [
            "uv",
            "export",
            "--locked",
            "--no-dev",
            "--no-emit-project",
            "--no-hashes",
            "--output-file",
            str(requirements),
        ],
        cwd=project,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    environment = {
        **os.environ,
        "UV_TOOL_DIR": str(tmp_path / "tools"),
        "UV_TOOL_BIN_DIR": str(tmp_path / "bin"),
        "AGENT_PARLEY_HOME": str(tmp_path / "installed-state"),
    }
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        environment["AGENT_PARLEY_PORT"] = str(sock.getsockname()[1])
    environment.pop("PYTHONPATH", None)
    subprocess.run(
        [
            "uv",
            "tool",
            "install",
            "--python",
            sys.executable,
            str(wheel),
            "--with-requirements",
            str(requirements),
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    executable = tmp_path / "bin" / "agent-parley"
    try:
        subprocess.run(
            [str(executable), "up"],
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        health = subprocess.run(
            [str(executable), "status"],
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        assert "Server: ready" in health.stdout
    finally:
        subprocess.run(
            [str(executable), "down"],
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )
    for participant in ("claude", "codex"):
        subprocess.run(
            [
                str(executable),
                "participant",
                "add",
                participant,
                "--repo",
                str(repo),
            ],
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
    result = subprocess.run(
        [str(executable), "setup", str(repo)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    lanes = json.loads(result.stdout)["lanes"]
    for agent, expected in (("claude", 0), ("codex", 1)):
        result = subprocess.run(
            [str(executable), "issue", "claim", "432"],
            cwd=lanes[agent],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert result.returncode == expected, result.stderr
    installed_python = (
        tmp_path / "tools" / metadata["project"]["name"] / "bin" / "python"
    )
    location = subprocess.run(
        [
            str(installed_python),
            "-I",
            "-c",
            "import agent_parley; print(agent_parley.__file__)",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    assert Path(location.stdout.strip()).is_relative_to(tmp_path / "tools")
    subprocess.run(
        [str(installed_python), "-I", "-m", "agent_parley", "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )


def test_many_accounts_of_one_provider_run_side_by_side(
    bridge, repo, monkeypatch, tmp_path
):
    """Runs one provider under several accounts, each with its own lane."""
    binary = tmp_path / "bin"
    binary.mkdir()
    executable = binary / "claude"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os\n"
        "with open(os.environ['CAPTURE'], 'w') as f:\n"
        " json.dump({'home': os.environ.get('CLAUDE_CONFIG_DIR', ''),\n"
        "  'cwd': os.getcwd(),\n"
        "  'token': os.environ.get('AGENT_PARLEY_TOKEN', '')}, f)\n"
    )
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(binary) + os.pathsep + os.environ["PATH"])
    monkeypatch.setattr(bridge, "up", lambda: None)
    store.initialize(bridge.home)
    accounts = {f"claude-{index}": f"account-{index}" for index in (1, 2, 3)}
    for profile in accounts.values():
        roster.define_credential(
            bridge.home, profile, str(tmp_path / profile), [], []
        )
    captured = {}
    for name, profile in accounts.items():
        capture = tmp_path / f"{name}.json"
        monkeypatch.setenv("CAPTURE", str(capture))
        assert (
            bridge.launch(name, repo, "Work on issue 42", "claude", profile)
            == 0
        )
        captured[name] = json.loads(capture.read_text())
    data = bridge.setup(repo)
    for name, profile in accounts.items():
        assert captured[name]["cwd"] == data["lanes"][name]
        assert captured[name]["home"] == str(tmp_path / profile)
        assert data["participants"][name]["provider"] == "claude"
    for field in ("cwd", "home", "token"):
        values = [captured[name][field] for name in accounts]
        assert len(set(values)) == len(accounts)
    assert len({data["branches"][name] for name in accounts}) == len(accounts)
    directory = Path(data["lanes"]["claude-1"]).parent
    with lock(directory / "claude-1.session.lock"):
        monkeypatch.setenv("CAPTURE", str(tmp_path / "second-run.json"))
        assert bridge.launch("claude-2", repo, "Continue") == 0


def test_provider_definitions_never_store_credential_values(
    bridge, monkeypatch
):
    """Keeps secrets in the caller's environment instead of bridge state."""
    with pytest.raises(BridgeError, match="does not store"):
        roster.define_provider(
            bridge.home,
            "vendor",
            "claude",
            "claude",
            "CLAUDE_CONFIG_DIR",
            ["ANTHROPIC_AUTH_TOKEN=super-secret"],
            [],
        )
    roster.define_provider(
        bridge.home,
        "vendor",
        "claude",
        "claude",
        "CLAUDE_CONFIG_DIR",
        ["ANTHROPIC_BASE_URL=https://vendor.example/anthropic"],
        ["ANTHROPIC_AUTH_TOKEN"],
    )
    assert "super-secret" not in (bridge.home / "providers.json").read_text()
    entry = roster.provider(bridge.home, "vendor")
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    with pytest.raises(BridgeError, match="Export these"):
        roster.launch_environment(bridge.home, entry, None)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "exported-by-the-user")
    assert roster.launch_environment(bridge.home, entry, None) == {
        "ANTHROPIC_BASE_URL": "https://vendor.example/anthropic"
    }
    with pytest.raises(BridgeError, match="adapter must be"):
        roster.define_provider(
            bridge.home, "vendor", "vendor-cli", "vendor", "", [], []
        )


@pytest.mark.parametrize(
    "name",
    [
        "../escape",
        "",
        "Claude",
        "project",
        "a" * 40,
        "issues.json",
        "codex-activity.json",
    ],
)
def test_invalid_participant_names_are_rejected(bridge, repo, name):
    with pytest.raises(BridgeError, match="must match"):
        bridge.add_participant(repo, name, "claude")


def test_one_drifted_lane_does_not_block_other_participants(
    bridge, repo, paired
):
    drifted = Path(paired["lanes"]["codex"])
    git(drifted, "switch", "-c", "personal-work")
    added = bridge.add_participant(repo, "kimi-1", "kimi")
    assert "kimi-1" in added["participants"]
    assert bridge.issue(Path(paired["lanes"]["claude"]), "claim", "51")
    with pytest.raises(BridgeError, match="codex lane is on"):
        bridge.add_participant(repo, "codex", "codex")
    assert git(drifted, "branch", "--show-current") == "personal-work"


def test_restore_returns_a_drifted_lane_without_discarding(
    bridge, repo, paired
):
    lane = Path(paired["lanes"]["codex"])
    branch = paired["branches"]["codex"]
    git(lane, "switch", "-c", "personal-work")
    (lane / "draft.txt").write_text("unsaved\n")
    with pytest.raises(BridgeError, match="uncommitted changes"):
        bridge.restore(repo, "codex")
    assert (lane / "draft.txt").read_text() == "unsaved\n"
    git(lane, "add", "draft.txt")
    git(
        lane,
        "-c",
        "user.name=Bridge Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-m",
        "Work in progress",
    )
    with pytest.raises(BridgeError, match="commits that"):
        bridge.restore(repo, "codex")
    assert git(lane, "log", "--oneline", "-1")
    git(lane, "branch", "-f", branch, "personal-work")
    assert "restored" in bridge.restore(repo, "codex")
    assert (lane / "draft.txt").read_text() == "unsaved\n"
    assert git(lane, "branch", "--show-current") == branch
    assert bridge.restore(repo, "codex") == f"codex is already on {branch}."


def test_retire_removes_a_lane_and_revokes_its_credential(bridge, repo, paired):
    store.initialize(bridge.home)
    token = asyncio.run(bridge.identity("codex", paired))["registration_token"]
    assert store.authenticate(bridge.home, token)
    lane = Path(paired["lanes"]["codex"])
    (lane / "draft.txt").write_text("unsaved\n")
    with pytest.raises(BridgeError, match="uncommitted changes"):
        bridge.retire(repo, "codex")
    assert lane.exists()
    (lane / "draft.txt").unlink()
    message = bridge.retire(repo, "codex")
    assert "deleted" in message
    assert not lane.exists()
    assert store.authenticate(bridge.home, token) is None
    remaining = bridge.setup(repo)
    assert "codex" not in remaining["participants"]
    assert "claude" in remaining["participants"]
    assert not (lane.parent / "codex-identity.json").exists()
    readded = bridge.add_participant(repo, "codex", "codex")
    assert Path(readded["lanes"]["codex"]).exists()


def test_retire_keeps_a_branch_that_still_holds_commits(bridge, repo, paired):
    lane = Path(paired["lanes"]["codex"])
    branch = paired["branches"]["codex"]
    (lane / "kept.txt").write_text("finished work\n")
    git(lane, "add", "kept.txt")
    git(
        lane,
        "-c",
        "user.name=Bridge Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-m",
        "Finished work",
    )
    assert "kept" in bridge.retire(repo, "codex")
    assert not lane.exists()
    assert git(repo, "rev-parse", "--verify", branch)
    assert "kept.txt" in git(repo, "show", "--name-only", branch)


def test_roster_change_alone_never_denies_a_tool_call(
    bridge, repo, paired, monkeypatch
):
    monkeypatch.setattr(
        "agent_parley.checkpoints.mailbox",
        lambda *args: {"pending_ack": 0, "messages": []},
    )
    directory = Path(paired["lanes"]["claude"]).parent
    write_json(directory / "claude-identity.json", {"name": "claude"})
    payload = {
        "hook_event_name": "PreToolUse",
        "session_id": "test",
        "cwd": paired["lanes"]["claude"],
        "tool_name": "Bash",
    }
    notice = checkpoint(bridge.home, directory, "claude", payload)
    assert "claude, codex" in notice["hookSpecificOutput"]["additionalContext"]
    assert "permissionDecision" not in notice["hookSpecificOutput"]
    stop = {**payload, "hook_event_name": "Stop"}
    bridge.add_participant(repo, "kimi-1", "kimi")
    assert checkpoint(bridge.home, directory, "claude", stop) == {}
    joined = checkpoint(bridge.home, directory, "claude", payload)
    assert "kimi-1" in joined["hookSpecificOutput"]["additionalContext"]
    assert "permissionDecision" not in joined["hookSpecificOutput"]


def test_messages_fan_out_to_every_named_participant(bridge, repo, paired):
    store.initialize(bridge.home)
    data = bridge.add_participant(repo, "kimi-1", "kimi")
    tokens = {
        name: asyncio.run(bridge.identity(name, data))["registration_token"]
        for name in data["participants"]
    }
    sender = store.authenticate(bridge.home, tokens["claude"])
    message = {
        "to": ["codex", "kimi-1"],
        "subject": "Interface change",
        "body_md": "Response now includes session_id.",
        "idempotency_key": "interface-1",
    }
    store.call(bridge.home, sender, "send_message", message)
    for name in ("codex", "kimi-1"):
        actor = store.authenticate(bridge.home, tokens[name])
        inbox = store.call(bridge.home, actor, "fetch_inbox", {})
        assert inbox["messages"][0]["subject"] == "Interface change"
    listed = store.call(bridge.home, sender, "list_participants", {})
    assert listed["you"] == "claude"
    assert len(listed["participants"]) == 3
    with pytest.raises(BridgeError, match="not registered"):
        store.call(
            bridge.home,
            sender,
            "send_message",
            {**message, "to": ["absent"], "idempotency_key": "interface-2"},
        )
    with pytest.raises(BridgeError, match="1..16"):
        store.call(
            bridge.home,
            sender,
            "send_message",
            {
                **message,
                "to": ["codex"] * 17,
                "idempotency_key": "interface-3",
            },
        )


def test_issue_handoff_reaches_a_third_participant(bridge, repo, paired):
    lanes = bridge.add_participant(repo, "kimi-1", "kimi")["lanes"]
    bridge.issue(Path(lanes["claude"]), "claim", "77")
    offer = bridge.issue(
        Path(lanes["claude"]),
        "offer",
        "77",
        to="kimi-1",
        summary="Take the review; commit abc is pushed.",
    )["offer"]
    with pytest.raises(BridgeError, match="recipient"):
        bridge.issue(Path(lanes["codex"]), "accept", "77", offer_id=offer["id"])
    accepted = bridge.issue(
        Path(lanes["kimi-1"]), "accept", "77", offer_id=offer["id"]
    )
    assert accepted["owner"] == "kimi-1"
    with pytest.raises(BridgeError, match="another participant"):
        bridge.issue(
            Path(lanes["kimi-1"]), "offer", "77", to="absent", summary="Take it"
        )


def test_legacy_two_lane_manifest_keeps_lanes_and_identities(
    bridge, repo, paired
):
    _, directory = bridge.project(repo)
    write_json(
        directory / "project.json",
        {
            "root": paired["root"],
            "base": paired["base"],
            "lanes": paired["lanes"],
            "branches": paired["branches"],
        },
    )
    migrated = bridge.setup(repo)
    assert migrated["lanes"] == paired["lanes"]
    assert migrated["participants"]["claude"]["display"] == "GreenCastle"
    assert migrated["participants"]["codex"]["provider"] == "codex"
    extended = bridge.add_participant(repo, "kimi-1", "kimi")
    assert extended["participants"]["claude"]["display"] == "GreenCastle"
    stored = json.loads((directory / "project.json").read_text())
    assert stored["version"] == 2
    assert sorted(stored["participants"]) == ["claude", "codex", "kimi-1"]


def test_joining_participant_is_announced_once(
    bridge, repo, paired, monkeypatch
):
    monkeypatch.setattr(
        "agent_parley.checkpoints.mailbox",
        lambda *args: {"pending_ack": 0, "messages": []},
    )
    directory = Path(paired["lanes"]["claude"]).parent
    write_json(directory / "claude-identity.json", {"name": "claude"})
    payload = {
        "hook_event_name": "UserPromptSubmit",
        "session_id": "test",
        "cwd": paired["lanes"]["claude"],
    }
    first = checkpoint(bridge.home, directory, "claude", payload)
    assert "claude, codex" in first["hookSpecificOutput"]["additionalContext"]
    assert checkpoint(bridge.home, directory, "claude", payload) == {}
    bridge.add_participant(repo, "kimi-1", "kimi")
    joined = checkpoint(bridge.home, directory, "claude", payload)
    assert "kimi-1" in joined["hookSpecificOutput"]["additionalContext"]


def identify(worktree):
    """Gives a fixture repository the identity a merge commit needs."""
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


def test_merge_integrates_a_lane_branch_and_leaves_the_lane_alone(
    bridge, repo, paired
):
    lane = Path(paired["lanes"]["codex"])
    branch = paired["branches"]["codex"]
    directory = lane.parent
    base = git(repo, "branch", "--show-current")
    identify(repo)
    (lane / "feature.txt").write_text("lane work\n")
    commit(lane, "Lane work")

    (lane / "draft.txt").write_text("unsaved\n")
    with pytest.raises(BridgeError, match="uncommitted changes that"):
        bridge.merge(repo, "codex")
    (lane / "draft.txt").unlink()

    (repo / "pending.txt").write_text("base scratch\n")
    with pytest.raises(BridgeError, match="base checkout"):
        bridge.merge(repo, "codex")
    (repo / "pending.txt").unlink()

    with lock(directory / "codex.session.lock"):
        with pytest.raises(BridgeError, match="running session"):
            bridge.merge(repo, "codex")

    message = bridge.merge(repo, "codex")
    assert f"Merged {branch} into {base}" in message
    assert (repo / "feature.txt").read_text() == "lane work\n"
    assert git(repo, "log", "-1", "--pretty=%s") == (
        f"Merge bridge lane codex from {branch}"
    )
    assert len(git(repo, "log", "-1", "--pretty=%P").split()) == 2
    assert lane.exists()
    assert git(lane, "branch", "--show-current") == branch
    assert bridge.merge(repo, "codex") == (
        f"{base} already contains every commit on {branch}."
    )


def test_merge_leaves_a_conflict_resolvable_without_discarding(
    bridge, repo, paired
):
    lane = Path(paired["lanes"]["codex"])
    identify(repo)
    (lane / "shared.txt").write_text("lane version\n")
    commit(lane, "Lane edit")
    (repo / "shared.txt").write_text("base version\n")
    commit(repo, "Base edit")

    with pytest.raises(BridgeError, match="stopped on conflicts") as failure:
        bridge.merge(repo, "codex")
    assert "shared.txt" in str(failure.value)
    assert "merge --abort" in str(failure.value)
    assert "shared.txt" in git(repo, "diff", "--name-only", "--diff-filter=U")

    with pytest.raises(BridgeError, match="already merging"):
        bridge.merge(repo, "codex")
    git(repo, "merge", "--abort")
    assert (repo / "shared.txt").read_text() == "base version\n"
    assert (lane / "shared.txt").read_text() == "lane version\n"


def test_preview_reports_pending_work_and_leaves_the_base_untouched(
    bridge, repo, paired
):
    lane = Path(paired["lanes"]["codex"])
    branch = paired["branches"]["codex"]
    base = git(repo, "branch", "--show-current")
    identify(repo)
    (lane / "feature.txt").write_text("lane work\n")
    commit(lane, "Lane work")
    head = git(repo, "rev-parse", "HEAD")
    status = git(repo, "status", "--porcelain")
    git_dir = Path(
        git(repo, "rev-parse", "--path-format=absolute", "--git-dir")
    )

    report = bridge.preview_merge(repo, "codex")
    assert f"Merging {branch} into {base} would bring in 1 commits:" in report
    assert "Lane work" in report
    assert "feature.txt" in report
    assert f"Nothing refuses this merge; it would land on {base}." in report
    assert "cannot predict conflicts" in report
    assert "would be refused" not in report

    assert git(repo, "rev-parse", "HEAD") == head
    assert git(repo, "status", "--porcelain") == status
    assert not (git_dir / "MERGE_HEAD").exists()
    assert not (repo / "feature.txt").exists()
    assert git(lane, "branch", "--show-current") == branch


def test_preview_reports_a_branch_the_base_already_contains(
    bridge, repo, paired
):
    branch = paired["branches"]["codex"]
    base = git(repo, "branch", "--show-current")
    report = bridge.preview_merge(repo, "codex")
    assert report.endswith(f"{base} already contains every commit on {branch}.")
    assert "would bring in" not in report


def test_preview_gathers_every_blocking_condition_in_one_report(
    bridge, repo, paired
):
    lane = Path(paired["lanes"]["codex"])
    branch = paired["branches"]["codex"]
    base = git(repo, "branch", "--show-current")
    identify(repo)
    (lane / "feature.txt").write_text("lane work\n")
    commit(lane, "Lane work")

    (repo / "pending.txt").write_text("base scratch\n")
    (lane / "draft.txt").write_text("unsaved\n")
    report = bridge.preview_merge(repo, "codex")
    assert f"The base checkout at {repo} has uncommitted changes." in report
    assert (
        f"codex has uncommitted changes that {branch} does not carry." in report
    )
    assert "Lane work" in report
    (repo / "pending.txt").unlink()
    (lane / "draft.txt").unlink()

    git(repo, "checkout", "--detach")
    assert "is on a detached HEAD" in bridge.preview_merge(repo, "codex")
    git(repo, "switch", base)

    git(repo, "switch", "--ignore-other-worktrees", branch)
    assert f"is on {branch} itself" in bridge.preview_merge(repo, "codex")
    git(repo, "switch", base)


def test_preview_reports_a_merge_in_progress_a_drift_and_a_missing_branch(
    bridge, repo, paired
):
    lane = Path(paired["lanes"]["codex"])
    branch = paired["branches"]["codex"]
    identify(repo)
    (lane / "shared.txt").write_text("lane version\n")
    commit(lane, "Lane edit")
    (repo / "shared.txt").write_text("base version\n")
    commit(repo, "Base edit")

    with pytest.raises(BridgeError, match="stopped on conflicts"):
        bridge.merge(repo, "codex")
    report = bridge.preview_merge(repo, "codex")
    assert f"The base checkout at {repo} is already merging." in report
    assert f"The base checkout at {repo} has uncommitted changes." in report
    git(repo, "merge", "--abort")

    git(lane, "switch", "--detach")
    assert "codex lane is on '<detached HEAD>'" in bridge.preview_merge(
        repo, "codex"
    )
    git(repo, "branch", "-D", branch)
    report = bridge.preview_merge(repo, "codex")
    assert f"Branch {branch} no longer exists." in report
    assert "Nothing further can be previewed while the branch" in report
    assert "would bring in" not in report


def test_preview_names_a_running_session_without_taking_its_lock(
    bridge, repo, paired
):
    lane = Path(paired["lanes"]["codex"])
    directory = lane.parent
    identify(repo)
    (lane / "feature.txt").write_text("lane work\n")
    commit(lane, "Lane work")
    write_json(
        directory / "codex-activity.json",
        {
            "activity": "working",
            "session_pid": os.getpid(),
            "session_ticks": start_ticks(os.getpid()),
        },
    )
    with lock(directory / "codex.session.lock"):
        report = bridge.preview_merge(repo, "codex")
        with pytest.raises(BridgeError, match="running session"):
            bridge.merge(repo, "codex")
    assert "codex has a running session; stop that terminal first." in report
    assert "Lane work" in report


def verification_script(directory, name, body):
    """Publishes an executable stand-in for a repository verification gate."""
    path = directory / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


def test_merge_runs_the_repository_verification_command_first(
    bridge, repo, paired, tmp_path
):
    lane = Path(paired["lanes"]["codex"])
    branch = paired["branches"]["codex"]
    root, _ = bridge.project(repo)
    base = git(repo, "branch", "--show-current")
    identify(repo)
    (lane / "feature.txt").write_text("lane work\n")
    commit(lane, "Lane work")

    assert "no verification command" in bridge.verification(repo)
    assert f"Merged {branch} into {base}" in bridge.merge(repo, "codex")
    assert (repo / "feature.txt").read_text() == "lane work\n"

    (lane / "second.txt").write_text("more lane work\n")
    commit(lane, "More lane work")

    red_marker = tmp_path / "red.txt"
    red = verification_script(
        tmp_path,
        "red.sh",
        f"pwd -P > {shlex.quote(str(red_marker))}\n"
        "echo checked line one\n"
        "echo checked line two >&2\n"
        "exit 3\n",
    )
    bridge.verification(repo, shlex.quote(str(red)))
    with pytest.raises(BridgeError, match="Verification failed") as failure:
        bridge.merge(repo, "codex")
    assert "exited 3" in str(failure.value)
    assert "checked line one" in str(failure.value)
    assert "checked line two" in str(failure.value)
    assert Path(red_marker.read_text().strip()).resolve() == root.resolve()
    assert not (repo / "second.txt").exists()

    bridge.verification(repo, shlex.quote(str(tmp_path / "absent.sh")))
    with pytest.raises(BridgeError, match="could not run"):
        bridge.merge(repo, "codex")
    assert not (repo / "second.txt").exists()

    with pytest.raises(BridgeError, match="argument list"):
        bridge.verification(repo, "make 'check")

    green_marker = tmp_path / "green.txt"
    green = verification_script(
        tmp_path, "green.sh", f"pwd -P > {shlex.quote(str(green_marker))}\n"
    )
    assert "runs `" in bridge.verification(repo, shlex.quote(str(green)))
    assert f"Merged {branch} into {base}" in bridge.merge(repo, "codex")
    assert Path(green_marker.read_text().strip()).resolve() == root.resolve()
    assert (repo / "second.txt").read_text() == "more lane work\n"

    assert "no verification command" in bridge.verification(repo, "")
    assert roster.read(Path(paired["lanes"]["codex"]).parent)["verify"] == []


def test_event_history_is_bounded_by_age_and_reported_by_window(
    bridge, repo, paired, tmp_path, capsys
):
    directory = Path(paired["lanes"]["claude"]).parent
    log = directory / "claude-events.jsonl"
    now = time.time()
    expired = now - MAX_EVENT_LOG_AGE - 60
    log.write_text(
        "".join(
            json.dumps(entry) + "\n"
            for entry in (
                {
                    "ts": expired,
                    "decision": "deny",
                    "injected_bytes": 5,
                    "reason_class": "branch_drift",
                },
                {
                    "ts": now - 30,
                    "decision": "allow",
                    "injected_bytes": 7,
                    "reason_class": "coordination_pending",
                },
            )
        )
    )
    assert event_summary(directory, "claude")["events"] == 2
    windowed = event_summary(directory, "claude", now - 60)
    assert windowed["events"] == 1
    assert windowed["denials"] == 0
    assert windowed["injected_bytes"] == 7

    dashboard.run(bridge.home, lambda: False, once=True, window=3600.0)
    assert "last 60m" in capsys.readouterr().out
    dashboard.run(bridge.home, lambda: False, once=True)
    assert "all retained" in capsys.readouterr().out

    destination = tmp_path / "events.jsonl"
    report = bridge.export_events(repo, ("claude",), output=destination)
    exported = [
        json.loads(line) for line in destination.read_text().splitlines()
    ]
    assert [entry["participant"] for entry in exported] == ["claude"] * 2
    assert [entry["ts"] for entry in exported] == [expired, now - 30]
    assert "2 records" in report and str(destination) in report
    assert bridge.export_events(repo, (), now - expired - 30, destination)
    assert len(destination.read_text().splitlines()) == 1

    with pytest.raises(BridgeError, match="Not a participant"):
        bridge.export_events(repo, ("absent",))

    assert prune(directory, "claude", now) == 1
    assert event_summary(directory, "claude")["events"] == 1
    assert json.loads(log.read_text().splitlines()[0])["ts"] == now - 30


def test_session_end_discards_event_records_past_the_retention_age(
    bridge, repo, paired
):
    lane = Path(paired["lanes"]["claude"])
    directory = lane.parent
    write_json(directory / "claude-identity.json", {"name": "claude"})
    log = directory / "claude-events.jsonl"
    log.write_text(
        json.dumps(
            {
                "ts": time.time() - MAX_EVENT_LOG_AGE - 60,
                "decision": "deny",
                "injected_bytes": 5,
                "reason_class": "branch_drift",
            }
        )
        + "\n"
    )
    checkpoint(
        bridge.home,
        directory,
        "claude",
        {
            "hook_event_name": "SessionEnd",
            "cwd": str(lane),
            "session_id": "test",
        },
    )
    entries = [json.loads(line) for line in log.read_text().splitlines()]
    assert [entry["event"] for entry in entries] == ["SessionEnd"]
