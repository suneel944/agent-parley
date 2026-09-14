"""Checks the idle marker for a live lane that owes an answer."""

import json
import os
import sys

import pytest

from agent_parley import cli, dashboard, store, supervision
from agent_parley.process import start_ticks
from agent_parley.state import write_json


def alive(directory, name, **extra):
    """Records a live session process for one lane."""
    write_json(
        directory / f"{name}-activity.json",
        {
            "session_pid": os.getpid(),
            "session_ticks": start_ticks(os.getpid()),
            "activity": "idle",
            "updated": 0,
            **extra,
        },
    )


def deliver(bridge, repo, paired, *, ack=False, aged=0):
    """Delivers one operator message and optionally ages it in the store."""
    store.initialize(bridge.home)
    store.register(bridge.home, paired["root"], "claude")
    delivered = bridge.say(repo, "claude", "Answer this", ack=ack)
    if aged:
        with store.connect(bridge.home, write=True) as db:
            db.execute(
                "UPDATE messages SET created_ts=datetime('now',?) WHERE id=?",
                (f"-{aged} seconds", delivered["id"]),
            )
    return delivered


def idle_for(bridge, paired, directory, after=600):
    """Derives the stall report for the claude lane."""
    return supervision.stall(bridge.home, directory, paired, "claude", after)


def test_a_live_lane_holding_old_unread_mail_reads_as_idle(
    bridge, repo, paired
):
    directory = bridge.project(repo)[1]
    alive(directory, "claude")
    delivered = deliver(bridge, repo, paired, aged=1800)
    report = idle_for(bridge, paired, directory)
    assert report["stalled"] is True
    assert report["kind"] == "unread"
    assert report["message_id"] == delivered["id"]
    assert report["sender"] == "operator"
    assert report["age_seconds"] >= 1800
    marker = supervision.stall_marker(report)
    assert marker.startswith("idle; message")
    assert "waiting" in marker


def test_an_unacknowledged_message_is_named_as_the_waiting_item(
    bridge, repo, paired
):
    directory = bridge.project(repo)[1]
    alive(directory, "claude")
    deliver(bridge, repo, paired, ack=True, aged=1800)
    report = idle_for(bridge, paired, directory)
    assert report["stalled"] is True
    assert report["kind"] == "acknowledgement"
    assert "acknowledgement of message" in supervision.stall_marker(report)


def test_a_recent_message_or_a_recent_call_is_not_a_stall(bridge, repo, paired):
    directory = bridge.project(repo)[1]
    alive(directory, "claude")
    deliver(bridge, repo, paired)
    assert idle_for(bridge, paired, directory)["stalled"] is False
    deliver(bridge, repo, paired, aged=1800)
    assert idle_for(bridge, paired, directory, after=86400)["stalled"] is False


def test_a_served_call_inside_the_interval_clears_the_marker(
    bridge, repo, paired
):
    directory = bridge.project(repo)[1]
    alive(directory, "claude")
    deliver(bridge, repo, paired, aged=1800)
    token = store.register(bridge.home, paired["root"], "claude")[
        "registration_token"
    ]
    actor = store.authenticate(bridge.home, token)
    assert actor is not None
    store.call(bridge.home, actor, "list_participants", {})
    assert idle_for(bridge, paired, directory)["stalled"] is False


def test_a_stopped_lane_is_never_reported_as_idle(bridge, repo, paired):
    directory = bridge.project(repo)[1]
    deliver(bridge, repo, paired, aged=1800)
    write_json(
        directory / "claude-activity.json",
        {"session_pid": os.getpid(), "session_ticks": "0"},
    )
    assert idle_for(bridge, paired, directory)["stalled"] is False


def test_top_and_status_both_mark_the_lane(
    bridge, repo, paired, monkeypatch, capsys
):
    directory = bridge.project(repo)[1]
    alive(directory, "claude")
    deliver(bridge, repo, paired, aged=1800)
    view = dashboard.collect(bridge.home, False, {})
    rows = {row["participant"]: row for row in view["projects"][0]["rows"]}
    assert rows["claude"]["stalled"] is True
    assert rows["claude"]["state"].startswith("idle ")
    assert rows["codex"]["stalled"] is False
    assert any("idle; message" in line for line in dashboard.render(view))
    bridge.status(cli.Selection(participant="claude"))
    assert "idle; message" in capsys.readouterr().out
    monkeypatch.setattr(
        sys,
        "argv",
        ["agent-parley", "--home", str(bridge.home), "top", "--json"],
    )
    assert cli.main() == 0
    document = json.loads(capsys.readouterr().out)
    reported = document["projects"][0]["participants"]
    assert [row["stalled"] for row in reported] == [True, False]


def test_the_interval_is_a_project_setting(bridge, repo, paired):
    directory = bridge.project(repo)[1]
    alive(directory, "claude")
    deliver(bridge, repo, paired, aged=900)
    manifest = {**paired, "supervision": {"stalled_after": 1800}}
    assert (
        supervision.configuration(bridge.home, manifest)["stalled_after"]
        == 1800
    )
    assert supervision.DEFAULTS["stalled_after"] == 600
    assert idle_for(bridge, paired, directory, after=1800)["stalled"] is False


@pytest.mark.parametrize("value", [0, 86401, "600", True])
def test_an_invalid_interval_is_refused(value):
    with pytest.raises(Exception, match="stalled_after|boolean|between"):
        supervision.settings({"stalled_after": value})
