"""Checks the version, show and list verbs the command surface offers."""

import argparse
import json
import sys
from pathlib import Path

import pytest

from agent_parley import BridgeError, cli, protocol, roster, start, store, views
from agent_parley.cli import git

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


def test_every_argument_carries_help():
    parser, _ = cli.root_parser(None)
    pending = [((), parser)]
    bare = []
    while pending:
        path, current = pending.pop()
        for action in current._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, child in action.choices.items():
                    pending.append(((*path, name), child))
            elif not action.help:
                named = action.option_strings or [action.dest]
                bare.append(" ".join((*path, named[0])))
    assert bare == []


def test_demo_help_describes_the_demo(monkeypatch, capsys):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(sys, "argv", ["agent-parley", "demo", "--help"])
    with pytest.raises(SystemExit):
        cli.main()
    described = " ".join(capsys.readouterr().out.split())
    assert "stub lanes in a throwaway sandbox" in described
    assert "press q or Ctrl-C to stop early" in described


def test_every_surface_names_a_stopped_service_the_same_way(
    bridge, repo, paired, monkeypatch, capsys
):
    store.initialize(bridge.home)
    lane = paired["lanes"]["claude"]
    monkeypatch.chdir(lane)
    printed = []
    for arguments in (
        ("status",),
        ("top", "--once"),
        ("problems",),
        ("doctor",),
    ):
        monkeypatch.setattr(
            sys,
            "argv",
            ["agent-parley", "--home", str(bridge.home), *arguments],
        )
        cli.main()
        printed.append(capsys.readouterr().out.lower())
    found = start.look(bridge.home, Path(lane))
    printed.append(start.render(found).lower())
    for output in printed:
        assert "service" in output
        assert protocol.STOPPED in output


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
    (bridge.home / roster.CREDENTIALS).write_text(
        json.dumps(
            {
                "version": 1,
                "entries": {"bare": {"home": "", "env": [], "require_env": []}},
            }
        )
    )
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
        "Account: selects no account",
    ]


def test_credentials_add_without_a_selector_exits_2_and_writes_nothing(
    bridge, monkeypatch, capsys
):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agent-parley",
            "--home",
            str(bridge.home),
            "credentials",
            "add",
            "work",
        ],
    )
    with pytest.raises(SystemExit) as exited:
        cli.main()
    assert exited.value.code == 2
    assert roster.EMPTY_CREDENTIAL in capsys.readouterr().err
    assert not (bridge.home / roster.CREDENTIALS).exists()


def test_unknown_credentials_show_ends_on_an_add_with_a_selector(
    bridge, monkeypatch, capsys
):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agent-parley",
            "--home",
            str(bridge.home),
            "credentials",
            "show",
            "work",
        ],
    )
    assert cli.main() == 1
    assert capsys.readouterr().err.splitlines()[-1] == (
        "next: agent-parley credentials add work --config-home ~/.claude-work"
    )


def test_a_lane_name_that_names_no_provider_points_at_provider_flag(
    bridge, repo
):
    with pytest.raises(BridgeError) as raised:
        bridge.add_participant(repo, "ada")
    assert "a new lane name needs --provider" in str(raised.value)
    assert raised.value.next_command == (
        "agent-parley run ada --provider claude"
    )
    bridge.add_participant(repo, "ada", "claude")
    assert "ada" in roster.read(bridge.project(repo)[1])["participants"]


def test_mail_list_from_the_main_checkout_names_as(
    bridge, repo, paired, monkeypatch, capsys
):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agent-parley",
            "--home",
            str(bridge.home),
            "mail",
            "list",
            "--repo",
            str(repo),
        ],
    )
    assert cli.main() == 1
    lines = capsys.readouterr().err.splitlines()
    assert "--as NAME" in lines[0]
    assert lines[-1] == "next: agent-parley mail list --as claude"


@pytest.mark.parametrize(
    "command", [("say", "nobody", "hi"), ("watch", "nobody")]
)
def test_say_and_watch_to_an_unknown_lane_end_on_the_participant_list(
    bridge, repo, paired, monkeypatch, capsys, command
):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agent-parley",
            "--home",
            str(bridge.home),
            *command,
            "--repo",
            str(repo),
        ],
    )
    assert cli.main() == 1
    lines = capsys.readouterr().err.splitlines()
    assert "nobody is not a participant" in lines[0]
    assert lines[-1] == "next: agent-parley participant list"


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


