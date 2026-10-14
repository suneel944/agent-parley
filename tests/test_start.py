"""Checks the start-here screen a bare invocation prints."""

import json
import os
import subprocess
import sys

import pytest

from agent_parley import cli, problems, process, start, store

FAKE = "#!/bin/sh\nexit 0\n"
PLUGIN = "agent-parley@agent-parley"


@pytest.fixture
def path(tmp_path, monkeypatch):
    """Puts a directory of fake native CLIs first on a minimal PATH."""
    binary = tmp_path / "bin"
    binary.mkdir()
    monkeypatch.setenv("PATH", f"{binary}:/usr/bin:/bin")

    def add(name):
        executable = binary / name
        executable.write_text(FAKE)
        executable.chmod(0o755)

    return add


def claude_plugin():
    """Records the plugin in the fake Claude Code configuration."""
    directory = os.environ["CLAUDE_CONFIG_DIR"] + "/plugins"
    os.makedirs(directory)
    with open(f"{directory}/installed_plugins.json", "w") as file:
        json.dump({"version": 2, "plugins": {PLUGIN: []}}, file)


def codex_plugin(enabled="true"):
    """Records the plugin in the fake Codex configuration."""
    os.makedirs(os.environ["CODEX_HOME"])
    with open(os.environ["CODEX_HOME"] + "/config.toml", "w") as file:
        file.write(f'[plugins."{PLUGIN}"]\nenabled = {enabled}\n')


def screen(capsys, home, directory):
    assert start.show(home, directory) == 0
    printed = capsys.readouterr().out
    lines = printed.splitlines()
    assert len(lines) <= 20
    assert all(len(line) <= 80 for line in lines)
    assert "\x1b" not in printed
    assert lines[-1] == "Full reference: agent-parley --help"
    return printed


def test_outside_a_repository_says_so_and_writes_nothing(
    path, tmp_path, capsys
):
    path("claude")
    claude_plugin()
    home = tmp_path / "never-created"
    outside = tmp_path / "plain"
    outside.mkdir()
    printed = screen(capsys, home, outside)
    assert "Here      not a Git repository" in printed
    assert "Project   not registered" in printed
    assert "CLIs      claude (plugin added)" in printed
    assert "Service   not running" in printed
    assert "cd REPOSITORY" in printed
    assert "agent-parley run claude" in printed
    assert "plugins install" not in printed
    assert not home.exists()


def test_a_plain_directory_is_not_a_checkout_whatever_git_answers(
    path, tmp_path
):
    path("git")
    outside = tmp_path / "plain"
    outside.mkdir()
    assert start.checkout(outside) == (False, False, False)


def test_repository_without_commits_leads_with_the_first_commit(
    path, tmp_path, capsys
):
    path("claude")
    claude_plugin()
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    subprocess.run(["git", "init", "-q", str(fresh)], check=True)
    (fresh / "notes.txt").write_text("draft\n")
    printed = screen(capsys, tmp_path / "home", fresh)
    assert "Git repository, no commits yet" in printed
    assert "clean" not in printed
    assert "uncommitted changes" not in printed
    assert printed.index("git commit") < printed.index(
        "agent-parley run claude"
    )
    assert "Make the first commit" in printed


def test_dirty_repository_points_at_its_uncommitted_work(
    path, repo, tmp_path, capsys
):
    path("claude")
    claude_plugin()
    (repo / "shared.txt").write_text("changed\n")
    printed = screen(capsys, tmp_path / "home", repo)
    assert "Git repository, uncommitted changes" in printed
    assert "git status" in printed
    (repo / "shared.txt").write_text("original\n")
    printed = screen(capsys, tmp_path / "home", repo)
    assert "Git repository, clean" in printed
    assert "git status" not in printed


