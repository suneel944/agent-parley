"""Runs a real local pseudo-terminal and its private wake transport."""

import contextlib
import json
import os
import pty
import select
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from agent_parley import terminal
from agent_parley.state import write_json


def test_control_socket_names_fit_valid_long_participants():
    directory = Path(
        "/home/operator/.local/state/agent-parley/projects/0123456789abcdef"
    )
    path = terminal.socket_path(directory, "a" * 39)
    assert path.parent == directory.parent.parent
    assert len(str(path).encode()) < 104
    assert path != terminal.socket_path(directory, "b" * 39)


@pytest.mark.parametrize(
    "entered,previous,expected",
    [
        (b"\x1b[12;1R", False, False),
        (b"\x1b[I", False, False),
        (b"\x1b[A", True, True),
        (b"\x1b", False, True),
        (b"\x1b[12;1Rtyped", False, True),
        (b"\x1bP>|tmux 3.4\x1b\\", False, False),
        (b"\x1bP>|tmux 3.4\x1b\\", True, True),
        (b"\x1b_capabilities\x1b\\", False, False),
        (b"\x1bX status\x07", False, False),
        (b"\x1b^message\x1b\\", False, False),
        (b"\x1bP>|tmux 3.4\x1b\\typed", False, True),
        (b"\x1bx", False, True),
        (b"abc", False, True),
        (b"abc\r", True, False),
        (b"\x03", True, False),
        (b"abc\x15", True, False),
        (b"abc\x15new", True, True),
    ],
)
def test_control_replies_never_hold_the_operator_line(
    entered, previous, expected
):
    assert terminal.pending(entered, previous) is expected


def test_control_sequences_split_across_reads_keep_later_operator_text():
    operator, control = terminal.operator_input(b"\x1b[12;")
    assert operator == b""
    assert control == b"\x1b[12;"
    operator, control = terminal.operator_input(b"1Rtyped", control)
    assert operator == b"typed"
    assert control == b""
    assert terminal.pending(operator, False)


def test_a_version_report_split_across_reads_holds_no_operator_line():
    operator, control = terminal.operator_input(b"\x1bP>|tmux")
    assert operator == b""
    assert control == b"\x1bP>|tmux"
    operator, control = terminal.operator_input(b" 3.4\x1b\\", control)
    assert operator == b""
    assert control == b""
    assert terminal.pending(b"\x1bP>|tmux 3.4\x1b\\", False) is False


def test_detached_terminal_replies_cover_native_startup_probes():
    replies, control = terminal.detached_terminal_replies(
        b"\x1b[6n\x1b]10;?\x1b\\\x1b]11;?\x07\x1b[?u\x1b[c"
    )

    assert replies == (
        b"\x1b[1;1R"
        b"\x1b]10;rgb:ffff/ffff/ffff\x1b\\"
        b"\x1b]11;rgb:0000/0000/0000\x07"
        b"\x1b[?1;2c"
    )
    assert control == b""


def test_detached_terminal_replies_reassemble_split_queries():
    replies, control = terminal.detached_terminal_replies(b"\x1b]10;?")
    assert replies == b""
    assert control == b"\x1b]10;?"

    replies, control = terminal.detached_terminal_replies(b"\x1b\\", control)
    assert replies == b"\x1b]10;rgb:ffff/ffff/ffff\x1b\\"
    assert control == b""


def test_detached_launcher_supplies_terminal_responses_and_window_size():
    with tempfile.TemporaryDirectory(prefix="wake-") as temporary:
        directory = Path(temporary)
        lane = directory / "lane"
        lane.mkdir()
        script = (
            "import os, signal, tty\n"
            "signal.alarm(5)\n"
            "tty.setraw(0)\n"
            "size = os.get_terminal_size(0)\n"
            "assert (size.lines, size.columns) == (24, 80), size\n"
            "os.write(1, b'\\x1b[6n\\x1b]10;?\\x1b\\\\'"
            " b'\\x1b]11;?\\x1b\\\\\\x1b[?u\\x1b[c')\n"
            "reply = b''\n"
            "while b'\\x1b[?1;2c' not in reply:\n"
            "    reply += os.read(0, 4096)\n"
            "assert b'\\x1b[1;1R' in reply, reply\n"
            "assert b'\\x1b]10;rgb:ffff/ffff/ffff\\x1b\\\\' in reply\n"
            "assert b'\\x1b]11;rgb:0000/0000/0000\\x1b\\\\' in reply\n"
            "assert b'\\x1b[?0u' not in reply, reply\n"
            "signal.alarm(0)\n"
            "print('READY', flush=True)\n"
        )
        harness = (
            "import os, sys\nfrom pathlib import Path\n"
            "from agent_parley.terminal import run\n"
            "raise SystemExit(run([sys.executable, '-c', sys.argv[2]], "
            "Path(sys.argv[1]), dict(os.environ), 'lane', attached=False))"
        )
        child = subprocess.Popen(
            [sys.executable, "-c", harness, str(lane), script],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            output, error = child.communicate(timeout=10)
            assert child.returncode == 0, error
            assert b"READY" in output
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=10)


