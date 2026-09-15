"""Checks the hook client against the service and its in-process fallback."""

import asyncio
import json
import shutil
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from agent_parley import checkpoints, cli, hook, server, store
from agent_parley.state import write_json

ALLOW = {"hook_event_name": "PreToolUse", "tool_name": "Read", "tool_input": {}}
DENY = {
    "hook_event_name": "PreToolUse",
    "tool_name": "Bash",
    "tool_input": {"command": "git checkout -b elsewhere"},
}
STOP = {"hook_event_name": "Stop", "stop_hook_active": False}
START = {"hook_event_name": "SessionStart", "session_id": "s1"}


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
    assert "cannot import name 'budgets'" in capsys.readouterr().err


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
