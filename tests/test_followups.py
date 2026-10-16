"""Checks lane follow-up suggestions, their acceptance and their transport."""

import contextlib
import json
import sys
import threading
import time

import httpx
import pytest

from agent_parley import (
    cli,
    decisions,
    followups,
    forge,
    inbound,
    issues,
    roster,
    server,
    store,
    supervision,
)
from agent_parley.state import BridgeError, write_json
from agent_parley.status import FORGE_ISSUES


@pytest.fixture
def lane(bridge, paired):
    """Registers the codex participant and returns its directory and actor."""
    store.initialize(bridge.home)
    token = store.register(bridge.home, paired["root"], "codex")
    actor = store.authenticate(bridge.home, token["registration_token"])
    directory = roster.locate(bridge.home, paired["root"])
    return {
        "directory": directory,
        "actor": actor,
        "token": token["registration_token"],
        "root": paired["root"],
    }


@pytest.fixture
def filed(monkeypatch):
    """Stands in for the GitHub forge and records every filing."""
    calls = []

    def file_issue(repo, title, body, milestone=""):
        calls.append({"title": title, "body": body, "milestone": milestone})
        return str(900 + len(calls))

    monkeypatch.setattr(forge, "file_issue", file_issue)
    monkeypatch.setattr(forge, "active_milestone", lambda repo: "0.21.0")
    return calls


def claimed(lane, owner, number, title):
    """Records one claim with a title in the issue ledger."""
    issues.change(
        lane["directory"],
        owner,
        "claim",
        number,
        participants={"claude", "codex"},
        title=title,
    )


def suggestion(**overrides):
    """Builds valid suggestion arguments."""
    return {
        "title": "Retry the flaky forge read in status",
        "why": "The status forge read fails once a day on a timeout.",
        "paths": ["agent_parley/forge.py"],
        **overrides,
    }


def test_a_suggestion_is_recorded_without_filing_or_offering(
    bridge, lane, filed
):
    result = store.call(
        bridge.home, lane["actor"], followups.TOOL, suggestion()
    )
    assert result["id"] == "f1"
    assert result["state"] == followups.PENDING
    assert result["repeated"] is False
    assert "Nothing was filed" in result["note"]
    assert filed == []
    assert issues.snapshot(lane["directory"])["issues"] == {}
    rows = followups.pending(lane["directory"])
    assert [row["id"] for row in rows] == ["f1"]
    assert rows[0]["lane"] == "codex"
    assert rows[0]["paths"] == ["agent_parley/forge.py"]


def test_a_repeated_suggestion_is_recorded_once(bridge, lane):
    first = store.call(bridge.home, lane["actor"], followups.TOOL, suggestion())
    again = store.call(bridge.home, lane["actor"], followups.TOOL, suggestion())
    assert again["id"] == first["id"]
    assert again["repeated"] is True
    assert len(followups.pending(lane["directory"])) == 1


def test_invalid_suggestions_are_refused(bridge, lane):
    for invalid in (
        suggestion(title=""),
        suggestion(why="   "),
        suggestion(title="x" * (followups.MAX_TITLE + 1)),
        suggestion(paths=["a"] * (followups.MAX_PATHS + 1)),
        suggestion(paths="agent_parley/forge.py"),
    ):
        with pytest.raises(BridgeError):
            store.call(bridge.home, lane["actor"], followups.TOOL, invalid)
    assert followups.pending(lane["directory"]) == []


def test_the_pending_bound_refuses_a_flood(bridge, lane, monkeypatch):
    monkeypatch.setattr(followups, "MAX_PENDING", 2)
    for index in range(2):
        followups.suggest(
            lane["directory"], "codex", f"Work item {index}", "Because."
        )
    with pytest.raises(BridgeError, match="already has 2"):
        followups.suggest(lane["directory"], "codex", "Third", "Because.")


