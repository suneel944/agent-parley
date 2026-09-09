"""Shared isolated state and Git repository fixtures."""

import socket

import pytest

from agent_bridge.cli import Bridge, git
from agent_bridge.state import write_json


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
