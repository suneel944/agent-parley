"""Checks the version, show and list verbs the command surface offers."""

import json
import sys

import pytest

from agent_parley import cli, protocol, roster, store, views

NEW_COMMANDS = (
    ("version",),
    ("issue", "show", "--help"),
    ("participant", "show", "--help"),
    ("mail", "list", "--help"),
    ("mail", "send", "--help"),
    ("provider", "show", "--help"),
    ("credentials", "show", "--help"),
    ("problems", "ack", "--help"),
    ("events", "show", "--help"),
    ("up", "--help"),
    ("down", "--help"),
    ("setup", "--help"),
    ("run", "--help"),
    ("say", "--help"),
    ("approve", "--help"),
    ("reject", "--help"),
    ("status", "--help"),
    ("top", "--help"),
    ("approval", "show", "--help"),
    ("branch", "show", "--help"),
    ("forge", "show", "--help"),
    ("state", "show", "--help"),
    ("history", "issue", "--help"),
)


def run(monkeypatch, capsys, *arguments):
    """Runs one CLI invocation and returns its parsed standard output."""
    monkeypatch.setattr(sys, "argv", ["agent-parley", *arguments])
    assert cli.main() == 0
    return json.loads(capsys.readouterr().out)


def text(monkeypatch, capsys, *arguments):
    """Runs one CLI invocation and returns its printed text."""
    monkeypatch.setattr(sys, "argv", ["agent-parley", *arguments])
    assert cli.main() == 0
    return capsys.readouterr().out


def envelope(document, kind):
    """Asserts the shared snapshot envelope and returns the document."""
    assert document["schema"] == views.SCHEMA
    assert document["kind"] == kind
    assert document["generated_at"].endswith("Z")
    return document


@pytest.mark.parametrize("arguments", NEW_COMMANDS)
def test_every_new_command_prints_its_own_help(
    bridge, monkeypatch, capsys, arguments
):
    if arguments == ("version",):
        assert protocol.launcher_version() in text(
            monkeypatch, capsys, "--home", str(bridge.home), "version"
        )
        return
    monkeypatch.setattr(sys, "argv", ["agent-parley", *arguments])
    with pytest.raises(SystemExit) as exit_status:
        cli.main()
    assert exit_status.value.code == 0
    assert "usage: agent-parley" in capsys.readouterr().out


def test_version_flag_prints_the_installed_version(monkeypatch, capsys):
    assert text(monkeypatch, capsys, "--version").strip() == (
        protocol.launcher_version()
    )
    assert text(monkeypatch, capsys, "-V").strip() == (
        protocol.launcher_version()
    )


def test_version_command_reports_the_state_directory(
    bridge, monkeypatch, capsys
):
    printed = text(monkeypatch, capsys, "--home", str(bridge.home), "version")
    assert protocol.launcher_version() in printed
    assert str(bridge.home) in printed
    document = envelope(
        run(
            monkeypatch,
            capsys,
            "--home",
            str(bridge.home),
            "version",
            "--json",
        ),
        "version",
    )
    assert document["version"] == protocol.launcher_version()
    assert document["state_directory"] == str(bridge.home)


def test_bare_invocation_prints_grouped_help(monkeypatch, capsys):
    printed = text(monkeypatch, capsys)
    headings = [title for title, _ in cli.COMMAND_GROUPS]
    positions = [printed.index(f"{title}:") for title in headings]
    assert positions == sorted(positions)
    assert "Other:" not in printed
    assert "Run `agent-parley COMMAND --help`" in printed


def test_grouped_help_lists_every_declared_command(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["agent-parley", "--help"])
    with pytest.raises(SystemExit) as exit_status:
        cli.main()
    assert exit_status.value.code == 0
    printed = capsys.readouterr().out
    assert printed == text(monkeypatch, capsys)
    grouped = {name for _, names in cli.COMMAND_GROUPS for name in names}
    for name in grouped:
        assert f"  {name} " in printed
    assert "__complete" not in printed


