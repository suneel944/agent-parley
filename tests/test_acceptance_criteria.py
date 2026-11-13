"""Checks acceptance criteria from claim to dispatch, report and resolve."""

import sys
import time
from pathlib import Path

import pytest

from agent_parley import cli, criteria, forge, issues, launch, protocol
from agent_parley.state import BridgeError
from tests.test_claim_termination import escalated, evidence, registered

FEATURE = """### Problem

Lanes guess what done means.

### Boundaries and acceptance criteria

Criteria, one per line:

- The parser reads both headings.
- [ ] The dispatch numbers each criterion.
  - a nested note is not a criterion
* A report missing a line is refused.

### Alternatives considered

- Not a criterion either.
"""

BUG = """## Steps to reproduce

1. Run it.

## Expected and actual behavior:

1. The report names the missing criterion.
2) The resolve records the forge outcome.
"""

THREE = [
    "The parser reads both headings.",
    "The dispatch numbers each criterion.",
    "A report missing a line is refused.",
]


def stated(monkeypatch, body):
    """Makes the forge return one fixed issue body at claim time."""
    monkeypatch.setattr(forge, "issue_text", lambda repo, number: (None, body))


def record(lane, number="1"):
    """Reads one issue's ledger record."""
    return issues.snapshot(lane.parent)["issues"][number]


def test_the_parser_reads_the_feature_heading():
    assert criteria.parse(FEATURE) == THREE


def test_the_parser_reads_the_bug_heading():
    assert criteria.parse(BUG) == [
        "The report names the missing criterion.",
        "The resolve records the forge outcome.",
    ]


def test_a_body_without_either_section_states_no_criteria():
    assert criteria.parse("### Problem\n\n- Something is wrong.\n") == []
    assert criteria.parse("") == []


def test_evidence_lines_match_by_number_and_a_gap_is_named():
    assert criteria.matched(
        THREE,
        "1: pytest -k parse: 3 passed\n"
        "2. test_dispatch\n3: n/a, no report path",
    ) == [
        "pytest -k parse: 3 passed",
        "test_dispatch",
        "n/a, no report path",
    ]
    with pytest.raises(BridgeError) as refused:
        criteria.matched(THREE, "1: ok\n3: ok")
    assert "2. The dispatch numbers each criterion." in str(refused.value)
    assert "1. The parser" not in str(refused.value)


def test_one_client_call_reads_both_title_and_body(monkeypatch):
    calls = []

    def answer(args, timeout):
        calls.append(args)
        return '{"title": "T", "body": "B"}'

    monkeypatch.setattr(forge, "_implementation", lambda repo: "github")
    monkeypatch.setattr(forge, "_reachable", lambda repo: "o/r")
    monkeypatch.setattr(forge, "_run", answer)
    assert forge.issue_text(Path("."), "7") == ("T", "B")
    assert len(calls) == 1
    assert calls[0][-2:] == ["--json", "title,body"]
    monkeypatch.setattr(forge, "_run", lambda args, timeout: None)
    assert forge.issue_text(Path("."), "7") == (None, None)


def test_a_fenced_heading_neither_ends_the_section_nor_is_a_criterion():
    body = (
        "### Boundaries and acceptance criteria\n\n"
        "- First.\n"
        "```sh\n# run tests\n- not a criterion\n```\n"
        "~~~\n## Still fenced\n~~~\n"
        "- Second.\n\n"
        "### Alternatives considered\n\n- Not one.\n"
    )
    assert criteria.parse(body) == ["First.", "Second."]


def test_the_dispatch_quotes_the_criteria_and_instructs_outside_the_quote():
    task = launch.issue_task(
        "7",
        {"title": "Criteria", "body": FEATURE, "comments": [], "linked": {}},
        {"verify": ["make", "check"]},
        THREE,
    )
    start, end = task.index(launch.QUOTE_START), task.index(launch.QUOTE_END)
    numbered = task.index("\n1. The parser reads both headings.")
    assert start < numbered < end
    assert start < task.index("3. A report missing a line is refused.") < end
    instruction = task.index(f"{protocol.cli_command()} report --state ready")
    assert instruction < start
    assert task.index(criteria.FORMAT) < start
    assert task.count(criteria.FORMAT) == 1


def test_criteria_without_a_forge_reading_still_sit_inside_a_quote():
    task = launch.issue_task("7", None, {"verify": []}, THREE)
    start, end = task.index(launch.QUOTE_START), task.index(launch.QUOTE_END)
    assert start < task.index("1. The parser reads both headings.") < end
    assert task.index(criteria.FORMAT) < start


def test_an_issue_without_criteria_asks_the_lane_to_state_its_own():
    task = launch.issue_task("7", None, {"verify": []}, [])
    assert "Issue #7 states no acceptance criteria" in task
    assert "criteria you will prove" in task
    assert launch.QUOTE_START not in task


def test_an_unread_issue_body_is_named_apart_from_no_criteria():
    task = launch.issue_task("7", None, {"verify": []}, None)
    assert "forge issue body of #7 could not be read" in task
    assert "states no acceptance criteria" not in task
    assert "criteria you will prove" in task


