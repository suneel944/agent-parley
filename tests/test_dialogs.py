"""Answers or escalates the native dialogs recorded from live clients."""

import datetime
import json
import os
import pty
import select
import signal
import subprocess
import sys
import tempfile
import time
import zoneinfo
from pathlib import Path

import pytest

from agent_parley import (
    checkpoints,
    dialogs,
    lanes,
    process,
    protocol,
    roster,
    terminal,
)
from agent_parley.state import BridgeError, lock, write_json

USAGE_LIMIT = (
    "\x1b[38;2;80;80;80m❯ Review pending coordination messages and handoff "
    "reminders.                     ⎿  You've hit your weekly limit · resets "
    "Sep 21, 6am (Asia/Dubai)    /upgrade or /usage-credits to finish what "
    "you're working on.✻Sautéed for 0s · done Saturday, 2:33 pm\x1b[38;2;153"
)

USAGE_LIMIT_RESUMED = (
    "\x1b[38;2;153;153;153mPushed to feat/824-martin-round-ab, read 1 file, "
    "ran 5 shell commands   ⎿  You've hit your weekly limit · resets Sep 21, "
    "6am (Asia/Dubai)    /usage-credits to finish what you're workng on."
    "✻Brewed for 1m 58· done 2:30 pm\x1b["
)

TOOL_PERMISSION = (
    ")unacknowledged: true  limit: 5  About the agent_parley — Fetch Inbox "
    "Tool:   │ Read incremental mail. Bodies opt-in; page long bodies. Do you "
    "want to proceed? ❯ 1. Yes   2. NoEsc to cancel · Tabto amend\x1b[?2026l"
)

HOOK_REVIEW = (
    "\x1b[0;3H\x1b[?25h\x1b[?2026l\x1b[?2026h\x1b[0 q╭\x1b[?25h   Hooks need "
    "review  7 hooks are new or changed.  Hooks can run outside the sandbox "
    "after you trust them. › 1. Review hooks  2. Trust all and continue  3. "
    "Continue without trusting (hooks won't run) Press enter to confirm or "
    "esc to go back            "
)

UNKNOWN_PROMPT = (
    "\x1b[2J\x1b[H  Do you trust the files in this folder?  "
    "1. Yes, proceed   2. No, exit  "
)

WORKING = "  ⎿  Read 4 files, ran 2 shell commands ✻ Brewing for 3s  "

CODEX_TRUST = (
    "\x1b[2J\x1b[HYou are in /home/operator/.local/state/agent-parley/"
    "projects/abc/lane\r\n\r\nNote: You're in a subdirectory of a Git "
    "project. Trusting will apply to the\r\nrepository root: "
    "/tmp/parley-probe-20260922\r\n\r\nDo you trust the contents of this "
    "directory? Working with untrusted contents\r\ncomes with higher risk "
    "of prompt injection.\r\n\r\n\x1b[1m› 1. Yes, continue\x1b[0m\r\n  2. "
    "No, quit\r\n\r\n  Press enter to continue\r\n"
)

CLAUDE_TRUST = (
    "\x1b[2J\x1b[H╭────╮\r\n│ Do you trust the files in this folder? │\r\n"
    "│ /home/operator/lane │\r\n│ Claude Code may read, write, or execute "
    "files contained in this directory. │\r\n│ ❯ 1. Yes, I trust this "
    "folder │\r\n│   2. No, exit │\r\n╰────╯\r\n  Enter to confirm · Esc "
    "to cancel\r\n"
)

QUESTION_PICKER = (
    "\x1b[2J\x1b[H☐ Layout\r\n\r\nHow should the rename stay under the "
    "module ceilings?\r\n\r\n❯ 1. Shrink the three modules instead\r\n     "
    "Cut 5 lines of unrelated code from those three files to stay under "
    "the\r\n     existing ceilings.\r\n  2. Re-export from replay/__init__.py"
    "\r\n     Keep a re-export so grouped imports survive and no file grows."
    "\r\n  3. Type something.\r\n  4. Chat about this\r\n\r\nEnter to select "
    "· ↑/↓ to navigate · Esc to cancel\r\n"
)

REPORTED_PICKER = (
    "Shrink the three modules instead\r\nCut 5 lines of unrelated code from "
    "those three files to stay under the\r\nexisting ceilings. Puts "
    "refactoring I did not plan into a rename PR, and\r\nthe cuts would be "
    "arbitrary.\r\nRe-export from replay/__init__.py\r\nKeep a re-export so "
    "grouped imports survive and no file grows. Reverses\r\nParley decisions "
    "616/618 and the 're-exports nothing' rule that fill/ and\r\nguarded/ "
    "already follow.\r\n4. Type something.\r\n5. Chat about this\r\nEnter to "
    "select · ↑/↓ to navigate · Esc to cancel\r\n"
)

