"""Exercises liveness, reminders and bounded wake decisions in local state."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent_parley import (
    cli,
    issues,
    process,
    roster,
    store,
    supervision,
    terminal,
)
from agent_parley.checkpoints import mailbox
from agent_parley.state import write_json


def registered(bridge, paired):
    store.initialize(bridge.home)
    return {
        name: store.authenticate(
            bridge.home,
            store.register(bridge.home, paired["root"], name)[
                "registration_token"
            ],
        )
        for name in ("claude", "codex")
    }


def send(bridge, actor, recipient, key="pending"):
    return store.call(
        bridge.home,
        actor,
        "send_message",
        {
            "to": [recipient],
            "subject": "Review",
            "body_md": "Review the result",
            "idempotency_key": key,
            "ack_required": True,
        },
    )


def test_presence_separates_an_idle_lane_from_a_stopped_one(tmp_path):
    write_json(
        tmp_path / "lane-activity.json",
        {
            "session_pid": os.getpid(),
            "session_ticks": process.start_ticks(os.getpid()),
            "updated": time.time() - 60,
            "activity": "working",
        },
    )
    assert (
        supervision.presence(tmp_path, "lane", 100)["state"]
        == supervision.ACTIVE
    )
    quiet = supervision.presence(tmp_path, "lane", 30)
    assert quiet["state"] == supervision.IDLE and quiet["process_alive"]
    write_json(tmp_path / "lane-activity.json", {"updated": time.time()})
    stopped = supervision.presence(tmp_path, "lane")
    assert stopped["state"] == supervision.STOPPED
    assert not stopped["process_alive"]


def test_send_reports_unreachable_and_status_lists_ack_age(
    bridge, paired, capsys
):
    actors = registered(bridge, paired)
    directory = Path(paired["lanes"]["claude"]).parent
    supervision.poll(bridge.home, directory)
    message = send(bridge, actors["claude"], "codex")
    warning = message["recipient_warnings"][0]
    assert warning["recipient"] == "codex"
    assert warning["state"] == "unreachable"
    assert warning["summary"] == "queued for codex (unreachable)"
    pending = mailbox(bridge.home, paired["root"], "codex")["outstanding_ack"]
    assert pending[0]["id"] == message["id"]
    assert pending[0]["age_seconds"] >= 0
    bridge.status(cli.Selection(participant="codex"))
    assert "Awaiting acknowledgement: message" in capsys.readouterr().out


def test_send_reports_a_quiet_live_recipient_as_idle_not_unreachable(
    bridge, paired
):
    actors = registered(bridge, paired)
    directory = Path(paired["lanes"]["claude"]).parent
    write_json(
        directory / "codex-activity.json",
        {
            "activity": "idle",
            "updated": time.time() - 500,
            "session_pid": os.getpid(),
            "session_ticks": process.start_ticks(os.getpid()),
        },
    )
    supervision.poll(bridge.home, directory)
    lanes = bridge.status_snapshot()["projects"][0]["participants"]
    observed = {
        record["participant"]: record["availability"] for record in lanes
    }
    assert observed["codex"]["state"] == "idle"
    assert observed["codex"]["process_alive"]
    assert observed["claude"]["state"] == "stopped"
    warning = send(bridge, actors["claude"], "codex")["recipient_warnings"][0]
    assert warning["state"] == "idle"
    assert warning["summary"] == "queued for codex (idle; wake requested)"


def test_release_reminds_waiter_without_transferring_work(bridge, paired):
    actors = registered(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    peer = Path(paired["lanes"]["codex"])
    bridge.issue(lane, "claim", "1")
    bridge.issue(peer, "claim", "2")
    bridge.issue(peer, "block", "2", on="1")
    bridge.issue(lane, "release", "1")
    ledger = issues.snapshot(lane.parent)
    prompt = ledger["issues"]["1"]["handoff_prompt"]
    assert prompt["waiting"] == ["codex"]
    assert "unanswered" in issues.describe(ledger)
    supervision.reminders(lane.parent, roster.read(lane.parent), set())
    assert issues.snapshot(lane.parent)["revision"] == ledger["revision"]
    send(bridge, actors["claude"], "codex", "handoff")
    supervision.observe_responses(
        bridge.home, lane.parent, roster.read(lane.parent)
    )
    after = issues.snapshot(lane.parent)
    assert after["issues"]["1"]["owner"] is None
    assert after["issues"]["2"]["owner"] == "codex"
    assert after["issues"]["1"]["handoff_prompt"]["responded_at"]


def test_closed_pr_reminds_holder_and_preserves_claim(
    bridge, paired, monkeypatch
):
    registered(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    peer = Path(paired["lanes"]["codex"])
    bridge.issue(lane, "claim", "1")
    bridge.issue(peer, "claim", "2")
    bridge.issue(peer, "block", "2", on="1")
    monkeypatch.setattr(
        supervision.forge,
        "branch_completion",
        lambda *args: ("MERGED", time.time()),
    )
    supervision.poll(bridge.home, lane.parent)
    record = issues.snapshot(lane.parent)["issues"]["1"]
    assert record["owner"] == "claude"
    assert record["handoff_prompt"]["trigger"] == "pull request ended"


def test_live_idle_wakes_are_bounded_without_acknowledging(
    bridge, paired, monkeypatch
):
    actors = registered(bridge, paired)
    lane = Path(paired["lanes"]["codex"])
    write_json(
        lane.parent / "codex-activity.json",
        {
            "activity": "idle",
            "updated": time.time() - 500,
            "session_pid": os.getpid(),
            "session_ticks": process.start_ticks(os.getpid()),
        },
    )
    message = send(bridge, actors["claude"], "codex")
    calls = []
    monkeypatch.setattr(
        terminal, "request", lambda *args: calls.append(args) or "accepted"
    )
    config = {**supervision.DEFAULTS, "inactive_after": 1}
    observed = supervision.presence(lane.parent, "codex", 1)
    for _ in range(5):
        supervision.wake(
            bridge.home, lane.parent, paired, "codex", observed, config
        )
        path = lane.parent / "codex-wake.json"
        record = json.loads(path.read_text())
        record["at"] = 0
        write_json(path, record)
    assert len(calls) == 3
    assert (
        mailbox(bridge.home, paired["root"], "codex")["outstanding_ack"][0][
            "id"
        ]
        == message["id"]
    )


def test_a_busy_refusal_does_not_consume_a_bounded_attempt(
    bridge, paired, monkeypatch
):
    actors = registered(bridge, paired)
    lane = Path(paired["lanes"]["codex"])
    write_json(
        lane.parent / "codex-activity.json",
        {
            "activity": "idle",
            "updated": time.time() - 500,
            "session_pid": os.getpid(),
            "session_ticks": process.start_ticks(os.getpid()),
        },
    )
    send(bridge, actors["claude"], "codex")
    answers = iter(["busy"] * 4 + ["accepted"] * 5)
    calls = []
    monkeypatch.setattr(
        terminal,
        "request",
        lambda *args: calls.append(args) or next(answers),
    )
    config = {**supervision.DEFAULTS, "inactive_after": 1}
    observed = supervision.presence(lane.parent, "codex", 1)
    path = lane.parent / "codex-wake.json"
    for _ in range(9):
        supervision.wake(
            bridge.home, lane.parent, paired, "codex", observed, config
        )
        record = json.loads(path.read_text())
        record["at"] = 0
        write_json(path, record)
    assert len(calls) == 7
    assert json.loads(path.read_text())["attempts"] == 3


def test_permission_prompt_is_never_woken(bridge, paired, monkeypatch):
    registered(bridge, paired)
    directory = Path(paired["lanes"]["codex"]).parent
    write_json(
        directory / "codex-activity.json", {"activity": "waiting for approval"}
    )
    monkeypatch.setattr(
        terminal, "request", lambda *args: pytest.fail("woke approval")
    )
    supervision.wake(
        bridge.home, directory, paired, "codex", {}, supervision.DEFAULTS
    )


def test_unmanaged_hook_state_cannot_start_a_native_client(
    bridge, paired, monkeypatch
):
    actors = registered(bridge, paired)
    directory = Path(paired["lanes"]["codex"]).parent
    write_json(
        directory / "codex-activity.json",
        {"activity": "stopped", "session_id": "legacy-hook-session"},
    )
    send(bridge, actors["claude"], "codex")
    monkeypatch.setattr(
        supervision.subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("resumed unmanaged state"),
    )
    supervision.wake(
        bridge.home,
        directory,
        paired,
        "codex",
        {"process_alive": False},
        supervision.DEFAULTS,
    )
    record = json.loads((directory / "codex-wake.json").read_text())
    assert "manual attention" in record["result"]


def test_global_wake_opt_out_wins_over_project(bridge, paired, monkeypatch):
    registered(bridge, paired)
    directory = Path(paired["lanes"]["codex"]).parent
    write_json(bridge.home / "supervision.json", {"wake": False})
    manifest = roster.read(directory)
    manifest["supervision"] = {"wake": True}
    write_json(directory / "project.json", manifest)
    monkeypatch.setattr(
        supervision, "wake", lambda *args: pytest.fail("wake disabled")
    )
    supervision.poll(bridge.home, directory)


@pytest.mark.parametrize("provider", ["claude", "codex", "gemini"])
def test_stopped_resume_keeps_native_interactive_permissions(
    bridge, repo, tmp_path, monkeypatch, provider
):
    manifest = bridge.add_participant(repo, provider, provider)
    directory = Path(manifest["lanes"][provider]).parent
    write_json(
        directory / f"{provider}-activity.json",
        {
            "activity": "stopped",
            "session_id": "12345678-abcd-1234-abcd-123456789abc",
        },
    )
    monkeypatch.setattr(bridge, "up", lambda: None)

    async def identity(*args):
        return {"registration_token": "test-only"}

    monkeypatch.setattr(bridge, "identity", identity)
    from agent_parley import cli

    original = cli.shutil.which
    monkeypatch.setattr(
        cli.shutil,
        "which",
        lambda name: "/bin/true" if name == provider else original(name),
    )
    captured = []
    monkeypatch.setattr(
        terminal,
        "run",
        lambda command, *args, **kwargs: captured.append(command) or 0,
    )
    assert bridge.launch(provider, repo, terminal.PROMPT, resume=True) == 0
    assert "12345678-abcd-1234-abcd-123456789abc" in captured[0]
    assert "exec" not in captured[0] and "--print" not in captured[0]
    assert not any(
        "bypass" in argument or "skip-permission" in argument
        for argument in captured[0]
    )


def test_reaping_collects_an_exited_launcher_without_blocking():
    exited = subprocess.Popen(
        [sys.executable, "-c", "raise SystemExit(0)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    running = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        supervision.track_launcher(exited)
        supervision.track_launcher(running)
        exited.wait(timeout=10)
        deadline = time.monotonic() + 10
        while supervision.reap_launchers() > 1:
            assert time.monotonic() < deadline
            time.sleep(0.05)
        assert exited.returncode == 0
        assert running.poll() is None
        assert not zombies_of(os.getpid())
    finally:
        running.kill()
        running.wait(timeout=10)
        supervision.reap_launchers()


def zombies_of(parent):
    found = []
    if not Path("/proc").is_dir():
        return found
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
        except (OSError, IndexError):
            continue
        if fields[0] == "Z" and fields[1] == str(parent):
            found.append(int(entry.name))
    return found
