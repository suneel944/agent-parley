"""Shared isolated state and Git repository fixtures."""

import socket
import subprocess
import sys

import pytest

from agent_parley import forge, records
from agent_parley.cli import Bridge, git
from agent_parley.state import write_json


@pytest.fixture
def worker():
    """Runs a child process standing in for a lane's background job."""
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)"]
    )
    yield child.pid
    child.kill()
    child.wait()


GIT_SETTINGS = {
    "user.name": "Bridge Test",
    "user.email": "test@example.com",
    "commit.gpgsign": "false",
    "tag.gpgsign": "false",
    "init.defaultBranch": "main",
    "maintenance.auto": "false",
    "gc.auto": "0",
}


@pytest.fixture(scope="session")
def git_config(tmp_path_factory):
    """Writes the only global Git configuration the suite reads.

    Each setting goes in the global file rather than in ``GIT_CONFIG_COUNT``
    so a repository's own configuration and ``git -c`` still override it,
    as they would on an operator's machine.
    """
    path = tmp_path_factory.mktemp("git") / "gitconfig"
    for key, value in GIT_SETTINGS.items():
        git(path.parent, "config", "--file", str(path), key, value)
    return path


@pytest.fixture(autouse=True)
def native_config_homes(tmp_path, monkeypatch, git_config):
    """Keeps every state read inside each test's own directory.

    The command line falls back to ``AGENT_PARLEY_HOME`` when a test drives
    ``cli.main()`` without ``--home``, so that variable is pinned as well;
    otherwise such a test reads the developer's live store and fails
    whenever that store is behind the checked-out code. A lane's own
    ``AGENT_PARLEY_TOKEN`` is removed, so a suite run from inside a lane
    still drives operator-only commands as the operator. ``FORCE_COLOR`` is
    removed because it makes help text carry colour codes, so a developer
    shell that exports it would fail every test that reads plain help.

    Git reads only ``git_config`` and no system file, so a developer's
    signing, hooks or default branch never reach the suite, and Git starts
    no detached maintenance inside a worktree a test is about to move.

    A default-account lane's client records are read from the client's own
    default home, so that home is pinned to the same directories.

    Each test starts with no recorded forge failure, so a refusal one test
    records never puts a later test's poll into the forge backoff.
    """
    for variable in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "AGENT_PARLEY_HOME"):
        monkeypatch.setenv(variable, str(tmp_path / variable.lower()))
    for adapter, variable in (
        ("claude", "CLAUDE_CONFIG_DIR"),
        ("codex", "CODEX_HOME"),
        ("copilot", "COPILOT_HOME"),
        ("gemini", "GEMINI_CLI_HOME"),
        ("opencode", "OPENCODE_CONFIG_DIR"),
    ):
        monkeypatch.setitem(
            records.DEFAULT_HOMES, adapter, str(tmp_path / variable.lower())
        )
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.delenv("OPENCODE_DB", raising=False)
    monkeypatch.delenv("AGENT_PARLEY_TOKEN", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(git_config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    for variable in ("GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setattr(forge, "_failure", {})


@pytest.fixture
def bridge(tmp_path):
    """Allocates private coordination state and tears down its own service."""
    instance = Bridge(tmp_path / "state")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    instance.config["port"] = port
    instance.url = f"http://127.0.0.1:{port}"
    write_json(instance.home / "config.json", instance.config)
    yield instance
    instance.down()


@pytest.fixture
def paired(bridge, repo):
    """Registers the two-participant roster most coordination tests assume."""
    bridge.add_participant(repo, "claude", "claude")
    return bridge.add_participant(repo, "codex", "codex")


@pytest.fixture
def repo(tmp_path):
    """Creates a committed repository whose path exercises spaces."""
    path = tmp_path / "project with spaces"
    path.mkdir()
    git(path, "init")
    (path / "shared.txt").write_text("original\n")
    git(path, "add", "shared.txt")
    git(
        path,
        "-c",
        "user.name=Bridge Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-m",
        "Initial fixture",
    )
    return path
