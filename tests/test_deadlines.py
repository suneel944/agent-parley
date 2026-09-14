"""Checks recorded deadlines, attempt budgets and their visible states."""

import json
import sys
import time

import pytest

from agent_parley import cli, dashboard, issues, roster, store, supervision
from agent_parley.state import BridgeError, write_json


def ledger(directory):
    """Returns the published issue ledger."""
    return issues.snapshot(directory)["issues"]


def age_claim(directory, number, seconds):
    """Moves one claim's recorded deadline into the past."""
    state = issues.snapshot(directory)
    state["issues"][number]["deadline"] = time.time() - seconds
    state["revision"] += 1
    write_json(directory / "issues.json", state)


def test_a_claim_records_the_window_it_was_given(bridge, repo, paired):
    directory = bridge.project(repo)[1]
    lane = paired["lanes"]["claude"]
    before = time.time()
    bridge.issue(lane, "claim", "42", within=7200)
    record = ledger(directory)["42"]
    assert before + 7100 < record["deadline"] < before + 7300
    assert record["attempts"] == 0
    timing = issues.deadline_state(record)
    assert timing["overdue"] is False


def test_a_project_default_is_inherited_without_a_flag(bridge, repo, paired):
    directory = bridge.project(repo)[1]
    bridge.budgets(repo, {"claim": 3600, "offer": 1800, "attempts": 2})
    bridge.issue(paired["lanes"]["claude"], "claim", "42")
    record = ledger(directory)["42"]
    assert record["deadline"] is not None
    assert record["budget"] == 2
    assert "attempt budget 2" in bridge.budgets(repo)


def test_an_overdue_claim_is_visible_and_still_owned(bridge, repo, paired):
    directory = bridge.project(repo)[1]
    lane = paired["lanes"]["claude"]
    bridge.issue(lane, "claim", "42", within=60)
    age_claim(directory, "42", 300)
    record = ledger(directory)["42"]
    timing = issues.deadline_state(record)
    assert timing["overdue"] is True
    assert timing["overdue_seconds"] >= 300
    assert record["owner"] == "claude"
    listing = issues.describe(issues.snapshot(directory))
    assert "overdue" in listing and "still owned" in listing
    rows = {
        row["participant"]: row
        for row in dashboard.collect(bridge.home, False, {})["projects"][0][
            "rows"
        ]
    }
    assert rows["claude"]["issues"] == "#42!"
    assert rows["claude"]["overdue"] == ["42"]


def test_a_blocked_report_spends_one_attempt(bridge, repo, paired):
    directory = bridge.project(repo)[1]
    lane = paired["lanes"]["claude"]
    bridge.budgets(repo, {"attempts": 2})
    bridge.issue(lane, "claim", "42")
    bridge.report(lane, "blocked", "Waiting on the API", "Needs a decision", "")
    assert ledger(directory)["42"]["attempts"] == 1
    bridge.report(lane, "blocked", "Still waiting", "Needs a decision", "")
    bridge.report(lane, "blocked", "Still waiting", "Needs a decision", "")
    timing = issues.deadline_state(ledger(directory)["42"])
    assert timing["attempts"] == 3
    assert timing["budget_exceeded"] is True
    assert ledger(directory)["42"]["owner"] == "claude"


def test_a_released_claim_keeps_no_deadline(bridge, repo, paired):
    directory = bridge.project(repo)[1]
    lane = paired["lanes"]["claude"]
    bridge.issue(lane, "claim", "42", within=60)
    bridge.issue(lane, "release", "42")
    assert ledger(directory)["42"]["deadline"] is None


def test_an_offer_records_and_reports_its_deadline(bridge, repo, paired):
    directory = bridge.project(repo)[1]
    lane = paired["lanes"]["claude"]
    bridge.issue(lane, "claim", "42")
    bridge.issue(
        lane, "offer", "42", to="codex", summary="commit, checks", within=60
    )
    record = ledger(directory)["42"]
    assert record["offer"]["deadline"] is not None
    record["offer"]["deadline"] = time.time() - 120
    assert issues.offer_state(record["offer"])["overdue_seconds"] >= 120
    accepted = bridge.issue(
        paired["lanes"]["codex"],
        "accept",
        "42",
        offer_id=record["offer"]["id"],
    )
    assert accepted["owner"] == "codex"
    assert accepted["attempts"] == 0


