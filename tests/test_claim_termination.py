"""Exercises the terminating transition for an observed-complete claim."""

import json
import time
from pathlib import Path

import pytest

from agent_parley import forge, issues, lifecycle, problems, store, supervision
from agent_parley.state import BridgeError

HOUR = 3600.0


def registered(bridge, paired):
    """Registers both fixture participants in the coordination store."""
    store.initialize(bridge.home)
    return {
        name: store.authenticate(
            bridge.home,
            store.register(bridge.home, paired["root"], name)[
                "registration_token"
            ],
        )
        for name in ("claude", "codex")
    }


def send(bridge, actor, recipient, key="handoff"):
    """Sends one explicit message from a lane to its peer."""
    return store.call(
        bridge.home,
        actor,
        "send_message",
        {
            "to": [recipient],
            "subject": "Completion",
            "body_md": "Merged as abcdef1234567",
            "idempotency_key": key,
            "ack_required": True,
        },
    )


def completion(monkeypatch, state, created):
    """Reports one fixed pull-request observation to the supervisor."""
    monkeypatch.setattr(
        supervision.forge,
        "branch_completion",
        lambda *args: None if state is None else (state, created),
    )


def evidence(monkeypatch, state, created, commit="abcdef1234567"):
    """Reports one fixed pull-request reading to the operator command."""
    monkeypatch.setattr(
        forge,
        "branch_evidence",
        lambda repo, branch: (
            None
            if state is None
            else {
                "branch": branch,
                "state": state,
                "created_at": created,
                "commit": commit,
                "pull_request": 1198,
                "url": "https://example.invalid/pull/1198",
            }
        ),
    )