def test_an_unaccepted_suggestion_expires(lane):
    created = followups.suggest(
        lane["directory"], "codex", "Old idea", "Stale.", now=1000.0
    )
    later = created["expires"] + 1
    assert followups.pending(lane["directory"], now=1001.0)
    assert followups.pending(lane["directory"], now=later) == []
    followups.suggest(
        lane["directory"], "codex", "New idea", "Fresh.", now=later
    )
    record = json.loads((lane["directory"] / followups.RECORD_NAME).read_text())
    assert [entry["title"] for entry in record["suggestions"].values()] == [
        "New idea"
    ]


def test_a_suggestion_matching_a_claim_or_open_issue_shows_duplicates(
    bridge, lane
):
    claimed(lane, "claude", "41", "Retry forge reads")
    write_json(
        lane["directory"] / FORGE_ISSUES,
        {
            "issues": {
                "52": {"title": "Status forge read timeout", "labels": []},
                "60": {"title": "Unrelated docs typo", "labels": []},
            },
            "read_at": time.time(),
        },
    )
    result = store.call(
        bridge.home, lane["actor"], followups.TOOL, suggestion()
    )
    assert {item["issue"] for item in result["duplicates"]} == {"41", "52"}
    held = next(item for item in result["duplicates"] if item["issue"] == "41")
    assert held["owner"] == "claude"
    rows = followups.pending(lane["directory"])
    assert rows[0]["duplicates"] == result["duplicates"]


def test_a_stale_forge_cache_is_not_matched(lane):
    write_json(
        lane["directory"] / FORGE_ISSUES,
        {
            "issues": {"52": "Status forge read timeout"},
            "read_at": 0,
        },
    )
    assert followups.duplicates(lane["directory"], "Status forge read") == []


def test_accept_files_in_the_active_milestone_and_offers_an_idle_lane(
    bridge, repo, lane, filed, monkeypatch
):
    monkeypatch.setattr(
        supervision,
        "lane_state",
        lambda activity: {"process_alive": True},
    )
    claimed(lane, "claude", "41", "Something else")
    created = followups.suggest(
        lane["directory"], "codex", "Cache forge reads", "Slow.", ["a.py"]
    )
    accepted = bridge.followup_accept(repo, created["id"])
    assert accepted["issue"] == "901"
    assert accepted["milestone"] == "0.21.0"
    assert accepted["offered_to"] == "codex"
    assert accepted["offer_id"]
    assert filed[0]["title"] == "Cache forge reads"
    assert filed[0]["milestone"] == "0.21.0"
    assert "`a.py`" in filed[0]["body"]
    assert "codex" not in filed[0]["body"]
    record = issues.snapshot(lane["directory"])["issues"]["901"]
    assert record["offer"]["to"] == "codex"
    assert followups.pending(lane["directory"]) == []
    again = bridge.followup_accept(repo, created["id"])
    assert again["repeated"] is True
    assert again["issue"] == "901"
    assert len(filed) == 1


def test_accept_with_no_idle_lane_leaves_the_issue_unclaimed(
    bridge, repo, lane, filed
):
    created = followups.suggest(lane["directory"], "codex", "Idea", "Why.")
    accepted = bridge.followup_accept(repo, created["id"], milestone="0.22.0")
    assert accepted["offered_to"] == ""
    assert filed[0]["milestone"] == "0.22.0"
    assert "901" not in issues.snapshot(lane["directory"])["issues"]
    assert "left unclaimed" in followups.accepted_text(accepted)


def test_accept_to_a_named_lane_and_refusals(bridge, repo, lane, filed):
    created = followups.suggest(lane["directory"], "codex", "Idea", "Why.")
    with pytest.raises(BridgeError, match="not a participant"):
        bridge.followup_accept(repo, created["id"], to="nobody")
    with pytest.raises(BridgeError, match="No pending suggestion"):
        bridge.followup_accept(repo, "f99")
    accepted = bridge.followup_accept(repo, created["id"], to="claude")
    assert accepted["offered_to"] == "claude"


def test_a_forge_refusal_keeps_the_suggestion_pending(
    bridge, repo, lane, monkeypatch
):
    monkeypatch.setattr(forge, "file_issue", lambda *args: None)
    monkeypatch.setattr(forge, "active_milestone", lambda repo: None)
    created = followups.suggest(lane["directory"], "codex", "Idea", "Why.")
    with pytest.raises(BridgeError, match="stays pending"):
        bridge.followup_accept(repo, created["id"])
    assert [row["id"] for row in followups.pending(lane["directory"])] == [
        created["id"]
    ]


