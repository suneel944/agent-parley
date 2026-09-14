"""Checks the advisory token, call and hour budget recorded per lane."""

import json
import os
import re
import sys
import time
from pathlib import Path

import pytest

from agent_parley import budgets, cli, dashboard, roster, store
from agent_parley.checkpoints import checkpoint
from agent_parley.process import start_ticks
from agent_parley.state import BridgeError, write_json


def usage_record(identifier, tokens):
    """Builds one assistant transcript record reporting its own usage."""
    return json.dumps(
        {
            "type": "assistant",
            "message": {
                "id": identifier,
                "usage": {
                    "input_tokens": tokens,
                    "cache_creation_input_tokens": tokens * 2,
                    "cache_read_input_tokens": tokens * 3,
                    "output_tokens": tokens * 4,
                },
            },
        }
    )


def claude_transcript(config, lane, lines):
    """Writes a Claude transcript where that client would keep one."""
    directory = (
        Path(config) / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(lane))
    )
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "session.jsonl"
    path.write_text("\n".join(lines) + "\n")
    return path


def registered(bridge, paired):
    """Registers both lanes so mail and served calls are readable."""
    store.initialize(bridge.home)
    tokens = {}
    for name in ("claude", "codex"):
        tokens[name] = store.register(bridge.home, paired["root"], name)[
            "registration_token"
        ]
    return tokens


def alive(directory, name, **extra):
    """Publishes a live session for one lane."""
    write_json(
        directory / f"{name}-activity.json",
        {
            "session_pid": os.getpid(),
            "session_ticks": start_ticks(os.getpid()),
            "activity": "idle",
            "updated": time.time(),
            **extra,
        },
    )


