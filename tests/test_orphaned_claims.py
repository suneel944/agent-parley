"""Checks how a dead lane's claims are marked, announced and taken over."""

import json
import os
import shlex
import subprocess
import sys
import time
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agent_parley import (
    checkpoints,
    cli,
    dashboard,
    issues,
    lifecycle,
    process,
    recovery,
    roster,
    store,
    supervision,
    tables,
)
from agent_parley.cli import git
from agent_parley.state import BridgeError, write_json

STALLED = supervision.DEFAULTS["stalled_after"]


def registered(bridge, paired):
    """Registers both lanes so mail and reservations can be recorded."""
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


def running(directory, name, age=0.0):
    """Records a live session process for one lane."""
    write_json(
        directory / f"{name}-activity.json",
        {
            "activity": "working",
            "updated": time.time() - age,
            "session_id": f"{name}-session",
            "session_pid": os.getpid(),
            "session_ticks": process.start_ticks(os.getpid()),
        },
    )


def killed(directory, name, age):
    """Records a session process that started, was killed and then aged."""
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    ticks = process.start_ticks(child.pid)
    child.kill()
    child.wait()
    write_json(
        directory / f"{name}-activity.json",
        {
            "activity": "working",
            "updated": time.time() - age,
            "session_id": f"{name}-session",
            "session_pid": child.pid,
            "session_ticks": ticks,
        },
    )
    return child.pid


def reserve(bridge, actor, path):
    """Reserves one path for a lane through the served call path."""
    return store.call(
        bridge.home,
        actor,
        "file_reservation_paths",
        {"paths": [path], "reason": "orphan fixture"},
    )


def inbox(bridge, paired, name):
    """Returns the subjects and previews waiting for one lane."""
    listed = store.list_messages(bridge.home, paired["root"], name)
    return [
        (message["subject"], message["body_md"])
        for message in listed["messages"]
    ]


