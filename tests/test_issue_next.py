"""Checks the ranked shortlist a lane reads before it claims anything."""

from pathlib import Path

import pytest

from agent_parley import forge, issues, recommend, store
from agent_parley.cli import git
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


def cochanged(repo, *names):
    """Commits the named files together three times in the base checkout."""
    for index in range(3):
        for name in names:
            path = repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a") as handle:
                handle.write(f"{index}\n")
        git(repo, "add", "--", *names)
        git(
            repo,
            "-c",
            "user.name=Bridge Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            f"together {index}",
        )


def order(ranked):
    """Lists the candidate numbers of one reading."""
    return [record["issue"] for record in ranked["candidates"]]


def test_work_on_held_files_gathers_on_the_lane_that_holds_them(
    bridge, repo, paired, tmp_path, monkeypatch
):
    claude = prepared(bridge, paired, tmp_path, repo)
    codex = Path(paired["lanes"]["codex"])
    bridge.issue(codex, "claim", "48")
    cochanged(repo, "docs/guide.md", "db/schema.sql")
    paths(
        monkeypatch,
        {
            "42": ["src/api.py"],
            "44": ["docs/guide.md"],
            "46": ["src/api.py"],
            "48": ["db/schema.sql"],
        },
    )
    declared(monkeypatch, {})
    ranked = bridge.issue_next(claude)
    assert order(ranked) == ["46", "43", "44"]
    first, _, last = ranked["candidates"]
    assert first["same_files"] == ["42"] and not first["held_back"]
    assert first["reasons"][1] == "same files as #42 you hold"
    assert last["held_back"] == [{"issue": "48", "owner": "codex"}]
    assert last["reasons"][1] == "held back: same files as #48 codex holds"
    peer = bridge.issue_next(codex)
    assert order(peer) == ["44", "43", "46"]
    assert "same files as #48 you hold" in peer["candidates"][0]["reasons"]
    assert (
        "held back: same files as #42 claude holds"
        in peer["candidates"][-1]["reasons"]
    )
    bridge.issue(codex, "release", "48")
    returned = bridge.issue_next(claude)
    assert order(returned)[:3] == ["46", "43", "44"]
    assert returned["candidates"][2]["held_back"] == []
    assert not any(
        reason.startswith("held back")
        for record in returned["candidates"]
        for reason in record["reasons"]
    )
    assert bridge.issue(repo, "list")["issues"]["44"]["owner"] is None


def test_claim_paths_are_read_once_per_interval(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(forge, "_paths_read", {})
    monkeypatch.setattr(forge, "_implementation", lambda repo: "github")
    monkeypatch.setattr(forge, "_reachable", lambda repo: "owner/name")
    monkeypatch.setattr(
        forge,
        "_run",
        lambda command, timeout: (
            calls.append(command) or '[{"files": [{"path": "src/api.py"}]}]'
        ),
    )
    assert forge.issue_pull_request_paths(tmp_path, "48") == ["src/api.py"]
    assert forge.issue_pull_request_paths(tmp_path, "48") == ["src/api.py"]
    assert len(calls) == 1
    forge.issue_pull_request_paths(tmp_path, "49")
    assert len(calls) == 2
    monkeypatch.setattr(forge, "PATHS_TTL", 0.0)
    forge.issue_pull_request_paths(tmp_path, "48")
    assert len(calls) == 3


def test_the_dispatch_order_holds_back_another_lanes_files():
    shared = {
        "44": [{"issue": "48", "owner": "codex"}],
        "46": [{"issue": "42", "owner": "claude"}],
    }
    assert recommend.for_lane(["43", "44", "46"], shared, "claude") == [
        "46",
        "43",
        "44",
    ]
    assert recommend.for_lane(["43", "44", "46"], shared, "codex") == [
        "44",
        "43",
        "46",
    ]
    assert recommend.for_lane(["43", "44"], {}, "claude") == ["43", "44"]


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


def test_a_released_claim_that_ended_on_the_forge_is_not_next_work():
    ended = {
        "revision": 10,
        "issues": {
            **LEDGER["issues"],
            "46": {**LEDGER["issues"]["46"], "ended_on_forge": {"at": 1.0}},
        },
    }
    ranked = recommend.rank(ended, {}, {}, {}, "claude")
    assert [record["issue"] for record in ranked] == ["43", "44"]


PLANNED = {
    "revision": 1,
    "issues": {
        number: {
            "owner": None,
            "offer": None,
            "blocked_by": blockers,
            "execution": {"authorized": True},
        }
        for number, blockers in (
            ("5", []),
            ("10", []),
            ("20", ["10"]),
            ("30", ["10"]),
            ("40", ["10"]),
        )
    },
}


def test_a_blocker_of_planned_work_ranks_above_an_isolated_issue():
    assert issues.unclaimed(PLANNED) == ["10", "5"]
    assert issues.waiters(PLANNED) == {}
    ranked = recommend.rank(PLANNED, {}, {}, {}, "claude")
    assert [record["issue"] for record in ranked] == ["10", "5"]
    assert ranked[0]["planned"] == ["20", "30", "40"]
    assert "unblocks #20, #30, #40 (planned)" in ranked[0]["reasons"]
    assert ranked[1]["reasons"] == ["no recorded dependency blocks it"]


def test_default_waiters_count_every_owned_waiter():
    ledger = {
        "issues": {
            "10": {"owner": None, "blocked_by": []},
            "20": {"owner": "codex", "blocked_by": ["10"]},
            "30": {
                "owner": "claude",
                "blocked_by": ["10"],
                "execution": {"authorized": True, "state": "complete"},
            },
            "40": {"owner": None, "blocked_by": ["10"]},
        }
    }
    assert issues.waiters(ledger) == {"10": ["20", "30"]}
    assert issues.waiters(ledger, owned_only=False) == {"10": ["20"]}


def test_nothing_free_reads_as_nothing_free():
    empty = {"provider": "claude", "forge_paths": False, "candidates": []}
    assert recommend.render(empty) == (
        "No unclaimed, unblocked issue is recorded."
    )
    assert recommend.rank({"revision": 1, "issues": {}}, {}, {}, {}, "") == []