def test_registration_stash_is_listed_with_its_restore_command(
    path, bridge, repo, capsys
):
    path("claude")
    claude_plugin()
    (repo / "shared.txt").write_text("changed\n")
    bridge.setup(repo)
    assert bridge.preserved
    entry = subprocess.run(
        ["git", "rev-parse", "refs/stash"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    printed = screen(capsys, bridge.home, repo)
    assert f"  Stashed   git stash apply {entry}" in printed
    subprocess.run(["git", "stash", "drop", "-q"], cwd=repo, check=True)
    assert "Stashed" not in screen(capsys, bridge.home, repo)


def test_a_stash_of_other_work_is_not_listed(path, bridge, repo, capsys):
    path("claude")
    claude_plugin()
    bridge.setup(repo)
    assert not bridge.preserved
    (repo / "shared.txt").write_text("changed\n")
    subprocess.run(["git", "stash", "-q"], cwd=repo, check=True)
    assert "Stashed" not in screen(capsys, bridge.home, repo)


@pytest.mark.parametrize("attached", [True, False])
def test_run_repeats_the_stash_notice_after_an_attached_client_exits(
    bridge, repo, monkeypatch, capsys, attached
):
    def launch(self, *args, **kwargs):
        self.preserved = "Preserved your pending changes as stash entry abc"
        print("client ran", file=sys.stderr)
        return 0

    monkeypatch.setattr(cli.Bridge, "launch", launch)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: attached)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agent-parley",
            "--home",
            str(bridge.home),
            "run",
            "claude",
            "--repo",
            str(repo),
        ],
    )
    assert cli.main() == 0
    error = capsys.readouterr().err
    repeated = error.split("client ran", 1)[1]
    assert ("stash entry abc" in repeated) is attached


def test_no_native_cli_offers_the_demo(path, repo, tmp_path, capsys):
    printed = screen(capsys, tmp_path / "home", repo)
    assert (
        "CLIs      none on PATH (claude, codex, copilot, gemini, opencode, amp)"
    ) in printed
    assert "agent-parley demo" in printed
    assert "agent-parley run" not in printed


@pytest.mark.parametrize("name", ["gemini", "copilot", "opencode", "amp"])
def test_a_provider_without_a_plugin_is_offered_to_run(
    name, path, repo, tmp_path, capsys
):
    path(name)
    printed = screen(capsys, tmp_path / "home", repo)
    assert f"CLIs      {name} (no plugin needed)" in printed
    assert printed.split("Next:\n")[1].startswith(f"  agent-parley run {name}")
    assert "agent-parley demo" not in printed
    assert "plugins install" not in printed


def test_every_provider_on_path_wraps_within_the_screen(
    path, repo, tmp_path, capsys
):
    for name in ("amp", "opencode", "gemini", "copilot", "codex", "claude"):
        path(name)
    claude_plugin()
    printed = screen(capsys, tmp_path / "home", repo)
    listed = printed.split("CLIs      ")[1].split("  Service")[0]
    assert " ".join(listed.split()) == (
        "claude (plugin added), codex (plugin missing), copilot (no plugin "
        "needed), gemini (no plugin needed), opencode (no plugin needed), "
        "amp (no plugin needed)"
    )
    assert "agent-parley run claude" in printed
    assert "Add the plugin to codex." in printed


def test_missing_plugin_offers_the_install(path, repo, tmp_path, capsys):
    path("claude")
    path("codex")
    claude_plugin()
    codex_plugin(enabled="false")
    printed = screen(capsys, tmp_path / "home", repo)
    assert "claude (plugin added), codex (plugin missing)" in printed
    assert "agent-parley plugins install  Add the plugin to codex." in printed
    assert "agent-parley run claude" in printed


def running(bridge, monkeypatch):
    """Makes the bridge's service read as running and ready."""
    store.initialize(bridge.home)
    (bridge.home / "server.json").write_text('{"pid": 1, "start_ticks": "1"}')
    monkeypatch.setattr(
        process, "identify", lambda record, home: process.ServerProcess(1, "1")
    )
    monkeypatch.setattr(cli.Bridge, "health", lambda self: {"status": "ready"})


