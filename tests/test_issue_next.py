"""Checks the ranked shortlist a lane reads before it claims anything."""

from pathlib import Path

import pytest

from agent_parley import forge, recommend, store
from agent_parley.state import BridgeError

PLAN = """
[plan]
name = "Next work"

[groups]
rewrite = ["42", "43"]
"""

LEDGER = {
    "revision": 9,
    "issues": {
        "42": {"owner": "claude", "offer": None, "blocked_by": []},
        "43": {
            "owner": None,
            "offer": None,
            "blocked_by": [],
            "execution": {"authorized": True},
        },
        "44": {
            "owner": None,
            "offer": None,
            "blocked_by": [],
            "title": "Docs",
            "execution": {"authorized": True},
        },
        "46": {
            "owner": None,
            "offer": None,
            "blocked_by": [],
            "execution": {"authorized": True},
        },
        "47": {"owner": "claude", "offer": None, "blocked_by": ["46"]},
    },
}


def applied(bridge, repo, tmp_path):
    """Applies a plan naming one group of two issues."""
    path = tmp_path / "next-plan.toml"
    path.write_text(PLAN)
    return bridge.work_plan(repo, "apply", path)


def prepared(bridge, paired, tmp_path, repo):
    """Builds a ledger with one owned group, three free issues and a waiter."""
    applied(bridge, repo, tmp_path)
    claude = Path(paired["lanes"]["claude"])
    codex = Path(paired["lanes"]["codex"])
    bridge.issue(claude, "claim", "42")
    for number in ("43", "44", "46"):
        bridge.issue(codex, "claim", number)
        bridge.issue(codex, "release", number)
    bridge.issue(claude, "claim", "47")
    bridge.issue(claude, "block", "47", on="46")
    return claude


def held(bridge, paired, *keys):
    """Reserves keys through the served tool path as a peer identity."""
    store.initialize(bridge.home)
    token = store.register(bridge.home, paired["root"], "codex")
    peer = store.authenticate(bridge.home, token["registration_token"])
    assert store.call(
        bridge.home, peer, "file_reservation_paths", {"paths": list(keys)}
    )["granted"]
    return peer


def paths(monkeypatch, mapping):
    """Answers the forge path lookup from a fixed mapping."""
    monkeypatch.setattr(
        "agent_parley.recommend.forge.issue_pull_request_paths",
        lambda repo, number: mapping.get(number, []),
    )


def declared(monkeypatch, mapping):
    """Answers the forge provider lookup from a fixed mapping."""
    monkeypatch.setattr(
        "agent_parley.recommend.forge.issue_providers",
        lambda repo, limit=forge.MAX_OPEN_ISSUES: mapping,
    )


def test_the_shortlist_prefers_a_started_group_and_clear_paths(
    bridge, repo, paired, tmp_path, monkeypatch
):
    claude = prepared(bridge, paired, tmp_path, repo)
    held(bridge, paired, "src/api.py")
    paths(monkeypatch, {"44": ["src/api.py"]})
    declared(monkeypatch, {})
    ranked = bridge.issue_next(claude)
    assert [record["issue"] for record in ranked["candidates"]] == [
        "43",
        "46",
        "44",
    ]
    first, second, last = ranked["candidates"]
    assert "plan group rewrite is already under way" in first["reasons"]
    assert "unblocks #47" in second["reasons"]
    assert last["overlaps"] == [{"path": "src/api.py", "peer": "codex"}]
    assert "codex reserves src/api.py" in last["reasons"]
    assert ranked["participant"] == "claude" and ranked["forge_paths"]


