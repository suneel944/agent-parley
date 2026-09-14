"""Follows one lane's coordination events without a terminal or a sleep."""

import json
import re
import sys

from agent_parley import checkpoints, cli, metrics, roster, store, watch

DENY = {"hookSpecificOutput": {"permissionDecision": "deny"}}
STAMPED = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z (\w+) (.+)$")


def registered(bridge, paired, name):
    """Registers one lane with the store and resolves its actor."""
    store.initialize(bridge.home)
    token = store.register(bridge.home, paired["root"], name)[
        "registration_token"
    ]
    actor = store.authenticate(bridge.home, token)
    assert actor is not None
    return actor


def hook(directory, name, event, tool="", output=None):
    """Appends one hook decision to a lane's event log."""
    checkpoints.record(
        directory,
        name,
        {"hook_event_name": event, "tool_name": tool},
        checkpoints.Reason.OBSERVED,
        output,
    )


def never(seconds):
    """Fails if the stream tries to sleep during a test."""
    raise AssertionError(f"slept {seconds}s")


def stream(bridge, repo, name, capsys, **options):
    """Runs one pass of the stream to a pipe and returns its lines."""
    directory = bridge.project(repo)[1]
    watch.run(
        bridge.home,
        directory,
        roster.read(directory),
        name,
        stop=lambda: True,
        sleep=never,
        **options,
    )
    return capsys.readouterr().out.splitlines()


def parsed(lines):
    """Splits plain lines into their kind and description."""
    matched = [STAMPED.match(line) for line in lines]
    assert all(matched), lines
    return [(match[1], match[2]) for match in matched if match]


def test_the_stream_starts_from_the_newest_twenty(bridge, repo, paired, capsys):
    directory = bridge.project(repo)[1]
    for number in range(25):
        hook(directory, "claude", "PreToolUse", f"tool-{number}", DENY)
    lines = parsed(stream(bridge, repo, "claude", capsys))
    assert len(lines) == 20
    assert lines[0] == ("denied", "denied tool-5 (observed)")
    assert lines[-1] == ("denied", "denied tool-24 (observed)")


def test_since_widens_the_backlog(bridge, repo, paired, capsys):
    directory = bridge.project(repo)[1]
    for number in range(25):
        hook(directory, "claude", "PreToolUse", f"tool-{number}", DENY)
    assert len(stream(bridge, repo, "claude", capsys, since=3600)) == 25


def test_kind_narrows_the_stream(bridge, repo, paired, capsys):
    directory = bridge.project(repo)[1]
    bridge.issue(paired["lanes"]["claude"], "claim", "42")
    hook(directory, "claude", "PreToolUse", "git", DENY)
    metrics.record_report(
        directory, "claude", {"kind": "report", "state": "ready"}
    )
    everything = parsed(stream(bridge, repo, "claude", capsys))
    assert ("claim", "claim issue 42") in everything
    assert ("denied", "denied git (observed)") in everything
    assert ("report", "report ready") in everything
    only = parsed(stream(bridge, repo, "claude", capsys, kinds=("claim",)))
    assert only == [("claim", "claim issue 42")]


def test_mail_and_served_calls_name_the_peer(bridge, repo, paired, capsys):
    codex = registered(bridge, paired, "codex")
    registered(bridge, paired, "claude")
    store.call(
        bridge.home,
        codex,
        "send_message",
        {
            "to": ["claude"],
            "subject": "Ready for review",
            "body_md": "Take a look.",
            "idempotency_key": "k1",
        },
    )
    received = parsed(stream(bridge, repo, "claude", capsys))
    assert received and received[-1][0] == "message"
    assert received[-1][1].startswith("mail from codex (thread ")
    sent = parsed(stream(bridge, repo, "codex", capsys))
    assert ("call", "call send_message ok") in sent
    assert any(
        kind == "message" and text.startswith("mail sent: Ready for review")
        for kind, text in sent
    )


def test_json_prints_one_object_per_line(bridge, repo, paired, capsys):
    directory = bridge.project(repo)[1]
    hook(directory, "claude", "PreToolUse", "git", DENY)
    bridge.issue(paired["lanes"]["claude"], "claim", "42")
    lines = stream(bridge, repo, "claude", capsys, json_lines=True)
    objects = [json.loads(line) for line in lines]
    assert [record["kind"] for record in objects] == ["denied", "claim"]
    for record in objects:
        assert record["at"].endswith("Z")
        assert record["participant"] == "claude"
        assert record["description"]
        assert "key" not in record
    assert objects[1]["issue"] == 42


