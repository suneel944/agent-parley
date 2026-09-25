"""Checks the triage view that lists every condition needing an operator."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent_parley import (
    checkpoints,
    cli,
    completion,
    dashboard,
    issues,
    problems,
    protocol,
    store,
    supervision,
)
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


def stopped(directory, name):
    """Records a lane whose session process is gone."""
    write_json(
        directory / f"{name}-activity.json",
        {"activity": "stopped", "updated": 0},
    )


def deliver(bridge, repo, paired, *, ack=False, aged=0, count=1):
    """Delivers operator messages and ages them in the store."""
    store.initialize(bridge.home)
    store.register(bridge.home, paired["root"], "claude")
    sent = [
        bridge.say(repo, "claude", f"Answer this {number}", ack=ack)
        for number in range(count)
    ]
    if aged:
        with store.connect(bridge.home, write=True) as db:
            db.executemany(
                "UPDATE messages SET created_ts=datetime('now',?) WHERE id=?",
                [(f"-{aged} seconds", item["id"]) for item in sent],
            )
    return sent[0]


def unwoken(bridge):
    """Turns the coordination service's wake loop off for the estate."""
    write_json(bridge.home / "supervision.json", {"wake": False})


def refuse(bridge, directory, name, result, attempts=0):
    """Records one wake refusal in a lane's state."""
    root = json.loads((directory / "project.json").read_text())["root"]
    supervision.store_wake(
        bridge.home,
        directory,
        root,
        name,
        {
            "at": time.time(),
            "attempts": attempts,
            "backlog": ["1"],
            "result": result,
        },
    )


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


def test_a_lane_that_never_checked_in_carries_no_age_and_no_row(
    bridge, repo, paired, served
):
    lanes = bridge.status_snapshot()["projects"][0]["participants"]
    assert lanes
    for record in lanes:
        assert record["availability"]["last_active_at"] is None
        assert record["availability"]["age_seconds"] is None
    assert not rows(bridge, problems.INACTIVE)
    assert not bridge.problems()


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


def test_a_stalled_lane_the_service_still_wakes_is_reported_as_its_work(
    bridge, repo, paired, served
):
    directory = bridge.project(repo)[1]
    alive(directory, "claude")
    deliver(bridge, repo, paired, aged=1800)
    [row] = rows(bridge, problems.STALLED)
    assert row["participant"] == "claude"
    assert row["detail"].startswith("idle; message")
    assert row["seconds"] >= 1800
    assert row["actor"] == problems.BY_SERVICE
    assert row["command"] == (
        "the coordination service wakes claude on its next poll; no "
        "operator action yet"
    )
    assert "agent-parley" not in row["command"]
    assert not rows(bridge, problems.INACTIVE)


def test_a_second_session_in_a_lane_is_named_while_it_runs(
    bridge, repo, paired, served
):
    directory = bridge.project(repo)[1]
    child = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.read()"],
        stdin=subprocess.PIPE,
    )
    foreign = {
        "session_id": "s2",
        "pid": child.pid,
        "ticks": start_ticks(child.pid),
        "seen": time.time() - 30,
    }
    try:
        alive(directory, "claude", session_id="s1", foreign_session=foreign)
        [row] = rows(bridge, problems.FOREIGN)
        assert row["participant"] == "claude"
        assert f"session s2 (pid {child.pid})" in row["detail"]
        assert row["seconds"] >= 30
    finally:
        child.stdin.close()
        child.wait()
    assert not rows(bridge, problems.FOREIGN)


def test_a_stalled_lane_the_service_cannot_wake_names_the_say(
    bridge, repo, paired, served
):
    unwoken(bridge)
    directory = bridge.project(repo)[1]
    alive(directory, "claude")
    deliver(bridge, repo, paired, aged=1800)
    [row] = rows(bridge, problems.STALLED)
    assert row["actor"] == problems.BY_OPERATOR
    assert row["command"] == (
        f'agent-parley say claude "<text>" --repo {paired["root"]}'
    )
    assert "--resume" not in row["command"]


def test_a_lane_the_service_has_woken_reports_the_attempts_it_made(
    bridge, repo, paired, served
):
    directory = bridge.project(repo)[1]
    alive(directory, "claude")
    refuse(bridge, directory, "claude", "busy:turn", attempts=2)
    deliver(bridge, repo, paired, aged=1800)
    [row] = rows(bridge, problems.STALLED)
    assert row["actor"] == problems.BY_SERVICE
    assert row["command"] == (
        "the coordination service has woken claude 2 times and wakes it "
        "again on its next poll; no operator action yet"
    )
    assert not rows(bridge, problems.WAKE)