def test_the_claim_records_the_criteria_and_a_ready_report_must_prove_them(
    bridge, paired, monkeypatch
):
    stated(monkeypatch, FEATURE)
    lane = Path(paired["lanes"]["claude"])
    claimed = bridge.issue(lane, "claim", "1")
    assert claimed["criteria"] == THREE
    with pytest.raises(BridgeError, match="criterion 3. A report missing"):
        bridge.report(lane, "ready", "Done.", "", "1: ok\n2: ok")
    assert record(lane)["execution"]["state"] != "ready"
    bridge.report(
        lane, "ready", "Done.", "", "Ran it.\n1: a\n2: test_b\n3: n/a"
    )
    after = record(lane)
    assert after["execution"]["state"] == "ready"
    assert after["execution"]["criteria_evidence"] == ["a", "test_b", "n/a"]


def test_an_issue_without_criteria_reports_as_before(
    bridge, paired, monkeypatch
):
    stated(monkeypatch, "No template here.")
    lane = Path(paired["lanes"]["claude"])
    assert bridge.issue(lane, "claim", "1")["criteria"] == []
    bridge.report(lane, "ready", "Done.", "", "make check passed")
    assert record(lane)["execution"]["state"] == "ready"
    assert "criteria_evidence" not in record(lane)["execution"]


def test_resolve_on_an_ended_pull_request_records_the_forge_outcome(
    bridge, repo, paired, monkeypatch
):
    registered(bridge, paired)
    stated(monkeypatch, FEATURE)
    lane = Path(paired["lanes"]["claude"])
    bridge.issue(lane, "claim", "1")
    escalated(bridge, lane, monkeypatch)
    evidence(monkeypatch, "MERGED", time.time())
    with pytest.raises(BridgeError, match="criterion 3"):
        bridge.issue_resolve(repo, "1", proof="1: a\n2: b")
    result = bridge.issue_resolve(repo, "1", reason="holder is silent")
    assert result["outcome"] == "complete"
    outcome = (
        "forge outcome: pull request #1198 merged on "
        f"{result['evidence']['branch']} (https://example.invalid/pull/1198)"
    )
    assert result["criteria"] == THREE
    assert result["criteria_evidence"] == [outcome] * 3
    assert record(lane)["execution"]["criteria_evidence"] == [outcome] * 3


def test_resolve_takes_the_operators_complete_evidence(
    bridge, repo, paired, monkeypatch
):
    registered(bridge, paired)
    stated(monkeypatch, BUG)
    lane = Path(paired["lanes"]["claude"])
    bridge.issue(lane, "claim", "1")
    escalated(bridge, lane, monkeypatch)
    evidence(monkeypatch, "CLOSED", time.time())
    result = bridge.issue_resolve(
        repo, "1", release=True, proof="1: seen in #9\n2: n/a, released"
    )
    assert result["outcome"] == "release"
    assert result["criteria_evidence"] == ["seen in #9", "n/a, released"]


def test_resolve_refuses_evidence_for_an_issue_without_criteria(
    bridge, repo, paired, monkeypatch
):
    registered(bridge, paired)
    stated(monkeypatch, None)
    lane = Path(paired["lanes"]["claude"])
    bridge.issue(lane, "claim", "1")
    escalated(bridge, lane, monkeypatch)
    evidence(monkeypatch, "MERGED", time.time())
    with pytest.raises(BridgeError, match="no acceptance criteria"):
        bridge.issue_resolve(repo, "1", proof="1: a")
    assert bridge.issue_resolve(repo, "1")["criteria_evidence"] == []


def main(monkeypatch, bridge, *arguments):
    """Runs one CLI command against the test home and returns its status."""
    monkeypatch.setattr(
        sys, "argv", ["agent-parley", "--home", str(bridge.home), *arguments]
    )
    return cli.main()


def test_a_three_criteria_claim_refuses_a_partial_report_end_to_end(
    bridge, service, paired, monkeypatch, capsys
):
    stated(monkeypatch, FEATURE)
    lane = str(paired["lanes"]["claude"])
    assert main(monkeypatch, bridge, "issue", "claim", "1", "--repo", lane) == 0
    capsys.readouterr()
    report = ["report", "--repo", lane, "--state", "ready", "--summary", "Done"]
    two = ["--evidence", "1: test_parse passed", "--evidence", "2: n/a"]
    assert main(monkeypatch, bridge, *report, *two) == 1
    refused = capsys.readouterr().err
    assert "3. A report missing a line is refused." in refused
    assert "1. The parser" not in refused
    three = [*two, "--evidence", "3: test_refusal passed"]
    assert main(monkeypatch, bridge, *report, *three) == 0
    capsys.readouterr()
    assert main(monkeypatch, bridge, "issue", "show", "1", "--repo", lane) == 0
    shown = capsys.readouterr().out
    assert "Acceptance criteria:" in shown
    assert "  3. A report missing a line is refused." in shown
    assert "evidence: test_refusal passed" in shown
