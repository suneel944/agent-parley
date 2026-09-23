"""Checks the hook client against the service and its in-process fallback."""

import asyncio
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

from agent_parley import (
    checkpoints,
    cli,
    hook,
    process,
    protocol,
    roster,
    server,
    store,
)
from agent_parley.state import lock, write_json

ALLOW = {"hook_event_name": "PreToolUse", "tool_name": "Read", "tool_input": {}}
DENY = {
    "hook_event_name": "PreToolUse",
    "tool_name": "Bash",
    "tool_input": {"command": "git checkout -b elsewhere"},
}
STOP = {"hook_event_name": "Stop", "stop_hook_active": False}
START = {"hook_event_name": "SessionStart", "session_id": "s1"}
PROMPT = {"hook_event_name": "UserPromptSubmit", "prompt": "continue"}
STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4} ")
SLEEPER = "import time; time.sleep(120)"


def events(directory, agent="codex"):
    path = directory / f"{agent}-events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.fixture(autouse=True)
def registered(request):
    """Writes both lanes' identity files, as the launcher does at a start."""
    if "paired" in request.fixturenames:
        bridge = request.getfixturevalue("bridge")
        paired = request.getfixturevalue("paired")
        store.initialize(bridge.home)
        for name in ("claude", "codex"):
            asyncio.run(bridge.identity(name, paired))


@pytest.fixture
def service(bridge):
    """Serves the bridge's configured loopback port on a thread."""
    with server.Server(bridge.home, bridge.config) as instance:
        thread = threading.Thread(target=instance.serve_forever, daemon=True)
        thread.start()
        try:
            yield instance
        finally:
            instance.shutdown()
            thread.join(timeout=2)


def importable():
    """Returns an environment that can import this checkout anywhere.

    A hook client reached through this helper may ask for the service
    back, and the relaunch it asks for starts the service from the bridge
    home rather than from the current directory. The implicit path entry
    that lets ``python -m`` find the package under test therefore does not
    reach that grandchild, which then fails to import it unless the
    interpreter running this suite also has the package installed. The
    checkout root travels on ``PYTHONPATH`` so the outage tests state that
    precondition themselves instead of inheriting it from the environment.
    """
    root = Path(__file__).resolve().parents[1]
    carried = os.environ.get("PYTHONPATH", "")
    entries = [str(root)] + ([carried] if carried else [])
    return {**os.environ, "PYTHONPATH": os.pathsep.join(entries)}


def run_hook(bridge, directory, payload, module="agent_parley.hook"):
    return subprocess.run(
        [
            sys.executable,
            "-m",
            module,
            "--home",
            str(bridge.home),
            "--directory",
            str(directory),
            "--participant",
            "codex",
        ],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env=importable(),
    )


@pytest.mark.parametrize("payload", [ALLOW, DENY, STOP, START])
def test_a_served_decision_equals_the_in_process_decision(
    bridge, repo, paired, service, payload
):
    lane = Path(paired["lanes"]["codex"])
    cwd = {"cwd": str(lane), "session_id": "s1"}
    served = run_hook(bridge, lane.parent, {**payload, **cwd})
    (lane.parent / "codex-activity.json").unlink(missing_ok=True)
    local = run_hook(
        bridge, lane.parent, {**payload, **cwd}, "agent_parley.checkpoints"
    )
    assert (served.returncode, served.stdout, served.stderr) == (
        local.returncode,
        local.stdout,
        local.stderr,
    )
    assert not any(
        entry["reason_class"] == "service_fallback"
        for entry in events(lane.parent)
    )


def test_a_served_decision_carries_context_and_blocks_completion(
    bridge, repo, paired, service
):
    lane = Path(paired["lanes"]["codex"])
    identity = json.loads((lane.parent / "claude-identity.json").read_text())
    actor = store.authenticate(bridge.home, identity["registration_token"])
    store.call(
        bridge.home,
        actor,
        "send_message",
        {
            "to": ["codex"],
            "subject": "Interface change",
            "body_md": "The API moved.",
            "idempotency_key": "one",
        },
    )
    cwd = {"cwd": str(lane), "session_id": "s1"}
    started = run_hook(bridge, lane.parent, {**START, **cwd})
    context = json.loads(started.stdout)["hookSpecificOutput"]
    assert "Interface change" in context["additionalContext"]
    store.call(
        bridge.home,
        actor,
        "send_message",
        {
            "to": ["codex"],
            "subject": "Second change",
            "body_md": "Again.",
            "idempotency_key": "two",
        },
    )
    stopped = run_hook(bridge, lane.parent, {**STOP, **cwd})
    assert json.loads(stopped.stdout)["decision"] == "block"
    recorded = events(lane.parent)
    assert recorded[-1]["decision"] == "block"
    assert recorded[-1]["reason_class"] == "coordination_pending"


def test_a_served_hook_records_its_foreground_native_process(
    bridge, repo, paired, service, monkeypatch
):
    lane = Path(paired["lanes"]["codex"])
    native = process.ServerProcess(
        os.getpid(), process.start_ticks(os.getpid())
    )
    inspected = []

    def foreground(hook_pid):
        inspected.append(hook_pid)
        return native

    monkeypatch.setattr(checkpoints.process, "foreground_process", foreground)
    started = run_hook(
        bridge,
        lane.parent,
        {
            **START,
            "cwd": str(lane),
            "session_pid": 999999,
            "session_ticks": "forged",
        },
    )
    assert started.returncode == 0, started.stderr
    state = json.loads((lane.parent / "codex-activity.json").read_text())
    assert inspected and type(inspected[0]) is int
    assert state["session_pid"] == native.pid
    assert state["session_ticks"] == native.ticks


def test_session_start_preserves_a_live_launcher_process(
    bridge, repo, paired, service, monkeypatch
):
    lane = Path(paired["lanes"]["codex"])
    directory = lane.parent
    native = process.ServerProcess(
        os.getpid(), process.start_ticks(os.getpid())
    )
    write_json(
        directory / "codex-activity.json",
        {
            "session_id": "s1",
            "session_pid": native.pid,
            "session_ticks": native.ticks,
        },
    )
    monkeypatch.setattr(
        checkpoints.process, "foreground_process", lambda hook_pid: None
    )
    started = run_hook(
        bridge,
        directory,
        {**START, "cwd": str(lane)},
    )
    assert started.returncode == 0, started.stderr
    state = json.loads((directory / "codex-activity.json").read_text())
    assert state["session_pid"] == native.pid
    assert state["session_ticks"] == native.ticks


