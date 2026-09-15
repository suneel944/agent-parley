"""Regression coverage for lane, configuration and inbox hardening."""

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from agent_parley import checkpoints, cli, dashboard, process, roster
from agent_parley.state import BridgeError, write_json


@pytest.mark.parametrize(
    "command", ["git switch main", "git branch -m feature"]
)
def test_child_tool_events_guard_the_lane_without_changing_parent_activity(
    bridge, paired, command
):
    lane = Path(paired["lanes"]["codex"])
    state_path = lane.parent / "codex-activity.json"
    parent = {"session_id": "parent", "activity": "working", "cursor": 42}
    write_json(state_path, parent)
    payload = {
        "hook_event_name": "PreToolUse",
        "agent_id": "child",
        "session_id": "child-session",
        "cwd": str(lane),
        "tool_name": "Bash",
        "tool_input": {"command": command},
    }
    result = checkpoints.checkpoint(bridge.home, lane.parent, "codex", payload)
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert json.loads(state_path.read_text()) == parent
    payload["tool_input"] = {"command": "git status"}
    assert (
        checkpoints.checkpoint(bridge.home, lane.parent, "codex", payload) == {}
    )
    assert json.loads(state_path.read_text()) == parent
    payload["hook_event_name"] = "SessionEnd"
    assert (
        checkpoints.checkpoint(bridge.home, lane.parent, "codex", payload) == {}
    )
    assert json.loads(state_path.read_text()) == parent


def test_copilot_preserves_custom_config_and_does_not_duplicate_hooks(tmp_path):
    other = {"type": "stdio", "command": "custom-mcp"}
    user_hook = {"type": "command", "bash": "user-check"}
    lane_hook = {"type": "command", "bash": "lane-check"}
    write_json(
        tmp_path / "mcp-config.json",
        {"mcpServers": {"custom": other}, "extra": 7},
    )
    write_json(
        tmp_path / "settings.json",
        {
            "version": 2,
            "theme": "dark",
            "hooks": {"preToolUse": [user_hook], "custom": []},
        },
    )
    for _ in range(2):
        cli.configure_copilot(
            tmp_path,
            {"url": "http://localhost/mcp"},
            {"preToolUse": [lane_hook]},
        )
    config = json.loads((tmp_path / "mcp-config.json").read_text())
    assert config["mcpServers"]["custom"] == other
    assert config["extra"] == 7
    settings = json.loads((tmp_path / "settings.json").read_text())
    assert settings == {
        "version": 2,
        "theme": "dark",
        "hooks": {"preToolUse": [user_hook, lane_hook], "custom": []},
    }


@pytest.mark.parametrize(
    "bad", ["{", "[]", '{"hooks": []}', '{"hooks": {"preToolUse": {}}}']
)
def test_invalid_copilot_config_preserves_both_original_files(tmp_path, bad):
    server_path = tmp_path / "mcp-config.json"
    settings_path = tmp_path / "settings.json"
    server_path.write_text('{"mcpServers": {"custom": {}}}')
    original = server_path.read_bytes()
    settings_path.write_text(bad)
    with pytest.raises(BridgeError):
        cli.configure_copilot(
            tmp_path, {"url": "http://localhost/mcp"}, {"preToolUse": [{}]}
        )
    assert server_path.read_bytes() == original
    assert settings_path.read_text() == bad


def test_remove_profiles_preserves_native_login_and_restores_presets(
    bridge, tmp_path
):
    native = tmp_path / "native"
    roster.define_credential(bridge.home, "work", str(native), [], [])
    login = native / "login.json"
    login.write_text("native login")
    roster.remove(bridge.home, "credentials", "work")
    assert "work" not in roster.credentials(bridge.home)
    assert login.read_text() == "native login"
    roster.define_provider(
        bridge.home, "claude", "claude", "custom-cli", "", [], []
    )
    roster.remove(bridge.home, "provider", "claude")
    assert roster.provider(bridge.home, "claude") == roster.PRESETS["claude"]
    with pytest.raises(BridgeError, match="No local"):
        roster.remove(bridge.home, "provider", "claude")


@pytest.mark.parametrize(
    "env,require", [(["TOKEN=secret"], []), ([], ["invalid-name"])]
)
def test_rejected_credential_does_not_create_config_directory(
    bridge, tmp_path, env, require
):
    native = tmp_path / "must-not-exist"
    with pytest.raises(BridgeError):
        roster.define_credential(bridge.home, "work", str(native), env, require)
    assert not native.exists()
    assert "work" not in roster.credentials(bridge.home)


def test_provider_cli_warns_on_preset_shadow_and_removes_override(
    bridge, monkeypatch, capsys
):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agent-parley",
            "--home",
            str(bridge.home),
            "provider",
            "add",
            "claude",
            "--adapter",
            "claude",
            "--executable",
            "custom-cli",
        ],
    )
    assert cli.main() == 0
    assert "shadows a built-in preset" in capsys.readouterr().err
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agent-parley",
            "--home",
            str(bridge.home),
            "provider",
            "remove",
            "claude",
        ],
    )
    assert cli.main() == 0
    assert roster.provider(bridge.home, "claude")["command"] == "claude"