def test_an_acknowledgement_deadline_is_recorded_and_reported(
    bridge, repo, paired
):
    store.initialize(bridge.home)
    store.register(bridge.home, paired["root"], "claude")
    delivered = bridge.say(repo, "claude", "Answer this", ack=True, within=60)
    with store.connect(bridge.home) as db:
        row = db.execute(
            "SELECT ack_deadline_ts FROM messages WHERE id=?",
            (delivered["id"],),
        ).fetchone()
    assert row["ack_deadline_ts"] is not None
    plain = bridge.say(repo, "claude", "No deadline here")
    with store.connect(bridge.home) as db:
        row = db.execute(
            "SELECT ack_deadline_ts FROM messages WHERE id=?", (plain["id"],)
        ).fetchone()
    assert row["ack_deadline_ts"] is None


def test_the_runtime_records_one_notice_per_breach(bridge, repo, paired):
    directory = bridge.project(repo)[1]
    lane = paired["lanes"]["claude"]
    bridge.issue(lane, "claim", "42", within=60)
    bridge.issue(paired["lanes"]["codex"], "claim", "43")
    bridge.issue(paired["lanes"]["codex"], "block", "43", on="42")
    age_claim(directory, "42", 300)
    supervision.deadline_notices(directory, paired)
    notice = ledger(directory)["42"]["deadline_notice"]
    assert notice["holder"] == "claude"
    assert notice["waiting"] == ["codex"]
    assert "still owns it" in notice["text"]
    revision = issues.snapshot(directory)["revision"]
    supervision.deadline_notices(directory, paired)
    assert issues.snapshot(directory)["revision"] == revision


def test_defaults_are_validated_and_reported_as_json(
    bridge, repo, monkeypatch, capsys
):
    bridge.setup(repo)
    assert "records no deadline defaults" in bridge.budgets(repo)
    with pytest.raises(BridgeError, match="attempt budget"):
        roster.deadlines({"attempts": 0})
    with pytest.raises(BridgeError, match="claim deadline"):
        roster.deadlines({"claim": 0})
    with pytest.raises(BridgeError, match="Deadline defaults accept only"):
        roster.deadlines({"unknown": 1})
    bridge.budgets(repo, {"claim": 3600})
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agent-parley",
            "--home",
            str(bridge.home),
            "deadlines",
            "show",
            "--repo",
            str(repo),
            "--json",
        ],
    )
    assert cli.main() == 0
    document = json.loads(capsys.readouterr().out)
    assert document["kind"] == "deadlines"
    assert document["deadlines"] == {"claim": 3600}


def test_status_and_issue_list_report_the_state(
    bridge, repo, paired, monkeypatch, capsys
):
    directory = bridge.project(repo)[1]
    lane = paired["lanes"]["claude"]
    bridge.budgets(repo, {"attempts": 1})
    bridge.issue(lane, "claim", "42", within=60)
    bridge.report(lane, "blocked", "Waiting", "Needs a decision", "")
    bridge.report(lane, "blocked", "Waiting", "Needs a decision", "")
    age_claim(directory, "42", 300)
    bridge.status()
    printed = capsys.readouterr().out
    assert "is overdue by" in printed and "still owned" in printed
    assert "attempts 2/1" in printed
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agent-parley",
            "--home",
            str(bridge.home),
            "issue",
            "list",
            "--repo",
            str(repo),
            "--json",
        ],
    )
    assert cli.main() == 0
    document = json.loads(capsys.readouterr().out)
    reported = document["issues"][0]
    assert reported["overdue"] is True
    assert reported["attempts"] == 2
    assert reported["attempt_budget"] == 1
    assert reported["budget_exceeded"] is True
    assert reported["deadline_at"].endswith("Z")
