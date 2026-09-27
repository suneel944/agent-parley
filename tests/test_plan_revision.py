"""Checks plan revisions: proposals, the envelope, approval and escalation."""

import json
import sys

import pytest

from agent_parley import cli, issues, lifecycle, plan, state
from agent_parley.state import BridgeError

ENVELOPED = """
[plan]
name = "Parser rewrite"

[dependencies]
"42" = ["17"]
"43" = ["17"]
"44" = ["42", "43"]

[revisions]
scope = ["17", "42", "43", "44"]
max_changes = 2
max_revisions = 5
"""

BARE = """
[dependencies]
"42" = ["17"]
"43" = ["17"]
"""


def applied(bridge, repo, text=ENVELOPED, name="plan.toml"):
    """Applies one plan file and returns the version a proposal names."""
    path = repo.parent / name
    path.write_text(text)
    bridge.work_plan(repo, "apply", path)
    return bridge.plan_revision(repo, "proposals")["revision"]


def ledger(bridge, repo):
    """Returns the recorded issue records."""
    return issues.snapshot(bridge.project(repo)[1])["issues"]


def proposed(bridge, where, base, add=(), remove=(), reason="found it"):
    """Files one proposal and returns its record."""
    return bridge.plan_revision(
        where,
        "propose",
        base=base,
        add=list(add),
        remove=list(remove),
        reason=reason,
        evidence=["tests/test_parser.py fails without #42"],
    )


def test_a_discovered_prerequisite_inside_the_envelope_applies(
    bridge, repo, paired
):
    base = applied(bridge, repo)
    lane = paired["lanes"]["claude"]
    record = proposed(bridge, lane, base, add=["43:42"])
    assert record["status"] == "accepted"
    assert record["decided_by"] == "envelope"
    assert record["by"] == "claude"
    assert record["effect"] == {"waits": [], "frees": []}
    assert ledger(bridge, repo)["43"]["blocked_by"] == ["17", "42"]
    listing = bridge.plan_revision(repo, "proposals")
    assert listing["revision"] == base + 1
    assert listing["automatic"] == 1
    shown = bridge.work_plan(repo, "show")
    assert shown["unplanned"] == []
    assert shown["applied_by"] == "claude"


def test_an_obsolete_edge_is_removed_by_explicit_revision(bridge, repo, paired):
    base = applied(bridge, repo)
    record = proposed(bridge, paired["lanes"]["claude"], base, remove=["44:43"])
    assert record["status"] == "accepted"
    assert ledger(bridge, repo)["44"]["blocked_by"] == ["42"]
    assert bridge.work_plan(repo, "show")["unplanned"] == []


def test_a_new_prerequisite_waits_for_authorization(bridge, repo, paired):
    base = applied(bridge, repo)
    lane = paired["lanes"]["claude"]
    record = proposed(bridge, lane, base, add=["42:60"])
    assert record["status"] == "pending"
    assert "#60 is not authorized yet" in record["held"]
    assert "#60 is outside the revision scope" in record["held"]
    assert "60" not in ledger(bridge, repo)
    assert ledger(bridge, repo)["42"]["blocked_by"] == ["17"]
    assert lifecycle.actionable(issues.snapshot(bridge.project(repo)[1])) == [
        "17"
    ]
    assert f"agent-parley plan approve {record['id']}" in (
        plan.render_proposal(record)
    )
    with pytest.raises(BridgeError, match="Only the operator"):
        bridge.plan_revision(lane, "approve", record["id"])
    approved = bridge.plan_revision(repo, "approve", record["id"])
    assert approved["status"] == "accepted"
    assert approved["decided_by"] == "operator"
    recorded = ledger(bridge, repo)
    assert recorded["42"]["blocked_by"] == ["17", "60"]
    assert lifecycle.state(recorded["60"])["authorized"]
    assert recorded["60"]["owner"] is None


def test_a_plan_without_an_envelope_holds_every_lane_proposal(
    bridge, repo, paired
):
    base = applied(bridge, repo, BARE)
    record = proposed(bridge, paired["lanes"]["claude"], base, add=["43:42"])
    assert record["status"] == "pending"
    assert record["held"] == [
        "the applied plan defines no [revisions] envelope"
    ]
    assert ledger(bridge, repo)["43"]["blocked_by"] == ["17"]


