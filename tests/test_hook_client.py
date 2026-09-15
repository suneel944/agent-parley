"""Checks the hook client against the service and its in-process fallback."""

import asyncio
import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from agent_parley import checkpoints, hook, server, store
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
