"""Checks the capture and reading of each lane's own task plan."""

import json
import sys
from pathlib import Path

import pytest

from agent_parley import (
    checkpoints,
    cli,
    dashboard,
    lanes,
    roster,
    status,
    views,
    webview,
)
from agent_parley.state import BridgeError, write_json

TODOS = [
    {"content": "Read the hook", "status": "completed", "activeForm": "x"},
    {"content": "Write the\n tests", "status": "in_progress"},
    {"content": "Open the PR", "status": "pending"},
]


def event(paired, name, **fields):
    """Runs one hook event for a lane and returns its output."""
    directory = Path(paired["lanes"][name]).parent
    write_json(directory / f"{name}-identity.json", {"name": name})
    payload = {
        "session_id": "test",
        "cwd": paired["lanes"][name],
        **fields,
    }
    return checkpoints.checkpoint(
        paired["bridge"].home, directory, name, payload
    )


@pytest.fixture
def lanes_ready(bridge, paired):
    """Adds the bridge to the paired roster the helpers read."""
    return {**paired, "bridge": bridge}


def test_a_todo_write_hook_records_the_lanes_plan(lanes_ready):
    directory = Path(lanes_ready["lanes"]["claude"]).parent
    event(
        lanes_ready,
        "claude",
        hook_event_name="PostToolUse",
        tool_name="TodoWrite",
        tool_input={"todos": TODOS},
    )
    plan = lanes.lane_task_plan(directory, "claude")
    assert plan is not None
    assert plan["source"] == "TodoWrite"
    assert plan["truncated"] is False
    assert plan["items"] == [
        {"text": "Read the hook", "status": "completed"},
        {"text": "Write the tests", "status": "in_progress"},
        {"text": "Open the PR", "status": "pending"},
    ]
    assert lanes.lane_task_plan(directory, "codex") is None


def test_a_subagent_todo_list_and_other_tools_change_nothing(lanes_ready):
    directory = Path(lanes_ready["lanes"]["claude"]).parent
    event(
        lanes_ready,
        "claude",
        hook_event_name="PostToolUse",
        tool_name="TodoWrite",
        agent_id="helper",
        tool_input={"todos": TODOS},
    )
    event(
        lanes_ready,
        "claude",
        hook_event_name="PostToolUse",
        tool_name="Bash",
        tool_input={"command": "true"},
    )
    assert lanes.lane_task_plan(directory, "claude") is None


def test_a_foreign_session_todo_list_is_not_recorded(lanes_ready):
    directory = Path(lanes_ready["lanes"]["claude"]).parent
    write_json(directory / "claude-activity.json", {"session_id": "other"})
    event(
        lanes_ready,
        "claude",
        hook_event_name="PostToolUse",
        tool_name="TodoWrite",
        tool_input={"todos": TODOS},
    )
    assert lanes.lane_task_plan(directory, "claude") is None


def test_a_malformed_todo_list_never_fails_the_hook(lanes_ready):
    directory = Path(lanes_ready["lanes"]["claude"]).parent
    event(
        lanes_ready,
        "claude",
        hook_event_name="PostToolUse",
        tool_name="TodoWrite",
        tool_input={"todos": [{"content": "x", "status": "maybe"}]},
    )
    assert lanes.lane_task_plan(directory, "claude") is None


def test_the_command_replaces_the_calling_lanes_plan(bridge, paired):
    lane = Path(paired["lanes"]["codex"])
    recorded = bridge.task_plan(lane, ["pending:Ship it", "completed:Ran"])
    assert len(recorded["items"]) == 2
    plan = lanes.lane_task_plan(lane.parent, "codex")
    assert plan is not None
    assert plan["source"] == lanes.PLAN_COMMAND
    assert plan["items"][0] == {"text": "Ship it", "status": "pending"}
    with pytest.raises(BridgeError, match="Plan status"):
        bridge.task_plan(lane, ["done:Ship it"])
    assert lanes.lane_task_plan(lane.parent, "codex") == plan
    assert lanes.lane_task_plan(lane.parent, "claude") is None


def test_the_command_reads_the_list_as_json(bridge, paired):
    lane = Path(paired["lanes"]["codex"])
    document = json.dumps([{"step": "Ship it", "status": "in_progress"}])
    bridge.task_plan(lane, [], document)
    plan = lanes.lane_task_plan(lane.parent, "codex")
    assert plan is not None
    assert plan["items"] == [{"text": "Ship it", "status": "in_progress"}]
    with pytest.raises(BridgeError, match="not valid"):
        bridge.task_plan(lane, [], "{nope")


def test_the_command_wants_exactly_one_form(bridge, paired):
    lane = Path(paired["lanes"]["codex"])
    with pytest.raises(BridgeError, match="not both and not neither"):
        bridge.task_plan(lane, [])
    with pytest.raises(BridgeError, match="not both and not neither"):
        bridge.task_plan(lane, ["pending:x"], "[]")


def test_the_command_refuses_a_checkout_that_is_no_lane(bridge, paired):
    with pytest.raises(BridgeError):
        bridge.task_plan(Path(paired["root"]), ["pending:x"])


def test_the_cli_takes_steps_and_caps_the_list(
    bridge, paired, capsys, monkeypatch
):
    monkeypatch.setenv("AGENT_PARLEY_HOME", str(bridge.home))
    lane = Path(paired["lanes"]["codex"])
    steps = [f"pending:step {n}" for n in range(40)]
    arguments = ["agent-parley", "plan", "update", "--repo", str(lane), *steps]
    monkeypatch.setattr(sys, "argv", arguments)
    assert cli.main() == 0
    assert "clipped to the plan cap" in capsys.readouterr().out
    plan = lanes.lane_task_plan(lane.parent, "codex")
    assert plan is not None
    assert len(plan["items"]) == lanes.MAX_PLAN_ITEMS


