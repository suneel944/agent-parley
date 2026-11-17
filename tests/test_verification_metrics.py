"""Checks the recorded verification outcomes and the find rate they give."""

import json
import pathlib
import sys
import time

import pytest

from agent_parley import (
    children,
    cli,
    issues,
    recommend,
    reviews,
    roster,
    store,
    supervision,
    verification,
)
from agent_parley.state import write_json
from agent_parley.status import FORGE_ISSUES

DAY = 86400.0


@pytest.fixture
def project(bridge, repo):
    """Registers a claude lane and a codex lane on one opus model."""
    bridge.add_participant(repo, "claude", "claude")
    manifest = bridge.add_participant(repo, "codex", "codex")
    directory = roster.locate(bridge.home, manifest["root"])
    manifest = roster.read(directory)
    manifest["participants"]["claude"]["model"] = "opus"
    write_json(directory / "project.json", manifest)
    return {"root": manifest["root"], "directory": directory}


def cache(directory, listed):
    """Writes a fresh forge reading naming each issue's labels."""
    write_json(
        directory / FORGE_ISSUES,
        {
            "issues": {
                number: {"title": f"Issue {number}", "labels": names}
                for number, names in listed.items()
            },
            "read_at": time.time(),
        },
    )


def outcome(directory, lane, issue, findings, at):
    """Records one review outcome by a lane."""
    return verification.record(
        directory,
        roster.read(directory),
        kind=verification.REVIEW,
        issue=issue,
        lane=lane,
        findings=findings,
        now=at,
    )


def run(bridge, monkeypatch, capsys, *arguments):
    """Runs the command line against the test bridge."""
    monkeypatch.setattr(
        sys, "argv", ["agent-parley", "--home", str(bridge.home), *arguments]
    )
    assert cli.main() == 0
    return capsys.readouterr().out


def test_an_empty_store_prints_no_outcome(bridge, monkeypatch, capsys):
    shown = run(bridge, monkeypatch, capsys, "metrics", "verification")
    assert shown.splitlines() == [
        "Verification outcomes over 30d: 0 (rate shown from 5 outcomes)",
        "No review round or child report recorded.",
    ]
    found = json.loads(
        run(bridge, monkeypatch, capsys, "metrics", "verification", "--json")
    )
    assert found["outcomes"] == 0
    assert found["labels"] == {} and found["models"] == {}


def test_fewer_than_five_outcomes_print_no_rate(
    bridge, project, monkeypatch, capsys
):
    directory = project["directory"]
    cache(directory, {"7": ["bug"]})
    now = time.time()
    for findings in (1, 0, 2, 0):
        outcome(directory, "codex", "7", findings, now)
    found = verification.reading(bridge.home, now=now + 1)
    assert found["labels"]["bug"] == {
        "outcomes": 4,
        "found": 2,
        "findings": 3,
        "rate": None,
    }
    shown = run(bridge, monkeypatch, capsys, "metrics", "verification")
    row = next(line for line in shown.splitlines() if line.startswith("bug"))
    assert row.split() == ["bug", "4", "2", "3", "-"]
    assert verification.reason(directory, ["bug"], now=now + 1) == ""


def test_outcomes_group_by_label_and_by_provider_and_model(project):
    directory = project["directory"]
    cache(directory, {"7": ["bug", "cli"], "8": []})
    now = time.time()
    for findings in (1, 0, 0, 3, 0):
        outcome(directory, "codex", "7", findings, now)
    for findings in (2, 1, 0, 1, 1, 0):
        outcome(directory, "claude", "8", findings, now)
    grouped = verification.grouped(verification.outcomes(directory))
    assert grouped["labels"]["bug"]["rate"] == pytest.approx(0.4)
    assert grouped["labels"]["cli"]["outcomes"] == 5
    assert grouped["labels"]["unlabelled"]["rate"] == pytest.approx(4 / 6)
    assert grouped["models"]["codex/default"]["rate"] == pytest.approx(0.4)
    assert grouped["models"]["claude/opus"]["findings"] == 5
    assert verification.reason(directory, ["bug", "docs"], now=now + 1) == (
        "verification find rate bug 40% of 5"
    )


