"""Checks the shared refusal shape and every command a refusal names."""

import ast
import json
import shlex
import sys
from pathlib import Path

import pytest

import agent_parley
from agent_parley import BridgeError, cli, refusal, roster, store, worktrees

PROGRAMS = {"agent-parley", "chmod", "git", "kill", "tail"}
PLACEHOLDER = "1"


def rendered(node: ast.expr) -> list[str]:
    """Renders every text a ``next_command`` expression can produce."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.JoinedStr):
        texts = [""]
        for part in node.values:
            pieces = (
                rendered(part)
                if isinstance(part, ast.Constant)
                else [PLACEHOLDER]
            )
            texts = [text + piece for text in texts for piece in pieces]
        return texts
    if isinstance(node, ast.IfExp):
        return rendered(node.body) + rendered(node.orelse)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return [
            left + right
            for left in rendered(node.left)
            for right in rendered(node.right)
        ]
    if isinstance(node, ast.Call):
        return [PLACEHOLDER]
    raise AssertionError(f"unrendered next_command: {ast.dump(node)}")


def suggested() -> list[tuple[str, str]]:
    """Collects every literal command a refusal names in the package."""
    found = []
    package = Path(agent_parley.__file__).parent
    for path in sorted(package.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.keyword):
                continue
            if node.arg != "next_command" or isinstance(node.value, ast.Call):
                continue
            where = f"{path.name}:{node.value.lineno}"
            found.extend((where, text) for text in rendered(node.value))
    return found


SUGGESTED = suggested()


def test_refusal_ends_on_the_next_command():
    failure = BridgeError("The store is gone.", next_command="agent-parley up")
    assert str(failure) == "The store is gone."
    assert refusal(failure) == "The store is gone.\nnext: agent-parley up"
    assert refusal(BridgeError("No command fits.")) == "No command fits."
    assert refusal(ValueError("bad window")) == "bad window"


def test_every_refusal_site_is_found():
    assert len(SUGGESTED) >= 20


@pytest.mark.parametrize(("where", "command"), SUGGESTED)
def test_every_suggested_command_is_runnable(where, command):
    for segment in command.split("&&"):
        argv = shlex.split(segment)
        assert argv[0] in PROGRAMS, where
        if argv[0] != "agent-parley":
            continue
        parser, _ = cli.root_parser(None)
        try:
            parser.parse_args(argv[1:])
        except SystemExit as exc:
            pytest.fail(f"{where}: {command!r} does not parse ({exc.code})")


def fail_up(monkeypatch, tmp_path, exc, *flags):
    """Makes ``up`` raise one failure and returns the CLI exit status."""

    def up(self):
        raise exc

    monkeypatch.setattr(cli.Bridge, "up", up)
    monkeypatch.setattr(
        sys,
        "argv",
        ["agent-parley", "--home", str(tmp_path), "up", *flags],
    )
    return cli.main()


def test_human_refusal_prints_the_next_line(monkeypatch, capsys, tmp_path):
    failure = BridgeError("Server is down.", next_command="agent-parley up")
    assert fail_up(monkeypatch, tmp_path, failure) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "agent-parley: Server is down.\nnext: agent-parley up\n"
    )


def test_json_error_document_is_unchanged(monkeypatch, capsys, tmp_path):
    failure = BridgeError("Server is down.", next_command="agent-parley up")
    assert fail_up(monkeypatch, tmp_path, failure, "--json") == 1
    document = json.loads(capsys.readouterr().out)
    assert document["error"] == {
        "type": "bridge",
        "message": "Server is down.",
    }


def refused(call, *args):
    """Returns the refusal one call raises."""
    with pytest.raises(BridgeError) as raised:
        call(*args)
    return raised.value


def test_unregistered_repository_names_setup(tmp_path):
    failure = refused(roster.read, tmp_path)
    assert "no bridge project yet" in str(failure)
    assert failure.next_command == "agent-parley setup ."


def test_main_checkout_names_the_lane_list(tmp_path):
    failure = refused(roster.resolve, {"participants": {}}, tmp_path)
    assert "assigned agent worktree" in str(failure)
    assert failure.next_command == "agent-parley participant list"


def test_missing_credentials_profile_names_its_definition(tmp_path):
    failure = refused(roster.credential, tmp_path, "work")
    assert "Unknown credential profile 'work'" in str(failure)
    assert failure.next_command == "agent-parley credentials add work"


def test_unknown_provider_names_the_provider_list(tmp_path):
    failure = refused(roster.provider, tmp_path, "nobody")
    assert failure.next_command == "agent-parley provider list"


def test_missing_store_names_up():
    failure = store.missing()
    assert str(failure) == store.MISSING
    assert failure.next_command == "agent-parley up"


def test_dirty_lane_commit_keeps_untracked_files(tmp_path):
    lane = tmp_path / "my lane"
    quoted = shlex.quote(str(lane))
    assert worktrees.commit_all(lane) == (
        f"git -C {quoted} add -A && git -C {quoted} commit -m wip"
    )