@pytest.fixture
def claimed(bridge, paired):
    """Claims one issue for the Claude lane and reports its state paths."""
    registered(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    bridge.issue(lane, "claim", "1")
    return lane


def record(lane):
    """Reads the claimed issue record."""
    return issues.snapshot(lane.parent)["issues"]["1"]


def unanswered(lane, windows):
    """Ages the completion reminder by whole default inactive windows."""
    path = lane.parent / "issues.json"
    ledger = json.loads(path.read_text())
    prompt = ledger["issues"]["1"]["handoff_prompt"]
    prompt["created"] -= windows * supervision.DEFAULTS["inactive_after"]
    path.write_text(json.dumps(ledger))


def escalated(bridge, lane, monkeypatch, state="MERGED"):
    """Drives the supervisor until the claim reads as unresolved."""
    completion(monkeypatch, state, time.time() + 1)
    supervision.poll(bridge.home, lane.parent)
    unanswered(lane, 2)
    supervision.poll(bridge.home, lane.parent)
    return record(lane)


def test_a_merged_branch_and_silence_escalate_exactly_once(
    bridge, claimed, monkeypatch
):
    completion(monkeypatch, "MERGED", time.time() + 1)
    for _ in range(4):
        supervision.poll(bridge.home, claimed.parent)
    assert record(claimed).get("unresolved_completion") is None
    assert record(claimed)["handoff_prompt"]["reminders"] == 1
    unanswered(claimed, 1)
    supervision.poll(bridge.home, claimed.parent)
    assert record(claimed).get("unresolved_completion") is None
    unanswered(claimed, 1)
    supervision.poll(bridge.home, claimed.parent)
    marker = record(claimed)["unresolved_completion"]
    assert marker["holder"] == "claude"
    assert marker["state"] == "MERGED"
    assert marker["reminders"] == 3
    assert "unanswered" in marker["reason"]
    assert record(claimed)["owner"] == "claude"
    revision = issues.snapshot(claimed.parent)["revision"]
    supervision.poll(bridge.home, claimed.parent)
    supervision.poll(bridge.home, claimed.parent)
    assert issues.snapshot(claimed.parent)["revision"] == revision
    assert record(claimed)["unresolved_completion"] == marker


def test_an_answering_holder_clears_its_own_escalation(
    bridge, paired, claimed, monkeypatch
):
    actors = registered(bridge, paired)
    completion(monkeypatch, "MERGED", time.time() + 1)
    supervision.poll(bridge.home, claimed.parent)
    supervision.poll(bridge.home, claimed.parent)
    send(bridge, actors["claude"], "codex")
    supervision.poll(bridge.home, claimed.parent)
    supervision.poll(bridge.home, claimed.parent)
    assert record(claimed)["handoff_prompt"]["responded_at"]
    assert record(claimed).get("unresolved_completion") is None
    assert record(claimed)["owner"] == "claude"


def test_an_open_pull_request_never_escalates(bridge, claimed, monkeypatch):
    completion(monkeypatch, "OPEN", time.time() + 1)
    for _ in range(4):
        supervision.poll(bridge.home, claimed.parent)
    assert record(claimed).get("unresolved_completion") is None


def test_the_escalation_reaches_status_and_problems(
    bridge, repo, claimed, monkeypatch
):
    escalated(bridge, claimed, monkeypatch)
    lanes = bridge.status_snapshot()["projects"][0]["participants"]
    claims = {
        item["issue"]: item
        for lane in lanes
        for item in lane["claims"]
        if lane["participant"] == "claude"
    }
    assert claims[1]["unresolved"]
    assert claims[1]["branch_state"] == "MERGED"
    listed = [
        row
        for row in bridge.problems()
        if row["condition"] == problems.UNRESOLVED
    ]
    assert len(listed) == 1
    assert listed[0]["participant"] == "claude"
    assert listed[0]["command"].startswith("agent-parley issue resolve 1")


def test_the_operator_resolves_a_merged_claim_with_its_evidence(
    bridge, repo, claimed, monkeypatch
):
    escalated(bridge, claimed, monkeypatch)
    evidence(monkeypatch, "MERGED", time.time())
    result = bridge.issue_resolve(repo, "1", reason="holder is silent")
    assert result["outcome"] == "complete"
    assert result["holder"] == "claude"
    assert result["owner"] is None
    assert result["state"] == lifecycle.COMPLETE
    after = record(claimed)
    assert after["owner"] is None
    assert after.get("unresolved_completion") is None
    resolution = after["resolution"]
    assert resolution["actor"] == "operator"
    assert resolution["reason"] == "holder is silent"
    assert resolution["evidence"]["commit"] == "abcdef1234567"
    assert resolution["evidence"]["pull_request"] == 1198
    assert resolution["evidence"]["observed_at"]
    assert after["history"][-1]["action"] == "resolve"
    assert after["history"][-1]["actor"] == "operator"
    assert lifecycle.state(after)["commit"] == "abcdef1234567"


def test_a_claim_closed_by_a_per_issue_pull_request_is_resolved(
    bridge, repo, claimed, monkeypatch
):
    reading = {
        "state": "MERGED",
        "closed_at": time.time() + 1,
        "pull_request": 1328,
        "url": "https://example.invalid/pull/1328",
        "branch": "refactor/1-replay-package",
        "commit": "abcdef1234567",
    }
    monkeypatch.setattr(forge, "issue_completion", lambda *args: reading)
    completion(monkeypatch, None, 0.0)
    evidence(monkeypatch, None, 0.0)
    supervision.poll(bridge.home, claimed.parent)
    unanswered(claimed, 2)
    supervision.poll(bridge.home, claimed.parent)
    marker = record(claimed)["unresolved_completion"]
    assert marker["branch"] == "refactor/1-replay-package"
    result = bridge.issue_resolve(repo, "1", reason="landed per issue")
    assert result["outcome"] == "complete"
    resolution = record(claimed)["resolution"]
    assert resolution["evidence"]["pull_request"] == 1328
    assert resolution["evidence"]["branch"] == "refactor/1-replay-package"


def test_a_resolution_is_not_an_owner_filed_completion(
    bridge, repo, claimed, monkeypatch
):
    escalated(bridge, claimed, monkeypatch)
    evidence(monkeypatch, "MERGED", time.time())
    bridge.issue_resolve(repo, "1")
    actions = [entry["action"] for entry in record(claimed)["history"]]
    assert "complete" not in actions
    assert actions[-1] == "resolve"
    assert record(claimed)["resolution"]["holder"] == "claude"


def test_a_claim_with_no_ended_pull_request_is_refused(
    bridge, repo, claimed, monkeypatch
):
    escalated(bridge, claimed, monkeypatch)
    evidence(monkeypatch, None, 0.0)
    with pytest.raises(BridgeError, match="no evidence"):
        bridge.issue_resolve(repo, "1")
    evidence(monkeypatch, "OPEN", time.time())
    with pytest.raises(BridgeError, match="no evidence"):
        bridge.issue_resolve(repo, "1")
    assert record(claimed)["owner"] == "claude"


def test_evidence_that_predates_the_claim_is_refused(
    bridge, repo, claimed, monkeypatch
):
    escalated(bridge, claimed, monkeypatch)
    evidence(monkeypatch, "MERGED", time.time() - HOUR)
    with pytest.raises(BridgeError, match="predates"):
        bridge.issue_resolve(repo, "1")
    assert record(claimed)["owner"] == "claude"


def test_a_live_holder_is_never_resolved_out_from_under_it(
    bridge, repo, paired, claimed, monkeypatch
):
    actors = registered(bridge, paired)
    completion(monkeypatch, "MERGED", time.time() + 1)
    supervision.poll(bridge.home, claimed.parent)
    send(bridge, actors["claude"], "codex")
    supervision.poll(bridge.home, claimed.parent)
    evidence(monkeypatch, "MERGED", time.time())
    with pytest.raises(BridgeError, match="no unresolved completion"):
        bridge.issue_resolve(repo, "1")
    assert record(claimed)["owner"] == "claude"
    assert lifecycle.state(record(claimed))["state"] == lifecycle.RUNNING


def test_a_closed_unmerged_pull_request_requires_the_release_outcome(
    bridge, repo, claimed, monkeypatch
):
    escalated(bridge, claimed, monkeypatch, state="CLOSED")
    evidence(monkeypatch, "CLOSED", time.time(), commit="")
    with pytest.raises(BridgeError, match="closed without"):
        bridge.issue_resolve(repo, "1")
    result = bridge.issue_resolve(repo, "1", release=True)
    assert result["outcome"] == "release"
    assert result["state"] == lifecycle.QUEUED
    after = record(claimed)
    assert after["owner"] is None
    assert after["resolution"]["evidence"]["commit"] == ""
    assert issues.released(after)
    bridge.issue(claimed, "claim", "1")
    assert record(claimed)["owner"] == "claude"


def test_a_resolved_issue_frees_the_issues_that_waited_on_it(
    bridge, repo, paired, claimed, monkeypatch
):
    peer = Path(paired["lanes"]["codex"])
    bridge.issue(peer, "claim", "2")
    bridge.issue(peer, "block", "2", on="1")
    escalated(bridge, claimed, monkeypatch)
    evidence(monkeypatch, "MERGED", time.time())
    bridge.issue_resolve(repo, "1")
    assert issues.snapshot(peer.parent)["issues"]["2"]["blocked_by"] == []


def test_no_lane_can_resolve_another_lanes_claim(bridge, paired, claimed):
    peer = Path(paired["lanes"]["codex"])
    with pytest.raises(BridgeError, match="Only claude"):
        bridge.issue(peer, "resolve", "1")
    with pytest.raises(BridgeError, match="Only claude"):
        bridge.issue(peer, "release", "1")
    with pytest.raises(BridgeError, match="Unknown issue action"):
        bridge.issue(claimed, "resolve", "1")
    assert record(claimed)["owner"] == "claude"


def test_the_reminder_threshold_is_a_project_setting(
    bridge, repo, claimed, monkeypatch
):
    directory = claimed.parent
    manifest = json.loads((directory / "project.json").read_text())
    manifest["supervision"] = {"completion_reminders": 1}
    (directory / "project.json").write_text(json.dumps(manifest))
    completion(monkeypatch, "MERGED", time.time() + 1)
    supervision.poll(bridge.home, directory)
    assert record(claimed)["unresolved_completion"]["reminders"] == 1


@pytest.mark.parametrize("value", [0, 101, "three", 2.5])
def test_an_invalid_reminder_threshold_is_refused(value):
    with pytest.raises(BridgeError, match="completion_reminders"):
        supervision.settings({"completion_reminders": value})


def test_the_forge_evidence_carries_the_merge_commit(monkeypatch, tmp_path):
    monkeypatch.setattr(forge, "_reachable", lambda repo: "owner/name")
    monkeypatch.setattr(
        forge,
        "_run",
        lambda *args: json.dumps(
            [
                {
                    "state": "MERGED",
                    "createdAt": "2026-09-01T00:00:00Z",
                    "mergeCommit": {"oid": "e36bf991"},
                    "number": 1198,
                    "url": "https://example.invalid/pull/1198",
                },
                {"state": "CLOSED", "createdAt": "2026-01-01T00:00:00Z"},
            ]
        ),
    )
    reading = forge.branch_evidence(tmp_path, "lane")
    assert reading["state"] == "MERGED"
    assert reading["commit"] == "e36bf991"
    assert reading["pull_request"] == 1198
    assert reading["branch"] == "lane"
    monkeypatch.setattr(forge, "_run", lambda *args: "[]")
    assert forge.branch_evidence(tmp_path, "lane") is None