@pytest.mark.parametrize("operation", ["push", "pull", "fetch"])
def test_network_git_operations_have_no_kill_timeout(
    tmp_path, monkeypatch, operation
):
    calls = []

    def run(command, **kwargs):
        calls.append(kwargs)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(cli.subprocess, "run", run)
    cli.git(tmp_path, operation, "origin")
    cli.git(tmp_path, "status")
    assert calls[0]["timeout"] is None
    assert calls[1]["timeout"] == 30


def test_a_merge_that_outlives_its_timeout_is_stopped_with_a_remedy(
    bridge, repo, paired, monkeypatch
):
    lane = Path(paired["lanes"]["codex"])
    branch = paired["branches"]["codex"]
    cli.git(repo, "config", "user.name", "Bridge Test")
    cli.git(repo, "config", "user.email", "test@example.com")
    (lane / "feature.txt").write_text("lane work\n")
    cli.git(lane, "add", "--all")
    cli.git(
        lane,
        "-c",
        "user.name=Bridge Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-m",
        "Lane work",
    )
    running = cli.subprocess.run

    def run(command, **kwargs):
        if "merge" in command:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return running(command, **kwargs)

    monkeypatch.setattr(cli.subprocess, "run", run)
    with pytest.raises(BridgeError) as failure:
        cli.merge_branch(repo, lane, "codex", branch)
    assert f"after {cli.GIT_SECONDS} seconds" in str(failure.value)
    assert "participant merge codex" in str(failure.value)
    assert not (repo / "feature.txt").exists()


@pytest.mark.parametrize("width", [40, 80, 120, 200])
def test_dashboard_fits_terminal_and_names_hidden_participants(
    bridge, paired, width
):
    directory = Path(paired["lanes"]["codex"]).parent
    write_json(
        directory / "codex-activity.json",
        {
            "session_pid": os.getpid(),
            "session_ticks": process.start_ticks(os.getpid()),
        },
    )
    view = dashboard.collect(bridge.home, False, {})
    rows = view["projects"][0]["rows"]
    codex = next(row for row in rows if row["participant"] == "codex")
    assert codex["state"] == "running; no hooks"
    rows[:] = [{**codex, "participant": f"lane-{index}"} for index in range(20)]
    lines = dashboard.render(view, width=width, height=12)
    assert len(lines) <= 12
    assert all(len(line) <= width for line in lines)
    shown = sum(line.startswith("lane-") for line in lines)
    assert lines[-1] == f"rows 1-{shown} of 20"
    assert "MAIL" in "\n".join(lines)
    if width >= 80:
        assert "running; no hooks" in "\n".join(lines)
    span = sum(size + 2 for _, size in dashboard.COLUMNS) - 2
    if width < span:
        assert "Hidden columns:" in "\n".join(lines)


def test_gh_pins_origin_when_upstream_also_exists(repo, tmp_path, monkeypatch):
    cli.git(
        repo, "remote", "add", "origin", "https://github.com/example/fork.git"
    )
    cli.git(
        repo,
        "remote",
        "add",
        "upstream",
        "https://github.com/example/upstream.git",
    )
    executable = tmp_path / "gh"
    executable.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    executable.chmod(0o755)
    monkeypatch.setattr(cli.shutil, "which", lambda name: str(executable))
    output = cli.gh(repo, "pr", "list").splitlines()
    assert output[-2:] == ["--repo", "example/fork"]


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux pidfd")
def test_portable_python_shutdown_keeps_pidfd_identity_check(monkeypatch):
    monkeypatch.delattr(os, "pidfd_open", raising=False)
    monkeypatch.delattr(signal, "pidfd_send_signal", raising=False)
    with subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"]
    ) as child:
        try:
            ticks = process.start_ticks(child.pid)
            with pytest.raises(BridgeError, match="PID changed"):
                process.linux_terminate(child.pid, ticks + "wrong")
            assert child.poll() is None
            process.linux_terminate(child.pid, ticks)
            child.wait(timeout=5)
        finally:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=5)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux pidfd")
def test_portable_python_pidfd_preserves_native_errors():
    with pytest.raises(ProcessLookupError):
        process.libc_pidfd("pidfd_open", 2**30, 0)
    with pytest.raises(OSError):
        process.libc_pidfd("pidfd_send_signal", -1, signal.SIGTERM, None, 0)


def test_the_hook_module_imports_without_launcher_only_dependencies():
    launcher_only = (
        "argparse",
        "dataclasses",
        "inspect",
        "importlib.metadata",
        "tomllib",
        "agent_parley.gemini",
        "agent_parley.copilot",
    )
    loaded = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, agent_parley.checkpoints; "
            f"print(sorted(set(sys.modules) & set({launcher_only!r})))",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout.strip()
    assert loaded == "[]"