def test_a_declared_provider_this_lane_does_not_run_ranks_lower(
    bridge, repo, paired, tmp_path, monkeypatch
):
    claude = prepared(bridge, paired, tmp_path, repo)
    paths(monkeypatch, {})
    declared(monkeypatch, {"43": "codex"})
    ranked = bridge.issue_next(claude)
    assert [record["issue"] for record in ranked["candidates"]] == [
        "46",
        "44",
        "43",
    ]
    assert ranked["forge_paths"] is False
    assert (
        "declares provider codex, not this lane's claude"
        in (ranked["candidates"][-1]["reasons"])
    )
    declared(monkeypatch, {"44": "claude"})
    matched = bridge.issue_next(claude)
    assert (
        "declares provider claude, which this lane runs"
        in (matched["candidates"][-1]["reasons"])
    )


def test_the_same_state_ranks_identically_and_claims_nothing(
    bridge, repo, paired, tmp_path, monkeypatch
):
    claude = prepared(bridge, paired, tmp_path, repo)
    paths(monkeypatch, {})
    declared(monkeypatch, {})
    before = bridge.issue(repo, "list")
    first = bridge.issue_next(claude)
    second = bridge.issue_next(claude)
    assert first == second
    after = bridge.issue(repo, "list")
    assert after == before
    assert [record["issue"] for record in first["candidates"]] == [
        "43",
        "46",
        "44",
    ]
    assert all(
        after["issues"][record["issue"]]["owner"] is None
        for record in first["candidates"]
    )


def test_the_limit_bounds_the_shortlist(
    bridge, repo, paired, tmp_path, monkeypatch
):
    claude = prepared(bridge, paired, tmp_path, repo)
    paths(monkeypatch, {})
    declared(monkeypatch, {})
    assert len(bridge.issue_next(claude, 1)["candidates"]) == 1
    assert len(bridge.issue_next(claude, 0)["candidates"]) == 1


def test_the_served_tool_recommends_without_writing_anything(
    bridge, repo, paired, tmp_path, monkeypatch
):
    prepared(bridge, paired, tmp_path, repo)
    peer = held(bridge, paired, "docs/**")
    paths(monkeypatch, {})
    declared(monkeypatch, {})
    served = store.call(bridge.home, peer, "next_issues", {"limit": 2})
    assert [record["issue"] for record in served["candidates"]] == [
        "43",
        "46",
    ]
    assert served["provider"] == "codex"
    assert bridge.issue(repo, "list")["issues"]["43"]["owner"] is None
    with pytest.raises(BridgeError):
        store.call(bridge.home, peer, "next_issues", {"limit": 0})


def test_ranking_states_its_reasons_without_reading_any_forge():
    ranked = recommend.rank(
        LEDGER,
        {"rewrite": ["42", "43"], "docs": ["44"]},
        {"44": {"overlaps": [], "collisions": []}},
        {},
        "claude",
    )
    assert [record["issue"] for record in ranked] == ["43", "46", "44"]
    assert ranked[0]["reasons"] == [
        "no recorded dependency blocks it",
        "plan group rewrite is already under way",
    ]
    assert ranked[1]["unblocks"] == ["47"]
    assert ranked[2]["title"] == "Docs"
    assert ranked[2]["reasons"] == [
        "no recorded dependency blocks it",
        "plan group docs is not started",
        "no peer reservation or forecast collision on its paths",
    ]


def test_a_forecast_collision_ranks_below_a_clean_path():
    risks = {
        "43": {
            "overlaps": [],
            "collisions": [{"path": "db/schema.sql", "peer": "x", "count": 4}],
        }
    }
    ranked = recommend.rank(LEDGER, {}, risks, {}, "claude")
    assert [record["issue"] for record in ranked] == ["46", "44", "43"]
    assert "likely to collide with x on db/schema.sql" in ranked[-1]["reasons"]


def test_nothing_free_reads_as_nothing_free():
    empty = {"provider": "claude", "forge_paths": False, "candidates": []}
    assert recommend.render(empty) == (
        "No unclaimed, unblocked issue is recorded."
    )
    assert recommend.rank({"revision": 1, "issues": {}}, {}, {}, {}, "") == []
