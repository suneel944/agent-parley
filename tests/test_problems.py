"""Checks the triage view that lists every condition needing an operator."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent_parley import cli, dashboard, issues, problems, protocol, store
from agent_parley.process import start_ticks
from agent_parley.state import write_json


def alive(directory, name, **extra):
    """Records a live but long-quiet session process for one lane."""
    write_json(
        directory / f"{name}-activity.json",
        {
            "session_pid": os.getpid(),
            "session_ticks": start_ticks(os.getpid()),
            "activity": "idle",
            "updated": 0,
            **extra,
        },
    )


def deliver(bridge, repo, paired, *, ack=False, aged=0):
    """Delivers one operator message and ages it in the store."""
    store.initialize(bridge.home)
    store.register(bridge.home, paired["root"], "claude")
    delivered = bridge.say(repo, "claude", "Answer this", ack=ack)
    if aged:
        with store.connect(bridge.home, write=True) as db:
            db.execute(
                "UPDATE messages SET created_ts=datetime('now',?) WHERE id=?",
                (f"-{aged} seconds", delivered["id"]),
            )
    return delivered


@pytest.fixture
def served(monkeypatch):
    """Reports the coordination service as up without starting it."""
    original = cli.Bridge.status_snapshot

    def ready(self):
        report = original(self)
        report["server"]["ready"] = True
        return report

    monkeypatch.setattr(cli.Bridge, "status_snapshot", ready)


def run(monkeypatch, capsys, bridge, *extra):
    """Runs the problems command and returns its exit code and output."""
    monkeypatch.setattr(
        sys,
        "argv",
        ["agent-parley", "--home", str(bridge.home), "problems", *extra],
    )
    code = cli.main()
    return code, capsys.readouterr().out


def rows(bridge, condition=None, **extra):
    """Derives the rows, optionally narrowed to one condition."""
    found = bridge.problems(**extra)
    return [row for row in found if condition in (None, row["condition"])]


def test_a_clear_estate_prints_one_line_and_exits_zero(
    bridge, repo, paired, served, monkeypatch, capsys
):
    code, out = run(monkeypatch, capsys, bridge)
    assert code == 0
    assert out.splitlines() == [problems.lines([])[0]]
    assert out.startswith("No problems")


def test_a_stopped_service_is_a_row_that_exits_one(
    bridge, repo, paired, monkeypatch, capsys
):
    code, out = run(monkeypatch, capsys, bridge)
    assert code == 1
    assert len(out.splitlines()) == 1
    assert "service" in out and "agent-parley up" in out
    assert rows(bridge, problems.SERVICE)[0]["command"] == "agent-parley up"


def test_a_store_behind_the_build_names_the_migration(
    bridge, repo, paired, served, monkeypatch
):
    monkeypatch.setattr(store, "schema_version", lambda home: 1)
    [row] = rows(bridge, problems.STORE)
    assert row["command"] == protocol.MIGRATE
    assert row["seconds"] is None
    assert bridge.problems()[0] == row


def test_a_stalled_lane_names_the_waiting_item_and_the_resume(
    bridge, repo, paired, served
):
    directory = bridge.project(repo)[1]
    alive(directory, "claude")
    deliver(bridge, repo, paired, aged=1800)
    [row] = rows(bridge, problems.STALLED)
    assert row["participant"] == "claude"
    assert row["detail"].startswith("idle; message")
    assert row["seconds"] >= 1800
    assert row["command"] == (
        f"agent-parley run claude --resume --repo {paired['root']}"
    )
    assert not rows(bridge, problems.INACTIVE)


def test_a_live_lane_past_the_inactive_threshold_is_a_row(
    bridge, repo, paired, served
):
    alive(bridge.project(repo)[1], "claude")
    [row] = rows(bridge, problems.INACTIVE)
    assert row["participant"] == "claude"
    assert row["command"].startswith("agent-parley run claude --resume")
    assert [r["participant"] for r in rows(bridge)] == ["claude"]


def test_an_overdue_claim_names_the_release(bridge, repo, paired, served):
    directory = bridge.project(repo)[1]
    bridge.issue(paired["lanes"]["claude"], "claim", "42", within=60)
    state = issues.snapshot(directory)
    state["issues"]["42"]["deadline"] = time.time() - 300
    state["revision"] += 1
    write_json(directory / "issues.json", state)
    [row] = rows(bridge, problems.OVERDUE)
    assert row["participant"] == "claude"
    assert row["seconds"] >= 300
    assert row["command"] == (
        f"agent-parley issue release 42 --repo {paired['root']}"
    )


def test_an_unanswered_offer_names_its_recipient_and_the_cancel(
    bridge, repo, paired, served
):
    bridge.issue(paired["lanes"]["claude"], "claim", "1")
    bridge.issue(
        paired["lanes"]["claude"], "offer", "1", to="codex", summary="Take it"
    )
    [row] = rows(bridge, problems.OFFER)
    assert row["participant"] == "codex"
    assert "offered to codex by peer" in row["detail"]
    assert row["command"] == (
        f"agent-parley issue cancel 1 --repo {paired['root']}"
    )
    bridge.issue(paired["lanes"]["claude"], "cancel", "1")
    bridge.issue_assign(repo, "2", "codex")
    [row] = rows(bridge, problems.OFFER)
    assert row["command"] == (
        f"agent-parley issue assign 2 codex --unassign --repo {paired['root']}"
    )


def test_a_message_awaiting_acknowledgement_past_the_age_is_a_row(
    bridge, repo, paired, served
):
    directory = bridge.project(repo)[1]
    alive(directory, "claude")
    delivered = deliver(bridge, repo, paired, ack=True, aged=1800)
    assert not rows(bridge, problems.ACK, ack_after=86400)
    [row] = rows(bridge, problems.ACK, ack_after=600)
    assert f"message {delivered['id']} from operator" in row["detail"]
    assert row["seconds"] >= 1800
    assert row["command"].startswith("agent-parley run claude --resume")


def test_a_drifted_lane_names_the_restore(bridge, repo, paired, served):
    lane = paired["lanes"]["claude"]
    subprocess.run(
        ["git", "-C", str(lane), "checkout", "-q", "-b", "elsewhere"],
        check=True,
    )
    [row] = rows(bridge, problems.DRIFT)
    assert "on elsewhere instead of" in row["detail"]
    assert row["command"] == (
        f"agent-parley participant restore claude --repo {paired['root']}"
    )
    assert not rows(bridge, problems.DIRTY)


def test_a_quiet_dirty_worktree_names_the_retire(bridge, repo, paired, served):
    (Path(paired["lanes"]["claude"]) / "draft.txt").write_text("unfinished\n")
    [row] = rows(bridge, problems.DIRTY)
    assert row["participant"] == "claude"
    assert row["command"] == (
        f"agent-parley participant retire claude --repo {paired['root']}"
    )


def test_a_lane_over_its_budget_names_the_budget_command(
    bridge, repo, paired, served
):
    directory = bridge.project(repo)[1]
    alive(directory, "claude", session_started=time.time() - 7200)
    bridge.budget(repo, "participant", "claude", {"hours": 1})
    [row] = rows(bridge, problems.BUDGET)
    assert row["detail"].startswith("over budget; hours 2h of 1h")
    assert row["command"] == (
        f"agent-parley participant budget claude --repo {paired['root']}"
    )


def test_rows_are_ordered_longest_held_first_behind_the_store_and_service(
    bridge, repo, paired, monkeypatch
):
    directory = bridge.project(repo)[1]
    alive(directory, "claude")
    alive(directory, "codex", updated=time.time() - 400)
    bridge.issue(paired["lanes"]["claude"], "claim", "42", within=60)
    state = issues.snapshot(directory)
    state["issues"]["42"]["deadline"] = time.time() - 900
    state["revision"] += 1
    write_json(directory / "issues.json", state)
    monkeypatch.setattr(store, "schema_version", lambda home: 1)
    found = bridge.problems()
    assert [row["condition"] for row in found] == [
        problems.STORE,
        problems.SERVICE,
        problems.INACTIVE,
        problems.OVERDUE,
        problems.INACTIVE,
    ]
    assert [row["participant"] for row in found[2:]] == [
        "claude",
        "claude",
        "codex",
    ]
    ages = [row["seconds"] for row in found[2:]]
    assert ages == sorted(ages, reverse=True)


def test_the_json_document_carries_every_row(
    bridge, repo, paired, monkeypatch, capsys
):
    alive(bridge.project(repo)[1], "claude")
    code, out = run(monkeypatch, capsys, bridge, "--json")
    assert code == 1
    document = json.loads(out)
    assert document["kind"] == "problems"
    assert document["count"] == 2
    assert [row["condition"] for row in document["problems"]] == [
        problems.SERVICE,
        problems.INACTIVE,
    ]
    assert set(document["problems"][1]) == {
        "project",
        "participant",
        "condition",
        "detail",
        "seconds",
        "command",
    }


def test_every_printed_row_names_the_lane_the_age_and_the_command(
    bridge, repo, paired, served, monkeypatch, capsys
):
    alive(bridge.project(repo)[1], "claude")
    code, out = run(monkeypatch, capsys, bridge)
    assert code == 1
    [line] = out.splitlines()
    assert "claude" in line
    assert problems.INACTIVE in line
    assert line.endswith(f"--repo {paired['root']}")
    assert line.split()[0].endswith("h")


class Screen:
    """Replays scripted keys and records what each frame drew."""

    def __init__(self, keys):
        self.keys = list(keys)
        self.cells = []
        self.frames = []

    def getmaxyx(self):
        return (20, 200)

    def timeout(self, value):
        self.wait = value

    def keypad(self, value):
        self.arrows = value

    def erase(self):
        self.cells = []

    def clear(self):
        self.cells = []

    def refresh(self):
        self.frames.append(list(self.cells))

    def addnstr(self, row, column, value, width, attribute=0):
        self.cells.append(value[:width])

    def getch(self):
        return self.keys.pop(0) if self.keys else ord("q")


def test_top_shows_the_problem_rows_in_place_on_p(monkeypatch, tmp_path):
    import curses

    view = dashboard.select(
        {
            "running": True,
            "home": str(tmp_path),
            "projects": [],
            "totals": {},
            "providers": [],
            "window": 0.0,
        }
    )
    monkeypatch.setattr(dashboard, "collect", lambda *a, **k: view)
    monkeypatch.setattr(curses, "has_colors", lambda: False)
    screen = Screen([ord("P"), ord(" "), ord("q")])
    shaping = {
        "sort": "",
        "reverse": False,
        "projects": (),
        "participants": (),
        "columns": (),
    }
    listed = problems.lines(
        [
            {
                "project": "/repo",
                "participant": "claude",
                "condition": problems.STALLED,
                "detail": "idle; message 3 from operator waiting 900s",
                "seconds": 900,
                "command": "agent-parley run claude --resume --repo /repo",
            }
        ]
    )
    dashboard._loop(
        screen,
        tmp_path,
        lambda: True,
        0.1,
        (),
        0.0,
        shaping,
        True,
        lambda: listed,
    )
    overlay = screen.frames[1]
    assert overlay[0] == "agent-parley problems"
    assert listed[0] in overlay
    assert any("P" in key for key, _ in dashboard.KEYS)
