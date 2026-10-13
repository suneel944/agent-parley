"""Generated shell completion and its state-backed candidate callback."""

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys

import pytest

from agent_parley import cli, completion
from agent_parley.state import lock, write_json


def parser():
    """Builds a small parser standing in for the program's command tree."""
    root = argparse.ArgumentParser()
    root.add_argument("--home")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("up")
    hidden = commands.add_parser("__complete", help=argparse.SUPPRESS)
    hidden.add_argument("kind")
    issue = commands.add_parser("issue")
    actions = issue.add_subparsers(dest="action", required=True)
    claim = actions.add_parser("claim")
    claim.add_argument("--issue")
    claim.add_argument("--repo")
    say = commands.add_parser("say")
    say.add_argument("--to")
    say.add_argument("--json", action="store_true")
    return root


def project(bridge, root, participants, numbers=()):
    """Writes one registered project manifest and its issue ledger."""
    directory = bridge.home / "projects" / "abcd"
    directory.mkdir(parents=True, exist_ok=True)
    write_json(
        directory / "project.json",
        {
            "root": root,
            "participants": {name: {"display": name} for name in participants},
        },
    )
    write_json(
        directory / "issues.json",
        {
            "revision": 1,
            "issues": {str(number): {"owner": ""} for number in numbers},
        },
    )
    return directory


def test_the_walk_reaches_every_nested_subcommand():
    entries = dict(
        (path, children) for path, children, _ in completion.tree(parser())
    )
    assert entries[""] == ["issue", "say", "up"]
    assert entries["issue"] == ["claim"]
    assert entries["issue claim"] == []


def test_the_walk_omits_the_hidden_callback_command():
    offered = [path for path, _, _ in completion.tree(parser())]
    assert "__complete" not in offered
    assert (
        "__complete"
        not in dict(
            (path, children) for path, children, _ in completion.tree(parser())
        )[""]
    )


def test_the_walk_collects_flags_without_the_help_option():
    flags = dict(
        (path, options) for path, _, options in completion.tree(parser())
    )
    assert flags["say"] == ["--to", "--json"]
    assert "--help" not in flags[""]


def test_state_backed_options_map_to_their_candidate_kind():
    values = completion.value_flags(parser())
    assert values["--to"] == "participants"
    assert values["--issue"] == "issues"
    assert values["--repo"] == "projects"
    assert "--json" not in values


def test_every_shell_emits_a_script_naming_the_hidden_callback():
    for shell in completion.SHELLS:
        text = completion.script(parser(), shell, program="agent-parley")
        assert "agent-parley __complete" in text
        assert text.endswith("\n")


def test_the_bash_script_parses_as_bash():
    text = completion.script(parser(), "bash", program="agent-parley")
    assert text.rstrip().endswith("complete -F _agent_parley agent-parley")
    checked = subprocess.run(
        ["bash", "-n"],
        input=text,
        text=True,
        capture_output=True,
        check=False,
    )
    assert checked.returncode == 0, checked.stderr


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_the_zsh_script_parses_as_zsh():
    text = completion.script(parser(), "zsh", program="agent-parley")
    assert text.startswith("#compdef agent-parley\n")
    checked = subprocess.run(
        ["zsh", "-n"],
        input=text,
        text=True,
        capture_output=True,
        check=False,
    )
    assert checked.returncode == 0, checked.stderr


@pytest.mark.skipif(
    shutil.which("fish") is None, reason="fish is not installed"
)
def test_the_fish_script_parses_as_fish():
    text = completion.script(parser(), "fish", program="agent-parley")
    checked = subprocess.run(
        ["fish", "--no-execute"],
        input=text,
        text=True,
        capture_output=True,
        check=False,
    )
    assert checked.returncode == 0, checked.stderr


def test_an_unknown_shell_is_refused_by_name():
    with pytest.raises(ValueError, match="Unknown shell"):
        completion.script(parser(), "elvish")


