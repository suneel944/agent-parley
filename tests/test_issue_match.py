"""Checks the goal-to-issue reading a lane runs before opening a new issue."""

from pathlib import Path

from agent_parley import forge, recommend, store

CATALOG = {
    "42": {
        "title": "perf: cut status command startup time",
        "labels": ["performance"],
    },
    "43": {"title": "docs: describe the handoff payload", "labels": []},
    "44": {"title": "feat: export retained events", "labels": ["startup"]},
}


def catalogued(monkeypatch, mapping):
    """Answers the forge open-issue read from a fixed mapping."""
    monkeypatch.setattr(
        "agent_parley.cli.forge.open_issues",
        lambda repo, limit=forge.MAX_OPEN_ISSUES: mapping,
    )


def held(bridge, paired, *keys):
    """Reserves keys through the served tool path as a peer identity."""
    store.initialize(bridge.home)
    token = store.register(bridge.home, paired["root"], "codex")
    peer = store.authenticate(bridge.home, token["registration_token"])
    assert store.call(
        bridge.home, peer, "file_reservation_paths", {"paths": list(keys)}
    )["granted"]
    return peer


def test_terms_drop_the_words_every_goal_shares():
    assert recommend.terms("Fix the slow status command") == {
        "slow",
        "status",
        "command",
    }
    assert recommend.terms("a to be") == set()


def test_a_goal_matches_the_issue_that_already_tracks_it(
    bridge, repo, paired, tmp_path, monkeypatch
):
    catalogued(monkeypatch, CATALOG)
    claude = Path(paired["lanes"]["claude"])
    found = bridge.issue_match(claude, "make the status command start faster")
    assert [record["issue"] for record in found["matches"]] == ["42"]
    assert "title shares command, status" in found["matches"][0]["reasons"]
    assert found["participant"] == "claude"
    assert "faster" in found["terms"]


def test_a_matching_label_counts_as_well_as_a_title(
    bridge, repo, paired, tmp_path, monkeypatch
):
    catalogued(monkeypatch, CATALOG)
    claude = Path(paired["lanes"]["claude"])
    found = bridge.issue_match(claude, "startup cost of every command")
    numbers = [record["issue"] for record in found["matches"]]
    assert numbers == ["42", "44"]
    assert "label startup matches" in found["matches"][1]["reasons"]
    assert found["matches"][1]["matched"] == 1


def test_an_owned_match_says_to_negotiate_rather_than_claim(
    bridge, repo, paired, tmp_path, monkeypatch
):
    catalogued(monkeypatch, CATALOG)
    codex = Path(paired["lanes"]["codex"])
    bridge.issue(codex, "claim", "42")
    claude = Path(paired["lanes"]["claude"])
    found = bridge.issue_match(claude, "status command startup")
    record = found["matches"][0]
    assert record["owner"] == "codex"
    assert (
        "held by codex; negotiate a handoff rather than claim"
        in record["reasons"]
    )


def test_a_peer_reservation_on_the_goal_words_is_named(
    bridge, repo, paired, tmp_path, monkeypatch
):
    catalogued(monkeypatch, CATALOG)
    held(bridge, paired, "agent_parley/status.py")
    claude = Path(paired["lanes"]["claude"])
    found = bridge.issue_match(claude, "status command startup")
    assert found["reservations"] == [
        {"peer": "codex", "path": "agent_parley/status.py"}
    ]
    assert (
        "codex reserves agent_parley/status.py"
        in found["matches"][0]["reasons"]
    )


def test_an_unmatched_goal_recommends_opening_an_issue(
    bridge, repo, paired, tmp_path, monkeypatch
):
    catalogued(monkeypatch, CATALOG)
    claude = Path(paired["lanes"]["claude"])
    found = bridge.issue_match(claude, "rotate the signing certificates")
    assert found["matches"] == []
    assert "opening a new one" in recommend.render_match(found)


def test_a_silent_forge_matches_nothing_rather_than_guessing(
    bridge, repo, paired, tmp_path, monkeypatch
):
    catalogued(monkeypatch, {})
    claude = Path(paired["lanes"]["claude"])
    found = bridge.issue_match(claude, "status command startup")
    assert found["matches"] == []


def test_the_doctor_reading_stays_available_to_a_lane(bridge):
    reported = bridge.doctor()
    assert "consistent" in reported
    named = {record["component"] for record in reported["components"]}
    assert {"launcher", "store"} <= named
    assert all("state" in record for record in reported["components"])
