import json
import sys
from pathlib import Path

import pytest

from agent_parley import cli, plugins, protocol, records, roster

LISTINGS = {
    "claude": {
        "market": '[{"name": "agent-parley", "repo": "suneel944/x"}]',
        "no-market": '[{"name": "other", "repo": "someone/agent-parley"}]',
        "plugin": '[{"id": "agent-parley@agent-parley", "enabled": true}]',
        "no-plugin": "[]",
    },
    "codex": {
        "market": "MARKETPLACE  ROOT\nagent-parley  /tmp/agent-parley",
        "no-market": "MARKETPLACE  ROOT\npersonal  /home",
        "plugin": "agent-parley@agent-parley  installed, enabled  0.15.0",
        "no-plugin": "agent-parley@agent-parley  not installed  0.15.0",
    },
}

FAKE = """#!/bin/sh
selected="${{{home_env}-}}"
state="$FAKE_STATE/{name}$(printf '%s' "$selected" | tr / _)"
printf '%s\\n' "$*" >> "$state.log"
printf '%s %s\\n' "${{selected:-unset}}" "$*" >> "$FAKE_STATE/{name}.homes"
[ -f "$state.fail" ] && exit 3
case "$*" in
    "plugin marketplace list"*)
        if [ -f "$state.market" ]; then echo '{market}'
        else echo '{no-market}'; fi ;;
    "plugin marketplace add "*) touch "$state.market" ;;
    "plugin list"*)
        if [ -f "$state.plugin" ]; then echo '{plugin}'
        else echo '{no-plugin}'; fi ;;
    "plugin install "* | "plugin add "*) touch "$state.plugin" ;;
esac
exit 0
"""


@pytest.fixture
def fake(tmp_path, monkeypatch):
    binary = tmp_path / "bin"
    state = tmp_path / "fake-state"
    binary.mkdir()
    state.mkdir()
    monkeypatch.setenv("PATH", f"{binary}:/usr/bin:/bin")
    monkeypatch.setenv("FAKE_STATE", str(state))

    def add(name):
        path = binary / name
        path.write_text(
            FAKE.format(
                name=name, home_env=plugins.HOME_ENV[name], **LISTINGS[name]
            )
        )
        path.chmod(0o755)

    return add, state


def calls(state, name):
    return (state / f"{name}.log").read_text().splitlines()


def test_nothing_on_path_is_reported_and_not_a_failure(fake):
    lines, succeeded = plugins.install()
    assert succeeded
    assert "No supported CLI on PATH (claude, codex)" in lines[0]
    assert plugins.status() == ["claude: not on PATH", "codex: not on PATH"]


def test_install_adds_marketplace_and_plugin_to_each_cli(fake):
    add, state = fake
    add("claude")
    add("codex")
    assert plugins.status() == [
        "claude: marketplace not added",
        "codex: marketplace not added",
    ]
    lines, succeeded = plugins.install()
    assert succeeded
    assert "claude: marketplace suneel944/agent-parley added" in lines
    assert "codex: plugin agent-parley@agent-parley installed" in lines
    assert "plugin marketplace add suneel944/agent-parley" in calls(
        state, "claude"
    )
    assert "plugin add agent-parley@agent-parley" in calls(state, "codex")
    assert plugins.status() == [
        "claude: plugin agent-parley@agent-parley installed",
        "codex: plugin agent-parley@agent-parley installed",
    ]


def test_rerun_refreshes_and_never_adds_twice(fake):
    add, state = fake
    add("claude")
    add("codex")
    plugins.install()
    lines, succeeded = plugins.install()
    assert succeeded
    assert lines == [
        "claude: marketplace agent-parley refreshed",
        "claude: plugin agent-parley@agent-parley updated",
        "codex: marketplace agent-parley refreshed",
        "codex: plugin agent-parley@agent-parley already installed",
    ]
    for name in ("claude", "codex"):
        adds = [
            call
            for call in calls(state, name)
            if call.startswith(
                ("plugin marketplace add", "plugin install", "plugin add")
            )
        ]
        assert len(adds) == 2


def test_one_failing_cli_leaves_the_other_installed(fake):
    add, state = fake
    add("claude")
    add("codex")
    (state / "claude.fail").touch()
    lines, succeeded = plugins.install()
    assert not succeeded
    assert lines[0].startswith("claude: failed: ")
    assert "codex: plugin agent-parley@agent-parley installed" in lines


def test_listing_matches_whole_names_only():
    assert plugins.names('"name": "agent-parley",', "agent-parley")
    assert not plugins.names('"repo": "someone/agent-parley"', "agent-parley")


def test_cli_dispatches_plugins_without_a_store(fake, tmp_path, monkeypatch):
    add, _ = fake
    add("claude")
    home = tmp_path / "home"
    monkeypatch.setattr(
        sys, "argv", ["agent-parley", "--home", str(home), "plugins", "install"]
    )
    assert cli.main() == 0
    assert not home.exists()


