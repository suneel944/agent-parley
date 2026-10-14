"""Checks the protocol contract, its refusals, and the doctor report."""

import io
import json
import re
import subprocess
import sys
import types
from pathlib import Path

import pytest

from agent_parley import (
    BridgeError as PackageBridgeError,
)
from agent_parley import (
    checkpoints,
    cli,
    plugins,
    protocol,
    records,
    roster,
    server,
    store,
    views,
)
from agent_parley.state import BridgeError

ROOT = Path(__file__).resolve().parent.parent


def test_operational_errors_keep_one_shared_identity():
    assert BridgeError is PackageBridgeError


@pytest.fixture
def answering(monkeypatch):
    """Answers readiness as a service on this build would.

    A stopped service is an outage wherever a lane is registered, so a
    report expected to read consistent needs a service that answers.
    """
    monkeypatch.setattr(
        cli.Bridge,
        "health",
        lambda self: {
            "status": "ready",
            "version": protocol.launcher_version(),
        },
    )


def test_every_shipped_plugin_declares_this_protocol():
    for client, manifest in protocol.manifests(ROOT).items():
        declared = json.loads(manifest.read_text()).get("protocol")
        assert declared == protocol.PROTOCOL, client


def test_the_manifest_lookup_resolves_where_the_plugins_were_installed():
    for client, manifest in protocol.manifests(protocol.plugin_root()).items():
        assert manifest.exists(), client


def test_a_package_carrying_the_plugins_resolves_them_beside_its_modules(
    tmp_path, monkeypatch
):
    package = tmp_path / "site-packages" / "agent_parley"
    (package / "plugins/agent-parley/.claude-plugin").mkdir(parents=True)
    monkeypatch.setattr(protocol, "__file__", str(package / "protocol.py"))
    assert protocol.plugin_root() == package


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


def test_the_monitoring_guide_names_the_stale_remedy_status_prints():
    text = " ".join((ROOT / "docs/monitoring.md").read_text().split())
    assert "Code: ok" not in text
    assert "down && agent-parley up" not in text
    assert protocol.RELAUNCH in text


def test_the_launcher_version_follows_the_checkout():
    declared = (ROOT / "pyproject.toml").read_text()
    version = re.search(r'^version = "([^"]+)"', declared, re.M)[1]
    assert protocol.launcher_version() == version


def test_the_launcher_version_uses_the_runtime_marker(monkeypatch):
    monkeypatch.setattr(protocol, "__version__", "9.9.9")
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
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
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


def test_doctor_reports_every_component(bridge, repo, paired, answering):
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
        "projects",
        "decisions",
        "notify",
        "approvals",
        "dialogs",
    }
    [drift] = [
        entry
        for entry in reported["components"]
        if entry["component"] == "dialogs"
    ]
    assert (drift["state"], drift["remedy"]) == (protocol.OK, "")
    [approvals] = [
        entry
        for entry in reported["components"]
        if entry["component"] == "approvals"
    ]
    assert approvals["state"] == protocol.SETUP_GAP
    assert approvals["remedy"].endswith(
        f"{paired['root']} claude, {paired['root']} codex"
    )
    text = protocol.render(reported)
    assert "Consistent." in text
    assert f"approvals: {approvals['remedy']}" in text
    assert str(bridge.home) not in text


def test_doctor_names_a_recorded_opt_in_the_cli_cannot_carry(
    bridge, repo, paired, answering
):
    store.initialize(bridge.home)
    bridge.add_participant(repo, "amper", "amp", None)
    bridge.resume_opt_in(repo, bridge_tools=True)
    [approvals] = [
        entry
        for entry in bridge.doctor()["components"]
        if entry["component"] == "approvals"
    ]
    assert approvals["state"] == protocol.SETUP_GAP
    assert approvals["remedy"] == (
        "the CLI cannot carry the recorded bridge tool approval, so resume "
        f"these lanes in your own terminal: {paired['root']} amper"
    )


