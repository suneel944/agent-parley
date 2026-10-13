"""Checks the version, show and list verbs the command surface offers."""

import argparse
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
    ("approval", "resume", "--help"),
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


def help_text(monkeypatch, capsys):
    """Runs `--help` and returns its printed text."""
    monkeypatch.setattr(sys, "argv", ["agent-parley", "--help"])
    with pytest.raises(SystemExit) as exit_status:
        cli.main()
    assert exit_status.value.code == 0
    return capsys.readouterr().out


def test_help_prints_grouped_commands(monkeypatch, capsys):
    printed = help_text(monkeypatch, capsys)
    headings = [cli.START_HERE[0]] + [title for title, _ in cli.COMMAND_GROUPS]
    positions = [printed.index(f"{title}:") for title in headings]
    assert positions == sorted(positions)
    assert "Other:" not in printed
    assert "Run `agent-parley COMMAND --help`" in printed


def test_grouped_help_lists_every_declared_command(monkeypatch, capsys):
    printed = help_text(monkeypatch, capsys)
    monkeypatch.setattr(sys, "argv", ["agent-parley", "-h"])
    with pytest.raises(SystemExit):
        cli.main()
    assert printed == capsys.readouterr().out
    grouped = {name for _, names in cli.COMMAND_GROUPS for name in names}
    for name in grouped:
        assert f"  {name} " in printed
    assert "__complete" not in printed


@pytest.mark.parametrize(
    "arguments",
    (
        ("issue", "show", "829"),
        ("participant", "show", "claude-2"),
        ("gc", "--dry-run"),
        ("issue", "release", "829"),
    ),
)
def test_outside_a_checkout_names_repo(
    tmp_path, monkeypatch, capsys, arguments
):
    outside = tmp_path / "plain"
    outside.mkdir()
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    monkeypatch.chdir(outside)
    monkeypatch.setattr(
        sys,
        "argv",
        ["agent-parley", "--home", str(tmp_path / "home"), *arguments],
    )
    assert cli.main() == 1
    assert capsys.readouterr().err == (
        "agent-parley: not inside a Git repository; pass --repo PATH or "
        "run from the checkout\n"
    )


@pytest.mark.parametrize(
    ("arguments", "usage"),
    (
        (("say",), "usage: agent-parley say "),
        (("report",), "usage: agent-parley report "),
        (("history",), "usage: agent-parley history "),
        (("mail", "send"), "usage: agent-parley mail send "),
    ),
)
def test_missing_argument_prints_the_command_usage(
    tmp_path, monkeypatch, capsys, arguments, usage
):
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(
        sys,
        "argv",
        ["agent-parley", "--home", str(tmp_path / "home"), *arguments],
    )
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    assert stopped.value.code == 2
    printed = capsys.readouterr().err
    assert printed.startswith(usage)
    assert f"\nagent-parley {arguments[0]}" in printed


def test_every_subcommand_action_carries_help():
    parser, _ = cli.root_parser(None)
    pending = [((), parser)]
    bare = []
    while pending:
        path, current = pending.pop()
        for action in current._actions:
            if not isinstance(action, argparse._SubParsersAction):
                continue
            helped = {
                id(action.choices[choice.dest])
                for choice in action._choices_actions
            }
            for name, child in action.choices.items():
                if id(child) not in helped:
                    bare.append(" ".join((*path, name)))
                pending.append(((*path, name), child))
    assert bare == []


@pytest.mark.parametrize(
    ("command", "described"),
    (
        ("issue", "Ask the holder of an issue to hand it to this lane."),
        ("participant", "Push the lane branch and open or locate its pull"),
        ("mail", "Search this lane's mail."),
        ("unattended", "Integrate one eligible lane under the policy."),
        ("approval", "Show which steps require a recorded approval first."),
        ("decision", "List or search the decisions recorded for this"),
    ),
)
def test_group_help_describes_each_action(
    monkeypatch, capsys, command, described
):
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(sys, "argv", ["agent-parley", command, "--help"])
    with pytest.raises(SystemExit):
        cli.main()
    assert described in " ".join(capsys.readouterr().out.split())


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
    bridge.issue(paired["lanes"]["codex"], "claim", "7")
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


