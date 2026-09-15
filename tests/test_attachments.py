"""Oversized payloads spill to attachments and travel as references."""

import asyncio
import sys
from pathlib import Path

import pytest

from agent_parley import attachments, checkpoints, cli, issues, metrics, store
from agent_parley.checkpoints import MAX_CONTEXT_BYTES, checkpoint
from agent_parley.state import BridgeError, write_json


def actors(bridge, data):
    """Authenticates every registered participant against the store."""
    store.initialize(bridge.home)
    return {
        name: store.authenticate(
            bridge.home,
            asyncio.run(bridge.identity(name, data))["registration_token"],
        )
        for name in data["participants"]
    }


def sent(bridge, sender, body, key="big-1", to=("codex",)):
    """Sends one message and returns the tool result."""
    return store.call(
        bridge.home,
        sender,
        "send_message",
        {
            "to": list(to),
            "subject": "Evidence",
            "body_md": body,
            "idempotency_key": key,
        },
    )


def run(bridge, monkeypatch, capsys, *arguments):
    """Runs one CLI invocation and returns its standard output."""
    monkeypatch.setattr(
        sys, "argv", ["agent-parley", "--home", str(bridge.home), *arguments]
    )
    assert cli.main() == 0
    return capsys.readouterr().out


def test_a_message_over_the_cap_is_attached_and_referenced(
    bridge, repo, paired
):
    lanes = actors(bridge, paired)
    directory = bridge.project(repo)[1]
    small = sent(bridge, lanes["claude"], "fits", key="small-1")
    assert "attachment" not in small
    body = "line of evidence\n" * 400
    result = sent(bridge, lanes["claude"], body)
    reference = result["attachment"]
    assert reference == f"message-{result['id']}"
    assert result["attachment_bytes"] == len(body.encode())
    stored = store.read_message(
        bridge.home, paired["root"], "codex", result["id"]
    )["body_md"]
    marker = attachments.marker(reference, len(body.encode()))
    assert stored.endswith(marker)
    assert len(stored.encode()) <= store.MAX_BODY_BYTES
    assert (
        attachments.folder(directory) / f"{reference}.md"
    ).read_text() == body
    repeat = sent(bridge, lanes["claude"], body)
    assert repeat == {
        "id": result["id"],
        "thread_id": result["thread_id"],
        "duplicate": True,
    }
    write_json(directory / "codex-identity.json", {"name": "codex"})
    notice = checkpoint(
        bridge.home,
        directory,
        "codex",
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "test",
            "cwd": paired["lanes"]["codex"],
        },
    )["hookSpecificOutput"]["additionalContext"]
    assert marker in notice
    assert len(notice.encode()) <= MAX_CONTEXT_BYTES


def test_read_attachment_pages_and_is_scoped_to_the_addressed(
    bridge, repo, paired
):
    data = bridge.add_participant(repo, "kimi-1", "kimi")
    lanes = actors(bridge, data)
    body = "".join(f"{index:05d}\n" for index in range(1000))
    reference = sent(bridge, lanes["claude"], body)["attachment"]
    first = store.call(
        bridge.home, lanes["codex"], "read_attachment", {"reference": reference}
    )
    assert first["body"] == body[: attachments.PAGE_CHARACTERS]
    assert first["bytes"] == len(body.encode())
    second = store.call(
        bridge.home,
        lanes["claude"],
        "read_attachment",
        {"reference": reference, "offset": first["next_offset"]},
    )
    assert second["body"].startswith(body[attachments.PAGE_CHARACTERS :][:6])
    with pytest.raises(BridgeError, match="readable"):
        store.call(
            bridge.home,
            lanes["kimi-1"],
            "read_attachment",
            {"reference": reference},
        )
    for shaped in ("../message-1", "message-1/../x", "message-1.md", "/etc"):
        with pytest.raises(BridgeError, match="reference"):
            store.call(
                bridge.home,
                lanes["codex"],
                "read_attachment",
                {"reference": shaped},
            )
    with pytest.raises(BridgeError, match="readable"):
        store.call(
            bridge.home,
            lanes["codex"],
            "read_attachment",
            {"reference": "message-999"},
        )


def test_attachment_and_lane_caps_are_enforced(
    bridge, repo, paired, monkeypatch
):
    lanes = actors(bridge, paired)
    monkeypatch.setattr(attachments, "MAX_ATTACHMENT_BYTES", 6000)
    with pytest.raises(BridgeError, match="body_md"):
        sent(bridge, lanes["claude"], "x" * 6001, key="too-big")
    monkeypatch.setattr(attachments, "MAX_LANE_BYTES", 9000)
    sent(bridge, lanes["claude"], "y" * 5000, key="first")
    with pytest.raises(BridgeError, match="allowance"):
        sent(bridge, lanes["claude"], "z" * 5000, key="second")
    assert sent(bridge, lanes["codex"], "w" * 5000, key="peer", to=("claude",))