def homes(state, name, command):
    return [
        line.split(" ", 1)[0]
        for line in (state / f"{name}.homes").read_text().splitlines()
        if line.split(" ", 1)[1].startswith(command)
    ]


def test_install_runs_once_per_config_home_with_that_home_selected(
    fake, bridge, repo, tmp_path, monkeypatch
):
    add, state = fake
    add("claude")
    add("codex")
    roster.define_credential(
        bridge.home, "work", str(tmp_path / "work"), [], []
    )
    bridge.add_participant(repo, "lane-1", "claude", "work")
    bridge.add_participant(repo, "lane-2", "claude", "work")
    bridge.add_participant(repo, "lane-3", "codex")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "shell"))
    lines, succeeded = plugins.install(bridge.home)
    assert succeeded
    assert sorted(homes(state, "claude", "plugin install")) == sorted(
        ["unset", str(tmp_path / "shell"), str(tmp_path / "work")]
    )
    assert homes(state, "codex", "plugin add") == ["unset"]
    assert "claude (work): plugin agent-parley@agent-parley installed" in lines
    assert "claude (shell): plugin agent-parley@agent-parley installed" in lines
    ref = f"v{protocol.launcher_version()}"
    assert f"plugin marketplace add suneel944/agent-parley --ref {ref}" in (
        calls(state, "codex")
    )
    assert f"codex: marketplace suneel944/agent-parley added at {ref}" in lines


def claude_record(config_dir, cache, version):
    manifest = cache / version / ".claude-plugin" / "plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"version": version, "protocol": 1}))
    record = config_dir / "plugins" / "installed_plugins.json"
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(
        json.dumps(
            {
                "version": 2,
                "plugins": {
                    plugins.PLUGIN: [
                        {
                            "scope": "user",
                            "installPath": str(manifest.parent.parent),
                            "version": version,
                        }
                    ]
                },
            }
        )
    )
    return manifest


def test_status_names_the_version_each_home_has_installed(fake, tmp_path):
    add, _ = fake
    add("claude")
    plugins.install()
    claude_record(
        Path(records.DEFAULT_HOMES["claude"]), tmp_path / "cache", "0.13.0"
    )
    assert plugins.status() == [
        "claude: plugin agent-parley@agent-parley installed at 0.13.0, "
        f"launcher is {protocol.launcher_version()}",
        "codex: not on PATH",
    ]


def test_the_codex_record_resolves_its_highest_cached_version(tmp_path):
    home = tmp_path / "codex"
    home.mkdir()
    assert plugins.installed_manifest("codex", home) is None
    (home / "config.toml").write_text(
        f'[plugins."{plugins.PLUGIN}"]\nenabled = true\n'
    )
    for version in ("0.9.0", "0.18.0"):
        manifest = home / plugins.CODEX_CACHE / version / ".codex-plugin"
        manifest.mkdir(parents=True)
        (manifest / "plugin.json").write_text(json.dumps({"version": version}))
    found = plugins.installed_manifest("codex", home)
    assert found is not None and found.parent.parent.name == "0.18.0"
    (home / "config.toml").write_text(
        f'[plugins."{plugins.PLUGIN}"]\nenabled = false\n'
    )
    assert plugins.installed_manifest("codex", home) is None


@pytest.fixture
def repairing(fake, tmp_path, monkeypatch):
    add, _ = fake
    add("claude")
    monkeypatch.delenv(plugins.REPAIR_ENV)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    default = Path(records.DEFAULT_HOMES["claude"])
    claude_record(default, tmp_path / "cache", "0.13.0")
    installs = []
    outcome = {"version": protocol.launcher_version(), "error": ""}

    def install_one(client, executable, env, label):
        installs.append(label)
        if outcome["error"]:
            raise plugins.Failed(outcome["error"])
        (default / "plugins").joinpath("installed_plugins.json").unlink()
        claude_record(
            default, tmp_path / f"cache-{len(installs)}", outcome["version"]
        )
        return []

    monkeypatch.setattr(plugins, "install_one", install_one)
    return tmp_path / "state", installs, outcome


def test_a_stale_plugin_is_repaired_once_per_launcher_version(repairing):
    home, installs, _ = repairing
    home.mkdir()
    launcher = protocol.launcher_version()
    assert plugins.repair(home, now=100.0) == [
        f"claude: plugin 0.13.0 repaired to {launcher}"
    ]
    assert plugins.repair(home, now=200.0) == []
    assert plugins.repair(home, now=9000.0) == []
    assert installs == [""]
    entry = next(iter(plugins.repairs(home).values()))
    assert entry["state"] == plugins.REPAIRED
    assert (entry["from"], entry["to"], entry["at"]) == (
        "0.13.0",
        launcher,
        100,
    )


