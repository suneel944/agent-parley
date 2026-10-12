"""Checks the triage view that lists every condition needing an operator."""

import json
import os
import shlex
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
    dialogs,
    issues,
    lanes,
    problems,
    protocol,
    roster,
    status,
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


def at(root):
    """Renders the quoted --repo argument a remedy ends with."""
    return f"--repo {shlex.quote(str(root))}"


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
        f'agent-parley say claude "<text>" {at(paired["root"])}'
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
        f"agent-parley run claude --resume {at(paired['root'])}"
    )
    assert not rows(bridge, problems.STALLED)
    assert not rows(bridge, problems.INACTIVE)


def test_a_live_lane_past_the_inactive_threshold_is_a_row(
    bridge, repo, paired, served
):
    alive(bridge.project(repo)[1], "claude")
    bridge.issue(paired["lanes"]["claude"], "claim", "42")
    [row] = rows(bridge, problems.INACTIVE)
    assert row["participant"] == "claude"
    assert row["actor"] == problems.BY_SERVICE
    assert row["command"].startswith("the coordination service wakes claude")
    assert [r["participant"] for r in rows(bridge)] == ["claude"]


@pytest.mark.parametrize("claimed", [False, True])
def test_a_quiet_lane_owing_no_work_is_at_rest(
    bridge, repo, paired, served, claimed
):
    alive(bridge.project(repo)[1], "claude")
    if claimed:
        lane = paired["lanes"]["claude"]
        bridge.issue(lane, "claim", "42")
        bridge.report(lane, "ready", "Task done.", "", "make check")
    assert not rows(bridge, problems.INACTIVE)


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


def test_a_refused_key_held_past_its_deadline_is_an_operator_decision(
    bridge, repo, paired, served, monkeypatch
):
    directory = bridge.project(repo)[1]
    alive(directory, "claude")
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
    store.call(
        bridge.home,
        lanes["codex"],
        "file_reservation_paths",
        {"paths": ["shared.txt"]},
    )
    manifest = roster.read(directory)
    idle = {"claude": {"state": supervision.IDLE}}

    def subjects(name):
        inbox = store.call(
            bridge.home,
            lanes[name],
            "fetch_inbox",
            {"include_bodies": True},
        )
        return [item["subject"] for item in inbox["messages"]]

    supervision.refused_keys(bridge.home, directory, manifest, idle)
    hold = supervision.key_hold(directory, "claude")
    assert hold["refused"] == ["codex"]
    assert hold["deadline"] == hold["since"] + supervision.KEY_HOLD_DEADLINE
    [told] = [s for s in subjects("claude") if "refused to codex" in s]
    assert told.startswith("Release by ")
    [row] = rows(bridge, problems.HOLDING)
    assert row["actor"] == problems.BY_SERVICE
    assert "asked to release it within" in row["detail"]
    assert not [s for s in subjects("codex") if "past its deadline" in s]

    supervision.refused_keys(
        bridge.home, directory, manifest, {"claude": {"state": "active"}}
    )
    assert supervision.key_hold(directory, "claude")["since"] == hold["since"]

    monkeypatch.setattr(supervision, "KEY_HOLD_DEADLINE", 0.0)
    supervision.refused_keys(bridge.home, directory, manifest, idle)
    supervision.refused_keys(bridge.home, directory, manifest, idle)
    [row] = rows(bridge, problems.HOLDING)
    assert row["actor"] == problems.BY_OPERATOR
    assert "deadline passed" in row["detail"]
    assert row["command"].startswith("decide: agent-parley say claude")
    assert len([s for s in subjects("codex") if "past its deadline" in s]) == 1
    assert len([s for s in subjects("claude") if "Release by" in s]) == 1

    store.call(bridge.home, lanes["claude"], "release_file_reservations", {})
    supervision.refused_keys(bridge.home, directory, manifest, idle)
    assert supervision.key_hold(directory, "claude") == {}
    assert not rows(bridge, problems.HOLDING)


def test_an_active_holder_is_given_no_deadline(bridge, repo, paired, served):
    directory = bridge.project(repo)[1]
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
    for name in ("claude", "codex"):
        store.call(
            bridge.home,
            lanes[name],
            "file_reservation_paths",
            {"paths": ["shared.txt"]},
        )
    supervision.refused_keys(
        bridge.home,
        directory,
        roster.read(directory),
        {"claude": {"state": supervision.ACTIVE}},
    )
    assert supervision.key_hold(directory, "claude") == {}


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


