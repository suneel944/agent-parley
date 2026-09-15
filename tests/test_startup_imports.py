"""Guards the command-line startup path against costly eager imports."""

import json
import subprocess
import sys

DEFERRED = ("asyncio", "curses", "agent_parley.dashboard", "urllib.request")

PROBE = """
import importlib
import json
import sys

importlib.import_module({module!r})

json.dump(sorted(set(sys.modules) & set({deferred!r})), sys.stdout)
"""


def loaded(module: str) -> list[str]:
    """Reports which deferred modules importing ``module`` pulls in.

    Args:
        module: Importable module whose startup cost is under test.

    Returns:
        The deferred module names present in ``sys.modules`` afterwards.
    """
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            PROBE.format(module=module, deferred=DEFERRED),
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
    )
    return json.loads(probe.stdout)


def test_importing_the_command_line_defers_the_costly_modules():
    assert loaded("agent_parley.cli") == []


def test_the_deferred_modules_still_load_where_they_are_used():
    for name in DEFERRED:
        __import__(name)
