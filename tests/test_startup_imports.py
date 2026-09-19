"""Guards the command-line startup path against costly eager imports."""

import json
import subprocess
import sys

DEFERRED = ("asyncio", "curses", "agent_parley.dashboard", "urllib.request")

COMMAND_ONLY = (
    "agent_parley.amp",
    "agent_parley.archive",
    "agent_parley.delivery",
    "agent_parley.gemini",
    "agent_parley.metrics",
    "agent_parley.notify",
    "agent_parley.plan",
    "agent_parley.problems",
    "agent_parley.supervision",
    "agent_parley.views",
    "agent_parley.watch",
    "dataclasses",
    "inspect",
)

SURFACE_ONLY = (
    "agent_parley.checkpoints",
    "agent_parley.cli",
    "agent_parley.store",
    "argparse",
    "sqlite3",
    "subprocess",
)

VERSION_ONLY = (
    "agent_parley.cli",
    "agent_parley.protocol",
    "argparse",
    "importlib.metadata",
    "tomllib",
)

PROBE = """
import importlib
import json
import sys
import types

importlib.import_module({module!r})

json.dump(
    sorted(
        name
        for name in {deferred!r}
        if type(sys.modules.get(name)) is types.ModuleType
    ),
    sys.stdout,
)
"""

VERSION_PROBE = """
import contextlib
import io
import json
import sys
import types

from agent_parley import entry

sys.argv = ["agent-parley", "--version"]
with contextlib.redirect_stdout(io.StringIO()):
    status = entry.main()
if status:
    raise SystemExit(status)
json.dump(
    sorted(
        name
        for name in {deferred!r}
        if type(sys.modules.get(name)) is types.ModuleType
    ),
    sys.stdout,
)
"""


def loaded(module: str, deferred: tuple[str, ...] = DEFERRED) -> list[str]:
    """Reports which deferred modules importing ``module`` executes.

    A module bound for later use is registered under its name before it runs,
    so presence in ``sys.modules`` is not evidence that its cost was paid.
    Only a module that has executed carries the plain module type, which is
    what this reads.

    Args:
        module: Importable module whose startup cost is under test.
        deferred: Module names that must stay out of the import.

    Returns:
        The deferred module names the import executed.
    """
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            PROBE.format(module=module, deferred=deferred),
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
    )
    return json.loads(probe.stdout)


def test_importing_the_command_line_defers_the_costly_modules():
    assert loaded("agent_parley.cli") == []
    assert loaded("agent_parley.cli", ("agent_parley.cli",)) == []


def test_the_command_surface_defers_what_one_command_alone_needs():
    assert loaded("agent_parley.cli", COMMAND_ONLY) == []


def test_the_entry_point_defers_the_whole_command_surface():
    assert loaded("agent_parley.entry", DEFERRED + COMMAND_ONLY) == []
    assert loaded("agent_parley.entry", SURFACE_ONLY) == []


def test_the_version_path_reads_only_the_recorded_marker():
    probe = subprocess.run(
        [sys.executable, "-c", VERSION_PROBE.format(deferred=VERSION_ONLY)],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert json.loads(probe.stdout) == []


def test_the_deferred_modules_still_load_where_they_are_used():
    for name in DEFERRED + COMMAND_ONLY + SURFACE_ONLY:
        __import__(name)
