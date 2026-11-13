"""Checks the one bucket function every view and notification shares."""

import pytest

from agent_parley import dashboard, decisions, notify, status, webview
from agent_parley.status import (
    DONE,
    IDLE,
    NEEDS_INPUT,
    WORKING,
    bucket_cell,
    bucket_lines,
    bucket_order,
    lane_bucket,
)

NOW = 1_000.0


def blocked(cause: str, inferred: bool = False, since: float = 820.0):
    """Builds a lane blocked on one cause, as the lane state record says."""
    return {
        "condition": {"state": "blocked", "cause": cause, "since": since},
        "provenance": {"inferred": inferred},
    }


def state(name: str) -> dict:
    """Builds a lane whose recorded state is one name."""
    return {"condition": {"state": name, "cause": "", "since": 900.0}}


@pytest.mark.parametrize(
    ("record", "waits", "seconds"),
    [
        (blocked("approval"), ["permission prompt"], 180),
        (blocked("prompt"), ["question"], 180),
        (blocked("dialog"), ["native dialog"], 180),
        (
            {
                **state("idle"),
                "awaiting_approval": True,
                "report_age_seconds": 60,
            },
            ["approval"],
            60,
        ),
        (
            {**state("working"), "decisions": [{"created": 940.0}]},
            ["decision"],
            60,
        ),
        (
            {
                **state("idle"),
                "outcome": "blocked",
                "claims": [{"issue": 7, "ended": False}],
                "report_age_seconds": 30,
            },
            ["blocked claim"],
            30,
        ),
        ({"availability": {"state": "waiting"}}, ["question"], None),
    ],
)
def test_each_source_of_needed_input_puts_the_lane_first(
    record, waits, seconds
):
    bucket = lane_bucket(record, NOW)
    assert bucket["bucket"] == NEEDS_INPUT
    assert bucket["waits"] == waits
    assert bucket["seconds"] == seconds


def test_every_wait_is_named_and_aged_from_the_oldest():
    record = {
        **blocked("approval", since=990.0),
        "awaiting_approval": True,
        "report_age_seconds": 10,
        "decisions": [{"created": 700.0}, {"created": 950.0}],
    }
    bucket = lane_bucket(record, NOW)
    assert bucket["waits"] == ["permission prompt", "approval", "2 decisions"]
    assert bucket["seconds"] == 300
    assert bucket_cell(bucket) == (
        "needs input: permission prompt, approval, 2 decisions 5m"
    )


def test_an_inferred_block_stays_marked_inferred():
    bucket = lane_bucket(blocked("prompt", inferred=True), NOW)
    assert bucket["inferred"] is True
    assert bucket_cell(bucket).endswith("(inferred)")
    recorded = lane_bucket(blocked("prompt"), NOW)
    assert recorded["inferred"] is False
    assert "inferred" not in bucket_cell(recorded)
    assert lane_bucket({"availability": {"state": "waiting"}}, NOW)["inferred"]


def test_an_inferred_working_lane_with_a_decision_is_not_marked_inferred():
    record = {
        **state("working"),
        "provenance": {"inferred": True},
        "decisions": [{"created": 990.0}],
    }
    assert lane_bucket(record, NOW)["inferred"] is False


@pytest.mark.parametrize(
    ("record", "bucket"),
    [
        (state("working"), WORKING),
        (state("starting"), WORKING),
        (state("idle"), IDLE),
        (state("stopped"), IDLE),
        (state("dead"), IDLE),
        (blocked("capacity"), IDLE),
        ({**state("working"), "paused": True}, IDLE),
        ({**state("idle"), "retired_at": "2026-10-16"}, IDLE),
        (
            {**state("idle"), "claims": [{"issue": 7, "ended": True}]},
            DONE,
        ),
        ({**state("stopped"), "pull_requests_merged": 1}, DONE),
        ({**state("idle"), "outcome": "ready"}, DONE),
        ({}, IDLE),
    ],
)
def test_a_lane_without_a_wait_is_working_idle_or_done(record, bucket):
    assert lane_bucket(record, NOW)["bucket"] == bucket
    assert lane_bucket(record, NOW)["waits"] == []


