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

import pytest

from agent_parley import (
    checkpoints,
    cli,
    hook,
    process,
    protocol,
    server,
    store,
)
from agent_parley.state import write_json

ALLOW = {"hook_event_name": "PreToolUse", "tool_name": "Read", "tool_input": {}}
DENY = {
    "hook_event_name": "PreToolUse",
    "tool_name": "Bash",
    "tool_input": {"command": "git checkout -b elsewhere"},
}
STOP = {"hook_event_name": "Stop", "stop_hook_active": False}
START = {"hook_event_name": "SessionStart", "session_id": "s1"}
STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4} ")


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
    def broken(home, request):
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
    assert entry.endswith(f"failed {hook.PATH} codex")
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


def test_a_stalled_decision_is_answered_and_frees_its_slot(
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
    release = threading.Event()

    def stalling(home, request):
        release.wait(20)
        return deciding(home, request)

    monkeypatch.setattr(server, "DECISION_SECONDS", 0.2)
    monkeypatch.setattr(server.checkpoints, "serve", stalling)
    status, reply = hook.request(bridge.config["port"], token, body)
    assert status == 503
    assert json.loads(reply)["status"] == protocol.STALE
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
    monkeypatch.setattr(server.checkpoints, "serve", deciding)
    release.set()
    assert hook.request(bridge.config["port"], token, body)[0] == 200


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
    def broken(home, request):
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
    def loud(home, request):
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
    """Collects the relaunch requests instead of starting a service."""
    asked = []
    monkeypatch.setattr(
        subprocess, "Popen", lambda *args, **named: asked.append(args)
    )
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
