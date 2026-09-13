"""Runs a real local pseudo-terminal and its private wake transport."""

import json
import select
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from agent_parley import terminal
from agent_parley.state import write_json


@pytest.mark.parametrize(
    "activity,expected",
    [("idle", "accepted"), ("waiting for approval", "busy")],
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
            if expected == "busy":
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