def test_an_out_of_scope_request_is_kept_for_the_operator(bridge, repo, paired):
    text = ENVELOPED.replace('"17", "42", "43", "44"', '"42", "43"')
    base = applied(bridge, repo, text)
    record = proposed(bridge, paired["lanes"]["claude"], base, remove=["42:17"])
    assert record["status"] == "pending"
    assert record["held"] == ["#17 is outside the revision scope"]
    assert ledger(bridge, repo)["42"]["blocked_by"] == ["17"]
    rejected = bridge.plan_revision(
        repo, "reject", record["id"], reason="17 still owns the lexer"
    )
    assert rejected["status"] == "rejected"
    assert rejected["decided_by"] == "operator"
    assert rejected["note"] == "17 still owns the lexer"
    assert rejected["evidence"] == ["tests/test_parser.py fails without #42"]
    assert ledger(bridge, repo)["42"]["blocked_by"] == ["17"]


def test_the_envelope_bounds_edges_and_automatic_revisions(
    bridge, repo, paired
):
    text = ENVELOPED.replace("max_revisions = 5", "max_revisions = 1")
    base = applied(bridge, repo, text)
    lane = paired["lanes"]["claude"]
    wide = proposed(
        bridge, lane, base, add=["43:42"], remove=["44:43", "44:42"]
    )
    assert wide["status"] == "pending"
    assert wide["held"] == [
        "it changes 3 edges; the envelope allows 2 per revision"
    ]
    assert proposed(bridge, lane, base, add=["43:42"])["status"] == "accepted"
    spent = proposed(bridge, lane, base + 1, remove=["44:43"])
    assert spent["held"] == ["the plan's 1 automatic revisions are spent"]


def test_a_stale_base_version_is_refused(bridge, repo, paired):
    base = applied(bridge, repo)
    with pytest.raises(BridgeError, match="plan is at version 1"):
        proposed(bridge, paired["lanes"]["claude"], base - 1, add=["43:42"])
    assert bridge.plan_revision(repo, "proposals")["proposals"] == []


def test_a_proposal_left_behind_by_a_new_apply_is_stale(bridge, repo, paired):
    base = applied(bridge, repo)
    record = proposed(bridge, paired["lanes"]["claude"], base, add=["42:60"])
    applied(bridge, repo, name="again.toml")
    [left] = bridge.plan_revision(repo, "proposals")["proposals"]
    assert left["current"] is False
    assert left["status"] == "stale"
    assert "propose again" in left["note"]
    with pytest.raises(BridgeError, match="already stale"):
        bridge.plan_revision(repo, "approve", record["id"])
    assert "60" not in ledger(bridge, repo)


def test_proposals_left_behind_never_hold_open_places(
    bridge, repo, paired, monkeypatch
):
    base = applied(bridge, repo, BARE)
    lane = paired["lanes"]["claude"]
    monkeypatch.setattr(plan, "MAX_PENDING", 1)
    proposed(bridge, lane, base, add=["43:42"])
    base = applied(bridge, repo, BARE, name="again.toml")
    assert proposed(bridge, lane, base, add=["43:42"])["status"] == "pending"


def test_concurrent_proposals_on_one_version_apply_once(bridge, repo, paired):
    base = applied(bridge, repo)
    first = proposed(bridge, paired["lanes"]["claude"], base, add=["42:60"])
    second = proposed(bridge, paired["lanes"]["codex"], base, add=["43:60"])
    assert bridge.plan_revision(repo, "approve", first["id"])["status"] == (
        "accepted"
    )
    with pytest.raises(BridgeError, match="already stale"):
        bridge.plan_revision(repo, "approve", second["id"])
    assert ledger(bridge, repo)["43"]["blocked_by"] == ["17"]
    with pytest.raises(BridgeError, match="plan is at version 2"):
        proposed(bridge, paired["lanes"]["codex"], base, add=["43:42"])


