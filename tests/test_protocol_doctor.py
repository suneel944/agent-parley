"""Checks the protocol contract, its refusals, and the doctor report."""

import importlib.metadata
import json
import re
import subprocess
import sys
import types
from pathlib import Path

import pytest

from agent_parley import checkpoints, cli, protocol, server, store, views
from agent_parley.state import BridgeError

ROOT = Path(__file__).resolve().parent.parent


def test_every_shipped_plugin_declares_this_protocol():
    for client, manifest in protocol.manifests(ROOT).items():
        declared = json.loads(manifest.read_text()).get("protocol")
        assert declared == protocol.PROTOCOL, client


def test_the_documented_table_matches_the_code_constants():
    text = (ROOT / "docs/operations.md").read_text()
    block = text.split("<!-- compatibility:start -->")[1].split(
        "<!-- compatibility:end -->"
    )[0]
    headings = ("| Launcher", "| ---")
    newest = [
        line
        for line in block.splitlines()
        if line.startswith("| ") and not line.startswith(headings)
    ][0]
    _, _, wire, schema, _ = newest.split("|")
    assert int(wire) == protocol.PROTOCOL
    assert int(schema) == store.SCHEMA_VERSION


def test_the_launcher_version_follows_the_checkout():
    declared = (ROOT / "pyproject.toml").read_text()
    version = re.search(r'^version = "([^"]+)"', declared, re.M)[1]
    assert protocol.launcher_version() == version


def test_an_installed_package_without_a_project_file_keeps_its_metadata(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(protocol, "package_root", lambda: tmp_path)
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "9.9.9")
    assert protocol.launcher_version() == "9.9.9"


def test_an_unreadable_project_file_keeps_the_recorded_metadata(
    tmp_path, monkeypatch
):
    (tmp_path / "pyproject.toml").write_text("not = [toml")
    monkeypatch.setattr(protocol, "package_root", lambda: tmp_path)
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "9.9.9")
    assert protocol.launcher_version() == "9.9.9"


def test_a_mismatch_names_both_numbers_and_one_command():
    sentence = protocol.mismatch("installed plugin", 99)
    assert "protocol 99" in sentence
    assert f"protocol {protocol.PROTOCOL}" in sentence
    assert protocol.UPDATE in sentence
    assert "no protocol" in protocol.mismatch("plugin", protocol.UNKNOWN)


def test_an_unreadable_manifest_reports_an_unknown_protocol(tmp_path):
    assert protocol.installed(tmp_path / "missing.json") == protocol.UNKNOWN
    broken = tmp_path / "plugin.json"
    broken.write_text("{ not json")
    assert protocol.installed(broken) == protocol.UNKNOWN
    broken.write_text('{"protocol": "one"}')
    assert protocol.installed(broken) == protocol.UNKNOWN


def test_a_lane_hook_carries_the_launcher_protocol(bridge, repo, paired):
    directory = bridge.project(repo)[1]
    hooks = bridge.hooks("claude", directory)
    command = next(iter(hooks.values()))[0]["hooks"][0]["command"]
    assert f"--protocol {protocol.PROTOCOL}" in command


def test_a_hook_speaking_another_protocol_is_refused(
    bridge, repo, paired, monkeypatch, capsys
):
    directory = bridge.project(repo)[1]
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "checkpoints",
            "--home",
            str(bridge.home),
            "--directory",
            str(directory),
            "--participant",
            "claude",
            "--protocol",
            "99",
        ],
    )
    assert checkpoints.main() == 2
    assert "speaks protocol 99" in capsys.readouterr().err


def call(bridge, actor, tool, headers):
    """Runs one tool through the handler's own envelope with given headers."""
    caller = types.SimpleNamespace(
        headers=headers, server=types.SimpleNamespace(home=bridge.home)
    )
    caller._declared_protocol = types.MethodType(
        server.Handler._declared_protocol, caller
    )
    return server.Handler._call(caller, actor, {"name": tool, "arguments": {}})


def registered(bridge, root, name):
    """Registers one lane and resolves its own store identity."""
    store.initialize(bridge.home)
    token = store.register(bridge.home, root, name)["registration_token"]
    resolved = store.authenticate(bridge.home, token)
    assert resolved is not None
    return resolved


def test_a_served_call_declaring_another_protocol_is_denied(
    bridge, repo, paired
):
    actor = registered(bridge, paired["root"], "claude")
    denied = call(bridge, actor, "list_participants", {protocol.HEADER: "99"})
    assert denied["isError"] is True
    assert "protocol 99" in denied["content"][0]["text"]
    with store.connect(bridge.home) as db:
        rows = db.execute("SELECT outcome FROM events").fetchall()
    assert "error" in [row[0] for row in rows]


def test_an_unreadable_declared_protocol_is_denied_by_name(
    bridge, repo, paired
):
    actor = registered(bridge, paired["root"], "claude")
    denied = call(bridge, actor, "list_participants", {protocol.HEADER: "one"})
    assert "no protocol" in denied["content"][0]["text"]


def test_a_served_call_without_the_header_is_still_served(bridge, repo, paired):
    actor = registered(bridge, paired["root"], "claude")
    assert "isError" not in call(bridge, actor, "list_participants", {})


