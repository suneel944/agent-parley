"""Checks the open-work status view, its scoping and its filters."""

import asyncio
import json
import os
import shlex
import shutil
import sys
import time
from pathlib import Path

import pytest

from agent_parley import (
    checkpoints,
    cli,
    forge,
    issues,
    lifecycle,
    roster,
    store,
    supervision,
)
from agent_parley.process import start_ticks
from agent_parley.state import write_json
from agent_parley.status import FORGE_ISSUES, FORGE_TIMEOUT, dormant


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """Keeps every status reading off the network; the forge starts down.

    Returns:
        The fake's state: ``calls`` records each read and ``answer`` is the
        catalog the next read returns, None for a failed read.
    """
    fake = {"calls": [], "answer": None}

    def catalog(repo, limit, timeout):
        fake["calls"].append(timeout)
        return fake["answer"]

    monkeypatch.setattr(forge, "open_issue_catalog", catalog)
    return fake


def second_project(bridge, tmp_path):
    """Registers a second repository with one participant of its own."""
    path = tmp_path / "other project"
    path.mkdir()
    cli.git(path, "init")
    (path / "README.md").write_text("other\n")
    cli.git(path, "add", "README.md")
    cli.git(
        path,
        "-c",
        "user.name=Bridge Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-m",
        "Second fixture",
    )
    bridge.add_participant(path, "codex", "codex")
    return path


def run(monkeypatch, bridge, *arguments):
    """Runs one status invocation against the fixture's state."""
    monkeypatch.setattr(
        sys, "argv", ["agent-parley", "--home", str(bridge.home), *arguments]
    )
    return cli.main()


def edit(bridge, repo, number, change):
    """Applies one change to a ledger record and publishes the ledger."""
    directory = bridge.project(repo)[1]
    ledger = issues.snapshot(directory)
    change(ledger["issues"][number])
    ledger["revision"] += 1
    write_json(directory / "issues.json", ledger)
    return directory


def older_than_reading(record):
    """Moves a claim's start two hours back, before any stale reading."""
    for entry in record.get("history", []):
        if entry.get("action") == "claim":
            entry["at"] = time.time() - 7200


def ended(record):
    """Records the reminder a forge-observed completion writes its holder."""
    record["handoff_prompt"] = {
        "id": f"{record['owner']}:ended",
        "holder": record["owner"],
        "created": time.time(),
        "trigger": supervision.ENDED,
        "text": "Issue closed; send an explicit completion message.",
    }


def test_status_inside_a_checkout_reports_that_project_only(
    bridge, repo, paired, tmp_path, monkeypatch, capsys
):
    other = second_project(bridge, tmp_path)
    for place in (repo, Path(paired["lanes"]["claude"])):
        monkeypatch.chdir(place)
        assert run(monkeypatch, bridge, "status") == 0
        output = capsys.readouterr().out
        assert f"Project: {repo}" in output
        assert f"Project: {other}" not in output
        assert "1 other project(s) not shown; --all-projects" in output
    assert run(monkeypatch, bridge, "status", "--all-projects") == 0
    output = capsys.readouterr().out
    assert f"Project: {repo}" in output
    assert f"Project: {other}" in output


def test_bare_status_reads_the_working_directory_without_a_parser(
    bridge, repo, paired, tmp_path, monkeypatch, capsys
):
    other = second_project(bridge, tmp_path)
    monkeypatch.setenv("AGENT_PARLEY_HOME", str(bridge.home))
    monkeypatch.setattr(sys, "argv", ["agent-parley", "status"])
    monkeypatch.chdir(other)
    assert cli.main() == 0
    output = capsys.readouterr().out
    assert f"Project: {other}" in output
    assert f"Project: {repo}" not in output
    monkeypatch.chdir(tmp_path)
    assert cli.main() == 0
    output = capsys.readouterr().out
    assert f"Project: {other}" in output
    assert f"Project: {repo}" in output


