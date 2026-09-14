"""Checks operator pause, resume, stop and restart against real lane state."""

import asyncio
import contextlib
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from agent_parley import checkpoints, cli, dashboard, process, roster, store
from agent_parley.state import BridgeError, write_json

SLEEPER = "import time; time.sleep(120)"


@contextlib.asynccontextmanager
async def client_session(url, auth):
    """Connects the official SDK to the real local HTTP service."""
    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {auth}"}, trust_env=False
    ) as http:
        async with streamable_http_client(url, http_client=http) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                yield session


def reasons(directory, name):
    """Reports the recorded enforcement reasons for one lane."""
    return [
        entry["reason_class"]
        for entry in checkpoints.read_events(directory, name)
    ]


def test_pause_refuses_served_calls_and_resume_restores_them(
    bridge, repo, paired
):
    store.initialize(bridge.home)
    bridge.up()
    try:
        token = asyncio.run(bridge.identity("claude", paired))[
            "registration_token"
        ]

        async def call():
            async with client_session(bridge.url + "/mcp/", token) as session:
                return await session.call_tool("fetch_inbox", {})

        assert not asyncio.run(call()).is_error
        assert "paused" in bridge.pause(repo, "claude")
        refused = asyncio.run(call())
        assert refused.is_error
        assert "paused by the operator" in refused.content[0].text
        assert "resumed" in bridge.pause(repo, "claude", resume=True)
        assert not asyncio.run(call()).is_error
    finally:
        bridge.down()


def test_pause_refuses_tool_use_through_the_hook_with_one_reason(
    bridge, repo, paired
):
    directory = Path(paired["lanes"]["claude"]).parent
    bridge.pause(repo, "claude")
    output = checkpoints.checkpoint(
        bridge.home,
        directory,
        "claude",
        {
            "hook_event_name": "PreToolUse",
            "session_id": "s1",
            "cwd": paired["lanes"]["claude"],
            "tool_name": "shell",
            "tool_input": {"command": "ls"},
        },
    )
    details = output["hookSpecificOutput"]
    assert details["permissionDecision"] == "deny"
    assert details["permissionDecisionReason"] == roster.PAUSED_REASON
    assert "paused" in reasons(directory, "claude")


def test_pause_retains_claims_and_never_releases_them(bridge, repo, paired):
    lane = Path(paired["lanes"]["claude"])
    bridge.issue(lane, "claim", "7")
    message = bridge.pause(repo, "claude")
    assert "#7" in message
    assert "nothing was released" in message
    ledger = cli.snapshot(lane.parent)["issues"]["7"]
    assert ledger["owner"] == "claude"
    assert "already paused" in bridge.pause(repo, "claude")


def test_top_reports_a_paused_lane(bridge, repo, paired, capsys):
    bridge.pause(repo, "claude")
    dashboard.run(bridge.home, lambda: False, once=True)
    assert "paused" in capsys.readouterr().out


def test_every_operator_command_is_recorded_with_its_reason(
    bridge, repo, paired
):
    directory = Path(paired["lanes"]["claude"]).parent
    bridge.pause(repo, "claude")
    bridge.pause(repo, "claude", resume=True)
    bridge.stop(repo, "claude")
    recorded = reasons(directory, "claude")
    assert "operator_paused" in recorded
    assert "operator_resumed" in recorded
    assert "operator_stopped" in recorded


def test_stop_reports_holdings_when_no_verified_session_runs(
    bridge, repo, paired
):
    lane = Path(paired["lanes"]["claude"])
    bridge.issue(lane, "claim", "7")
    message = bridge.stop(repo, "claude")
    assert "no verified running session" in message
    assert "#7" in message
    assert cli.snapshot(lane.parent)["issues"]["7"]["owner"] == "claude"


