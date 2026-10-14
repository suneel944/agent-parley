"""Detection of the check command a repository declares at HEAD."""

import pytest

from agent_parley.cli import git
from agent_parley.core import suggested_gate


def commit(repo, files):
    """Writes and commits files in the repository fixture."""
    for name, content in files.items():
        (repo / name).write_text(content)
        git(repo, "add", name)
    git(
        repo,
        "-c",
        "user.name=Bridge Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-m",
        "Add project files",
    )


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        ({"Makefile": "check:\n\ttrue\ntest:\n\ttrue\n"}, ["make", "check"]),
        ({"Makefile": "build:\n\ttrue\ntest: build\n"}, ["make", "test"]),
        (
            {"package.json": '{"scripts": {"test": "jest"}}'},
            ["npm", "test"],
        ),
        (
            {"pyproject.toml": "[tool.pytest.ini_options]\naddopts = '-q'\n"},
            ["pytest", "-q"],
        ),
        ({"Cargo.toml": "[package]\nname = 'x'\n"}, ["cargo", "test"]),
        ({"go.mod": "module example.com/x\n"}, ["go", "test", "./..."]),
        (
            {
                "Makefile": "check:\n\ttrue\n",
                "pyproject.toml": "[tool.pytest.ini_options]\n",
            },
            ["make", "check"],
        ),
    ],
)
def test_each_ecosystem_file_suggests_its_check_command(repo, files, expected):
    commit(repo, files)
    assert suggested_gate(repo) == expected


@pytest.mark.parametrize(
    "files",
    [
        {"Makefile": "build:\n\ttrue\n"},
        {"package.json": '{"scripts": {"build": "tsc"}}'},
        {"package.json": "not json"},
        {"pyproject.toml": "[project]\nname = 'x'\n"},
    ],
)
def test_files_without_a_check_command_suggest_nothing(repo, files):
    commit(repo, files)
    assert suggested_gate(repo) is None


def test_an_uncommitted_file_suggests_nothing(repo):
    (repo / "Makefile").write_text("check:\n\ttrue\n")
    assert suggested_gate(repo) is None