def test_a_cycle_anywhere_in_the_result_is_refused(bridge, repo, paired):
    base = applied(bridge, repo)
    with pytest.raises(BridgeError, match="cycle"):
        proposed(bridge, paired["lanes"]["claude"], base, add=["17:44"])
    with pytest.raises(BridgeError, match="does not wait on"):
        proposed(bridge, paired["lanes"]["claude"], base, remove=["17:44"])
    with pytest.raises(BridgeError, match="already waits"):
        proposed(bridge, paired["lanes"]["claude"], base, add=["42:17"])
    assert bridge.plan_revision(repo, "proposals")["proposals"] == []
    assert ledger(bridge, repo)["17"]["blocked_by"] == []


def test_another_owners_claim_is_never_seized(bridge, repo, paired):
    base = applied(bridge, repo)
    codex = paired["lanes"]["codex"]
    bridge.issue(codex, "claim", "42")
    before = ledger(bridge, repo)["42"]
    record = proposed(bridge, paired["lanes"]["claude"], base, add=["42:43"])
    assert record["status"] == "pending"
    assert record["held"] == ["#42 is held by codex"]
    bridge.plan_revision(repo, "approve", record["id"])
    after = ledger(bridge, repo)["42"]
    assert after["blocked_by"] == ["17", "43"]
    assert after["owner"] == "codex"
    assert after["claim_id"] == before["claim_id"]
    assert after["execution"]["state"] == before["execution"]["state"]
    assert after["execution"]["claim_id"] == before["execution"]["claim_id"]


def test_the_owner_may_revise_its_own_claim(bridge, repo, paired):
    base = applied(bridge, repo)
    lane = paired["lanes"]["claude"]
    bridge.issue(lane, "claim", "42")
    record = proposed(bridge, lane, base, remove=["42:17"])
    assert record["status"] == "accepted"
    assert record["effect"] == {"waits": [], "frees": ["42"]}
    assert ledger(bridge, repo)["42"]["owner"] == "claude"


def test_replay_and_restart_never_duplicate_a_change(bridge, repo, paired):
    base = applied(bridge, repo)
    lane = paired["lanes"]["claude"]
    directory = bridge.project(repo)[1]
    before = (directory / plan.PLAN).read_text()
    first = proposed(bridge, lane, base, add=["43:42"])
    revision = issues.snapshot(directory)["revision"]
    again = proposed(bridge, lane, base, add=["43:42"])
    assert again["replayed"] is True
    assert again["id"] == first["id"]
    (directory / plan.PLAN).write_text(before)
    restarted = proposed(bridge, lane, base, add=["43:42"])
    assert restarted["status"] == "accepted"
    assert issues.snapshot(directory)["revision"] == revision
    assert ledger(bridge, repo)["43"]["blocked_by"] == ["17", "42"]
    held = proposed(bridge, lane, base + 1, add=["42:60"])
    bridge.plan_revision(repo, "approve", held["id"])
    twice = bridge.plan_revision(repo, "approve", held["id"])
    assert twice["replayed"] is True
    assert ledger(bridge, repo)["42"]["blocked_by"] == ["17", "60"]
    with pytest.raises(BridgeError, match="already accepted"):
        bridge.plan_revision(repo, "reject", held["id"])


def test_contradictory_revisions_escalate(bridge, repo, paired):
    base = applied(bridge, repo)
    lane = paired["lanes"]["claude"]
    assert proposed(bridge, lane, base, add=["43:42"])["status"] == "accepted"
    assert (
        proposed(bridge, lane, base + 1, remove=["43:42"])["status"]
        == "accepted"
    )
    looped = proposed(bridge, lane, base + 2, add=["43:42"])
    assert looped["status"] == "escalated"
    assert looped["held"][0] == (
        "#43 waits on #42 was already revised 2 times under this plan version"
    )
    assert ledger(bridge, repo)["43"]["blocked_by"] == ["17"]
    applied(bridge, repo, name="again.toml")
    fresh = bridge.plan_revision(repo, "proposals")
    assert fresh["automatic"] == 0
    assert (
        proposed(bridge, lane, fresh["revision"], add=["43:42"])["status"]
        == "accepted"
    )