@pytest.mark.parametrize(
    ("canonical", "alias"),
    [
        (
            ("say", "claude", "Rebase", "--key", "k", "--json"),
            ("mail", "send", "claude", "Rebase", "--key", "k", "--json"),
        ),
        (("say", "claude", "Rebase"), ("mail", "send", "claude", "Rebase")),
        (("gc", "--apply", "--force"), ("reclaim", "--apply", "--force")),
        (("gc",), ("reclaim",)),
    ],
)
def test_an_alias_parses_to_the_canonical_arguments(canonical, alias):
    parser, _ = cli.root_parser(None)
    expected = vars(parser.parse_args(list(canonical)))
    parsed = vars(parser.parse_args(list(alias)))
    for key in ("command", "action"):
        expected.pop(key, None)
        parsed.pop(key, None)
    assert parsed == expected


def test_help_names_the_canonical_command_for_each_alias(monkeypatch, capsys):
    printed = " ".join(help_text(monkeypatch, capsys).split())
    assert "`mail send` is a compatibility alias" in printed
    assert "`reclaim` is a compatibility alias" in printed
    assert "`decision` queries the recorded ones" in printed
    assert "`decide` records a new one" in printed
    assert "`top` draws the live dashboard" in printed
    assert "`status` prints one table" in printed


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
    assert "Credential: work" in printed
    assert "Environment: ANTHROPIC_BASE_URL=<redacted>" in printed
    assert "Required from shell: ANTHROPIC_API_KEY" in printed
    assert "{" not in printed
    listed = envelope(
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
    assert listed["credentials"][0]["env"] == ["ANTHROPIC_BASE_URL=<redacted>"]
    listing = text(
        monkeypatch,
        capsys,
        "--home",
        str(bridge.home),
        "credentials",
        "list",
    )
    assert "example.invalid" not in listing
    assert "ANTHROPIC_BASE_URL=<redacted>" in listing


def test_provider_show_prints_labeled_fields(bridge, monkeypatch, capsys):
    printed = text(
        monkeypatch,
        capsys,
        "--home",
        str(bridge.home),
        "provider",
        "show",
        "deepseek",
    )
    lines = printed.splitlines()
    assert lines[0] == "Provider: deepseek"
    assert "Adapter: claude" in lines
    assert "Environment: none" in lines
    assert (
        "Required from shell: ANTHROPIC_BASE_URL, ANTHROPIC_AUTH_TOKEN" in lines
    )
    assert "{" not in printed
    assert cli.CLIPPED_HINT not in printed


def test_credentials_show_reports_an_empty_profile_in_words(
    bridge, monkeypatch, capsys
):
    roster.define_credential(bridge.home, "bare", "", [], [])
    printed = text(
        monkeypatch,
        capsys,
        "--home",
        str(bridge.home),
        "credentials",
        "show",
        "bare",
    )
    assert printed.splitlines() == [
        "Credential: bare",
        "Config home: none",
        "Environment: none",
        "Required from shell: none",
    ]


def test_detail_clips_long_values_on_a_narrow_terminal(
    bridge, monkeypatch, capsys
):
    home = str(bridge.home / ("deep" * 20))
    roster.define_credential(bridge.home, "long", home, [], [])
    monkeypatch.setattr(cli, "terminal_width", lambda: 30)
    printed = text(
        monkeypatch,
        capsys,
        "--home",
        str(bridge.home),
        "credentials",
        "show",
        "long",
    )
    lines = printed.splitlines()
    assert lines[-1] == cli.CLIPPED_HINT
    assert all(len(line) <= 30 for line in lines[:-1])
    assert lines[1].startswith("Config home: ")
    assert lines[1].endswith("…")
    document = run(
        monkeypatch,
        capsys,
        "--home",
        str(bridge.home),
        "credentials",
        "show",
        "long",
        "--json",
    )
    assert document["config_home"] == home


def test_detail_keeps_every_value_when_piped(bridge, monkeypatch, capsys):
    home = str(bridge.home / ("deep" * 20))
    roster.define_credential(bridge.home, "long", home, [], [])
    printed = text(
        monkeypatch,
        capsys,
        "--home",
        str(bridge.home),
        "credentials",
        "show",
        "long",
    )
    assert f"Config home: {home}" in printed.splitlines()
    assert cli.CLIPPED_HINT not in printed


def decisions(bridge, paired, monkeypatch, capsys, *arguments):
    """Runs ``decision list`` from a registered lane and returns its output."""
    return text(
        monkeypatch,
        capsys,
        "--home",
        str(bridge.home),
        "decision",
        "list",
        *arguments,
        "--repo",
        str(paired["lanes"]["codex"]),
    )


def registered(bridge, paired):
    """Registers both paired lanes with a fresh coordination store."""
    store.initialize(bridge.home)
    for name in ("claude", "codex"):
        store.register(
            bridge.home, paired["root"], paired["participants"][name]["display"]
        )


def test_decision_list_prints_rows_and_keeps_its_document(
    bridge, repo, paired, monkeypatch, capsys
):
    registered(bridge, paired)
    recorded = bridge.decide(repo, "Ship the lane merge\nbehind a flag.")
    printed = decisions(bridge, paired, monkeypatch, capsys)
    lines = printed.splitlines()
    assert lines[0].startswith(f"Decision {recorded['id']}  ")
    assert lines[0].endswith("Operator decision")
    assert lines[1] == "  Ship the lane merge behind a flag."
    assert "{" not in printed
    document = envelope(
        json.loads(decisions(bridge, paired, monkeypatch, capsys, "--json")),
        "decision_list",
    )
    assert [row["id"] for row in document["messages"]] == [recorded["id"]]
    assert document["messages"][0]["body_md"] == (
        "Ship the lane merge\nbehind a flag."
    )
    assert document["has_more"] is False


def test_decision_list_explains_an_empty_log(
    bridge, repo, paired, monkeypatch, capsys
):
    registered(bridge, paired)
    assert (
        decisions(bridge, paired, monkeypatch, capsys)
        == "No decisions recorded.\n"
    )
    assert decisions(
        bridge, paired, monkeypatch, capsys, "flag", "--since", "2h"
    ) == ("No decisions recorded matching 'flag' in the last 2h.\n")


def test_decision_list_clips_long_rows_and_names_more(
    bridge, repo, paired, monkeypatch, capsys
):
    registered(bridge, paired)
    bridge.decide(repo, "first " * 40)
    bridge.decide(repo, "second " * 40)
    monkeypatch.setattr(cli, "terminal_width", lambda: 40)
    lines = decisions(
        bridge, paired, monkeypatch, capsys, "--limit", "1"
    ).splitlines()
    assert lines[1].endswith("…")
    assert len(lines[1]) == 40
    assert lines[2].startswith("More decisions match; raise --limit")
    assert lines[-1] == cli.CLIPPED_HINT


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


@pytest.mark.parametrize(
    "text",
    ["nan", "inf", "-inf", "infm", "nanh", "1e309", "1e308d", "1e307h"],
)
def test_duration_refuses_a_non_finite_window(text):
    with pytest.raises(ValueError, match="is not a window"):
        cli.duration(text)


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("45m", 2700.0), ("6h", 21600.0), ("7d", 604800.0), ("90", 90.0)],
)
def test_duration_reads_a_finite_positive_window(text, seconds):
    assert cli.duration(text) == seconds