def test_candidates_read_participants_and_projects_from_the_manifest(bridge):
    project(bridge, "/tmp/work", ["claude", "codex"])
    assert completion.candidates(bridge.home, "participants") == [
        "claude",
        "codex",
    ]
    assert completion.candidates(bridge.home, "projects") == ["/tmp/work"]


def test_candidates_order_issue_numbers_numerically(bridge):
    project(bridge, "/tmp/work", ["claude"], numbers=[9, 10, 406, 1010])
    assert completion.candidates(bridge.home, "issues") == [
        "9",
        "10",
        "406",
        "1010",
    ]


def test_candidates_skip_a_damaged_manifest_rather_than_failing(bridge):
    directory = project(bridge, "/tmp/work", ["claude"])
    (directory / "project.json").write_text("{not json")
    assert completion.candidates(bridge.home, "participants") == []


def test_candidates_skip_a_damaged_issue_ledger(bridge):
    directory = project(bridge, "/tmp/work", ["claude"], numbers=[7])
    (directory / "issues.json").write_text("{not json")
    assert completion.candidates(bridge.home, "issues") == []


def test_an_unknown_kind_offers_nothing(bridge):
    project(bridge, "/tmp/work", ["claude"])
    assert completion.candidates(bridge.home, "profiles") == []


def test_candidates_answer_while_another_process_holds_the_issue_lock(bridge):
    directory = project(bridge, "/tmp/work", ["claude"], numbers=[42])
    with lock(directory / "issues.lock", timeout=1):
        assert completion.candidates(bridge.home, "issues") == ["42"]
        assert completion.candidates(bridge.home, "participants") == ["claude"]


def test_the_ledger_offers_held_and_unclaimed_issues_alike(bridge):
    directory = project(bridge, "/tmp/work", ["claude"])
    write_json(
        directory / "issues.json",
        {
            "revision": 1,
            "issues": {
                "11": {"owner": "claude"},
                "12": {"owner": ""},
            },
        },
    )
    assert completion.candidates(bridge.home, "issues") == ["11", "12"]


def test_the_command_prints_a_script_and_the_callback_prints_candidates(
    bridge, monkeypatch, capsys
):
    project(bridge, "/tmp/work", ["claude", "codex"])
    monkeypatch.setattr(
        "sys.argv",
        ["agent-parley", "--home", str(bridge.home), "completion", "bash"],
    )
    from agent_parley import cli

    assert cli.main() == 0
    assert "complete -F _agent_parley agent-parley" in capsys.readouterr().out
    monkeypatch.setattr(
        "sys.argv",
        [
            "agent-parley",
            "--home",
            str(bridge.home),
            "__complete",
            "participants",
        ],
    )
    assert cli.main() == 0
    assert capsys.readouterr().out.split() == ["claude", "codex"]


def test_the_generated_script_reflects_a_command_added_to_the_parser():
    root = parser()
    commands = [
        action
        for action in root._actions
        if isinstance(action, argparse._SubParsersAction)
    ][0]
    commands.add_parser("invented")
    assert "invented" in completion.script(root, "bash")
    assert "invented" in completion.script(root, "fish")


def test_a_manifest_without_a_root_contributes_no_project(bridge):
    directory = bridge.home / "projects" / "abcd"
    directory.mkdir(parents=True)
    write_json(directory / "project.json", {"participants": {}})
    assert completion.candidates(bridge.home, "projects") == []


def test_candidates_merge_across_several_registered_projects(bridge):
    for index, (root, name) in enumerate(
        (("/tmp/one", "claude"), ("/tmp/two", "codex"))
    ):
        directory = bridge.home / "projects" / f"p{index}"
        directory.mkdir(parents=True)
        write_json(
            directory / "project.json",
            {"root": root, "participants": {name: {"display": name}}},
        )
    assert completion.candidates(bridge.home, "participants") == [
        "claude",
        "codex",
    ]
    assert completion.candidates(bridge.home, "projects") == [
        "/tmp/one",
        "/tmp/two",
    ]