STANDING = (
    "No operator is watching. Choose the option that keeps every guard, "
    "record the choice in your report, and continue."
)

QUOTED_LIMIT = (
    "  ⎿  Read tests/fixtures/limits.log\r\n```\r\n2026-09-22 provider said: "
    "usage limit reached; you've hit your weekly limit · resets Sep 21, 6am"
    "\r\n```\r\n"
)

HARNESS = (
    "import os, sys\nfrom pathlib import Path\n"
    "from agent_parley import dialogs\n"
    "from agent_parley.terminal import run\n"
    "dialogs.ESCALATE_AFTER = float(sys.argv[3])\n"
    "raise SystemExit(run([sys.executable, '-c', sys.argv[2], sys.argv[4]], "
    "Path(sys.argv[1]), dict(os.environ), 'lane', attached=False))"
)

CLIENT = (
    "import os, sys, tty\ntty.setraw(0)\n"
    "os.write(1, sys.argv[1].encode())\n"
    "print('DRAWN', flush=True)\n"
    "seen = b''\n"
    "while b'\\r' not in seen:\n"
    "    seen += os.read(0, 4096)\n"
    "print('PRESSED:' + repr(seen), flush=True)\n"
    "os.read(0, 4096)\n"
)

SILENT_CLIENT = (
    "import os, sys, tty\ntty.setraw(0)\n"
    "os.write(1, sys.argv[1].encode())\n"
    "print('DRAWN', flush=True)\n"
    "seen = b''\n"
    "while b'\\r' not in seen:\n"
    "    seen += os.read(0, 4096)\n"
    "open('pressed', 'w').write(repr(seen))\n"
    "os.read(0, 4096)\n"
)

HOLDING_CLIENT = (
    "import os, sys, tty\ntty.setraw(0)\n"
    "os.write(1, sys.argv[1].encode())\n"
    "print('DRAWN', flush=True)\n"
    "os.read(0, 4096)\n"
)


def dubai(month: int, day: int, hour: int, year: int = 2026) -> float:
    """Returns a Unix time in the zone the recorded screen named."""
    zone = zoneinfo.ZoneInfo("Asia/Dubai")
    return datetime.datetime(year, month, day, hour, tzinfo=zone).timestamp()


def project(directory: Path, answers: dict) -> None:
    """Writes a minimal manifest carrying one lane's dialog answers."""
    write_json(
        directory / "project.json",
        {
            "root": str(directory / "repo"),
            "base": "main",
            "participants": {
                "lane": {
                    "provider": "claude",
                    "display": "Lane",
                    "lane": str(directory / "lane"),
                    "branch": "parley/lane",
                    "credential": None,
                    "dialogs": answers,
                }
            },
        },
    )


def launch(directory: Path, screen: str, client: str, deadline: str):
    """Starts a launcher whose fake client draws one recorded screen."""
    lane = directory / "lane"
    lane.mkdir(exist_ok=True)
    write_json(
        directory / "lane-activity.json",
        {"activity": "starting; awaiting native hook", "updated": 1},
    )
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            HARNESS,
            str(lane),
            client,
            deadline,
            screen,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )


def read_line(child, timeout: float = 10) -> str:
    """Returns the next line the launcher forwarded from its client."""
    if not select.select([child.stdout], [], [], timeout)[0]:
        return ""
    return child.stdout.readline().decode(errors="replace")


def marked(master: int, marker: bytes, timeout: float = 30) -> None:
    """Waits for an attached client to report a marker of its own progress.

    Typing at a client that has not put its terminal in raw mode leaves the
    keystrokes in the line discipline's canonical buffer, where they stay
    until a newline completes the line. A test that types on a timer rather
    than on a marker therefore reads as a single late line instead of the
    keystrokes it meant to send.
    """
    deadline = time.monotonic() + timeout
    seen = b""
    while time.monotonic() < deadline:
        if not select.select([master], [], [], 0.5)[0]:
            continue
        seen += os.read(master, 4096)
        if marker in seen:
            return
    raise AssertionError(f"the client never reported {marker.decode()}")