def test_a_stopped_lane_awaiting_acknowledgement_names_the_resume(
    bridge, repo, paired, served
):
    directory = bridge.project(repo)[1]
    stopped(directory, "claude")
    deliver(bridge, repo, paired, ack=True, aged=1800)
    [row] = rows(bridge, problems.ACK, ack_after=600)
    assert row["command"] == (
        f"agent-parley run claude --resume --repo {paired['root']}"
    )
    assert not rows(bridge, problems.STALLED)
    assert not rows(bridge, problems.INACTIVE)


def test_a_live_lane_past_the_inactive_threshold_is_a_row(
    bridge, repo, paired, served
):
    alive(bridge.project(repo)[1], "claude")
    [row] = rows(bridge, problems.INACTIVE)
    assert row["participant"] == "claude"
    assert row["actor"] == problems.BY_SERVICE
    assert row["command"].startswith("the coordination service wakes claude")
    assert [r["participant"] for r in rows(bridge)] == ["claude"]


def test_an_idle_lane_holding_a_refused_key_names_the_refused_lane(
    bridge, repo, paired, served
):
    alive(bridge.project(repo)[1], "claude")
    store.initialize(bridge.home)
    lanes = {
        name: store.authenticate(
            bridge.home,
            store.register(bridge.home, paired["root"], name)[
                "registration_token"
            ],
        )
        for name in ("claude", "codex")
    }
    store.call(
        bridge.home,
        lanes["claude"],
        "file_reservation_paths",
        {"paths": ["shared.txt"]},
    )
    refused = store.call(
        bridge.home,
        lanes["codex"],
        "file_reservation_paths",
        {"paths": ["shared.txt"]},
    )
    assert refused["conflicts"]
    [record] = [
        lane
        for project in bridge.status_snapshot()["projects"]
        for lane in project["participants"]
        if lane["participant"] == "claude"
    ]
    assert record["mail"]["refused"] == ["codex"]
    [row] = rows(bridge, problems.HOLDING)
    assert row["participant"] == "claude"
    assert "codex" in row["detail"]
    assert row["seconds"] is not None
    store.call(bridge.home, lanes["claude"], "release_file_reservations", {})
    assert not rows(bridge, problems.HOLDING)


def test_a_hook_refusal_on_a_held_key_names_the_refused_lane(
    bridge, repo, paired, served
):
    directory = bridge.project(repo)[1]
    alive(directory, "claude")
    store.initialize(bridge.home)
    holder = store.authenticate(
        bridge.home,
        store.register(bridge.home, paired["root"], "claude")[
            "registration_token"
        ],
    )
    store.register(bridge.home, paired["root"], "codex")
    store.call(
        bridge.home,
        holder,
        "file_reservation_paths",
        {"paths": ["shared.txt"], "exclusive": True},
    )
    lane = Path(paired["lanes"]["codex"])
    write_json(directory / "codex-identity.json", {"name": "codex"})
    denied = checkpoints.checkpoint(
        bridge.home,
        directory,
        "codex",
        {
            "hook_event_name": "PreToolUse",
            "cwd": str(lane),
            "session_id": "s1",
            "tool_name": "Write",
            "tool_input": {"file_path": str(lane / "shared.txt")},
        },
    )
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    [record] = [
        lane
        for project in bridge.status_snapshot()["projects"]
        for lane in project["participants"]
        if lane["participant"] == "claude"
    ]
    assert record["mail"]["refused"] == ["codex"]
    [row] = rows(bridge, problems.HOLDING)
    assert row["participant"] == "claude"
    assert "codex" in row["detail"]


def test_an_escalated_native_dialog_is_a_row_naming_it(
    bridge, repo, paired, served
):
    alive(
        bridge.project(repo)[1],
        "claude",
        activity="dialog: native directory trust prompt",
        dialog={
            "name": "directory-trust",
            "label": "native directory trust prompt",
            "action": "answer",
            "escalated": True,
            "options": ["1. Yes, continue", "2. No, quit"],
            "at": time.time() - 40,
        },
    )
    [row] = rows(bridge, problems.HELD)
    assert row["participant"] == "claude"
    assert row["detail"] == (
        "the client is held by native directory trust prompt "
        "(1. Yes, continue; 2. No, quit)"
    )
    assert row["command"] == "answer the prompt in claude's terminal"
    assert row["seconds"] >= 40


def test_a_paused_lane_is_resumed_rather_than_spoken_to(
    bridge, repo, paired, served
):
    directory = bridge.project(repo)[1]
    alive(directory, "claude")
    bridge.pause(repo, "claude")
    [row] = rows(bridge, problems.INACTIVE)
    assert row["actor"] == problems.BY_OPERATOR
    assert row["command"] == (
        f"agent-parley participant resume claude --repo {paired['root']}"
    )