def test_status_accepts_repo_as_the_project_selector(
    bridge, repo, paired, monkeypatch, capsys
):
    selected = envelope(
        run(
            monkeypatch,
            capsys,
            "--home",
            str(bridge.home),
            "status",
            "--repo",
            paired["root"],
            "--json",
        ),
        "status",
    )
    assert [project["root"] for project in selected["projects"]] == [
        paired["root"]
    ]
    legacy = envelope(
        run(
            monkeypatch,
            capsys,
            "--home",
            str(bridge.home),
            "status",
            "--project",
            paired["root"],
            "--json",
        ),
        "status",
    )
    assert legacy["projects"] == selected["projects"]
    missing = envelope(
        run(
            monkeypatch,
            capsys,
            "--home",
            str(bridge.home),
            "status",
            "--repo",
            "no-such-project",
            "--json",
        ),
        "status",
    )
    assert missing["projects"] == []


def test_top_accepts_repo_as_the_project_selector(
    bridge, repo, paired, monkeypatch, capsys
):
    document = envelope(
        run(
            monkeypatch,
            capsys,
            "--home",
            str(bridge.home),
            "top",
            "--repo",
            paired["root"],
            "--json",
        ),
        "top",
    )
    assert [project["root"] for project in document["projects"]] == [
        paired["root"]
    ]


def test_issue_show_reports_owner_blockers_and_history(
    bridge, repo, paired, monkeypatch, capsys
):
    lane = paired["lanes"]["claude"]
    bridge.issue(lane, "claim", "42")
    bridge.issue(lane, "block", "42", on="7")
    document = envelope(
        run(
            monkeypatch,
            capsys,
            "--home",
            str(bridge.home),
            "issue",
            "show",
            "42",
            "--repo",
            str(lane),
            "--json",
        ),
        "issue",
    )
    assert document["issue"] == 42
    assert document["record"]["owner"] == "claude"
    assert document["record"]["blocked_by"] == [7]
    assert document["reservations"] == []
    assert document["history"]["subject"] == "issue"
    printed = text(
        monkeypatch,
        capsys,
        "--home",
        str(bridge.home),
        "issue",
        "show",
        "42",
        "--repo",
        str(lane),
    )
    assert "Issue #42: claude" in printed
    assert "Blocked by: #7" in printed


def test_issue_show_reports_an_issue_the_ledger_never_recorded(
    bridge, repo, paired, monkeypatch, capsys
):
    lane = paired["lanes"]["claude"]
    document = envelope(
        run(
            monkeypatch,
            capsys,
            "--home",
            str(bridge.home),
            "issue",
            "show",
            "99",
            "--repo",
            str(lane),
            "--json",
        ),
        "issue",
    )
    assert document["record"] is None


def test_participant_show_reports_one_lane(
    bridge, repo, paired, monkeypatch, capsys
):
    document = envelope(
        run(
            monkeypatch,
            capsys,
            "--home",
            str(bridge.home),
            "participant",
            "show",
            "claude",
            "--repo",
            str(repo),
            "--json",
        ),
        "participant",
    )
    assert document["participant"] == "claude"
    assert document["worktree"] == str(paired["lanes"]["claude"])
    assert document["assigned_branch"]
    assert document["provider"] == "claude"
    assert document["budget_limits"] == {}
    assert document["wake_enabled"] is True
    printed = text(
        monkeypatch,
        capsys,
        "--home",
        str(bridge.home),
        "participant",
        "show",
        "claude",
        "--repo",
        str(repo),
    )
    assert "claude" in printed
    assert "Provider: claude" in printed


def test_participant_show_refuses_an_unknown_lane(
    bridge, repo, paired, monkeypatch, capsys
):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agent-parley",
            "--home",
            str(bridge.home),
            "participant",
            "show",
            "nobody",
            "--repo",
            str(repo),
        ],
    )
    assert cli.main() == 1
    assert "No participant named" in capsys.readouterr().err


def test_mail_list_reports_the_inbox_without_a_query(
    bridge, repo, paired, monkeypatch, capsys
):
    lane = paired["lanes"]["claude"]
    store.initialize(bridge.home)
    store.register(bridge.home, paired["root"], "claude")
    delivered = bridge.say(repo, "claude", "Read the shared fixture")
    document = envelope(
        run(
            monkeypatch,
            capsys,
            "--home",
            str(bridge.home),
            "mail",
            "list",
            "--repo",
            str(lane),
            "--json",
        ),
        "mail_list",
    )
    assert document["messages"][0]["id"] == delivered["id"]
    assert document["has_more"] is False
    assert document["limit"] == store.MAX_SEARCH_HITS