def test_doctor_names_a_project_whose_root_is_gone(
    bridge, repo, paired, answering, tmp_path
):
    store.initialize(bridge.home)
    gone = tmp_path / "parley-probe-gone"
    directory = bridge.home / "projects" / "0000000000000000"
    directory.mkdir(parents=True)
    (directory / "project.json").write_text(json.dumps({"root": str(gone)}))

    reported = bridge.doctor()

    named = {entry["component"]: entry for entry in reported["components"]}
    assert named["projects"]["state"] == protocol.ROOT_GONE
    assert named["projects"]["compatible"] is False
    assert (
        f"agent-parley gc --project {gone} --apply"
        in named["projects"]["remedy"]
    )
    assert str(repo) not in named["projects"]["remedy"]
    assert reported["consistent"] is False
    assert str(gone) in protocol.render(reported)


def test_doctor_warns_on_a_client_newer_than_its_dialog_matchers(
    bridge, answering, tmp_path
):
    store.initialize(bridge.home)
    root = tmp_path / "parley-probe-drift"
    root.mkdir()
    directory = bridge.home / "projects" / "0000000000000001"
    directory.mkdir(parents=True)
    (directory / "project.json").write_text(
        json.dumps({"root": str(root), "participants": {"lane": {}}})
    )
    (directory / "lane-activity.json").write_text(
        json.dumps({"client": "claude", "client_version": "2.1.999 (Claude)"})
    )

    reported = bridge.doctor()

    named = {entry["component"]: entry for entry in reported["components"]}
    assert named["dialogs"]["state"] == protocol.DRIFT
    assert named["dialogs"]["compatible"] is True
    assert (
        "lane: claude 2.1.999 (Claude) is newer" in (named["dialogs"]["remedy"])
    )
    assert f"dialogs: {named['dialogs']['remedy']}" in protocol.render(reported)


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


def claude_plugin(config_dir, version, number):
    """Writes the Claude Code record of one installed plugin version."""
    install = config_dir / "cache" / version
    (install / ".claude-plugin").mkdir(parents=True)
    (install / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"version": version, "protocol": number})
    )
    (config_dir / "plugins").mkdir(parents=True, exist_ok=True)
    (config_dir / "plugins" / "installed_plugins.json").write_text(
        json.dumps(
            {
                "version": 2,
                "plugins": {
                    plugins.PLUGIN: [
                        {"scope": "user", "installPath": str(install)}
                    ]
                },
            }
        )
    )


def claude_rows(reported):
    return {
        entry["component"]: entry
        for entry in reported["components"]
        if entry["component"].startswith("claude plugin")
    }


def test_doctor_reads_the_plugin_the_cli_loads_not_the_bundled_one(
    bridge, repo, paired, answering
):
    store.initialize(bridge.home)
    assert claude_rows(bridge.doctor())["claude plugin"]["version"] == (
        "bundled"
    )
    claude_plugin(Path(records.DEFAULT_HOMES["claude"]), "0.13.0", 1)
    reported = bridge.doctor()
    row = claude_rows(reported)["claude plugin"]
    assert (row["version"], row["state"]) == ("0.13.0", protocol.STALE)
    assert reported["consistent"] is False
    text = protocol.render(reported)
    assert "STALE" in text
    assert "agent-parley plugins install" in text


def test_doctor_reports_a_profile_plugin_speaking_another_protocol(
    bridge, repo, paired, answering, tmp_path
):
    store.initialize(bridge.home)
    roster.define_credential(
        bridge.home, "work", str(tmp_path / "work"), [], []
    )
    bridge.add_participant(repo, "helper", "claude", "work")
    claude_plugin(tmp_path / "work", protocol.launcher_version(), 2)
    reported = bridge.doctor()
    row = claude_rows(reported)["claude plugin (work)"]
    assert (row["protocol"], row["state"]) == (2, protocol.MISMATCH)
    assert reported["consistent"] is False
    assert "MISMATCH" in protocol.render(reported)
    assert str(tmp_path / "work") not in protocol.render(reported)


@pytest.fixture
def claude_on_path(bridge, monkeypatch, tmp_path):
    """Puts a stub claude on PATH that records it was started."""
    binary = tmp_path / "bin"
    binary.mkdir()
    executable = binary / "claude"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import os\n"
        "open(os.environ['CAPTURE'], 'w').close()\n"
    )
    executable.chmod(0o755)
    capture = tmp_path / "started"
    monkeypatch.setenv("CAPTURE", str(capture))
    monkeypatch.setenv("PATH", str(binary) + ":" + "/usr/bin:/bin")
    monkeypatch.setattr(bridge, "up", lambda: None)
    store.initialize(bridge.home)
    roster.define_credential(
        bridge.home, "work", str(tmp_path / "work"), [], []
    )
    return capture


