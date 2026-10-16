"""Checks the turn recap a lane keeps for the operator while it waits."""

import json
import os
import subprocess
from pathlib import Path

from agent_parley import (
    checkpoints,
    cli,
    dashboard,
    decisions,
    notify,
    recaps,
    records,
    roster,
    views,
    webview,
)


def git(path, *args):
    """Runs Git in a checkout and returns its stripped output."""
    return subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def transcript(paired, name, entries):
    """Writes a Claude transcript for one lane of the paired roster."""
    lane = Path(paired["lanes"][name])
    directory = (
        Path(os.environ["CLAUDE_CONFIG_DIR"])
        / "projects"
        / records.UNSAFE.sub("-", str(lane))
    )
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "session.jsonl").write_text(
        "".join(json.dumps(entry) + "\n" for entry in entries)
    )


def prompt(text):
    """Builds a Claude user prompt record."""
    return {"type": "user", "message": {"role": "user", "content": text}}


def reply(*parts):
    """Builds a Claude assistant record from content parts."""
    return {"type": "assistant", "message": {"content": list(parts)}}


def bash(identifier, command):
    """Builds a Claude Bash tool call part."""
    return {
        "type": "tool_use",
        "id": identifier,
        "name": "Bash",
        "input": {"command": command},
    }


def result(identifier, error):
    """Builds a Claude tool result record."""
    return {
        "type": "user",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": identifier,
                    "is_error": error,
                    "content": "out",
                }
            ]
        },
    }


def test_assemble_caps_every_part_and_the_whole_record():
    record = recaps.assemble(
        message="é" * 2000,
        files=[f"src/{index}/{'x' * 300}.py" for index in range(30)],
        commands=[f"pytest -k case{index} " + "y" * 400 for index in range(20)],
        failed=[f"make check {index} " + "z" * 400 for index in range(20)],
        started=10.0,
        now=20.0,
    )
    encoded = json.dumps(record, ensure_ascii=False).encode()
    assert len(encoded) <= recaps.MAX_RECAP_BYTES
    assert record["message"].endswith(recaps.ELLIPSIS)
    assert len(record["message"].encode()) <= recaps.MAX_MESSAGE_BYTES
    assert "�" not in record["message"]
    for name, total in (("files", 30), ("commands", 20), ("failed", 20)):
        kept = record[name]
        assert len(kept) + record[f"{name}_omitted"] == total
        assert all(len(item.encode()) <= recaps.MAX_ITEM_BYTES for item in kept)
    assert record["files"] == []
    assert record["failed"][-1].startswith("make check 19 ")


def test_assemble_keeps_a_short_turn_whole():
    record = recaps.assemble(
        message="Done with\nthe refactor.",
        files=["a.py"],
        commands=["ruff check ."],
        failed=[],
        started=0.0,
        now=5.0,
    )
    assert record["message"] == "Done with the refactor."
    assert record["files"] == ["a.py"]
    assert record["commands_omitted"] == 0


def test_format_recap_fits_its_byte_limit_without_splitting_text():
    recap = recaps.assemble(
        message="ü" * 400,
        files=["a.py", "b.py"],
        commands=["pytest -q"],
        failed=["pytest -q"],
        started=0.0,
        now=1.0,
    )
    whole = recaps.format_recap(recap, 4096)
    assert whole.splitlines()[0] == "Last turn:"
    assert "  changed: a.py, b.py" in whole
    assert "  failed: pytest -q" in whole
    cut = recaps.format_recap(recap, 120)
    assert len(cut.encode()) <= 120
    assert cut.endswith(recaps.ELLIPSIS)
    cut.encode().decode()
    assert recaps.format_recap(None, 100) == ""
    assert (
        recaps.format_recap(
            recaps.assemble(
                message="",
                files=[],
                commands=[],
                failed=[],
                started=0.0,
                now=0.0,
            ),
            100,
        )
        == ""
    )