def test_a_hook_with_no_terminal_records_the_launched_session(
    bridge, repo, paired, service, monkeypatch
):
    lane = Path(paired["lanes"]["codex"])
    directory = lane.parent
    launcher = os.getpid()
    write_json(
        directory / "codex-activity.json",
        {
            "session_id": "",
            "launcher_pid": launcher,
            "launcher_ticks": process.start_ticks(launcher),
        },
    )
    monkeypatch.setattr(
        checkpoints.process, "foreground_process", lambda hook_pid: None
    )
    started = run_hook(bridge, directory, {**START, "cwd": str(lane)})
    assert started.returncode == 0, started.stderr
    state = json.loads((directory / "codex-activity.json").read_text())
    assert type(state["session_pid"]) is int
    assert state["session_pid"] not in (launcher, 0)
    assert state["session_ticks"]


def test_new_session_clears_an_unrelated_live_process(
    bridge, repo, paired, service, monkeypatch
):
    lane = Path(paired["lanes"]["codex"])
    directory = lane.parent
    unrelated = subprocess.Popen([sys.executable, "-c", SLEEPER])
    try:
        write_json(
            directory / "codex-activity.json",
            {
                "session_id": "previous",
                "session_pid": unrelated.pid,
                "session_ticks": process.start_ticks(unrelated.pid),
            },
        )
        monkeypatch.setattr(
            checkpoints.process, "foreground_process", lambda hook_pid: None
        )
        started = run_hook(
            bridge,
            directory,
            {**START, "cwd": str(lane)},
        )
    finally:
        unrelated.kill()
        unrelated.wait(10)
    assert started.returncode == 0, started.stderr
    state = json.loads((directory / "codex-activity.json").read_text())
    assert "session_pid" not in state
    assert "session_ticks" not in state


def test_a_new_session_id_keeps_a_live_client_and_reports_it_live(
    bridge, repo, paired, service, monkeypatch
):
    lane = Path(paired["lanes"]["codex"])
    directory = lane.parent
    client = process.ServerProcess(
        os.getpid(), process.start_ticks(os.getpid())
    )
    write_json(
        directory / "codex-activity.json",
        {
            "session_id": "cleared",
            "session_pid": client.pid,
            "session_ticks": client.ticks,
            "activity": "working",
            "updated": time.time(),
        },
    )
    monkeypatch.setattr(
        checkpoints.process, "foreground_process", lambda hook_pid: None
    )
    started = run_hook(bridge, directory, {**START, "cwd": str(lane)})
    assert started.returncode == 0, started.stderr
    state = json.loads((directory / "codex-activity.json").read_text())
    assert state["session_id"] == "s1"
    assert state["session_pid"] == client.pid
    assert state["session_ticks"] == client.ticks
    liveness = checkpoints.participant_liveness(directory, "codex")
    assert not liveness.startswith("stopped")


def test_a_new_session_id_on_a_dead_client_still_reports_stopped(
    bridge, repo, paired, service, monkeypatch
):
    lane = Path(paired["lanes"]["codex"])
    directory = lane.parent
    gone = subprocess.Popen([sys.executable, "-c", SLEEPER])
    ticks = process.start_ticks(gone.pid)
    gone.kill()
    gone.wait(10)
    write_json(
        directory / "codex-activity.json",
        {
            "session_id": "cleared",
            "session_pid": gone.pid,
            "session_ticks": ticks,
            "activity": "working",
            "updated": time.time(),
        },
    )
    monkeypatch.setattr(
        checkpoints.process, "foreground_process", lambda hook_pid: None
    )
    started = run_hook(bridge, directory, {**START, "cwd": str(lane)})
    assert started.returncode == 0, started.stderr
    state = json.loads((directory / "codex-activity.json").read_text())
    assert "session_pid" not in state
    assert "session_ticks" not in state
    liveness = checkpoints.participant_liveness(directory, "codex")
    assert liveness.startswith("stopped")


def test_a_down_service_falls_back_in_process(bridge, repo, paired):
    lane = Path(paired["lanes"]["codex"])
    cwd = {"cwd": str(lane), "session_id": "s1"}
    served = run_hook(bridge, lane.parent, {**DENY, **cwd})
    assert served.returncode == 0
    decision = json.loads(served.stdout)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    recorded = events(lane.parent)
    assert recorded[0]["reason_class"] == "service_fallback"
    assert "ConnectionRefusedError" in recorded[0]["cause"]
    assert recorded[-1]["decision"] == "deny"


def test_a_reply_without_a_status_line_falls_back_in_process(
    bridge, repo, paired
):
    listener = socket.create_server(("127.0.0.1", bridge.config["port"]))
    listener.settimeout(10)

    def close_without_answering():
        accepted, _ = listener.accept()
        accepted.recv(65536)
        accepted.close()

    thread = threading.Thread(target=close_without_answering, daemon=True)
    thread.start()
    try:
        lane = Path(paired["lanes"]["codex"])
        cwd = {"cwd": str(lane), "session_id": "s1"}
        served = run_hook(bridge, lane.parent, {**DENY, **cwd})
    finally:
        thread.join(timeout=10)
        listener.close()
    assert served.returncode == 0, served.stderr
    assert "Traceback" not in served.stderr
    decision = json.loads(served.stdout)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    recorded = events(lane.parent)
    assert recorded[0]["reason_class"] == "service_fallback"
    assert "ValueError: reply has no status line" in recorded[0]["cause"]


