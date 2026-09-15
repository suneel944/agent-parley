"""Checks the status table, its filters and the gate they drive."""

import asyncio
import json
import sys

import pytest

from agent_parley import checkpoints, cli, store
from agent_parley.state import write_json


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


def printed(capsys):
    """Returns the captured lines of one status reading."""
    return capsys.readouterr().out.splitlines()


def rows_of(lines):
    """Returns the participant rows of every table in a reading."""
    rows = []
    inside = False
    for line in lines:
        if line.startswith("PARTICIPANT"):
            inside = True
        elif not line or line.startswith("Hidden columns:"):
            inside = False
        elif inside:
            rows.append(line)
    return rows


def run(monkeypatch, *arguments):
    """Runs one CLI invocation and returns its exit status."""
    monkeypatch.setattr(sys, "argv", ["agent-parley", *arguments])
    return cli.main()


def test_one_project_prints_one_table(bridge, repo, paired, capsys):
    assert bridge.status() == 2
    lines = printed(capsys)
    heading = next(line for line in lines if line.startswith("PARTICIPANT"))
    assert heading.split() == [
        "PARTICIPANT",
        "PROVIDER",
        "ACCOUNT",
        "SESSION",
        "BRANCH",
        "OUTCOME",
        "ISSUES",
        "MAIL",
        "LEASES",
        "REPORTED",
        "TASK",
    ]
    assert sum(line.startswith("PARTICIPANT") for line in lines) == 1
    assert [row.split()[0] for row in rows_of(lines)] == ["claude", "codex"]
    assert "Reported outcome" not in "\n".join(lines)


def test_several_projects_each_get_their_own_table(
    bridge, repo, paired, tmp_path, capsys
):
    other = second_project(bridge, tmp_path)
    assert bridge.status() == 3
    lines = printed(capsys)
    assert sum(line.startswith("PARTICIPANT") for line in lines) == 2
    assert f"Project: {other}" in lines
    assert f"Project: {repo}" in lines


def test_a_named_participant_replaces_its_row_with_the_reading(
    bridge, repo, paired, capsys
):
    assert bridge.status(cli.Selection(participant="claude")) == 1
    output = "\n".join(printed(capsys))
    assert "PARTICIPANT" not in output
    assert "claude (claude):" in output
    assert "Reported outcome: unknown" in output
    assert "codex (codex):" not in output


def test_each_filter_narrows_the_reported_rows(bridge, repo, paired, capsys):
    bridge.issue(paired["lanes"]["claude"], "claim", "42")
    assert bridge.status(cli.Selection(providers=("codex",))) == 1
    assert [row.split()[0] for row in rows_of(printed(capsys))] == ["codex"]
    assert bridge.status(cli.Selection(issue=42)) == 1
    assert [row.split()[0] for row in rows_of(printed(capsys))] == ["claude"]
    assert bridge.status(cli.Selection(outcome="unknown")) == 2
    capsys.readouterr()
    assert bridge.status(cli.Selection(project=str(repo))) == 2
    capsys.readouterr()


def test_a_drifted_lane_is_the_only_one_reported(bridge, repo, paired, capsys):
    cli.git(paired["lanes"]["claude"], "checkout", "-b", "elsewhere")
    assert bridge.status(cli.Selection(drifted=True)) == 1
    rows = rows_of(printed(capsys))
    assert len(rows) == 1
    assert rows[0].startswith("claude")
    assert "elsewhere!" in rows[0]


def test_a_filter_matching_nothing_says_which_one(bridge, repo, paired, capsys):
    assert bridge.status(cli.Selection(outcome="ready")) == 0
    lines = printed(capsys)
    assert lines[-1] == "No participant matches --outcome ready."
    assert not [line for line in lines if line.startswith("PARTICIPANT")]


def test_an_unknown_participant_reports_the_empty_selection(
    bridge, repo, paired, capsys
):
    assert bridge.status(cli.Selection(participant="absent")) == 0
    assert printed(capsys)[-1] == "No participant matches participant absent."