def test_claims_on_ended_issues_are_hidden_unless_asked_for(
    bridge, repo, paired, monkeypatch, capsys
):
    lane = paired["lanes"]["claude"]
    bridge.issue(lane, "claim", "42")
    bridge.issue(paired["lanes"]["codex"], "claim", "43")
    edit(bridge, repo, "42", lambda record: record.update(title="Wire it"))
    edit(bridge, repo, "43", ended)
    monkeypatch.chdir(repo)
    assert run(monkeypatch, bridge, "status") == 0
    lines = capsys.readouterr().out.splitlines()
    heading = next(line for line in lines if line.startswith("ISSUE"))
    assert heading.split() == [
        "ISSUE",
        "TITLE",
        "OWNER",
        "STATE",
        "LAST",
        "EVENT",
        "PR",
    ]
    rows = [line for line in lines if line.startswith("#")]
    assert len(rows) == 1
    assert rows[0].startswith("#42") and "Wire it" in rows[0]
    assert "claude" in rows[0]
    assert any(
        line.startswith("Closed or ended on the forge, still owned: #43;")
        and "--all lists them" in line
        and "agent-parley issue resolve N" in line
        for line in lines
    )
    assert run(monkeypatch, bridge, "status", "--all") == 0
    rows = [
        line
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("#")
    ]
    assert [row.split()[0] for row in rows] == ["#42", "#43"]
    assert "ended" in rows[1]


def test_a_cached_pull_request_fills_the_pr_column(
    bridge, repo, paired, capsys
):
    bridge.issue(paired["lanes"]["claude"], "claim", "42")
    directory = bridge.project(repo)[1]
    write_json(
        directory / supervision.PULL_REQUEST_RECORD,
        {
            "read_at": time.time(),
            "pull_requests": {
                "7": {
                    "number": 7,
                    "url": "https://example.test/pull/7",
                    "branch": "elsewhere",
                    "checks": "pending",
                    "mergeable": "CONFLICTING",
                    "issues": ["42"],
                }
            },
        },
    )
    bridge.board(cli.Selection(project=str(repo)), width=None)
    row = next(
        line
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("#42")
    )
    assert row.endswith("#7 CI pending, conflicting")


def test_a_dead_project_is_hidden_then_listed_last(
    bridge, repo, paired, tmp_path, capsys
):
    other = second_project(bridge, tmp_path)
    shutil.rmtree(other)
    assert bridge.board() == 2
    output = capsys.readouterr().out
    assert f"Project: {repo}" in output
    assert str(other) not in output
    assert "1 dormant project(s) hidden" in output
    assert bridge.board(every_project=True) == 3
    lines = capsys.readouterr().out.splitlines()
    projects = [line for line in lines if line.startswith("Project:")]
    assert projects == [f"Project: {repo}", f"Project: {other} (dormant)"]


def test_a_project_whose_lanes_all_stopped_long_ago_is_dormant():
    stopped = {
        "retired_at": None,
        "retired_age_seconds": None,
        "condition": {"state": "stopped", "seconds": 90000},
        "availability": {"process_alive": False, "age_seconds": 90000},
    }
    working = {**stopped, "condition": {"state": "working", "seconds": 5}}
    root = str(Path.cwd())
    assert dormant(root, [stopped, stopped])
    assert not dormant(root, [stopped, working])
    assert not dormant(root, [])


def test_the_lane_line_shows_its_own_work_not_the_operator_prompt(
    bridge, repo, paired, capsys
):
    directory = bridge.project(repo)[1]
    store.initialize(bridge.home)
    store.authenticate(
        bridge.home,
        asyncio.run(bridge.identity("claude", paired))["registration_token"],
    )
    checkpoints.checkpoint(
        bridge.home,
        directory,
        "claude",
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "test",
            "cwd": paired["lanes"]["claude"],
            "prompt": "are you working?",
        },
    )
    bridge.report(
        paired["lanes"]["claude"], "ready", "Engine built", "", "checks pass"
    )
    bridge.issue(paired["lanes"]["claude"], "claim", "42")
    bridge.board(cli.Selection(project=str(repo)), width=None)
    lines = capsys.readouterr().out.splitlines()
    heading = lines.index(
        next(line for line in lines if line.startswith("LANE"))
    )
    assert lines[heading].split() == ["LANE", "STATE", "CLAIMS", "TASK"]
    lane = next(line for line in lines if line.startswith("claude "))
    assert lane.split()[2] == "1"
    assert lane.endswith("Engine built")
    assert "are you working?" not in "\n".join(lines)


