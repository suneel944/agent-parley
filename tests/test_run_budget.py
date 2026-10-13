"""Checks the opt-in run budget that gates wakes, dispatch and launches."""

import datetime
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

import pytest

from agent_parley import (
    budgets,
    checkpoints,
    cli,
    lanes,
    problems,
    process,
    records,
    roster,
    store,
    supervision,
    terminal,
)
from agent_parley.checkpoints import checkpoint
from agent_parley.state import BridgeError, lock, write_json


def transcript(lane, tokens, name="session.jsonl"):
    """Writes a Claude transcript whose client counts that many tokens."""
    directory = (
        Path(os.environ["CLAUDE_CONFIG_DIR"])
        / "projects"
        / re.sub(r"[^A-Za-z0-9]", "-", str(lane))
    )
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "id": f"msg-{name}",
                    "usage": {"input_tokens": tokens, "output_tokens": 0},
                },
            }
        )
        + "\n"
    )
    return path


def actors(bridge, paired):
    """Registers both lanes and returns their authenticated identities."""
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


def serve(bridge, actor, count):
    """Serves that many coordination calls to one lane."""
    for _ in range(count):
        store.call(bridge.home, actor, "list_participants", {})


def enforced(bridge, repo, **limits):
    """Records a run budget and returns the directory and manifest."""
    bridge.enforce(repo, limits)
    directory = bridge.project(repo, create=False)[1]
    return directory, roster.read(directory)


def idle_codex(bridge, paired, monkeypatch, pending=True):
    """Leaves codex idle, optionally with mail, and records terminal wakes."""
    identities = actors(bridge, paired)
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
    if pending:
        store.call(
            bridge.home,
            identities["claude"],
            "send_message",
            {
                "to": ["codex"],
                "subject": "Review",
                "body_md": "Review the result",
                "idempotency_key": "pending",
                "ack_required": True,
            },
        )
    calls = []
    monkeypatch.setattr(
        terminal, "request", lambda *args: calls.append(args) or "accepted"
    )
    config = {**supervision.DEFAULTS, "inactive_after": 1}
    observed = supervision.presence(lane.parent, "codex", 1)
    with store.connect(bridge.home, write=True) as db:
        lanes.sample(
            db,
            paired["root"],
            "codex",
            observed,
            dead_after=supervision.DEFAULTS["stalled_after"],
        )
    return calls, config, observed


def main(monkeypatch, bridge, *arguments):
    """Runs the command line against the test state root."""
    monkeypatch.setattr(
        sys, "argv", ["agent-parley", "--home", str(bridge.home), *arguments]
    )
    return cli.main()


def test_budgets_stay_advisory_until_a_run_budget_is_recorded(
    bridge, repo, paired
):
    directory = bridge.project(repo, create=False)[1]
    bridge.budget(repo, "project", "", {"calls": 1})
    serve(bridge, actors(bridge, paired)["claude"], 3)
    manifest = roster.read(directory)
    assert "run_budget" not in manifest
    assert budgets.admit(directory, manifest, "claude") == ""
    assert not (directory / budgets.LEDGER).exists()
    assert "enforces no run budget" in bridge.enforce(repo)
    bridge.enforce(repo, {"calls": 5, "tokens": None, "hours": None})
    assert roster.read(directory)["run_budget"] == {"calls": 5}
    assert (directory / budgets.LEDGER).exists()
    bridge.enforce(repo, {"calls": 0, "tokens": None, "hours": None})
    assert "run_budget" not in roster.read(directory)


