import sys

import pytest

from agent_parley import cli, plugins

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
state="$FAKE_STATE/{name}"
printf '%s\\n' "$*" >> "$state.log"
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
    state = tmp_path / "state"
    binary.mkdir()
    state.mkdir()
    monkeypatch.setenv("PATH", f"{binary}:/usr/bin:/bin")
    monkeypatch.setenv("FAKE_STATE", str(state))

    def add(name):
        path = binary / name
        path.write_text(FAKE.format(name=name, **LISTINGS[name]))
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