@pytest.mark.parametrize("peer", [True, False])
def test_orphaned_and_overdue_claims_name_the_resolving_command(
    bridge, repo, paired, capsys, peer
):
    bridge.issue(paired["lanes"]["claude"], "claim", "42")
    bridge.issue(paired["lanes"]["codex"], "claim", "43")
    if peer:
        write_json(
            bridge.project(repo)[1] / "codex-activity.json",
            {
                "session_pid": os.getpid(),
                "session_ticks": start_ticks(os.getpid()),
                "activity": "idle",
                "updated": time.time(),
            },
        )
    edit(
        bridge,
        repo,
        "42",
        lambda record: record.update(
            orphan={
                "owner": "claude",
                "reason": "worktree missing",
                "created": time.time(),
                "reservations": [],
            }
        ),
    )
    edit(
        bridge,
        repo,
        "43",
        lambda record: record.update(deadline=time.time() - 120),
    )
    bridge.board(cli.Selection(project=str(repo)), width=None)
    lines = capsys.readouterr().out.splitlines()
    start = lines.index("Needs action:")
    at = f"--repo {shlex.quote(str(repo))}"
    assert lines[start + 1] == (
        "  #42 orphaned from claude (worktree missing); a peer lane runs "
        "agent-parley issue claim 42 --take-orphaned"
        if peer
        else "  #42 orphaned from claude (worktree missing); no live lane can "
        f"take it: start one with agent-parley run NAME {at} and have it "
        "run agent-parley issue claim 42 --take-orphaned, or run "
        f"agent-parley issue release 42 {at}"
    )
    assert lines[start + 2].startswith("  #43 overdue 2m with codex; ")
    assert lines[start + 2].endswith(
        "agent-parley issue assign 43 LANE --reason TEXT"
    )


def test_the_json_document_stays_complete_and_unscoped(
    bridge, repo, paired, tmp_path, monkeypatch, capsys
):
    other = second_project(bridge, tmp_path)
    bridge.issue(paired["lanes"]["claude"], "claim", "42")
    edit(bridge, repo, "42", ended)
    monkeypatch.chdir(repo)
    assert run(monkeypatch, bridge, "status", "--json") == 0
    document = json.loads(capsys.readouterr().out)
    roots = {project["root"] for project in document["projects"]}
    assert roots == {str(repo), str(other)}
    project = next(
        item for item in document["projects"] if item["root"] == str(repo)
    )
    assert project["dormant"] is False
    assert project["root_missing"] is False
    assert [record["issue"] for record in project["issues"]] == [42]
    claude = next(
        record
        for record in project["participants"]
        if record["participant"] == "claude"
    )
    assert {"current_task", "lane_state", "mail", "session"} <= set(claude)
    claim = claude["claims"][0]
    assert claim["issue"] == 42
    assert claim["ended"] is True
    assert claim["pull_request"] is None
    assert {"title", "last_event_seconds", "overdue", "orphaned"} <= set(claim)


def test_json_status_shows_a_declared_wait_as_its_own_reason_and_deadline(
    bridge, repo, paired, monkeypatch, capsys
):
    directory = bridge.project(repo)[1]
    until = time.time() + 900
    supervision.record_wait(
        directory, "claude", "3 background jobs", until, 3600
    )
    monkeypatch.chdir(repo)
    assert run(monkeypatch, bridge, "status", "--json") == 0
    document = json.loads(capsys.readouterr().out)
    project = next(
        item for item in document["projects"] if item["root"] == str(repo)
    )
    claude = next(
        record
        for record in project["participants"]
        if record["participant"] == "claude"
    )
    assert claude["self_wait"]["reason"] == "3 background jobs"
    assert claude["self_wait"]["until"]


def claimed(bridge, repo, paired, monkeypatch):
    """Claims #42 and #43 from the checkout and returns the forge cache."""
    bridge.issue(paired["lanes"]["claude"], "claim", "42")
    bridge.issue(paired["lanes"]["codex"], "claim", "43")
    monkeypatch.chdir(repo)
    return bridge.project(repo)[1] / FORGE_ISSUES


def board(monkeypatch, bridge, capsys, *arguments):
    """Returns the lines one status invocation prints."""
    assert run(monkeypatch, bridge, "status", *arguments) == 0
    return capsys.readouterr().out.splitlines()