@pytest.mark.parametrize(
    "dialog",
    [
        {"name": "directory-trust", "escalated": True, "at": 0},
        {"name": dialogs.PERMISSION, "tool": "Bash", "since": 0},
    ],
)
def test_a_dialog_ends_with_the_session_process_that_showed_it(dialog):
    record = lane_record(
        dialog=dialog,
        availability={
            "state": supervision.STOPPED,
            "process_alive": False,
            "age_seconds": 900,
        },
        idle={**lane_record()["idle"], "stalled": True, "age_seconds": 900},
    )
    found = problems._lane_rows(
        record,
        {"lane": "/lane", "branch": "work"},
        "/root",
        {**supervision.DEFAULTS, "wake": False},
        600,
        time.time(),
    )
    conditions = {row["condition"] for row in found}
    assert not conditions & {problems.HELD, problems.APPROVAL}
    assert [row["command"] for row in found] == [
        "agent-parley run claude --resume --repo /root"
    ]


def test_a_held_permission_prompt_is_a_waiting_on_approval_row(
    bridge, repo, paired, served
):
    alive(
        bridge.project(repo)[1],
        "claude",
        activity="waiting for approval: Bash",
        dialog={
            "name": dialogs.PERMISSION,
            "tool": "Bash",
            "since": time.time() - 7200,
        },
    )
    [row] = rows(bridge, "waiting on approval")
    assert row["participant"] == "claude"
    assert row["detail"] == "the client is waiting for approval of Bash"
    assert row["command"] == "answer the prompt in claude's terminal"
    assert problems.APPROVAL == "waiting on approval"


def test_a_prompt_in_a_lane_resumed_without_a_terminal_names_the_restart(
    bridge, repo, paired, served
):
    directory = bridge.project(repo)[1]
    alive(
        directory,
        "claude",
        attached=False,
        dialog={
            "name": dialogs.PERMISSION,
            "tool": "Bash",
            "since": time.time() - 7200,
        },
    )
    deliver(bridge, repo, paired, ack=True, aged=1800)
    found = rows(bridge, ack_after=600)
    [row] = [item for item in found if item["condition"] == problems.APPROVAL]
    assert "claude's terminal" not in row["command"]
    assert str(directory / "claude-wake.log") in row["command"]
    assert f"participant restart claude {at(paired['root'])}" in row["command"]
    assert all("agent-parley say" not in item["command"] for item in found)


def test_a_paused_lane_is_resumed_rather_than_spoken_to(
    bridge, repo, paired, served
):
    directory = bridge.project(repo)[1]
    alive(directory, "claude")
    bridge.pause(repo, "claude")
    [row] = rows(bridge, problems.INACTIVE)
    assert row["actor"] == problems.BY_OPERATOR
    assert row["command"] == (
        f"agent-parley participant resume claude {at(paired['root'])}"
    )


