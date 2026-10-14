"""Checks the Copilot hook wire contract through the configured command."""

import io
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from agent_parley import cli, copilot, dialogs, hook, roster, store
from agent_parley.cli import COPILOT_EVENTS, main
from agent_parley.state import BridgeError, write_json

STUB = (
    "#!/usr/bin/env python3\n"
    "import json, os, sys\n"
    "with open(os.environ['CAPTURE'], 'w') as f:\n"
    " json.dump({'argv': sys.argv[1:]}, f)\n"
)


@pytest.fixture
def lane(bridge, repo, monkeypatch, tmp_path):
    """Launches a Copilot lane and reports its configured hook commands."""
    binary = tmp_path / "bin"
    binary.mkdir()
    executable = binary / "copilot"
    executable.write_text(STUB)
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(binary) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("CAPTURE", str(tmp_path / "capture.json"))
    monkeypatch.setattr(bridge, "up", lambda: None)
    store.initialize(bridge.home)
    account = tmp_path / "copilot-account"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agent-parley",
            "--home",
            str(bridge.home),
            "credentials",
            "add",
            "work",
            "--config-home",
            str(account),
        ],
    )
    assert main() == 0
    assert bridge.launch("helper", repo, "Coordinate", "copilot", "work") == 0
    data = roster.read(bridge.project(repo)[1])
    settings = json.loads((account / "settings.json").read_text())
    return {
        "commands": {
            event: entries[0]["bash"]
            for event, entries in settings["hooks"].items()
        },
        "lane": Path(data["participants"]["helper"]["lane"]),
        "directory": bridge.project(repo)[1],
    }


def run_hook(lane, event, payload, session="helper"):
    """Runs one configured hook in a session the launcher named."""
    env = {**os.environ}
    env.pop("AGENT_PARLEY_PARTICIPANT", None)
    if session is not None:
        env["AGENT_PARLEY_PARTICIPANT"] = session
    return subprocess.run(
        shlex.split(lane["commands"][event]),
        input=json.dumps({"hook_event_name": event, **payload}),
        capture_output=True,
        text=True,
        check=False,
        cwd=lane["lane"],
        env=env,
    )


def fire(lane, event, payload):
    """Runs one configured hook command exactly as Copilot would run it."""
    result = run_hook(lane, event, payload)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_every_supported_event_is_registered_in_compatible_form(lane):
    assert set(lane["commands"]) == set(COPILOT_EVENTS)
    assert all(
        command.endswith("--adapter copilot")
        for command in lane["commands"].values()
    )


def test_a_native_branch_switch_is_refused_in_copilot_schema(lane):
    output = fire(
        lane,
        "PreToolUse",
        {
            "session_id": "session-1",
            "cwd": str(lane["lane"]),
            "tool_name": "shell",
            "tool_input": {"command": "git switch main"},
        },
    )
    assert output["permissionDecision"] == "deny"
    assert "owns this worktree" in output["permissionDecisionReason"]
    assert "hookSpecificOutput" not in output


@pytest.mark.parametrize(
    ("command", "denied"),
    [
        ("git worktree add {wt}/fix -b fix/foo", False),
        ("git -C .. worktree add {name}-wt/up", False),
        ("git worktree add /tmp/x -b fix/foo", True),
        ("git worktree add -b fix/foo ../beside", True),
        ("git -C .. worktree add beside", True),
        ("git worktree add ~/stray-wt", True),
    ],
)
def test_a_worktree_add_outside_the_lane_wt_is_refused(lane, command, denied):
    path = Path(lane["lane"]).resolve()
    home = Path("~/stray-wt").expanduser().resolve()
    text = command.format(wt=f"{path}-wt", name=path.name)
    output = fire(
        lane,
        "PreToolUse",
        {
            "session_id": "session-wt",
            "cwd": str(path),
            "tool_name": "shell",
            "tool_input": {"command": text},
        },
    )
    assert (output.get("permissionDecision") == "deny") is denied
    if denied:
        assert f"inside {path}-wt/" in output["permissionDecisionReason"]
    if "~" in command:
        reason = output["permissionDecisionReason"]
        assert f"{home} is outside it" in reason