def test_a_pipe_gets_plain_lines_without_cursor_control(
    bridge, repo, paired, capsys
):
    directory = bridge.project(repo)[1]
    hook(directory, "claude", "PreToolUse", "git", DENY)
    lines = stream(bridge, repo, "claude", capsys)
    assert len(lines) == 1
    assert STAMPED.match(lines[0])
    assert "\x1b" not in lines[0]


def test_a_rotation_between_polls_neither_drops_nor_repeats(
    bridge, repo, paired
):
    directory = bridge.project(repo)[1]
    manifest = roster.read(directory)
    for number in range(3):
        hook(directory, "claude", "PreToolUse", f"before-{number}", DENY)
    emitted = []
    polls = []

    def sleep(seconds):
        """Rotates the log between polls and appends past the rotation."""
        polls.append(seconds)
        if len(polls) == 1:
            with checkpoints.event_lock(directory, "claude", exclusive=True):
                (directory / "claude-events.jsonl").replace(
                    directory / "claude-events.1.jsonl"
                )
            hook(directory, "claude", "PreToolUse", "after-0", DENY)
            hook(directory, "claude", "PreToolUse", "after-1", DENY)

    watch.follow(
        lambda after: watch.collect(
            bridge.home, directory, manifest, "claude", after
        ),
        emitted.append,
        stop=lambda: len(polls) == 2,
        sleep=sleep,
    )
    tools = [record["tool"] for record in emitted]
    assert tools == ["before-0", "before-1", "before-2", "after-0", "after-1"]
    assert len({record["key"] for record in emitted}) == len(emitted)


def test_a_session_end_is_one_line_and_the_stream_keeps_following(
    bridge, repo, paired
):
    directory = bridge.project(repo)[1]
    manifest = roster.read(directory)
    hook(directory, "claude", "SessionEnd")
    emitted = []
    polls = []

    def sleep(seconds):
        """Restarts the lane after the first poll."""
        polls.append(seconds)
        hook(directory, "claude", "SessionStart")

    watch.follow(
        lambda after: watch.collect(
            bridge.home, directory, manifest, "claude", after
        ),
        emitted.append,
        stop=lambda: len(polls) == 1,
        sleep=sleep,
    )
    assert [record["description"] for record in emitted] == [
        "session ended",
        "session started",
    ]


def test_watching_mutates_nothing(bridge, repo, paired, capsys):
    directory = bridge.project(repo)[1]
    codex = registered(bridge, paired, "codex")
    registered(bridge, paired, "claude")
    store.call(
        bridge.home,
        codex,
        "send_message",
        {
            "to": ["claude"],
            "subject": "Hello",
            "body_md": "Hello.",
            "idempotency_key": "k1",
        },
    )
    bridge.issue(paired["lanes"]["claude"], "claim", "42")
    hook(directory, "claude", "PreToolUse", "git", DENY)

    def state():
        """Snapshots every durable file under the private state root."""
        return {
            str(path): path.read_bytes()
            for path in sorted(bridge.home.rglob("*"))
            if path.is_file() and not path.name.endswith(("-wal", "-shm"))
        }

    before = state()
    assert stream(bridge, repo, "claude", capsys)
    assert state() == before


def test_the_command_parses_and_dispatches(
    bridge, repo, paired, monkeypatch, capsys
):
    calls = []
    original = watch.run
    monkeypatch.setattr(
        cli.stream, "run", lambda *args, **kwargs: calls.append((args, kwargs))
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agent-parley",
            "--home",
            str(bridge.home),
            "watch",
            "claude",
            "--repo",
            str(repo),
            "--since",
            "1h",
            "--kind",
            "claim",
            "--kind",
            "denied",
            "--json",
        ],
    )
    assert cli.main() == 0
    (args, options), *_ = calls
    assert args[0] == bridge.home and args[3] == "claude"
    assert options == {
        "since": 3600.0,
        "kinds": ("claim", "denied"),
        "json_lines": True,
        "interval": watch.INTERVAL,
    }
    monkeypatch.setattr(
        sys,
        "argv",
        ["agent-parley", "--home", str(bridge.home), "watch", "nobody"],
    )
    monkeypatch.chdir(repo)
    monkeypatch.setattr(cli.stream, "run", original)
    assert cli.main() == 1
    assert "not a participant" in capsys.readouterr().err
