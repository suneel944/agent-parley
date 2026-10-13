"""Checks that new forge issues and distinct leads reach waiting lanes."""

import json
import os
import time
from pathlib import Path

import pytest

from agent_parley import forge, issues, recommend, store, supervision
from agent_parley.process import start_ticks
from agent_parley.state import write_json
from agent_parley.status import FORGE_ISSUES, FORGE_STALE


@pytest.fixture(autouse=True)
def quiet_forge(monkeypatch):
    """Keeps the poll and the shortlist off the host forge."""
    monkeypatch.setattr(
        supervision.forge, "branch_completion", lambda *args: None
    )
    monkeypatch.setattr(
        "agent_parley.recommend.forge.issue_pull_request_paths",
        lambda repo, number: [],
    )
    monkeypatch.setattr(
        "agent_parley.recommend.forge.issue_providers",
        lambda repo, limit=forge.MAX_OPEN_ISSUES: {},
    )


def cached(directory, titles, age=0.0):
    """Writes the supervisor's forge cache with the given open issues."""
    write_json(
        directory / FORGE_ISSUES,
        {
            "read_at": time.time() - age,
            "limit": 1000,
            "issues": {
                number: {"title": title} for number, title in titles.items()
            },
        },
    )


def alive(directory, name):
    """Publishes a live, quiet session for one lane."""
    write_json(
        directory / f"{name}-activity.json",
        {
            "session_pid": os.getpid(),
            "session_ticks": start_ticks(os.getpid()),
            "activity": "idle",
            "updated": time.time(),
        },
    )


def swept(bridge, directory):
    """Runs one work sweep and returns the manifest and settings it used."""
    manifest = json.loads((directory / "project.json").read_text())
    config = supervision.configuration(bridge.home, manifest)
    supervision.work(bridge.home, directory, manifest, config)
    return manifest, config


def offer_for(directory, name):
    """Returns the advisory offer published for one lane, if any."""
    return supervision.published_work(directory, name)["offer"]


def ready(owner):
    """Builds one ledger record whose claim waits on CI and review."""
    return {
        "owner": owner,
        "claim_id": f"{owner}-c",
        "offer": None,
        "blocked_by": [],
        "execution": {
            "authorized": True,
            "state": "ready",
            "claim_id": f"{owner}-c",
        },
    }


def three_lanes(bridge, repo, paired):
    """Adds a third lane, registers all three and marks them alive."""
    bridge.add_participant(repo, "copilot", "claude")
    directory = Path(paired["lanes"]["claude"]).parent
    store.initialize(bridge.home)
    for name in ("claude", "codex", "copilot"):
        store.register(bridge.home, paired["root"], name)
        alive(directory, name)
    return directory


def test_open_forge_issues_the_ledger_never_saw_are_free_candidates(
    tmp_path,
):
    ledger = {
        "revision": 3,
        "issues": {
            "41": {**ready("claude"), "title": "Held"},
            "42": {
                "owner": None,
                "ended_on_forge": {"at": 1.0},
                "execution": {"authorized": True},
            },
        },
    }
    cached(tmp_path, {"41": "Held", "42": "Ended", "50": "New work"})
    pool = issues.with_forge(tmp_path, ledger)
    assert issues.unclaimed(pool) == ["50"]
    assert pool["issues"]["50"]["title"] == "New work"
    assert pool["issues"]["50"]["forge_only"] is True
    ranked = recommend.rank(pool, {}, {}, {}, "claude")
    assert [record["issue"] for record in ranked] == ["50"]
    assert "50" not in ledger["issues"]
    assert not (tmp_path / "issues.json").exists()


def test_a_forge_issue_held_by_someone_else_or_not_ready_is_not_offered(
    tmp_path,
):
    ledger = {"revision": 1, "issues": {}}
    write_json(
        tmp_path / FORGE_ISSUES,
        {
            "read_at": time.time(),
            "login": "operator",
            "issues": {
                "50": {"title": "Theirs", "assignees": ["teammate"]},
                "51": {"title": "Blocked", "labels": ["blocked"]},
                "52": {"title": "Mine", "assignees": ["operator"]},
                "53": {"title": "Free", "labels": ["bug"], "assignees": []},
                "54": {"title": "Old shape"},
            },
        },
    )
    pool = issues.with_forge(tmp_path, ledger)
    assert issues.unclaimed(pool) == ["52", "53", "54"]
    assert issues.forge_assignees(tmp_path, "50") == ["teammate"]
    assert issues.forge_assignees(tmp_path, "52") == []
    write_json(
        tmp_path / FORGE_ISSUES,
        {
            **json.loads((tmp_path / FORGE_ISSUES).read_text()),
            "skip_labels": ["bug"],
        },
    )
    pool = issues.with_forge(tmp_path, ledger)
    assert issues.unclaimed(pool) == ["51", "52", "54"]


