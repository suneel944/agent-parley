"""Checks the machine-readable form of every read-only command."""

import json
import sys
from pathlib import Path

import pytest

from agent_parley import cli, metrics, store, views


def run(monkeypatch, capsys, *arguments):
    """Runs one CLI invocation and returns its parsed standard output."""
    monkeypatch.setattr(sys, "argv", ["agent-parley", *arguments])
    assert cli.main() == 0
    return json.loads(capsys.readouterr().out)


def envelope(document, kind):
    """Asserts the shared snapshot envelope and returns the document."""
    assert document["schema"] == views.SCHEMA
    assert document["kind"] == kind
    assert document["generated_at"].endswith("Z")
    return document


def test_status_reports_projects_lanes_and_ownership(
    bridge, repo, paired, monkeypatch, capsys
):
    bridge.issue(paired["lanes"]["claude"], "claim", "42")
    document = envelope(
        run(
            monkeypatch,
            capsys,
            "--home",
            str(bridge.home),
            "status",
            "--json",
        ),
        "status",
    )
    assert document["server"] == {"ready": False, "state": "not ready"}
    assert document["state_directory"] == str(bridge.home)
    project = document["projects"][0]
    assert project["root"] == paired["root"]
    assert len(project["issues"]) == 1
    claimed = project["issues"][0]
    expected = {
        "issue": 42,
        "owner": "claude",
        "title": None,
        "blocked_by": [],
        "offer": None,
        "reminder": None,
    }
    assert claimed | expected == claimed
    lanes = {lane["participant"]: lane for lane in project["participants"]}
    assert sorted(lanes) == ["claude", "codex"]
    assert lanes["claude"]["provider"] == "claude"
    assert lanes["claude"]["branch"] == paired["branches"]["claude"]
    assert lanes["claude"]["drift"] is False
    assert lanes["claude"]["outcome"] == "unknown"
    assert lanes["claude"]["paused"] is False


def test_status_reports_a_report_and_its_age(
    bridge, repo, paired, monkeypatch, capsys
):
    lane = paired["lanes"]["claude"]
    bridge.report(lane, "ready", "Engine built", "", "12 tests passed")
    document = run(
        monkeypatch, capsys, "--home", str(bridge.home), "status", "--json"
    )
    lanes = document["projects"][0]["participants"]
    reported = next(row for row in lanes if row["participant"] == "claude")
    assert reported["outcome"] == "ready"
    assert reported["summary"] == "Engine built"
    assert reported["evidence"] == "12 tests passed"
    assert reported["reported_at"].endswith("Z")
    assert reported["report_age_seconds"] >= 0
    assert reported["review"] is None


def test_status_reports_the_verdict_a_peer_recorded(
    bridge, repo, paired, monkeypatch, capsys
):
    lane = paired["lanes"]["claude"]
    bridge.report(lane, "ready", "Engine built", "", "12 tests passed")
    recorded = metrics.report_records(Path(lane).parent, "claude")[-1]["id"]
    bridge.review_report(
        Path(paired["lanes"]["codex"]), recorded, "fail", "One case regressed"
    )
    document = run(
        monkeypatch, capsys, "--home", str(bridge.home), "status", "--json"
    )
    lanes = document["projects"][0]["participants"]
    reviewed = next(row for row in lanes if row["participant"] == "claude")
    assert reviewed["review"]["verdict"] == "fail"
    assert reviewed["review"]["reviewer"] == "codex"
    assert reviewed["review"]["report_id"] == recorded
    assert reviewed["review"]["evidence"] == "One case regressed"
    assert reviewed["review"]["recorded_at"].endswith("Z")
    assert reviewed["review"]["independent_verification"] is False


def test_status_text_still_prints_the_same_report(bridge, repo, paired, capsys):
    bridge.status(cli.Selection(participant="claude"))
    output = capsys.readouterr().out
    assert "Server: not ready" in output
    assert "claude (claude):" in output
    assert "Reported outcome: unknown" in output


def test_top_prints_one_frame_and_exits(
    bridge, repo, paired, monkeypatch, capsys
):
    document = envelope(
        run(monkeypatch, capsys, "--home", str(bridge.home), "top", "--json"),
        "top",
    )
    assert document["server"] == {"running": False}
    assert document["window_seconds"] is None
    assert document["totals"]["participants"] == 2
    rows = document["projects"][0]["participants"]
    assert [row["participant"] for row in rows] == ["claude", "codex"]
    assert rows[0]["issues"] == []
    assert rows[0]["provider"] == "claude"
    assert rows[0]["credential"] is None
    assert rows[0]["drift"] is False