def test_doctor_reports_every_component(bridge, repo, paired):
    store.initialize(bridge.home)
    reported = bridge.doctor()
    assert reported["consistent"] is True
    assert reported["protocol"] == protocol.PROTOCOL
    assert reported["schema"] == store.SCHEMA_VERSION
    named = {entry["component"] for entry in reported["components"]}
    assert named == {
        "launcher",
        "claude plugin",
        "codex plugin",
        "store",
        "service",
    }
    text = protocol.render(reported)
    assert "Consistent." in text
    assert str(bridge.home) not in text


def store_schema(bridge, schema):
    store.initialize(bridge.home)
    with store.connect(bridge.home, write=True) as db:
        db.execute(f"PRAGMA user_version={schema}")


def reported_store(bridge):
    return [
        entry
        for entry in bridge.doctor()["components"]
        if entry["component"] == "store"
    ][0]


def test_a_store_behind_this_build_breaks_consistency(bridge, repo, paired):
    store_schema(bridge, store.SCHEMA_VERSION - 1)
    reported = bridge.doctor()
    assert reported_store(bridge)["state"] == store.SCHEMA_BEHIND
    assert reported["consistent"] is False
    text = protocol.render(reported)
    assert "NEEDS MIGRATION" in text
    assert protocol.MIGRATE in text
    assert protocol.UPDATE not in text


def test_a_store_ahead_of_this_build_names_the_upgrade(bridge, repo, paired):
    store_schema(bridge, store.SCHEMA_VERSION + 1)
    reported = bridge.doctor()
    assert reported_store(bridge)["state"] == store.SCHEMA_UNSUPPORTED
    assert reported["consistent"] is False
    assert protocol.UPGRADE in protocol.render(reported)


def test_a_store_not_yet_created_is_consistent(bridge, repo, paired):
    assert reported_store(bridge)["state"] == store.SCHEMA_ABSENT
    assert bridge.doctor()["consistent"] is True


def test_a_behind_store_exits_non_zero(bridge, repo, paired, monkeypatch):
    store_schema(bridge, store.SCHEMA_VERSION - 1)
    monkeypatch.setattr(
        sys,
        "argv",
        ["agent-parley", "--home", str(bridge.home), "doctor"],
    )
    assert cli.main() == 1


def test_two_causes_name_two_commands(bridge, repo, paired, monkeypatch):
    store_schema(bridge, store.SCHEMA_VERSION - 1)
    monkeypatch.setattr(protocol, "SUPPORTED", (99,))
    text = protocol.render(bridge.doctor())
    assert protocol.MIGRATE in text
    assert protocol.UPDATE in text


def test_doctor_exits_non_zero_on_a_mismatch(
    bridge, repo, paired, monkeypatch, capsys
):
    monkeypatch.setattr(protocol, "SUPPORTED", (99,))
    monkeypatch.setattr(sys, "argv", ["agent-parley", "doctor"])
    assert cli.main() == 1
    assert "MISMATCH" in capsys.readouterr().out


def test_doctor_reports_one_document(bridge, repo, paired, monkeypatch, capsys):
    monkeypatch.setattr(
        sys,
        "argv",
        ["agent-parley", "--home", str(bridge.home), "doctor", "--json"],
    )
    assert cli.main() == 0
    document = json.loads(capsys.readouterr().out)
    assert document["schema"] == views.SCHEMA
    assert document["kind"] == "doctor"
    assert document["store_schema"] == store.SCHEMA_VERSION
    assert document["consistent"] is True


def test_a_newer_store_schema_is_still_refused(bridge, tmp_path):
    store.initialize(bridge.home)
    with store.connect(bridge.home, write=True) as db:
        db.execute(f"PRAGMA user_version={store.SCHEMA_VERSION + 1}")
    assert store.schema_version(bridge.home) == store.SCHEMA_VERSION + 1
    with pytest.raises(BridgeError, match="Unsupported store schema"):
        store.initialize(bridge.home)


def test_the_release_path_rewrites_the_table(tmp_path):
    sys.path.insert(0, str(ROOT))
    from scripts import release_publish

    root = tmp_path / "checkout"
    (root / "agent_parley").mkdir(parents=True)
    (root / "docs").mkdir()
    (root / "agent_parley/protocol.py").write_text("PROTOCOL = 4\n")
    (root / "agent_parley/store.py").write_text("SCHEMA_VERSION = 9\n")
    (root / "docs/operations.md").write_text(
        "before\n\n"
        f"{release_publish.COMPATIBILITY_START}\n"
        "| Launcher | Wire protocol | Store schema |\n"
        "| --- | --- | --- |\n"
        "| 0.6.0 | 1 | 7 |\n"
        f"{release_publish.COMPATIBILITY_END}\n\nafter\n"
    )
    release_publish.write_compatibility(root, "0.7.0")
    text = (root / "docs/operations.md").read_text()
    assert "| 0.7.0 | 4 | 9 |" in text
    assert "| 0.6.0 | 1 | 7 |" in text
    assert text.startswith("before\n")
    assert text.endswith("after\n")
    release_publish.write_compatibility(root, "0.7.0")
    assert len(re.findall(r"\| 0\.7\.0 \| 4 \| 9 \|", text)) == 1


def test_a_subprocess_hook_refuses_a_mismatched_protocol(bridge, repo, paired):
    directory = bridge.project(repo)[1]
    finished = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_parley.checkpoints",
            "--home",
            str(bridge.home),
            "--directory",
            str(directory),
            "--participant",
            "claude",
            "--protocol",
            "99",
        ],
        input="{}",
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )
    assert finished.returncode == 2
    assert "protocol 99" in finished.stderr