def test_concurrent_admissions_share_one_allowance(
    bridge, repo, paired, monkeypatch
):
    identities = actors(bridge, paired)
    directory, manifest = enforced(bridge, repo, calls=5)
    serve(bridge, identities["claude"], 2)
    serve(bridge, identities["codex"], 3)
    escalations = []
    monkeypatch.setattr(
        budgets.notify,
        "deliver",
        lambda *args: escalations.append(args) or "",
    )
    refusals: list[str] = []

    def poll_then_admit(name):
        budgets.account(bridge.home, directory, manifest)
        refusals.append(budgets.admit(directory, manifest, name))

    threads = [
        threading.Thread(target=poll_then_admit, args=(name,))
        for name in ("claude", "codex") * 4
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(refusals) == 8
    assert all(
        refusal.startswith("run budget exhausted") for refusal in refusals
    )
    assert len(escalations) == 1
    ledger = json.loads((directory / budgets.LEDGER).read_text())
    assert ledger["calls"]["total"] == 5
    assert ledger["exhausted"]["fields"] == ["calls"]


def test_concurrent_accounting_never_double_counts(bridge, repo, paired):
    identities = actors(bridge, paired)
    directory, manifest = enforced(bridge, repo, calls=100)
    serve(bridge, identities["claude"], 3)
    threads = [
        threading.Thread(
            target=budgets.account, args=(bridge.home, directory, manifest)
        )
        for _ in range(6)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    reading = budgets.account(bridge.home, directory, manifest)
    assert reading["used"]["calls"] == 3
    assert reading["exhausted"] is None


def test_exhaustion_blocks_wakes_and_retries_without_spending_attempts(
    bridge, repo, paired, monkeypatch
):
    enforced(bridge, repo, calls=1000)
    calls, config, observed = idle_codex(bridge, paired, monkeypatch)
    directory, manifest = enforced(bridge, repo, calls=1000)
    supervision.wake(
        bridge.home, directory, manifest, "codex", observed, config
    )
    assert len(calls) == 1
    record = supervision.wake_record(bridge.home, paired["root"], "codex")
    supervision.store_wake(
        bridge.home, directory, paired["root"], "codex", {**record, "at": 0}
    )
    directory, manifest = enforced(bridge, repo, calls=1)
    supervision.wake(
        bridge.home, directory, manifest, "codex", observed, config
    )
    assert len(calls) == 1
    parked = supervision.wake_record(bridge.home, paired["root"], "codex")
    assert parked["attempts"] == 1
    assert parked["blocked"].startswith("run budget exhausted")
    assert budgets.halted(directory, manifest)["fields"] == ["calls"]


def test_exhaustion_survives_restart_and_is_one_actionable_record(
    bridge, repo, paired
):
    lane = Path(paired["lanes"]["claude"])
    directory, manifest = enforced(bridge, repo, tokens=500)
    transcript(lane, 600)
    first = budgets.account(bridge.home, directory, manifest)
    assert first["used"]["tokens"] == 600
    second = budgets.account(bridge.home, directory, manifest)
    assert second["used"]["tokens"] == 600
    assert second["exhausted"]["at"] == first["exhausted"]["at"]
    rows = problems._run_rows(directory, manifest, paired["root"], time.time())
    assert [row["condition"] for row in rows] == [problems.RUN_BUDGET]
    assert "agent-parley budget resume" in rows[0]["command"]


def test_lane_replacement_and_retries_keep_counting(bridge, repo, paired):
    lane = Path(paired["lanes"]["claude"])
    directory, manifest = enforced(bridge, repo, tokens=10_000)
    old = transcript(lane, 300)
    assert (
        budgets.account(bridge.home, directory, manifest)["used"]["tokens"]
        == 300
    )
    old.unlink()
    transcript(lane, 200, "retry.jsonl")
    assert (
        budgets.account(bridge.home, directory, manifest)["used"]["tokens"]
        == 500
    )
    replaced = {
        **manifest,
        "participants": {
            name: participant
            for name, participant in manifest["participants"].items()
            if name != "claude"
        },
    }
    assert (
        budgets.account(bridge.home, directory, replaced)["used"]["tokens"]
        == 500
    )


def test_missing_or_corrupt_usage_never_grants_allowance(bridge, repo, paired):
    transcript(Path(paired["lanes"]["claude"]), 10)
    directory, manifest = enforced(bridge, repo, tokens=1000)
    assert budgets.admit(directory, manifest, "claude") == ""
    refused = budgets.admit(directory, manifest, "codex")
    assert "cannot be metered" in refused
    (directory / budgets.LEDGER).write_text("{not json")
    refused = budgets.admit(directory, manifest, "claude")
    assert refused.startswith("run budget exhausted")
    assert "unreadable" in refused
    budgets.account(bridge.home, directory, manifest)
    assert (directory / f"{budgets.LEDGER}.corrupt").read_text() == "{not json"
    assert "unreadable" in budgets.halted(directory, manifest)["cause"]
    assert budgets.admit(directory, manifest, "claude")
    (directory / budgets.LEDGER).write_text("[]")
    with pytest.raises(BridgeError, match="unreadable"):
        budgets.resume(bridge.home, directory, manifest)
    assert "Resumed" in budgets.resume(
        bridge.home, directory, manifest, reset=True
    )
    assert budgets.admit(directory, manifest, "claude") == ""


def test_a_deleted_ledger_reads_as_exhausted_not_as_none(bridge, repo, paired):
    identities = actors(bridge, paired)
    directory, manifest = enforced(bridge, repo, calls=100)
    serve(bridge, identities["claude"], 3)
    budgets.account(bridge.home, directory, manifest)
    ledger = json.loads((directory / budgets.LEDGER).read_text())
    assert ledger["calls"]["total"] == 3
    assert budgets.halted(directory, manifest) is None
    (directory / budgets.LEDGER).unlink()
    assert "missing" in budgets.halted(directory, manifest)["cause"]
    assert budgets.admit(directory, manifest, "claude").startswith(
        "run budget exhausted"
    )
    reading = budgets.account(bridge.home, directory, manifest)
    assert reading["used"]["calls"] == 3
    assert "missing" in reading["exhausted"]["cause"]
    assert "missing" in budgets.halted(directory, manifest)["cause"]
    assert "missing" in budgets.admit(directory, manifest, "claude")


def test_reset_on_an_unreadable_ledger_starts_from_the_present(
    bridge, repo, paired
):
    serve(bridge, actors(bridge, paired)["claude"], 3)
    directory, manifest = enforced(bridge, repo, calls=100)
    (directory / budgets.LEDGER).write_text("{not json")
    budgets.resume(bridge.home, directory, manifest, reset=True)
    ledger = json.loads((directory / budgets.LEDGER).read_text())
    assert ledger["calls"]["total"] == 0
    assert ledger["reset"] is True
    reading = budgets.account(bridge.home, directory, manifest)
    assert reading["used"]["calls"] == 0
    assert reading["exhausted"] is None


def test_concurrent_admissions_at_the_last_call_admit_exactly_one(
    bridge, repo, paired
):
    identities = actors(bridge, paired)
    directory, manifest = enforced(bridge, repo, calls=5)
    serve(bridge, identities["claude"], 4)
    budgets.account(bridge.home, directory, manifest)
    refusals: list[str] = []
    threads = [
        threading.Thread(
            target=lambda name=name: refusals.append(
                budgets.admit(directory, manifest, name)
            )
        )
        for name in ("claude", "codex") * 4
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(refusals) == 8
    assert refusals.count("") == 1
    assert all(
        "reserved by turns already admitted" in refusal
        for refusal in refusals
        if refusal
    )
    ledger = json.loads((directory / budgets.LEDGER).read_text())
    assert ledger["reserved"] == 1
    reading = budgets.account(bridge.home, directory, manifest)
    assert reading["exhausted"] is None
    assert budgets.admit(directory, manifest, "claude") == ""


def test_a_busy_ledger_parks_the_wake_instead_of_raising(
    bridge, repo, paired, monkeypatch
):
    calls, config, observed = idle_codex(bridge, paired, monkeypatch)
    directory, manifest = enforced(bridge, repo, calls=1000)
    monkeypatch.setattr(budgets, "LOCK_SECONDS", 0.05)
    with lock(directory / "run-budget.lock"):
        supervision.wake(
            bridge.home, directory, manifest, "codex", observed, config
        )
    assert calls == []
    parked = supervision.wake_record(bridge.home, paired["root"], "codex")
    assert "ledger stayed busy" in parked["blocked"]
    supervision.wake(
        bridge.home, directory, manifest, "codex", observed, config
    )
    assert len(calls) == 1


def test_a_first_refusal_for_an_unmetered_lane_is_parked_and_shown(
    bridge, repo, paired, monkeypatch
):
    calls, config, observed = idle_codex(bridge, paired, monkeypatch)
    transcript(Path(paired["lanes"]["claude"]), 10)
    directory, manifest = enforced(bridge, repo, tokens=1000)
    assert supervision.wake_record(bridge.home, paired["root"], "codex") == {}
    supervision.wake(
        bridge.home, directory, manifest, "codex", observed, config
    )
    assert calls == []
    parked = supervision.wake_record(bridge.home, paired["root"], "codex")
    assert "cannot be metered" in parked["blocked"]
    rows = problems._run_rows(directory, manifest, paired["root"], time.time())
    assert [(row["condition"], row["participant"]) for row in rows] == [
        (problems.RUN_UNMETERED, "codex")
    ]


def test_in_flight_lanes_are_asked_to_checkpoint_and_stop(bridge, repo, paired):
    identities = actors(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    directory = lane.parent
    (lane / "unsaved.txt").write_text("work in progress\n")
    write_json(
        directory / "claude-activity.json",
        {
            "session_pid": os.getpid(),
            "session_ticks": process.start_ticks(os.getpid()),
            "activity": "working",
            "updated": time.time(),
        },
    )
    write_json(directory / "claude-identity.json", {"name": "claude"})
    _, manifest = enforced(bridge, repo, calls=1)
    serve(bridge, identities["claude"], 2)
    budgets.account(bridge.home, directory, manifest)
    event = {
        "session_id": "claude-run",
        "cwd": str(lane),
        "tool_name": "Read",
        "tool_input": {"file_path": str(lane / "shared.txt")},
    }
    reply = checkpoint(
        bridge.home,
        directory,
        "claude",
        {**event, "hook_event_name": "PreToolUse"},
    )
    details = reply["hookSpecificOutput"]
    assert "Run budget exhausted" in details["additionalContext"]
    assert "permissionDecision" not in details
    write_json(directory / "issues.json", {"revision": 1, "issues": {}})
    stop = {
        "hook_event_name": "Stop",
        "session_id": "claude-run",
        "cwd": str(lane),
    }
    assert checkpoint(bridge.home, directory, "claude", stop) == {}
    assert (lane / "unsaved.txt").read_text() == "work in progress\n"
    budgets.resume(bridge.home, directory, manifest, reset=True)
    write_json(directory / "issues.json", {"revision": 2, "issues": {}})
    assert (
        checkpoint(bridge.home, directory, "claude", stop).get("decision")
        == "block"
    )


def test_only_the_operator_resumes_or_raises_the_run(
    bridge, repo, paired, monkeypatch, capsys
):
    identities = actors(bridge, paired)
    directory, manifest = enforced(bridge, repo, calls=2)
    serve(bridge, identities["claude"], 3)
    budgets.account(bridge.home, directory, manifest)
    with pytest.raises(BridgeError, match="run budget is exhausted"):
        bridge.launch("claude", repo, "continue")
    monkeypatch.setenv("AGENT_PARLEY_TOKEN", "lane-token")
    assert main(monkeypatch, bridge, "budget", "resume", "--repo", str(repo))
    assert main(
        monkeypatch,
        bridge,
        "budget",
        "enforce",
        "--calls",
        "100",
        "--repo",
        str(repo),
    )
    assert "operator authority" in capsys.readouterr().err
    assert budgets.halted(directory, roster.read(directory))
    monkeypatch.delenv("AGENT_PARLEY_TOKEN")
    assert main(monkeypatch, bridge, "budget", "resume", "--repo", str(repo))
    assert "still at or over" in capsys.readouterr().err
    assert not main(
        monkeypatch,
        bridge,
        "budget",
        "enforce",
        "--calls",
        "100",
        "--repo",
        str(repo),
    )
    assert budgets.halted(directory, roster.read(directory))
    assert not main(
        monkeypatch, bridge, "budget", "resume", "--repo", str(repo)
    )
    assert "Resumed the run" in capsys.readouterr().out
    manifest = roster.read(directory)
    assert budgets.halted(directory, manifest) is None
    assert budgets.admit(directory, manifest, "claude") == ""
    bridge.enforce(repo, {"calls": 3, "tokens": None, "hours": None})
    manifest = roster.read(directory)
    assert budgets.admit(directory, manifest, "claude")
    assert "new accounting period" in budgets.resume(
        bridge.home, directory, manifest, reset=True
    )
    assert budgets.admit(directory, manifest, "claude") == ""


def live_session(directory, name, **fields):
    """Records a live native session for one lane, owned by this process."""
    write_json(
        directory / f"{name}-activity.json",
        {
            "activity": "working",
            "updated": time.time(),
            "session_pid": os.getpid(),
            "session_ticks": process.start_ticks(os.getpid()),
            **fields,
        },
    )


def append(path, tokens, identifier):
    """Appends one more reported message to a transcript."""
    with path.open("a") as handle:
        handle.write(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "id": identifier,
                        "usage": {"input_tokens": tokens},
                    },
                }
            )
            + "\n"
        )


def test_hours_count_a_run_session_and_refuse_an_unstarted_one(
    bridge, repo, paired
):
    directory, manifest = enforced(bridge, repo, hours=100)
    live_session(directory, "claude", session_started=time.time() - 3600)
    live_session(directory, "codex")
    reading = budgets.account(bridge.home, directory, manifest)
    assert 0.99 <= reading["used"]["hours"] <= 1.01
    assert reading["missing"] == ["codex"]
    assert budgets.admit(directory, manifest, "claude") == ""
    assert "cannot be metered" in budgets.admit(directory, manifest, "codex")
    rows = problems._run_rows(directory, manifest, paired["root"], time.time())
    assert [row["participant"] for row in rows] == ["codex"]


def test_every_transcript_is_read_to_its_end_from_a_durable_offset(
    bridge, repo, paired
):
    lane = Path(paired["lanes"]["claude"])
    history = transcript(lane, 999, "history.jsonl")
    os.utime(history, (time.time() - 3600, time.time() - 3600))
    directory, manifest = enforced(bridge, repo, tokens=10_000)
    first = transcript(lane, 100, "first.jsonl")
    assert (
        budgets.account(bridge.home, directory, manifest)["used"]["tokens"]
        == 100
    )
    append(first, 50, "msg-late")
    second = transcript(lane, 20, "second.jsonl")
    os.utime(first, (time.time() - 60, time.time() - 60))
    transcript(lane, 5, "offline.jsonl")
    reading = budgets.account(bridge.home, directory, manifest)
    assert reading["used"]["tokens"] == 175
    append(second, 7, "msg-second.jsonl")
    assert (
        budgets.account(bridge.home, directory, manifest)["used"]["tokens"]
        == 175
    )
    append(second, 3, "msg-next")
    assert (
        budgets.account(bridge.home, directory, manifest)["used"]["tokens"]
        == 178
    )


def test_a_limit_added_later_never_counts_earlier_tokens(bridge, repo, paired):
    enforced(bridge, repo, calls=1_000)
    live = transcript(Path(paired["lanes"]["claude"]), 900, "live.jsonl")
    directory, manifest = enforced(bridge, repo, tokens=10_000)
    assert (
        budgets.account(bridge.home, directory, manifest)["used"]["tokens"] == 0
    )
    append(live, 7, "msg-after")
    assert (
        budgets.account(bridge.home, directory, manifest)["used"]["tokens"] == 7
    )


def test_a_limit_added_later_never_counts_earlier_hours(bridge, repo, paired):
    directory, _ = enforced(bridge, repo, calls=1_000)
    live_session(directory, "claude", session_started=time.time() - 3600)
    directory, manifest = enforced(bridge, repo, hours=100)
    reading = budgets.account(bridge.home, directory, manifest)
    assert reading["used"]["hours"] < 0.01


def test_first_enforcement_counts_only_growth_of_a_live_transcript(
    bridge, repo, paired
):
    live = transcript(Path(paired["lanes"]["claude"]), 900, "live.jsonl")
    directory, manifest = enforced(bridge, repo, tokens=10_000)
    append(live, 7, "msg-after")
    assert (
        budgets.account(bridge.home, directory, manifest)["used"]["tokens"] == 7
    )


def test_a_re_enforced_hours_limit_keeps_hours_already_counted(
    bridge, repo, paired
):
    directory, manifest = enforced(bridge, repo, calls=1_000, hours=100)
    live_session(directory, "claude", session_started=time.time() - 3600)
    counted = budgets.account(bridge.home, directory, manifest)["used"]
    bridge.enforce(repo, {"hours": 0})
    directory, manifest = enforced(bridge, repo, hours=100)
    reading = budgets.account(bridge.home, directory, manifest)
    assert reading["used"]["hours"] >= counted["hours"] > 0.99


def test_a_reset_after_a_lost_ledger_never_rereads_history(
    bridge, repo, paired
):
    big = transcript(Path(paired["lanes"]["claude"]), 1, "big.jsonl")
    with big.open("a") as handle:
        for number in range(2 * records.MAX_READ // 100):
            handle.write(
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "id": f"msg-{number}",
                            "usage": {"input_tokens": 1},
                            "pad": "x" * 40,
                        },
                    }
                )
                + "\n"
            )
    directory, manifest = enforced(bridge, repo, tokens=10**9)
    (directory / budgets.LEDGER).unlink()
    assert budgets.account(bridge.home, directory, manifest)["exhausted"]
    budgets.resume(bridge.home, directory, manifest, reset=True)
    for _ in range(3):
        reading = budgets.account(bridge.home, directory, manifest)
        assert reading["used"]["tokens"] == 0
        assert reading["exhausted"] is None
    append(big, 5, "msg-after")
    assert (
        budgets.account(bridge.home, directory, manifest)["used"]["tokens"] == 5
    )