def test_a_failure_inside_the_served_decision_answers_500(
    bridge, repo, paired, service, monkeypatch, capsys
):
    def broken(home, request, stages=None, settle=0.0, record_only=False):
        raise ImportError("cannot import name 'budgets'")

    monkeypatch.setattr(server.checkpoints, "serve", broken)
    lane = Path(paired["lanes"]["codex"])
    cwd = {"cwd": str(lane), "session_id": "s1"}
    served = run_hook(bridge, lane.parent, {**DENY, **cwd})
    assert served.returncode == 0, served.stderr
    decision = json.loads(served.stdout)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    recorded = events(lane.parent)
    assert recorded[0]["reason_class"] == "service_fallback"
    assert "service answered 500" in recorded[0]["cause"]
    logged = capsys.readouterr().out
    entry = next(line for line in logged.splitlines() if " failed " in line)
    assert STAMP.match(entry)
    assert re.search(
        rf"failed {hook.PATH} codex after \d+\.\d{{3}} seconds$", entry
    )
    assert "cannot import name 'budgets'" in logged


def moved_sources(monkeypatch):
    """Makes the running service read a source tree that has moved on."""
    monkeypatch.setattr(server, "REVISION_SECONDS", 0.0)
    monkeypatch.setattr(server.protocol, "revision", lambda: "moved")


def test_a_stale_service_answers_the_hook_with_a_fallback_status(
    bridge, repo, paired, service, monkeypatch, capsys
):
    moved_sources(monkeypatch)
    lane = Path(paired["lanes"]["codex"])
    cwd = {"cwd": str(lane), "session_id": "s1"}
    served = run_hook(bridge, lane.parent, {**DENY, **cwd})
    assert served.returncode == 0, served.stderr
    decision = json.loads(served.stdout)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    recorded = events(lane.parent)
    assert recorded[0]["reason_class"] == "service_fallback"
    assert "service answered 503" in recorded[0]["cause"]
    logged = capsys.readouterr()
    assert "Traceback" not in logged.out + logged.err
    assert logged.out.count(server.DRIFTED) == 1
    assert service.stopping.is_set()


@pytest.fixture
def watched_service(bridge):
    """Serves on a thread the test watches return when the service stops."""
    with server.Server(bridge.home, bridge.config) as instance:
        thread = threading.Thread(target=instance.serve_forever, daemon=True)
        thread.start()
        try:
            yield instance, thread
        finally:
            instance.shutdown()
            thread.join(timeout=2)


def hook_request(bridge, lane, payload):
    """Posts one hook event over the transport the hook client uses."""
    identity = json.loads((lane.parent / "codex-identity.json").read_text())
    with httpx.Client(base_url=bridge.url, trust_env=False) as client:
        return client.post(
            hook.PATH,
            json={
                "directory": str(lane.parent),
                "participant": "codex",
                "hook_pid": os.getpid(),
                "payload": payload,
            },
            headers={
                "Authorization": f"Bearer {identity['registration_token']}"
            },
            timeout=hook.REPLY_TIMEOUT,
        )


def test_drift_stops_a_busy_service_within_one_revision_interval(
    bridge, repo, paired, watched_service, monkeypatch
):
    instance, serving = watched_service
    statuses = []
    entries = []
    finished = threading.Event()
    stalled = threading.Event()
    released = threading.Event()
    written = server.log

    def entry(home, event, detail=""):
        if event == "drifted":
            entries.append(detail)
            stalled.set()
            released.wait(10)
        written(home, event, detail)

    def busy():
        headers = {"Authorization": f"Bearer {bridge.config['token']}"}
        with httpx.Client(base_url=bridge.url, trust_env=False) as client:
            while not finished.is_set():
                try:
                    reading = client.get("/health/readiness", headers=headers)
                except httpx.HTTPError:
                    return
                statuses.append(reading.json().get("status"))

    monkeypatch.setattr(server, "log", entry)
    callers = [threading.Thread(target=busy, daemon=True) for _ in range(3)]
    for caller in callers:
        caller.start()
    monkeypatch.setattr(server.protocol, "revision", lambda: "moved")
    instance.checked = time.monotonic() - server.REVISION_SECONDS
    serving.join(timeout=server.REVISION_SECONDS)
    finished.set()
    released.set()
    for caller in callers:
        caller.join(timeout=2)
    assert stalled.is_set()
    assert not serving.is_alive()
    assert entries == [server.DRIFTED]
    assert protocol.STALE in statuses


def test_a_hook_call_in_the_drift_window_answers_inside_the_budget(
    bridge, repo, paired, service, monkeypatch
):
    moved_sources(monkeypatch)
    lane = Path(paired["lanes"]["codex"])
    started = time.monotonic()
    answered = hook_request(bridge, lane, DENY)
    elapsed = time.monotonic() - started
    assert answered.status_code == 503
    assert answered.json() == {
        "status": protocol.STALE,
        "detail": server.DRIFTED,
    }
    assert elapsed < hook.REPLY_TIMEOUT
    assert service.stopping.is_set()


def test_a_stale_service_reads_as_degraded_in_the_status_document(
    bridge, repo, paired, service, monkeypatch
):
    moved_sources(monkeypatch)
    reading = bridge.status_snapshot()
    assert reading["server"] == {"ready": False, "state": protocol.STALE}
    assert service.stopping.is_set()


def test_status_names_the_drift_a_stale_service_reports(
    bridge, repo, paired, service, monkeypatch, capsys
):
    moved_sources(monkeypatch)
    bridge.status()
    printed = capsys.readouterr().out
    assert "Server: not ready" in printed
    assert f"Code: {protocol.STALE}" in printed
    assert protocol.RELAUNCH in printed


def test_doctor_reports_a_stale_service_as_inconsistent(
    bridge, repo, paired, service, monkeypatch
):
    moved_sources(monkeypatch)
    reported = bridge.doctor()
    named = {entry["component"]: entry for entry in reported["components"]}
    assert named["service"]["state"] == protocol.STALE
    assert named["service"]["remedy"] == protocol.RELAUNCH
    assert named["service"]["compatible"] is False
    assert reported["consistent"] is False
    assert protocol.RELAUNCH in protocol.render(reported)


def test_doctor_reports_a_current_service_as_consistent(
    bridge, repo, paired, service
):
    store.initialize(bridge.home)
    reported = bridge.doctor()
    named = {entry["component"]: entry for entry in reported["components"]}
    assert named["service"]["state"] == protocol.OK
    assert named["service"]["version"] == protocol.launcher_version()
    assert reported["consistent"] is True