def test_a_lane_whose_profile_plugin_speaks_another_protocol_is_refused(
    bridge, repo, tmp_path, claude_on_path
):
    claude_plugin(tmp_path / "work", protocol.launcher_version(), 2)
    with pytest.raises(BridgeError, match="installed plugin") as refused:
        bridge.launch("helper", repo, "Work on issue 9", "claude", "work")
    assert refused.value.next_command == "agent-parley plugins install"
    assert not claude_on_path.exists()


def test_a_lane_on_an_older_plugin_starts_with_a_warning(
    bridge, repo, tmp_path, claude_on_path, capsys
):
    claude_plugin(tmp_path / "work", "0.13.0", 1)
    assert (
        bridge.launch("helper", repo, "Work on issue 9", "claude", "work") == 0
    )
    assert claude_on_path.exists()
    warning = capsys.readouterr().err
    assert "0.13.0" in warning
    assert "agent-parley plugins install" in warning


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


def test_a_store_not_yet_created_is_consistent(bridge, repo, paired, answering):
    assert reported_store(bridge)["state"] == store.SCHEMA_ABSENT
    assert bridge.doctor()["consistent"] is True


GARBAGE = b"this is not a sqlite database at all, only text" * 4


def damage_store(bridge):
    bridge.home.mkdir(parents=True, exist_ok=True)
    (bridge.home / store.DATABASE).write_bytes(GARBAGE)


def test_a_damaged_store_is_never_reported_absent(bridge, repo, paired):
    damage_store(bridge)
    state = store.schema_state(store.schema_version(bridge.home))
    assert state == store.SCHEMA_DAMAGED
    assert store.remedy(state) == protocol.REPAIR
    assert "not a database" in store.damage(bridge.home)
    assert reported_store(bridge)["state"] == store.SCHEMA_DAMAGED
    assert bridge.doctor()["consistent"] is False
    reconciled = store.reconcile(bridge.home)
    assert "is damaged" in reconciled
    assert protocol.REPAIR in reconciled
    assert protocol.REPAIR in checkpoints.outage(bridge.home, "boom")


def test_status_names_a_damaged_store_and_its_repair(
    bridge, repo, paired, monkeypatch, capsys
):
    damage_store(bridge)
    monkeypatch.setattr(
        sys, "argv", ["agent-parley", "--home", str(bridge.home), "status"]
    )
    cli.main()
    assert (
        "Store: damaged (file is not a database); run agent-parley store "
        "repair" in capsys.readouterr().out
    )


def test_the_repair_keeps_the_damaged_bytes_and_opens_a_fresh_store(
    bridge, repo, paired, monkeypatch, capsys
):
    damage_store(bridge)
    (bridge.home / f"{store.DATABASE}-wal").write_bytes(b"wal")
    monkeypatch.setattr(
        sys,
        "argv",
        ["agent-parley", "--home", str(bridge.home), "store", "repair"],
    )
    cli.main()
    printed = capsys.readouterr().out
    kept = {
        path.name.split(".corrupt-")[0]: path
        for path in bridge.home.glob(f"{store.DATABASE}*.corrupt-*")
    }
    assert kept[store.DATABASE].read_bytes() == GARBAGE
    assert kept[f"{store.DATABASE}-wal"].read_bytes() == b"wal"
    assert all(str(path) in printed for path in kept.values())
    assert store.schema_version(bridge.home) == store.SCHEMA_VERSION
    with store.connect(bridge.home) as db:
        db.execute("SELECT 1").fetchone()
    cli.main()
    assert "nothing was moved" in capsys.readouterr().out
    assert len(list(bridge.home.glob("*.corrupt-*"))) == len(kept)


def test_a_missing_store_still_reads_absent_after_the_check(bridge):
    assert store.damage(bridge.home) == ""
    assert store.schema_state(store.schema_version(bridge.home)) == (
        store.SCHEMA_ABSENT
    )
    assert store.repair(bridge.home) == ("", [])


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


def test_doctor_reports_one_document(
    bridge, repo, paired, answering, monkeypatch, capsys
):
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