def test_attached_launcher_admits_a_wake_after_a_cursor_report():
    with tempfile.TemporaryDirectory(prefix="wake-") as temporary:
        directory = Path(temporary)
        lane = directory / "lane"
        lane.mkdir()
        write_json(
            directory / "lane-activity.json",
            {"activity": "idle", "updated": 1},
        )
        script = (
            "import sys\nprint('READY', flush=True)\n"
            "for line in sys.stdin:\n"
            "    print('RECEIVED:' + line.rstrip(), flush=True)\n"
        )
        harness = (
            "import os, sys\nfrom pathlib import Path\n"
            "from agent_parley.terminal import run\n"
            "raise SystemExit(run([sys.executable, '-c', sys.argv[2]], "
            "Path(sys.argv[1]), dict(os.environ), 'lane', attached=True))"
        )
        pid, master = pty.fork()
        if pid == 0:
            os.execvp(
                sys.executable,
                [sys.executable, "-c", harness, str(lane), script],
            )
        try:
            assert b"READY" in _read_until(master, b"READY")
            os.write(master, b"\x1b[12;1R")
            time.sleep(0.5)
            assert terminal.request(directory, "lane") == "accepted"
            received = _read_until(master, b"RECEIVED:")
            assert terminal.PROMPT.encode() in received.split(b"RECEIVED:")[1]
            os.write(master, b"typed")
            time.sleep(0.5)
            write_json(
                directory / "lane-activity.json",
                {"activity": "idle", "updated": 2},
            )
            assert terminal.request(directory, "lane") == "busy:input"
        finally:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)


def _read_until(master: int, marker: bytes, timeout: float = 10) -> bytes:
    """Collects pseudo-terminal output until the marker or the timeout."""
    output = b""
    deadline = time.monotonic() + timeout
    while marker not in output and time.monotonic() < deadline:
        if select.select([master], [], [], 0.2)[0]:
            try:
                output += os.read(master, 65536)
            except OSError:
                break
    return output


@pytest.mark.parametrize(
    "activity,expected",
    [("idle", "accepted"), ("waiting for approval", "busy:approval")],
)
def test_wake_transport_respects_native_activity(activity, expected):
    with tempfile.TemporaryDirectory(prefix="wake-") as temporary:
        directory = Path(temporary)
        lane = directory / "lane"
        lane.mkdir()
        write_json(
            directory / "lane-activity.json",
            {"activity": activity, "updated": 1},
        )
        script = (
            "import sys\nprint('READY', flush=True)\n"
            "line = input()\nprint('RECEIVED:' + line, flush=True)\n"
        )
        harness = (
            "import os, sys\nfrom pathlib import Path\n"
            "from agent_parley.terminal import run\n"
            "raise SystemExit(run([sys.executable, '-c', sys.argv[2]], "
            "Path(sys.argv[1]), dict(os.environ), 'lane', attached=False))"
        )
        child = subprocess.Popen(
            [sys.executable, "-c", harness, str(lane), script],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            assert select.select([child.stdout], [], [], 10)[0]
            assert b"READY" in child.stdout.readline()
            assert terminal.request(directory, "lane") == expected
            if expected == "busy:approval":
                assert child.poll() is None
                state = json.loads(
                    (directory / "lane-activity.json").read_text()
                )
                assert state["activity"] == "waiting for approval"
                write_json(
                    directory / "lane-activity.json",
                    {"activity": "idle", "updated": 2},
                )
                assert terminal.request(directory, "lane") == "accepted"
            output, error = child.communicate(timeout=10)
            assert child.returncode == 0, error
            assert ("RECEIVED:" + terminal.PROMPT).encode() in output
            assert not (directory / "lane-wake.sock").exists()
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=10)