def test_a_claude_turn_reads_its_last_message_commands_and_failures(
    bridge, repo, paired
):
    transcript(
        paired,
        "claude",
        [
            prompt("earlier work"),
            reply(bash("old", "echo before")),
            prompt("fix the build"),
            reply({"type": "text", "text": "Running checks."}),
            reply(bash("t1", "pytest -q"), bash("t2", "ruff check .")),
            result("t1", True),
            result("t2", False),
            reply({"type": "text", "text": "Need approval to push."}),
        ],
    )
    reading = records.turn_reading(
        bridge.home, paired["participants"]["claude"]
    )
    assert reading == {
        "message": "Need approval to push.",
        "commands": ["pytest -q", "ruff check ."],
        "failed": ["pytest -q"],
    }


def test_a_codex_turn_reads_failures_from_its_exit_codes():
    reading: dict = {}
    records._new_turn(reading)
    for record in (
        {"type": "event_msg", "payload": {"type": "user_message"}},
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "call_id": "c1",
                "arguments": json.dumps({"command": ["bash", "-lc", "make"]}),
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "c1",
                "output": "Exit code: 2\nWall time: 1s",
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "call_id": "c2",
                "arguments": json.dumps({"cmd": "ls"}),
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "c2",
                "output": json.dumps({"metadata": {"exit_code": 0}}),
            },
        },
        {
            "type": "event_msg",
            "payload": {"type": "agent_message", "message": "Blocked."},
        },
    ):
        records._codex_turn(record, reading)
    assert reading["message"] == "Blocked."
    assert reading["commands"] == ["bash -lc make", "ls"]
    assert reading["failed"] == ["bash -lc make"]


def test_the_recorded_head_matches_git_in_a_lane_worktree(paired):
    lane = Path(paired["lanes"]["codex"])
    assert checkpoints.recorded_head(lane) == git(lane, "rev-parse", "HEAD")
    git(lane, "pack-refs", "--all")
    assert checkpoints.recorded_head(lane) == git(lane, "rev-parse", "HEAD")
    assert checkpoints.recorded_head(lane / "missing") == ""


def test_changed_files_cover_commits_edits_and_new_files(paired):
    lane = Path(paired["lanes"]["codex"])
    base = git(lane, "rev-parse", "HEAD")
    (lane / "committed.txt").write_text("one\n")
    git(lane, "add", "committed.txt")
    git(lane, "commit", "-qm", "add")
    (lane / "shared.txt").write_text("edited\n")
    (lane / "new.txt").write_text("new\n")
    assert recaps.changed_files(lane, base) == [
        "committed.txt",
        "new.txt",
        "shared.txt",
    ]
    assert recaps.changed_files(lane, "0" * 40) == ["new.txt", "shared.txt"]


def test_a_recap_is_kept_until_the_lane_moves_again(
    bridge, repo, paired, monkeypatch
):
    _, directory = bridge.project(repo, create=False)
    participant = roster.read(directory)["participants"]["claude"]
    calls = []

    def taken(home, directory, name, participant, now=None):
        calls.append(name)
        return {"at": 50.0, "message": "m"}

    monkeypatch.setattr(recaps, "capture", taken)
    (directory / "claude-activity.json").write_text(
        json.dumps({"updated": 40.0})
    )
    (directory / "claude-recap.json").write_text(json.dumps({"at": 45.0}))
    assert recaps.ensure(bridge.home, directory, "claude", participant) == {
        "at": 45.0
    }
    assert calls == []
    (directory / "claude-activity.json").write_text(
        json.dumps({"updated": 60.0})
    )
    assert (
        recaps.ensure(bridge.home, directory, "claude", participant)["message"]
        == "m"
    )
    assert calls == ["claude"]


def test_capture_stores_the_turn_and_detail_prints_it(bridge, repo, paired):
    _, directory = bridge.project(repo, create=False)
    participant = roster.read(directory)["participants"]["claude"]
    lane = Path(participant["lane"])
    (directory / "claude-activity.json").write_text(
        json.dumps(
            {
                "updated": 1.0,
                "turn": {
                    "started": 1.0,
                    "head": git(lane, "rev-parse", "HEAD"),
                },
            }
        )
    )
    (lane / "draft.txt").write_text("x\n")
    transcript(
        paired,
        "claude",
        [prompt("go"), reply({"type": "text", "text": "Waiting on you."})],
    )
    stored = recaps.capture(bridge.home, directory, "claude", participant, 9.0)
    assert stored["files"] == ["draft.txt"]
    assert stored["started"] == 1.0
    assert recaps.lane_recap(directory, "claude") == stored
    assert recaps.lane_recap(directory, "codex") is None
    assert cli.recap_lines(directory, "claude", now=19.0) == [
        "    Recap taken 10s ago:",
        "      Last turn:",
        "        said: Waiting on you.",
        "        changed: draft.txt",
    ]
    assert cli.recap_lines(directory, "codex") == []