def test_mail_send_delivers_the_same_message_as_say(
    bridge, repo, paired, monkeypatch, capsys
):
    lane = paired["lanes"]["claude"]
    store.initialize(bridge.home)
    store.register(bridge.home, paired["root"], "claude")
    document = envelope(
        run(
            monkeypatch,
            capsys,
            "--home",
            str(bridge.home),
            "mail",
            "send",
            "claude",
            "Rebase before the pull request",
            "--repo",
            str(repo),
            "--json",
        ),
        "say",
    )
    assert document["participant"] == "claude"
    listed = envelope(
        run(
            monkeypatch,
            capsys,
            "--home",
            str(bridge.home),
            "mail",
            "list",
            "--repo",
            str(lane),
            "--json",
        ),
        "mail_list",
    )
    assert listed["messages"][0]["id"] == document["message"]["id"]


def test_say_reports_the_delivered_message_as_json(
    bridge, repo, paired, monkeypatch, capsys
):
    store.initialize(bridge.home)
    store.register(bridge.home, paired["root"], "claude")
    document = envelope(
        run(
            monkeypatch,
            capsys,
            "--home",
            str(bridge.home),
            "say",
            "claude",
            "Check the coordination state",
            "--repo",
            str(repo),
            "--json",
        ),
        "say",
    )
    assert document["message"]["id"] > 0


def test_problems_ack_clears_an_awaited_acknowledgement(
    bridge, repo, paired, monkeypatch, capsys
):
    store.initialize(bridge.home)
    store.register(bridge.home, paired["root"], "claude")
    delivered = bridge.say(repo, "claude", "Answer this", "", "", True)
    document = envelope(
        run(
            monkeypatch,
            capsys,
            "--home",
            str(bridge.home),
            "problems",
            "ack",
            str(delivered["id"]),
            "--repo",
            str(repo),
            "--json",
        ),
        "problems_ack",
    )
    assert document["id"] == delivered["id"]
    assert document["participants"] == ["claude"]
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agent-parley",
            "--home",
            str(bridge.home),
            "problems",
            "ack",
            str(delivered["id"]),
            "--repo",
            str(repo),
        ],
    )
    assert cli.main() == 1
    assert "awaits no acknowledgement" in capsys.readouterr().err


def test_provider_show_reports_one_definition(bridge, monkeypatch, capsys):
    document = envelope(
        run(
            monkeypatch,
            capsys,
            "--home",
            str(bridge.home),
            "provider",
            "show",
            "claude",
            "--json",
        ),
        "provider",
    )
    assert document["provider"] == "claude"
    assert document["adapter"]
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agent-parley",
            "--home",
            str(bridge.home),
            "provider",
            "show",
            "nothing",
        ],
    )
    assert cli.main() == 1
    assert "Unknown provider" in capsys.readouterr().err


def test_credentials_show_redacts_every_recorded_value(
    bridge, monkeypatch, capsys
):
    roster.define_credential(
        bridge.home,
        "work",
        str(bridge.home / "work"),
        ["ANTHROPIC_BASE_URL=https://example.invalid"],
        ["ANTHROPIC_API_KEY"],
    )
    document = envelope(
        run(
            monkeypatch,
            capsys,
            "--home",
            str(bridge.home),
            "credentials",
            "show",
            "work",
            "--json",
        ),
        "credentials_show",
    )
    assert document["credential"] == "work"
    assert document["env"] == ["ANTHROPIC_BASE_URL=<redacted>"]
    assert document["require_env"] == ["ANTHROPIC_API_KEY"]
    printed = text(
        monkeypatch,
        capsys,
        "--home",
        str(bridge.home),
        "credentials",
        "show",
        "work",
    )
    assert "example.invalid" not in printed


def test_policy_show_commands_print_documents(
    bridge, repo, paired, monkeypatch, capsys
):
    approval = envelope(
        run(
            monkeypatch,
            capsys,
            "--home",
            str(bridge.home),
            "approval",
            "show",
            "--repo",
            str(repo),
            "--json",
        ),
        "approval",
    )
    assert approval["root"] == paired["root"]
    assert approval["required"] is bool(approval["approval"])
    branch = envelope(
        run(
            monkeypatch,
            capsys,
            "--home",
            str(bridge.home),
            "branch",
            "show",
            "--repo",
            str(repo),
            "--json",
        ),
        "branch",
    )
    assert branch["prefix"]
    tracked = envelope(
        run(
            monkeypatch,
            capsys,
            "--home",
            str(bridge.home),
            "forge",
            "show",
            "--repo",
            str(repo),
            "--json",
        ),
        "forge",
    )
    assert tracked["forge"]