def test_a_native_session_start_records_activity_and_session(lane):
    output = fire(
        lane,
        "SessionStart",
        {"session_id": "session-7", "cwd": str(lane["lane"])},
    )
    assert "Participants: helper" in output["additionalContext"]
    assert "hookSpecificOutput" not in output
    state = json.loads((lane["directory"] / "helper-activity.json").read_text())
    assert state["session_id"] == "session-7"
    assert state["event"] == "SessionStart"
    assert state["activity"] == "working"


def test_a_native_stop_without_coordination_ends_the_turn(lane):
    fire(
        lane,
        "SessionStart",
        {"session_id": "session-9", "cwd": str(lane["lane"])},
    )
    assert (
        fire(
            lane,
            "Stop",
            {"session_id": "session-9", "cwd": str(lane["lane"])},
        )
        == {}
    )


def test_an_approval_request_never_carries_a_permission_decision(lane):
    output = fire(
        lane,
        "PermissionRequest",
        {
            "session_id": "session-3",
            "cwd": str(lane["lane"]),
            "tool_name": "shell",
            "tool_input": {"command": "rm -rf /"},
        },
    )
    assert "permissionDecision" not in output
    assert "behavior" not in output


def test_a_qualified_coordination_tool_is_recognized_after_renaming():
    assert (
        copilot.payload({"tool_name": "mcp-agent-parley-fetch_inbox"})[
            "tool_name"
        ]
        == "mcp__agent_parley__fetch_inbox"
    )
    assert copilot.payload({"tool_name": "shell"})["tool_name"] == "shell"


def test_shared_output_is_flattened_into_copilot_fields():
    assert copilot.response(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "Review mail",
                "additionalContext": "Message 7",
            }
        }
    ) == {
        "permissionDecision": "deny",
        "permissionDecisionReason": "Review mail",
        "additionalContext": "Message 7",
    }
    assert copilot.response(
        {"decision": "block", "reason": "Restore branch"}
    ) == {"decision": "block", "reason": "Restore branch"}
    assert copilot.response({}) == {}


def test_resume_without_a_recorded_session_is_refused(lane, bridge, repo):
    with pytest.raises(BridgeError, match="No usable native session"):
        bridge.launch(
            "helper", repo, "Continue", "copilot", "work", resume=True
        )


def test_a_relaunch_adds_no_duplicate_hooks_and_retire_removes_them(
    lane, bridge, repo, tmp_path
):
    account = tmp_path / "copilot-account"
    settings_path = account / "settings.json"
    before = json.loads(settings_path.read_text())
    before["hooks"]["PreToolUse"].insert(
        0, {"type": "command", "bash": "operator-policy", "timeoutSec": 1}
    )
    settings_path.write_text(json.dumps(before))
    assert bridge.launch("helper", repo, "Coordinate", "copilot", "work") == 0
    after = json.loads(settings_path.read_text())
    assert after["hooks"] == before["hooks"]
    foreign = {
        **after["hooks"]["PreToolUse"][1],
        "bash": after["hooks"]["PreToolUse"][1]["bash"].replace(
            "--participant helper", "--participant second"
        ),
    }
    after["hooks"]["PreToolUse"].append(foreign)
    settings_path.write_text(json.dumps(after))
    bridge.retire(repo, "helper")
    settings = json.loads(settings_path.read_text())
    assert settings["hooks"]["PreToolUse"] == [
        {"type": "command", "bash": "operator-policy", "timeoutSec": 1},
        foreign,
    ]
    servers = json.loads((account / "mcp-config.json").read_text())
    assert "agent_parley" in servers["mcpServers"]
    copilot.release_copilot(
        bridge.home, {"provider": "copilot", "credential": "work"}, "second"
    )
    settings = json.loads(settings_path.read_text())
    assert settings["hooks"]["PreToolUse"] == [
        {"type": "command", "bash": "operator-policy", "timeoutSec": 1}
    ]
    servers = json.loads((account / "mcp-config.json").read_text())
    assert "agent_parley" not in servers["mcpServers"]


def test_a_second_copilot_lane_on_one_profile_is_refused_naming_the_first(
    lane, bridge, repo, tmp_path
):
    with pytest.raises(BridgeError, match="hooks of helper") as refusal:
        bridge.launch("second", repo, "Coordinate", "copilot", "work")
    assert "credentials add" in str(refusal.value)
    settings = json.loads(
        (tmp_path / "copilot-account" / "settings.json").read_text()
    )
    assert "--participant second" not in json.dumps(settings)
    assert bridge.launch("helper", repo, "Coordinate", "copilot", "work") == 0


