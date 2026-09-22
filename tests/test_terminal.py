"""Runs a real local pseudo-terminal and its private wake transport."""

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