def test_setup_and_approval_decisions_print_documents(
    bridge, repo, paired, monkeypatch, capsys
):
    registered = envelope(
        run(
            monkeypatch,
            capsys,
            "--home",
            str(bridge.home),
            "setup",
            str(repo),
            "--json",
        ),
        "setup",
    )
    assert registered["root"] == paired["root"]
    bridge.report(
        paired["lanes"]["claude"], "ready", "Work is ready", "", "make check"
    )
    approved = envelope(
        run(
            monkeypatch,
            capsys,
            "--home",
            str(bridge.home),
            "approve",
            "claude",
            "--repo",
            str(repo),
            "--json",
        ),
        "approve",
    )
    assert approved["participant"] == "claude"
    assert approved["decision"] == "approved"
    rejected = envelope(
        run(
            monkeypatch,
            capsys,
            "--home",
            str(bridge.home),
            "reject",
            "claude",
            "Rebase first",
            "--repo",
            str(repo),
            "--json",
        ),
        "reject",
    )
    assert rejected["reason"] == "Rebase first"


def test_events_show_prints_the_records_export_writes(
    bridge, repo, paired, monkeypatch, capsys
):
    shown = text(
        monkeypatch,
        capsys,
        "--home",
        str(bridge.home),
        "events",
        "show",
        "--repo",
        str(repo),
    )
    exported = text(
        monkeypatch,
        capsys,
        "--home",
        str(bridge.home),
        "events",
        "export",
        "--repo",
        str(repo),
    )
    assert shown == exported


def test_history_exports_one_document_to_a_file(
    bridge, repo, paired, tmp_path, monkeypatch, capsys
):
    lane = paired["lanes"]["claude"]
    bridge.issue(lane, "claim", "42")
    destination = tmp_path / "history.json"
    printed = text(
        monkeypatch,
        capsys,
        "--home",
        str(bridge.home),
        "history",
        "issue",
        "42",
        "--repo",
        str(lane),
        "--output",
        str(destination),
    )
    assert str(destination) in printed
    document = envelope(json.loads(destination.read_text()), "history")
    assert document["value"] == "42"


def test_state_show_prints_the_archive_manifest(
    bridge, repo, paired, tmp_path, monkeypatch, capsys
):
    archive_path = tmp_path / "state.tar"
    text(
        monkeypatch,
        capsys,
        "--home",
        str(bridge.home),
        "state",
        "export",
        "--output",
        str(archive_path),
    )
    document = envelope(
        run(
            monkeypatch,
            capsys,
            "--home",
            str(bridge.home),
            "state",
            "show",
            str(archive_path),
            "--json",
        ),
        "state",
    )
    assert document["archive"] == str(archive_path)
    assert document["manifest"]["projects"][0]["root"] == paired["root"]


def test_up_and_down_report_the_service_as_json(bridge, monkeypatch, capsys):
    started = envelope(
        run(monkeypatch, capsys, "--home", str(bridge.home), "up", "--json"),
        "up",
    )
    assert started["url"].endswith("/mcp/")
    assert started["state_directory"] == str(bridge.home)
    stopped = envelope(
        run(monkeypatch, capsys, "--home", str(bridge.home), "down", "--json"),
        "down",
    )
    assert stopped["stopped"] is True


def test_mail_cancel_reports_the_outcome_as_json(
    bridge, repo, paired, monkeypatch, capsys
):
    store.initialize(bridge.home)
    store.register(bridge.home, paired["root"], "claude")
    bridge.say(repo, "claude", "Later", "", "", False, None, after=3600)
    pending = envelope(
        run(
            monkeypatch,
            capsys,
            "--home",
            str(bridge.home),
            "mail",
            "pending",
            "--repo",
            str(repo),
            "--json",
        ),
        "mail_pending",
    )
    item = pending["pending"][0]["id"]
    document = envelope(
        run(
            monkeypatch,
            capsys,
            "--home",
            str(bridge.home),
            "mail",
            "cancel",
            str(item),
            "--repo",
            str(repo),
            "--json",
        ),
        "mail_cancel",
    )
    assert document["cancelled"] is True