def test_a_peers_plan_is_never_delivered_to_another_lane(bridge, lanes_ready):
    bridge.task_plan(
        Path(lanes_ready["lanes"]["codex"]), ["pending:SECRET-STEP-TEXT"]
    )
    told = event(
        lanes_ready, "claude", hook_event_name="UserPromptSubmit", prompt="go"
    )
    assert "SECRET-STEP-TEXT" not in json.dumps(told)


def test_the_plan_is_capped_by_items_and_bytes():
    many = [{"step": f"step {n}", "status": "pending"} for n in range(40)]
    items, truncated = lanes.plan_items(many, "step")
    assert len(items) == lanes.MAX_PLAN_ITEMS
    assert truncated is True
    wide = [{"step": "w" * 500, "status": "pending"}] * 30
    items, truncated = lanes.plan_items(wide, "step")
    assert truncated is True
    assert all(len(i["text"]) == lanes.MAX_STEP_CHARACTERS for i in items)
    assert len(json.dumps(items).encode()) <= lanes.MAX_PLAN_BYTES
    with pytest.raises(BridgeError):
        lanes.plan_items("not a list", "step")
    with pytest.raises(BridgeError):
        lanes.plan_items([{"step": " ", "status": "pending"}], "step")


def test_an_unreadable_plan_file_reads_as_none(tmp_path):
    (tmp_path / f"lane{lanes.PLAN_FILE}").write_text("{not json")
    assert lanes.lane_task_plan(tmp_path, "lane") is None
    write_json(tmp_path / f"lane{lanes.PLAN_FILE}", {"items": "x"})
    assert lanes.lane_task_plan(tmp_path, "lane") is None


def test_every_provider_contract_has_a_plan_path():
    assert lanes.plan_source("claude") == "TodoWrite"
    for adapter in roster.ADAPTERS:
        assert lanes.plan_source(adapter)
    assert lanes.plan_source("unknown") == ""


def test_lane_detail_checks_completed_steps():
    plan = {
        "items": [
            {"text": "Done", "status": "completed"},
            {"text": "Now", "status": "in_progress"},
            {"text": "Later", "status": "pending"},
        ],
        "source": "TodoWrite",
        "updated": 0.0,
        "truncated": True,
    }
    assert cli.plan_lines(plan) == [
        "    Task plan (from TodoWrite):",
        "      [x] Done",
        "      [>] Now",
        "      [ ] Later",
        "      (clipped to the plan cap)",
    ]
    assert cli.plan_lines(None) == []


PLAN_ENTRIES = [
    {"step": "Read the hook", "status": "completed"},
    {"step": "Write tests", "status": "in_progress"},
    {"step": "Open the PR", "status": "pending"},
]


def test_plan_reading_counts_steps_and_names_the_current_one(tmp_path):
    lanes.record_plan(tmp_path, "a", PLAN_ENTRIES, "step", lanes.PLAN_COMMAND)
    reading = status.plan_reading(tmp_path, tmp_path, "a", "codex")
    assert reading["done"] == 1
    assert reading["total"] == 3
    assert reading["current"] == "Write tests"
    assert status.plan_cell(reading) == "1/3 Write tests"
    finished = [{"step": "Only", "status": "completed"}]
    lanes.record_plan(tmp_path, "a", finished, "step", lanes.PLAN_COMMAND)
    reading = status.plan_reading(tmp_path, tmp_path, "a", "codex")
    assert status.plan_cell(reading) == "1/1 done"


def test_an_absent_plan_reads_as_a_dash_or_n_a(tmp_path):
    reading = status.plan_reading(tmp_path, tmp_path, "a", "codex")
    assert reading["recorded"] is False
    assert reading["source"] == lanes.PLAN_COMMAND
    assert status.plan_cell(reading) == "-"
    assert status.plan_cell({**reading, "source": ""}) == "n/a"


def test_every_view_shows_the_plan(bridge, repo, paired, capsys):
    directory = bridge.project(repo)[1]
    lanes.record_plan(directory, "claude", PLAN_ENTRIES, "step", "TodoWrite")
    reading = {
        record["participant"]: record
        for record in bridge.status_snapshot()["projects"][0]["participants"]
    }
    assert reading["claude"]["task_plan"]["done"] == 1
    assert reading["claude"]["task_plan"]["source"] == "TodoWrite"
    assert reading["codex"]["task_plan"]["recorded"] is False
    bridge.status()
    out = capsys.readouterr().out
    assert "Plan claude: 1/3 Write tests" in out
    assert "Plan codex" not in out
    view = dashboard.collect(bridge.home, False, {})
    rows = {
        row["participant"]: row
        for row in dashboard.select(view)["projects"][0]["rows"]
    }
    plan = [name for name, _ in dashboard.COLUMNS].index("PLAN")
    assert dashboard.row_cells(rows["claude"])[plan] == "1/3 Write tests"
    assert dashboard.row_cells(rows["codex"])[plan] == "-"
    assert "PLAN" not in dashboard.DEFAULT_COLUMNS
    assert "1/3 Write tests" in webview.page(view, 5)
    live = {
        row["participant"]: row
        for row in views.frame(view)["projects"][0]["participants"]
    }
    assert live["claude"]["task_plan"]["total"] == 3
    assert live["codex"]["task_plan"]["recorded"] is False