def test_an_admitted_prompt_submits_after_its_text():
    with tempfile.TemporaryDirectory(prefix="wake-") as temporary:
        directory = Path(temporary)
        lane = directory / "lane"
        lane.mkdir()
        write_json(
            directory / "lane-activity.json",
            {"activity": "idle", "updated": 1},
        )
        script = (
            "import os, sys, tty\ntty.setraw(0)\n"
            "print('READY', flush=True)\nseen = b''\n"
            "while b'\\r' not in seen:\n"
            "    chunk = os.read(0, 4096)\n"
            "    seen += chunk\n"
            "    print('CHUNK:' + repr(chunk), flush=True)\n"
        )
        harness = (
            "import os, sys\nfrom pathlib import Path\n"
            "from agent_parley.terminal import run\n"
            "raise SystemExit(run([sys.executable, '-c', sys.argv[2]], "
            "Path(sys.argv[1]), dict(os.environ), 'lane', attached=False))"
        )
        child = subprocess.Popen(
            [sys.executable, "-c", harness, str(lane), script],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            assert select.select([child.stdout], [], [], 10)[0]
            assert b"READY" in child.stdout.readline()
            assert terminal.request(directory, "lane") == "accepted"
            output, error = child.communicate(timeout=10)
            assert child.returncode == 0, error
            chunks = [
                line.split("CHUNK:", 1)[1]
                for line in output.decode().splitlines()
                if line.startswith("CHUNK:")
            ]
            assert chunks[-1] == repr(b"\r")
            assert terminal.PROMPT in "".join(chunks[:-1])
            assert "\\r" not in "".join(chunks[:-1])
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=10)


def test_latched_checkpoint_admits_one_retry_then_requires_attention():
    with tempfile.TemporaryDirectory(prefix="wake-") as temporary:
        directory = Path(temporary)
        lane = directory / "lane"
        lane.mkdir()
        write_json(
            directory / "lane-activity.json",
            {"activity": "idle", "updated": 1},
        )
        script = (
            "import sys\nprint('READY', flush=True)\n"
            "for line in sys.stdin:\n"
            "    print('RECEIVED:' + line.rstrip(), flush=True)\n"
        )
        harness = (
            "import os, sys\nfrom pathlib import Path\n"
            "from agent_parley.terminal import run\n"
            "raise SystemExit(run([sys.executable, '-c', sys.argv[2]], "
            "Path(sys.argv[1]), dict(os.environ), 'lane', attached=False, "
            "inactive_after=0.05))"
        )
        child = subprocess.Popen(
            [sys.executable, "-c", harness, str(lane), script],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            assert select.select([child.stdout], [], [], 10)[0]
            assert b"READY" in child.stdout.readline()
            assert terminal.request(directory, "lane") == "accepted"
            assert terminal.request(directory, "lane") == "busy:repeat"
            time.sleep(0.1)
            assert terminal.request(directory, "lane") == "accepted"
            assert terminal.request(directory, "lane") == "busy:repeat"
            time.sleep(0.1)
            assert (
                terminal.request(directory, "lane")
                == "manual attention required"
            )
            assert child.poll() is None
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=10)


def test_stale_selected_work_is_refused_without_terminal_injection():
    with tempfile.TemporaryDirectory(prefix="wake-") as temporary:
        directory = Path(temporary)
        lane = directory / "lane"
        lane.mkdir()
        write_json(
            directory / "lane-activity.json",
            {"activity": "idle", "updated": 1},
        )
        write_json(directory / "lane-wake-work.json", {})
        script = (
            "import sys\nprint('READY', flush=True)\n"
            "for line in sys.stdin:\n"
            "    print('RECEIVED:' + line.rstrip(), flush=True)\n"
        )
        harness = (
            "import os, sys\nfrom pathlib import Path\n"
            "from agent_parley.terminal import run\n"
            "raise SystemExit(run([sys.executable, '-c', sys.argv[2]], "
            "Path(sys.argv[1]), dict(os.environ), 'lane', attached=False))"
        )
        child = subprocess.Popen(
            [sys.executable, "-c", harness, str(lane), script],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            assert select.select([child.stdout], [], [], 10)[0]
            assert b"READY" in child.stdout.readline()
            assert terminal.request(directory, "lane") == "busy:stale"
            assert child.poll() is None
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=10)


