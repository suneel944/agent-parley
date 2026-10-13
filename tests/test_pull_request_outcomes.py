"""Checks the pull request outcomes reported per lane."""

import json
import sys
import time
from pathlib import Path

import pytest

from agent_parley import cli, forge, metrics, store, supervision

NOW = 1_800_000_000.0
DAY = 86400.0
SINCE = NOW - 7 * DAY


def resolved(holder, pull_request, claimed, merged):
    """Builds one ledger record whose claim a merged pull request ended."""
    return {
        "owner": None,
        "history": [
            {"action": "claim", "at": claimed, "owner": holder},
            {
                "action": "resolve",
                "at": merged + 30,
                "outcome": "complete",
                "holder": holder,
                "evidence": {
                    "state": "MERGED",
                    "closed_at": merged,
                    "pull_request": pull_request,
                    "url": f"https://example.test/pull/{pull_request}",
                },
            },
        ],
    }


def replay():
    """Returns a recorded pull request payload and the ledger beside it."""
    ledger = {
        "issues": {
            "10": resolved("claude", 7, NOW - 3 * DAY, NOW - 3 * DAY + 3600),
            "11": resolved("claude", 8, NOW - 2 * DAY, NOW - 2 * DAY + 7200),
            "12": resolved("claude", 9, NOW - DAY, NOW - DAY + 600),
            "13": resolved("codex", 5, NOW - 20 * DAY, NOW - 19 * DAY),
        }
    }
    record = {
        "read_at": NOW,
        "pull_requests": {
            "20": {"number": 20, "lane": "codex", "opened_at": NOW - 10},
            "21": {"number": 21, "opened_at": NOW - 5},
        },
        "history": [
            {
                "number": 7,
                "lane": "claude",
                "opened_at": NOW - 3 * DAY + 60,
                "gone_at": NOW - 3 * DAY + 3700,
                "rounds": 1,
                "reviews": [{"author": "peer", "state": "APPROVED"}],
            },
            {
                "number": 8,
                "lane": "claude",
                "opened_at": NOW - 2 * DAY + 60,
                "gone_at": NOW - 2 * DAY + 7300,
                "rounds": 3,
                "reviews": [{"author": "peer", "state": "CHANGES_REQUESTED"}],
            },
            {
                "number": 9,
                "opened_at": NOW - DAY + 60,
                "gone_at": NOW - DAY + 700,
            },
            {
                "number": 30,
                "lane": "codex",
                "opened_at": NOW - 100,
                "gone_at": NOW - 50,
            },
            {"number": 31, "opened_at": NOW - 90, "gone_at": NOW - 40},
            {
                "number": 32,
                "lane": "codex",
                "opened_at": NOW - 30 * DAY,
                "gone_at": NOW - 29 * DAY,
            },
        ],
    }
    return record, ledger


def test_a_replayed_window_gives_exact_counts_and_medians():
    record, ledger = replay()
    reading = metrics.pull_request_outcomes(record, ledger, SINCE)
    assert reading["excluded"] == 2
    assert reading["lanes"]["claude"] == {
        "pull_requests_opened": 3,
        "pull_requests_merged": 3,
        "pull_requests_closed": 0,
        "first_pass": 1,
        "first_pass_known": 2,
        "first_pass_rate": 0.5,
        "ci_rounds_unknown": 1,
        "ci_rounds_median": 2.0,
        "claim_to_merge_seconds": 3600.0,
    }
    codex = reading["lanes"]["codex"]
    assert codex["pull_requests_opened"] == 2
    assert codex["pull_requests_merged"] == 0
    assert codex["pull_requests_closed"] == 1
    assert codex["first_pass_rate"] == 0.0
    line = metrics.outcome_line(reading, "7d")
    assert line == (
        "Pull requests (last 7d): 5 opened, 3 merged, 1 closed unmerged, "
        "first pass 1/2 (1 without CI rounds), 2 unowned excluded"
    )


def test_an_empty_window_reports_nothing_rather_than_failing():
    assert metrics.pull_request_outcomes(None, {"issues": {}}, SINCE) == {
        "lanes": {},
        "excluded": 0,
    }
    record, ledger = replay()
    later = metrics.pull_request_outcomes(record, ledger, NOW + DAY)
    assert later == {"lanes": {}, "excluded": 0}
    assert metrics.outcome_line(later, "7d") == ""