def test_issue_list_reports_offers_with_their_identifiers(
    bridge, repo, paired, monkeypatch, capsys
):
    lane = paired["lanes"]["claude"]
    bridge.issue(paired["lanes"]["codex"], "claim", "9")
    bridge.issue(lane, "claim", "7")
    bridge.issue(lane, "block", "7", on="9")
    record = bridge.issue(
        lane, "offer", "7", to="codex", summary="commit abc; tests green"
    )
    document = envelope(
        run(
            monkeypatch,
            capsys,
            "--home",
            str(bridge.home),
            "issue",
            "list",
            "--repo",
            str(repo),
            "--json",
        ),
        "issues",
    )
    assert document["revision"] >= 3
    claimed = next(row for row in document["issues"] if row["issue"] == 7)
    assert claimed["owner"] == "claude"
    assert claimed["blocked_by"] == [9]
    assert claimed["offer"]["offer_id"] == record["offer"]["id"]
    assert claimed["offer"]["to"] == "codex"
    assert claimed["offer"]["created_at"].endswith("Z")


def test_participant_list_reports_branches_and_accounts(
    bridge, repo, paired, monkeypatch, capsys
):
    document = envelope(
        run(
            monkeypatch,
            capsys,
            "--home",
            str(bridge.home),
            "participant",
            "list",
            "--repo",
            str(repo),
            "--json",
        ),
        "participants",
    )
    assert document["root"] == paired["root"]
    rows = document["participants"]
    assert [row["participant"] for row in rows] == ["claude", "codex"]
    assert rows[0]["branch"] == paired["branches"]["claude"]
    assert rows[0]["credential"] is None
    assert rows[0]["identity"] == "claude"


def test_verify_and_init_report_their_configured_commands(
    bridge, repo, monkeypatch, capsys
):
    bridge.setup(repo)
    bridge.verification(repo, "make check")
    for command, kind, configured in (
        ("verify", "verify", ["make", "check"]),
        ("init", "init", []),
    ):
        document = envelope(
            run(
                monkeypatch,
                capsys,
                "--home",
                str(bridge.home),
                command,
                "show",
                "--repo",
                str(repo),
                "--json",
            ),
            kind,
        )
        assert document["command"] == configured
        assert document["configured"] is bool(configured)


def test_provider_and_credential_registries_report_as_arrays(
    bridge, monkeypatch, capsys
):
    document = envelope(
        run(
            monkeypatch,
            capsys,
            "--home",
            str(bridge.home),
            "provider",
            "list",
            "--json",
        ),
        "providers",
    )
    names = [entry["name"] for entry in document["providers"]]
    assert "claude" in names and "codex" in names
    empty = envelope(
        run(
            monkeypatch,
            capsys,
            "--home",
            str(bridge.home),
            "credentials",
            "list",
            "--json",
        ),
        "credentials",
    )
    assert empty["credentials"] == []


def test_mail_thread_and_search_report_identifiers(
    bridge, repo, paired, monkeypatch, capsys
):
    lane = paired["lanes"]["claude"]
    store.initialize(bridge.home)
    store.register(bridge.home, paired["root"], "claude")
    delivered = bridge.say(repo, "claude", "Check the shared fixture")
    document = envelope(
        run(
            monkeypatch,
            capsys,
            "--home",
            str(bridge.home),
            "mail",
            "thread",
            delivered["thread_id"],
            "--repo",
            str(lane),
            "--json",
        ),
        "mail_thread",
    )
    assert document["thread_id"] == delivered["thread_id"]
    assert document["messages"][0]["id"] == delivered["id"]
    found = envelope(
        run(
            monkeypatch,
            capsys,
            "--home",
            str(bridge.home),
            "mail",
            "search",
            "shared fixture",
            "--repo",
            str(lane),
            "--json",
        ),
        "mail_search",
    )
    assert found["messages"][0]["id"] == delivered["id"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, "1970-01-01T00:00:00Z"),
        ("2026-09-14 08:30:00", "2026-09-14T08:30:00Z"),
        (None, None),
        ("", None),
        ("not a time", None),
    ],
)
def test_recorded_times_report_as_rfc_3339(value, expected):
    assert views.timestamp(value) == expected