def rows(lines):
    """Returns the issue numbers of the open-work rows."""
    return [line.split()[0] for line in lines if line.startswith("#")]


def poll_forge(bridge, repo):
    """Runs the supervisor's forge refresh for the fixture's project."""
    directory = bridge.project(repo)[1]
    supervision.refresh_forge_issues(directory, roster.read(directory))


def test_status_never_reads_the_forge_or_writes_its_cache(
    bridge, repo, paired, monkeypatch, capsys, offline
):
    cache = claimed(bridge, repo, paired, monkeypatch)
    offline["answer"] = {"42": {"title": "Wire the forge", "labels": []}}
    lines = board(monkeypatch, bridge, capsys)
    assert offline["calls"] == []
    assert not cache.exists()
    assert rows(lines) == ["#42", "#43"]
    assert any("unavailable (service not refreshing)" in x for x in lines)
    stale = {"read_at": time.time() - 3600, "limit": 1000, "issues": {}}
    write_json(cache, stale)
    lines = board(monkeypatch, bridge, capsys)
    assert offline["calls"] == []
    assert json.loads(cache.read_text()) == stale
    assert any("refresh failed (service not refreshing)" in x for x in lines)


def test_a_claim_newer_than_the_forge_reading_is_not_called_closed(
    bridge, repo, paired, monkeypatch, capsys
):
    directory = bridge.project(repo)[1]
    write_json(
        directory / FORGE_ISSUES,
        {
            "read_at": time.time() - 60,
            "limit": 1000,
            "issues": {"42": {"title": "Wire the forge"}},
        },
    )
    cache = claimed(bridge, repo, paired, monkeypatch)
    lines = board(monkeypatch, bridge, capsys)
    assert rows(lines) == ["#42", "#43"]
    assert not any(line.startswith("Closed or ended") for line in lines)
    reading = json.loads(cache.read_text())
    write_json(cache, {**reading, "read_at": time.time()})
    lines = board(monkeypatch, bridge, capsys)
    assert rows(lines) == ["#42"]
    assert any(
        line.startswith("Closed or ended on the forge, still owned: #43;")
        for line in lines
    )


def test_a_fresh_forge_reading_hides_closed_issues_and_titles_the_rest(
    bridge, repo, paired, monkeypatch, capsys, offline
):
    cache = claimed(bridge, repo, paired, monkeypatch)
    offline["answer"] = {"42": {"title": "Wire the forge", "labels": []}}
    poll_forge(bridge, repo)
    lines = board(monkeypatch, bridge, capsys)
    assert offline["calls"] == [FORGE_TIMEOUT]
    assert rows(lines) == ["#42"]
    assert "Wire the forge" in next(x for x in lines if x.startswith("#42"))
    assert not any(line.startswith("Forge:") for line in lines)
    assert any(
        line.startswith("Closed or ended on the forge, still owned: #43;")
        for line in lines
    )
    assert json.loads(cache.read_text())["issues"] == {
        "42": {"title": "Wire the forge"}
    }
    offline["answer"] = {}
    poll_forge(bridge, repo)
    lines = board(monkeypatch, bridge, capsys, "--all")
    assert offline["calls"] == [FORGE_TIMEOUT]
    assert rows(lines) == ["#42", "#43"]


def test_a_released_issue_closed_on_the_forge_leaves_free_work(
    bridge, repo, paired, monkeypatch, offline
):
    claimed(bridge, repo, paired, monkeypatch)
    bridge.issue(paired["lanes"]["codex"], "release", "43")
    directory = bridge.project(repo)[1]
    assert "43" in lifecycle.actionable(issues.snapshot(directory))
    offline["answer"] = {"42": {"title": "Wire the forge", "labels": []}}
    poll_forge(bridge, repo)
    ledger = issues.snapshot(directory)
    ended = ledger["issues"]["43"]
    assert ended["ended_on_forge"]
    assert lifecycle.state(ended)["next_action"] == "none"
    assert not lifecycle.state(ended)["authorized"]
    assert "43" not in lifecycle.actionable(ledger)
    assert "43" not in issues.unclaimed(ledger)
    assert "ended_on_forge" not in ledger["issues"]["42"]
    reclaimed = bridge.issue(paired["lanes"]["codex"], "claim", "43")
    assert "ended_on_forge" not in reclaimed


