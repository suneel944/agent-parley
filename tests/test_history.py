"""Checks the recorded history of an issue, a lane and one claim."""

import json
import sys

import pytest

from agent_parley import cli, history, issues, store
from agent_parley.state import BridgeError


def registered(bridge, paired, name):
    """Registers one lane with the store and resolves its actor."""
    store.initialize(bridge.home)
    token = store.register(bridge.home, paired["root"], name)[
        "registration_token"
    ]
    actor = store.authenticate(bridge.home, token)
    assert actor is not None
    return actor


def run(monkeypatch, capsys, *arguments):
    """Runs one CLI invocation and returns its standard output."""
    monkeypatch.setattr(sys, "argv", ["agent-parley", *arguments])
    assert cli.main() == 0
    return capsys.readouterr().out


def test_every_claim_generation_carries_its_own_identifier(
    bridge, repo, paired
):
    directory = bridge.project(repo)[1]
    claude = paired["lanes"]["claude"]
    first = bridge.issue(claude, "claim", "42")["claim_id"]
    bridge.issue(claude, "release", "42")
    second = bridge.issue(claude, "claim", "42")["claim_id"]
    assert first and second and first != second
    record = bridge.issue(
        claude, "offer", "42", to="codex", summary="commit, checks"
    )
    accepted = bridge.issue(
        paired["lanes"]["codex"],
        "accept",
        "42",
        offer_id=record["offer"]["id"],
    )
    assert accepted["claim_id"] not in (first, second)
    held = history.holdings(directory, "42")
    assert [generation["participant"] for generation in held] == [
        "claude",
        "claude",
        "codex",
    ]
    assert held[0]["ended"] is not None and held[0]["seconds"] >= 0
    assert held[-1]["ended"] is None


def test_history_of_an_issue_lists_its_transitions(bridge, repo, paired):
    claude = paired["lanes"]["claude"]
    bridge.issue(paired["lanes"]["codex"], "claim", "17")
    bridge.issue(claude, "claim", "42")
    bridge.issue(claude, "block", "42", on="17")
    bridge.issue(claude, "release", "42")
    reported = bridge.history(repo, "issue", "42")
    actions = [record["action"] for record in reported["records"]]
    assert actions == ["claim", "block", "release"]
    assert {record["kind"] for record in reported["records"]} == {
        "claim",
        "dependency",
    }
    assert reported["holdings"][0]["participant"] == "claude"


def test_history_of_a_lane_covers_every_substrate(bridge, repo, paired):
    claude = paired["lanes"]["claude"]
    actor = registered(bridge, paired, "claude")
    claim = bridge.issue(claude, "claim", "42")["claim_id"]
    store.call(
        bridge.home,
        actor,
        "file_reservation_paths",
        {"paths": ["src/engine.py"], "reason": "engine work"},
    )
    registered(bridge, paired, "codex")
    store.call(
        bridge.home,
        actor,
        "send_message",
        {
            "to": ["codex"],
            "subject": "Interface change",
            "body_md": "The parser signature changed.",
            "idempotency_key": "k1",
        },
    )
    bridge.report(claude, "ready", "Engine built", "", "12 tests passed")
    reported = bridge.history(repo, "participant", "claude")
    kinds = [record["kind"] for record in reported["records"]]
    assert {"claim", "reservation", "message", "report"} <= set(kinds)
    assert all(
        record["claim_id"] == claim
        for record in reported["records"]
        if record["kind"] in {"reservation", "message", "report"}
    )
    assert all(record["provider"] == "claude" for record in reported["records"])


def test_history_of_a_claim_follows_one_piece_of_work(bridge, repo, paired):
    claude = paired["lanes"]["claude"]
    actor = registered(bridge, paired, "claude")
    first = bridge.issue(claude, "claim", "42")["claim_id"]
    store.call(
        bridge.home,
        actor,
        "file_reservation_paths",
        {"paths": ["src/first.py"]},
    )
    bridge.issue(claude, "release", "42")
    second = bridge.issue(claude, "claim", "43")["claim_id"]
    store.call(
        bridge.home,
        actor,
        "file_reservation_paths",
        {"paths": ["src/second.py"]},
    )
    reported = bridge.history(repo, "claim", second)
    paths = [
        record["path"]
        for record in reported["records"]
        if record["kind"] == "reservation"
    ]
    assert paths == ["src/second.py"]
    assert all(record["claim_id"] == second for record in reported["records"])
    assert first != second


def test_records_without_a_recorded_claim_carry_no_claim_suffix(
    bridge, repo, paired
):
    actor = registered(bridge, paired, "claude")
    registered(bridge, paired, "codex")
    store.call(
        bridge.home,
        actor,
        "send_message",
        {
            "to": ["codex"],
            "subject": "No claim held",
            "body_md": "Sent before any claim.",
            "idempotency_key": "k2",
        },
    )
    reported = bridge.history(repo, "participant", "claude")
    message = next(
        record for record in reported["records"] if record["kind"] == "message"
    )
    assert message["claim_id"] is None
    rendered = history.describe([message])
    assert "; claim" not in rendered
    held = [
        {
            "participant": "claude",
            "seconds": 12,
            "ended": None,
            "claim_id": "c-1",
        }
    ]
    assert "; claim c-1" in history.describe([], held)


def test_filters_and_windows_narrow_the_listing(bridge, repo, paired):
    claude = paired["lanes"]["claude"]
    bridge.issue(claude, "claim", "42")
    bridge.issue(paired["lanes"]["codex"], "claim", "43")
    only_claims = bridge.history(repo, "", "", kinds=("claim",))
    assert {record["kind"] for record in only_claims["records"]} == {"claim"}
    mine = bridge.history(repo, "", "", participant="codex")
    assert {record["participant"] for record in mine["records"]} == {"codex"}
    one_issue = bridge.history(repo, "", "", issue="42")
    assert {record["issue"] for record in one_issue["records"]} == {42}
    future = bridge.history(repo, "", "", window=0.000001)
    assert future["records"] == []


def test_history_reads_and_reports_as_json(
    bridge, repo, paired, monkeypatch, capsys
):
    claude = paired["lanes"]["claude"]
    bridge.issue(claude, "claim", "42")
    directory = bridge.project(repo)[1]
    before = issues.snapshot(directory)["revision"]
    printed = run(
        monkeypatch,
        capsys,
        "--home",
        str(bridge.home),
        "history",
        "issue",
        "42",
        "--repo",
        str(repo),
    )
    assert "claim #42" in printed
    assert issues.snapshot(directory)["revision"] == before
    document = json.loads(
        run(
            monkeypatch,
            capsys,
            "--home",
            str(bridge.home),
            "history",
            "issue",
            "42",
            "--repo",
            str(repo),
            "--json",
        )
    )
    assert document["kind"] == "history"
    assert document["subject"] == "issue"
    assert document["records"][0]["at"].endswith("Z")
    assert document["holdings"][0]["started_at"].endswith("Z")


def test_an_unknown_participant_is_refused(bridge, repo, paired):
    with pytest.raises(BridgeError, match="not a participant"):
        bridge.history(repo, "participant", "absent")