def test_a_line_longer_than_one_read_is_skipped_not_stalled_on(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(records, "MAX_READ", 64)
    path = tmp_path / "long.jsonl"
    lines = [
        {"usage": {"input_tokens": 1000}, "pad": "x" * 300},
        {"usage": {"input_tokens": 3}},
    ]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))

    def fold(record, reading):
        reading["tokens"] += record["usage"]["input_tokens"]

    reading = {}
    for _ in range(20):
        reading = records._advance(path, fold, reading)
    assert reading["offset"] == path.stat().st_size
    assert reading["tokens"] == 3


def test_a_hand_started_session_never_inherits_a_run_start(
    bridge, repo, paired
):
    lane = Path(paired["lanes"]["claude"])
    directory = lane.parent
    write_json(directory / "claude-identity.json", {"name": "claude"})
    live_session(
        directory,
        "claude",
        session_id="launched",
        session_pid=2**22 + 1,
        session_ticks=1,
        session_started=time.time() - 7200,
    )
    _, manifest = enforced(bridge, repo, hours=100)
    checkpoint(
        bridge.home,
        directory,
        "claude",
        {
            "hook_event_name": "SessionStart",
            "session_id": "by-hand",
            "cwd": str(lane),
        },
        session_process=process.ServerProcess(
            os.getpid(), process.start_ticks(os.getpid())
        ),
    )
    state = checkpoints.activity(directory, "claude")
    assert state["session_pid"] == os.getpid()
    assert "session_started" not in state
    reading = budgets.account(bridge.home, directory, manifest)
    assert reading["used"]["hours"] == 0
    assert reading["missing"] == ["claude"]