def test_doctor_reports_a_stopped_service_with_lanes_as_an_outage(
    bridge, repo, paired
):
    store.initialize(bridge.home)
    reported = bridge.doctor()
    named = {entry["component"]: entry for entry in reported["components"]}
    assert named["service"]["state"] == protocol.STOPPED
    assert named["service"]["remedy"] == protocol.START
    assert named["service"]["compatible"] is False
    assert reported["consistent"] is False
    assert protocol.START in protocol.render(reported)


def test_doctor_exits_non_zero_when_lanes_have_no_service(
    bridge, repo, paired, monkeypatch, capsys
):
    store.initialize(bridge.home)
    monkeypatch.setattr(
        sys, "argv", ["agent-parley", "--home", str(bridge.home), "doctor"]
    )
    assert cli.main() == 1
    assert protocol.START in capsys.readouterr().out


def test_doctor_stays_consistent_with_no_lane_registered(bridge):
    store.initialize(bridge.home)
    reported = bridge.doctor()
    named = {entry["component"]: entry for entry in reported["components"]}
    assert named["service"]["state"] == protocol.STOPPED
    assert named["service"]["remedy"] == ""
    assert named["service"]["compatible"] is True
    assert reported["consistent"] is True


def test_a_service_reads_its_own_sources_as_unchanged(
    bridge, service, monkeypatch
):
    monkeypatch.setattr(server, "REVISION_SECONDS", 0.0)
    assert protocol.revision() == service.revision
    assert service.drifted() is False
    assert not service.stopping.is_set()


def test_a_wrong_credential_is_refused_and_falls_back(
    bridge, repo, paired, service
):
    lane = Path(paired["lanes"]["codex"])
    path = lane.parent / "codex-identity.json"
    identity = json.loads(path.read_text())
    write_json(path, {**identity, "registration_token": "forged"})
    cwd = {"cwd": str(lane), "session_id": "s1"}
    served = run_hook(bridge, lane.parent, {**ALLOW, **cwd})
    assert served.returncode == 0
    assert "Participants" in json.dumps(json.loads(served.stdout))
    recorded = events(lane.parent)
    assert recorded[0]["reason_class"] == "service_fallback"
    assert "service answered 401" in recorded[0]["cause"]
    assert recorded[-1]["reason_class"] == "coordination_pending"


def test_a_credential_for_another_lane_is_refused(
    bridge, repo, paired, service
):
    lane = Path(paired["lanes"]["codex"])
    other = json.loads((lane.parent / "claude-identity.json").read_text())
    status, body = hook.request(
        bridge.config["port"],
        other["registration_token"],
        json.dumps(
            {
                "directory": str(lane.parent),
                "participant": "codex",
                "payload": {**ALLOW, "cwd": str(lane)},
            }
        ).encode(),
    )
    assert status == 403
    assert body == b""


def test_a_missing_identity_file_falls_back(bridge, repo, paired, service):
    lane = Path(paired["lanes"]["codex"])
    (lane.parent / "codex-identity.json").unlink()
    cwd = {"cwd": str(lane), "session_id": "s1"}
    served = run_hook(bridge, lane.parent, {**STOP, **cwd})
    assert served.returncode == 0
    assert served.stdout == "{}\n"
    assert "checkpoint failed" in served.stderr
    assert events(lane.parent)[0]["reason_class"] == "service_fallback"


def test_the_service_refuses_a_mismatched_protocol(
    bridge, repo, paired, service
):
    lane = Path(paired["lanes"]["codex"])
    identity = json.loads((lane.parent / "codex-identity.json").read_text())
    status, body = hook.request(
        bridge.config["port"],
        identity["registration_token"],
        json.dumps(
            {
                "directory": str(lane.parent),
                "participant": "codex",
                "protocol": 99,
                "payload": {**ALLOW, "cwd": str(lane)},
            }
        ).encode(),
    )
    assert status == 200
    served = json.loads(body)
    assert served["status"] == 2
    assert "speaks protocol 99" in served["stderr"]


def test_the_service_rejects_a_malformed_hook_request(
    bridge, repo, paired, service
):
    lane = Path(paired["lanes"]["codex"])
    identity = json.loads((lane.parent / "codex-identity.json").read_text())
    token = identity["registration_token"]
    assert hook.request(bridge.config["port"], token, b"[]")[0] == 400
    assert hook.request(bridge.config["port"], token, b"{")[0] == 400
    assert (
        hook.request(
            bridge.config["port"], token, b"x" * (server.MAX_HOOK_BYTES + 1)
        )[0]
        == 413
    )


def free_slots(instance):
    """Counts the worker slots the service currently has available."""
    taken = 0
    while instance.slots.acquire(blocking=False):
        taken += 1
    for _ in range(taken):
        instance.slots.release()
    return taken


def test_a_stalled_decision_is_held_and_frees_its_slot(
    bridge, repo, paired, service, monkeypatch, capsys
):
    lane = Path(paired["lanes"]["codex"])
    identity = json.loads((lane.parent / "codex-identity.json").read_text())
    token = identity["registration_token"]
    body = json.dumps(
        {
            "directory": str(lane.parent),
            "participant": "codex",
            "payload": {**ALLOW, "cwd": str(lane), "session_id": "s1"},
        }
    ).encode()
    deciding = checkpoints.serve
    budget = server.DECISION_SECONDS
    release = threading.Event()
    finished = threading.Event()

    def stalling(home, request, stages=None, settle=0.0, record_only=False):
        release.wait(20)
        try:
            return deciding(home, request, stages, settle, record_only)
        finally:
            finished.set()

    monkeypatch.setattr(server, "DECISION_SECONDS", 0.2)
    monkeypatch.setattr(server.checkpoints, "serve", stalling)
    status, reply = hook.request(bridge.config["port"], token, body)
    assert status == hook.DECIDING
    assert json.loads(reply)["detail"] == server.UNDECIDED
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and free_slots(service) < server.WORKERS:
        time.sleep(0.05)
    assert free_slots(service) == server.WORKERS
    entry = next(
        line
        for line in capsys.readouterr().out.splitlines()
        if " expired " in line
    )
    assert STAMP.match(entry)
    assert f"{hook.PATH} codex undecided" in entry
    assert token not in entry
    monkeypatch.setattr(server, "DECISION_SECONDS", budget)
    monkeypatch.setattr(server.checkpoints, "serve", deciding)
    release.set()
    assert finished.wait(20)
    deadline = time.monotonic() + 30
    answered = hook.DECIDING
    while answered != 200 and time.monotonic() < deadline:
        answered = hook.request(bridge.config["port"], token, body)[0]
        time.sleep(0.05)
    assert answered == 200


