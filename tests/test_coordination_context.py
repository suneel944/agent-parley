"""Checks that pending coordination arrives as context, not as a denial."""

import time
from pathlib import Path

import pytest

from agent_parley import checkpoints, store
from agent_parley.state import write_json


@pytest.fixture
def lanes(bridge, paired):
    """Authenticates claude and names the codex lane that receives mail."""
    lane = Path(paired["lanes"]["codex"])
    store.initialize(bridge.home)
    registered = {
        name: store.register(bridge.home, paired["root"], name)
        for name in ("claude", "codex")
    }
    write_json(lane.parent / "codex-identity.json", {"name": "codex"})
    return {
        "actor": store.authenticate(
            bridge.home, registered["claude"]["registration_token"]
        ),
        "lane": lane,
        "directory": lane.parent,
        "root": paired["root"],
    }


def send(bridge, lanes, key, **extra):
    """Mails codex from claude."""
    return store.call(
        bridge.home,
        lanes["actor"],
        "send_message",
        {
            "to": ["codex"],
            "subject": f"Status {key}",
            "body_md": "Parser half done.",
            "idempotency_key": key,
            **extra,
        },
    )


def hook(bridge, lanes, event, **extra):
    """Runs one codex checkpoint decision."""
    payload = {
        "hook_event_name": event,
        "cwd": str(lanes["lane"]),
        "session_id": "s1",
        **extra,
    }
    return checkpoints.checkpoint(
        bridge.home, lanes["directory"], "codex", payload
    )


def reserve(bridge, lanes, path):
    """Makes claude hold an exclusive reservation on one path."""
    store.call(
        bridge.home,
        lanes["actor"],
        "file_reservation_paths",
        {"paths": [path], "ttl_seconds": 300, "exclusive": True},
    )


def test_status_mail_rides_a_bash_call_as_context(bridge, paired, lanes):
    send(bridge, lanes, "one")

    result = hook(
        bridge,
        lanes,
        "PreToolUse",
        tool_name="Bash",
        tool_input={"command": "make check"},
    )

    details = result["hookSpecificOutput"]
    assert "permissionDecision" not in details
    assert "Status one" in details["additionalContext"]
    assert "Mail: 1 of 1 unread" in details["additionalContext"]
    mail = checkpoints.mailbox(bridge.home, lanes["root"], "codex")
    assert mail["unread"] == 0


def test_a_write_on_a_peer_reservation_is_denied_naming_the_holder(
    bridge, paired, lanes
):
    reserve(bridge, lanes, "src/parser.py")
    target = str(lanes["lane"] / "src" / "parser.py")

    denied = hook(
        bridge,
        lanes,
        "PreToolUse",
        tool_name="Write",
        tool_input={"file_path": target, "content": "x"},
    )["hookSpecificOutput"]

    assert denied["permissionDecision"] == "deny"
    assert "src/parser.py" in denied["permissionDecisionReason"]
    assert "claude" in denied["permissionDecisionReason"]
    summary = checkpoints.event_summary(lanes["directory"], "codex")
    assert {
        "reason": "reserved_path",
        "tool": "Write",
        "count": 1,
    } in summary["denied_by"]


def test_a_read_is_never_denied_for_coordination(bridge, paired, lanes):
    send(bridge, lanes, "two")
    reserve(bridge, lanes, "src/parser.py")
    target = str(lanes["lane"] / "src" / "parser.py")

    for key in ("first", "second"):
        result = hook(
            bridge,
            lanes,
            "PreToolUse",
            tool_name="Read",
            tool_input={"file_path": target},
            tool_use_id=key,
        )
        assert "permissionDecision" not in result.get("hookSpecificOutput", {})


def test_stop_never_blocks_on_mail_and_the_debt_carries_forward(
    bridge, paired, lanes
):
    sent = send(bridge, lanes, "ack", ack_required=True)

    assert "decision" not in hook(bridge, lanes, "Stop")

    first = hook(bridge, lanes, "UserPromptSubmit", prompt="go on")
    assert "[ACK REQUIRED]" in first["hookSpecificOutput"]["additionalContext"]
    second = hook(bridge, lanes, "UserPromptSubmit", prompt="and again")
    assert (
        f"Acknowledgements owed: message {sent['id']} from claude"
        in second["hookSpecificOutput"]["additionalContext"]
    )


def test_an_offer_about_to_expire_denies_a_mutating_call(tmp_path):
    ledger = {
        "issues": {
            "7": {
                "offer": {
                    "id": "o1",
                    "to": "codex",
                    "deadline": time.time() + 30,
                }
            }
        }
    }
    payload = {"tool_name": "Bash", "tool_input": {"command": "make"}}

    reason, text = checkpoints.hazard(payload, tmp_path, "codex", {}, ledger)

    assert reason == checkpoints.Reason.OFFER_EXPIRING
    assert "issue accept 7 --offer-id o1" in text
    assert (
        checkpoints.hazard(
            {"tool_name": "Grep", "tool_input": {}},
            tmp_path,
            "codex",
            {},
            ledger,
        )
        is None
    )