def recorded_tokens(tmp_path, paired, name, tokens):
    """Writes a transcript where the lane's client counts that many tokens."""
    claude_transcript(
        tmp_path / "claude_config_dir",
        paired["lanes"][name],
        [usage_record("msg_a", tokens // 10)],
    )


def rows_of(view):
    """Indexes one project's rendered rows by participant."""
    return {row["participant"]: row for row in view["projects"][0]["rows"]}


def test_limits_inherit_participant_over_provider_over_project(
    bridge, repo, paired
):
    directory = bridge.project(repo)[1]
    assert budgets.limits(bridge.home, roster.read(directory), "claude") == {}
    bridge.budget(repo, "project", "", {"tokens": 1000, "calls": 50})
    bridge.budget(repo, "provider", "claude", {"calls": 5, "hours": 8})
    bridge.budget(repo, "participant", "claude", {"hours": 1.5})
    data = roster.read(directory)
    assert data["budget"] == {"tokens": 1000, "calls": 50}
    assert roster.provider(bridge.home, "claude")["budget"] == {
        "calls": 5,
        "hours": 8,
    }
    assert data["participants"]["claude"]["budget"] == {"hours": 1.5}
    assert budgets.limits(bridge.home, data, "claude") == {
        "tokens": 1000,
        "calls": 5,
        "hours": 1.5,
    }
    assert budgets.limits(bridge.home, data, "codex") == {
        "tokens": 1000,
        "calls": 50,
    }
    bridge.budget(repo, "participant", "claude", {"hours": 0})
    bridge.budget(repo, "provider", "claude", {"calls": 0})
    bridge.budget(repo, "project", "", {"tokens": 0})
    data = roster.read(directory)
    assert data["participants"]["claude"]["budget"] == {}
    assert budgets.limits(bridge.home, data, "claude") == {
        "calls": 50,
        "hours": 8,
    }
    account = bridge.budget(repo, "participant", "claude")
    assert "records no budget of its own" in account
    assert "informs and does not gate" in account


@pytest.mark.parametrize(
    "changes", [{"tokens": -1}, {"calls": 2.5}, {"hours": "8"}, {"spend": 1}]
)
def test_an_unusable_limit_is_refused(bridge, repo, paired, changes):
    with pytest.raises(BridgeError, match="budget"):
        bridge.budget(repo, "participant", "claude", changes)


def test_a_lane_without_a_limit_is_never_marked(bridge, repo, paired, tmp_path):
    directory = bridge.project(repo)[1]
    recorded_tokens(tmp_path, paired, "claude", 5000)
    row = rows_of(dashboard.collect(bridge.home, False, {}))["claude"]
    assert row["over_budget"] is False
    assert row["budget_marker"] == ""
    reading = budgets.report(bridge.home, directory, paired, "claude", {})
    assert reading["crossed"] == []
    assert reading["used"]["tokens"] is None


def test_a_crossed_token_limit_marks_top_and_status(
    bridge, repo, paired, tmp_path, monkeypatch, capsys
):
    recorded_tokens(tmp_path, paired, "claude", 1500)
    bridge.budget(repo, "participant", "claude", {"tokens": 1000})
    bridge.budget(repo, "participant", "codex", {"tokens": 1000})
    view = dashboard.collect(bridge.home, False, {})
    rows = rows_of(view)
    assert rows["claude"]["over_budget"] is True
    assert rows["claude"]["budget"]["share"] == {"tokens": 150}
    assert rows["claude"]["budget_marker"] == (
        "over budget; tokens 1,500 of 1,000 (150%)!"
    )
    assert rows["codex"]["over_budget"] is False
    assert rows["codex"]["budget_marker"] == "budget; tokens ? of 1,000"
    assert any("over budget; tokens" in line for line in dashboard.render(view))
    bridge.status(cli.Selection(participant="claude"))
    assert "over budget; tokens 1,500 of 1,000" in capsys.readouterr().out
    monkeypatch.setattr(
        sys,
        "argv",
        ["agent-parley", "--home", str(bridge.home), "top", "--json"],
    )
    assert cli.main() == 0
    reported = json.loads(capsys.readouterr().out)["projects"][0][
        "participants"
    ]
    assert [row["over_budget"] for row in reported] == [True, False]
    assert reported[0]["budget"]["crossed"] == ["tokens"]


def test_served_calls_are_counted_against_the_call_limit(bridge, repo, paired):
    directory = bridge.project(repo)[1]
    tokens = registered(bridge, paired)
    actor = store.authenticate(bridge.home, tokens["claude"])
    for _ in range(3):
        store.call(bridge.home, actor, "list_participants", {})
    bridge.budget(repo, "project", "", {"calls": 2})
    data = roster.read(directory)
    reading = budgets.standing(bridge.home, directory, data, "claude")
    assert reading["used"]["calls"] == 3
    assert reading["crossed"] == ["calls"]
    assert budgets.marker(reading) == "over budget; calls 3 of 2 (150%)!"
    peer = budgets.standing(bridge.home, directory, data, "codex")
    assert peer["crossed"] == []
    assert budgets.marker(peer) == "budget; calls 0 of 2 (0%)"


def test_session_hours_are_counted_against_the_hour_limit(bridge, repo, paired):
    directory = bridge.project(repo)[1]
    alive(directory, "claude", session_started=time.time() - 7200)
    bridge.budget(repo, "participant", "claude", {"hours": 1})
    bridge.budget(repo, "participant", "codex", {"hours": 1})
    data = roster.read(directory)
    reading = budgets.report(bridge.home, directory, data, "claude", {})
    assert reading["used"]["hours"] == 2.0
    assert reading["crossed"] == ["hours"]
    assert budgets.marker(reading) == "over budget; hours 2h of 1h (200%)!"
    stopped = budgets.report(bridge.home, directory, data, "codex", {})
    assert stopped["used"]["hours"] == 0.0
    assert stopped["crossed"] == []


def test_the_lane_receives_one_notice_per_crossing(
    bridge, repo, paired, tmp_path
):
    registered(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    directory = lane.parent
    alive(directory, "claude")
    write_json(directory / "claude-identity.json", {"name": "claude"})
    recorded_tokens(tmp_path, paired, "claude", 1500)
    bridge.budget(repo, "participant", "claude", {"tokens": 1000})
    prompt = {
        "hook_event_name": "PreToolUse",
        "session_id": "claude-budget",
        "cwd": str(lane),
        "tool_name": "Read",
        "tool_input": {"file_path": str(lane / "shared.txt")},
    }
    first = checkpoint(bridge.home, directory, "claude", prompt)
    details = first["hookSpecificOutput"]
    assert "Budget notice" in details["additionalContext"]
    assert "tokens 1,500 of 1,000" in details["additionalContext"]
    assert "Nothing is stopped" in details["additionalContext"]
    assert "permissionDecision" not in details
    assert len(details["additionalContext"].encode()) <= 1536
    assert checkpoint(bridge.home, directory, "claude", prompt) == {}
    state = json.loads((directory / "claude-activity.json").read_text())
    assert state["budget_notified"] == ["tokens"]
    bridge.budget(repo, "participant", "claude", {"tokens": 2000})
    assert checkpoint(bridge.home, directory, "claude", prompt) == {}
    state = json.loads((directory / "claude-activity.json").read_text())
    assert state["budget_notified"] == []
    bridge.budget(repo, "participant", "claude", {"tokens": 1000})
    again = checkpoint(bridge.home, directory, "claude", prompt)
    assert "Budget notice" in again["hookSpecificOutput"]["additionalContext"]


def test_over_budget_selects_only_the_crossed_lanes(
    bridge, repo, paired, tmp_path, monkeypatch, capsys
):
    directory = bridge.project(repo)[1]
    recorded_tokens(tmp_path, paired, "claude", 1500)
    bridge.budget(repo, "project", "", {"tokens": 1000})
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agent-parley",
            "--home",
            str(bridge.home),
            "status",
            "--over-budget",
            "--json",
        ],
    )
    assert cli.main() == 1
    reported = json.loads(capsys.readouterr().out)["projects"][0][
        "participants"
    ]
    assert [row["participant"] for row in reported] == ["claude"]
    assert reported[0]["budget"]["marker"].startswith("over budget; tokens")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agent-parley",
            "--home",
            str(bridge.home),
            "participant",
            "pause",
            "--over-budget",
            "--yes",
            "--repo",
            str(repo),
        ],
    )
    assert cli.main() == 0
    participants = roster.read(directory)["participants"]
    assert participants["claude"].get("paused") is True
    assert participants["codex"].get("paused", False) is False
    assert cli.Selection(over_budget=True).describe() == "--over-budget"