def test_a_current_plugin_and_an_uninstalled_home_are_left_alone(
    fake, tmp_path, monkeypatch
):
    add, state = fake
    add("claude")
    monkeypatch.delenv(plugins.REPAIR_ENV)
    home = tmp_path / "state"
    home.mkdir()
    assert plugins.repair(home) == []
    claude_record(
        Path(records.DEFAULT_HOMES["claude"]),
        tmp_path / "cache",
        protocol.launcher_version(),
    )
    assert plugins.repair(home) == []
    assert not (state / "claude.log").exists()
    assert not (home / plugins.REPAIR_RECORD).exists()


def test_a_failed_repair_is_recorded_and_retried_with_backoff(repairing):
    home, installs, outcome = repairing
    home.mkdir()
    outcome["error"] = "claude plugin update exited 1: offline"
    assert plugins.repair(home, now=100.0) == [
        "claude: plugin 0.13.0 failed: claude plugin update exited 1: offline"
    ]
    entry = next(iter(plugins.repairs(home).values()))
    assert (entry["state"], entry["next_at"]) == (plugins.FAILED, 160.0)
    assert plugins.repair(home, now=150.0) == []
    outcome["version"] = "0.13.0"
    outcome["error"] = ""
    assert plugins.repair(home, now=170.0) == [
        "claude: plugin 0.13.0 failed: plugin still at 0.13.0 after install"
    ]
    entry = next(iter(plugins.repairs(home).values()))
    assert (entry["attempts"], entry["next_at"]) == (2, 290.0)
    outcome["version"] = protocol.launcher_version()
    assert plugins.repair(home, now=290.0)[0].endswith(
        f"repaired to {protocol.launcher_version()}"
    )
    assert installs == ["", "", ""]


def test_the_repair_switch_turns_the_service_repair_off(repairing, monkeypatch):
    home, installs, _ = repairing
    home.mkdir()
    monkeypatch.setenv(plugins.REPAIR_ENV, "off")
    assert plugins.repair(home) == []
    assert installs == []


def test_lanes_started_before_a_repair_are_named_with_their_resume(
    repairing, bridge, repo
):
    from agent_parley import problems

    _, _, outcome = repairing
    bridge.add_participant(repo, "lane-1", "claude")
    bridge.add_participant(repo, "lane-2", "claude")
    directory = bridge.project(repo, create=False)[1]
    plugins.repair(bridge.home, now=1000.0)
    root = str(repo.resolve())
    project = {
        "root": root,
        "participants": [
            {
                "participant": name,
                "availability": {"process_alive": True},
                "session_started": started,
            }
            for name, started in (("lane-1", 900.0), ("lane-2", 1100.0))
        ],
    }
    data = roster.read(directory)
    [row] = problems._plugin_rows(bridge.home, directory, data, project, 1200)
    assert row["condition"] == problems.PLUGIN
    assert "lane-1" in row["detail"] and "lane-2" not in row["detail"]
    assert "agent-parley run lane-1 --repo" in row["command"]
    assert row["command"].endswith("--resume")
    assert row["seconds"] == 200
    project["participants"][0]["availability"]["process_alive"] = False
    assert (
        problems._plugin_rows(bridge.home, directory, data, project, 1200) == []
    )
    outcome["error"] = "offline"
    entry = next(iter(plugins.repairs(bridge.home).values()))
    entry.update(state=plugins.FAILED, error="offline")
    (bridge.home / plugins.REPAIR_RECORD).write_text(
        json.dumps({f"claude:{entry['directory']}": entry})
    )
    project["participants"][0]["availability"]["process_alive"] = True
    [row] = problems._plugin_rows(bridge.home, directory, data, project, 1200)
    assert "failed, retrying: offline" in row["detail"]
    assert row["command"] == "agent-parley plugins install"


def test_lanes_on_an_older_plugin_name_the_oldest_resume(bridge, repo):
    from agent_parley import problems

    for name in ("lane-1", "lane-2", "lane-3"):
        bridge.add_participant(repo, name, "claude")
    directory = bridge.project(repo, create=False)[1]
    project = {
        "root": str(repo.resolve()),
        "participants": [
            {
                "participant": name,
                "availability": {"process_alive": True},
                "plugin_stale": name != "lane-3",
                "session_started": started,
            }
            for name, started in (
                ("lane-1", 900.0),
                ("lane-2", 800.0),
                ("lane-3", 700.0),
            )
        ],
    }
    data = roster.read(directory)
    [row] = problems._plugin_rows(bridge.home, directory, data, project, 1000)
    assert "lane-1, lane-2" in row["detail"] and "lane-3" not in row["detail"]
    assert "agent-parley run lane-2 --repo" in row["command"]
    assert "lane-1" not in row["command"]
    assert (row["count"], row["seconds"]) == (2, 200)