@pytest.mark.parametrize(
    ("reading", "expected"),
    [
        (None, None),
        ({}, None),
        ({"rounds": True}, None),
        ({"rounds": 0}, None),
        ({"rounds": "2"}, None),
        ({"rounds": 2}, 2),
    ],
)
def test_the_ci_round_reader_returns_only_a_stored_count(reading, expected):
    assert metrics.ci_rounds(reading) == expected


@pytest.fixture
def project(bridge, paired, monkeypatch):
    """Registers both lanes and reads the forge on every call."""
    store.initialize(bridge.home)
    for name in ("claude", "codex"):
        store.register(bridge.home, paired["root"], name)
    monkeypatch.setattr(supervision, "PULL_REQUEST_SECONDS", 0.0)
    return Path(paired["lanes"]["claude"]).parent


def observe(bridge, directory, monkeypatch, *readings):
    """Runs the pull request stage once with a fixed forge answer."""
    monkeypatch.setattr(
        supervision.forge, "open_pull_requests", lambda root: list(readings)
    )
    supervision.pull_request_wakes(
        bridge.home, directory, supervision.roster.read(directory)
    )
    return json.loads((directory / supervision.PULL_REQUEST_RECORD).read_text())


def opened(number):
    """Builds one open pull request reading with green checks."""
    return {
        "number": number,
        "url": f"https://example.test/pull/{number}",
        "branch": f"feature/{number}",
        "sha": "a" * 40,
        "checks": "green",
        "failing": [],
        "reviews": [],
        "mergeable": "MERGEABLE",
        "issues": [],
    }


def test_a_pull_request_leaving_the_open_list_is_archived_and_pruned(
    bridge, project, monkeypatch
):
    first = observe(bridge, project, monkeypatch, opened(7))
    seen = first["pull_requests"]["7"]["opened_at"]
    stale = {"number": 3, "gone_at": time.time() - 31 * DAY}
    (project / supervision.PULL_REQUEST_RECORD).write_text(
        json.dumps({**first, "read_at": 0, "history": [stale]})
    )
    after = observe(bridge, project, monkeypatch)
    assert after["pull_requests"] == {}
    assert [item["number"] for item in after["history"]] == [7]
    assert after["history"][0]["opened_at"] == seen
    assert after["history"][0]["gone_at"] >= seen


def test_a_full_reading_archives_nothing(bridge, project, monkeypatch):
    monkeypatch.setattr(forge, "MAX_PULL_REQUESTS", 1)
    observe(bridge, project, monkeypatch, opened(7))
    after = observe(bridge, project, monkeypatch, opened(8))
    assert sorted(after["pull_requests"]) == ["7", "8"]
    assert after["history"] == []


def run(monkeypatch, capsys, *arguments):
    """Runs one CLI invocation and returns its standard output."""
    monkeypatch.setattr(sys, "argv", ["agent-parley", *arguments])
    assert cli.main() == 0
    return capsys.readouterr().out


def test_metrics_status_and_top_report_recorded_outcomes(
    bridge, paired, monkeypatch, capsys
):
    directory = Path(paired["lanes"]["claude"]).parent
    home = ["--home", str(bridge.home)]
    labels = (
        f'project="{paired["root"]}",participant="claude",provider="claude"'
    )
    empty = run(monkeypatch, capsys, *home, "metrics")
    assert f"agent_parley_lane_pull_requests_merged_total{{{labels}}} 0" in (
        empty
    )
    assert (
        f'agent_parley_project_pull_requests_excluded{{project="'
        f'{paired["root"]}"}} 0' in empty
    )
    now = time.time()
    (directory / supervision.PULL_REQUEST_RECORD).write_text(
        json.dumps(
            {
                "read_at": now,
                "pull_requests": {
                    "4": {"number": 4, "lane": "claude", "opened_at": now}
                },
                "history": [{"number": 5, "opened_at": now, "gone_at": now}],
            }
        )
    )
    text = run(monkeypatch, capsys, *home, "metrics")
    assert f"agent_parley_lane_pull_requests_opened_total{{{labels}}} 1" in (
        text
    )
    assert (
        f'agent_parley_project_pull_requests_excluded{{project="'
        f'{paired["root"]}"}} 1' in text
    )
    document = json.loads(run(monkeypatch, capsys, *home, "top", "--json"))
    row = next(
        participant
        for project in document["projects"]
        for participant in project["participants"]
        if participant["participant"] == "claude"
    )
    assert row["pull_requests"]["pull_requests_opened"] == 1
    summary = "1 opened, 0 merged, 0 closed unmerged"
    assert summary in run(monkeypatch, capsys, *home, "top", "--once")
    assert summary in run(monkeypatch, capsys, *home, "status")
