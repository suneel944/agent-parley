"""Checks the operator's issue assign path from offer to acceptance."""

import sys

import pytest

from agent_parley import cli, issues, store
from agent_parley.state import BridgeError


def run(bridge, monkeypatch, capsys, *arguments):
    """Runs one CLI invocation and returns its standard output."""
    monkeypatch.setattr(
        sys, "argv", ["agent-parley", "--home", str(bridge.home), *arguments]
    )
    assert cli.main() == 0
    return capsys.readouterr().out


def ledger(bridge, repo):
    """Reads the published issue ledger of the project."""
    return issues.snapshot(bridge.project(repo)[1])["issues"]


def listing(bridge, repo):
    """Formats the ledger exactly as issue list and status print it."""
    return issues.describe(issues.snapshot(bridge.project(repo)[1]))


def mailed(bridge):
    """Reads the most recent message delivered to any lane."""
    with store.connect(bridge.home) as db:
        return db.execute(
            "SELECT subject, body_md FROM messages ORDER BY id DESC LIMIT 1"
        ).fetchone()


def held(bridge, paired, name, number="42"):
    """Registers a lane with the store and claims one issue for it."""
    store.initialize(bridge.home)
    store.register(bridge.home, paired["root"], name)
    return bridge.issue(paired["lanes"][name], "claim", number)


def test_an_unheld_issue_is_offered_to_the_named_lane(bridge, repo, paired):
    result = bridge.issue_assign(repo, "42", "codex", reason="Parser work")
    record = ledger(bridge, repo)["42"]
    assert result["recorded"] == "offer"
    assert record["owner"] is None
    assert record["offer"]["to"] == "codex"
    assert record["offer"]["id"] == result["offer_id"]
    assert record["offer"]["reason"] == "Parser work"
    assert issues.offer_source(record["offer"]) == issues.OPERATOR


def test_the_listing_shows_the_pending_offer_and_its_operator_source(
    bridge, repo, paired
):
    result = bridge.issue_assign(repo, "42", "codex", reason="Parser work")
    reported = listing(bridge, repo)
    assert "#42: unclaimed" in reported
    assert "operator offer to codex" in reported
    assert result["offer_id"] in reported
    assert 'Operator-stated reason: "Parser work"' in reported


def test_ownership_moves_only_when_the_named_lane_accepts(bridge, repo, paired):
    result = bridge.issue_assign(repo, "42", "codex", reason="Parser work")
    assert ledger(bridge, repo)["42"]["owner"] is None
    record = bridge.issue(
        paired["lanes"]["codex"], "accept", "42", offer_id=result["offer_id"]
    )
    assert record["owner"] == "codex"
    assert record["offer"] is None


def test_a_lane_the_operator_did_not_name_cannot_accept(bridge, repo, paired):
    result = bridge.issue_assign(repo, "42", "codex")
    with pytest.raises(BridgeError, match="named recipient"):
        bridge.issue(
            paired["lanes"]["claude"],
            "accept",
            "42",
            offer_id=result["offer_id"],
        )
    assert ledger(bridge, repo)["42"]["owner"] is None


def test_a_declined_operator_offer_leaves_the_issue_unclaimed(
    bridge, repo, paired
):
    result = bridge.issue_assign(repo, "42", "codex")
    record = bridge.issue(
        paired["lanes"]["codex"], "decline", "42", offer_id=result["offer_id"]
    )
    assert record["owner"] is None
    assert record["offer"] is None
    assert listing(bridge, repo) == "No issues claimed."


def test_an_unknown_lane_is_refused(bridge, repo, paired):
    with pytest.raises(BridgeError, match="Choose a participant"):
        bridge.issue_assign(repo, "42", "nobody")
    assert "42" not in ledger(bridge, repo)


def test_a_lane_that_already_holds_the_issue_is_refused(bridge, repo, paired):
    bridge.issue(paired["lanes"]["codex"], "claim", "42")
    with pytest.raises(BridgeError, match="already owned by codex"):
        bridge.issue_assign(repo, "42", "codex")
    record = ledger(bridge, repo)["42"]
    assert record["owner"] == "codex"
    assert record["request"] is None


def test_a_second_offer_on_the_same_issue_is_refused(bridge, repo, paired):
    bridge.issue_assign(repo, "42", "codex")
    with pytest.raises(BridgeError, match="offer is pending"):
        bridge.issue_assign(repo, "42", "claude")


def test_a_held_issue_records_a_request_its_owner_is_mailed(
    bridge, repo, paired
):
    held(bridge, paired, "claude")
    result = bridge.issue_assign(repo, "42", "codex", reason="Codex is idle")
    record = ledger(bridge, repo)["42"]
    message = mailed(bridge)
    assert result["recorded"] == "request"
    assert result["delivered"] is True
    assert record["owner"] == "claude"
    assert record["offer"] is None
    assert record["request"]["to"] == "codex"
    assert record["request"]["reason"] == "Codex is idle"
    assert "hand issue #42 to codex" in message["subject"]
    assert result["offer_id"] in message["body_md"]
    assert "Codex is idle" in message["body_md"]