def published(directory: Path, timeout: float = 10) -> dict:
    """Waits for the launcher to publish a dialog on the lane's state."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = json.loads((directory / "lane-activity.json").read_text())
        if isinstance(state.get("dialog"), dict):
            return state
        time.sleep(0.1)
    raise AssertionError("no dialog was published")


def test_the_recorded_usage_limit_screens_are_recognized():
    for capture in (USAGE_LIMIT, USAGE_LIMIT_RESUMED):
        screen = dialogs.flatten(capture.encode())
        found = dialogs.match(screen)
        assert found is not None
        assert found.name == "usage-limit"
        assert found.action == dialogs.EXHAUSTED


@pytest.mark.parametrize("capture", [USAGE_LIMIT, USAGE_LIMIT_RESUMED])
def test_the_usage_limit_screen_names_a_parseable_reset(capture):
    screen = dialogs.flatten(capture.encode())
    assert dialogs.reset_at(screen, dubai(9, 22, 12)) == dubai(9, 21, 6)


def test_a_usage_limit_screen_without_a_reset_reports_none():
    screen = dialogs.flatten(
        "  ⎿  You've hit your weekly limit  /upgrade  ".encode()
    )
    assert dialogs.match(screen) is not None
    assert dialogs.reset_at(screen, dubai(9, 22, 12)) is None


def test_the_recorded_permission_prompt_answers_by_option_text():
    screen = dialogs.flatten(TOOL_PERMISSION.encode())
    found = dialogs.match(screen)
    assert found is not None
    assert found.name == "tool-permission"
    assert dialogs.options(screen) == {"1": "Yes", "2": "No"}
    assert dialogs.keys(screen, "yes") == b"1\r"
    assert dialogs.keys(screen, "no") == b"2\r"
    assert dialogs.keys(screen, "trust all") == b""


def test_the_recorded_hook_review_answers_by_option_text():
    screen = dialogs.flatten(HOOK_REVIEW.encode())
    found = dialogs.match(screen)
    assert found is not None
    assert found.name == "hook-review"
    assert dialogs.keys(screen, "review hooks") == b"1\r"
    assert dialogs.keys(screen, "trust all and continue") == b"2\r"
    assert dialogs.keys(screen, "continue without trusting") == b"3\r"


def test_an_ordinary_working_screen_is_no_dialog():
    screen = dialogs.flatten(WORKING.encode())
    assert dialogs.match(screen) is None
    assert dialogs.prompted(screen) is False


def test_a_dialog_split_across_reads_is_still_recognized(tmp_path):
    watch = dialogs.Watch(tmp_path, "lane", {"hook-review": "review hooks"})
    half = len(HOOK_REVIEW) // 2
    assert watch.advance(HOOK_REVIEW[:half].encode(), 0.0) == b""
    assert watch.advance(HOOK_REVIEW[half:].encode(), 0.1) == b""
    assert watch.advance(b"", 0.2) == b"1\r"
    assert watch.holding is True


def test_the_answers_an_operator_recorded_are_read_per_lane():
    manifest = {
        "supervision": {"dialogs": {"hook-review": "review hooks"}},
        "participants": {
            "lane": {"dialogs": {"tool-permission": "no", "unknown": "yes"}},
            "other": {},
        },
    }
    assert dialogs.configured(manifest, "lane") == {
        "hook-review": "review hooks",
        "tool-permission": "no",
    }
    assert dialogs.configured(manifest, "other") == {
        "hook-review": "review hooks"
    }


def test_a_recorded_dialog_without_an_answer_escalates(tmp_path):
    watch = dialogs.Watch(tmp_path, "lane")
    assert watch.advance(HOOK_REVIEW.encode(), 0.0) == b""
    assert watch.advance(b"", 0.5) == b""
    state = json.loads((tmp_path / "lane-activity.json").read_text())
    assert state["activity"] == "dialog: native hook trust review"
    assert state["dialog"]["escalated"] is True
    assert state["dialog"]["name"] == "hook-review"
    assert "Hooks need review" in " ".join(state["dialog"]["screen"])


def test_an_unknown_prompt_escalates_only_after_the_deadline(tmp_path):
    watch = dialogs.Watch(tmp_path, "lane", deadline=1.0)
    assert watch.advance(UNKNOWN_PROMPT.encode(), 0.0) == b""
    assert watch.advance(b"", 0.5) == b""
    assert watch.holding is False
    assert not (tmp_path / "lane-activity.json").exists()

    assert watch.advance(b"", 1.5) == b""
    state = json.loads((tmp_path / "lane-activity.json").read_text())
    assert state["dialog"]["name"] == "unknown"
    assert state["dialog"]["escalated"] is True
    assert state["activity"] == "dialog: an unrecognized native prompt"
    assert watch.holding is True


def test_the_usage_limit_dialog_records_exhausted_capacity(tmp_path):
    write_json(tmp_path / "lane-activity.json", {"activity": "working"})
    watch = dialogs.Watch(tmp_path, "lane")
    assert watch.advance(USAGE_LIMIT.encode(), 0.0) == b""
    assert watch.advance(b"", 0.5) == b""
    capacity = json.loads((tmp_path / "lane-capacity.json").read_text())
    assert capacity["state"] == "exhausted"
    assert capacity["source"] == "native-dialog"
    assert capacity["reset_at"] == pytest.approx(dubai(9, 21, 6))
    state = json.loads((tmp_path / "lane-activity.json").read_text())
    assert state["activity"] == "dialog: provider usage limit"
    assert state["dialog"]["previous"] == "working"
    assert "updated" not in state


def test_a_cleared_screen_restores_the_previous_activity(tmp_path):
    write_json(tmp_path / "lane-activity.json", {"activity": "working"})
    watch = dialogs.Watch(tmp_path, "lane")
    watch.advance(HOOK_REVIEW.encode(), 0.0)
    watch.advance(b"", 0.5)
    assert watch.holding is True

    watch.advance((WORKING * 200).encode(), 1.0)
    assert watch.holding is False
    state = json.loads((tmp_path / "lane-activity.json").read_text())
    assert state["activity"] == "working"
    assert "dialog" not in state


def test_an_answer_that_never_dismisses_the_dialog_escalates(tmp_path):
    watch = dialogs.Watch(tmp_path, "lane", {"hook-review": "review hooks"})
    for round_number in range(dialogs.REPEAT_LIMIT):
        watch.advance(HOOK_REVIEW.encode(), round_number * 10.0)
        assert watch.advance(b"", round_number * 10.0 + 1) == b"1\r"
        watch.advance((WORKING * 200).encode(), round_number * 10.0 + 2)
    watch.advance(HOOK_REVIEW.encode(), 100.0)
    assert watch.advance(b"", 101.0) == b""
    state = json.loads((tmp_path / "lane-activity.json").read_text())
    assert state["dialog"]["escalated"] is True


def test_the_launcher_answers_a_configured_dialog_and_refuses_wakes():
    with tempfile.TemporaryDirectory(prefix="dialog-") as temporary:
        directory = Path(temporary)
        project(directory, {"hook-review": "continue without trusting"})
        child = launch(directory, HOOK_REVIEW, SILENT_CLIENT, "30")
        try:
            assert "DRAWN" in read_line(child)
            state = published(directory)
            assert state["activity"] == "dialog: native hook trust review"
            assert state["dialog"]["previous"] == (
                "starting; awaiting native hook"
            )
            assert state["dialog"]["keys"] == "3"
            assert state["dialog"]["answer"] == "continue without trusting"
            assert (
                terminal.request(directory, "lane")
                == "manual attention required"
            )
            pressed = directory / "lane" / "pressed"
            deadline = time.monotonic() + 10
            while not pressed.exists() and time.monotonic() < deadline:
                time.sleep(0.1)
            assert pressed.read_text() == repr(b"3\r")
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=10)


def test_the_launcher_releases_an_answered_dialog_on_the_next_output():
    with tempfile.TemporaryDirectory(prefix="dialog-") as temporary:
        directory = Path(temporary)
        project(directory, {"hook-review": "continue without trusting"})
        child = launch(directory, HOOK_REVIEW, CLIENT, "30")
        try:
            assert "DRAWN" in read_line(child)
            assert "PRESSED:" + repr(b"3\r") in read_line(child)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                state = json.loads(
                    (directory / "lane-activity.json").read_text()
                )
                if "dialog" not in state:
                    break
                time.sleep(0.1)
            assert "dialog" not in state
            assert state["activity"] == "starting; awaiting native hook"
            assert (
                terminal.request(directory, "lane")
                != "manual attention required"
            )
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=10)


def test_the_launcher_parks_a_lane_on_the_usage_limit_dialog():
    with tempfile.TemporaryDirectory(prefix="dialog-") as temporary:
        directory = Path(temporary)
        project(directory, {})
        child = launch(directory, USAGE_LIMIT, HOLDING_CLIENT, "30")
        try:
            state = published(directory)
            assert state["activity"] == "dialog: provider usage limit"
            assert state["dialog"]["name"] == "usage-limit"
            assert state["dialog"]["screen"]
            capacity = json.loads(
                (directory / "lane-capacity.json").read_text()
            )
            assert capacity["state"] == "exhausted"
            assert capacity["reset_at"] == pytest.approx(dubai(9, 21, 6))
            assert (
                terminal.request(directory, "lane")
                == "manual attention required"
            )
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=10)


def test_the_launcher_escalates_an_unknown_prompt_within_the_deadline():
    with tempfile.TemporaryDirectory(prefix="dialog-") as temporary:
        directory = Path(temporary)
        project(directory, {})
        child = launch(directory, UNKNOWN_PROMPT, HOLDING_CLIENT, "0.5")
        try:
            state = published(directory)
            assert state["dialog"]["name"] == "unknown"
            assert state["dialog"]["escalated"] is True
            assert state["activity"].startswith("dialog: ")
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=10)


def test_an_attached_answer_waits_for_the_operator_to_finish_a_line():
    with tempfile.TemporaryDirectory(prefix="dialog-") as temporary:
        directory = Path(temporary)
        lane = directory / "lane"
        lane.mkdir()
        project(directory, {"hook-review": "review hooks"})
        write_json(
            directory / "lane-activity.json", {"activity": "idle", "updated": 1}
        )
        harness = (
            "import os, sys\nfrom pathlib import Path\n"
            "from agent_parley.terminal import run\n"
            "raise SystemExit(run([sys.executable, '-c', sys.argv[2], "
            "sys.argv[3]], "
            "Path(sys.argv[1]), dict(os.environ), 'lane', attached=True))"
        )
        client = (
            "import os, sys, tty\ntty.setraw(0)\n"
            "print('READY', flush=True)\n"
            "seen = b''\n"
            "while b'go' not in seen:\n"
            "    seen += os.read(0, 4096)\n"
            "os.write(1, sys.argv[1].encode())\n"
            "print('DRAWN', flush=True)\n"
            "rest = os.read(0, 4096)\n"
            "open('pressed', 'w').write(repr(rest))\n"
            "os.read(0, 4096)\n"
        )
        pid, master = pty.fork()
        if pid == 0:
            os.execvp(
                sys.executable,
                [sys.executable, "-c", harness, str(lane), client, HOOK_REVIEW],
            )
        try:
            marked(master, b"READY")
            os.write(master, b"typed")
            time.sleep(0.2)
            os.write(master, b"go")
            marked(master, b"DRAWN")
            time.sleep(1.0)
            state = json.loads((directory / "lane-activity.json").read_text())
            assert "dialog" not in state

            os.write(master, b"\r")
            state = published(directory)
            assert state["dialog"]["name"] == "hook-review"
            assert state["dialog"]["keys"] == "1"
        finally:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)


BRIDGE_TOOL = f"{protocol.TOOL_PREFIX}__fetch_inbox"


def test_a_permission_request_records_the_tool_and_when_it_began(
    bridge, paired
):
    """Shows the hook naming the waiting tool and the instant it began."""
    directory = Path(paired["lanes"]["claude"]).parent
    write_json(directory / "claude-identity.json", {"name": "claude"})
    payload = {
        "hook_event_name": "PermissionRequest",
        "session_id": "test",
        "cwd": paired["lanes"]["claude"],
        "tool_name": BRIDGE_TOOL,
    }
    checkpoints.checkpoint(bridge.home, directory, "claude", payload)
    state = json.loads((directory / "claude-activity.json").read_text())
    assert state["activity"] == f"{dialogs.APPROVAL}: {BRIDGE_TOOL}"
    record = state["dialog"]
    assert record["name"] == dialogs.PERMISSION
    assert record["tool"] == BRIDGE_TOOL
    assert record["bridge"] is True
    assert record["since"] <= record["at"]

    began = record["since"] - 120
    asked = record["at"]
    state["dialog"]["since"] = began
    write_json(directory / "claude-activity.json", state)
    checkpoints.checkpoint(bridge.home, directory, "claude", payload)
    again = json.loads((directory / "claude-activity.json").read_text())
    assert again["dialog"]["since"] == began
    assert again["dialog"]["at"] >= asked


def test_a_repeated_request_keeps_the_instant_the_wait_began():
    """Separates how long the prompt stood from when the client asked."""
    first = dialogs.requested(BRIDGE_TOOL, None, 100.0)
    again = dialogs.requested(BRIDGE_TOOL, first, 160.0)
    assert (again["since"], again["at"]) == (100.0, 160.0)
    other = dialogs.requested("Bash", first, 160.0)
    assert other["since"] == 160.0
    assert other["bridge"] is False
    assert first["bridge"] is True


def test_status_names_the_prompt_and_how_long_it_has_waited(tmp_path):
    """Reports the waiting tool and the age of the unanswered prompt."""
    now = time.time()
    write_json(
        tmp_path / "lane-activity.json",
        {
            "activity": f"{dialogs.APPROVAL}: {BRIDGE_TOOL}",
            "updated": now,
            "session_pid": os.getpid(),
            "session_ticks": process.start_ticks(os.getpid()),
            "dialog": dialogs.requested(BRIDGE_TOOL, None, now - 90),
        },
    )
    line = checkpoints.participant_liveness(tmp_path, "lane")
    assert line.startswith(f"{dialogs.APPROVAL}: {BRIDGE_TOOL}")
    assert "; waiting 90s" in line


@pytest.mark.parametrize(
    "opt_in,allowed",
    [
        (None, None),
        (False, None),
        (True, [protocol.TOOL_PREFIX, protocol.cli_rule()]),
    ],
)
def test_the_launch_approves_this_bridge_and_nothing_else(
    bridge, repo, monkeypatch, tmp_path, opt_in, allowed
):
    """Records what the opt-in adds to the client's own settings."""
    roster.define_provider(
        bridge.home, "stub-claude", "claude", "stub-claude", "", [], []
    )
    binary = tmp_path / "bin"
    binary.mkdir(exist_ok=True)
    script = binary / "stub-claude"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "with open(os.environ['CAPTURE'], 'w') as f:\n"
        " json.dump({'argv': sys.argv[1:]}, f)\n"
    )
    script.chmod(0o755)
    capture = tmp_path / "stub-claude.json"
    monkeypatch.setenv("CAPTURE", str(capture))
    monkeypatch.setenv("PATH", str(binary) + os.pathsep + os.environ["PATH"])
    data = bridge.add_participant(repo, "lane", "stub-claude")
    directory = Path(data["participants"]["lane"]["lane"]).parent
    if opt_in is not None:
        manifest = json.loads((directory / "project.json").read_text())
        manifest["supervision"] = {dialogs.PRE_APPROVE: opt_in}
        write_json(directory / "project.json", manifest)
    monkeypatch.setattr(bridge, "up", lambda: None)

    async def fake_identity(*args):
        return {"registration_token": "test-scoped-credential"}

    monkeypatch.setattr(bridge, "identity", fake_identity)
    assert bridge.launch("lane", repo, "Work on issue 359") == 0
    argv = json.loads(capture.read_text())["argv"]
    settings = json.loads(argv[argv.index("--settings") + 1])
    if allowed is None:
        assert set(settings) == {"hooks"}
    else:
        assert set(settings) == {"hooks", "permissions"}
        assert settings["permissions"] == {"allow": allowed}
    assert "bypass" not in " ".join(argv).lower()
    assert "--dangerously-skip-permissions" not in argv