def test_a_stale_or_missing_forge_cache_adds_nothing(tmp_path):
    ledger = {"revision": 1, "issues": {}}
    assert issues.with_forge(tmp_path, ledger) is ledger
    cached(tmp_path, {"50": "New work"}, age=FORGE_STALE + 1)
    assert issues.with_forge(tmp_path, ledger) is ledger
    (tmp_path / FORGE_ISSUES).write_text("not json")
    assert issues.with_forge(tmp_path, ledger) is ledger


def test_a_forge_only_issue_is_a_pull_offer_and_an_idle_lead(
    bridge, repo, paired
):
    directory = Path(paired["lanes"]["claude"]).parent
    store.initialize(bridge.home)
    store.register(bridge.home, paired["root"], "claude")
    alive(directory, "claude")
    cached(directory, {"88": "Brand new"}, age=FORGE_STALE + 1)
    swept(bridge, directory)
    assert offer_for(directory, "claude") is None
    cached(directory, {"88": "Brand new"})
    manifest, config = swept(bridge, directory)
    offer = offer_for(directory, "claude")
    assert offer["kind"] == "pull"
    assert offer["lead"] == "88"
    assert "#88" in offer["text"]
    assert "before exploring or editing" in offer["text"]
    assert "88" not in issues.snapshot(directory)["issues"]
    pool = issues.with_forge(
        directory, {"revision": 1, "issues": {"2": ready("claude")}}
    )
    leads = supervision._idle_leads(
        bridge.home, directory, manifest, "claude", pool, 0
    )
    assert leads["next"] == "88"


def test_issue_next_ranks_a_forge_only_issue_and_says_claim_first(
    bridge, repo, paired
):
    lane = Path(paired["lanes"]["claude"])
    cached(lane.parent, {"88": "Brand new"})
    ranked = bridge.issue_next(lane)
    assert [record["issue"] for record in ranked["candidates"]] == ["88"]
    assert ranked["candidates"][0]["title"] == "Brand new"
    closing = recommend.render(ranked).splitlines()[-1]
    assert closing.startswith(
        "Claim one with agent-parley issue claim before exploring or editing"
    )


def test_three_waiting_lanes_get_three_distinct_next_leads(
    bridge, repo, paired
):
    directory = three_lanes(bridge, repo, paired)
    write_json(
        directory / "issues.json",
        {
            "revision": 1,
            "issues": {
                "1": ready("claude"),
                "2": ready("codex"),
                "3": ready("copilot"),
            },
        },
    )
    cached(directory, {"7": "Seven", "8": "Eight", "9": "Nine"})
    manifest, config = swept(bridge, directory)
    offers = {
        name: offer_for(directory, name)
        for name in ("claude", "codex", "copilot")
    }
    assert {name: offer["lead"] for name, offer in offers.items()} == {
        "claude": "7",
        "codex": "8",
        "copilot": "9",
    }
    for name, offer in offers.items():
        assert offer["kind"] == "continue"
        assert (
            f"claim #{offer['lead']}, the top agent-parley issue next "
            f"candidate, with agent-parley issue claim {offer['lead']} "
            "before exploring or editing it" in offer["text"]
        )
        record = supervision.published_work(directory, name)
        assert supervision._work_backlog(
            bridge.home, directory, manifest, name, config, record
        )


def delivered(directory, name):
    """Marks the lane's published offer delivered and returns the record."""
    record = supervision.published_work(directory, name)
    record["dispatch"] = {**record["dispatch"], "delivered": True}
    record["delivered"] = [record["offer"]["id"]]
    write_json(directory / f"{name}-work.json", record)
    return supervision.published_work(directory, name)


def test_a_delivered_continue_offer_naming_a_free_lead_is_kept(
    bridge, repo, paired
):
    directory = Path(paired["lanes"]["claude"]).parent
    store.initialize(bridge.home)
    store.register(bridge.home, paired["root"], "claude")
    alive(directory, "claude")
    write_json(
        directory / "issues.json",
        {"revision": 1, "issues": {"1": ready("claude")}},
    )
    cached(directory, {"7": "Seven"})
    manifest, config = swept(bridge, directory)
    offer = offer_for(directory, "claude")
    assert offer["kind"] == "continue"
    assert offer["lead"] == "7"
    record = delivered(directory, "claude")
    assert supervision._work_backlog(
        bridge.home, directory, manifest, "claude", config, record
    ) == (f"work:{offer['id']}:{offer['progress']}", offer)


def test_a_delivered_waiting_only_offer_stays_suppressed(bridge, repo, paired):
    directory = Path(paired["lanes"]["claude"]).parent
    store.initialize(bridge.home)
    store.register(bridge.home, paired["root"], "claude")
    alive(directory, "claude")
    write_json(
        directory / "issues.json",
        {"revision": 1, "issues": {"1": ready("claude")}},
    )
    manifest, config = swept(bridge, directory)
    offer = offer_for(directory, "claude")
    assert offer["kind"] == "continue"
    assert "lead" not in offer
    record = delivered(directory, "claude")
    assert (
        supervision._work_backlog(
            bridge.home, directory, manifest, "claude", config, record
        )
        is None
    )