def test_stop_never_signals_a_process_whose_identity_changed(
    bridge, repo, paired
):
    directory = Path(paired["lanes"]["claude"]).parent
    child = subprocess.Popen([sys.executable, "-c", SLEEPER])
    try:
        write_json(
            directory / "claude-activity.json",
            {
                "activity": "working",
                "session_pid": child.pid,
                "session_ticks": "0",
            },
        )
        assert "no verified running session" in bridge.stop(repo, "claude")
        assert child.poll() is None
    finally:
        child.kill()
        child.wait(10)


def test_stop_ends_a_recorded_session_and_keeps_its_claims(
    bridge, repo, paired
):
    lane = Path(paired["lanes"]["claude"])
    directory = lane.parent
    bridge.issue(lane, "claim", "7")
    child = subprocess.Popen([sys.executable, "-c", SLEEPER])
    try:
        write_json(
            directory / "claude-activity.json",
            {
                "activity": "working",
                "session_pid": child.pid,
                "session_ticks": process.start_ticks(child.pid),
            },
        )
        message = bridge.stop(repo, "claude")
        assert "ended from the base checkout" in message
        assert "#7" in message
        assert child.poll() is not None
        state = json.loads((directory / "claude-activity.json").read_text())
        assert state["activity"] == "stopped"
        assert "session_pid" not in state
        assert cli.snapshot(directory)["issues"]["7"]["owner"] == "claude"
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(10)


def test_restart_refuses_while_a_session_is_alive(bridge, repo, paired):
    directory = Path(paired["lanes"]["claude"]).parent
    write_json(
        directory / "claude-activity.json",
        {
            "activity": "working",
            "session_pid": os.getpid(),
            "session_ticks": process.start_ticks(os.getpid()),
        },
    )
    with pytest.raises(BridgeError, match="still has a live session"):
        bridge.restart(repo, "claude")


def test_restart_refuses_a_dirty_worktree_and_names_the_paths(
    bridge, repo, paired
):
    lane = Path(paired["lanes"]["claude"])
    (lane / "scratch.txt").write_text("unsaved work\n")
    with pytest.raises(BridgeError) as refusal:
        bridge.restart(repo, "claude")
    message = str(refusal.value)
    assert "uncommitted changes" in message
    assert "scratch.txt" in message
    assert "resets, cleans or stashes" in message
    assert (lane / "scratch.txt").read_text() == "unsaved work\n"


def test_restart_replays_initialization_and_launches_the_same_provider(
    bridge, repo, paired, monkeypatch, tmp_path
):
    record = tmp_path / "restart-init.txt"
    monkeypatch.setenv("INIT_RECORD", str(record))
    bridge.initialization(
        repo,
        f"{sys.executable} -c "
        + repr(
            "import os, pathlib;"
            "pathlib.Path(os.environ['INIT_RECORD']).write_text(os.getcwd())"
        ),
    )
    captured: list = []
    monkeypatch.setattr(
        bridge,
        "launch",
        lambda *args, **kwargs: captured.append(args) or 0,
    )
    assert bridge.restart(repo, "claude", "Continue") == 0
    lane = Path(paired["lanes"]["claude"])
    assert Path(record.read_text()).resolve() == lane.resolve()
    assert captured[0][0] == "claude"
    assert captured[0][2] == "Continue"
    assert captured[0][3] == "claude"
    assert "operator_restarted" in reasons(lane.parent, "claude")


def test_a_paused_flag_only_applies_to_the_named_lane(bridge, repo, paired):
    bridge.pause(repo, "claude")
    assert roster.paused(bridge.home, paired["root"], "claude")
    assert not roster.paused(bridge.home, paired["root"], "codex")
    assert not roster.paused(bridge.home, "/no/such/project", "claude")


def test_a_non_boolean_paused_setting_is_refused(bridge, repo, paired):
    directory = Path(paired["lanes"]["claude"]).parent
    manifest = json.loads((directory / "project.json").read_text())
    manifest["participants"]["claude"]["paused"] = "yes"
    write_json(directory / "project.json", manifest)
    with pytest.raises(BridgeError, match="paused setting must be a boolean"):
        roster.read(directory)