def test_a_failed_refresh_falls_back_to_the_stale_reading_once(
    bridge, repo, paired, monkeypatch, capsys, offline
):
    cache = claimed(bridge, repo, paired, monkeypatch)
    edit(bridge, repo, "42", older_than_reading)
    write_json(
        cache,
        {
            "read_at": time.time() - 3600,
            "limit": 1000,
            "issues": {"43": {"title": "Still open"}},
        },
    )
    poll_forge(bridge, repo)
    lines = board(monkeypatch, bridge, capsys)
    assert offline["calls"] == [FORGE_TIMEOUT]
    assert rows(lines) == ["#43"]
    assert "Still open" in next(x for x in lines if x.startswith("#43"))
    notes = [line for line in lines if line.startswith("Forge:")]
    assert len(notes) == 1
    assert "ago; refresh failed (forge unreachable)" in notes[0]
    assert json.loads(cache.read_text())["failed_at"] > 0
    offline["answer"] = {"42": {"title": "Wire", "labels": []}}
    poll_forge(bridge, repo)
    lines = board(monkeypatch, bridge, capsys)
    assert offline["calls"] == [FORGE_TIMEOUT]
    assert rows(lines) == ["#43"]


def test_no_forge_reading_hides_nothing_and_says_so(
    bridge, repo, paired, monkeypatch, capsys, offline
):
    cache = claimed(bridge, repo, paired, monkeypatch)
    poll_forge(bridge, repo)
    lines = board(monkeypatch, bridge, capsys)
    assert rows(lines) == ["#42", "#43"]
    assert [line for line in lines if line.startswith("Forge:")] == [
        "Forge: open issues unavailable (forge unreachable); claims on "
        "closed issues are not hidden"
    ]
    monkeypatch.setattr(forge, "select", lambda root, manifest: "null")
    cache.unlink()
    poll_forge(bridge, repo)
    assert not cache.exists()
    lines = board(monkeypatch, bridge, capsys)
    assert offline["calls"] == [FORGE_TIMEOUT]
    assert rows(lines) == ["#42", "#43"]
    assert any("unavailable (no GitHub)" in line for line in lines)


def lane_reading(bridge, repo, name="claude"):
    """Returns one lane's record from a fresh status reading."""
    project = next(
        item
        for item in bridge.status_snapshot()["projects"]
        if item["root"] == str(repo)
    )
    return next(
        record
        for record in project["participants"]
        if record["participant"] == name
    )


def test_a_report_on_a_held_issue_is_the_task_line(bridge, repo, paired):
    lane = paired["lanes"]["claude"]
    bridge.issue(lane, "claim", "42")
    bridge.issue(lane, "claim", "43")
    bridge.report(lane, "partial", "Wiring the engine", "tests", "", issue="42")
    record = lane_reading(bridge, repo)
    assert record["current_task"] == "Wiring the engine"
    assert record["report_issue"] == 42
    assert record["report_held"] is True
    assert [claim["reported_since_claim"] for claim in record["claims"]] == [
        True,
        False,
    ]


def test_a_report_on_a_released_issue_is_labelled_and_silent_claims_named(
    bridge, repo, paired
):
    lane = paired["lanes"]["claude"]
    bridge.issue(lane, "claim", "42")
    bridge.report(lane, "partial", "Issue closed", "nothing", "", issue="42")
    bridge.issue(lane, "release", "42")
    bridge.issue(lane, "claim", "43")
    bridge.issue(lane, "claim", "44")
    path = bridge.project(repo)[1] / "claude-activity.json"
    state = json.loads(path.read_text())
    state["reported_at"] = time.time() - 1260
    write_json(path, state)
    record = lane_reading(bridge, repo)
    assert record["report_issue"] == 42
    assert record["report_held"] is False
    assert record["current_task"] == (
        "no report on held #43, #44; "
        "#42 (not held, reported 21m ago): Issue closed"
    )