def test_three_idle_lanes_get_three_distinct_first_pull_picks(
    bridge, repo, paired
):
    directory = three_lanes(bridge, repo, paired)
    cached(directory, {"7": "Seven", "8": "Eight", "9": "Nine"})
    swept(bridge, directory)
    offers = [
        offer_for(directory, name) for name in ("claude", "codex", "copilot")
    ]
    assert [offer["lead"] for offer in offers] == ["7", "8", "9"]
    assert [offer["issues"][0] for offer in offers] == ["7", "8", "9"]


def test_issue_next_demotes_the_lead_another_lane_was_named(
    bridge, repo, paired
):
    lane = Path(paired["lanes"]["claude"])
    directory = lane.parent
    cached(directory, {"43": "First", "44": "Second"})
    write_json(
        directory / "codex-work.json",
        {"offer": {"kind": "pull", "issues": ["43"], "lead": "43"}},
    )
    write_json(
        directory / "claude-work.json",
        {"offer": {"kind": "pull", "issues": ["44"], "lead": "44"}},
    )
    ranked = bridge.issue_next(lane)
    assert [record["issue"] for record in ranked["candidates"]] == [
        "44",
        "43",
    ]
    demoted = ranked["candidates"][-1]
    assert demoted["named_to"] == "codex"
    assert "already named as codex's next work" in demoted["reasons"]
    assert supervision.named_leads(
        directory, json.loads((directory / "project.json").read_text()), "x"
    ) == {"43": "codex", "44": "claude"}


def test_a_fit_lane_after_an_unfit_one_is_pointed_at_the_best_work(
    bridge, repo, paired, monkeypatch
):
    directory = Path(paired["lanes"]["claude"]).parent
    store.initialize(bridge.home)
    for name in ("claude", "codex"):
        store.register(bridge.home, paired["root"], name)
        alive(directory, name)
    measured = supervision.fit

    def session_down(home, directory, manifest, name, *args):
        """Reads claude's session as not running and leaves codex fit."""
        result = measured(home, directory, manifest, name, *args)
        if name != "claude":
            return result
        checks = {**result["checks"], "session": False}
        return {**result, "fit": False, "checks": checks}

    monkeypatch.setattr(supervision, "fit", session_down)
    write_json(
        directory / "issues.json",
        {"revision": 1, "issues": {"1": ready("claude"), "2": ready("codex")}},
    )
    cached(directory, {"7": "Seven", "8": "Eight"})
    swept(bridge, directory)
    unfit = offer_for(directory, "claude")
    assert supervision.published_work(directory, "claude")["fit"] is False
    assert unfit["kind"] == "continue" and "lead" not in unfit
    assert offer_for(directory, "codex")["lead"] == "7"


def test_a_lane_that_can_only_wait_on_an_operator_is_never_woken(
    bridge, repo, paired
):
    directory = Path(paired["lanes"]["claude"]).parent
    store.initialize(bridge.home)
    store.register(bridge.home, paired["root"], "claude")
    alive(directory, "claude")
    blocked = ready("claude")
    blocked["execution"] = {
        **blocked["execution"],
        "state": "blocked",
        "resume_when": {"kind": "external", "detail": "100 trades"},
    }
    write_json(
        directory / "issues.json", {"revision": 1, "issues": {"1": blocked}}
    )
    manifest, config = swept(bridge, directory)
    offer = offer_for(directory, "claude")
    assert offer["kind"] == "continue"
    assert "End the turn" in offer["text"]
    record = supervision.published_work(directory, "claude")
    assert not record["dispatch"].get("delivered")
    assert (
        supervision._work_backlog(
            bridge.home, directory, manifest, "claude", config, record
        )
        is None
    )


def test_next_issues_and_issue_next_rank_published_leads_alike(
    bridge, repo, paired
):
    lane = Path(paired["lanes"]["claude"])
    directory = lane.parent
    cached(directory, {"2": "Two", "3": "Three", "4": "Four"})
    write_json(
        directory / "codex-work.json",
        {"offer": {"kind": "pull", "issues": ["2"], "lead": "2"}},
    )
    write_json(
        directory / "claude-work.json",
        {"offer": {"kind": "pull", "issues": ["3"], "lead": "3"}},
    )
    store.initialize(bridge.home)
    token = store.register(bridge.home, paired["root"], "claude")
    actor = store.authenticate(bridge.home, token["registration_token"])
    served = store.call(bridge.home, actor, "next_issues", {})
    command = bridge.issue_next(lane)
    order = [record["issue"] for record in served["candidates"]]
    assert order == ["3", "4", "2"]
    assert order == [record["issue"] for record in command["candidates"]]
    first, *_, last = served["candidates"]
    assert "named as this lane's next work" in first["reasons"]
    assert "already named as codex's next work" in last["reasons"]
