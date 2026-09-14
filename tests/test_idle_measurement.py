"""Checks measured idle time and the waits each pending item accumulates."""

import json
import os
import time

from agent_parley import cli, dashboard, metrics, store
from agent_parley.process import start_ticks
from agent_parley.state import write_json


def log_events(directory, name, *entries):
    """Writes a synthetic hook event log for one lane."""
    (directory / f"{name}-events.jsonl").write_text(
        "".join(json.dumps(entry) + "\n" for entry in entries),
        encoding="utf-8",
    )


def alive(directory, name):
    """Records a live session process for one lane."""
    write_json(
        directory / f"{name}-activity.json",
        {
            "session_pid": os.getpid(),
            "session_ticks": start_ticks(os.getpid()),
            "activity": "idle",
            "updated": time.time(),
        },
    )


def test_idle_runs_from_a_turn_end_to_the_next_activity(bridge, repo, paired):
    directory = bridge.project(repo)[1]
    now = time.time()
    log_events(
        directory,
        "claude",
        {"ts": now - 900, "event": "UserPromptSubmit"},
        {"ts": now - 880, "event": "Stop"},
        {"ts": now - 580, "event": "UserPromptSubmit"},
        {"ts": now - 560, "event": "PostToolUse"},
    )
    measured = metrics.idle_intervals(directory, "claude", now=now)
    assert [interval["seconds"] for interval in measured["intervals"]] == [300]
    assert measured["seconds"] == 300
    assert measured["complete"] is True


def test_an_open_interval_counts_only_while_the_session_is_alive(
    bridge, repo, paired
):
    directory = bridge.project(repo)[1]
    now = time.time()
    log_events(directory, "claude", {"ts": now - 120, "event": "Stop"})
    stopped = metrics.idle_intervals(directory, "claude", now=now)
    assert stopped["intervals"] == []
    alive(directory, "claude")
    running = metrics.idle_intervals(directory, "claude", now=now)
    assert running["intervals"][0]["open"] is True
    assert running["seconds"] == 120


def test_a_window_reaching_past_retention_reports_incomplete(
    bridge, repo, paired
):
    directory = bridge.project(repo)[1]
    now = time.time()
    log_events(
        directory,
        "claude",
        {"ts": now - 60, "event": "Stop"},
        {"ts": now - 30, "event": "UserPromptSubmit"},
    )
    measured = metrics.idle_intervals(
        directory, "claude", since=now - 3600, now=now
    )
    assert measured["seconds"] == 30
    assert measured["complete"] is False
    assert metrics.idle_intervals(directory, "codex", now=now) == {
        "intervals": [],
        "seconds": 0,
        "complete": False,
    }


def test_mail_waits_report_read_and_acknowledgement_separately(
    bridge, repo, paired
):
    directory = bridge.project(repo)[1]
    store.initialize(bridge.home)
    token = store.register(bridge.home, paired["root"], "claude")[
        "registration_token"
    ]
    delivered = bridge.say(repo, "claude", "Answer this", ack=True)
    with store.connect(bridge.home, write=True) as db:
        db.execute(
            "UPDATE messages SET created_ts=datetime('now','-600 seconds') "
            "WHERE id=?",
            (delivered["id"],),
        )
    reported = metrics.waits(bridge.home, directory, paired, "claude")
    kinds = {wait["kind"]: wait for wait in reported}
    assert kinds["message_read"]["complete"] is False
    assert kinds["message_read"]["seconds"] >= 600
    assert kinds["acknowledgement"]["complete"] is False
    actor = store.authenticate(bridge.home, token)
    assert actor is not None
    store.call(
        bridge.home,
        actor,
        "acknowledge_message",
        {"message_id": delivered["id"]},
    )
    answered = {
        wait["kind"]: wait
        for wait in metrics.waits(bridge.home, directory, paired, "claude")
    }
    assert answered["message_read"]["complete"] is True
    assert answered["acknowledgement"]["complete"] is True
    assert answered["acknowledgement"]["seconds"] >= 600