def test_running_service_and_registered_project_offer_the_dashboard(
    path, bridge, repo, paired, monkeypatch, capsys
):
    path("codex")
    codex_plugin()
    running(bridge, monkeypatch)
    monkeypatch.setenv("AGENT_PARLEY_NOTIFY", "email")
    monkeypatch.setenv("AGENT_PARLEY_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("AGENT_PARLEY_SMTP_FROM", "bridge@example.com")
    monkeypatch.setenv("AGENT_PARLEY_SMTP_TO", "owner@example.com")
    printed = screen(capsys, bridge.home, repo)
    (bridge.home / "server.json").unlink()
    assert "Project   registered" in printed
    assert "Service   running" in printed
    assert "agent-parley run codex  Start a lane." in printed
    assert "agent-parley top" in printed
    assert "Needs you" not in printed
    assert "Notify" not in printed
    assert "agent-parley problems" not in printed


def test_problems_lead_the_screen_and_its_next_commands(
    path, bridge, repo, paired, monkeypatch, capsys
):
    path("codex")
    codex_plugin()
    running(bridge, monkeypatch)
    monkeypatch.setenv("AGENT_PARLEY_NOTIFY", "email")
    rows = [
        {"participant": "claude", "condition": "overdue claim"},
        {"participant": "", "condition": "pull request checks failed"},
        {"participant": "codex", "condition": "stalled"},
    ]
    monkeypatch.setattr(problems, "derive", lambda home, report: rows)
    printed = screen(capsys, bridge.home, repo)
    (bridge.home / "server.json").unlink()
    assert (
        "  Needs you 3 problems: claude overdue claim, "
        "pull request checks failed\n"
    ) in printed
    assert printed.split("Next:\n")[1].startswith("  agent-parley problems")


def test_notify_off_with_lanes_offers_its_setup(
    path, bridge, repo, paired, monkeypatch, capsys
):
    path("codex")
    codex_plugin()
    running(bridge, monkeypatch)
    monkeypatch.delenv("AGENT_PARLEY_NOTIFY", raising=False)
    printed = screen(capsys, bridge.home, repo)
    (bridge.home / "server.json").unlink()
    assert "  Notify    off\n" in printed
    assert "agent-parley notify setup  Get told when a lane needs you." in (
        printed
    )
    assert "Needs you" not in printed


def test_notify_missing_email_settings_keeps_its_setup_hint(
    path, bridge, repo, paired, monkeypatch, capsys
):
    path("codex")
    codex_plugin()
    running(bridge, monkeypatch)
    monkeypatch.setenv("AGENT_PARLEY_NOTIFY", "email")
    for name in (
        "AGENT_PARLEY_SMTP_HOST",
        "AGENT_PARLEY_SMTP_FROM",
        "AGENT_PARLEY_SMTP_TO",
    ):
        monkeypatch.delenv(name, raising=False)
    printed = screen(capsys, bridge.home, repo)
    (bridge.home / "server.json").unlink()
    assert "  Notify    off\n" in printed
    assert "agent-parley notify setup  Get told when a lane needs you." in (
        printed
    )


def snapshot(root):
    """Maps every path under a directory to its size and modification time."""
    return {
        str(entry): (entry.stat().st_size, entry.stat().st_mtime_ns)
        for entry in sorted(root.rglob("*"))
    }


def test_existing_state_and_checkout_are_left_untouched(
    path, bridge, repo, paired, monkeypatch, capsys
):
    path("claude")
    (repo / "shared.txt").write_text("changed\n")
    running(bridge, monkeypatch)
    state = snapshot(bridge.home)
    checkout = snapshot(repo)
    screen(capsys, bridge.home, repo)
    after = snapshot(bridge.home), snapshot(repo)
    (bridge.home / "server.json").unlink()
    assert after == (state, checkout)


def test_bare_entry_does_not_load_the_command_surface(
    path, tmp_path, monkeypatch
):
    monkeypatch.setenv("AGENT_PARLEY_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("NO_COLOR", "1")
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, types\n"
            "from agent_parley import entry\n"
            "sys.argv = ['agent-parley']\n"
            "status = entry.main()\n"
            "cli = sys.modules.get('agent_parley.cli')\n"
            "print(status, type(cli) is types.ModuleType)\n",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "Found:" in probe.stdout
    assert probe.stdout.splitlines()[-1] == "0 False"
    assert not (tmp_path / "home").exists()


def test_bare_cli_prints_the_screen(path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["agent-parley"])
    assert cli.main() == 0
    assert "Full reference: agent-parley --help" in capsys.readouterr().out


def test_help_leads_with_start_here_and_skips_undeclared(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["agent-parley", "--help"])
    with pytest.raises(SystemExit):
        cli.main()
    printed = capsys.readouterr().out
    leading = printed.split("Start here:\n", 1)[1].split("\n\n", 1)[0]
    names = [line.split()[0] for line in leading.splitlines() if line[2] != " "]
    declared = [name for name in cli.START_HERE[1] if f"\n  {name} " in printed]
    assert names == declared
    assert printed.index("Start here:") < printed.index("Coordination:")
    for name in names:
        assert printed.count(f"\n  {name} ") == 2