@pytest.mark.parametrize(
    "result,detail,remedy",
    [
        (
            problems.DIALOG,
            "operator input is pending",
            "answer the prompt open in claude's own client; it reads no "
            "mail until that prompt is cleared",
        ),
        (
            problems.ATTENTION,
            "requires operator attention",
            "take the turn waiting in claude's own client; the service "
            "stopped asking after its refusals",
        ),
        (
            problems.RETRY,
            "produced no checkpoint",
            "the coordination service wakes claude on its next poll; no "
            "operator action yet",
        ),
    ],
)
def test_a_wake_refusal_names_its_reason_and_an_actor_who_can_clear_it(
    bridge, repo, paired, served, result, detail, remedy
):
    directory = bridge.project(repo)[1]
    alive(directory, "claude")
    refuse(bridge, directory, "claude", result)
    [record] = [
        row
        for row in bridge.status_snapshot()["projects"][0]["participants"]
        if row["participant"] == "claude"
    ]
    assert record["wake"]["result"] == result
    [row] = rows(bridge, problems.WAKE)
    assert detail in row["detail"]
    assert row["command"] == remedy
    assert "complete or stop the session" not in row["command"]


def test_a_lane_that_cannot_read_mail_is_never_offered_say(
    bridge, repo, paired, served
):
    directory = bridge.project(repo)[1]
    alive(directory, "claude")
    refuse(bridge, directory, "claude", problems.DIALOG)
    deliver(bridge, repo, paired, ack=True, aged=1800)
    found = rows(bridge, ack_after=600)
    assert found
    assert all("agent-parley say" not in row["command"] for row in found)
    [row] = [item for item in found if item["condition"] == problems.ACK]
    assert row["actor"] == problems.BY_OPERATOR
    assert "reads no mail" in row["command"]


def test_a_live_working_lane_is_never_told_to_stop_its_session(
    bridge, repo, paired, served
):
    directory = bridge.project(repo)[1]
    alive(directory, "claude", updated=time.time())
    refuse(bridge, directory, "claude", problems.RETRY)
    [row] = rows(bridge, problems.WAKE)
    assert row["actor"] == problems.BY_SERVICE
    assert row["command"] == (
        "the coordination service wakes claude once it goes idle; the lane "
        "is active and needs no operator now"
    )
    assert "terminal" not in row["command"]
    assert not rows(bridge, problems.INACTIVE)


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
    assert row["count"] == 1


def test_two_overdue_claims_on_one_lane_are_one_row(
    bridge, repo, paired, served
):
    directory = bridge.project(repo)[1]
    for number in ("42", "43"):
        bridge.issue(paired["lanes"]["claude"], "claim", number, within=60)
    state = issues.snapshot(directory)
    state["issues"]["42"]["deadline"] = time.time() - 900
    state["issues"]["43"]["deadline"] = time.time() - 300
    state["revision"] += 1
    write_json(directory / "issues.json", state)
    [row] = rows(bridge, problems.OVERDUE)
    assert row["count"] == 2
    assert row["seconds"] >= 900
    assert row["detail"] == "2 claims are past their deadline: #42, #43"
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


