"""Checks recorded deadlines, attempt budgets and their visible states."""

import json
import os
import sys
import time

import pytest

from agent_parley import (
    checkpoints,
    cli,
    dashboard,
    issues,
    lifecycle,
    roster,
    store,
    supervision,
)
from agent_parley.process import start_ticks
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


def registered(bridge, paired, name):
    """Registers one lane with the store and authenticates it."""
    store.initialize(bridge.home)
    return store.authenticate(
        bridge.home,
        store.register(bridge.home, paired["root"], name)["registration_token"],
    )


def request_ack(bridge, actor, recipients, key, within=None):
    """Sends one acknowledgement request from a served lane."""
    arguments = {
        "to": recipients,
        "subject": "Confirm the schema change",
        "body_md": "Confirm the schema change",
        "idempotency_key": key,
        "ack_required": True,
    }
    if within is not None:
        arguments["ack_within"] = within
    return store.call(bridge.home, actor, "send_message", arguments)


def remaining(bridge, identifier):
    """Returns the seconds left on one message's acknowledgement deadline."""
    with store.connect(bridge.home) as db:
        row = db.execute(
            "SELECT CAST((julianday(ack_deadline_ts)-julianday('now'))*86400 "
            "AS INTEGER) AS window FROM messages WHERE id=?",
            (identifier,),
        ).fetchone()
    return None if row["window"] is None else int(row["window"])


def expire(bridge, identifier):
    """Moves one acknowledgement deadline into the past."""
    with store.connect(bridge.home, write=True) as db:
        db.execute(
            "UPDATE messages SET ack_deadline_ts=datetime('now','-90 seconds') "
            "WHERE id=?",
            (identifier,),
        )


def returned(bridge, name):
    """Returns the deadline notices one sender received."""
    with store.connect(bridge.home) as db:
        return [
            dict(row)
            for row in db.execute(
                "SELECT m.id,m.subject,m.body_md FROM messages m "
                "JOIN message_recipients r ON r.message_id=m.id "
                "JOIN agents a ON a.id=r.agent_id WHERE a.name=? AND "
                "m.subject LIKE 'Acknowledgement deadline passed%' "
                "ORDER BY m.id",
                (name,),
            )
        ]


def outstanding(bridge, paired, name):
    """Returns the acknowledgements one lane still owes."""
    return checkpoints.mailbox(bridge.home, paired["root"], name)[
        "outstanding_ack"
    ]


def bounced(bridge, name):
    """Returns the returned shares one sender received."""
    with store.connect(bridge.home) as db:
        return [
            dict(row)
            for row in db.execute(
                "SELECT m.id,m.subject,m.body_md FROM messages m "
                "JOIN message_recipients r ON r.message_id=m.id "
                "JOIN agents a ON a.id=r.agent_id WHERE a.name=? AND "
                "m.subject LIKE 'Share returned%' ORDER BY m.id",
                (name,),
            )
        ]


def alive(directory, name):
    """Records a live but long-quiet session process for one lane."""
    write_json(
        directory / f"{name}-activity.json",
        {
            "session_pid": os.getpid(),
            "session_ticks": start_ticks(os.getpid()),
            "activity": "idle",
            "updated": 0,
        },
    )


def readings(bridge, directory):
    """Returns the manifest and one presence reading per participant."""
    manifest = roster.read(directory)
    return manifest, {
        name: supervision.presence(directory, name)
        for name in manifest["participants"]
    }


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


def test_a_claim_reported_ready_is_never_overdue(bridge, repo, paired):
    directory = bridge.project(repo)[1]
    lane = paired["lanes"]["claude"]
    bridge.issue(lane, "claim", "42", within=60)
    age_claim(directory, "42", 300)
    lifecycle.record_report(
        directory, "claude", "ready", "abcdef1", "", issue="42"
    )
    record = ledger(directory)["42"]
    timing = issues.deadline_state(record)
    assert timing["overdue"] is False
    assert timing["overdue_seconds"] == 0
    assert record["owner"] == "claude"
    rows = {
        row["participant"]: row
        for row in dashboard.collect(bridge.home, False, {})["projects"][0][
            "rows"
        ]
    }
    assert rows["claude"]["issues"] == "#42"
    record["execution"]["claim_id"] = "an-earlier-generation"
    assert issues.deadline_state(record)["overdue"] is True


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


def test_a_request_without_a_window_takes_the_configured_default(
    bridge, repo, paired
):
    actor = registered(bridge, paired, "claude")
    registered(bridge, paired, "codex")
    inherited = request_ack(bridge, actor, ["codex"], "inherited")
    assert (
        store.DEFAULT_ACK_SECONDS - 10
        <= remaining(bridge, inherited["id"])
        <= store.DEFAULT_ACK_SECONDS
    )
    bridge.budgets(repo, {"ack": 900})
    configured = request_ack(bridge, actor, ["codex"], "configured")
    assert 890 <= remaining(bridge, configured["id"]) <= 900
    explicit = request_ack(bridge, actor, ["codex"], "explicit", within=60)
    assert 50 <= remaining(bridge, explicit["id"]) <= 60
    operator = bridge.say(repo, "claude", "Answer this", ack=True)
    assert remaining(bridge, operator["id"]) is not None