def test_open_proposals_are_bounded(bridge, repo, paired, monkeypatch):
    base = applied(bridge, repo, BARE)
    lane = paired["lanes"]["claude"]
    monkeypatch.setattr(plan, "MAX_PENDING", 1)
    proposed(bridge, lane, base, add=["43:42"])
    with pytest.raises(BridgeError, match="already await the operator"):
        proposed(bridge, lane, base, remove=["43:17"])
    assert len(bridge.plan_revision(repo, "proposals")["proposals"]) == 1


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"add": ["42"]}, "ISSUE:BLOCKER"),
        ({"add": ["42:42"]}, "cannot wait on itself"),
        ({"add": ["43:42"], "remove": ["43:42"]}, "same edge twice"),
        ({}, "1 to 10 edges"),
    ],
)
def test_a_malformed_proposal_is_refused(
    bridge, repo, paired, changes, message
):
    base = applied(bridge, repo)
    with pytest.raises(BridgeError, match=message):
        proposed(bridge, paired["lanes"]["claude"], base, **changes)


def test_evidence_is_bounded(bridge, repo, paired):
    base = applied(bridge, repo)
    with pytest.raises(BridgeError, match="evidence items"):
        bridge.plan_revision(
            paired["lanes"]["claude"],
            "propose",
            base=base,
            add=["43:42"],
            reason="found it",
            evidence=["x" * (plan.MAX_EVIDENCE_TEXT + 1)],
        )


@pytest.mark.parametrize(
    ("table", "message"),
    [
        ("[revisions]\nunknown = 1\n", "holds only scope"),
        ("[revisions]\nmax_changes = 0\n", "max_changes"),
        ('[revisions]\nscope = ["x"]\n', "positive number"),
    ],
)
def test_an_unusable_envelope_is_refused(bridge, repo, paired, table, message):
    path = repo.parent / "broken.toml"
    path.write_text('[dependencies]\n"42" = ["17"]\n' + table)
    with pytest.raises(BridgeError, match=message):
        bridge.work_plan(repo, "apply", path)


def test_the_command_line_proposes_lists_and_approves(
    bridge, repo, paired, capsys, monkeypatch
):
    base = applied(bridge, repo)
    lane = paired["lanes"]["claude"]

    def run(*arguments, code=0):
        """Runs one CLI invocation and returns its standard output."""
        monkeypatch.setattr(
            sys,
            "argv",
            ["agent-parley", "--home", str(bridge.home), *arguments],
        )
        assert cli.main() == code
        return capsys.readouterr().out

    filed = json.loads(
        run(
            "plan",
            "propose",
            "--repo",
            str(lane),
            "--base",
            str(base),
            "--add",
            "42:60",
            "--reason",
            "the lexer needs a token table first",
            "--evidence",
            "lexer.py imports tokens.py",
            "--json",
        )
    )
    assert filed["kind"] == "plan_revision"
    assert filed["status"] == "pending"
    listing = run("plan", "proposals", "--repo", str(repo))
    assert f"Plan version {base}." in listing
    assert f"agent-parley plan approve {filed['id']}" in listing
    assert "accepted" in run(
        "plan", "approve", filed["id"], "--repo", str(repo)
    )
    stale = proposed(bridge, lane, base + 1, add=["43:60"])
    applied(bridge, repo, name="again.toml")
    assert f"{stale['id']} stale" in run(
        "plan", "proposals", "--repo", str(repo)
    )


def test_a_lane_cannot_apply_a_plan(bridge, repo, paired):
    applied(bridge, repo)
    lane = paired["lanes"]["claude"]
    path = repo.parent / "wider.toml"
    path.write_text(ENVELOPED.replace('"44"]', '"44", "60"]'))
    with pytest.raises(BridgeError, match="Only the operator applies"):
        bridge.work_plan(lane, "apply", path)
    assert bridge.plan_revision(repo, "proposals")["revision"] == 1
    assert bridge.work_plan(lane, "diff", path)["add"] == []
    assert bridge.work_plan(lane, "show")["versions"] == 1


def test_apply_holds_the_plan_lock_across_the_ledger_write(
    bridge, repo, paired
):
    directory = bridge.project(repo)[1]
    path = repo.parent / "plan.toml"
    path.write_text(ENVELOPED)
    with (
        state.lock(directory / "plan.lock"),
        pytest.raises(state.LockBusy),
    ):
        bridge.work_plan(repo, "apply", path)
    assert "44" not in ledger(bridge, repo)