@pytest.mark.parametrize(
    "capture,answer,pressed",
    [
        (CODEX_TRUST, "yes, continue", b"1\r"),
        (CLAUDE_TRUST, "no, exit", b"2\r"),
    ],
)
def test_the_directory_trust_screens_are_named(capture, answer, pressed):
    screen = dialogs.flatten(capture.encode())
    found = dialogs.locate(screen)
    assert found is not None
    assert found.dialog.name == dialogs.TRUST
    assert dialogs.keys(found.text, answer) == pressed


def test_a_trust_screen_escalates_by_name_until_an_answer_is_recorded(
    tmp_path,
):
    write_json(tmp_path / "lane-activity.json", {"activity": "starting"})
    watch = dialogs.Watch(tmp_path, "lane")
    watch.advance(CODEX_TRUST.encode(), 0.0)
    assert watch.advance(b"", 0.5) == b""
    state = json.loads((tmp_path / "lane-activity.json").read_text())
    assert state["activity"] == "dialog: native directory trust prompt"
    assert state["dialog"]["name"] == dialogs.TRUST
    assert state["dialog"]["escalated"] is True
    manifest = {"supervision": {"dialogs": {dialogs.TRUST: "yes, continue"}}}
    answers = dialogs.configured(manifest, "lane")
    assert answers == {dialogs.TRUST: "yes, continue"}


