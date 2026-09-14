"""Checks review measurements against real retained coordination records."""

import json
import time
from pathlib import Path

from agent_parley import checkpoints, evidence, store


def test_claim_evidence_excludes_older_denials_and_private_mail(bridge, paired):
    lane = Path(paired["lanes"]["claude"])
    log = lane.parent / "claude-events.jsonl"
    log.write_text(
        json.dumps(
            {"ts": time.time() - 60, "decision": "deny", "reason_class": "old"}
        )
        + "\n"
    )
    bridge.issue(lane, "claim", "1")
    checkpoints.record(
        lane.parent,
        "claude",
        {"hook_event_name": "PreToolUse"},
        checkpoints.Reason.BRANCH_SWITCH,
        {"hookSpecificOutput": {"permissionDecision": "deny"}},
    )
    store.initialize(bridge.home)
    actors = {}
    for name in ("claude", "codex"):
        registration = store.register(bridge.home, paired["root"], name)
        actors[name] = store.authenticate(
            bridge.home, registration["registration_token"]
        )
    for name in ("codex", "claude"):
        store.call(
            bridge.home,
            actors[name],
            "file_reservation_paths",
            {"paths": ["shared.txt"]},
        )
    record = evidence.collect(
        bridge.home, lane.parent, paired, "claude", "a" * 40
    )
    assert record["denials"] == {"branch_switch": 1}
    assert record["reservation_conflicts"] == 1
    assert record["reservations"] == []
    body = evidence.publish(lane.parent, record)
    assert "No verification command configured" in body
    assert "branch_switch: 1" in body
    assert "Recorded conflicting requests: 1" in body
    exported = lane.parent / ("review-" + "a" * 40 + "-events.jsonl")
    assert len(exported.read_text().splitlines()) == 1
    assert "registration_token" not in exported.read_text()


REVIEWED = "## Reviewer notes\n\nThe parser needs another look.\n"


def rendered(commit):
    """Renders evidence the way publish does, heading included."""
    return f"{evidence.SECTION_HEADING}\n\nCommit: `{commit}`.\n"


def test_refresh_replaces_only_the_delimited_evidence_section():
    body = (
        "## Problem and result\n\nOriginal report.\n\n"
        + evidence.section(rendered("old"))
        + "\n"
        + REVIEWED
    )
    updated = evidence.refresh(body, rendered("new"))
    assert "Original report." in updated
    assert REVIEWED in updated
    assert "Commit: `new`." in updated
    assert "Commit: `old`." not in updated
    assert updated.count(evidence.SECTION_START) == 1
    assert updated.count(evidence.SECTION_HEADING) == 1


def test_refresh_replaces_an_undelimited_legacy_evidence_section():
    body = (
        "## Problem and result\n\nOriginal report.\n\n"
        f"{evidence.SECTION_HEADING}\n\nCommit: `old`. Stale figures.\n"
    )
    updated = evidence.refresh(body, rendered("new"))
    assert "Original report." in updated
    assert "Stale figures." not in updated
    assert "Commit: `new`." in updated
    assert updated.count(evidence.SECTION_HEADING) == 1
    assert updated.count(evidence.SECTION_START) == 1


def test_refresh_appends_when_no_evidence_section_exists():
    updated = evidence.refresh(
        "## Problem and result\n\nHand written body.\n", rendered("new")
    )
    assert "Hand written body." in updated
    assert updated.endswith(evidence.SECTION_END + "\n")
    assert updated.count(evidence.SECTION_START) == 1


def test_refresh_is_idempotent_across_repeated_pushes():
    body = evidence.refresh("Body.\n", rendered("one"))
    once = evidence.refresh(body, rendered("two"))
    twice = evidence.refresh(once, rendered("two"))
    assert once == twice
    assert "Commit: `one`." not in twice