def test_two_offers_waiting_on_one_lane_are_one_row(
    bridge, repo, paired, served
):
    for number in ("1", "2"):
        bridge.issue(paired["lanes"]["claude"], "claim", number)
        bridge.issue(
            paired["lanes"]["claude"],
            "offer",
            number,
            to="codex",
            summary="Take it",
        )
    directory = bridge.project(repo)[1]
    state = issues.snapshot(directory)
    state["issues"]["1"]["offer"]["created"] = time.time() - 900
    state["revision"] += 1
    write_json(directory / "issues.json", state)
    [row] = rows(bridge, problems.OFFER)
    assert row["count"] == 2
    assert row["seconds"] >= 900
    assert row["detail"] == "2 offers await codex, the oldest issue #1 by peer"
    assert row["command"] == (
        f"agent-parley issue cancel 1 --repo {paired['root']}"
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
    assert row["count"] == 1
    assert row["actor"] == problems.BY_SERVICE
    assert row["command"].startswith("the coordination service wakes claude")


def test_a_parked_lane_with_twenty_awaited_messages_is_one_row(
    bridge, repo, paired, served
):
    directory = bridge.project(repo)[1]
    stopped(directory, "claude")
    deliver(bridge, repo, paired, ack=True, aged=1800, count=20)
    found = rows(bridge, ack_after=600)
    assert [row["condition"] for row in found] == [problems.ACK]
    [row] = found
    assert row["count"] == 20
    assert row["seconds"] >= 1800
    assert row["detail"].startswith("20 messages await acknowledgement, ")
    assert row["command"] == (
        f"agent-parley run claude --resume --repo {paired['root']}"
    )


def test_a_broadcast_costs_one_row_per_parked_lane(
    bridge, repo, paired, served
):
    directory = bridge.project(repo)[1]
    store.initialize(bridge.home)
    for name in ("claude", "codex"):
        stopped(directory, name)
        store.register(bridge.home, paired["root"], name)
    for key in ("first", "second"):
        for name in ("claude", "codex"):
            bridge.say(
                repo,
                name,
                f"Answer the {key}",
                ack=True,
                key=f"{key}-{name}",
            )
    with store.connect(bridge.home, write=True) as db:
        db.execute(
            "UPDATE messages SET created_ts=datetime('now','-1800 seconds')"
        )
    found = rows(bridge, problems.ACK, ack_after=600)
    assert len(found) == 2
    assert {row["participant"] for row in found} == {"claude", "codex"}
    assert "2 messages await acknowledgement" in found[0]["detail"]
    assert "the oldest" in found[0]["detail"]


def test_a_bounced_share_is_a_row_on_the_sender(bridge, repo, paired, served):
    directory = bridge.project(repo)[1]
    store.initialize(bridge.home)
    alive(directory, "claude")
    stopped(directory, "codex")
    actor = store.authenticate(
        bridge.home,
        store.register(bridge.home, paired["root"], "claude")[
            "registration_token"
        ],
    )
    store.register(bridge.home, paired["root"], "codex")
    share = store.call(
        bridge.home,
        actor,
        "send_message",
        {
            "to": ["codex"],
            "subject": "Take the parser half",
            "body_md": "Take the parser half",
            "idempotency_key": "share",
            "ack_required": True,
            "ack_within": 600,
        },
    )
    [row] = rows(bridge, problems.BOUNCE)
    assert row["participant"] == "claude"
    assert f"share {share['id']}" in row["detail"]
    assert "codex has no live session process" in row["detail"]
    assert row["command"] == (
        f"agent-parley run codex --resume --repo {paired['root']}"
    )


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


def test_a_quiet_dirty_worktree_names_its_path_and_files_without_retiring(
    bridge, repo, paired, served
):
    lane = Path(paired["lanes"]["claude"])
    (lane / "draft.txt").write_text("unfinished\n")
    (lane / "notes.md").write_text("unfinished\n")
    [row] = rows(bridge, problems.DIRTY)
    assert row["participant"] == "claude"
    assert row["count"] == 2
    assert str(lane) in row["detail"]
    assert "draft.txt" in row["detail"] and "notes.md" in row["detail"]
    assert row["actor"] == problems.BY_OPERATOR
    assert row["command"] == (
        f"commit or stash the work in {lane}; retiring claude would drop "
        "the claims it holds to clean one directory"
    )
    assert "agent-parley" not in row["command"]


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
    assert document["operator"] == 1
    assert document["service"] == 1
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
        "count",
        "actor",
        "command",
    }


def test_every_printed_row_names_the_lane_the_age_and_the_command(
    bridge, repo, paired, served, monkeypatch, capsys
):
    unwoken(bridge)
    alive(bridge.project(repo)[1], "claude")
    code, out = run(monkeypatch, capsys, bridge)
    assert code == 1
    [line] = out.splitlines()
    assert "claude" in line
    assert problems.INACTIVE in line
    assert line.endswith(f"--repo {paired['root']}")
    assert line.split()[0].endswith("h")


def test_the_report_closes_by_counting_what_the_service_is_handling(
    bridge, repo, paired, served, monkeypatch, capsys
):
    alive(bridge.project(repo)[1], "claude")
    code, out = run(monkeypatch, capsys, bridge)
    assert code == 1
    printed = out.splitlines()
    assert len(printed) == 2
    assert printed[-1] == (
        "0 need an operator; 1 the coordination service is handling."
    )


def declared():
    """Maps every command path the CLI declares to its subcommands."""
    parser, _ = cli.root_parser(None)
    return {path: names for path, names, _ in completion.tree(parser)}


def cited(remedy):
    """Lists the command words each `agent-parley` mention in a remedy names."""
    words = [word.strip(",.;") for word in remedy.split()]
    return [
        words[index + 1 : index + 3]
        for index, word in enumerate(words)
        if word == "agent-parley"
    ]