def test_the_window_leaves_out_older_outcomes(bridge, project):
    directory = project["directory"]
    now = time.time()
    for _ in range(5):
        outcome(directory, "codex", "7", 1, now - 40 * DAY)
    outcome(directory, "codex", "7", 0, now - DAY)
    assert verification.reading(bridge.home, now=now)["outcomes"] == 1
    wide = verification.reading(bridge.home, 60 * DAY, now=now)
    assert wide["outcomes"] == 6
    assert wide["models"]["codex/default"]["rate"] == pytest.approx(5 / 6)


def test_issue_next_cites_the_find_rate_of_the_issue_labels(project, repo):
    directory = project["directory"]
    cache(directory, {"9": ["bug"], "10": ["docs"]})
    now = time.time()
    for findings in (1, 1, 0, 0, 0):
        outcome(directory, "codex", "9", findings, now)
    for findings in (1, 0):
        outcome(directory, "codex", "10", findings, now)
    result = recommend.shortlist(
        directory, project["root"], repo, "claude", {}, lambda a, b: False
    )
    reasons = {
        record["issue"]: record["reasons"] for record in result["candidates"]
    }
    assert "verification find rate bug 40% of 5" in reasons["9"]
    assert not any("find rate" in said for said in reasons["10"])


def test_a_returned_review_round_records_its_outcome(bridge, repo, monkeypatch):
    bridge.add_participant(repo, "claude", "claude")
    manifest = bridge.add_participant(repo, "codex", "codex")
    store.initialize(bridge.home)
    actors = {}
    for name in ("claude", "codex"):
        token = store.register(bridge.home, manifest["root"], name)
        actors[name] = store.authenticate(
            bridge.home, token["registration_token"]
        )
    monkeypatch.setattr(
        supervision,
        "lane_state",
        lambda *args, **kwargs: {"process_alive": True},
    )
    directory = roster.locate(bridge.home, manifest["root"])
    cache(directory, {"7": ["bug"]})
    issues.change(
        directory,
        "claude",
        "claim",
        "7",
        participants={"claude", "codex"},
        title="Issue 7",
    )
    asked = store.call(
        bridge.home,
        actors["claude"],
        "send_message",
        {
            "to": [reviews.ADDRESS, "agent_parley/retries.py"],
            "subject": "feat/7-retry",
            "body_md": "Check it.",
            "idempotency_key": "ask-1",
        },
    )["review"]
    store.call(
        bridge.home,
        actors["codex"],
        "send_message",
        {
            "to": [reviews.ADDRESS, asked["id"]],
            "subject": "Findings",
            "body_md": "- bound is off by one\n- no test",
            "idempotency_key": "answer-1",
        },
    )
    [row] = verification.outcomes(directory)
    assert row["kind"] == verification.REVIEW
    assert row["issue"] == "7"
    assert row["labels"] == ["bug"]
    assert row["provider"] == "codex"
    assert row["findings"] == 2


def test_a_relayed_child_report_records_its_outcome(project, monkeypatch):
    directory = project["directory"]
    cache(directory, {"7": ["cli"]})
    issues.change(
        directory,
        "claude",
        "claim",
        "7",
        participants={"claude", "codex"},
        title="Issue 7",
    )
    manifest = roster.read(directory)
    manifest["participants"]["claude-c1"] = {
        "provider": "claude",
        "display": "claude-c1",
        "parent": "claude",
        "branch": "feat/7-child",
        "model": "haiku",
    }
    write_json(directory / "project.json", manifest)
    monkeypatch.setattr(store, "speak", lambda *args, **kwargs: {"id": "m1"})
    report = {
        "state": "ready",
        "commit": "abc",
        "summary": "Checked the parser.\n- empty input crashes\n- no error",
        "remaining": "1. add a fuzz test\n2. update the docs\n3. release",
    }
    assert children.deliver(
        pathlib.Path("unused"), directory, "claude-c1", report
    )
    [row] = verification.outcomes(directory)
    assert row["kind"] == verification.CHILD
    assert row["issue"] == "7"
    assert row["labels"] == ["cli"]
    assert (row["provider"], row["model"]) == ("claude", "haiku")
    assert row["findings"] == 2