@pytest.mark.parametrize(
    "result,detail,remedy",
    [
        (
            problems.DIALOG,
            "operator input is pending",
            "submit or clear the unsent text in claude's own terminal "
            "(Enter, or Ctrl-U to clear the line); it reads no mail until "
            "that line is empty",
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
        (
            supervision.SESSION_HELD,
            "holds the session lock",
            "take the turn waiting in claude's own client; its launcher "
            "still holds the session lock, so a resume would be refused",
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


def test_an_undelivered_wake_names_when_it_was_attempted_and_why(
    bridge, repo, paired, served
):
    directory = bridge.project(repo)[1]
    alive(directory, "claude")
    refuse(bridge, directory, "claude", supervision.WAKE_UNAVAILABLE)
    [record] = [
        row
        for row in bridge.status_snapshot()["projects"][0]["participants"]
        if row["participant"] == "claude"
    ]
    [row] = rows(bridge, problems.WAKE)
    assert row["detail"].startswith(
        f"wake attempted at {record['wake']['at']}, not delivered: no "
        "launcher answered the lane's wake socket"
    )
    assert row["actor"] == problems.BY_OPERATOR
    assert row["command"] == (
        "take the turn waiting in claude's own client, or end it and run "
        f"agent-parley run claude --resume {at(paired['root'])} so wakes "
        "reach it"
    )


def test_a_withheld_resume_names_the_missing_approval_opt_in(
    bridge, repo, paired, served
):
    directory = bridge.project(repo)[1]
    alive(directory, "claude")
    refuse(bridge, directory, "claude", supervision.OPT_IN_MISSING)
    [row] = rows(bridge, problems.WAKE)
    assert row["detail"].startswith("setup gap")
    assert row["actor"] == problems.BY_OPERATOR
    repo_arg = at(paired["root"])
    assert row["command"] == (
        f"agent-parley approval resume --bridge-tools on --participant claude "
        f"{repo_arg}, or agent-parley run claude --resume {repo_arg} in your "
        "own terminal"
    )


def test_withheld_resumes_of_one_project_share_one_row(
    bridge, repo, paired, served
):
    directory = bridge.project(repo)[1]
    for name in ("claude", "codex"):
        alive(directory, name)
        refuse(bridge, directory, name, supervision.OPT_IN_MISSING)

    [row] = rows(bridge, problems.WAKE)

    assert row["count"] == 2
    assert row["detail"].endswith("lanes: claude, codex")
    assert row["command"].startswith(
        f"agent-parley approval resume --bridge-tools on {at(paired['root'])}"
    )


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
        f"agent-parley issue release 42 {at(paired['root'])}"
    )
    assert row["count"] == 1


def test_a_refused_recovery_names_its_reason_while_the_claim_stands(
    bridge, repo, paired, served
):
    directory = bridge.project(repo)[1]
    bridge.issue(paired["lanes"]["claude"], "claim", "42")
    claim_id = issues.snapshot(directory)["issues"]["42"]["claim_id"]
    refusal = directory / "recovery" / "issue-42-refusal.json"
    refusal.parent.mkdir(exist_ok=True)
    write_json(
        refusal,
        {
            "issue": "42",
            "claim_id": claim_id,
            "reason": "Claim owner is waiting for operator input.",
            "refused_at": time.time() - 120,
        },
    )
    [row] = rows(bridge, problems.REFUSED)
    assert row["participant"] == "claude"
    assert row["seconds"] >= 120
    assert row["detail"] == (
        "issue #42: approved recovery refused: "
        "Claim owner is waiting for operator input."
    )

    write_json(refusal, {**json.loads(refusal.read_text()), "claim_id": "x"})
    assert not rows(bridge, problems.REFUSED)


def test_a_missing_root_names_the_live_lanes_it_keeps(
    bridge, repo, paired, served
):
    directory = bridge.project(repo)[1]
    assert not rows(bridge, problems.ROOT)
    write_json(
        directory / supervision.ROOT_PUBLICATION,
        {
            "since": time.time() - 600,
            "retired": None,
            "state_directory": str(directory),
            "lanes": [],
            "live": ["claude"],
        },
    )
    [row] = rows(bridge, problems.ROOT)
    assert row["participant"] == ""
    assert row["project"] == paired["root"]
    assert row["seconds"] >= 600
    assert row["detail"] == (
        "project root is gone; live lanes kept from retirement: claude"
    )


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
        f"agent-parley issue release 42 {at(paired['root'])}"
    )


def test_an_unanswered_offer_names_its_recipient_and_the_cancel(
    bridge, repo, paired, served, monkeypatch, capsys
):
    bridge.issue(paired["lanes"]["claude"], "claim", "1")
    bridge.issue(
        paired["lanes"]["claude"], "offer", "1", to="codex", summary="Take it"
    )
    [row] = rows(bridge, problems.OFFER)
    assert row["participant"] == "codex"
    assert "offered to codex by peer" in row["detail"]
    assert row["command"] == (
        f"agent-parley issue cancel 1 {at(paired['root'])}"
    )
    bridge.issue(paired["lanes"]["claude"], "cancel", "1")
    bridge.issue_assign(repo, "2", "codex")
    [row] = rows(bridge, problems.OFFER)
    assert row["command"] == (
        f"agent-parley issue assign 2 --unassign {at(paired['root'])}"
    )
    program, *arguments = shlex.split(row["command"])
    monkeypatch.setattr(
        sys, "argv", [program, "--home", str(bridge.home), *arguments]
    )
    assert cli.main() == 0
    capsys.readouterr()
    assert rows(bridge, problems.OFFER) == []


def test_an_unanswered_request_names_its_holder_and_the_unassign(
    bridge, repo, paired, served, monkeypatch, capsys
):
    bridge.issue(paired["lanes"]["claude"], "claim", "1")
    bridge.issue(paired["lanes"]["codex"], "request", "1", summary="Take it")
    [row] = rows(bridge, problems.REQUEST)
    assert row["participant"] == "claude"
    assert (
        "issue #1 takeover asked by codex, unanswered by claude"
        in row["detail"]
    )
    assert row["command"] == (
        f"agent-parley issue assign 1 --unassign {at(paired['root'])}"
    )
    program, *arguments = shlex.split(row["command"])
    monkeypatch.setattr(
        sys, "argv", [program, "--home", str(bridge.home), *arguments]
    )
    assert cli.main() == 0
    capsys.readouterr()
    assert rows(bridge, problems.REQUEST) == []


def test_an_operator_request_is_not_a_row(bridge, repo, paired, served):
    bridge.issue(paired["lanes"]["claude"], "claim", "1")
    bridge.issue_assign(repo, "1", "codex")
    directory = bridge.project(repo)[1]
    assert issues.snapshot(directory)["issues"]["1"]["request"]["source"] == (
        issues.OPERATOR
    )
    assert rows(bridge, problems.REQUEST) == []


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
        f"agent-parley issue cancel 1 {at(paired['root'])}"
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
        f"agent-parley run claude --resume {at(paired['root'])}"
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
        f"agent-parley run codex --resume {at(paired['root'])}"
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
        f"agent-parley participant restore claude {at(paired['root'])}"
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
        f"agent-parley participant budget claude {at(paired['root'])}"
    )


def test_rows_are_ordered_longest_held_first_behind_the_store_and_service(
    bridge, repo, paired, monkeypatch
):
    directory = bridge.project(repo)[1]
    alive(directory, "claude", updated=time.time() - 7200)
    alive(directory, "codex", updated=time.time() - 400)
    bridge.issue(paired["lanes"]["claude"], "claim", "42", within=60)
    bridge.issue(paired["lanes"]["codex"], "claim", "43")
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
    bridge.issue(paired["lanes"]["claude"], "claim", "42")
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
    alive(bridge.project(repo)[1], "claude", updated=time.time() - 7200)
    bridge.issue(paired["lanes"]["claude"], "claim", "42")
    code, out = run(monkeypatch, capsys, bridge)
    assert code == 1
    [line] = out.splitlines()
    assert "claude" in line
    assert problems.INACTIVE in line
    assert line.endswith(at(paired["root"]))
    assert line.split()[0].endswith("h")


def test_the_report_closes_by_counting_what_the_service_is_handling(
    bridge, repo, paired, served, monkeypatch, capsys
):
    alive(bridge.project(repo)[1], "claude", updated=time.time() - 7200)
    bridge.issue(paired["lanes"]["claude"], "claim", "42")
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
    {
        "claims": [
            {"issue": number, "overdue": False, "overdue_seconds": 0}
            for number in (41, 42, 43)
        ]
    },
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
        problems.OVER_CAP,
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


def test_claims_past_the_cap_are_named_with_their_excess():
    claims = [
        {"issue": number, "overdue": False, "overdue_seconds": 0}
        for number in range(1376, 1388)
    ]
    rows = problems._lane_rows(
        lane_record(claims=claims),
        {"lane": "/lane", "branch": "work"},
        "/root",
        {**supervision.DEFAULTS, "wake": False},
        600,
        time.time(),
    )
    [row] = [row for row in rows if row["condition"] == problems.OVER_CAP]
    assert row["count"] == 10
    assert "holds 12 claims, 10 past max_claims_per_lane 2" in row["detail"]
    assert row["command"] == "agent-parley issue release 1387 --repo /root"
    within = problems._lane_rows(
        lane_record(claims=claims[:2]),
        {"lane": "/lane", "branch": "work"},
        "/root",
        {**supervision.DEFAULTS, "wake": False},
        600,
        time.time(),
    )
    assert problems.OVER_CAP not in {row["condition"] for row in within}


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


def test_plan_revisions_waiting_on_the_operator_are_rows(
    bridge, repo, paired, served
):
    path = repo.parent / "plan.toml"
    path.write_text(
        '[dependencies]\n"42" = ["17"]\n"43" = ["17"]\n"44" = ["42"]\n\n'
        '[revisions]\nscope = ["17", "42", "43", "44", "60"]\n'
        "max_changes = 2\nmax_revisions = 5\n"
    )
    bridge.work_plan(repo, "apply", path)
    lane = paired["lanes"]["claude"]
    assert not rows(bridge, problems.ESCALATED)
    assert not rows(bridge, problems.PROPOSED)
    base = bridge.plan_revision(repo, "proposals")["revision"]
    for offset, change in enumerate(
        ({"add": ["43:42"]}, {"remove": ["43:42"]}, {"add": ["43:42"]})
    ):
        looped = bridge.plan_revision(
            lane, "propose", base=base + offset, reason="loop", **change
        )
    assert looped["status"] == "escalated"
    waiting = bridge.plan_revision(
        lane, "propose", base=base + 2, add=["42:60"], reason="new"
    )
    assert waiting["status"] == "pending"
    root = paired["root"]
    [escalated] = rows(bridge, problems.ESCALATED)
    assert escalated["participant"] == "claude"
    assert escalated["project"] == root
    assert escalated["detail"] == (
        f"plan proposal {looped['id']} is escalated: #43 waits on #42 "
        "was already revised 2 times under this plan version"
    )
    assert escalated["command"] == (
        f"agent-parley plan approve {looped['id']} {at(root)} or "
        f"agent-parley plan reject {looped['id']} --reason TEXT {at(root)}"
    )
    [pending] = rows(bridge, problems.PROPOSED)
    assert pending["count"] == 1
    assert pending["command"] == f"agent-parley plan proposals {at(root)}"
    bridge.plan_revision(repo, "reject", waiting["id"], reason="not needed")
    bridge.plan_revision(repo, "approve", looped["id"])
    assert not rows(bridge, problems.ESCALATED)
    assert not rows(bridge, problems.PROPOSED)


ORPHANED = [
    {
        "issue": 42,
        "overdue": False,
        "overdue_seconds": 0,
        "orphaned": True,
        "orphan_recorded_seconds": 7200,
    }
]

LIVE = {"state": supervision.IDLE, "process_alive": True, "age_seconds": 900}
GONE = {
    "state": supervision.STOPPED,
    "process_alive": False,
    "age_seconds": 900,
}


def orphaned_lane(availability, monkeypatch, condition, dirty=()):
    """Derives one quiet lane's rows of one condition, all claims orphaned."""
    monkeypatch.setattr(supervision, "dirty_paths", lambda lane: list(dirty))
    return [
        row
        for row in problems._lane_rows(
            lane_record(claims=ORPHANED, availability=availability),
            {"lane": "/lane", "branch": "work"},
            "/root",
            {**supervision.DEFAULTS, "wake": False},
            600,
            time.time(),
        )
        if row["condition"] == condition
    ]


@pytest.mark.parametrize("peer", [True, False])
def test_an_orphaned_claim_names_who_can_move_it(monkeypatch, peer):
    monkeypatch.setattr(supervision, "dirty_paths", lambda lane: [])
    claims = [{**ORPHANED[0], "orphan_recorded_seconds": 60}]
    found = problems._lane_rows(
        lane_record(claims=claims, availability=GONE),
        {"lane": "/lane", "branch": "work"},
        "/root",
        {**supervision.DEFAULTS, "wake": False},
        600,
        time.time(),
        None,
        peer,
    )
    [row] = [row for row in found if row["condition"] == problems.ORPHANED]
    assert row["detail"] == "claims #42 orphaned (no reason recorded)"
    assert row["seconds"] == 60
    assert row["command"] == supervision.orphan_remedy("42", "/root", peer)
    assert ("no live lane" in row["command"]) is not peer
    assert not [row for row in found if row["condition"] == problems.READY]


def test_live_peer_counts_only_other_lanes_that_take_turns():
    states = {"claude": supervision.IDLE, "codex": "dead", "gemini": None}
    assert not supervision.live_peer(states, "claude")
    assert supervision.live_peer(states, "codex")
    assert not supervision.live_peer({**states, "claude": "retired"}, "gemini")
    marker = supervision.orphan_marker(["42"], ["a.py"], "/root", peer=False)
    assert marker == (
        "orphaned claims #42; holds a.py; "
        + supervision.orphan_remedy("42", "/root", False)
    )


def test_a_lane_ready_to_retire_carries_no_separate_orphan_row(monkeypatch):
    assert orphaned_lane(GONE, monkeypatch, problems.READY)
    assert not orphaned_lane(GONE, monkeypatch, problems.ORPHANED)
    assert not orphaned_lane(LIVE, monkeypatch, problems.ORPHANED)


def integrating_lane(monkeypatch, seconds, pull=True, approval=None):
    """Derives a dead lane's rows holding a waiting ready report."""
    monkeypatch.setattr(supervision, "dirty_paths", lambda lane: [])
    opened = {"number": 830, "url": "https://example.test/pull/830"}
    claims = [
        {**ORPHANED[0], "pull_request": opened if pull else None},
    ]
    record = lane_record(
        claims=claims,
        availability=GONE,
        report_issue=42,
        approval=approval,
        waiting=[
            {
                "kind": "report_integration",
                "action": "pending",
                "seconds": seconds,
                "complete": False,
            }
        ],
    )
    return problems._lane_rows(
        record,
        {"lane": "/lane", "branch": "work"},
        "/root",
        {**supervision.DEFAULTS, "wake": False},
        600,
        time.time(),
    )


def test_a_ready_report_waiting_on_its_pull_request_replaces_retirement(
    monkeypatch,
):
    ceiling = supervision.DEFAULT_WAIT_CEILING
    late = integrating_lane(monkeypatch, ceiling + 1)
    conditions = [row["condition"] for row in late]
    assert problems.READY not in conditions
    assert problems.ORPHANED not in conditions
    [row] = [row for row in late if row["condition"] == problems.INTEGRATE]
    assert row["detail"] == (
        "issue #42 reported ready with open pull request #830 "
        "(https://example.test/pull/830) awaiting integration"
    )
    assert row["command"].endswith(
        "agent-parley participant merge claude --repo /root"
    )
    assert row["seconds"] == ceiling + 1
    early = integrating_lane(monkeypatch, ceiling)
    assert not {problems.INTEGRATE, problems.READY} & {
        row["condition"] for row in early
    }
    closed = integrating_lane(monkeypatch, ceiling + 1, pull=False)
    assert problems.READY in {row["condition"] for row in closed}
    held = integrating_lane(
        monkeypatch, ceiling + 1, approval={"state": "pending"}
    )
    [row] = [row for row in held if row["condition"] == problems.INTEGRATE]
    assert row["command"] == "agent-parley approve claude --repo /root"
    assert "approve" in declared()


def test_a_ready_claim_with_no_open_pull_request_is_reported(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(supervision, "dirty_paths", lambda lane: [])
    ceiling = supervision.DEFAULT_WAIT_CEILING

    def rows(ready, pull=None, approval=None, directory=tmp_path):
        claims = [{**ORPHANED[0], "ready_seconds": ready, "pull_request": pull}]
        record = lane_record(claims=claims, approval=approval)
        return [
            row
            for row in problems._lane_rows(
                record,
                {"lane": "/lane", "branch": "work"},
                "/root",
                {**supervision.DEFAULTS, "wake": False},
                600,
                time.time(),
                directory,
            )
            if row["condition"] == problems.UNOPENED
        ]

    assert not rows(ceiling + 1)
    (tmp_path / supervision.PULL_REQUEST_RECORD).write_text("{}")
    [row] = rows(ceiling + 1)
    assert row["detail"] == "issue #42 is ready with no open pull request"
    assert row["command"].endswith("agent-parley issue release 42 --repo /root")
    assert row["seconds"] == ceiling + 1
    assert not rows(ceiling)
    assert not rows(None)
    assert not rows(ceiling + 1, pull={"number": 830})
    assert not rows(ceiling + 1, approval={"state": "pending"})
    assert not rows(ceiling + 1, directory=None)


def test_a_lane_pull_request_past_the_newest_window_blocks_retirement(
    bridge, paired, monkeypatch
):
    store.initialize(bridge.home)
    store.register(bridge.home, paired["root"], "claude")
    monkeypatch.setattr(supervision, "PULL_REQUEST_SECONDS", 0.0)
    monkeypatch.setattr(supervision, "dirty_paths", lambda lane: [])
    directory = Path(paired["lanes"]["claude"]).parent
    data = roster.read(directory)
    branch = data["participants"]["claude"]["branch"]
    oldest = {
        "number": 1,
        "url": "https://example.test/pull/1",
        "branch": branch,
        "sha": "a" * 40,
        "checks": "green",
        "failing": [],
        "reviews": [],
        "mergeable": "MERGEABLE",
        "issues": ["42"],
    }
    opened = [oldest] + [
        {**oldest, "number": number, "branch": f"other-{number}", "issues": []}
        for number in range(2, 36)
    ]
    newest = sorted(opened, key=lambda item: -item["number"])
    monkeypatch.setattr(
        supervision.forge, "open_pull_requests", lambda root: [oldest]
    )
    supervision.pull_request_wakes(bridge.home, directory, data)
    monkeypatch.setattr(
        supervision.forge,
        "open_pull_requests",
        lambda root: newest[: supervision.forge.MAX_PULL_REQUESTS],
    )
    monkeypatch.setattr(
        supervision.forge,
        "pull_request",
        lambda root, number: next(
            item for item in opened if item["number"] == number
        ),
    )
    supervision.pull_request_wakes(bridge.home, directory, data)
    cached = json.loads(
        (directory / supervision.PULL_REQUEST_RECORD).read_text()
    )
    found = status.pull_request(
        list(cached["pull_requests"].values()), "42", branch
    )
    assert found is not None and found["number"] == 1
    record = lane_record(
        claims=[{**ORPHANED[0], "pull_request": found}],
        availability=GONE,
        report_issue=42,
        waiting=[
            {
                "kind": "report_integration",
                "action": "pending",
                "seconds": 7200,
                "complete": False,
            }
        ],
    )
    rows = problems._lane_rows(
        record,
        {"lane": "/lane", "branch": "work"},
        "/root",
        {**supervision.DEFAULTS, "wake": False},
        600,
        time.time(),
    )
    conditions = {row["condition"] for row in rows}
    assert problems.READY not in conditions
    assert problems.INTEGRATE in conditions


def test_a_live_lane_with_orphan_markers_is_never_offered_retirement(
    monkeypatch,
):
    assert not orphaned_lane(LIVE, monkeypatch, problems.READY)
    [row] = orphaned_lane(GONE, monkeypatch, problems.READY)
    assert cited(row["command"]) == [["participant", "retire"]]
    assert "retire" in declared()["participant"]


def test_a_live_quiet_lane_with_edits_is_not_reported_as_abandoned(
    monkeypatch,
):
    assert not orphaned_lane(LIVE, monkeypatch, problems.DIRTY, ["a.txt"])
    [row] = orphaned_lane(GONE, monkeypatch, problems.DIRTY, ["a.txt"])
    assert row["count"] == 1


def test_a_lane_quiet_for_days_follows_a_lane_quiet_for_minutes(
    bridge, repo, paired
):
    directory = bridge.project(repo)[1]
    alive(directory, "claude", updated=time.time() - 3 * 86400)
    alive(directory, "codex", updated=time.time() - 400)
    bridge.issue(paired["lanes"]["claude"], "claim", "42")
    bridge.issue(paired["lanes"]["codex"], "claim", "43")
    found = rows(bridge, problems.INACTIVE)
    assert [row["participant"] for row in found] == ["codex", "claude"]


def test_rows_older_than_a_day_follow_todays_under_a_heading():
    fresh = problems._row(problems.OVERDUE, "late", "x y", 900, "claude")
    old = problems._row(problems.BOUNCE, "bounced", "x y", 200000, "codex")
    listed = problems.lines([fresh, old])
    assert listed[1] == problems.STALE_HEADING
    assert "claude" in listed[0] and "codex" in listed[2]
    assert problems.STALE_HEADING not in problems.lines([fresh])


@pytest.mark.parametrize("provider", ["gemini", "amp"])
def test_a_watcher_inferred_block_is_marked_and_still_asks_the_operator(
    bridge, repo, served, provider
):
    manifest = bridge.add_participant(repo, "helper", provider)
    directory = bridge.project(repo)[1]
    alive(
        directory,
        "helper",
        activity="dialog: native tool permission prompt",
        dialog={
            "name": dialogs.PERMISSION,
            "tool": "Bash",
            "since": time.time() - 7200,
        },
    )
    store.initialize(bridge.home)
    with store.connect(bridge.home, write=True) as db:
        lanes.transition(
            db, manifest["root"], "helper", lanes.IDLE, evidence="Stop: idle"
        )
    config = supervision.configuration(bridge.home, manifest)
    readings = {
        name: supervision.presence(directory, name, config["inactive_after"])
        for name in manifest["participants"]
    }
    supervision.settle_lanes(
        bridge.home, manifest, config, readings, directory=directory
    )
    [lane] = [
        record
        for record in bridge.status_snapshot()["projects"][0]["participants"]
        if record["participant"] == "helper"
    ]
    assert lane["condition"]["state"] == lanes.BLOCKED
    assert lane["condition"]["cause"] == lanes.APPROVAL
    assert lane["provenance"] == {
        "source": "dialog watcher",
        "inferred": True,
        "gap": ["PermissionRequest"],
    }
    note = "inferred by dialog watcher, no PermissionRequest hook"
    assert lane["session"].endswith(note)
    [row] = rows(bridge, problems.APPROVAL)
    assert row["participant"] == "helper"
    assert row["detail"] == (
        f"the client is waiting for approval of Bash ({note})"
    )


def test_a_hooked_client_is_left_to_its_hooks_and_shows_no_inference(
    bridge, repo, paired, served
):
    directory = bridge.project(repo)[1]
    alive(
        directory,
        "claude",
        activity="waiting for approval: Bash",
        dialog={
            "name": dialogs.PERMISSION,
            "tool": "Bash",
            "since": time.time() - 7200,
        },
    )
    store.initialize(bridge.home)
    with store.connect(bridge.home, write=True) as db:
        lanes.transition(
            db,
            paired["root"],
            "claude",
            lanes.BLOCKED,
            cause=lanes.APPROVAL,
            evidence="PermissionRequest: approval",
        )
    watched = supervision.watched_dialog(
        bridge.home,
        directory,
        paired["participants"]["claude"],
        "claude",
        {"state": lanes.BLOCKED},
        {"process_alive": True},
    )
    assert watched is None
    [lane] = [
        record
        for record in bridge.status_snapshot()["projects"][0]["participants"]
        if record["participant"] == "claude"
    ]
    assert lane["provenance"] == {
        "source": "hook",
        "inferred": False,
        "gap": [],
    }
    [row] = rows(bridge, problems.APPROVAL)
    assert row["detail"] == "the client is waiting for approval of Bash"


def test_only_a_hook_the_client_lacks_is_named_as_the_gap():
    idle = {"state": lanes.IDLE, "cause": "", "evidence": "liveness: idle"}
    assert lanes.provenance(idle, ["Stop"])["gap"] == ["Stop"]
    assert lanes.provenance(idle, [])["gap"] == []
    working = {**idle, "state": lanes.WORKING}
    assert lanes.provenance(working, ["UserPromptSubmit"])["gap"] == []
    held = {
        "state": lanes.BLOCKED,
        "cause": lanes.DIALOG,
        "evidence": "dialog: native directory trust prompt",
    }
    assert lanes.provenance(held, ["PermissionRequest"])["gap"] == []
    assert lanes.inference(lanes.provenance(held, [])) == (
        "inferred by dialog watcher"
    )
    assert lanes.provenance(None) is None


def aging_record(name, state, age, claims=()):
    """Shapes the fields of one status lane record that folding reads."""
    return {
        "participant": name,
        "lane_state": state,
        "claims": list(claims),
        "report_age_seconds": age,
        "availability": {"process_alive": False, "age_seconds": age},
    }


def folded_rows():
    """Derives rows for one long-dead lane and one live lane, then folds."""
    project = {
        "participants": [
            aging_record("claude", "dead", 3 * 86400),
            aging_record("codex", "working", 30),
        ]
    }
    stale = problems._row(problems.WAKE, "woken", "x y", 300000, "claude")
    recent = problems._row(problems.DIRTY, "a.txt", "x y", 600, "claude")
    live = problems._row(problems.CHECKS, "ci", "x y", 200000, "codex")
    rows = [stale, recent, live]
    problems._fold(rows, project, 86400)
    return stale, recent, live


def test_old_rows_of_a_long_dead_lane_fold_and_a_live_problem_stays():
    stale, recent, live = folded_rows()
    assert stale["folded"] == 86400
    assert "folded" not in recent and "folded" not in live
    listed = problems.lines([live, stale, recent])
    assert not any("woken" in line for line in listed)
    assert any("ci" in line and "codex" in line for line in listed)
    assert any("a.txt" in line for line in listed)
    assert listed[-1] == (
        "1 dead-lane problem older than 24h hidden; --all shows them"
    )
    assert problems.shown([live, stale]) == [live]


def test_problems_all_lists_folded_rows_without_the_summary():
    stale, recent, live = folded_rows()
    listed = problems.lines([live, stale, recent], everything=True)
    assert any("woken" in line for line in listed)
    assert not any("hidden; --all" in line for line in listed)
    document = problems.rendered([live, stale, recent])
    assert document["count"] == 2 and document["folded"] == 1
    assert problems.rendered([live, stale], everything=True)["count"] == 2


def test_only_folded_rows_print_the_summary_alone():
    stale, _, _ = folded_rows()
    assert problems.lines([stale]) == [
        "1 dead-lane problem older than 24h hidden; --all shows them"
    ]


def test_a_dead_lane_holding_a_claim_keeps_its_old_rows():
    project = {
        "participants": [
            aging_record("claude", "dead", 3 * 86400, [{"issue": 42}])
        ]
    }
    row = problems._row(problems.WAKE, "woken", "x y", 300000, "claude")
    problems._fold([row], project, 86400)
    assert "folded" not in row
    assert any(line.endswith("woken; x y") for line in problems.lines([row]))