def test_the_question_picker_is_named_by_its_question(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(
        dialogs.Watch,
        "_notify",
        lambda self, label, detail: sent.append((label, detail)),
    )
    screen = dialogs.flatten(QUESTION_PICKER.encode())
    found = dialogs.locate(screen)
    assert found is not None
    assert found.dialog.name == dialogs.QUESTION
    watch = dialogs.Watch(tmp_path, "lane")
    watch.advance(QUESTION_PICKER.encode(), 0.0)
    assert watch.advance(b"", 0.5) == b""
    state = json.loads((tmp_path / "lane-activity.json").read_text())
    label = (
        "asks the operator: Layout How should the rename stay under the "
        "module ceilings?"
    )
    assert state["activity"] == f"dialog: {label}"
    record = state["dialog"]
    assert record["name"] == dialogs.QUESTION
    assert record["question"] == label.removeprefix("asks the operator: ")
    assert record["options"][0].startswith(
        "1. Shrink the three modules instead"
    )
    assert record["options"][2:] == ["3. Type something.", "4. Chat about this"]
    [(notified, detail)] = sent
    assert notified == label
    assert "1. Shrink the three modules instead" in detail
    assert "4. Chat about this" in detail


def test_the_reported_picker_capture_is_a_question():
    screen = dialogs.flatten(REPORTED_PICKER.encode())
    assert dialogs.match(screen) is dialogs.BY_NAME[dialogs.QUESTION]


def test_numbered_lines_without_the_picker_footer_are_no_dialog(tmp_path):
    bare = QUESTION_PICKER.rsplit("Enter to select", 1)[0]
    screen = dialogs.flatten(bare.encode())
    assert dialogs.match(screen) is None
    assert dialogs.prompted(screen) is False
    watch = dialogs.Watch(tmp_path, "lane", deadline=0.0)
    watch.advance(bare.encode(), 0.0)
    watch.advance(b"", 5.0)
    assert watch.holding is False


def test_a_standing_reply_answers_the_question_as_free_text(tmp_path):
    manifest = {
        "supervision": {dialogs.STANDING_REPLY: "project reply"},
        "participants": {"lane": {dialogs.STANDING_REPLY: STANDING}},
    }
    assert dialogs.standing_reply(manifest, "lane") == STANDING
    assert dialogs.standing_reply(manifest, "other") == "project reply"
    assert dialogs.standing_reply({}, "lane") == ""
    watch = dialogs.Watch(tmp_path, "lane", reply=STANDING)
    watch.advance(QUESTION_PICKER.encode(), 0.0)
    assert watch.advance(b"", 0.5) == f"3{STANDING}\r".encode()
    state = json.loads((tmp_path / "lane-activity.json").read_text())
    assert state["dialog"]["answer"] == STANDING
    assert "escalated" not in state["dialog"]


@pytest.mark.parametrize(
    "reply", ["", "   ", "line one\nline two", "\x1b[A", "x" * 501, 7]
)
def test_a_standing_reply_must_be_one_printable_line(reply):
    with pytest.raises(BridgeError):
        roster.standing_reply(reply)


def test_a_quoted_usage_limit_in_output_is_no_dialog(tmp_path):
    screen = dialogs.flatten(QUOTED_LIMIT.encode())
    assert dialogs.match(screen) is None
    watch = dialogs.Watch(tmp_path, "lane")
    watch.advance(QUOTED_LIMIT.encode(), 0.0)
    watch.advance(b"", 0.5)
    assert not (tmp_path / "lane-capacity.json").exists()
    assert watch.holding is False


def test_dialog_words_in_scrollback_are_no_dialog():
    screen = dialogs.flatten((TOOL_PERMISSION + WORKING * 40).encode())
    assert dialogs.match(screen) is None
    assert dialogs.prompted(screen) is False


def test_an_answered_permission_dialog_clears_holding_on_the_next_read(
    tmp_path,
):
    write_json(tmp_path / "lane-activity.json", {"activity": "working"})
    watch = dialogs.Watch(tmp_path, "lane", {"tool-permission": "yes"})
    watch.advance(TOOL_PERMISSION.encode(), 0.0)
    assert watch.advance(b"", 0.5) == b"1\r"
    assert watch.holding is True

    watch.advance("  ⎿  Fetched 2 messages ".encode(), 1.0)
    assert watch.holding is False
    state = json.loads((tmp_path / "lane-activity.json").read_text())
    assert state["activity"] == "working"
    assert "dialog" not in state

    watch.advance(TOOL_PERMISSION.encode(), 2.0)
    assert watch.advance(b"", 2.5) == b"1\r"
    assert watch.holding is True


def test_an_operator_answer_releases_the_dialog_on_the_next_output(tmp_path):
    write_json(tmp_path / "lane-activity.json", {"activity": "working"})
    watch = dialogs.Watch(tmp_path, "lane")
    watch.advance(HOOK_REVIEW.encode(), 0.0)
    watch.advance(b"", 0.5)
    assert watch.holding is True
    watch.answered()
    watch.advance(b"", 1.0)
    assert watch.holding is True
    watch.advance(b" Continuing ", 1.5)
    assert watch.holding is False
    state = json.loads((tmp_path / "lane-activity.json").read_text())
    assert state["activity"] == "working"


def test_a_screen_block_and_its_answer_are_lane_evidence(tmp_path):
    write_json(tmp_path / "lane-activity.json", {"activity": "working"})
    watch = dialogs.Watch(tmp_path, "lane")
    watch.advance(USAGE_LIMIT.encode(), 0.0)
    watch.advance(b"", 0.5)
    watch.answered()
    watch.advance(b" Continuing ", 1.0)
    moves = [
        (item["source"], item["state"], item["cause"])
        for item in lanes.pending(tmp_path)
    ]
    assert moves == [
        ("dialog", lanes.BLOCKED, lanes.CAPACITY),
        ("dialog", lanes.WORKING, ""),
    ]


@pytest.mark.parametrize(
    ("record", "cause"),
    [
        ({"name": "usage-limit", "action": dialogs.EXHAUSTED}, "capacity"),
        ({"name": dialogs.PERMISSION, "action": "answer"}, "approval"),
        ({"name": "question", "action": dialogs.ASK}, "prompt"),
        ({"name": "unknown", "action": "escalate"}, "prompt"),
        ({"name": "hook-review", "action": "answer"}, "dialog"),
    ],
)
def test_a_dialog_names_its_blocked_cause(record, cause):
    assert dialogs.lane_cause(record) == cause


def test_a_release_that_meets_a_held_lock_is_retried(tmp_path):
    write_json(tmp_path / "lane-activity.json", {"activity": "working"})
    watch = dialogs.Watch(tmp_path, "lane")
    watch.advance(HOOK_REVIEW.encode(), 0.0)
    watch.advance(b"", 0.5)
    assert watch.holding is True
    with lock(tmp_path / "lane-checkpoint.lock"):
        watch.advance((WORKING * 200).encode(), 1.0)
        assert watch.holding is True
    watch.advance(b"", 2.0)
    assert watch.holding is False
    state = json.loads((tmp_path / "lane-activity.json").read_text())
    assert state["activity"] == "working"
    assert "dialog" not in state


def _shell_prompt(command: str) -> str:
    """Draws the shell permission prompt recorded on lane claude-2."""
    return (
        "\x1b[2J ╭──────────╮ │ Bash command │   "
        f"{command}   List Agent Parley issue ownership  "
        "This command requires approval  Do you want to proceed? ❯ 1. Yes"
        "  2. Yes, and don't ask again for: "
        f"{command}  3. No  Esc to cancel · Tab to amend"
    )


def test_the_prompt_and_the_launch_rule_spell_one_command():
    """Keeps the allow rule on the exact string the prompt orders."""
    assert protocol.cli_rule() == f"Bash({protocol.cli_command()} *)"
    assert protocol.cli_command().endswith(" -m agent_parley.cli")


def test_an_opted_in_lane_answers_a_prompt_for_the_bridge_cli(tmp_path):
    """Unparks a lane on the command the protocol prompt ordered."""
    write_json(tmp_path / "lane-activity.json", {"activity": "working"})
    screen = _shell_prompt(protocol.cli_command() + " issue list")
    watch = dialogs.Watch(tmp_path, "lane", bridge=True)
    watch.advance(screen.encode(), 0.0)
    assert watch.advance(b"", 0.5) == b"1\r"
    state = json.loads((tmp_path / "lane-activity.json").read_text())
    assert state["dialog"]["answer"] == dialogs.BRIDGE_ANSWER


@pytest.mark.parametrize(
    "command",
    [
        "gh pr view 12",
        "python3 -m agent_parley.cli issue list",
        protocol.cli_command() + " issue list && rm -rf build",
        protocol.cli_command() + " report > /tmp/out",
        "cd x; " + protocol.cli_command() + " issue list",
    ],
)
def test_any_other_shell_prompt_escalates(tmp_path, command):
    """Answers nothing beyond this bridge's own CLI."""
    write_json(tmp_path / "lane-activity.json", {"activity": "working"})
    watch = dialogs.Watch(tmp_path, "lane", bridge=True)
    watch.advance(_shell_prompt(command).encode(), 0.0)
    assert watch.advance(b"", 0.5) == b""
    state = json.loads((tmp_path / "lane-activity.json").read_text())
    assert state["dialog"]["escalated"] is True


def test_the_opt_in_is_read_when_the_prompt_is_drawn(bridge, repo):
    """Covers a lane launched before the operator recorded the opt-in."""
    data = bridge.add_participant(repo, "lane", "claude")
    directory = Path(data["participants"]["lane"]["lane"]).parent
    write_json(directory / "lane-activity.json", {"activity": "working"})
    screen = _shell_prompt(protocol.cli_command() + " issue list").encode()
    before = dialogs.watcher(directory, "lane")
    before.advance(screen, 0.0)
    assert before.advance(b"", 0.5) == b""
    manifest = json.loads((directory / "project.json").read_text())
    manifest["supervision"] = {dialogs.PRE_APPROVE: True}
    write_json(directory / "project.json", manifest)
    assert before.advance(b"", 1.0) == b"1\r"