def test_the_owner_authorizes_the_request_before_the_offer_exists(
    bridge, repo, paired
):
    held(bridge, paired, "claude")
    result = bridge.issue_assign(repo, "42", "codex", reason="Codex is idle")
    authorized = bridge.issue(
        paired["lanes"]["claude"], "accept", "42", offer_id=result["offer_id"]
    )
    assert authorized["owner"] == "claude"
    assert authorized["request"] is None
    assert authorized["offer"]["to"] == "codex"
    assert issues.offer_source(authorized["offer"]) == issues.OPERATOR
    accepted = bridge.issue(
        paired["lanes"]["codex"],
        "accept",
        "42",
        offer_id=authorized["offer"]["id"],
    )
    assert accepted["owner"] == "codex"


def test_only_the_owner_answers_an_operator_request(bridge, repo, paired):
    held(bridge, paired, "claude")
    result = bridge.issue_assign(repo, "42", "codex")
    with pytest.raises(BridgeError, match="Only claude can answer"):
        bridge.issue(
            paired["lanes"]["codex"],
            "accept",
            "42",
            offer_id=result["offer_id"],
        )
    assert ledger(bridge, repo)["42"]["owner"] == "claude"


def test_a_refused_request_leaves_the_work_with_its_owner(bridge, repo, paired):
    held(bridge, paired, "claude")
    result = bridge.issue_assign(repo, "42", "codex")
    record = bridge.issue(
        paired["lanes"]["claude"], "decline", "42", offer_id=result["offer_id"]
    )
    assert record["owner"] == "claude"
    assert record["offer"] is None
    assert record["request"] is None


def test_a_request_stands_when_its_notice_cannot_be_delivered(
    bridge, repo, paired
):
    bridge.issue(paired["lanes"]["claude"], "claim", "42")
    result = bridge.issue_assign(repo, "42", "codex")
    assert result["recorded"] == "request"
    assert result["delivered"] is False
    assert result["detail"]
    assert ledger(bridge, repo)["42"]["request"]["to"] == "codex"


def test_unassign_withdraws_an_offer_no_lane_accepted(bridge, repo, paired):
    bridge.issue_assign(repo, "42", "codex")
    result = bridge.issue_assign(repo, "42", withdraw=True)
    assert result["recorded"] == "withdrawal"
    assert ledger(bridge, repo)["42"]["offer"] is None
    assert listing(bridge, repo) == "No issues claimed."


def test_unassign_withdraws_a_request_its_owner_has_not_answered(
    bridge, repo, paired
):
    held(bridge, paired, "claude")
    bridge.issue_assign(repo, "42", "codex")
    bridge.issue_assign(repo, "42", withdraw=True)
    record = ledger(bridge, repo)["42"]
    assert record["request"] is None
    assert record["owner"] == "claude"


def test_unassign_names_the_lane_that_already_accepted(bridge, repo, paired):
    result = bridge.issue_assign(repo, "42", "codex")
    bridge.issue(
        paired["lanes"]["codex"], "accept", "42", offer_id=result["offer_id"]
    )
    with pytest.raises(BridgeError, match="codex accepted it"):
        bridge.issue_assign(repo, "42", withdraw=True)
    assert ledger(bridge, repo)["42"]["owner"] == "codex"


def test_unassign_without_a_pending_offer_is_refused(bridge, repo, paired):
    with pytest.raises(BridgeError, match="No operator offer is pending"):
        bridge.issue_assign(repo, "42", withdraw=True)


def test_history_reports_why_the_operator_moved_the_work(bridge, repo, paired):
    bridge.issue_assign(repo, "42", "codex", reason="Parser work")
    reported = bridge.history(repo, "issue", "42")
    details = [record["detail"] for record in reported["records"]]
    assert "assign #42: Parser work" in details


def test_the_command_prints_which_of_the_two_it_recorded(
    bridge, repo, paired, monkeypatch, capsys
):
    offered = run(
        bridge,
        monkeypatch,
        capsys,
        "issue",
        "assign",
        "42",
        "codex",
        "--reason",
        "Parser work",
        "--repo",
        str(repo),
    )
    assert "Offered issue #42 to codex" in offered
    assert "accepts it" in offered
    withdrawn = run(
        bridge,
        monkeypatch,
        capsys,
        "issue",
        "assign",
        "42",
        "--unassign",
        "--repo",
        str(repo),
    )
    assert "Withdrew the operator offer on issue #42." in withdrawn
    held(bridge, paired, "claude")
    requested = run(
        bridge,
        monkeypatch,
        capsys,
        "issue",
        "assign",
        "42",
        "codex",
        "--repo",
        str(repo),
    )
    assert "Asked claude to hand issue #42 to codex" in requested
    assert "claude still owns it." in requested


def test_the_command_needs_a_lane_or_a_withdrawal(
    bridge, repo, paired, monkeypatch, capsys
):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agent-parley",
            "--home",
            str(bridge.home),
            "issue",
            "assign",
            "42",
            "--repo",
            str(repo),
        ],
    )
    with pytest.raises(SystemExit):
        cli.main()


def test_a_lane_and_a_withdrawal_together_are_refused(
    bridge, repo, paired, monkeypatch, capsys
):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agent-parley",
            "--home",
            str(bridge.home),
            "issue",
            "assign",
            "42",
            "codex",
            "--unassign",
            "--repo",
            str(repo),
        ],
    )
    with pytest.raises(SystemExit):
        cli.main()