def test_a_handoff_offer_reports_the_seconds_it_waited(bridge, repo, paired):
    directory = bridge.project(repo)[1]
    lane = paired["lanes"]["claude"]
    bridge.issue(lane, "claim", "42")
    record = bridge.issue(
        lane, "offer", "42", to="codex", summary="commit, checks, remaining"
    )
    pending = metrics.pending(
        metrics.waits(bridge.home, directory, paired, "claude")
    )
    assert pending[0]["kind"] == "handoff_offer"
    assert pending[0]["issue"] == 42
    bridge.issue(
        paired["lanes"]["codex"],
        "accept",
        "42",
        offer_id=record["offer"]["id"],
    )
    answered = [
        wait
        for wait in metrics.waits(bridge.home, directory, paired, "claude")
        if wait["kind"] == "handoff_offer"
    ]
    assert answered[0]["complete"] is True
    assert answered[0]["answer"] == "accept"


def test_a_ready_report_waits_until_it_is_integrated(bridge, repo, paired):
    directory = bridge.project(repo)[1]
    lane = paired["lanes"]["claude"]
    bridge.report(lane, "ready", "Engine built", "", "12 tests passed")
    waiting = metrics.pending(
        metrics.waits(bridge.home, directory, paired, "claude")
    )
    assert [wait["kind"] for wait in waiting] == ["report_integration"]
    metrics.record_report(
        directory, "claude", {"kind": "integration", "action": "merge"}
    )
    integrated = [
        wait
        for wait in metrics.waits(bridge.home, directory, paired, "claude")
        if wait["kind"] == "report_integration"
    ]
    assert integrated[0]["complete"] is True
    assert integrated[0]["action"] == "merge"


def test_top_shows_the_column_and_the_project_total(bridge, repo, paired):
    directory = bridge.project(repo)[1]
    now = time.time()
    log_events(
        directory,
        "claude",
        {"ts": now - 900, "event": "Stop"},
        {"ts": now - 300, "event": "UserPromptSubmit"},
    )
    view = dashboard.collect(bridge.home, False, {})
    rows = {row["participant"]: row for row in view["projects"][0]["rows"]}
    assert rows["claude"]["idle_seconds"] == 600
    assert view["totals"]["idle"] == 600
    assert view["totals"]["idle_leader"] == "claude"
    lines = dashboard.render(view)
    assert any("IDLE" in line for line in lines)
    assert any("idle 10m (most claude 10m)" in line for line in lines)
    assert any("observed coordination inactivity" in line for line in lines)


def test_status_and_export_carry_the_figures(bridge, repo, paired, capsys):
    directory = bridge.project(repo)[1]
    now = time.time()
    log_events(
        directory,
        "claude",
        {"ts": now - 900, "event": "Stop"},
        {"ts": now - 300, "event": "UserPromptSubmit"},
    )
    bridge.report(
        paired["lanes"]["claude"], "ready", "Engine built", "", "checks pass"
    )
    bridge.status(cli.Selection(participant="claude"))
    printed = capsys.readouterr().out
    assert "Observed coordination inactivity: 600s" in printed
    assert "Waiting" in printed and "report_integration" in printed
    destination = bridge.home / "events.jsonl"
    bridge.export_events(repo, ("claude",), output=destination)
    exported = [
        json.loads(line) for line in destination.read_text().splitlines()
    ]
    kinds = {entry["record"] for entry in exported}
    assert {"event", "idle_interval", "wait"} <= kinds
    interval = next(
        entry for entry in exported if entry["record"] == "idle_interval"
    )
    assert interval["seconds"] == 600


def test_the_report_log_is_trimmed_once_it_passes_its_ceiling(
    bridge, repo, paired, monkeypatch
):
    directory = bridge.project(repo)[1]
    monkeypatch.setattr(metrics, "MAX_REPORT_RECORDS", 3)
    monkeypatch.setattr(metrics, "MAX_REPORT_LOG_BYTES", 400)
    for number in range(8):
        metrics.record_report(
            directory, "claude", {"kind": "report", "state": str(number)}
        )
    path = directory / "claude-reports.jsonl"
    assert path.stat().st_size < 400
    states = [
        record["state"]
        for record in metrics.report_records(directory, "claude")
    ]
    assert states == ["5", "6", "7"]