def test_report_evidence_over_the_cap_is_attached(
    bridge, repo, paired, monkeypatch, capsys
):
    lane = Path(paired["lanes"]["claude"])
    directory = bridge.project(repo)[1]
    with pytest.raises(BridgeError, match="summary"):
        bridge.report(lane, "ready", "s" * 5000, "", "pytest: 1 passed")
    evidence = "pytest: 1 passed\n" + "assert 1 == 1\n" * 500
    bridge.report(lane, "ready", "done", "", evidence)
    record = metrics.report_records(directory, "claude")[-1]
    reference = record["attachment"]
    assert reference == f"report-{record['id']}"
    assert record["evidence"].endswith(
        attachments.marker(reference, len(evidence.encode()))
    )
    assert len(record["evidence"].encode()) <= metrics.MAX_REPORT_BYTES
    assert attachments.body(directory, reference, "claude") == evidence
    with pytest.raises(BridgeError, match="readable"):
        attachments.body(directory, reference, "codex")
    shown = run(
        bridge,
        monkeypatch,
        capsys,
        "report",
        "show",
        record["id"],
        "--repo",
        str(lane),
    )
    assert reference in shown and "assert 1 == 1\n" * 500 not in shown
    full = run(
        bridge,
        monkeypatch,
        capsys,
        "report",
        "show",
        record["id"],
        "--repo",
        str(lane),
        "--full",
    )
    assert evidence in full


def test_report_rotation_removes_the_attachment(
    bridge, repo, paired, monkeypatch
):
    lane = Path(paired["lanes"]["claude"])
    directory = bridge.project(repo)[1]
    monkeypatch.setattr(metrics, "MAX_REPORT_LOG_BYTES", 1)
    monkeypatch.setattr(metrics, "MAX_REPORT_RECORDS", 1)
    bridge.report(lane, "ready", "first", "", "e\n" * 3000)
    reference = metrics.report_records(directory, "claude")[-1]["attachment"]
    path = attachments.folder(directory) / f"{reference}.md"
    assert path.exists()
    bridge.report(lane, "ready", "second", "", "pytest: 1 passed")
    assert not path.exists()
    assert not (attachments.folder(directory) / f"{reference}.json").exists()


def test_an_offer_over_the_cap_is_attached_until_it_is_answered(
    bridge, repo, paired, monkeypatch
):
    lanes = {name: Path(path) for name, path in paired["lanes"].items()}
    directory = bridge.project(repo)[1]
    bridge.issue(lanes["claude"], "claim", "42")
    summary = ("design note\n" * 300).strip()
    offered = bridge.issue(
        lanes["claude"], "offer", "42", to="codex", summary=summary
    )["offer"]
    reference = offered["attachment"]
    assert reference == f"offer-{offered['id']}"
    marker = attachments.marker(reference, len(summary.encode()))
    assert offered["summary"].endswith(marker)
    assert len(offered["summary"].encode()) <= issues.MAX_SUMMARY_BYTES
    assert attachments.body(directory, reference, "codex") == summary
    monkeypatch.setattr(
        checkpoints, "mailbox", lambda *args: {"pending_ack": 0, "messages": []}
    )
    write_json(directory / "codex-identity.json", {"name": "codex"})
    notice = checkpoint(
        bridge.home,
        directory,
        "codex",
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "test",
            "cwd": str(lanes["codex"]),
        },
    )["hookSpecificOutput"]["additionalContext"]
    assert marker in notice
    assert len(notice.encode()) <= MAX_CONTEXT_BYTES
    bridge.issue(lanes["codex"], "decline", "42", offer_id=offered["id"])
    assert not (attachments.folder(directory) / f"{reference}.md").exists()
    offered = bridge.issue(
        lanes["claude"], "offer", "42", to="codex", summary=summary
    )["offer"]
    kept = attachments.folder(directory) / f"{offered['attachment']}.md"
    accepted = bridge.issue(
        lanes["codex"], "accept", "42", offer_id=offered["id"]
    )
    assert accepted["attachment"] == offered["attachment"] and kept.exists()
    bridge.issue(lanes["codex"], "release", "42")
    assert not kept.exists()


def test_mail_show_prints_the_reference_and_the_whole_body_on_request(
    bridge, repo, paired, monkeypatch, capsys
):
    lanes = actors(bridge, paired)
    body = "diff --git a b\n" + "+added\n" * 1000
    result = sent(bridge, lanes["claude"], body)
    codex = paired["lanes"]["codex"]
    shown = run(
        bridge,
        monkeypatch,
        capsys,
        "mail",
        "show",
        str(result["id"]),
        "--repo",
        codex,
    )
    assert result["attachment"] in shown and "+added\n" * 1000 not in shown
    full = run(
        bridge,
        monkeypatch,
        capsys,
        "mail",
        "show",
        str(result["id"]),
        "--repo",
        codex,
        "--full",
    )
    assert body in full
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agent-parley",
            "--home",
            str(bridge.home),
            "mail",
            "show",
            "999",
            "--repo",
            codex,
        ],
    )
    assert cli.main() != 0