def test_a_held_blocked_report_followed_by_mail_on_its_issue_is_labelled(
    bridge, repo, paired
):
    lane = paired["lanes"]["claude"]
    bridge.issue(lane, "claim", "42")
    bridge.report(
        lane,
        "blocked",
        "Kit drafted; none posted",
        "operator posts",
        "",
        issue="42",
    )
    path = bridge.project(repo)[1] / "claude-activity.json"
    state = json.loads(path.read_text())
    state["reported_at"] = time.time() - 3480
    write_json(path, state)
    assert lane_reading(bridge, repo)["current_task"] == (
        "Kit drafted; none posted"
    )
    store.initialize(bridge.home)
    actors = {
        name: store.authenticate(
            bridge.home,
            store.register(bridge.home, paired["root"], name)[
                "registration_token"
            ],
        )
        for name in ("claude", "codex")
    }
    for key, sender, recipient, subject in (
        ("posted", "codex", "claude", "Posted the #42 comments"),
        ("decided", "claude", "codex", "Decision on #42"),
        ("other", "codex", "claude", "Started #420"),
    ):
        store.call(
            bridge.home,
            actors[sender],
            "send_message",
            {
                "to": [recipient],
                "subject": subject,
                "body_md": "Done.",
                "idempotency_key": key,
            },
        )
    record = lane_reading(bridge, repo)
    assert record["report_newer_mail"] == 2
    assert record["current_task"] == (
        "#42 (reported 58m ago, 2 newer messages on it): "
        "Kit drafted; none posted"
    )


def long_dead(bridge, monkeypatch, age=3 * 86400):
    """Reads claude as a dead lane whose report and activity are `age` old."""
    real = bridge.status_snapshot

    def reading():
        report = real()
        for project in report["projects"]:
            for record in project["participants"]:
                if record["participant"] == "claude":
                    record.update(lane_state="dead", report_age_seconds=age)
                    record["availability"].update(
                        process_alive=False, age_seconds=age
                    )
        return report

    monkeypatch.setattr(bridge, "status_snapshot", reading)


def lane_rows(output):
    """Returns the LANE table rows of one board, keyed by lane name."""
    return {
        line.split()[0]: line
        for line in output.splitlines()
        if line.split()[:1] in (["claude"], ["codex"])
    }


def test_a_dead_lane_without_claims_past_retention_is_folded(
    bridge, repo, paired, monkeypatch, capsys
):
    long_dead(bridge, monkeypatch)
    bridge.board(cli.Selection(project=str(repo)), width=None)
    output = capsys.readouterr().out
    assert "claude" not in lane_rows(output)
    assert "codex" in lane_rows(output)
    assert "1 dead lane older than 24h hidden; --all shows them" in output


def test_a_dead_lane_inside_retention_stays_listed(
    bridge, repo, paired, monkeypatch, capsys
):
    long_dead(bridge, monkeypatch, age=3600)
    bridge.board(cli.Selection(project=str(repo)), width=None)
    output = capsys.readouterr().out
    assert lane_rows(output)["claude"].split()[1] == "dead"
    assert "hidden; --all shows them" not in output


def test_a_dead_lane_still_holding_a_claim_stays_listed(
    bridge, repo, paired, monkeypatch, capsys
):
    bridge.issue(paired["lanes"]["claude"], "claim", "42")
    long_dead(bridge, monkeypatch)
    bridge.board(cli.Selection(project=str(repo)), width=None)
    output = capsys.readouterr().out
    assert lane_rows(output)["claude"].split()[1:3] == ["dead", "1"]
    assert "dead lane older than" not in output


def test_status_all_lists_a_folded_lane(
    bridge, repo, paired, monkeypatch, capsys
):
    long_dead(bridge, monkeypatch)
    bridge.board(cli.Selection(project=str(repo)), width=None, every_claim=True)
    output = capsys.readouterr().out
    assert lane_rows(output)["claude"].split()[1] == "dead"
    assert "hidden; --all shows them" not in output


def test_status_reading_carries_the_project_retention(bridge, repo, paired):
    [project] = bridge.status_snapshot()["projects"]
    assert project["fold_after"] == supervision.DEFAULTS["fold_after"]


def test_fold_after_is_bounded_like_every_other_interval():
    assert supervision.settings({})["fold_after"] == 86400
    with pytest.raises(cli.BridgeError, match="fold_after"):
        supervision.settings({"fold_after": 0})