def test_drift_and_pending_work_fail_the_shell_gate(
    bridge, repo, paired, monkeypatch, capsys
):
    assert run(monkeypatch, "--home", str(bridge.home), "status") == 0
    capsys.readouterr()
    for flag in ("--drifted", "--pending"):
        assert run(monkeypatch, "--home", str(bridge.home), "status", flag) == 0
        capsys.readouterr()
    cli.git(paired["lanes"]["codex"], "checkout", "-b", "elsewhere")
    bridge.issue(paired["lanes"]["claude"], "claim", "42")
    bridge.issue(
        paired["lanes"]["claude"], "offer", "42", to="codex", summary="ready"
    )
    for flag in ("--drifted", "--pending"):
        assert run(monkeypatch, "--home", str(bridge.home), "status", flag) == 1
        assert [row.split()[0] for row in rows_of(printed(capsys))] == ["codex"]


def test_an_idle_lane_is_reported_by_its_own_inactivity(
    bridge, repo, paired, capsys
):
    directory = bridge.project(repo)[1]
    write_json(
        directory / "claude-activity.json",
        {"session_pid": 1, "session_ticks": "0"},
    )
    assert bridge.status(cli.Selection(idle=True, since=60)) == 0
    assert printed(capsys)[-1].startswith("No participant matches --idle")


def test_the_json_document_carries_only_the_matching_rows(
    bridge, repo, paired, monkeypatch, capsys
):
    assert (
        run(
            monkeypatch,
            "--home",
            str(bridge.home),
            "status",
            "--provider",
            "codex",
            "--json",
        )
        == 0
    )
    document = json.loads(capsys.readouterr().out)
    assert document["kind"] == "status"
    reported = document["projects"][0]["participants"]
    assert [record["participant"] for record in reported] == ["codex"]
    assert document["projects"][0]["issues"] == []


@pytest.mark.parametrize("width", [40, 80, 200])
def test_a_narrow_terminal_drops_columns_and_says_so(
    bridge, repo, paired, width, capsys
):
    bridge.report(
        paired["lanes"]["claude"], "ready", "Engine built", "", "checks pass"
    )
    bridge.status(width=width)
    lines = printed(capsys)
    table = [
        line for line in lines if line.startswith(("PARTICIPANT", "claude"))
    ]
    assert table
    assert all(len(line) <= width for line in table)
    assert "PARTICIPANT" in table[0]
    if width < 100:
        assert any(line.startswith("Hidden columns:") for line in lines)


def test_a_harness_notification_leaves_the_operator_task_standing(
    bridge, repo, paired, capsys
):
    directory = bridge.project(repo)[1]
    store.initialize(bridge.home)
    store.authenticate(
        bridge.home,
        asyncio.run(bridge.identity("claude", paired))["registration_token"],
    )
    notification = (
        "<task-notification> <task-id>b6zl98ow2</task-id> "
        "<output-file>/tmp/claude-1000/task.log</output-file>"
    )
    for prompt in ("Wire the dashboard", notification):
        checkpoints.checkpoint(
            bridge.home,
            directory,
            "claude",
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "test",
                "cwd": paired["lanes"]["claude"],
                "prompt": prompt,
            },
        )
    bridge.status(width=None)
    row = next(
        line for line in rows_of(printed(capsys)) if line.startswith("claude")
    )
    assert "Wire the dashboard" in row
    assert "task-notification" not in row


def test_a_quoted_harness_tag_still_reads_as_an_operator_prompt():
    assert not checkpoints.operator_prompt("  <system-reminder> hold edits")
    assert checkpoints.operator_prompt("Explain <system-reminder> to me")


def test_an_unbounded_table_keeps_every_column(bridge, repo, paired, capsys):
    bridge.status(width=None)
    lines = printed(capsys)
    assert not [line for line in lines if line.startswith("Hidden columns:")]
    assert "TASK" in next(
        line for line in lines if line.startswith("PARTICIPANT")
    )