def test_providers_and_credentials_come_from_the_registry(bridge):
    write_json(
        bridge.home / "credentials.json",
        {
            "version": 1,
            "entries": {
                "work": {"config_home": "", "env": [], "require_env": []}
            },
        },
    )
    assert "work" in completion.candidates(bridge.home, "credentials")
    assert "claude" in completion.candidates(bridge.home, "providers")


def test_the_manifest_reader_tolerates_an_absent_projects_directory(bridge):
    assert completion.candidates(bridge.home, "participants") == []
    assert json.dumps(completion.candidates(bridge.home, "issues")) == "[]"


def test_positionals_name_what_each_command_slot_accepts():
    slots = completion.positionals(cli.root_parser(None)[0])
    assert slots["say#0"] == "@participants"
    assert "say#1" not in slots
    assert slots["completion#0"] == "bash zsh fish"
    assert slots["participant restart#0"] == "@participants"
    assert slots["provider show#0"] == "@providers"
    assert slots["credentials show#0"] == "@credentials"
    assert "participant add#0" not in slots


def test_the_credentials_option_completes_profiles():
    values = completion.value_flags(cli.root_parser(None)[0])
    assert values["--credentials"] == "credentials"


def test_value_options_include_home_but_not_switches():
    taking = completion.value_options(cli.root_parser(None)[0])
    assert "--home" in taking
    assert "--credentials" in taking
    assert "--json" not in taking


def offered(shell, script, line, environment):
    """Runs one shell's generated completion for a typed command line."""
    words = ["agent-parley", *line.split(), ""]
    quoted = " ".join(shlex.quote(word) for word in words)
    if shell == "bash":
        command = [
            "bash",
            "-c",
            f"source {script}; COMP_WORDS=({quoted}); "
            f"COMP_CWORD={len(words) - 1}; _agent_parley; "
            'printf "%s\\n" "${COMPREPLY[@]}"',
        ]
    elif shell == "zsh":
        command = [
            "zsh",
            "-f",
            "-c",
            "compdef() { :; }; _files() { :; }; "
            'compadd() { [[ $1 == -- ]] && shift; print -l -- "$@"; }; '
            f"source {script}; words=({quoted}); CURRENT={len(words)}; "
            "_agent_parley",
        ]
    else:
        typed = shlex.quote(f"agent-parley {line} ")
        command = [
            "fish",
            "--no-config",
            "-c",
            f"source {script}; complete -C {typed}",
        ]
    finished = subprocess.run(
        command,
        env=environment,
        text=True,
        capture_output=True,
        check=True,
        timeout=60,
    )
    return {row.split("\t")[0] for row in finished.stdout.splitlines()}


@pytest.mark.parametrize("shell", completion.SHELLS)
@pytest.mark.parametrize(
    ("line", "expected"),
    (
        ("say", {"claude-2"}),
        ("participant restart", {"claude-2"}),
        ("completion", {"bash", "zsh", "fish"}),
        ("--home elsewhere participant", {"restart", "list", "show"}),
        ("run claude --credentials", {"work"}),
    ),
)
def test_a_live_shell_completes_positionals_and_values(
    bridge, tmp_path, monkeypatch, shell, line, expected
):
    if shutil.which(shell) is None:
        pytest.skip(f"{shell} is not installed")
    project(bridge, "/tmp/one", ["claude-2"])
    write_json(
        bridge.home / "credentials.json",
        {"version": 1, "entries": {"work": {"config_home": ""}}},
    )
    tools = tmp_path / "bin"
    tools.mkdir()
    stub = tools / "agent-parley"
    stub.write_text(
        f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -m agent_parley "$@"\n'
    )
    stub.chmod(0o755)
    script = tmp_path / f"completion.{shell}"
    script.write_text(completion.script(cli.root_parser(None)[0], shell))
    environment = {
        **os.environ,
        "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}",
        "AGENT_PARLEY_HOME": str(bridge.home),
    }
    assert expected <= offered(shell, script, line, environment)