def test_the_telegram_accept_button_files_through_the_decision(
    bridge, repo, lane, filed
):
    result = store.call(
        bridge.home, lane["actor"], followups.TOOL, suggestion()
    )
    opened = decisions.list_open(lane["directory"])
    assert [record["kind"] for record in opened] == [followups.KIND]
    name = opened[0]["id"]
    assert followups.by_decision(lane["directory"], name) == result["id"]
    state, _ = inbound.decide(bridge.home, name, followups.ACCEPT, "telegram:1")
    assert state == inbound.CONFIRMING
    assert filed == []
    state, answered = inbound.decide(
        bridge.home, name, followups.ACCEPT, "telegram:1", confirmed=True
    )
    assert state == inbound.ANSWERED
    assert answered["answer"] == followups.ACCEPT
    assert len(filed) == 1
    assert followups.pending(lane["directory"]) == []


def test_followup_commands_list_and_accept(
    bridge, repo, lane, filed, capsys, monkeypatch
):
    followups.suggest(lane["directory"], "codex", "Idea", "Why.")

    def run(*arguments):
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "agent-parley",
                "--home",
                str(bridge.home),
                "followup",
                *arguments,
                "--repo",
                str(repo),
            ],
        )
        assert cli.main() == 0
        return capsys.readouterr().out

    assert "f1  codex: Idea" in run("list")
    document = json.loads(run("list", "--json"))
    assert document["kind"] == "followup_list"
    assert [row["id"] for row in document["suggestions"]] == ["f1"]
    assert "f1 filed as #901 in milestone 0.21.0" in run("accept", "f1")
    assert "No pending" in run("list")


def test_render_names_duplicates_and_paths():
    text = followups.render(
        [
            {
                "id": "f2",
                "lane": "codex",
                "title": "Idea",
                "why": "Why.",
                "paths": ["a.py"],
                "expires": 0,
                "duplicates": [
                    {"issue": "7", "title": "Same idea", "owner": "claude"}
                ],
            }
        ]
    )
    assert "duplicate of #7 Same idea, held by claude" in text
    assert "paths: a.py" in text


@contextlib.contextmanager
def serving(bridge):
    """Runs the real service on an ephemeral port in this process."""
    with server.Server(bridge.home, {"token": "health", "port": 0}) as running:
        thread = threading.Thread(target=running.serve_forever, daemon=True)
        thread.start()
        try:
            yield running
        finally:
            running.shutdown()
            thread.join(timeout=5)


def rpc(running, token, method, params):
    """Sends one JSON-RPC request over the authenticated local transport."""
    return httpx.post(
        f"http://127.0.0.1:{running.server_port}/mcp/",
        headers={"Authorization": f"Bearer {token}"},
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        timeout=30,
        trust_env=False,
    ).json()


def test_the_tool_is_served_over_real_mcp_transport(bridge, lane, filed):
    with serving(bridge) as running:
        listed = rpc(running, lane["token"], "tools/list", {})
        called = rpc(
            running,
            lane["token"],
            "tools/call",
            {"name": followups.TOOL, "arguments": suggestion()},
        )
        refused = rpc(
            running,
            lane["token"],
            "tools/call",
            {"name": followups.TOOL, "arguments": {"title": "Only a title"}},
        )
    tools = {tool["name"]: tool for tool in listed["result"]["tools"]}
    assert tools[followups.TOOL]["inputSchema"]["required"] == [
        "title",
        "why",
    ]
    assert "readOnlyHint" not in tools[followups.TOOL]["annotations"]
    result = json.loads(called["result"]["content"][0]["text"])
    assert result["id"] == "f1"
    assert result["state"] == followups.PENDING
    assert filed == []
    assert issues.snapshot(lane["directory"])["issues"] == {}
    assert "error" in refused or refused["result"].get("isError")
