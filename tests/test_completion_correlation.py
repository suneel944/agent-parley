"""Checks that completion reminders follow the current claim, not a branch."""

import json
import time
from pathlib import Path

import pytest

from agent_parley import forge, issues, store, supervision

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


@pytest.fixture
def claimed(bridge, paired):
    """Claims one issue for the Claude lane and reports its state paths."""
    registered(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    bridge.issue(lane, "claim", "1")
    return lane


def completion(monkeypatch, state, created):
    """Reports one fixed pull-request observation to the supervisor."""
    monkeypatch.setattr(
        supervision.forge,
        "branch_completion",
        lambda *args: None if state is None else (state, created),
    )


def prompt(lane):
    """Reads any completion reminder recorded against the claimed issue."""
    return issues.snapshot(lane.parent)["issues"]["1"].get("handoff_prompt")


def test_an_older_merged_pull_request_never_finishes_a_newer_claim(
    bridge, claimed, monkeypatch
):
    completion(monkeypatch, "MERGED", time.time() - HOUR)
    supervision.poll(bridge.home, claimed.parent)
    assert prompt(claimed) is None
    assert issues.snapshot(claimed.parent)["issues"]["1"]["owner"] == "claude"


def test_a_pull_request_merged_during_the_claim_reminds_the_holder(
    bridge, claimed, monkeypatch
):
    completion(monkeypatch, "MERGED", time.time() + 1)
    supervision.poll(bridge.home, claimed.parent)
    assert prompt(claimed)["trigger"] == "pull request ended"
    assert issues.snapshot(claimed.parent)["issues"]["1"]["owner"] == "claude"


def test_a_closed_unmerged_pull_request_still_reminds_the_holder(
    bridge, claimed, monkeypatch
):
    completion(monkeypatch, "CLOSED", time.time() + 1)
    supervision.poll(bridge.home, claimed.parent)
    assert prompt(claimed)["trigger"] == "pull request ended"


def test_an_open_pull_request_never_reminds_the_holder(
    bridge, claimed, monkeypatch
):
    completion(monkeypatch, "OPEN", time.time() + 1)
    supervision.poll(bridge.home, claimed.parent)
    assert prompt(claimed) is None


def test_an_unreachable_forge_never_reminds_the_holder(
    bridge, claimed, monkeypatch
):
    completion(monkeypatch, None, 0.0)
    supervision.poll(bridge.home, claimed.parent)
    assert prompt(claimed) is None


def test_the_newest_pull_request_decides_a_reused_branch(monkeypatch, tmp_path):
    monkeypatch.setattr(forge, "_reachable", lambda repo: "owner/name")
    monkeypatch.setattr(
        forge,
        "_run",
        lambda *args: json.dumps(
            [
                {"state": "MERGED", "createdAt": "2026-01-01T00:00:00Z"},
                {"state": "OPEN", "createdAt": "2026-09-01T00:00:00Z"},
                {"state": "CLOSED", "createdAt": "2026-05-01T00:00:00Z"},
            ]
        ),
    )
    state, created = forge.branch_completion(tmp_path, "lane")
    assert state == "OPEN"
    assert created == forge._epoch("2026-09-01T00:00:00Z")


def test_a_branch_without_any_pull_request_reports_nothing(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(forge, "_reachable", lambda repo: "owner/name")
    monkeypatch.setattr(forge, "_run", lambda *args: "[]")
    assert forge.branch_completion(tmp_path, "lane") is None
    monkeypatch.setattr(forge, "_run", lambda *args: "not json")
    assert forge.branch_completion(tmp_path, "lane") is None
    monkeypatch.setattr(forge, "_reachable", lambda repo: None)
    assert forge.branch_completion(tmp_path, "lane") is None


def test_the_claim_generation_starts_at_the_latest_claim_or_handoff():
    assert supervision.claimed_since({}) == 0.0
    assert (
        supervision.claimed_since(
            {
                "history": [
                    {"action": "claim", "at": 10.0},
                    {"action": "release", "at": 20.0},
                    {"action": "claim", "at": 30.0},
                    {"action": "offer", "at": 40.0},
                ]
            }
        )
        == 30.0
    )
    assert (
        supervision.claimed_since(
            {
                "history": [
                    {"action": "claim", "at": 5.0},
                    {"action": "accept", "at": 9.0},
                ]
            }
        )
        == 9.0
    )


def closing(monkeypatch, reading):
    """Reports one fixed issue reading to the supervisor."""
    monkeypatch.setattr(
        supervision.forge, "issue_completion", lambda *args: reading
    )


def closed_by(branch, state="MERGED", at=None):
    """Builds a closed-issue reading naming its closing pull request."""
    return {
        "state": state,
        "closed_at": time.time() + 1 if at is None else at,
        "pull_request": 1328,
        "url": "https://example.invalid/pull/1328",
        "branch": branch,
        "commit": "abcdef1234567",
    }


def test_an_issue_closed_from_a_non_lane_branch_is_observed_complete(
    bridge, claimed, monkeypatch
):
    completion(monkeypatch, None, 0.0)
    closing(monkeypatch, closed_by("refactor/1-replay-package"))
    ended = supervision.completed_claims(
        supervision.roster.read(claimed.parent),
        issues.snapshot(claimed.parent),
    )
    assert ended["1"]["branch"] == "refactor/1-replay-package"
    assert ended["1"]["pull_request"] == 1328
    assert ended["1"]["commit"] == "abcdef1234567"
    supervision.poll(bridge.home, claimed.parent)
    assert prompt(claimed)["trigger"] == "pull request ended"
    assert issues.snapshot(claimed.parent)["issues"]["1"]["owner"] == "claude"


def test_an_issue_closed_before_the_claim_is_not_observed_complete(
    bridge, claimed, monkeypatch
):
    completion(monkeypatch, None, 0.0)
    closing(monkeypatch, closed_by("refactor/1", at=time.time() - HOUR))
    supervision.poll(bridge.home, claimed.parent)
    assert prompt(claimed) is None


def test_a_lane_branch_merge_does_not_mark_an_open_issue(
    bridge, claimed, monkeypatch
):
    completion(monkeypatch, "MERGED", time.time() + 1)
    closing(monkeypatch, {"state": "OPEN"})
    supervision.poll(bridge.home, claimed.parent)
    assert prompt(claimed) is None


def test_an_unreachable_forge_leaves_the_claim_unchanged(
    bridge, claimed, monkeypatch
):
    completion(monkeypatch, None, 0.0)
    closing(monkeypatch, None)
    before = issues.snapshot(claimed.parent)["issues"]["1"]
    supervision.poll(bridge.home, claimed.parent)
    assert issues.snapshot(claimed.parent)["issues"]["1"] == before


def test_a_peer_lane_that_landed_the_claim_is_named_to_the_owner(
    bridge, claimed, monkeypatch
):
    peer = supervision.roster.read(claimed.parent)["participants"]["codex"]
    completion(monkeypatch, None, 0.0)
    closing(monkeypatch, closed_by(peer["branch"]))
    supervision.poll(bridge.home, claimed.parent)
    assert "codex landed the pull request" in prompt(claimed)["text"]


def test_the_issue_reading_names_its_closing_pull_request(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(forge, "_reachable", lambda repo: "owner/name")
    replies = {
        "issue": {
            "state": "CLOSED",
            "closedAt": "2026-09-20T00:00:00Z",
            "closedByPullRequestsReferences": [{"number": 1328}],
        },
        "pr": {
            "state": "MERGED",
            "number": 1328,
            "url": "https://example.invalid/pull/1328",
            "headRefName": "refactor/1216-replay",
            "mergeCommit": {"oid": "abcdef1234567"},
        },
    }
    monkeypatch.setattr(
        forge, "_run", lambda args, timeout: json.dumps(replies[args[1]])
    )
    reading = forge.issue_completion(tmp_path, "1216")
    assert reading == {
        "state": "MERGED",
        "closed_at": forge._epoch("2026-09-20T00:00:00Z"),
        "pull_request": 1328,
        "url": "https://example.invalid/pull/1328",
        "branch": "refactor/1216-replay",
        "commit": "abcdef1234567",
    }
    replies["issue"] = {"state": "OPEN"}
    assert forge.issue_completion(tmp_path, "1216") == {"state": "OPEN"}
    monkeypatch.setattr(forge, "_run", lambda *args: None)
    assert forge.issue_completion(tmp_path, "1216") is None
