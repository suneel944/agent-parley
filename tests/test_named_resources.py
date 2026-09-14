"""Checks reservations of named resources beside repository-relative paths."""

import json
import sys

import pytest

from agent_parley import cli, roster, store, views
from agent_parley.state import BridgeError


def actor(bridge, root, name):
    """Registers one lane and resolves its own store identity."""
    store.initialize(bridge.home)
    token = store.register(bridge.home, root, name)["registration_token"]
    resolved = store.authenticate(bridge.home, token)
    assert resolved is not None
    return resolved


def reserve(bridge, holder, *keys, **arguments):
    """Reserves keys as one lane through the served tool path."""
    return store.call(
        bridge.home,
        holder,
        "file_reservation_paths",
        {"paths": list(keys), **arguments},
    )


def test_a_named_resource_is_granted_beside_a_path(bridge, repo, paired):
    holder = actor(bridge, paired["root"], "claude")
    granted = reserve(bridge, holder, "port:5432", "src/engine.py")
    assert [lease["path"] for lease in granted["granted"]] == [
        "port:5432",
        "src/engine.py",
    ]
    assert granted["conflicts"] == []


def test_a_named_resource_conflicts_only_on_an_exact_match(
    bridge, repo, paired
):
    owner = actor(bridge, paired["root"], "claude")
    peer = actor(bridge, paired["root"], "codex")
    reserve(bridge, owner, "port:5432", reason="dev server")
    assert reserve(bridge, peer, "port:5433")["granted"]
    conflicted = reserve(bridge, peer, "port:5432")
    assert conflicted["granted"] == []
    assert conflicted["conflicts"] == [
        {"path": "port:5432", "owner": "claude", "reason": "dev server"}
    ]


def test_a_named_resource_is_not_matched_as_a_glob_or_a_directory(
    bridge, repo, paired
):
    owner = actor(bridge, paired["root"], "claude")
    peer = actor(bridge, paired["root"], "codex")
    reserve(bridge, owner, "suite:integration", "db:local")
    assert reserve(bridge, peer, "suite:integration-slow")["granted"]
    assert reserve(bridge, peer, "db:localhost")["granted"]
    with pytest.raises(BridgeError, match="not a named resource"):
        reserve(bridge, peer, "suite:*")
    with pytest.raises(BridgeError, match="not a named resource"):
        reserve(bridge, peer, "db:local/tables")


@pytest.mark.parametrize(
    "key", ["port:", "PORT:5432", "port:/etc/passwd", "a" * 17 + ":x"]
)
def test_a_malformed_resource_is_refused_by_name(bridge, repo, paired, key):
    holder = actor(bridge, paired["root"], "claude")
    with pytest.raises(BridgeError, match="not a named resource"):
        reserve(bridge, holder, key)


def test_an_undeclared_resource_is_refused_with_the_declared_list(
    bridge, repo, paired
):
    bridge.resources(repo, "port:5432 db:local")
    holder = actor(bridge, paired["root"], "claude")
    with pytest.raises(BridgeError, match="not declared for this project"):
        reserve(bridge, holder, "port:9999")
    assert reserve(bridge, holder, "port:5432")["granted"]


def test_no_declaration_accepts_every_well_formed_name(bridge, repo, paired):
    holder = actor(bridge, paired["root"], "claude")
    assert reserve(bridge, holder, "device:android-1")["granted"]
    assert store.declared_resources(bridge.home, paired["root"]) is None


def test_a_declaration_lives_outside_the_target_repository(bridge, repo):
    bridge.setup(repo)
    message = bridge.resources(repo, "port:5432")
    assert "declares 1 named resources" in message
    assert "port:5432" in message
    _, directory = bridge.project(repo)
    stored = json.loads((directory / "project.json").read_text())
    assert stored["resources"] == ["port:5432"]
    assert not list(repo.glob(".agent-parley*"))
    assert "no named resources" in bridge.resources(repo, "")


def test_a_malformed_declaration_is_refused(bridge, repo):
    bridge.setup(repo)
    with pytest.raises(BridgeError, match="not a named resource"):
        bridge.resources(repo, "port 5432")
    with pytest.raises(BridgeError, match="at most 64"):
        roster.resources([f"port:{number}" for number in range(1, 100)])


def test_status_reports_held_named_resources(bridge, repo, paired, capsys):
    holder = actor(bridge, paired["root"], "claude")
    reserve(bridge, holder, "port:5432", "suite:integration", ttl_seconds=60)
    bridge.status(cli.Selection(participant="claude"))
    output = capsys.readouterr().out
    assert "active reservations: 2" in output
    assert "Named resources held: port:5432, suite:integration" in output


def test_the_resource_declaration_reports_as_json(
    bridge, repo, monkeypatch, capsys
):
    bridge.setup(repo)
    bridge.resources(repo, "port:5432")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agent-parley",
            "--home",
            str(bridge.home),
            "resources",
            "show",
            "--repo",
            str(repo),
            "--json",
        ],
    )
    assert cli.main() == 0
    document = json.loads(capsys.readouterr().out)
    assert document["schema"] == views.SCHEMA
    assert document["kind"] == "resources"
    assert document["resources"] == ["port:5432"]
    assert document["declared"] is True