def test_the_decision_message_carries_the_recap_above_its_buttons(
    bridge, repo, paired, monkeypatch
):
    _, directory = bridge.project(repo, create=False)
    sent: list[dict] = []
    monkeypatch.setitem(
        notify.TRANSPORTS,
        "fake",
        lambda config, subject, body: sent.append(
            {"body": body, "markup": config.get("markup")}
        ),
    )
    monkeypatch.setattr(
        recaps,
        "ensure",
        lambda home, directory, name, participant: {
            "message": f"{name} wants to push",
            "commands": ["git push"],
        },
    )
    record = decisions.open_or_refresh(
        directory,
        project=str(repo),
        lane="claude",
        kind="approval",
        key="k",
        question="Approve git push?",
        options=("allow", "deny"),
        now=100.0,
    )
    decisions.flush(directory, {"transports": ["fake"]}, now=101.0)
    body = sent[0]["body"]
    lines = body.splitlines()
    assert lines[lines.index("Last turn:") - 1].startswith("reply: decide")
    assert "  said: claude wants to push" in lines
    assert "  ran: git push" in lines
    assert sent[0]["markup"]["inline_keyboard"]
    assert "recap" not in decisions.get(directory, record["id"])
    assert len(body.encode()) <= notify.MAX_MESSAGE_BYTES


def wait_on_operator(directory, with_recap):
    """Opens a decision for claude and stores a recap when asked."""
    decisions.open_or_refresh(
        directory,
        project="/repo",
        lane="claude",
        kind="handoff_offered",
        key="k",
        question="A handoff offer is waiting",
        options=("accept", "decline"),
    )
    if with_recap:
        recaps.path(directory, "claude").write_text(
            json.dumps(
                {
                    "at": 5.0,
                    "message": "Pushed the branch",
                    "files": ["a.py", "b.py"],
                    "commands": ["pytest"],
                    "failed": ["pytest"],
                }
            )
        )


def test_every_view_shows_the_recap_of_a_lane_that_needs_input(
    bridge, repo, paired, capsys
):
    _, directory = bridge.project(repo, create=False)
    wait_on_operator(directory, True)
    note = "last turn: Pushed the branch; 1 failed, 2 files changed"
    reading = {
        record["participant"]: record
        for record in bridge.status_snapshot()["projects"][0]["participants"]
    }
    assert reading["claude"]["recap"]["failed"] == ["pytest"]
    assert reading["codex"]["recap"] is None
    bridge.status()
    assert f"  {note}" in capsys.readouterr().out.splitlines()
    view = dashboard.collect(bridge.home, False, {})
    rows = {
        row["participant"]: row
        for row in dashboard.select(view)["projects"][0]["rows"]
    }
    assert note in dashboard.notes(rows["claude"])
    assert not any(
        item.startswith("last turn:") for item in dashboard.notes(rows["codex"])
    )
    assert views._row(rows["claude"])["recap"]["message"] == "Pushed the branch"
    assert views._row(rows["codex"])["recap"] is None
    assert note in webview.page(view, 5)


def test_a_lane_without_a_recap_renders_no_last_turn_line(
    bridge, repo, paired, capsys
):
    _, directory = bridge.project(repo, create=False)
    wait_on_operator(directory, False)
    reading = bridge.status_snapshot()["projects"][0]["participants"]
    assert all(record["recap"] is None for record in reading)
    bridge.status()
    assert "last turn" not in capsys.readouterr().out
    view = dashboard.collect(bridge.home, False, {})
    rows = dashboard.select(view)["projects"][0]["rows"]
    assert not any(
        item.startswith("last turn:")
        for row in rows
        for item in dashboard.notes(row)
    )
    assert "last turn" not in webview.page(view, 5)
