"""Checks usage recorded per resolved claim and the operator price table."""

import json
import sys

import pytest

from agent_parley import budgets, cli, history, issues, lifecycle, roster
from agent_parley.state import BridgeError

MERGED = {
    "branch": "agent-parley/claude",
    "state": "MERGED",
    "commit": "a" * 40,
    "pull_request": 7,
    "observed_at": 1.0,
}


def run(monkeypatch, capsys, *arguments):
    """Runs one CLI invocation and returns its standard output."""
    monkeypatch.setattr(sys, "argv", ["agent-parley", *arguments])
    assert cli.main() == 0
    return capsys.readouterr().out


def reading(tokens, calls, hours, session=100.0, name="claude"):
    """Builds one consumption reading as `budgets.claim_reading` returns."""
    return {
        "participant": name,
        "tokens": tokens,
        "calls": calls,
        "hours": hours,
        "session": session,
        "at": 1.0,
    }


@pytest.fixture
def resolved(bridge, repo, paired, monkeypatch):
    """Claims issue 42 at a known reading and resolves it at a later one."""
    monkeypatch.setenv("AGENT_PARLEY_HOME", str(bridge.home))
    monkeypatch.setattr(
        budgets, "claim_reading", lambda *_: reading(1_000, 10, 0.5)
    )
    claim = bridge.issue(paired["lanes"]["claude"], "claim", "42")["claim_id"]
    directory = bridge.project(repo)[1]
    assert issues.snapshot(directory)["issues"]["42"]["usage_start"] == (
        reading(1_000, 10, 0.5)
    )
    lifecycle.resolve(
        directory,
        "42",
        evidence=MERGED,
        outcome="complete",
        actor="supervisor",
        claim_id=claim,
        usage=reading(413_000, 1_840, 2.9),
    )
    return directory


def test_history_reports_what_a_resolved_claim_used(
    resolved, repo, monkeypatch, capsys
):
    document = json.loads(
        run(
            monkeypatch,
            capsys,
            "history",
            "issue",
            "42",
            "--repo",
            str(repo),
            "--json",
        )
    )
    (entry,) = [
        record
        for record in document["records"]
        if record["action"] == "resolve"
    ]
    assert entry["usage"] == {
        "tokens": 412_000,
        "calls": 1_830,
        "hours": 2.4,
        "basis": "claim",
    }
    record = issues.snapshot(resolved)["issues"]["42"]
    assert "usage_start" not in record
    assert record["resolution"]["usage"] == entry["usage"]
    text = run(
        monkeypatch, capsys, "history", "issue", "42", "--repo", str(repo)
    )
    assert "tokens 412,000, calls 1,830, 2.4 h" in text


def test_report_usage_totals_without_money_until_a_price_is_recorded(
    resolved, bridge, repo, monkeypatch, capsys
):
    arguments = ("report", "usage", "--since", "7d", "--repo", str(repo))
    document = json.loads(run(monkeypatch, capsys, *arguments, "--json"))
    assert document["schema"] == "agent-parley/read/v1"
    assert document["kind"] == "report_usage"
    (row,) = document["rows"]
    assert row["issue"] == 42
    assert row["participant"] == "claude"
    assert row["pull_request"] == 7
    assert row["pull_request_state"] == "MERGED"
    assert document["totals"] == {
        "tokens": 412_000,
        "calls": 1_830,
        "hours": 2.4,
    }
    assert "estimated_spend" not in row
    assert "est." not in run(monkeypatch, capsys, *arguments)
    monkeypatch.chdir(repo)
    assert "estimate" in bridge.price(repo, "claude", 3.0)
    document = json.loads(run(monkeypatch, capsys, *arguments, "--json"))
    assert document["rows"][0]["estimated_spend"] == 1.24
    assert document["totals"]["estimated_spend"] == 1.24
    text = run(monkeypatch, capsys, *arguments)
    assert "est. $1.24" in text and "an estimate" in text
    bridge.price(repo, "claude", 0)
    assert "price" not in roster.provider(bridge.home, "claude")


@pytest.mark.parametrize("via", ["token", "worktree"])
def test_a_lane_cannot_record_a_price(bridge, repo, paired, monkeypatch, via):
    if via == "token":
        monkeypatch.setenv(roster.LANE_TOKEN, "lane-token")
    else:
        monkeypatch.chdir(paired["lanes"]["claude"])
    with pytest.raises(BridgeError, match="operator shell"):
        bridge.price(paired["lanes"]["claude"], "claude", 3.0)
    assert "price" not in roster.provider(bridge.home, "claude")
    assert "no price" in bridge.price(repo, "claude", None)


def test_a_price_is_validated_like_the_rest_of_a_provider():
    entry = {"adapter": "claude", "command": "claude", "price": {"per_mtok": 3}}
    assert roster.validated_provider("mine", entry)["price"] == {"per_mtok": 3}
    for price in ({"per_mtok": -1}, {"per_mtok": "3"}, {"usd": 3}):
        with pytest.raises(BridgeError, match="price"):
            roster.validated_provider("mine", {**entry, "price": price})


def test_a_rotated_session_reports_its_own_figures():
    used = lifecycle.usage_between(
        reading(5_000, 10, 1.0), reading(800, 25, 0.25, session=200.0)
    )
    assert used == {
        "tokens": 800,
        "calls": 15,
        "hours": 0.25,
        "basis": "session",
    }
    unknown = lifecycle.usage_between(None, reading(None, 25, 0.25))
    assert unknown == {
        "tokens": None,
        "calls": None,
        "hours": 0.25,
        "basis": "session",
    }


def test_a_ledger_without_usage_loads_and_reports_unknown(
    bridge, repo, paired, monkeypatch, capsys
):
    monkeypatch.setenv("AGENT_PARLEY_HOME", str(bridge.home))
    monkeypatch.setattr(budgets, "claim_reading", lambda *_: None)
    claim = bridge.issue(paired["lanes"]["claude"], "claim", "42")["claim_id"]
    directory = bridge.project(repo)[1]
    assert "usage_start" not in issues.snapshot(directory)["issues"]["42"]
    lifecycle.resolve(
        directory,
        "42",
        evidence=MERGED,
        outcome="complete",
        actor="supervisor",
        claim_id=claim,
    )
    entry = issues.snapshot(directory)["issues"]["42"]["history"][-1]
    assert entry["action"] == "resolve" and "usage" not in entry
    spent = history.usage(bridge.home, directory, roster.read(directory))
    assert spent["rows"][0]["tokens"] is None
    assert spent["totals"] == {"tokens": 0, "calls": 0, "hours": 0}
    text = run(monkeypatch, capsys, "report", "usage", "--repo", str(repo))
    assert "tokens n/a, calls n/a, n/a h" in text
