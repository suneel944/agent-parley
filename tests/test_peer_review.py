"""Checks the verdict one lane records against another lane's report."""

from pathlib import Path

import pytest

from agent_parley import attachments, cli, metrics, store, tables
from agent_parley.state import BridgeError


def reported(directory, name):
    """Returns the identifier of a lane's latest recorded report."""
    return [
        record["id"]
        for record in metrics.report_records(directory, name)
        if record.get("kind") == "report"
    ][-1]


def lane_record(snapshot, name):
    """Returns one participant's record from a status reading."""
    return [
        record
        for project in snapshot["projects"]
        for record in project["participants"]
        if record["participant"] == name
    ][0]


def peer(bridge, paired, name):
    """Authenticates one participant against the coordination store."""
    store.initialize(bridge.home)
    token = store.register(bridge.home, paired["root"], name)
    return store.authenticate(bridge.home, token["registration_token"])


@pytest.fixture
def ready(bridge, repo, paired):
    """Leaves one ready report by claude for codex to review."""
    claude = Path(paired["lanes"]["claude"])
    bridge.report(claude, "ready", "Parser built", "", "12 tests passed")
    directory = claude.parent
    return {
        "claude": claude,
        "codex": Path(paired["lanes"]["codex"]),
        "directory": directory,
        "report": reported(directory, "claude"),
    }


def test_the_author_of_a_report_cannot_record_its_own_verdict(bridge, ready):
    with pytest.raises(BridgeError, match="another lane records it"):
        bridge.review_report(
            ready["claude"], ready["report"], "pass", "I read my own diff"
        )
    assert metrics.latest_review(ready["directory"], "claude") is None


def test_a_verdict_names_a_recorded_report_and_a_known_outcome(bridge, ready):
    with pytest.raises(BridgeError, match="pass or fail"):
        bridge.review_report(
            ready["codex"], ready["report"], "maybe", "Ran the suite"
        )
    with pytest.raises(BridgeError, match="--evidence"):
        bridge.review_report(ready["codex"], ready["report"], "pass", "   ")
    with pytest.raises(BridgeError, match="No report absent"):
        bridge.review_report(ready["codex"], "absent", "pass", "Ran it")


def test_a_peer_verdict_is_kept_beside_the_report_it_judges(bridge, ready):
    recorded = bridge.review_report(
        ready["codex"], ready["report"], "fail", "The parser drops a token"
    )
    assert recorded["reviewer"] == "codex"
    assert recorded["author"] == "claude"
    assert recorded["report_id"] == ready["report"]
    assert recorded["verdict"] == "fail"
    assert recorded["attachment"] is None

    latest = metrics.latest_review(ready["directory"], "claude")
    assert latest == recorded
    assert metrics.latest_review(ready["directory"], "claude", "absent") is None

    shown = bridge.show_report(ready["claude"], ready["report"], False)
    assert shown["review"]["verdict"] == "fail"
    printed = cli.shown_record(shown)
    assert "Peer review: fail by codex" in printed
    assert "not independent verification" in printed
    assert "The parser drops a token" in printed


def test_long_review_evidence_is_attached_as_a_report_evidence_is(
    bridge, ready
):
    body = "byte" * 2000
    recorded = bridge.review_report(
        ready["codex"], ready["report"], "pass", body
    )
    reference = recorded["attachment"]
    assert reference and reference.startswith("report-")
    assert len(recorded["evidence"].encode()) <= metrics.MAX_REPORT_BYTES
    assert attachments.body(ready["directory"], reference, "claude") == body
    assert attachments.body(ready["directory"], reference, "codex") == body


def test_the_verdict_reaches_status_and_its_table(bridge, ready):
    bridge.review_report(
        ready["codex"], ready["report"], "pass", "Ran make check"
    )
    record = lane_record(bridge.status_snapshot(), "claude")
    review = record["review"]
    assert review["verdict"] == "pass"
    assert review["reviewer"] == "codex"
    assert review["report_id"] == ready["report"]
    assert review["evidence"] == "Ran make check"
    assert review["independent_verification"] is False
    assert review["age_seconds"] is not None

    row = tables.status_row(record, ())
    assert row[tables.STATUS_COLUMNS.index("REVIEW")] == "pass"
    unreviewed = lane_record(bridge.status_snapshot(), "codex")
    assert unreviewed["review"] is None
    assert (
        tables.status_row(unreviewed, ())[tables.STATUS_COLUMNS.index("REVIEW")]
        == "-"
    )


def test_the_served_tool_records_a_verdict_for_the_calling_lane(
    bridge, paired, ready
):
    codex = peer(bridge, paired, "codex")
    served = store.call(
        bridge.home,
        codex,
        "review_report",
        {
            "report_id": ready["report"],
            "verdict": "pass",
            "evidence": "Rebuilt and ran the suite",
        },
    )
    assert served["reviewer"] == "codex"
    assert served["verdict"] == "pass"
    assert metrics.latest_review(ready["directory"], "claude") == served

    claude = peer(bridge, paired, "claude")
    with pytest.raises(BridgeError, match="another lane records it"):
        store.call(
            bridge.home,
            claude,
            "review_report",
            {
                "report_id": ready["report"],
                "verdict": "pass",
                "evidence": "I read my own diff",
            },
        )
    with pytest.raises(BridgeError, match="report_id"):
        store.call(
            bridge.home,
            codex,
            "review_report",
            {"report_id": "", "verdict": "pass", "evidence": "Ran it"},
        )