DETACHED_HARNESS = (
    "import os, sys\nfrom pathlib import Path\n"
    "from agent_parley.terminal import run\n"
    "try:\n"
    "    code = run([sys.executable, '-c', sys.argv[2]], Path(sys.argv[1]), "
    "dict(os.environ), 'lane', attached=False)\n"
    "finally:\n"
    "    print('CLEANUP', flush=True)\n"
    "raise SystemExit(code)\n"
)


def _detached(lane: Path, script: str, stdout=subprocess.PIPE):
    """Starts a detached launcher around a stub client script."""
    return subprocess.Popen(
        [sys.executable, "-c", DETACHED_HARNESS, str(lane), script],
        stdin=subprocess.DEVNULL,
        stdout=stdout,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )


def _finish(child: subprocess.Popen) -> None:
    """Kills a launcher a failed assertion left running."""
    if child.poll() is None:
        child.kill()
    child.communicate(timeout=10)


def test_the_launcher_ends_when_its_client_exits_under_a_held_terminal():
    with tempfile.TemporaryDirectory(prefix="wake-") as temporary:
        lane = Path(temporary) / "lane"
        lane.mkdir()
        holder = Path(temporary) / "holder"
        script = (
            "import subprocess, sys\n"
            "held = subprocess.Popen([sys.executable, '-c', "
            "'import time; time.sleep(30)'], start_new_session=True)\n"
            f"open({str(holder)!r}, 'w').write(str(held.pid))\n"
            "print('BYE', flush=True)\n"
        )
        child = _detached(lane, script)
        try:
            output, error = child.communicate(timeout=10)
            assert child.returncode == 0, error
            assert b"BYE" in output
            assert output.count(b"CLEANUP") == 1
            assert not (Path(temporary) / "lane-wake.sock").exists()
        finally:
            _finish(child)
            if holder.exists():
                with contextlib.suppress(ProcessLookupError):
                    os.kill(int(holder.read_text()), signal.SIGKILL)


@pytest.mark.parametrize("number", [signal.SIGTERM, signal.SIGHUP])
def test_a_stop_signal_runs_cleanup_and_reaches_the_client(number):
    with tempfile.TemporaryDirectory(prefix="wake-") as temporary:
        directory = Path(temporary)
        lane = directory / "lane"
        lane.mkdir()
        marker = directory / "signalled"
        script = (
            "import signal, sys, time\n"
            "def handle(signum, frame):\n"
            f"    open({str(marker)!r}, 'w').write(str(signum))\n"
            "    sys.exit(0)\n"
            "signal.signal(signal.SIGTERM, handle)\n"
            "signal.signal(signal.SIGHUP, handle)\n"
            "print('READY', flush=True)\n"
            "time.sleep(30)\n"
        )
        child = _detached(lane, script)
        try:
            assert select.select([child.stdout], [], [], 10)[0]
            assert b"READY" in child.stdout.readline()
            assert (directory / "lane-wake.sock").exists()
            child.send_signal(number)
            output, error = child.communicate(timeout=10)
            assert b"CLEANUP" in output, error
            assert not (directory / "lane-wake.sock").exists()
            assert marker.exists()
        finally:
            _finish(child)


def test_an_unreadable_activity_record_gets_an_explicit_wake_reply():
    with tempfile.TemporaryDirectory(prefix="wake-") as temporary:
        directory = Path(temporary)
        lane = directory / "lane"
        lane.mkdir()
        script = "print('READY', flush=True)\ninput()\n"
        child = _detached(lane, script)
        try:
            assert select.select([child.stdout], [], [], 10)[0]
            assert b"READY" in child.stdout.readline()
            assert terminal.request(directory, "lane") == "unknown"
            (directory / "lane-activity.json").write_text("{")
            assert terminal.request(directory, "lane") == "unknown"
            assert child.poll() is None
        finally:
            _finish(child)


