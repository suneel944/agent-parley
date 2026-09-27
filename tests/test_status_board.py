"""Checks the open-work status view, its scoping and its filters."""

import asyncio
import json
import shutil
import sys
import time
from pathlib import Path

import pytest

from agent_parley import checkpoints, cli, forge, issues, store, supervision
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


def test_orphaned_and_overdue_claims_name_the_resolving_command(
    bridge, repo, paired, capsys
):
    bridge.issue(paired["lanes"]["claude"], "claim", "42")
    bridge.issue(paired["lanes"]["codex"], "claim", "43")
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
    assert lines[start + 1] == (
        "  #42 orphaned from claude (worktree missing); a peer lane runs "
        "agent-parley issue claim 42 --take-orphaned"
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


def test_a_fresh_forge_reading_hides_closed_issues_and_titles_the_rest(
    bridge, repo, paired, monkeypatch, capsys, offline
):
    cache = claimed(bridge, repo, paired, monkeypatch)
    offline["answer"] = {"42": {"title": "Wire the forge", "labels": []}}
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
    lines = board(monkeypatch, bridge, capsys, "--all")
    assert offline["calls"] == [FORGE_TIMEOUT]
    assert rows(lines) == ["#42", "#43"]


def test_a_failed_refresh_falls_back_to_the_stale_reading_once(
    bridge, repo, paired, monkeypatch, capsys, offline
):
    cache = claimed(bridge, repo, paired, monkeypatch)
    write_json(
        cache,
        {
            "read_at": time.time() - 3600,
            "limit": 1000,
            "issues": {"43": {"title": "Still open"}},
        },
    )
    lines = board(monkeypatch, bridge, capsys)
    assert offline["calls"] == [FORGE_TIMEOUT]
    assert rows(lines) == ["#43"]
    assert "Still open" in next(x for x in lines if x.startswith("#43"))
    notes = [line for line in lines if line.startswith("Forge:")]
    assert len(notes) == 1
    assert "ago; refresh failed (forge unreachable)" in notes[0]
    assert json.loads(cache.read_text())["failed_at"] > 0
    offline["answer"] = {"42": {"title": "Wire", "labels": []}}
    lines = board(monkeypatch, bridge, capsys)
    assert offline["calls"] == [FORGE_TIMEOUT]
    assert rows(lines) == ["#43"]


def test_no_forge_reading_hides_nothing_and_says_so(
    bridge, repo, paired, monkeypatch, capsys, offline
):
    cache = claimed(bridge, repo, paired, monkeypatch)
    lines = board(monkeypatch, bridge, capsys)
    assert rows(lines) == ["#42", "#43"]
    assert [line for line in lines if line.startswith("Forge:")] == [
        "Forge: open issues unavailable (forge unreachable); claims on "
        "closed issues are not hidden"
    ]
    monkeypatch.setattr(forge, "select", lambda root, manifest: "null")
    cache.unlink()
    lines = board(monkeypatch, bridge, capsys)
    assert offline["calls"] == [FORGE_TIMEOUT]
    assert rows(lines) == ["#42", "#43"]
    assert any("unavailable (no GitHub)" in line for line in lines)
