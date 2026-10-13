"""Checks that work merged into the integration base lands its claim."""

import json
import sys
import time
from pathlib import Path

import pytest

from agent_parley import (
    BridgeError,
    cli,
    forge,
    issues,
    notify,
    problems,
    roster,
    status,
    store,
    supervision,
)

BASE = "integration/1.0.0"


@pytest.fixture
def claimed(bridge, paired, repo):
    """Records the integration base and claims issue 1 for the Claude lane."""
    store.initialize(bridge.home)
    for name in ("claude", "codex"):
        store.authenticate(
            bridge.home,
            store.register(bridge.home, paired["root"], name)[
                "registration_token"
            ],
        )
    bridge.integration_branch(repo, BASE)
    lane = Path(paired["lanes"]["claude"])
    bridge.issue(lane, "claim", "1")
    return lane


def landed_by(monkeypatch, at=None):
    """Reports issue 1 open on the forge and merged into the base."""
    monkeypatch.setattr(
        supervision.forge, "branch_completion", lambda *args: None
    )
    monkeypatch.setattr(
        supervision.forge,
        "issue_completion",
        lambda *args: {"state": "OPEN"},
    )
    reading = {
        "state": "MERGED",
        "closed_at": time.time() + 1 if at is None else at,
        "pull_request": 775,
        "url": "https://example.invalid/pull/775",
        "branch": "fix/1-c2",
        "commit": "abcdef1234567",
        "base": BASE,
    }
    monkeypatch.setattr(
        supervision.forge,
        "integration_landings",
        lambda repo, base: {"1": reading} if base == BASE else {},
    )


def test_the_forge_reads_closing_keywords_of_pull_requests_into_the_base(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(forge, "_reachable", lambda repo: "owner/name")
    asked = []
    replies = [
        {
            "number": 775,
            "url": "https://example.invalid/pull/775",
            "headRefName": "fix/754-c2",
            "mergeCommit": {"oid": "abcdef1234567"},
            "mergedAt": "2026-09-29T21:20:00Z",
            "body": "Closes #754\nFixes #755, refs #756",
        },
        {
            "number": 770,
            "url": "https://example.invalid/pull/770",
            "headRefName": "fix/754-c1",
            "mergeCommit": {"oid": "1234567abcdef"},
            "mergedAt": "2026-09-29T20:00:00Z",
            "body": "Resolves #754 and #7540",
        },
    ]

    def run(args, timeout):
        asked.append(args)
        return json.dumps(replies)

    monkeypatch.setattr(forge, "_run", run)
    landed = forge.integration_landings(tmp_path, BASE)
    assert asked[0][asked[0].index("--base") + 1] == BASE
    assert asked[0][asked[0].index("--state") + 1] == "merged"
    assert sorted(landed) == ["754", "755"]
    assert landed["754"] == {
        "state": "MERGED",
        "closed_at": forge._epoch("2026-09-29T21:20:00Z"),
        "pull_request": 775,
        "url": "https://example.invalid/pull/775",
        "branch": "fix/754-c2",
        "commit": "abcdef1234567",
        "base": BASE,
    }
    monkeypatch.setattr(forge, "_run", lambda *args: None)
    assert forge.integration_landings(tmp_path, BASE) is None
    monkeypatch.setattr(forge, "_run", lambda *args: "not json")
    assert forge.integration_landings(tmp_path, BASE) is None


def catalog(directory, numbers, limit=status.FORGE_LIMIT):
    """Caches a fresh forge reading holding the given open issues."""
    (directory / status.FORGE_ISSUES).write_text(
        json.dumps(
            {
                "issues": {number: "title" for number in numbers},
                "read_at": time.time(),
                "limit": limit,
            }
        )
    )


def test_keywords_github_does_not_link_are_ignored():
    body = (
        "Refs #9\n<!-- Closes #1 -->\n`Fixes #2`\n```\nResolves #3\n```\n"
        "~~~text\nCloses #4\n~~~\nCloses #5"
    )
    assert forge.closing_numbers(body) == ["5"]
    assert forge.closing_numbers("<!-- Closes #6") == []


def test_a_quoted_closing_keyword_does_not_end_the_claim(
    bridge, claimed, monkeypatch
):
    monkeypatch.setattr(
        supervision.forge, "branch_completion", lambda *args: None
    )
    monkeypatch.setattr(
        supervision.forge, "issue_completion", lambda *args: {"state": "OPEN"}
    )
    monkeypatch.setattr(forge, "_implementation", lambda repo: "github")
    monkeypatch.setattr(forge, "_reachable", lambda repo: "owner/name")
    merged = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 5))
    reply = [
        {
            "number": 775,
            "url": "https://example.invalid/pull/775",
            "headRefName": "fix/1-c2",
            "mergeCommit": {"oid": "abcdef1234567"},
            "mergedAt": merged,
            "body": "<!-- Closes #1 -->\nSee `Fixes #1`.",
        }
    ]
    monkeypatch.setattr(
        forge,
        "_run",
        lambda args, timeout: json.dumps(reply) if "--base" in args else None,
    )
    supervision.poll(bridge.home, claimed.parent)
    assert issues.snapshot(claimed.parent)["issues"]["1"]["owner"] == "claude"


def test_a_failed_landing_read_waits_for_the_retry_window(
    monkeypatch, tmp_path
):
    calls = []
    now = [1000.0]
    monkeypatch.setattr(supervision.time, "time", lambda: now[0])
    monkeypatch.setattr(
        supervision.forge,
        "integration_landings",
        lambda repo, base: calls.append(base),
    )
    assert supervision._landing_reading(tmp_path, BASE) == {}
    now[0] += status.FORGE_RETRY - 1
    assert supervision._landing_reading(tmp_path, BASE) == {}
    assert len(calls) == 1
    now[0] += 2
    supervision._landing_reading(tmp_path, BASE)
    assert len(calls) == 2