@pytest.mark.filterwarnings("ignore:subprocess \\d+ is still running")
def test_up_and_down_report_the_service_as_json(bridge, monkeypatch, capsys):
    """Starts and stops the service in this process.

    `up` leaves the service it started running on purpose and drops its
    process handle, so the handle warns when collected here; a launcher
    that exits never sees it.
    """
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


def test_setup_confirms_in_plain_text_and_json_carries_the_envelope(
    bridge, repo, monkeypatch, capsys
):
    printed = text(
        monkeypatch, capsys, "--home", str(bridge.home), "setup", str(repo)
    )
    assert not printed.startswith("{")
    lines = printed.splitlines()
    assert lines[0] == f"Root: {repo.resolve()}"
    assert [line.split(":")[0] for line in lines] == [
        "Root",
        "Base",
        "Forge",
        "Verify",
        "Init",
        "Next",
    ]
    assert lines[3] == "Verify: none; participant merge runs no gate"
    assert lines[4] == "Init: none"
    assert lines[-1] == "Next: agent-parley run claude"
    document = envelope(
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
    assert document["root"] == str(repo.resolve())
    assert lines[1] == f"Base: {document['base'][:12]}"


def test_setup_suggests_the_gate_its_committed_makefile_declares(
    bridge, repo, monkeypatch, capsys
):
    (repo / "Makefile").write_text("lint:\n\ttrue\ncheck: lint\n\ttrue\n")
    git(repo, "add", "Makefile")
    git(
        repo,
        "-c",
        "user.name=Bridge Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-m",
        "Add a check target",
    )
    printed = text(
        monkeypatch, capsys, "--home", str(bridge.home), "setup", str(repo)
    )
    assert "Verify: none; participant merge runs no gate" in printed
    assert 'Suggested: agent-parley verify set "make check"' in printed
    assert printed.splitlines()[-1].startswith("Next: ")
    assert roster.read(bridge.project(repo)[1])["verify"] == []


def test_setup_records_the_gate_and_init_under_the_operator_guard(
    bridge, repo, monkeypatch, capsys
):
    monkeypatch.chdir(repo)
    printed = text(
        monkeypatch,
        capsys,
        "--home",
        str(bridge.home),
        "setup",
        str(repo),
        "--verify",
        "make check",
        "--init",
        "uv sync",
    )
    assert "Verify: make check" in printed
    assert "Init: uv sync" in printed
    assert "Suggested:" not in printed
    stored = roster.read(bridge.project(repo)[1])
    assert stored["verify"] == ["make", "check"]
    assert stored["initialize"] == ["uv", "sync"]
    document = run(
        monkeypatch,
        capsys,
        "--home",
        str(bridge.home),
        "setup",
        str(repo),
        "--json",
    )
    assert document["verify"] == ["make", "check"]
    monkeypatch.setenv("AGENT_PARLEY_TOKEN", "lane-token")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agent-parley",
            "--home",
            str(bridge.home),
            "setup",
            str(repo),
            "--verify",
            "true",
        ],
    )
    assert cli.main() != 0
    assert "AGENT_PARLEY_TOKEN" in capsys.readouterr().err
    assert roster.read(bridge.project(repo)[1])["verify"] == ["make", "check"]


def test_credentials_add_names_its_settings_without_values(
    bridge, tmp_path, monkeypatch, capsys
):
    config = tmp_path / "claude-work"
    printed = text(
        monkeypatch,
        capsys,
        "--home",
        str(bridge.home),
        "credentials",
        "add",
        "work",
        "--config-home",
        str(config),
        "--env",
        "WORK_REGION=eu-west",
        "--require-env",
        "WORK_TOKEN",
    )
    assert printed == (
        f"Defined credential profile work (config home {config}; "
        "sets WORK_REGION; requires WORK_TOKEN).\n"
        "Next: agent-parley run claude --credentials work\n"
    )
    assert "eu-west" not in printed
    document = envelope(
        run(
            monkeypatch,
            capsys,
            "--home",
            str(bridge.home),
            "credentials",
            "add",
            "home",
            "--require-env",
            "HOME_TOKEN",
            "--json",
        ),
        "credentials",
    )
    assert [entry["name"] for entry in document["credentials"]] == [
        "home",
        "work",
    ]