@pytest.mark.parametrize("text", ["0", "0m", "-5", "-1h", "", "m", "5x"])
def test_duration_refuses_an_empty_or_malformed_window(text):
    with pytest.raises(ValueError, match="is not a window"):
        cli.duration(text)


@pytest.mark.parametrize("text", ["nan", "inf", "1e309", "1e308d"])
def test_a_duration_option_rejects_a_non_finite_window(
    monkeypatch, capsys, text
):
    monkeypatch.setattr(
        sys, "argv", ["agent-parley", "mail", "send", "--within", text]
    )
    with pytest.raises(SystemExit) as exit_status:
        cli.main()
    assert exit_status.value.code == 2
    assert f"--within: invalid duration value: '{text}'" in (
        capsys.readouterr().err
    )


def test_approval_resume_records_the_service_resume_opt_ins(
    bridge, repo, paired, monkeypatch, capsys
):
    from agent_parley import supervision

    def resume(*flags):
        return text(
            monkeypatch,
            capsys,
            "--home",
            str(bridge.home),
            "approval",
            "resume",
            *flags,
            "--repo",
            str(repo),
        )

    directory = bridge.project(repo)[1]
    assert "claude: approve_bridge_tools off, auto_mode off" in resume()
    shown = resume("--bridge-tools", "on", "--participant", "claude")
    assert shown.strip() == "claude: approve_bridge_tools on, auto_mode off"
    assert roster.read(directory)["participants"]["claude"][
        "approve_bridge_tools"
    ]
    assert not supervision.opt_in_missing(
        bridge.home, roster.read(directory), "claude"
    )
    shown = resume("--auto-mode", "on")
    assert "codex: approve_bridge_tools off, auto_mode on" in shown
    assert roster.read(directory)["supervision"]["auto_mode"] is True
    resume("--bridge-tools", "off", "--participant", "claude")
    assert "claude: approve_bridge_tools off, auto_mode on" in resume()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agent-parley",
            "--home",
            str(bridge.home),
            "approval",
            "resume",
            "--participant",
            "nobody",
            "--repo",
            str(repo),
        ],
    )
    assert cli.main() != 0
    assert "nobody is not a participant" in capsys.readouterr().err