def test_a_crashed_proposal_lands_after_another_acceptance(
    bridge, repo, paired
):
    base = applied(bridge, repo)
    directory = bridge.project(repo)[1]
    before = (directory / plan.PLAN).read_text()
    first = proposed(bridge, paired["lanes"]["claude"], base, add=["43:42"])
    (directory / plan.PLAN).write_text(before)
    other = proposed(bridge, paired["lanes"]["codex"], base, remove=["44:43"])
    assert other["status"] == "accepted"
    revision = issues.snapshot(directory)["revision"]
    replayed = proposed(bridge, paired["lanes"]["claude"], base, add=["43:42"])
    assert replayed["id"] == first["id"]
    assert replayed["status"] == "accepted"
    assert issues.snapshot(directory)["revision"] == revision
    assert ledger(bridge, repo)["43"]["blocked_by"] == ["17", "42"]
    assert bridge.plan_revision(repo, "proposals")["revision"] == base + 2


def test_a_crashed_approval_lands_after_another_acceptance(
    bridge, repo, paired
):
    base = applied(bridge, repo)
    directory = bridge.project(repo)[1]
    held = proposed(bridge, paired["lanes"]["claude"], base, add=["42:60"])
    before = (directory / plan.PLAN).read_text()
    bridge.plan_revision(repo, "approve", held["id"])
    (directory / plan.PLAN).write_text(before)
    proposed(bridge, paired["lanes"]["codex"], base, remove=["44:43"])
    approved = bridge.plan_revision(repo, "approve", held["id"])
    assert approved["status"] == "accepted"
    assert ledger(bridge, repo)["42"]["blocked_by"] == ["17", "60"]


def test_removing_an_edge_authorizes_nothing(bridge, repo, paired):
    base = applied(bridge, repo, BARE)
    directory = bridge.project(repo)[1]
    snapshot = issues.snapshot(directory)
    snapshot["issues"]["42"]["execution"]["authorized"] = False
    snapshot["revision"] += 1
    state.write_json(directory / "issues.json", snapshot)
    record = proposed(bridge, paired["lanes"]["claude"], base, remove=["42:17"])
    assert record["status"] == "pending"
    approved = bridge.plan_revision(repo, "approve", record["id"])
    assert approved["status"] == "accepted"
    after = ledger(bridge, repo)["42"]
    assert after["blocked_by"] == []
    assert not lifecycle.state(after)["authorized"]


def test_a_busy_ledger_leaves_an_approval_open(bridge, repo, paired):
    base = applied(bridge, repo)
    directory = bridge.project(repo)[1]
    held = proposed(bridge, paired["lanes"]["claude"], base, add=["42:60"])
    with (
        state.lock(directory / "issues.lock"),
        pytest.raises(state.LockBusy),
    ):
        bridge.plan_revision(repo, "approve", held["id"])
    [still] = bridge.plan_revision(repo, "proposals")["proposals"]
    assert still["status"] == "pending"
    assert "60" not in ledger(bridge, repo)


def test_a_lane_cannot_propose_from_a_peer_worktree(
    bridge, repo, paired, monkeypatch
):
    base = applied(bridge, repo)
    monkeypatch.chdir(paired["lanes"]["codex"])
    with pytest.raises(BridgeError, match="cannot propose from"):
        proposed(bridge, paired["lanes"]["claude"], base, add=["43:42"])
    assert ledger(bridge, repo)["43"]["blocked_by"] == ["17"]
    record = proposed(bridge, paired["lanes"]["codex"], base, add=["43:42"])
    assert record["by"] == "codex"


def test_a_replay_after_retention_drops_the_record_changes_nothing(
    bridge, repo, paired
):
    base = applied(bridge, repo)
    lane = paired["lanes"]["claude"]
    record = proposed(bridge, lane, base, add=["43:42"])
    directory = bridge.project(repo)[1]
    history = json.loads((directory / plan.PLAN).read_text())
    del history["proposals"][record["id"]]
    state.write_json(directory / plan.PLAN, history)
    again = proposed(bridge, lane, base, add=["43:42"])
    assert again["replayed"]
    assert again["status"] == "accepted"
    assert bridge.plan_revision(repo, "proposals")["revision"] == base + 1
    assert ledger(bridge, repo)["43"]["blocked_by"] == ["17", "42"]