def test_a_merge_into_the_base_ends_the_claim_and_names_the_pull_request(
    bridge, claimed, monkeypatch
):
    landed_by(monkeypatch)
    supervision.poll(bridge.home, claimed.parent)
    record = issues.snapshot(claimed.parent)["issues"]["1"]
    assert record["owner"] is None
    assert record["resolution"]["actor"] == "supervisor"
    assert record["resolution"]["evidence"]["base"] == BASE
    assert record["resolution"]["evidence"]["pull_request"] == 775
    assert BASE in record["resolution"]["reason"]
    ledger = issues.snapshot(claimed.parent)
    assert supervision.lifecycle.actionable(ledger, "claude") == []
    assert supervision.lifecycle.actionable(ledger) == []
    project = bridge.status_snapshot()["projects"][0]
    assert project["integration"]["landed"][0]["issue"] == 1
    assert project["integration"]["held"] == []
    line = status.landed_line(project["integration"])
    assert line.startswith(f"Landed in {BASE}: #1 (PR #775)")


def test_without_an_integration_base_an_open_issue_keeps_its_claim(
    bridge, claimed, repo, monkeypatch
):
    bridge.integration_branch(repo, "")
    assert "integration_base" not in roster.read(claimed.parent)
    landed_by(monkeypatch)
    supervision.poll(bridge.home, claimed.parent)
    assert issues.snapshot(claimed.parent)["issues"]["1"]["owner"] == "claude"
    assert "integration" not in bridge.status_snapshot()["projects"][0]


def test_a_merge_into_the_base_before_the_claim_does_not_end_it(
    bridge, claimed, monkeypatch
):
    landed_by(monkeypatch, at=time.time() - 3600)
    supervision.poll(bridge.home, claimed.parent)
    assert issues.snapshot(claimed.parent)["issues"]["1"]["owner"] == "claude"


def test_the_operator_is_asked_once_to_cross_a_fully_landed_base(
    bridge, claimed, paired, monkeypatch
):
    other = Path(paired["lanes"]["codex"])
    bridge.issue(other, "claim", "2")
    landed_by(monkeypatch)
    supervision.poll(bridge.home, claimed.parent)
    rows = problems.derive(bridge.home, bridge.status_snapshot())
    assert not [row for row in rows if row["condition"] == problems.CROSSING]
    bridge.issue(other, "release", "2")
    rows = problems.derive(bridge.home, bridge.status_snapshot())
    assert not [row for row in rows if row["condition"] == problems.CROSSING]
    catalog(claimed.parent, ["1", "2"], limit=2)
    rows = problems.derive(bridge.home, bridge.status_snapshot())
    assert not [row for row in rows if row["condition"] == problems.CROSSING]
    catalog(claimed.parent, ["1", "2"])
    rows = problems.derive(bridge.home, bridge.status_snapshot())
    crossing = [row for row in rows if row["condition"] == problems.CROSSING]
    assert len(crossing) == 1
    assert crossing[0]["actor"] == problems.BY_OPERATOR
    assert "#1" in crossing[0]["detail"]
    assert f"--head {BASE}" in crossing[0]["command"]
    assert problems.CROSSING in notify.PROBLEM_CONDITIONS
    assert issues.snapshot(claimed.parent)["issues"]["2"]["owner"] is None
    catalog(claimed.parent, ["2"])
    rows = problems.derive(bridge.home, bridge.status_snapshot())
    assert not [row for row in rows if row["condition"] == problems.CROSSING]


def test_the_crossing_ages_from_the_claim_that_ended_last():
    now = 10 * problems.STALE_AFTER
    project = {
        "root": "/repo",
        "integration": {
            "base": BASE,
            "landed": [{"issue": 1, "at": now - 3 * problems.STALE_AFTER}],
            "held": [],
            "catalog": True,
            "settled_at": now - 10,
        },
    }
    (row,) = problems._crossing_rows(project, now)
    assert row["seconds"] == 10
    ledger = {
        "issues": {
            "1": {"owner": None, "history": [{"action": "resolve", "at": 4}]},
            "2": {"owner": None, "history": [{"action": "release", "at": 9}]},
            "3": {"owner": "codex", "history": [{"action": "claim", "at": 20}]},
        }
    }
    assert issues.settled_at(ledger) == 9.0


def test_a_landed_issue_the_forge_closed_no_longer_waits_on_the_crossing():
    ledger = {
        "issues": {
            "1": {
                "owner": None,
                "execution": {"state": "complete"},
                "resolution": {
                    "outcome": "complete",
                    "at": 5.0,
                    "evidence": {"base": BASE, "pull_request": 775},
                },
            }
        }
    }
    assert [item["issue"] for item in issues.landed(ledger, BASE, None)] == [1]
    assert issues.landed(ledger, BASE, {"2": {}}) == []
    assert issues.landed(ledger, "", None) == []


def test_only_the_operator_records_a_valid_integration_base(
    bridge, paired, repo, monkeypatch, capsys
):
    with pytest.raises(BridgeError, match="one Git branch name"):
        bridge.integration_branch(repo, "bad..name")
    with pytest.raises(BridgeError, match="base checkout"):
        bridge.integration_branch(Path(paired["lanes"]["claude"]), BASE)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agent-parley",
            "--home",
            str(bridge.home),
            "branch",
            "integration",
            BASE,
            "--repo",
            str(repo),
        ],
    )
    assert cli.main() == 0
    assert BASE in capsys.readouterr().out
    directory = Path(paired["lanes"]["claude"]).parent
    assert roster.read(directory)["integration_base"] == BASE
