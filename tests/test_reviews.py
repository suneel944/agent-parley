"""Checks builder-reviewer review rounds requested by mail to ``@review``."""

import sys

import pytest

from agent_parley import cli, issues, reviews, roster, store, supervision
from agent_parley.children import REPORT_BYTES
from agent_parley.state import BridgeError

NAMES = ("claude", "claude-b", "codex")


@pytest.fixture
def lanes(bridge, repo, monkeypatch):
    """Registers two claude lanes and a codex lane, all running."""
    bridge.add_participant(repo, "claude", "claude")
    bridge.add_participant(repo, "claude-b", "claude")
    manifest = bridge.add_participant(repo, "codex", "codex")
    store.initialize(bridge.home)
    actors = {}
    for name in NAMES:
        token = store.register(bridge.home, manifest["root"], name)
        actors[name] = store.authenticate(
            bridge.home, token["registration_token"]
        )
    monkeypatch.setattr(
        supervision,
        "lane_state",
        lambda *args, **kwargs: {"process_alive": True},
    )
    directory = roster.locate(bridge.home, manifest["root"])
    claim(directory, "claude", "7")
    return {"directory": directory, "actors": actors}


def claim(directory, owner, number):
    """Records one claim in the issue ledger."""
    issues.change(
        directory,
        owner,
        "claim",
        number,
        participants=set(NAMES),
        title=f"Issue {number}",
    )


def send(bridge, lanes, sender, to, subject, body, key):
    """Sends one mail through the store as a lane."""
    return store.call(
        bridge.home,
        lanes["actors"][sender],
        "send_message",
        {"to": to, "subject": subject, "body_md": body, "idempotency_key": key},
    )


def ask(bridge, lanes, key, builder="claude"):
    """Asks for a review round on the builder's branch."""
    return send(
        bridge,
        lanes,
        builder,
        [reviews.ADDRESS, "agent_parley/retries.py"],
        "feat/7-retry",
        "Check the retry bound.",
        key,
    )


def inbox(bridge, lanes, name):
    """Reads one lane's mail with bodies."""
    return store.call(
        bridge.home,
        lanes["actors"][name],
        "fetch_inbox",
        {"include_bodies": True},
    )["messages"]


def test_rounds_return_findings_to_the_builder_up_to_the_cap(
    bridge, repo, lanes, capsys, monkeypatch
):
    for number in range(1, reviews.MAX_ROUNDS + 1):
        asked = ask(bridge, lanes, f"ask-{number}")["review"]
        assert asked["round"] == number
        assert asked["reviewer"] == "codex"
        assert asked["builder"] == "claude"
        offer = inbox(bridge, lanes, "codex")[-1]
        assert offer["subject"].startswith(f"Review round {asked['id']}")
        answered = send(
            bridge,
            lanes,
            "codex",
            [reviews.ADDRESS, asked["id"]],
            "findings",
            "- retry bound is off by one\n- missing test\n",
            f"found-{number}",
        )["review"]
        assert answered["state"] == reviews.RETURNED
        assert answered["findings"] == 2
        delivered = inbox(bridge, lanes, "claude")[-1]
        assert "2 from codex" in delivered["subject"]
        record = issues.snapshot(lanes["directory"])["issues"]["7"]
        assert record["owner"] == "claude"
    with pytest.raises(BridgeError, match="open the pull request"):
        ask(bridge, lanes, "ask-4")
    assert len(reviews.rounds(lanes["directory"], issue="7")) == 3
    monkeypatch.undo()
    for command in (("issue", "show", "7"), ("participant", "show", "codex")):
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "agent-parley",
                "--home",
                str(bridge.home),
                *command,
                "--repo",
                str(repo),
            ],
        )
        capsys.readouterr()
        assert cli.main() == 0
        shown = capsys.readouterr().out
        assert "round 3/3, claude reviewed by codex" in shown
        assert "2 findings" in shown


def test_one_open_round_per_claim_and_only_its_reviewer_answers(bridge, lanes):
    asked = ask(bridge, lanes, "ask-1")["review"]
    again = ask(bridge, lanes, "ask-1")["review"]
    assert again["id"] == asked["id"]
    with pytest.raises(BridgeError, match="still open"):
        ask(bridge, lanes, "ask-2")
    with pytest.raises(BridgeError, match="offered to codex"):
        send(
            bridge,
            lanes,
            "claude-b",
            [reviews.ADDRESS, asked["id"]],
            "findings",
            "- nothing",
            "wrong-1",
        )
    with pytest.raises(BridgeError, match="exactly one claim"):
        ask(bridge, lanes, "ask-3", builder="claude-b")


def test_findings_are_capped_like_a_child_report(bridge, lanes):
    asked = ask(bridge, lanes, "ask-1")["review"]
    send(
        bridge,
        lanes,
        "codex",
        [reviews.ADDRESS, asked["id"]],
        "findings",
        "- " + "x" * (REPORT_BYTES * 2),
        "found-1",
    )
    delivered = inbox(bridge, lanes, "claude")[-1]
    assert len(delivered["body_md"].encode()) <= REPORT_BYTES


def test_a_same_provider_lane_reviews_when_no_other_is_idle(bridge, lanes):
    claim(lanes["directory"], "codex", "8")
    assert ask(bridge, lanes, "ask-1")["review"]["reviewer"] == "claude-b"


def test_no_idle_lane_records_nothing(bridge, lanes):
    claim(lanes["directory"], "codex", "8")
    claim(lanes["directory"], "claude-b", "9")
    with pytest.raises(BridgeError, match="No idle lane"):
        ask(bridge, lanes, "ask-1")
    assert reviews.rounds(lanes["directory"]) == []


def test_count_reads_bullets_and_numbered_lines():
    assert reviews.count("- a\n* b\n+ c\n1. d\nprose\n12. e") == 5
