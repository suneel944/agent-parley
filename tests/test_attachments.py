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


def test_a_rolled_back_message_leaves_no_attachment(bridge, repo, paired):
    lanes = actors(bridge, paired)
    directory = bridge.project(repo)[1]
    body = "line of evidence\n" * 400
    with (
        pytest.raises(RuntimeError),
        store.connect(bridge.home, write=True) as db,
    ):
        result = store._send(
            db,
            lanes["claude"],
            {
                "to": ["codex"],
                "subject": "Evidence",
                "body_md": body,
                "idempotency_key": "big-1",
            },
            directory=directory,
        )
        spilled = attachments.folder(directory) / f"{result['attachment']}.md"
        assert spilled.exists()
        raise RuntimeError("a later statement failed")
    assert not spilled.exists()
    assert not spilled.with_suffix(".json").exists()
    retried = sent(bridge, lanes["claude"], "fits", key="small-1")
    assert retried["id"] == result["id"]
    assert not spilled.exists()


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


def test_search_finds_text_only_in_a_spilled_body_after_a_rebuild(
    bridge, repo, paired
):
    lanes = actors(bridge, paired)
    tail = "line of evidence\n" * 400 + "rotate the signing key\n"
    mail = sent(bridge, lanes["claude"], tail)
    decision = store.call(
        bridge.home,
        lanes["claude"],
        "send_message",
        {
            "to": ["codex"],
            "subject": "Agreed",
            "body_md": tail,
            "idempotency_key": "decided-1",
            "decision": True,
        },
    )
    assert mail["attachment"] and decision["attachment"]

    def found():
        hits = store.call(
            bridge.home,
            lanes["codex"],
            "search_messages",
            {"query": "signing key"},
        )
        decided = store.call(
            bridge.home,
            lanes["codex"],
            "search_decisions",
            {"query": "signing key"},
        )
        assert hits["index"] == decided["index"] == "fts5"
        return (
            [row["id"] for row in hits["messages"]],
            [row["id"] for row in decided["messages"]],
        )

    expected = ([decision["id"], mail["id"]], [decision["id"]])
    assert found() == expected
    with store.connect(bridge.home, write=True) as db:
        db.execute(
            "INSERT INTO message_search(message_search) VALUES ('rebuild')"
        )
        db.execute(f"PRAGMA user_version={store.SCHEMA_VERSION - 1}")
    assert found() == ([], [])
    store.initialize(bridge.home)
    assert found() == expected
    with store.connect(bridge.home, write=True) as db:
        db.execute("DROP TRIGGER message_indexed")
    store.initialize(bridge.home)
    assert found() == expected


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


def test_a_full_allowance_frees_once_its_recipients_read_the_mail(
    bridge, repo, paired, monkeypatch
):
    lanes = actors(bridge, paired)
    directory = bridge.project(repo)[1]
    monkeypatch.setattr(attachments, "MAX_LANE_BYTES", 12000)
    first = sent(bridge, lanes["claude"], "a" * 5000, key="first")
    second = sent(bridge, lanes["claude"], "b" * 5000, key="second")
    with pytest.raises(BridgeError, match="read"):
        sent(bridge, lanes["claude"], "c" * 5000, key="third")
    store.call(
        bridge.home,
        lanes["codex"],
        "mark_message_read",
        {"message_id": first["id"]},
    )
    third = sent(bridge, lanes["claude"], "c" * 5000, key="third")
    assert third["attachment"]
    home = attachments.folder(directory)
    assert not (home / f"{first['attachment']}.md").exists()
    assert (home / f"{second['attachment']}.md").exists()
    assert attachments.used(directory, "claude") == 10000
    with pytest.raises(BridgeError, match="released"):
        store.call(
            bridge.home,
            lanes["codex"],
            "read_attachment",
            {"reference": first["attachment"]},
        )
    with pytest.raises(BridgeError, match="readable"):
        attachments.body(directory, first["attachment"], "kimi-1")
    stored = store.read_message(
        bridge.home, paired["root"], "codex", first["id"]
    )["body_md"]
    assert stored.startswith("a" * 100)


def test_a_read_decision_keeps_its_attachment_when_the_allowance_fills(
    bridge, repo, paired, monkeypatch
):
    lanes = actors(bridge, paired)
    directory = bridge.project(repo)[1]
    monkeypatch.setattr(attachments, "MAX_LANE_BYTES", 12000)
    decision = store.call(
        bridge.home,
        lanes["claude"],
        "send_message",
        {
            "to": ["codex"],
            "subject": "Interface agreed",
            "body_md": "d" * 5000,
            "idempotency_key": "decision",
            "decision": True,
        },
    )
    sent(bridge, lanes["claude"], "b" * 5000, key="second")
    store.call(
        bridge.home,
        lanes["codex"],
        "mark_message_read",
        {"message_id": decision["id"]},
    )
    with pytest.raises(BridgeError, match="read"):
        sent(bridge, lanes["claude"], "c" * 5000, key="third")
    home = attachments.folder(directory)
    assert (home / f"{decision['attachment']}.md").exists()


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


def test_a_decision_is_readable_whole_by_every_registered_lane(
    bridge, repo, paired
):
    data = bridge.add_participant(repo, "kimi-1", "kimi")
    lanes = actors(bridge, data)
    directory = bridge.project(repo)[1]
    body = "line of evidence\n" * 400
    result = store.call(
        bridge.home,
        lanes["claude"],
        "send_message",
        {
            "to": [],
            "subject": "Interface agreed",
            "body_md": body,
            "idempotency_key": "decision-1",
            "decision": True,
        },
    )
    reference = result["attachment"]
    for reader in ("codex", "kimi-1"):
        assert attachments.body(directory, reference, reader) == body


def test_operator_decide_and_speak_spill_a_long_body(bridge, repo, paired):
    actors(bridge, paired)
    directory = bridge.project(repo)[1]
    body = "line of evidence\n" * 400
    decided = bridge.decide(repo, body)
    assert attachments.body(directory, decided["attachment"], "codex") == body
    spoken = bridge.say(repo, "codex", body)
    assert attachments.body(directory, spoken["attachment"], "codex") == body


def test_releasing_with_an_offer_pending_removes_the_offer_attachment(
    bridge, repo, paired
):
    lanes = {name: Path(path) for name, path in paired["lanes"].items()}
    directory = bridge.project(repo)[1]
    bridge.issue(lanes["claude"], "claim", "42")
    summary = ("design note\n" * 300).strip()
    offered = bridge.issue(
        lanes["claude"], "offer", "42", to="codex", summary=summary
    )["offer"]
    pending = attachments.folder(directory) / f"{offered['attachment']}.md"
    assert pending.exists()
    released = bridge.issue(lanes["claude"], "release", "42")
    assert released["offer"] is None
    assert not pending.exists()


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
