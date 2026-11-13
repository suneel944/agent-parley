"""Checks that every decision kind waits for the operator."""

import sys
from pathlib import Path

import pytest

from agent_parley import cli, roster, timeouts, unattended
from agent_parley.state import BridgeError

RETIRED = ("orphan_claim", "closed_claim", "idle_key", "failed_ci")
FAKE = "test_claim"


@pytest.fixture
def reversible(monkeypatch) -> str:
    """Declares one reversible kind, since no shipped kind is reversible."""
    monkeypatch.setitem(
        timeouts.KINDS,
        FAKE,
        timeouts.Kind(timeouts.REVERSIBLE, "resolve", "end a test claim"),
    )
    return FAKE


def test_every_kind_declares_a_class_and_a_default():
    for kind in timeouts.KINDS.values():
        assert kind.reversibility in (
            timeouts.REVERSIBLE,
            timeouts.IRREVERSIBLE,
        )
        assert kind.recommended
    irreversible = {
        name
        for name, kind in timeouts.KINDS.items()
        if kind.reversibility == timeouts.IRREVERSIBLE
    }
    assert irreversible == {
        "merge_default",
        "release_tag",
        "discard_work",
        "delete_branch",
        "native_permission",
    }


@pytest.mark.parametrize(
    "kind",
    [
        name
        for name, entry in timeouts.KINDS.items()
        if entry.reversibility == timeouts.IRREVERSIBLE
    ]
    + ["unknown_kind"],
)
def test_an_irreversible_decision_never_times_out(kind):
    assert timeouts.timeout({}, kind) is None
    assert timeouts.kind_of(kind).recommended == timeouts.WAIT


def test_a_project_policy_override_is_honoured(reversible):
    assert timeouts.timeout({}, reversible) == timeouts.TIMEOUT_SECONDS
    asking = {timeouts.POLICY: {reversible: {timeouts.ASK: True}}}
    assert timeouts.timeout(asking, reversible) is None
    longer = {timeouts.POLICY: {reversible: {timeouts.SECONDS: 7200}}}
    assert timeouts.timeout(longer, reversible) == 7200.0


@pytest.mark.parametrize(
    ("entry", "match"),
    [
        ({"merge_default": {timeouts.SECONDS: 3600}}, "irreversible"),
        ({FAKE: {timeouts.SECONDS: 60}}, "only raise"),
        ({FAKE: {timeouts.ASK: False}}, "invalid"),
    ],
)
def test_no_policy_makes_a_kind_less_cautious(reversible, entry, match):
    with pytest.raises(BridgeError, match=match):
        timeouts.timeout({timeouts.POLICY: entry}, reversible)


@pytest.mark.parametrize("name", RETIRED)
def test_a_stored_policy_for_a_retired_kind_is_ignored(name):
    manifest = {timeouts.POLICY: {name: {timeouts.SECONDS: 7200}}}
    assert name not in timeouts.KINDS
    assert timeouts.policy(manifest) == {}
    assert name not in timeouts.describe(manifest)
    assert timeouts.timeout(manifest, name) is None


def test_the_command_line_sets_shows_and_resets_a_kind(
    bridge, repo, paired, monkeypatch, capsys, reversible
):
    monkeypatch.delenv(unattended.LANE_TOKEN, raising=False)

    def run(*arguments):
        monkeypatch.setattr(
            sys,
            "argv",
            ["agent-parley", "--home", str(bridge.home), *arguments],
        )
        assert cli.main() == 0
        return capsys.readouterr().out

    shown = run(
        "timeout", "set", reversible, "--after", "2h", "--repo", str(repo)
    )
    assert f"{reversible}: reversible, default resolve, 7200s" in shown
    shown = run("timeout", "show", "--repo", str(repo))
    assert "merge_default: irreversible" in shown
    assert "failed_ci" not in shown
    _, directory = bridge.project(repo, create=False)
    assert roster.read(directory)[timeouts.POLICY] == {
        reversible: {timeouts.SECONDS: 7200}
    }
    run("timeout", "set", reversible, "--repo", str(repo))
    assert not roster.read(directory).get(timeouts.POLICY)


def test_a_lane_cannot_set_a_timeout(
    bridge, repo, paired, monkeypatch, reversible
):
    monkeypatch.delenv(unattended.LANE_TOKEN, raising=False)
    lane = Path(paired["lanes"]["codex"])
    with pytest.raises(BridgeError, match="never from an assigned worktree"):
        timeouts.configure(bridge, lane, reversible, seconds=7200)
    with pytest.raises(BridgeError, match="irreversible"):
        timeouts.configure(bridge, repo, "merge_default", seconds=7200)


@pytest.mark.parametrize("name", RETIRED)
def test_a_retired_kind_timeout_is_refused(
    bridge, repo, paired, monkeypatch, name
):
    monkeypatch.delenv(unattended.LANE_TOKEN, raising=False)
    for options in ({"ask": True}, {"seconds": 7200}, {}):
        with pytest.raises(BridgeError, match="not a decision kind"):
            timeouts.configure(bridge, repo, name, **options)