def test_provider_add_confirms_in_plain_text_and_json_carries_the_envelope(
    bridge, monkeypatch, capsys
):
    printed = text(
        monkeypatch,
        capsys,
        "--home",
        str(bridge.home),
        "provider",
        "add",
        "claude-work",
        "--adapter",
        "claude",
        "--executable",
        "claude",
    )
    assert printed == (
        "Defined provider claude-work (claude adapter; runs claude).\n"
        "Next: agent-parley provider show claude-work\n"
    )
    document = envelope(
        run(
            monkeypatch,
            capsys,
            "--home",
            str(bridge.home),
            "provider",
            "add",
            "codex-work",
            "--adapter",
            "codex",
            "--executable",
            "codex",
            "--json",
        ),
        "providers",
    )
    names = [entry["name"] for entry in document["providers"]]
    assert "claude-work" in names and "codex-work" in names


RESOLVED = {
    "issue": 42,
    "outcome": "complete",
    "holder": "claude",
    "owner": None,
    "state": "complete",
    "evidence": {
        "branch": "parley/claude",
        "state": "MERGED",
        "observed_at": 1760000000.0,
    },
}


def test_issue_resolve_says_what_it_ended_and_json_carries_the_envelope(
    bridge, repo, monkeypatch, capsys
):
    monkeypatch.setattr(
        cli.Bridge, "issue_resolve", lambda *args, **kwargs: dict(RESOLVED)
    )
    arguments = (
        "--home",
        str(bridge.home),
        "issue",
        "resolve",
        "42",
        "--repo",
        str(repo),
    )
    printed = text(monkeypatch, capsys, *arguments)
    assert printed == (
        "Resolved issue #42: recorded complete, ending claude's claim on "
        "parley/claude (merged).\n"
        "Next: agent-parley issue show 42\n"
    )
    document = envelope(
        run(monkeypatch, capsys, *arguments, "--json"), "issue_resolve"
    )
    assert document["outcome"] == "complete"
    assert document["evidence"]["observed_at"] == "2025-10-09T08:53:20Z"


def test_issue_resolve_release_says_the_work_went_back_to_the_queue(
    bridge, repo, monkeypatch, capsys
):
    monkeypatch.setattr(
        cli.Bridge,
        "issue_resolve",
        lambda *args, **kwargs: {**RESOLVED, "outcome": "release"},
    )
    printed = text(
        monkeypatch,
        capsys,
        "--home",
        str(bridge.home),
        "issue",
        "resolve",
        "42",
        "--release",
        "--repo",
        str(repo),
    )
    assert printed.startswith("Resolved issue #42: returned to the queue, ")


def test_issue_recover_says_what_it_approved_and_json_carries_the_envelope(
    bridge, repo, monkeypatch, capsys
):
    approval = {
        "id": "authorization:c1:s1",
        "issue": "42",
        "claim_id": "c1",
        "owner": "claude",
        "session_id": "s1",
        "reason": "stuck on capacity",
        "authorized_by": "operator",
        "authorized_at": 1760000000.0,
    }
    monkeypatch.setattr(
        cli.Bridge, "authorize_recovery", lambda *args: dict(approval)
    )
    arguments = (
        "--home",
        str(bridge.home),
        "issue",
        "recover",
        "42",
        "--reason",
        "stuck on capacity",
        "--repo",
        str(repo),
    )
    printed = text(monkeypatch, capsys, *arguments)
    assert printed == (
        "Recovery approved for issue #42: claude's claim stops once a "
        "matching capacity observation is published; nothing has moved "
        "yet.\n"
    )
    document = envelope(
        run(monkeypatch, capsys, *arguments, "--json"), "issue_recover"
    )
    assert document["claim_id"] == "c1"
    assert document["authorized_at"] == "2025-10-09T08:53:20Z"