def test_a_failed_exec_never_runs_the_launcher_cleanup():
    with tempfile.TemporaryDirectory(prefix="wake-") as temporary:
        directory = Path(temporary)
        lane = directory / "lane"
        lane.mkdir()
        harness = (
            "import os, sys\nfrom pathlib import Path\n"
            "from agent_parley.terminal import run\n"
            "try:\n"
            "    code = run([str(Path(sys.argv[1]) / 'missing')], "
            "Path(sys.argv[1]), dict(os.environ), 'lane', attached=False)\n"
            "finally:\n"
            "    print('CLEANUP', os.getpid(), flush=True)\n"
            "raise SystemExit(code)\n"
        )
        child = subprocess.Popen(
            [sys.executable, "-c", harness, str(lane)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            output, error = child.communicate(timeout=10)
            assert child.returncode == terminal.EXEC_FAILED, error
            assert output.count(b"CLEANUP") == 1, output
            assert b"Traceback" not in output + error
        finally:
            _finish(child)


def test_a_resumed_lane_keeps_its_wake_log_under_the_cap():
    with tempfile.TemporaryDirectory(prefix="wake-") as temporary:
        directory = Path(temporary)
        lane = directory / "lane"
        lane.mkdir()
        log = directory / "lane-wake.log"
        script = (
            "import os\nline = b'x' * 1023 + b'\\n'\n"
            "for _ in range(10240):\n"
            "    os.write(1, line)\n"
            "print('DONE', flush=True)\n"
        )
        with log.open("ab") as output:
            child = _detached(lane, script, stdout=output)
        try:
            _, error = child.communicate(timeout=60)
            assert child.returncode == 0, error
            assert 0 < log.stat().st_size <= terminal.LOG_LIMIT
            assert b"CLEANUP" in log.read_bytes()
        finally:
            _finish(child)


@pytest.mark.skipif(
    not Path("/dev/full").exists(), reason="needs a device that fills"
)
def test_a_failing_log_write_keeps_the_session():
    with tempfile.TemporaryDirectory(prefix="wake-") as temporary:
        directory = Path(temporary)
        lane = directory / "lane"
        lane.mkdir()
        received = directory / "received"
        write_json(
            directory / "lane-activity.json",
            {"activity": "idle", "updated": 1},
        )
        script = (
            "import os\nos.write(1, b'output\\n' * 100)\n"
            "line = input()\n"
            f"open({str(received)!r}, 'w').write(line)\n"
        )
        with open("/dev/full", "wb") as full:
            child = _detached(lane, script, stdout=full)
        try:
            deadline = time.monotonic() + 10
            while not (directory / "lane-wake.sock").exists():
                assert time.monotonic() < deadline
                time.sleep(0.05)
            time.sleep(0.5)
            assert child.poll() is None
            assert terminal.request(directory, "lane") == "accepted"
            child.communicate(timeout=10)
            assert received.read_text() == terminal.PROMPT
        finally:
            _finish(child)


def test_lane_title_names_lane_state_and_claim_progress():
    ledger = {
        "issues": {
            "12": {"owner": "claude-a"},
            "9": {"owner": "claude-a"},
            "3": {"completed_by": "claude-a"},
            "4": {"owner": "codex-b"},
        }
    }
    working = {"activity": "PreToolUse"}
    idle = {"activity": "idle"}
    approval = {"activity": "waiting for approval: Bash"}
    assert (
        terminal.lane_title("claude-a", working, ledger, False)
        == "[claude-a] working #9 - 1/3 done"
    )
    assert (
        terminal.lane_title("claude-a", idle, ledger, False)
        == "[claude-a] idle with claim #9 - 1/3 done"
    )
    assert (
        terminal.lane_title("codex-c", idle, ledger, False)
        == "[codex-c] idle - 0 open"
    )
    assert terminal.lane_title("claude-a", idle, ledger, True).startswith(
        "[claude-a] blocked: dialog #9"
    )
    assert terminal.lane_title("claude-a", approval, None, False) == (
        "[claude-a] blocked: approval - 0 open"
    )
    assert terminal.lane_title("x", None, None, False) == "[x] unknown - 0 open"
    assert terminal.lane_title("claude-a", idle, ledger, False) != (
        terminal.lane_title("codex-b", idle, ledger, False)
    )


def test_retitle_keeps_the_client_title_as_a_suffix_across_reads():
    first, carry, title = terminal.retitle(b"a\x1b]0;Sha", b"", "[l] idle")
    assert (first, title) == (b"a", None)
    second, carry, title = terminal.retitle(
        b"red\x1b\\b\x1b]8;;u\x07", carry, "[l] idle"
    )
    assert carry == b""
    assert title == "Shared"
    assert second == b"\x1b]0;[l] idle | Shared\x07b\x1b]8;;u\x07"
    wide = terminal.compose_title("[l] idle", "y" * 500)
    assert wide.startswith("[l] idle | y")
    assert len(wide) == terminal.TITLE_LIMIT
    assert terminal.compose_title("[l] idle", "a\x07\x1bb") == "[l] idle | ab"


def test_titles_setting_must_be_a_boolean():
    from agent_parley import supervision
    from agent_parley.state import BridgeError

    assert supervision.settings({})["titles"] is True
    assert supervision.settings({"titles": False})["titles"] is False
    with pytest.raises(BridgeError):
        supervision.settings({"titles": "off"})


@pytest.mark.parametrize("titles", [True, False])
def test_attached_launcher_relays_the_lane_title(titles):
    with tempfile.TemporaryDirectory(prefix="wake-") as temporary:
        directory = Path(temporary)
        lane = directory / "lane"
        lane.mkdir()
        write_json(
            directory / "lane-activity.json",
            {"activity": "PreToolUse", "updated": 1},
        )
        write_json(
            directory / "issues.json", {"issues": {"7": {"owner": "lane"}}}
        )
        script = (
            "import sys\n"
            "sys.stdout.write('\\x1b]2;native\\x07READY\\n')\n"
            "sys.stdout.flush()\n"
            "for line in sys.stdin:\n"
            "    sys.stdout.write('\\x1b]0;again\\x07ECHO\\n')\n"
            "    sys.stdout.flush()\n"
        )
        harness = (
            "import os, sys\nfrom pathlib import Path\n"
            "from agent_parley.terminal import run\n"
            "raise SystemExit(run([sys.executable, '-c', sys.argv[2]], "
            "Path(sys.argv[1]), dict(os.environ), 'lane', attached=True, "
            f"titles={titles}))"
        )
        pid, master = pty.fork()
        if pid == 0:
            os.execvp(
                sys.executable,
                [sys.executable, "-c", harness, str(lane), script],
            )
        try:
            output = _read_until(master, b"READY")
            if not titles:
                assert b"\x1b]2;native\x07" in output
                assert b"[lane]" not in output
                return
            assert output.startswith(terminal.TITLE_SAVE)
            assert b"\x1b]0;[lane] working #7 - 0/1 done\x07" in output
            assert b"\x1b]0;[lane] working #7 - 0/1 done | native\x07" in output
            assert b"\x1b]2;native" not in output
            write_json(
                directory / "lane-activity.json",
                {"activity": "idle", "updated": 2},
            )
            changed = _read_until(master, b"idle with claim")
            assert (
                b"\x1b]0;[lane] idle with claim #7 - 0/1 done | native\x07"
                in changed
            )
            os.write(master, b"go\r")
            echoed = _read_until(master, b"ECHO")
            assert b"[lane] idle with claim #7 - 0/1 done | again" in echoed
        finally:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)


def test_the_status_line_command_prints_the_lane_summary():
    with tempfile.TemporaryDirectory(prefix="wake-") as temporary:
        directory = Path(temporary)
        nested = directory / "lane" / "src"
        nested.mkdir(parents=True)
        write_json(directory / "project.json", {"participants": {"lane": {}}})
        write_json(
            directory / "lane-activity.json",
            {"activity": "dialog: trust folder", "updated": 1},
        )
        write_json(
            directory / "issues.json", {"issues": {"7": {"owner": "lane"}}}
        )
        expected = "[lane] blocked: dialog #7 - 0/1 done"
        assert terminal.lane_summary(nested) == expected
        assert terminal.lane_summary(directory) == ""
        assert terminal.lane_summary(Path("/")) == ""
        printed = subprocess.run(
            [sys.executable, "-m", "agent_parley.cli", "title"],
            cwd=nested,
            input="{}",
            capture_output=True,
            text=True,
            timeout=20,
            check=True,
        )
        assert printed.stdout == expected + "\n"
        outside = subprocess.run(
            [sys.executable, "-m", "agent_parley.cli", "title"],
            cwd=directory,
            capture_output=True,
            text=True,
            timeout=20,
            check=True,
        )
        assert outside.stdout == ""