def test_a_missed_deadline_returns_to_the_sender_with_its_reason(
    bridge, repo, paired
):
    directory = bridge.project(repo)[1]
    actor = registered(bridge, paired, "claude")
    registered(bridge, paired, "codex")
    request = request_ack(bridge, actor, ["codex"], "missed", within=60)
    assert outstanding(bridge, paired, "codex")
    supervision.poll(bridge.home, directory)
    assert returned(bridge, "claude") == []
    expire(bridge, request["id"])
    supervision.poll(bridge.home, directory)
    [notice] = returned(bridge, "claude")
    assert f"message {request['id']}" in notice["subject"]
    assert "codex has no running session" in notice["body_md"]
    assert "retired" in notice["body_md"]
    assert outstanding(bridge, paired, "codex") == []
    supervision.poll(bridge.home, directory)
    assert len(returned(bridge, "claude")) == 1


def test_an_acknowledged_request_never_returns_to_its_sender(
    bridge, repo, paired
):
    directory = bridge.project(repo)[1]
    actor = registered(bridge, paired, "claude")
    recipient = registered(bridge, paired, "codex")
    request = request_ack(bridge, actor, ["codex"], "answered", within=60)
    store.call(
        bridge.home,
        recipient,
        "acknowledge_message",
        {"message_id": request["id"]},
    )
    expire(bridge, request["id"])
    supervision.poll(bridge.home, directory)
    assert returned(bridge, "claude") == []
    assert store.overdue_acknowledgements(bridge.home, paired["root"]) == []


def test_a_broadcast_returns_one_notice_naming_every_silent_lane(
    bridge, repo, paired
):
    bridge.add_participant(repo, "claude-1", "claude")
    directory = bridge.project(repo)[1]
    actor = registered(bridge, paired, "claude")
    registered(bridge, paired, "codex")
    registered(bridge, paired, "claude-1")
    request = request_ack(
        bridge, actor, ["codex", "claude-1"], "broadcast", within=60
    )
    expire(bridge, request["id"])
    [breach] = store.overdue_acknowledgements(bridge.home, paired["root"])
    assert breach["recipients"] == ["claude-1", "codex"]
    supervision.poll(bridge.home, directory)
    [notice] = returned(bridge, "claude")
    assert "claude-1 has no running session" in notice["body_md"]
    assert "codex has no running session" in notice["body_md"]
    assert not outstanding(bridge, paired, "codex")
    assert not outstanding(bridge, paired, "claude-1")


def test_a_share_no_recipient_can_act_on_returns_before_its_deadline(
    bridge, repo, paired
):
    directory = bridge.project(repo)[1]
    actor = registered(bridge, paired, "claude")
    registered(bridge, paired, "codex")
    share = request_ack(bridge, actor, ["codex"], "parked", within=600)
    supervision.poll(bridge.home, directory)
    [notice] = bounced(bridge, "claude")
    assert f"message {share['id']}" in notice["subject"]
    assert "codex has no live session process" in notice["body_md"]
    assert "You keep the work" in notice["body_md"]
    assert len(outstanding(bridge, paired, "codex")) == 1
    assert returned(bridge, "claude") == []
    supervision.poll(bridge.home, directory)
    assert len(bounced(bridge, "claude")) == 1


def test_a_returned_share_still_reaches_its_deadline_and_retires(
    bridge, repo, paired
):
    directory = bridge.project(repo)[1]
    actor = registered(bridge, paired, "claude")
    registered(bridge, paired, "codex")
    share = request_ack(bridge, actor, ["codex"], "unanswered", within=60)
    supervision.poll(bridge.home, directory)
    assert len(bounced(bridge, "claude")) == 1
    expire(bridge, share["id"])
    assert store.pending_acknowledgements(bridge.home, paired["root"]) == []
    supervision.poll(bridge.home, directory)
    [missed] = returned(bridge, "claude")
    assert f"message {share['id']}" in missed["subject"]
    assert outstanding(bridge, paired, "codex") == []
    manifest, observations = readings(bridge, directory)
    assert (
        supervision.bounced_shares(
            bridge.home, directory, manifest, observations
        )
        == []
    )


def test_a_share_a_fit_lane_can_answer_is_delivered_once(bridge, repo, paired):
    directory = bridge.project(repo)[1]
    actor = registered(bridge, paired, "claude")
    registered(bridge, paired, "codex")
    alive(directory, "codex")
    request_ack(bridge, actor, ["codex"], "fit", within=600)
    manifest, observations = readings(bridge, directory)
    assert (
        supervision.bounced_shares(
            bridge.home, directory, manifest, observations
        )
        == []
    )
    supervision.share_bounces(bridge.home, directory, manifest, observations)
    assert bounced(bridge, "claude") == []
    assert len(outstanding(bridge, paired, "codex")) == 1


def test_a_recipient_a_dialog_or_a_blocked_claim_holds_cannot_act(
    bridge, repo, paired
):
    directory = bridge.project(repo)[1]
    registered(bridge, paired, "claude")
    registered(bridge, paired, "codex")
    alive(directory, "codex")
    manifest, observations = readings(bridge, directory)

    def blocker():
        return supervision.share_blocker(
            bridge.home,
            directory,
            manifest,
            "codex",
            observations["codex"],
            issues.snapshot(directory),
        )

    assert blocker() == ""
    supervision.store_wake(
        bridge.home,
        directory,
        manifest["root"],
        "codex",
        {"result": "manual attention required"},
    )
    assert "native dialog" in blocker()
    supervision.store_wake(
        bridge.home,
        directory,
        manifest["root"],
        "codex",
        {"result": "delivered"},
    )
    assert blocker() == ""
    state = issues.snapshot(directory)
    state["issues"]["7"] = {
        "owner": "codex",
        "blocked_by": ["9"],
        "action": "implement",
    }
    state["revision"] += 1
    write_json(directory / "issues.json", state)
    assert blocker() == "holds #7, itself blocked by #9"


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
    bridge.status(cli.Selection(participant="claude"))
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