def test_calls_served_before_enforcement_never_count(bridge, repo, paired):
    identities = actors(bridge, paired)
    serve(bridge, identities["claude"], 3)
    directory, manifest = enforced(bridge, repo, calls=100)
    assert (
        budgets.account(bridge.home, directory, manifest)["used"]["calls"] == 0
    )
    serve(bridge, identities["codex"], 2)
    assert (
        budgets.account(bridge.home, directory, manifest)["used"]["calls"] == 2
    )


def test_stop_continuation_is_admitted_through_the_run_budget(
    bridge, repo, paired
):
    actors(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    directory = lane.parent
    live_session(directory, "claude")
    write_json(directory / "claude-identity.json", {"name": "claude"})
    stop = {
        "hook_event_name": "Stop",
        "session_id": "claude-run",
        "cwd": str(lane),
    }
    _, manifest = enforced(bridge, repo, tokens=1000)
    assert budgets.unmetered(directory, manifest) == ["claude", "codex"]
    write_json(directory / "issues.json", {"revision": 1, "issues": {}})
    assert checkpoint(bridge.home, directory, "claude", stop) == {}
    bridge.enforce(repo, {"tokens": 0, "calls": 5, "hours": None})
    manifest = roster.read(directory)
    budgets.account(bridge.home, directory, manifest)
    write_json(directory / "issues.json", {"revision": 2, "issues": {}})
    reply = checkpoint(bridge.home, directory, "claude", stop)
    assert reply.get("decision") == "block"
    ledger = json.loads((directory / budgets.LEDGER).read_text())
    assert ledger["reserved"] == 1


@pytest.mark.parametrize(
    ("command", "refused"),
    [
        ("env -u AGENT_PARLEY_TOKEN agent-parley budget resume", True),
        ("AGENT_PARLEY_TOKEN= agent-parley budget enforce --calls 9", True),
        ("true && /usr/bin/agent-parley budget resume --reset", True),
        ("bash -c 'agent-parley budget enforce --tokens 0'", True),
        ("rtk proxy agent-parley budget --repo . resume", True),
        ("agent-parley --home /h budget resume", True),
        ("agent-parley --home=/h budget enforce --calls 9", True),
        (
            "env -u AGENT_PARLEY_TOKEN agent-parley --home /h budget resume",
            True,
        ),
        ("python3 -m agent_parley.cli --home /h budget resume", True),
        ("agent-parley --home /h budget show", False),
        ("agent-parley --home budget status", False),
        ("agent-parley budget show", False),
        ("agent-parley status", False),
        ("echo budget resume", False),
    ],
)
def test_a_lane_cannot_lift_its_own_run_budget(
    bridge, repo, paired, command, refused
):
    lane = Path(paired["lanes"]["claude"])
    payload = {
        "hook_event_name": "PreToolUse",
        "session_id": "claude-run",
        "cwd": str(lane),
        "tool_name": "Bash",
        "tool_input": {"command": command},
    }
    assert checkpoints.lifts_run_budget(payload) is refused
    if refused:
        reply = checkpoint(bridge.home, lane.parent, "claude", payload)
        details = reply["hookSpecificOutput"]
        assert details["permissionDecision"] == "deny"
        assert (
            "not an authority boundary" in (details["permissionDecisionReason"])
        )


def test_an_exhausted_run_registers_no_new_lane(bridge, repo, paired):
    identities = actors(bridge, paired)
    directory, manifest = enforced(bridge, repo, calls=1)
    serve(bridge, identities["claude"], 1)
    budgets.account(bridge.home, directory, manifest)
    with pytest.raises(BridgeError, match="run budget is exhausted"):
        bridge.launch("gemini", repo, "start", "claude")
    assert "gemini" not in roster.read(directory)["participants"]


def test_a_half_written_rollout_is_matched_once_complete(tmp_path):
    lane = tmp_path / "lane"
    day = datetime.date.today()
    folder = tmp_path / "sessions" / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}"
    folder.mkdir(parents=True)
    rollout = folder / "rollout-partial.jsonl"
    rollout.write_text('{"payload": {"cw')
    assert records._codex_sources(tmp_path, lane) == []
    rollout.write_text(json.dumps({"payload": {"cwd": str(lane)}}) + "\n")
    assert records._codex_sources(tmp_path, lane) == [rollout]