def test_the_connection_past_the_worker_cap_is_refused_in_the_log(
    bridge, service, capsys
):
    port = bridge.config["port"]
    holding = []
    try:
        for _ in range(server.WORKERS):
            holding.append(
                socket.create_connection(("127.0.0.1", port), timeout=5)
            )
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and free_slots(service):
            time.sleep(0.01)
        assert free_slots(service) == 0
        with socket.create_connection(("127.0.0.1", port), timeout=5) as extra:
            assert extra.recv(64) == b""
    finally:
        for held in holding:
            held.close()
    refused = [
        line
        for line in capsys.readouterr().out.splitlines()
        if " refused " in line
    ]
    assert len(refused) == 1
    assert STAMP.match(refused[0])
    assert (
        f"closed unanswered with all {server.WORKERS} worker slots busy"
        in refused[0]
    )


def test_the_hook_client_imports_no_engine_modules():
    heavy = (
        "sqlite3",
        "subprocess",
        "argparse",
        "http.client",
        "agent_parley.checkpoints",
        "agent_parley.store",
    )
    loaded = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; before = set(sys.modules); import agent_parley.hook; "
            f"print(sorted((set(sys.modules) - before) & set({heavy!r})))",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout.strip()
    assert loaded == "[]"


def test_the_client_reads_the_agent_spelling_of_the_lane_option():
    assert hook.options(["--agent", "codex", "--home", "/h"]) == {
        "participant": "codex",
        "home": "/h",
    }