@pytest.mark.parametrize("session", ["alpha", None])
def test_a_hook_for_another_participant_prints_nothing_and_succeeds(
    lane, session
):
    result = run_hook(
        lane,
        "PreToolUse",
        {
            "session_id": "session-x",
            "cwd": "/elsewhere",
            "tool_name": "shell",
            "tool_input": {"command": "git switch main"},
        },
        session=session,
    )
    assert result.returncode == 0
    assert result.stdout == ""


def test_the_in_process_hook_ignores_another_participants_session(
    monkeypatch, capsys
):
    monkeypatch.setenv("AGENT_PARLEY_PARTICIPANT", "alpha")
    monkeypatch.setattr(
        sys,
        "argv",
        ["hook", "--participant", "beta", "--adapter", "copilot"],
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    assert hook.main() == 0
    assert capsys.readouterr().out == ""


def test_a_relaunch_drops_hooks_a_vanished_lane_left_in_the_profile(
    lane, bridge, repo, tmp_path
):
    settings_path = tmp_path / "copilot-account" / "settings.json"
    settings = json.loads(settings_path.read_text())
    entry = settings["hooks"]["PreToolUse"][0]
    settings["hooks"]["PreToolUse"].append(
        {
            **entry,
            "bash": entry["bash"].replace(
                "--participant helper", "--participant gone"
            ),
        }
    )
    settings_path.write_text(json.dumps(settings))
    assert bridge.launch("helper", repo, "Coordinate", "copilot", "work") == 0
    settings = json.loads(settings_path.read_text())
    assert "--participant gone" not in json.dumps(settings)
    assert settings["hooks"]["PreToolUse"] == [entry]


def test_retiring_a_lane_whose_provider_needs_an_unset_variable_unhooks_it(
    lane, bridge, repo, tmp_path, monkeypatch
):
    roster.define_provider(
        bridge.home,
        "vendor",
        "copilot",
        "copilot",
        "COPILOT_HOME",
        [],
        ["VENDOR_TOKEN"],
    )
    other = tmp_path / "vendor-account"
    roster.define_credential(bridge.home, "vendor-work", str(other), [], [])
    monkeypatch.setenv("VENDOR_TOKEN", "exported")
    assert bridge.launch("vendored", repo, "Go", "vendor", "vendor-work") == 0
    assert "--participant vendored" in (other / "settings.json").read_text()
    monkeypatch.delenv("VENDOR_TOKEN")
    bridge.retire(repo, "vendored")
    settings = (other / "settings.json").read_text()
    assert "--participant vendored" not in settings


def test_a_copilot_lane_stays_interactive_on_launch_and_on_resume(
    lane, bridge, repo, tmp_path, monkeypatch
):
    capture = tmp_path / "capture.json"
    assert json.loads(capture.read_text())["argv"][0] == "-i"
    fire(
        lane,
        "SessionStart",
        {"session_id": "session-5", "cwd": str(lane["lane"])},
    )
    monkeypatch.setattr(
        cli.terminal,
        "run",
        lambda command, lane, env, agent, **_: subprocess.call(
            command, cwd=lane, env=env
        ),
    )
    assert (
        bridge.launch(
            "helper", repo, "Continue", "copilot", "work", resume=True
        )
        == 0
    )
    assert json.loads(capture.read_text())["argv"][:3] == [
        "--resume",
        "session-5",
        "-i",
    ]


def test_an_opted_in_copilot_lane_allows_only_the_bridge_server(
    lane, bridge, repo, capsys
):
    capture = Path(os.environ["CAPTURE"])
    assert "--allow-tool" not in json.loads(capture.read_text())["argv"]
    capsys.readouterr()
    assert bridge.launch("helper", repo, "Coordinate", "copilot", "work") == 0
    assert "helper is a copilot lane" in capsys.readouterr().err
    path = lane["directory"] / "project.json"
    manifest = json.loads(path.read_text())
    manifest.setdefault("supervision", {})[dialogs.PRE_APPROVE] = True
    write_json(path, manifest)
    assert bridge.launch("helper", repo, "Coordinate", "copilot", "work") == 0
    assert "is a copilot lane" not in capsys.readouterr().err
    argv = json.loads(capture.read_text())["argv"]
    assert argv.count("--allow-tool") == 1
    assert argv[argv.index("--allow-tool") + 1] == "agent_parley"
    assert not {"--allow-all-tools", "--allow-all"} & set(argv)
