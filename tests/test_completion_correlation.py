"""Checks that completion reminders follow the current claim, not a branch."""

import json
import subprocess
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
    ended_record = issues.snapshot(claimed.parent)["issues"]["1"]
    assert ended_record["owner"] is None
    assert ended_record["resolution"]["actor"] == "supervisor"


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


def test_a_per_issue_branch_is_attributed_to_the_lane_that_checked_it_out(
    bridge, claimed, monkeypatch
):
    peer = supervision.roster.read(claimed.parent)["participants"]["codex"]
    for step in (["-b", "codex/1-fix"], [peer["branch"]]):
        subprocess.run(
            ["git", "-C", peer["lane"], "checkout", "-q", *step],
            check=True,
        )
    completion(monkeypatch, None, 0.0)
    closing(monkeypatch, closed_by("codex/1-fix"))
    ended = supervision.completed_claims(
        supervision.roster.read(claimed.parent),
        issues.snapshot(claimed.parent),
    )
    assert ended["1"]["landed_by"] == "codex"
    supervision.poll(bridge.home, claimed.parent)
    assert "codex landed the pull request" in prompt(claimed)["text"]


def test_a_branch_no_lane_checked_out_is_attributed_to_nobody(
    bridge, claimed, monkeypatch
):
    completion(monkeypatch, None, 0.0)
    closing(monkeypatch, closed_by("elsewhere/1"))
    ended = supervision.completed_claims(
        supervision.roster.read(claimed.parent),
        issues.snapshot(claimed.parent),
    )
    assert "landed_by" not in ended["1"]


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


@pytest.mark.parametrize("pull", [None, "", "null", "{}", "not json"])
def test_an_unreadable_closing_pull_request_gives_no_reading(
    monkeypatch, tmp_path, pull
):
    monkeypatch.setattr(forge, "_reachable", lambda repo: "owner/name")
    issue = {
        "state": "CLOSED",
        "closedAt": "2026-09-20T00:00:00Z",
        "closedByPullRequestsReferences": [{"number": 1328}],
    }

    def run(args, timeout):
        return json.dumps(issue) if args[1] == "issue" else pull

    monkeypatch.setattr(forge, "_run", run)
    assert forge.issue_completion(tmp_path, "1216") is None


def test_a_hand_closed_issue_finds_its_pull_request_on_the_timeline(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(forge, "_reachable", lambda repo: "owner/name")

    def reference(number, body, merged="2026-09-19T00:00:00Z", home=None):
        return {
            "event": "cross-referenced",
            "source": {
                "issue": {
                    "number": number,
                    "body": body,
                    "pull_request": {"merged_at": merged},
                    "repository": {"full_name": home or "owner/name"},
                }
            },
        }

    replies = {
        "issue": {
            "state": "CLOSED",
            "closedAt": "2026-09-20T00:00:00Z",
            "closedByPullRequestsReferences": [],
        },
        "api": [
            {"event": "labeled"},
            reference(1330, "Refs #1216"),
            reference(1331, "Closes #1216", merged=None),
            reference(1332, "Closes #1216", merged="2026-09-21T00:00:00Z"),
            reference(1333, "Closes #1216", home="other/name"),
            reference(1334, "Closes #12160"),
            reference(1328, "Body.\n\nCloses #1216"),
        ],
        "pr": {
            "state": "MERGED",
            "number": 1328,
            "url": "https://example.invalid/pull/1328",
            "headRefName": "fix/1216-replay",
            "mergeCommit": {"oid": "abcdef1234567"},
        },
    }
    viewed = []

    def run(args, timeout):
        if args[1] == "pr":
            viewed.append(args[3])
        return json.dumps(replies[args[1]])

    monkeypatch.setattr(forge, "_run", run)
    reading = forge.issue_completion(tmp_path, "1216")
    assert viewed == ["1328"]
    assert reading["state"] == "MERGED"
    assert reading["commit"] == "abcdef1234567"
    replies["api"] = [reference(1330, "Refs #1216")]
    reading = forge.issue_completion(tmp_path, "1216")
    assert reading["state"] == "CLOSED"
    assert reading["pull_request"] == 0


def test_a_closing_reference_past_the_first_timeline_page_is_found(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(forge, "_reachable", lambda repo: "owner/name")
    closing = {
        "event": "cross-referenced",
        "source": {
            "issue": {
                "number": 1328,
                "body": "Closes #1216",
                "pull_request": {"merged_at": "2026-09-19T00:00:00Z"},
                "repository": {"full_name": "owner/name"},
            }
        },
    }
    full = [{"event": "labeled"}] * forge.TIMELINE_PAGE
    pages = {"1": full, "2": [closing]}
    requested = []

    def run(args, timeout):
        if args[1] == "issue":
            return json.dumps(
                {
                    "state": "CLOSED",
                    "closedAt": "2026-09-20T00:00:00Z",
                    "closedByPullRequestsReferences": [],
                }
            )
        if args[1] == "api":
            page = args[2].rsplit("page=", 1)[1]
            requested.append(page)
            return json.dumps(pages.get(page, full))
        return json.dumps(
            {
                "state": "MERGED",
                "number": 1328,
                "url": "https://example.invalid/pull/1328",
                "headRefName": "fix/1216-replay",
                "mergeCommit": {"oid": "abcdef1234567"},
            }
        )

    monkeypatch.setattr(forge, "_run", run)
    reading = forge.issue_completion(tmp_path, "1216")
    assert requested == ["1", "2"]
    assert reading["state"] == "MERGED"
    pages["2"] = full
    requested.clear()
    forge.issue_completion(tmp_path, "1216")
    assert len(requested) == forge.TIMELINE_PAGES


def test_a_ready_report_on_an_observed_complete_claim_asks_for_completion(
    bridge, claimed, monkeypatch
):
    completion(monkeypatch, "MERGED", time.time() + 1)
    supervision.poll(bridge.home, claimed.parent)
    owed = bridge.report(claimed, "ready", "Parser built", "", "make check")
    assert owed.startswith("Issue #1 is observed complete")
    assert "`issue resolve 1`" in owed


def test_a_ready_report_on_an_open_claim_owes_nothing(
    bridge, claimed, monkeypatch
):
    completion(monkeypatch, "OPEN", time.time() + 1)
    supervision.poll(bridge.home, claimed.parent)
    assert bridge.report(claimed, "ready", "Parser built", "", "ok") == ""