def run_shell(bridge, directory, payload, trace=False):
    """Runs the generated shell client exactly as the launcher configures it."""
    client = hook.write_client(str(bridge.home), sys.executable)
    interpreter = shutil.which("bash") or "bash"
    return subprocess.run(
        [
            interpreter,
            *(["-x"] if trace else []),
            client,
            "--home",
            str(bridge.home),
            "--directory",
            str(directory),
            "--participant",
            "codex",
        ],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def shell_diagnosis(bridge, directory, payload):
    """Reports the interpreter and an execution trace for a wrong decision.

    A shell client that answers with the wrong decision and no diagnostics is
    unanswerable from a build log, and the interpreters this client runs on
    differ by platform. The trace names the statement that produced the
    answer, so a platform-specific failure is read rather than guessed at.
    """
    interpreter = shutil.which("bash") or "bash"
    version = subprocess.run(
        [interpreter, "--version"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    ).stdout.splitlines()[:1]
    traced = run_shell(bridge, directory, payload, trace=True)
    return (
        f"interpreter {interpreter}: {version}\n"
        f"exit {traced.returncode}\nstdout {traced.stdout!r}\n"
        f"trace {traced.stderr[-4000:]}"
    )


@pytest.mark.skipif(not shutil.which("bash"), reason="requires bash")
@pytest.mark.parametrize("payload", [ALLOW, DENY, STOP, START])
def test_the_shell_client_serves_the_decision_the_module_serves(
    bridge, repo, paired, service, payload
):
    lane = Path(paired["lanes"]["codex"])
    cwd = {"cwd": str(lane), "session_id": "s1"}
    shell = run_shell(bridge, lane.parent, {**payload, **cwd})
    (lane.parent / "codex-activity.json").unlink(missing_ok=True)
    served = run_hook(bridge, lane.parent, {**payload, **cwd})
    assert (shell.returncode, shell.stdout, shell.stderr) == (
        served.returncode,
        served.stdout,
        served.stderr,
    ), shell_diagnosis(bridge, lane.parent, {**payload, **cwd})
    assert not any(
        entry["reason_class"] == "service_fallback"
        for entry in events(lane.parent)
    )


@pytest.mark.skipif(not shutil.which("bash"), reason="requires bash")
def test_the_shell_client_starts_python_when_the_service_is_down(
    bridge, repo, paired
):
    lane = Path(paired["lanes"]["codex"])
    cwd = {"cwd": str(lane), "session_id": "s1"}
    shell = run_shell(bridge, lane.parent, {**DENY, **cwd})
    assert shell.returncode == 0, shell.stderr
    decision = json.loads(shell.stdout)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert events(lane.parent)[0]["reason_class"] == "service_fallback"


@pytest.mark.skipif(not shutil.which("bash"), reason="requires bash")
def test_the_shell_client_starts_python_when_the_service_fails(
    bridge, repo, paired, service, monkeypatch, capsys
):
    def broken(home, request, stages=None, settle=0.0, record_only=False):
        raise ImportError("cannot import name 'budgets'")

    monkeypatch.setattr(server.checkpoints, "serve", broken)
    lane = Path(paired["lanes"]["codex"])
    cwd = {"cwd": str(lane), "session_id": "s1"}
    shell = run_shell(bridge, lane.parent, {**DENY, **cwd})
    assert shell.returncode == 0, shell.stderr
    decision = json.loads(shell.stdout)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    recorded = events(lane.parent)
    assert recorded[0]["reason_class"] == "service_fallback"
    assert "service answered 500" in recorded[0]["cause"]


@pytest.mark.skipif(not shutil.which("bash"), reason="requires bash")
def test_the_shell_client_falls_back_on_a_forged_credential(
    bridge, repo, paired, service
):
    lane = Path(paired["lanes"]["codex"])
    path = lane.parent / "codex-identity.json"
    identity = json.loads(path.read_text())
    write_json(path, {**identity, "registration_token": "forged"})
    cwd = {"cwd": str(lane), "session_id": "s1"}
    shell = run_shell(bridge, lane.parent, {**ALLOW, **cwd})
    assert shell.returncode == 0, shell.stderr
    assert "Participants" in json.dumps(json.loads(shell.stdout))
    assert events(lane.parent)[0]["reason_class"] == "service_fallback"


@pytest.mark.skipif(not shutil.which("bash"), reason="requires bash")
def test_the_shell_client_forwards_both_served_streams_and_the_status(
    bridge, repo, paired, service, monkeypatch
):
    def loud(home, request, stages=None, settle=0.0, record_only=False):
        return {"stdout": '{"ok": true}\n', "stderr": "warned\n", "status": 2}

    monkeypatch.setattr(server.checkpoints, "serve", loud)
    lane = Path(paired["lanes"]["codex"])
    shell = run_shell(
        bridge, lane.parent, {**ALLOW, "cwd": str(lane), "session_id": "s1"}
    )
    assert (shell.returncode, shell.stdout, shell.stderr) == (
        2,
        '{"ok": true}\n',
        "warned\n",
    )


def test_the_shell_client_asks_for_a_timeout_bash_3_2_accepts(bridge):
    script = Path(hook.write_client(str(bridge.home), sys.executable))
    timeouts = re.findall(r"read[^\n]*?-t (\S+)", script.read_text())
    assert timeouts
    assert all(value.isdigit() and int(value) > 0 for value in timeouts)
    assert 'export AGENT_PARLEY_HOOK_PID="$$"' in script.read_text()
    assert '\\"hook_pid\\":$AGENT_PARLEY_HOOK_PID' in script.read_text()


@pytest.mark.skipif(not shutil.which("bash"), reason="requires bash")
def test_the_launcher_configures_the_shell_client(bridge, repo, paired):
    lane = Path(paired["lanes"]["codex"])
    command = bridge.hooks("codex", lane.parent)["PreToolUse"][0]["hooks"][0]
    assert hook.CLIENT_NAME in command["command"]
    assert cli.owned_hook({"bash": command["command"]}, "codex")
    assert not cli.owned_hook({"bash": command["command"]}, "claude")


def test_main_still_answers_a_direct_call(bridge, repo, paired, monkeypatch):
    lane = Path(paired["lanes"]["codex"])
    local = run_hook(
        bridge,
        lane.parent,
        {**ALLOW, "cwd": str(lane)},
        "agent_parley.checkpoints",
    )
    assert local.returncode == 0
    assert "Participants" in json.dumps(json.loads(local.stdout))
    assert events(lane.parent)[-1]["reason_class"] == "coordination_pending"
    assert checkpoints.Reason.SERVICE_FALLBACK.value == "service_fallback"


def gone(bridge):
    """Publishes the record a reboot or a drift exit leaves behind."""
    child = subprocess.Popen([sys.executable, "-c", ""])
    child.wait()
    write_json(
        bridge.home / "server.json", {"pid": child.pid, "start_ticks": "1"}
    )


def requests(monkeypatch):
    """Collects the relaunch requests instead of starting a service.

    Only a request to start this package is collected. Every other spawn
    reaches the real one, because reading a process's creation identity
    runs an external command on a platform without `/proc`, and answering
    that call with a recorder breaks the relaunch decision under test.
    """
    asked = []
    spawn = subprocess.Popen

    def collect(*args, **named):
        if args and "agent_parley" in " ".join(str(part) for part in args[0]):
            asked.append(args)
            return None
        return spawn(*args, **named)

    monkeypatch.setattr(subprocess, "Popen", collect)
    return asked


def test_an_outage_brings_the_service_back_on_the_next_hook(
    bridge, repo, paired
):
    lane = Path(paired["lanes"]["codex"])
    gone(bridge)
    decided = run_hook(
        bridge, lane.parent, {**ALLOW, "cwd": str(lane), "session_id": "s1"}
    )
    assert decided.returncode == 0, decided.stderr
    assert events(lane.parent)[0]["reason_class"] == "service_fallback"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and bridge.server_process() is None:
        time.sleep(0.2)
    assert bridge.server_process() is not None
    assert bridge.ready()


def test_one_relaunch_is_asked_for_within_the_interval(bridge, monkeypatch):
    gone(bridge)
    asked = requests(monkeypatch)
    hook.relaunch(str(bridge.home))
    hook.relaunch(str(bridge.home))
    assert len(asked) == 1
    stamp = bridge.home / hook.RELAUNCH_STAMP
    past = time.time() - hook.RELAUNCH_INTERVAL - 1
    os.utime(stamp, (past, past))
    hook.relaunch(str(bridge.home))
    assert len(asked) == 2


def test_no_relaunch_is_asked_for_without_a_recorded_service(
    bridge, monkeypatch
):
    asked = requests(monkeypatch)
    hook.relaunch(str(bridge.home))
    write_json(
        bridge.home / "server.json",
        {"pid": os.getpid(), "start_ticks": process.start_ticks(os.getpid())},
    )
    hook.relaunch(str(bridge.home))
    assert asked == []
    assert not (bridge.home / hook.RELAUNCH_STAMP).exists()


def held_lock(path, release):
    """Holds one lock from its own thread until the caller releases it."""
    taken = threading.Event()

    def hold():
        with lock(path):
            taken.set()
            release.wait(30)

    keeper = threading.Thread(target=hold, daemon=True)
    keeper.start()
    assert taken.wait(10)
    return keeper


def settled(directory, count, agent="codex"):
    """Waits for the event log to hold at least the expected records."""
    path = directory / f"{agent}-events.jsonl"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if path.exists() and len(events(directory, agent)) >= count:
            return
        time.sleep(0.05)


@pytest.mark.parametrize("payload", [ALLOW, PROMPT])
def test_a_held_checkpoint_lock_defers_rather_than_denying(
    bridge, repo, paired, service, payload
):
    lane = Path(paired["lanes"]["codex"])
    release = threading.Event()
    keeper = held_lock(lane.parent / "codex-checkpoint.lock", release)
    try:
        served = run_hook(
            bridge,
            lane.parent,
            {**payload, "cwd": str(lane), "session_id": "s1"},
        )
    finally:
        release.set()
        keeper.join(timeout=10)
    assert served.returncode == 0, served.stderr
    assert json.loads(served.stdout) == {}
    assert "deferred" in served.stderr
    settled(lane.parent, 1)
    assert [entry["reason_class"] for entry in events(lane.parent)] == [
        "lock_contended"
    ]


def stopped_under_a_held_lock(bridge, lane, meanwhile):
    """Sends Stop while the checkpoint lock is held for 1.5 seconds."""
    prompted = run_hook(
        bridge, lane.parent, {**PROMPT, "cwd": str(lane), "session_id": "s1"}
    )
    assert prompted.returncode == 0, prompted.stderr
    release = threading.Event()
    keeper = held_lock(lane.parent / "codex-checkpoint.lock", release)

    def later():
        time.sleep(0.5)
        meanwhile()
        time.sleep(1.0)
        release.set()

    threading.Thread(target=later, daemon=True).start()
    try:
        stopped = run_hook(
            bridge,
            lane.parent,
            {**STOP, "cwd": str(lane), "session_id": "s1"},
        )
    finally:
        release.set()
        keeper.join(timeout=10)
    assert stopped.returncode == 0, stopped.stderr
    settled(lane.parent, 2)
    return events(lane.parent)[-1]["reason_class"]


def test_a_served_stop_outlasts_a_held_checkpoint_lock(
    bridge, repo, paired, service
):
    lane = Path(paired["lanes"]["codex"])
    reason = stopped_under_a_held_lock(bridge, lane, lambda: None)
    state = json.loads((lane.parent / "codex-activity.json").read_text())
    assert reason != "lock_contended"
    assert state["activity"] == "idle"


def test_a_served_stop_yields_to_a_newer_event(bridge, repo, paired, service):
    lane = Path(paired["lanes"]["codex"])
    path = lane.parent / "codex-activity.json"

    def newer():
        state = json.loads(path.read_text())
        state["updated"] = time.time()
        state["activity"] = "working"
        write_json(path, state)

    reason = stopped_under_a_held_lock(bridge, lane, newer)
    assert reason == "superseded"
    assert json.loads(path.read_text())["activity"] == "working"


def stalling(monkeypatch, seconds):
    """Delays every served decision past the service's own deadline."""
    served = server.checkpoints.serve
    asked = []

    def slow(home, request, stages=None, settle=0.0, record_only=False):
        asked.append(record_only)
        time.sleep(seconds)
        return served(home, request, stages, settle, record_only)

    monkeypatch.setattr(server.checkpoints, "serve", slow)
    return asked


@pytest.mark.parametrize("payload", [ALLOW, PROMPT])
def test_a_decision_past_its_deadline_is_never_decided_again(
    bridge, repo, paired, service, monkeypatch, payload
):
    asked = stalling(monkeypatch, server.DECISION_SECONDS + 0.5)
    lane = Path(paired["lanes"]["codex"])
    served = run_hook(
        bridge, lane.parent, {**payload, "cwd": str(lane), "session_id": "s1"}
    )
    assert served.returncode == 0, served.stderr
    assert json.loads(served.stdout) == {}
    settled(lane.parent, 1)
    assert len(asked) == 1
    assert len(events(lane.parent)) == 1


def hook_body(lane, payload=ALLOW):
    """Builds the hook request body the service accepts for a lane."""
    return json.dumps(
        {
            "directory": str(lane.parent),
            "participant": "codex",
            "payload": {**payload, "cwd": str(lane), "session_id": "s1"},
        }
    ).encode()


def test_a_lane_only_records_events_while_one_is_abandoned(
    bridge, repo, paired, service, monkeypatch, capsys
):
    deciding = checkpoints.serve
    asked = stalling(monkeypatch, server.DECISION_SECONDS * 2)
    lane = Path(paired["lanes"]["codex"])
    identity = json.loads((lane.parent / "codex-identity.json").read_text())
    token = identity["registration_token"]
    body = hook_body(lane)
    port = bridge.config["port"]
    answers = [
        hook.request(port, token, body)
        for _ in range(server.UNDECIDED_LIMIT + 2)
    ]
    assert [status for status, _ in answers] == [hook.DECIDING] * len(answers)
    assert json.loads(answers[-1][1])["detail"] == server.UNDECIDED
    limit = server.UNDECIDED_LIMIT
    assert asked == [False] * limit + [True] * (len(answers) - limit)
    entries = capsys.readouterr().out.splitlines()
    held = [line for line in entries if "already has" in line]
    assert len(held) == 1
    root = roster.read(lane.parent)["root"]
    assert f"codex of {root} already has" in held[0]
    assert "recorded without building context" in held[0]
    assert token not in held[0]
    monkeypatch.setattr(server.checkpoints, "serve", deciding)
    deadline = time.monotonic() + 30
    answered = hook.DECIDING
    while answered != 200 and time.monotonic() < deadline:
        answered = hook.request(port, token, body)[0]
        time.sleep(0.05)
    assert answered == 200


def test_turn_ending_events_are_recorded_past_an_abandoned_decision(
    bridge, repo, paired, service, monkeypatch
):
    lane = Path(paired["lanes"]["codex"])
    directory = lane.parent
    identity = json.loads((directory / "codex-identity.json").read_text())
    token = identity["registration_token"]
    port = bridge.config["port"]
    scanning = checkpoints.scan
    release = threading.Event()

    def stuck(*args):
        release.wait(20)
        return scanning(*args)

    monkeypatch.setattr(checkpoints, "scan", stuck)
    command = {"tool_name": "Bash", "tool_input": {"command": "pytest -q"}}
    testing = {"hook_event_name": "PreToolUse", **command}
    finished = {"hook_event_name": "PostToolUse", **command}
    try:
        assert hook.request(port, token, hook_body(lane, testing))[0] == (
            hook.DECIDING
        )
        assert hook.request(port, token, hook_body(lane, finished))[0] == 200
        assert checkpoints.activity(directory, "codex")["activity"] == (
            "working"
        )
        assert hook.request(port, token, hook_body(lane, STOP))[0] == 200
        assert checkpoints.activity(directory, "codex")["activity"] == "idle"
    finally:
        release.set()
    settled(directory, 3)
    assert [entry["event"] for entry in events(directory)] == [
        "PostToolUse",
        "Stop",
        "PreToolUse",
    ]
    state = checkpoints.activity(directory, "codex")
    assert state["activity"] == "idle"
    assert state["event"] == "Stop"


def test_an_abandoned_decision_holds_only_its_own_project(
    bridge, repo, paired, service, monkeypatch, tmp_path
):
    second = tmp_path / "second"
    second.mkdir()
    cli.git(second, "init")
    (second / "shared.txt").write_text("original\n")
    cli.git(second, "add", "shared.txt")
    cli.git(
        second,
        "-c",
        "user.name=Bridge Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-m",
        "Second fixture",
    )
    other = bridge.add_participant(second, "codex", "codex")
    asyncio.run(bridge.identity("codex", other))
    first = Path(paired["lanes"]["codex"])
    elsewhere = Path(other["lanes"]["codex"])
    served = checkpoints.serve
    asked = []
    release = threading.Event()

    def selective(home, request, stages=None, settle=0.0, record_only=False):
        asked.append((request["directory"], record_only))
        if request["directory"] == str(first.parent):
            release.wait(20)
        return served(home, request, stages, settle, record_only)

    monkeypatch.setattr(server.checkpoints, "serve", selective)
    port = bridge.config["port"]
    tokens = [
        json.loads((lane.parent / "codex-identity.json").read_text())[
            "registration_token"
        ]
        for lane in (first, elsewhere)
    ]
    try:
        held = hook.request(port, tokens[0], hook_body(first))[0]
        status, _ = hook.request(port, tokens[1], hook_body(elsewhere))
    finally:
        release.set()
    assert held == hook.DECIDING
    assert status == 200
    assert asked[-1] == (str(elsewhere.parent), False)


def test_an_expired_decision_names_the_step_that_held_it(
    bridge, repo, paired, service, monkeypatch, capsys
):
    release = threading.Event()

    def holding(home, request, stages=None, settle=0.0, record_only=False):
        stages.enter("mail")
        release.wait(20)
        return {"status": 0, "stdout": "{}\n", "stderr": ""}

    monkeypatch.setattr(server.checkpoints, "serve", holding)
    lane = Path(paired["lanes"]["codex"])
    identity = json.loads((lane.parent / "codex-identity.json").read_text())
    try:
        status, _ = hook.request(
            bridge.config["port"],
            identity["registration_token"],
            hook_body(lane),
        )
    finally:
        release.set()
    assert status == hook.DECIDING
    entry = next(
        line
        for line in capsys.readouterr().out.splitlines()
        if " expired " in line
    )
    assert re.search(r"undecided after \d+\.\d{3} seconds", entry)
    assert re.search(r"in mail \d+\.\d{3}s$", entry)


def test_a_slow_served_decision_is_logged_with_its_duration(
    bridge, repo, paired, service, monkeypatch, capsys
):
    def unhurried(home, request, stages=None, settle=0.0, record_only=False):
        stages.enter("scan")
        time.sleep(0.2)
        return {"status": 0, "stdout": "{}\n", "stderr": ""}

    monkeypatch.setattr(server, "SLOW_DECISION", 0.05)
    monkeypatch.setattr(server.checkpoints, "serve", unhurried)
    lane = Path(paired["lanes"]["codex"])
    identity = json.loads((lane.parent / "codex-identity.json").read_text())
    status, _ = hook.request(
        bridge.config["port"], identity["registration_token"], hook_body(lane)
    )
    assert status == 200
    entry = next(
        line
        for line in capsys.readouterr().out.splitlines()
        if " decided " in line
    )
    assert STAMP.match(entry)
    assert re.search(r"codex in \d+\.\d{3} seconds: ", entry)
    assert re.search(r"in scan \d+\.\d{3}s$", entry)


def test_a_served_decision_times_every_step_it_walked(bridge, repo, paired):
    lane = Path(paired["lanes"]["codex"])
    stages = checkpoints.Stages()
    served = checkpoints.serve(
        bridge.home,
        {
            "directory": str(lane.parent),
            "participant": "codex",
            "payload": {**ALLOW, "cwd": str(lane), "session_id": "s1"},
        },
        stages,
    )
    assert served["status"] == 0
    assert [name for name, _ in stages.spent] == [
        "start",
        "process",
        "roster",
        "session",
        "guard",
        "scan",
        "lock",
        "state",
        "mail",
    ]
    assert all(seconds >= 0 for _, seconds in stages.spent)
    assert re.search(r"^start \d+\.\d{3}s ", stages.report())
    assert re.search(r"in record \d+\.\d{3}s$", stages.report())


def test_a_client_that_stopped_reading_is_recorded_as_one_line(
    bridge, service, capsys
):
    try:
        raise BrokenPipeError(32, "Broken pipe")
    except BrokenPipeError:
        service.handle_error(None, ("127.0.0.1", 0))
    entry = next(
        line
        for line in capsys.readouterr().out.splitlines()
        if " unanswered " in line
    )
    assert STAMP.match(entry)
    assert "stopped reading before the reply was written" in entry
    assert "BrokenPipeError" not in entry
    try:
        raise ValueError("an unexpected failure")
    except ValueError:
        service.handle_error(None, ("127.0.0.1", 0))
    recorded = capsys.readouterr().out
    assert " failed request handling" in recorded
    assert "ValueError: an unexpected failure" in recorded


@pytest.mark.skipif(not shutil.which("bash"), reason="requires bash")
def test_the_shell_client_leaves_a_running_decision_alone(
    bridge, repo, paired, service, monkeypatch
):
    asked = stalling(monkeypatch, server.DECISION_SECONDS + 0.5)
    lane = Path(paired["lanes"]["codex"])
    shell = run_shell(
        bridge, lane.parent, {**PROMPT, "cwd": str(lane), "session_id": "s1"}
    )
    assert shell.returncode == 0, shell.stderr
    assert json.loads(shell.stdout) == {}
    settled(lane.parent, 1)
    assert len(asked) == 1
    assert not any(
        entry["reason_class"] == "service_fallback"
        for entry in events(lane.parent)
    )


def test_the_hook_budget_covers_the_worst_bounded_path():
    assert (
        hook.CONNECT_TIMEOUT
        + server.DECISION_SECONDS
        + checkpoints.LOCK_SECONDS
        <= checkpoints.HOOK_TIMEOUT
    )