def test_an_unreadable_ledger_still_relays_the_child_report(
    project, monkeypatch
):
    directory = project["directory"]
    manifest = roster.read(directory)
    manifest["participants"]["claude-c1"] = {
        "provider": "claude",
        "display": "claude-c1",
        "parent": "claude",
        "branch": "feat/7-child",
    }
    write_json(directory / "project.json", manifest)
    monkeypatch.setattr(store, "speak", lambda *args, **kwargs: {"id": "m1"})

    def broken(*args, **kwargs):
        raise ValueError("damaged ledger")

    monkeypatch.setattr(issues, "snapshot", broken)
    report = {"state": "ready", "commit": "abc", "summary": "- one finding"}
    assert children.deliver(
        pathlib.Path("unused"), directory, "claude-c1", report
    )
    [row] = verification.outcomes(directory)
    assert row["issue"] == ""
    assert row["findings"] == 1
    entry = roster.read(directory)["participants"]["claude-c1"]
    assert entry["result"]["outcome"] == "ready"


def test_a_malformed_line_is_skipped(bridge, project, monkeypatch, capsys):
    directory = project["directory"]
    now = time.time()
    outcome(directory, "codex", "7", 1, now)
    valid = {"kind": verification.REVIEW, "findings": 1}
    with (directory / verification.RECORD_NAME).open("a") as stream:
        for at in ("x", [1], {"t": 1}):
            stream.write(json.dumps({**valid, "at": at}) + "\n")
        stream.write('[1, 2]\n"text"\nnot json\n')
    [row] = verification.outcomes(directory)
    assert row["lane"] == "codex"
    assert verification.reading(bridge.home, now=now + 1)["outcomes"] == 1
    shown = run(bridge, monkeypatch, capsys, "metrics", "verification")
    assert "Verification outcomes over 30d: 1" in shown
    assert verification.reason(directory, ["bug"], now=now + 1) == ""


def test_issue_next_reads_the_outcome_log_once(project, repo, monkeypatch):
    directory = project["directory"]
    cache(directory, {"9": ["bug"], "10": ["bug"], "11": ["docs"]})
    now = time.time()
    for findings in (1, 1, 0, 0, 0):
        outcome(directory, "codex", "9", findings, now)
    reads = []
    original = verification.outcomes

    def spy(*args, **kwargs):
        reads.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(verification, "outcomes", spy)
    result = recommend.shortlist(
        directory, project["root"], repo, "claude", {}, lambda a, b: False
    )
    reasons = {
        record["issue"]: record["reasons"] for record in result["candidates"]
    }
    assert {"9", "10", "11"} <= set(reasons)
    assert "verification find rate bug 40% of 5" in reasons["10"]
    assert len(reads) == 1


def test_the_reading_reads_no_transcript(bridge, project, monkeypatch):
    directory = project["directory"]
    now = time.time()
    for findings in (1, 0, 0, 0, 0):
        outcome(directory, "codex", "7", findings, now)
    opened = []
    read_text = pathlib.Path.read_text

    def spy(path, *args, **kwargs):
        opened.append(path.name)
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "read_text", spy)
    found = verification.reading(bridge.home, now=now + 1)
    verification.reason(directory, ["unlabelled"], now=now + 1)
    assert found["models"]["codex/default"]["rate"] == pytest.approx(0.2)
    assert set(opened) == {verification.RECORD_NAME}
