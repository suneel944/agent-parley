"""Checks that a side question never touches the lane it is about."""

import json
import subprocess
from pathlib import Path

import pytest

from agent_parley import budgets, cli, views
from agent_parley.hook import PARTICIPANT_ENV
from agent_parley.launch import ask_command
from agent_parley.state import BridgeError, write_json

SESSION = "12345678-abcd-1234-abcd-123456789abc"


@pytest.fixture
def ran(monkeypatch):
    """Records every native command instead of starting a model CLI."""
    calls: list = []
    original = subprocess.run

    def run(command, *args, **kwargs):
        if command[0] == "git":
            return original(command, *args, **kwargs)
        calls.append({"command": command, **kwargs})
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setenv(PARTICIPANT_ENV, "someone")
    monkeypatch.setenv("AGENT_PARLEY_TOKEN", "secret")
    return calls


def lane(bridge, repo, name, provider):
    """Registers a lane with a recorded session and returns its paths."""
    manifest = bridge.add_participant(repo, name, provider)
    worktree = Path(manifest["lanes"][name])
    directory = worktree.parent
    write_json(
        directory / f"{name}-activity.json",
        {"activity": "idle", "session_id": SESSION, "summary": "Parser."},
    )
    return worktree, directory


def test_a_claude_lane_is_forked_read_only_outside_its_worktree(
    bridge, repo, ran
):
    worktree, directory = lane(bridge, repo, "claude", "claude")
    activity = (directory / "claude-activity.json").read_text()
    head = subprocess.check_output(
        ["git", "-C", str(worktree), "rev-parse", "HEAD"]
    )

    assert bridge.ask("claude", repo, "Which test fails?") == 0

    [call] = ran
    command = call["command"]
    assert command[:4] == ["/bin/claude", "-p", "--permission-mode", "plan"]
    assert command[command.index("--resume") + 1] == SESSION
    assert "--fork-session" in command
    assert command[-1].endswith("Question:\nWhich test fails?")
    assert not any("dangerously" in part for part in command)
    assert Path(call["cwd"]) == budgets.ask_directory(directory, "claude")
    assert Path(call["cwd"]) != worktree
    assert PARTICIPANT_ENV not in call["env"]
    assert "AGENT_PARLEY_TOKEN" not in call["env"]
    assert call["stdin"] is subprocess.DEVNULL
    assert (directory / "claude-activity.json").read_text() == activity
    assert (
        subprocess.check_output(
            ["git", "-C", str(worktree), "status", "--porcelain"]
        )
        == b""
    )
    assert (
        subprocess.check_output(
            ["git", "-C", str(worktree), "rev-parse", "HEAD"]
        )
        == head
    )
    [entry] = (directory / "claude-asks.jsonl").read_text().splitlines()
    assert json.loads(entry)["mode"] == "fork"


def test_checkpoint_skips_the_fork_and_carries_the_lane_state(
    bridge, repo, ran
):
    lane(bridge, repo, "claude", "claude")
    bridge.ask("claude", repo, "Status?", checkpoint=True)
    command = ran[0]["command"]
    assert "--resume" not in command
    assert "Open claims: none." in command[-1]
    assert "Parser." in command[-1]


def test_a_codex_lane_answers_from_its_checkpoint_in_a_read_only_sandbox(
    bridge, repo, ran
):
    _, directory = lane(bridge, repo, "codex", "codex")
    bridge.ask("codex", repo, "Status?")
    command = ran[0]["command"]
    assert command[:4] == ["/bin/codex", "exec", "--sandbox", "read-only"]
    assert "resume" not in command
    assert "Last report:" in command[-1]
    assert (budgets.ask_directory(directory, "codex") / ".git").is_dir()
    assert budgets.asked(directory, "codex")["count"] == 1


@pytest.mark.parametrize(
    ("adapter", "expected"),
    [
        ("copilot", ["--deny-tool", "write", "--deny-tool", "shell"]),
        ("gemini", ["--approval-mode", "default"]),
        ("opencode", ["run", "--agent", "plan"]),
    ],
)
def test_each_adapter_is_held_read_only_by_its_own_client(adapter, expected):
    command = ask_command(adapter, adapter, "q", "/lane", SESSION)
    assert " ".join(expected) in " ".join(command)
    assert SESSION not in command
    assert not any(
        word in part
        for part in command
        for word in ("dangerously", "yolo", "allow-all")
    )


def test_an_adapter_without_a_read_only_mode_is_refused():
    with pytest.raises(BridgeError, match="no native read-only mode"):
        ask_command("amp", "amp", "q", "/lane")


def test_an_unknown_lane_is_refused(bridge, repo, ran):
    lane(bridge, repo, "claude", "claude")
    with pytest.raises(BridgeError, match="No active participant"):
        bridge.ask("nobody", repo, "Status?")
    assert ran == []


def test_side_question_tokens_count_toward_the_lane_budget(bridge, repo):
    _, directory = lane(bridge, repo, "codex", "codex")
    budgets.record_ask(directory, "codex", {"mode": "fork", "tokens": 40})
    budgets.record_ask(directory, "codex", {"mode": "fork", "tokens": None})
    manifest = cli.roster.read(directory)
    used = budgets.consumption(
        bridge.home, directory, manifest, "codex", {}, {}, {"tokens"}
    )
    assert used["tokens"] >= 40
    assert budgets.asked(directory, "codex") == {"count": 2, "tokens": 40}


def test_side_questions_are_exported_as_lane_metrics():
    view = {
        "projects": [
            {
                "root": "/repo",
                "rows": [
                    {
                        "participant": "codex",
                        "provider_name": "codex",
                        "side_questions": 2,
                        "side_question_tokens": 40,
                    }
                ],
            }
        ]
    }
    text = views.exposition(view)
    assert (
        'agent_parley_lane_side_questions_retained{project="/repo",'
        'participant="codex",provider="codex"} 2'
    ) in text
    assert "agent_parley_lane_side_question_tokens_retained{" in text