def lane_record(**extra):
    """Shapes one participant record carrying no condition."""
    return {
        "participant": "claude",
        "availability": {
            "state": supervision.IDLE,
            "process_alive": True,
            "age_seconds": 900,
        },
        "idle": {
            "stalled": False,
            "kind": None,
            "message_id": None,
            "sender": "",
            "age_seconds": 0,
            "served_age_seconds": None,
        },
        "claims": [],
        "mail": {"outstanding_ack": []},
        "drift": False,
        "branch": "work",
        "assigned_branch": "work",
        "paused": False,
        "budget": {"over": False, "marker": "over budget; hours 2h of 1h"},
        "wake": None,
        **extra,
    }


LANE_STATES = [
    {},
    {"paused": True},
    {
        "availability": {
            "state": supervision.ACTIVE,
            "process_alive": True,
            "age_seconds": 10,
        }
    },
    {
        "availability": {
            "state": supervision.STOPPED,
            "process_alive": False,
            "age_seconds": 900,
        }
    },
    {"wake": {"result": problems.DIALOG, "attempts": 1, "age_seconds": 60}},
    {"wake": {"result": problems.ATTENTION, "attempts": 3, "age_seconds": 60}},
    {"wake": {"result": problems.RETRY, "attempts": 0, "age_seconds": 60}},
    {"wake": {"result": problems.RETRY, "attempts": 9, "age_seconds": 60}},
]

LANE_CAUSES = [
    {},
    {
        "idle": {
            "stalled": True,
            "kind": "unread",
            "message_id": 3,
            "sender": "operator",
            "age_seconds": 900,
            "served_age_seconds": None,
        }
    },
    {"claims": [{"issue": 42, "overdue": True, "overdue_seconds": 900}]},
    {
        "claims": [
            {
                "issue": 42,
                "overdue": False,
                "overdue_seconds": 0,
                "unresolved": True,
                "reason": "the lane pull request is merged",
                "observed_at": 0,
            }
        ]
    },
    {
        "mail": {
            "outstanding_ack": [
                {"message_id": 3, "sender": "operator", "age_seconds": 900}
            ]
        }
    },
    {"drift": True, "branch": "elsewhere"},
    {"budget": {"over": True, "marker": "over budget; hours 2h of 1h"}},
]

OFFERED = {
    "root": "/root",
    "issues": [
        {"issue": 7, "offer": {"to": "codex", "created_at": None}},
        {
            "issue": 8,
            "offer": {
                "to": "mimi",
                "created_at": "2026-09-20T10:00:00Z",
                "source": "operator",
            },
        },
    ],
}


def test_every_remedy_the_view_emits_names_a_command_the_cli_declares(
    monkeypatch,
):
    monkeypatch.setattr(supervision, "dirty_paths", lambda lane: ["draft.txt"])
    tree = declared()
    emitted = [
        problems._row(problems.STORE, "schema is behind", protocol.MIGRATE),
        problems._row(problems.STORE, "schema is ahead", protocol.UPGRADE),
        problems._row(problems.SERVICE, "not ready", "agent-parley up"),
        *problems._offer_rows(OFFERED, time.time()),
    ]
    for waking in (True, False):
        for state in LANE_STATES:
            for cause in LANE_CAUSES:
                emitted.extend(
                    problems._lane_rows(
                        lane_record(**{**cause, **state}),
                        {"lane": "/lane", "branch": "work"},
                        "/root",
                        {**supervision.DEFAULTS, "wake": waking},
                        600,
                        time.time(),
                    )
                )
    assert {row["condition"] for row in emitted} == {
        problems.STORE,
        problems.SERVICE,
        problems.STALLED,
        problems.INACTIVE,
        problems.OVERDUE,
        problems.UNRESOLVED,
        problems.OFFER,
        problems.ACK,
        problems.DRIFT,
        problems.DIRTY,
        problems.BUDGET,
        problems.WAKE,
    }
    for row in emitted:
        assert row["actor"] in (problems.BY_OPERATOR, problems.BY_SERVICE)
        assert row["command"]
        remedy = row["command"]
        pasteable = remedy.startswith("agent-parley")
        assert not (pasteable and row["actor"] == problems.BY_SERVICE), remedy
        for words in cited(remedy):
            if words[0] not in tree[""]:
                assert not pasteable, remedy
                continue
            if tree[words[0]]:
                assert words[1:2] and words[1] in tree[words[0]], remedy
        if not pasteable:
            assert " " in remedy
            assert any(
                named in remedy
                for named in ("claude", "/lane", "agent-parley", "service")
            ), remedy


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
                "count": 1,
                "actor": problems.BY_OPERATOR,
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