def test_only_a_state_change_moves_a_lane_between_buckets():
    record = state("working")
    assert {
        lane_bucket(record, moment)["bucket"]
        for moment in (NOW, NOW + 3_600, NOW + 86_400)
    } == {WORKING}
    waiting = blocked("approval")
    assert {
        lane_bucket(waiting, moment)["bucket"] for moment in (NOW, NOW + 86_400)
    } == {NEEDS_INPUT}
    assert lane_bucket(state("idle"), NOW)["bucket"] == IDLE


def test_ordering_groups_buckets_and_keeps_the_order_inside_each():
    records = [
        {"participant": "a", **state("idle")},
        {"participant": "b", **state("working")},
        {"participant": "c", **blocked("approval")},
        {"participant": "d", **state("idle"), "outcome": "ready"},
        {"participant": "e", **state("working")},
        {"participant": "f", **blocked("prompt")},
    ]
    ordered = bucket_order(records)
    assert [record["participant"] for record in ordered] == [
        "c",
        "f",
        "b",
        "e",
        "a",
        "d",
    ]
    assert bucket_order(ordered) == ordered


def test_status_lines_carry_one_heading_per_bucket():
    records = bucket_order(
        [
            {"participant": "claude", **state("working")},
            {
                "participant": "codex",
                **blocked("approval"),
                "bucket": lane_bucket(blocked("approval"), NOW),
            },
        ]
    )
    lines = bucket_lines(
        ["PARTICIPANT", "codex row", "claude row", "Hidden columns: x"],
        records,
    )
    assert lines == [
        "PARTICIPANT",
        "Needs input (1): codex permission prompt 3m",
        "codex row",
        "Working (1):",
        "claude row",
        "Hidden columns: x",
    ]


def open_decision(directory, lane="claude", key="k"):
    """Opens one decision for a lane, as a notification does."""
    return decisions.open_or_refresh(
        directory,
        project="/repo",
        lane=lane,
        kind="handoff_offered",
        key=key,
        question="A handoff offer is waiting",
        options=("accept", "decline"),
    )


def test_an_open_decision_puts_the_lane_first_in_every_view(
    bridge, repo, paired, capsys
):
    directory = bridge.project(repo)[1]
    open_decision(directory)
    reading = {
        record["participant"]: record
        for record in bridge.status_snapshot()["projects"][0]["participants"]
    }
    assert reading["claude"]["bucket"]["bucket"] == NEEDS_INPUT
    assert reading["claude"]["bucket"]["waits"] == ["decision"]
    assert reading["codex"]["bucket"]["bucket"] == IDLE
    bridge.status()
    lines = capsys.readouterr().out.splitlines()
    heading = next(line for line in lines if line.startswith("PARTICIPANT"))
    after = lines[lines.index(heading) + 1 :]
    assert after[0].startswith("Needs input (1): claude decision")
    assert after[1].startswith("claude")
    assert after[2] == "Idle (1):"
    assert after[3].startswith("codex")
    view = dashboard.collect(bridge.home, False, {})
    rows = dashboard.select(view)["projects"][0]["rows"]
    assert [row["participant"] for row in rows] == ["claude", "codex"]
    assert dashboard.row_cells(rows[0])[1].startswith("needs input: decision")
    page = webview.page(view, 5)
    assert page.index("Needs input (1)</th>") < page.index("Idle (1)</th>")


def test_a_notification_says_the_bucket_through_the_shared_function(
    tmp_path,
):
    event = next(
        event for event in notify.Event if event.value != "handoff_offered"
    )
    assert notify._bucket(tmp_path, "claude", event).startswith(
        "needs input: decision"
    )
    open_decision(tmp_path, key="older")
    line = notify._bucket(tmp_path, "claude", event)
    assert line.startswith("needs input: 2 decisions")
    _, body = notify.compose(event.value, {"lane": "claude", "bucket": line})
    assert f"bucket: {line}" in body.splitlines()
    assert status.lane_bucket({"decisions": []})["bucket"] == IDLE