def test_a_killed_lane_is_orphaned_announced_and_taken_by_a_peer(
    bridge, repo, paired
):
    actors = registered(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    peer = Path(paired["lanes"]["codex"])
    directory = lane.parent
    bridge.issue(lane, "claim", "42")
    reserve(bridge, actors["claude"], "src/app.py")
    killed(directory, "claude", STALLED + 100)
    running(directory, "codex")

    supervision.poll(bridge.home, directory)

    record = issues.snapshot(directory)["issues"]["42"]
    assert record["owner"] == "claude"
    assert record["orphan"]["owner"] == "claude"
    assert record["orphan"]["reservations"] == ["src/app.py"]
    assert "no running session process" in record["orphan"]["reason"]
    listing = issues.describe(issues.snapshot(directory))
    assert "orphaned" in listing and "--take-orphaned" in listing
    subject, body = inbox(bridge, paired, "codex")[0]
    assert subject == "Orphaned claims held by claude"
    assert "#42" in body and "src/app.py" in body
    assert not inbox(bridge, paired, "claude")
    held = bridge.status_snapshot()["projects"][0]["participants"]
    reported = {lane["participant"]: lane for lane in held}
    assert reported["claude"]["claims"][0]["orphaned"] is True
    assert "#42*" in tables.status_row(reported["claude"], ())
    rows = {
        row["participant"]: row
        for row in dashboard.collect(bridge.home, False, {})["projects"][0][
            "rows"
        ]
    }
    assert rows["claude"]["orphaned"] == ["42"]
    assert "#42*" in rows["claude"]["issues"]
    assert "orphaned claims #42" in rows["claude"]["orphan"]
    assert lifecycle.actionable(issues.snapshot(directory), "claude") == []

    taken = bridge.issue(peer, "claim", "42", take_orphaned=True)

    assert taken["owner"] == "codex"
    assert taken["taken"]["from"] == "claude"
    assert "no running session process" in taken["taken"]["reason"]
    assert taken["reservations_moved"] == ["src/app.py"]
    assert "orphan" not in taken
    assert taken["history"][-1]["action"] == "take"
    assert lifecycle.actionable(issues.snapshot(directory), "codex") == ["42"]
    assert store.active_reservations(bridge.home, paired["root"]) == {
        actors["codex"]["name"]: ["src/app.py"]
    }


def test_a_native_exit_retains_the_generation_needed_for_recovery(
    bridge, repo, paired, monkeypatch
):
    registered(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    peer = Path(paired["lanes"]["codex"])
    directory = lane.parent
    activity_path = directory / "claude-activity.json"
    bridge.issue(lane, "claim", "42")
    child_identity = {}

    def exit_native(*args, **kwargs):
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        ticks = process.start_ticks(child.pid)
        child.kill()
        child.wait()
        child_identity.update(pid=child.pid, ticks=ticks)
        state = json.loads(activity_path.read_text())
        state.update(
            activity="working",
            session_id="native-session",
            session_pid=child.pid,
            session_ticks=ticks,
        )
        write_json(activity_path, state)
        return 0

    async def identity(*args):
        return {"registration_token": "test-scoped-credential"}

    monkeypatch.setattr(bridge, "up", lambda: None)
    monkeypatch.setattr(bridge, "identity", identity)
    monkeypatch.setattr(cli.shutil, "which", lambda command: sys.executable)
    monkeypatch.setattr(cli.subprocess, "call", exit_native)
    monkeypatch.setattr(
        cli.sys, "stdin", types.SimpleNamespace(isatty=lambda: False)
    )

    assert bridge.launch("claude", repo, "Continue recovery work") == 0
    activity = checkpoints.activity(directory, "claude")
    assert activity["activity"] == "stopped"
    assert activity["session_pid"] == child_identity["pid"]
    assert activity["session_ticks"] == child_identity["ticks"]

    activity["updated"] = time.time() - STALLED - 100
    write_json(activity_path, activity)
    running(directory, "codex")
    supervision.poll(bridge.home, directory)

    taken = bridge.issue(peer, "claim", "42", take_orphaned=True)
    assert taken["owner"] == "codex"
    assert taken["taken"]["from"] == "claude"


def test_takeover_restores_committed_staged_unstaged_and_untracked_work(
    bridge, repo, paired
):
    registered(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    peer = Path(paired["lanes"]["codex"])
    directory = lane.parent
    bridge.issue(lane, "claim", "42")
    (lane / ".gitignore").write_text("ignored-secret.bin\n")
    (lane / "committed.txt").write_text("committed\n")
    git(lane, "add", ".gitignore", "committed.txt")
    git(
        lane,
        "-c",
        "user.name=Bridge Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-m",
        "Work before interruption",
    )
    (lane / "staged.txt").write_text("staged\n")
    git(lane, "add", "staged.txt")
    (lane / "shared.txt").write_text("unstaged\n")
    (lane / "artifact.bin").write_bytes(b"\x00\xffrecovery")
    (lane / "ignored-secret.bin").write_bytes(b"secret")
    state = issues.snapshot(directory)
    state["issues"]["42"]["handoff"] = {
        "remaining": ["run make check"],
        "from": "earlier-owner",
    }
    write_json(directory / "issues.json", state)
    recovery.capture(
        directory,
        roster.read(directory),
        "claude",
        {
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"cmd": "pytest -q"},
            "tool_response": {"exit_code": 0},
        },
    )
    killed(directory, "claude", STALLED + 100)
    running(directory, "codex")
    exclude = Path(git(peer, "rev-parse", "--git-path", "info/exclude"))
    if not exclude.is_absolute():
        exclude = peer / exclude
    exclude.write_text(exclude.read_text() + "\n.pytest_cache/\n")
    cache = peer / ".pytest_cache"
    cache.mkdir()
    (cache / "README.md").write_text("recipient cache\n")

    supervision.poll(bridge.home, directory)
    taken = bridge.issue(peer, "claim", "42", take_orphaned=True)

    assert (peer / "committed.txt").read_text() == "committed\n"
    assert (peer / "staged.txt").read_text() == "staged\n"
    assert (peer / "shared.txt").read_text() == "unstaged\n"
    assert (peer / "artifact.bin").read_bytes() == b"\x00\xffrecovery"
    assert (lane / "ignored-secret.bin").read_bytes() == b"secret"
    assert not (peer / "ignored-secret.bin").exists()
    assert (cache / "README.md").read_text() == "recipient cache\n"
    assert git(peer, "log", "-1", "--format=%s") == "Work before interruption"
    status = git(peer, "status", "--short")
    assert "committed.txt" not in status
    assert git(peer, "diff", "--cached", "--name-only") == "staged.txt"
    assert git(peer, "diff", "--name-only") == "shared.txt"
    assert "?? artifact.bin" in status
    assert taken["handoff"]["remaining"] == ["run make check"]
    assert taken["recovery"]["remaining"] == ["run make check"]
    checkpoint = taken["taken"]["checkpoint"]
    assert checkpoint["artifact"]["bytes"] > 0
    assert len(checkpoint["artifact"]["sha256"]) == 64
    assert checkpoint["last_verified_step"] == "PostToolUse: Bash"
    assert checkpoint["gate"]["command"] == "pytest -q"
    assert checkpoint["gate"]["exit_code"] == 0

    refused = checkpoints.checkpoint(
        bridge.home,
        directory,
        "claude",
        {
            "hook_event_name": "PreToolUse",
            "session_id": "claude-session",
            "cwd": str(lane),
            "tool_name": "Write",
        },
    )
    assert refused["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert (
        "cannot resume edits"
        in refused["hookSpecificOutput"]["permissionDecisionReason"]
    )
    state = issues.snapshot(directory)
    state["issues"]["42"].update(owner="gemini", claim_id="a" * 16)
    write_json(directory / "issues.json", state)
    transferred_again = checkpoints.checkpoint(
        bridge.home,
        directory,
        "claude",
        {
            "hook_event_name": "PreToolUse",
            "session_id": "claude-session",
            "cwd": str(lane),
            "tool_name": "Write",
        },
    )
    assert (
        transferred_again["hookSpecificOutput"]["permissionDecision"] == "deny"
    )
    state["issues"]["42"].update(owner=None, claim_id="b" * 16)
    write_json(directory / "issues.json", state)
    released = checkpoints.checkpoint(
        bridge.home,
        directory,
        "claude",
        {
            "hook_event_name": "PreToolUse",
            "session_id": "claude-session",
            "cwd": str(lane),
            "tool_name": "Write",
        },
    )
    assert released["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_takeover_refuses_a_dirty_destination_before_changing_owner(
    bridge, repo, paired
):
    registered(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    peer = Path(paired["lanes"]["codex"])
    directory = lane.parent
    bridge.issue(lane, "claim", "42")
    (lane / "source.txt").write_text("recover me\n")
    killed(directory, "claude", STALLED + 100)
    running(directory, "codex")
    supervision.poll(bridge.home, directory)
    (peer / "local.txt").write_text("keep me\n")

    with pytest.raises(BridgeError) as refusal:
        bridge.issue(peer, "claim", "42", take_orphaned=True)

    assert "dirty or untracked work" in str(refusal.value)
    assert (peer / "local.txt").read_text() == "keep me\n"
    record = issues.snapshot(directory)["issues"]["42"]
    assert record["owner"] == "claude"
    assert record["orphan"]["owner"] == "claude"


def test_capture_does_not_reuse_gate_evidence_for_changed_content(
    bridge, repo, paired
):
    lane = Path(paired["lanes"]["claude"])
    directory = lane.parent
    bridge.issue(lane, "claim", "42")
    manifest = roster.read(directory)
    recovery.capture(
        directory,
        manifest,
        "claude",
        {
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"cmd": "pytest -q"},
            "tool_response": {"exit_code": 0},
        },
    )
    (lane / "changed-after-gate.txt").write_text("new content\n")

    saved = recovery.capture(directory, manifest, "claude")[0]

    assert saved["gate"] == {}
    assert saved["last_verified_step"] == "PostToolUse: Bash"


@pytest.mark.parametrize("boundary", ["head", "index", "worktree"])
def test_takeover_restore_resumes_after_each_durable_phase(
    bridge, repo, paired, monkeypatch, boundary
):
    registered(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    peer = Path(paired["lanes"]["codex"])
    directory = lane.parent
    bridge.issue(lane, "claim", "42")
    (lane / "committed.txt").write_text("committed\n")
    git(lane, "add", "committed.txt")
    git(
        lane,
        "-c",
        "user.name=Bridge Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-m",
        "Captured commit",
    )
    (lane / "staged.txt").write_text("staged\n")
    git(lane, "add", "staged.txt")
    (lane / "worktree.txt").write_text("worktree\n")
    killed(directory, "claude", STALLED + 100)
    running(directory, "codex")
    supervision.poll(bridge.home, directory)
    original_git = recovery._git
    interrupted = False

    def interrupt_after(lane, *args, **kwargs):
        nonlocal interrupted
        result = original_git(lane, *args, **kwargs)
        matches = {
            "head": args[:2] == ("merge", "--ff-only"),
            "index": args[:2] == ("apply", "--index"),
            "worktree": args[:2] == ("apply", "--binary"),
        }
        if not interrupted and matches[boundary]:
            interrupted = True
            raise BridgeError(f"interrupted after {boundary}")
        return result

    monkeypatch.setattr(recovery, "_git", interrupt_after)
    with pytest.raises(BridgeError, match=f"interrupted after {boundary}"):
        bridge.issue(peer, "claim", "42", take_orphaned=True)
    assert issues.snapshot(directory)["issues"]["42"]["owner"] == "codex"

    monkeypatch.setattr(recovery, "_git", original_git)
    resumed = bridge.issue(peer, "claim", "42")

    assert interrupted is True
    assert resumed["recovery"]["phase"] == "complete"
    assert git(peer, "log", "-1", "--format=%s") == "Captured commit"
    assert (peer / "staged.txt").read_text() == "staged\n"
    assert (peer / "worktree.txt").read_text() == "worktree\n"


def test_takeover_revalidates_a_returned_owner_process(bridge, repo, paired):
    registered(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    peer = Path(paired["lanes"]["codex"])
    directory = lane.parent
    bridge.issue(lane, "claim", "42")
    killed(directory, "claude", STALLED + 100)
    running(directory, "codex")
    supervision.poll(bridge.home, directory)
    running(directory, "claude")

    with pytest.raises(BridgeError) as refusal:
        bridge.issue(peer, "claim", "42", take_orphaned=True)

    assert "has a live session" in str(refusal.value)
    assert issues.snapshot(directory)["issues"]["42"]["owner"] == "claude"


def test_takeover_refuses_an_owner_with_missing_process_identity(
    bridge, repo, paired
):
    registered(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    peer = Path(paired["lanes"]["codex"])
    directory = lane.parent
    bridge.issue(lane, "claim", "42")
    recovery.capture(directory, roster.read(directory), "claude")
    state = issues.snapshot(directory)
    state["issues"]["42"]["orphan"] = {
        "id": "missing-process",
        "owner": "claude",
        "reason": "session identity unavailable",
    }
    write_json(directory / "issues.json", state)
    write_json(
        directory / "claude-activity.json",
        {"activity": "working", "session_id": "unknown-process"},
    )

    with pytest.raises(BridgeError, match="no complete session identity"):
        bridge.issue(peer, "claim", "42", take_orphaned=True)

    assert issues.snapshot(directory)["issues"]["42"]["owner"] == "claude"


def test_competing_takeovers_publish_one_owner_and_one_fence(
    bridge, repo, paired
):
    registered(bridge, paired)
    expanded = bridge.add_participant(repo, "gemini", "claude")
    store.register(bridge.home, expanded["root"], "gemini")
    lane = Path(expanded["lanes"]["claude"])
    peers = [
        Path(expanded["lanes"]["codex"]),
        Path(expanded["lanes"]["gemini"]),
    ]
    directory = lane.parent
    bridge.issue(lane, "claim", "42")
    killed(directory, "claude", STALLED + 100)
    running(directory, "codex")
    running(directory, "gemini")
    supervision.poll(bridge.home, directory)

    def take(peer):
        try:
            return bridge.issue(peer, "claim", "42", take_orphaned=True)
        except BridgeError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(take, peers))

    successes = [item for item in results if isinstance(item, dict)]
    failures = [item for item in results if isinstance(item, BridgeError)]
    assert len(successes) == len(failures) == 1
    record = issues.snapshot(directory)["issues"]["42"]
    assert record["owner"] == successes[0]["owner"]
    activity = json.loads((directory / "claude-activity.json").read_text())
    assert activity["ownership_fence"]["taken_by"] == record["owner"]


def test_unpublished_takeover_fence_does_not_stop_the_old_owner(
    bridge, repo, paired, monkeypatch
):
    registered(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    peer = Path(paired["lanes"]["codex"])
    directory = lane.parent
    bridge.issue(lane, "claim", "42")
    killed(directory, "claude", STALLED + 100)
    running(directory, "codex")
    supervision.poll(bridge.home, directory)
    original_write = issues.write_json
    interrupted = False

    def interrupt_ledger(path, value):
        nonlocal interrupted
        if path.name == "issues.json" and not interrupted:
            interrupted = True
            raise BridgeError("interrupted before takeover publication")
        original_write(path, value)

    monkeypatch.setattr(issues, "write_json", interrupt_ledger)
    with pytest.raises(
        BridgeError, match="interrupted before takeover publication"
    ):
        bridge.issue(peer, "claim", "42", take_orphaned=True)
    payload = {"session_id": "claude-session", "hook_event_name": "PreToolUse"}
    assert recovery.stale_session(directory, "claude", payload) is None
    assert issues.snapshot(directory)["issues"]["42"]["owner"] == "claude"

    monkeypatch.setattr(issues, "write_json", original_write)
    taken = bridge.issue(peer, "claim", "42", take_orphaned=True)

    assert taken["owner"] == "codex"
    refusal = recovery.stale_session(directory, "claude", payload)
    assert refusal["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_explicit_approval_quiesces_session_observation_after_reclaim(
    bridge, repo, paired
):
    registered(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    directory = lane.parent
    bridge.issue(lane, "claim", "42")
    (lane / "capacity-work.bin").write_bytes(b"\x00pending\xff")
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    session_id = "capacity-session"
    try:
        write_json(
            directory / "claude-activity.json",
            {
                "activity": "working",
                "updated": time.time(),
                "session_id": session_id,
                "session_pid": child.pid,
                "session_ticks": process.start_ticks(child.pid),
            },
        )
        write_json(
            directory / "capacity-candidates.json",
            {
                "version": 1,
                "candidates": [
                    {
                        "issue": "42",
                        "owner": "claude",
                        "eligible_peers": ["codex"],
                        "reason": "provider capacity exhausted",
                        "next_action": "request recorded recovery transition",
                        "reset_at": None,
                        "source": "provider-status",
                        "session_id": session_id,
                        "observation_id": "capacity-observation",
                    }
                ],
            },
        )
        bridge.issue(lane, "release", "42")
        bridge.issue(lane, "claim", "42")

        manifest = roster.read(directory)
        assert recovery.quiesce_authorized(directory, manifest) == []
        assert child.poll() is None
        approved = bridge.authorize_recovery(
            repo, "42", "continue this claim on an available lane"
        )
        activity_path = directory / "claude-activity.json"
        activity = json.loads(activity_path.read_text())
        activity["activity"] = "waiting for approval"
        write_json(activity_path, activity)
        with pytest.raises(BridgeError, match="waiting for operator input"):
            recovery.quiesce_authorized(directory, manifest)
        activity["activity"] = "idle"
        write_json(activity_path, activity)
        with pytest.raises(BridgeError, match="waiting for operator input"):
            recovery.quiesce_authorized(directory, manifest)
        activity["activity"] = "working"
        write_json(activity_path, activity)
        manifest["participants"]["claude"]["paused"] = True
        with pytest.raises(BridgeError, match="is paused"):
            recovery.quiesce_authorized(directory, manifest)
        manifest["participants"]["claude"]["paused"] = False
        markers = recovery.quiesce_authorized(directory, manifest)

        child.wait(timeout=5)
        assert approved["owner"] == "claude"
        assert markers[0]["authorization"]["id"] == approved["id"]
        assert markers[0]["source"] == "provider-status"
        record = issues.snapshot(directory)["issues"]["42"]
        assert record["owner"] == "claude"
        assert record["orphan"]["observation_id"] == "capacity-observation"
        assert record["orphan"]["reset_at"] is None
        saved = recovery.checkpoint(directory, "42", record["claim_id"])
        assert saved["artifact"]["bytes"] > 0
        marker = dict(record["orphan"])
        activity = json.loads(activity_path.read_text())
        activity["updated"] = time.time() - STALLED - 100
        write_json(activity_path, activity)
        supervision.poll(bridge.home, directory)
        assert issues.snapshot(directory)["issues"]["42"]["orphan"] == marker
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


def test_quiesce_resumes_after_the_exact_process_stops(
    bridge, repo, paired, monkeypatch
):
    registered(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    directory = lane.parent
    bridge.issue(lane, "claim", "42")
    (lane / "pending.txt").write_text("pending\n")
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    session_id = "interrupted-capacity-session"
    try:
        write_json(
            directory / "claude-activity.json",
            {
                "activity": "working",
                "updated": time.time(),
                "session_id": session_id,
                "session_pid": child.pid,
                "session_ticks": process.start_ticks(child.pid),
            },
        )
        write_json(
            directory / "capacity-candidates.json",
            {
                "version": 1,
                "candidates": [
                    {
                        "issue": "42",
                        "owner": "claude",
                        "eligible_peers": ["codex"],
                        "reason": "provider capacity exhausted",
                        "next_action": "request recorded recovery transition",
                        "reset_at": None,
                        "source": "provider-status",
                        "session_id": session_id,
                        "observation_id": "interrupted-observation",
                    }
                ],
            },
        )
        manifest = roster.read(directory)
        bridge.authorize_recovery(repo, "42", "recover after provider refusal")
        original_stop = process.ServerProcess.stop

        def interrupted_stop(server):
            original_stop(server)
            raise BridgeError("interrupted after process stop")

        monkeypatch.setattr(process.ServerProcess, "stop", interrupted_stop)
        with pytest.raises(BridgeError, match="interrupted after process stop"):
            recovery.quiesce_authorized(directory, manifest)
        child.wait(timeout=5)
        assert "orphan" not in issues.snapshot(directory)["issues"]["42"]

        monkeypatch.setattr(process.ServerProcess, "stop", original_stop)
        original_write = recovery.write_json

        def interrupt_before_approval_consumption(path, value):
            if path.name.endswith("-approval.json") and value.get("used_at"):
                raise BridgeError("interrupted before approval consumption")
            original_write(path, value)

        monkeypatch.setattr(
            recovery, "write_json", interrupt_before_approval_consumption
        )
        with pytest.raises(
            BridgeError, match="interrupted before approval consumption"
        ):
            recovery.quiesce_authorized(directory, manifest)
        approval_path = next((directory / "recovery").glob("*-approval.json"))
        assert "used_at" not in json.loads(approval_path.read_text())
        monkeypatch.setattr(recovery, "write_json", original_write)
        markers = recovery.quiesce_authorized(directory, manifest)

        assert markers[0]["checkpoint"].startswith("issue-42-")
        record = issues.snapshot(directory)["issues"]["42"]
        assert record["orphan"]["authorization"]["actor"] == "operator"
        transitions = list((directory / "recovery").glob("*-quiesce.json"))
        assert json.loads(transitions[0].read_text())["phase"] == "complete"
        assert recovery.approval(directory, "42", record["claim_id"]) is None
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


def test_pre_stop_transition_requires_fresh_authority_after_session_restart(
    bridge, repo, paired, monkeypatch
):
    registered(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    directory = lane.parent
    bridge.issue(lane, "claim", "42")
    old = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    fresh = None
    try:
        write_json(
            directory / "claude-activity.json",
            {
                "activity": "working",
                "updated": time.time(),
                "session_id": "old-capacity-session",
                "session_pid": old.pid,
                "session_ticks": process.start_ticks(old.pid),
            },
        )

        def publish_candidate(session_id, observation_id):
            write_json(
                directory / "capacity-candidates.json",
                {
                    "version": 1,
                    "candidates": [
                        {
                            "issue": "42",
                            "owner": "claude",
                            "eligible_peers": ["codex"],
                            "reason": "provider capacity exhausted",
                            "next_action": (
                                "request recorded recovery transition"
                            ),
                            "reset_at": None,
                            "source": "provider-status",
                            "session_id": session_id,
                            "observation_id": observation_id,
                        }
                    ],
                },
            )

        publish_candidate("old-capacity-session", "old-observation")
        bridge.authorize_recovery(repo, "42", "recover old session")
        original_write = recovery.write_json

        def interrupt_after_authorization(path, value):
            original_write(path, value)
            if (
                path.name.endswith("-quiesce.json")
                and value.get("phase") == "authorized"
            ):
                raise BridgeError("interrupted after authorization")

        monkeypatch.setattr(
            recovery, "write_json", interrupt_after_authorization
        )
        with pytest.raises(
            BridgeError, match="interrupted after authorization"
        ):
            recovery.quiesce_authorized(directory, roster.read(directory))
        monkeypatch.setattr(recovery, "write_json", original_write)
        old.kill()
        old.wait()
        fresh = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        write_json(
            directory / "claude-activity.json",
            {
                "activity": "working",
                "updated": time.time(),
                "session_id": "fresh-capacity-session",
                "session_pid": fresh.pid,
                "session_ticks": process.start_ticks(fresh.pid),
            },
        )
        publish_candidate("fresh-capacity-session", "fresh-observation")
        with pytest.raises(BridgeError, match="stale session"):
            recovery.quiesce_authorized(directory, roster.read(directory))
        transition_path = next((directory / "recovery").glob("*-quiesce.json"))
        assert (
            json.loads(transition_path.read_text())["session_id"]
            == "old-capacity-session"
        )
        bridge.authorize_recovery(repo, "42", "recover fresh session")

        markers = recovery.quiesce_authorized(directory, roster.read(directory))

        fresh.wait(timeout=5)
        assert markers[0]["session_id"] == "fresh-capacity-session"
        transition = json.loads(transition_path.read_text())
        assert transition["phase"] == "complete"
        assert transition["superseded"]["session_id"] == "old-capacity-session"
    finally:
        for child in (old, fresh):
            if child is not None and child.poll() is None:
                child.kill()
                child.wait()


def test_a_live_but_idle_lane_is_never_orphaned(bridge, repo, paired):
    registered(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    directory = lane.parent
    bridge.issue(lane, "claim", "42")
    running(directory, "claude", STALLED + 100)
    running(directory, "codex")

    supervision.poll(bridge.home, directory)

    assert "orphan" not in issues.snapshot(directory)["issues"]["42"]
    assert not inbox(bridge, paired, "codex")


def test_a_lane_gone_for_less_than_the_threshold_is_never_orphaned(
    bridge, repo, paired, monkeypatch
):
    registered(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    directory = lane.parent
    bridge.issue(lane, "claim", "42")
    killed(directory, "claude", STALLED - 60)
    running(directory, "codex")
    manifest = roster.read(directory)
    manifest["supervision"] = {"wake": False}
    write_json(directory / "project.json", manifest)
    monkeypatch.setattr(
        supervision,
        "subprocess",
        types.SimpleNamespace(
            **{
                **vars(subprocess),
                "Popen": lambda *args, **kwargs: pytest.fail(
                    "started a native launcher"
                ),
            }
        ),
    )

    supervision.poll(bridge.home, directory)

    assert "orphan" not in issues.snapshot(directory)["issues"]["42"]


def test_the_notice_is_recorded_once_for_one_orphaning(bridge, repo, paired):
    registered(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    directory = lane.parent
    bridge.issue(lane, "claim", "42")
    killed(directory, "claude", STALLED + 100)
    running(directory, "codex")

    supervision.poll(bridge.home, directory)
    revision = issues.snapshot(directory)["revision"]
    supervision.poll(bridge.home, directory)

    assert issues.snapshot(directory)["revision"] == revision
    assert len(inbox(bridge, paired, "codex")) == 1


def test_a_live_owner_is_never_taken_from(bridge, repo, paired):
    lane = Path(paired["lanes"]["claude"])
    peer = Path(paired["lanes"]["codex"])
    bridge.issue(lane, "claim", "42")

    with pytest.raises(BridgeError) as refusal:
        bridge.issue(peer, "claim", "42", take_orphaned=True)

    assert "does not read as orphaned" in str(refusal.value)
    assert issues.snapshot(lane.parent)["issues"]["42"]["owner"] == "claude"


def test_an_unclaimed_issue_is_never_taken(bridge, repo, paired):
    peer = Path(paired["lanes"]["codex"])

    with pytest.raises(BridgeError) as refusal:
        bridge.issue(peer, "claim", "42", take_orphaned=True)

    assert "no orphaned owner" in str(refusal.value)


def test_a_peer_without_the_flag_is_told_how_to_take_it(bridge, repo, paired):
    registered(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    peer = Path(paired["lanes"]["codex"])
    directory = lane.parent
    bridge.issue(lane, "claim", "42")
    killed(directory, "claude", STALLED + 100)
    running(directory, "codex")
    supervision.poll(bridge.home, directory)

    with pytest.raises(BridgeError) as refusal:
        bridge.issue(peer, "claim", "42")

    assert "--take-orphaned" in str(refusal.value)
    assert issues.snapshot(directory)["issues"]["42"]["owner"] == "claude"


def test_a_returning_owner_loses_the_marker_and_keeps_the_claim(
    bridge, repo, paired
):
    registered(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    peer = Path(paired["lanes"]["codex"])
    directory = lane.parent
    bridge.issue(lane, "claim", "42")
    killed(directory, "claude", STALLED + 100)
    running(directory, "codex")
    supervision.poll(bridge.home, directory)
    marked = issues.snapshot(directory)["issues"]["42"]
    assert marked["orphan"]["owner"] == "claude"

    running(directory, "claude")
    supervision.poll(bridge.home, directory)

    returned = issues.snapshot(directory)["issues"]["42"]
    assert "orphan" not in returned
    assert returned["owner"] == "claude"
    assert returned["claim_id"] == marked["claim_id"]
    assert lifecycle.actionable(issues.snapshot(directory), "claude") == ["42"]
    delivered = {
        subject: body for subject, body in inbox(bridge, paired, "codex")
    }
    withdrawal = delivered["Orphan marker withdrawn for claude"]
    assert "#42" in withdrawal
    assert "no longer available to take" in withdrawal
    assert not inbox(bridge, paired, "claude")

    with pytest.raises(BridgeError) as refusal:
        bridge.issue(peer, "claim", "42", take_orphaned=True)

    assert "does not read as orphaned" in str(refusal.value)
    assert issues.snapshot(directory)["issues"]["42"]["owner"] == "claude"


def test_an_authorized_capacity_marker_survives_a_returning_owner(
    bridge, repo, paired
):
    registered(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    directory = lane.parent
    claimed = bridge.issue(lane, "claim", "42")
    ledger = issues.snapshot(directory)
    ledger["issues"]["42"]["orphan"] = {
        "id": f"capacity:{claimed['claim_id']}:observation",
        "owner": "claude",
        "claim_id": claimed["claim_id"],
        "reason": "capacity exhausted",
        "reservations": [],
        "created": time.time(),
        "checkpoint": "checkpoint-1",
        "authorization": {
            "id": "authorization-1",
            "actor": "operator",
            "reason": "Exercise the saved work",
            "approved_at": time.time(),
        },
    }
    write_json(directory / "issues.json", ledger)
    running(directory, "claude")
    running(directory, "codex")

    supervision.poll(bridge.home, directory)

    kept = issues.snapshot(directory)["issues"]["42"]["orphan"]
    assert kept["id"] == f"capacity:{claimed['claim_id']}:observation"
    assert kept["authorization"]["id"] == "authorization-1"


def test_the_printed_remedy_takes_the_orphaned_claim(
    bridge, repo, paired, monkeypatch, capsys
):
    registered(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    peer = Path(paired["lanes"]["codex"])
    directory = lane.parent
    bridge.issue(lane, "claim", "42")
    killed(directory, "claude", STALLED + 100)
    running(directory, "codex")
    supervision.poll(bridge.home, directory)

    listing = issues.describe(issues.snapshot(directory))
    remedy = listing.split("still owned until a peer runs ")[1].splitlines()[0]
    monkeypatch.chdir(peer)
    monkeypatch.setattr(
        sys,
        "argv",
        ["agent-parley", "--home", str(bridge.home), *shlex.split(remedy)],
    )

    assert cli.main() == 0

    taken = json.loads(capsys.readouterr().out)
    assert taken["owner"] == "codex"
    assert taken["taken"]["from"] == "claude"
    assert issues.snapshot(directory)["issues"]["42"]["owner"] == "codex"
