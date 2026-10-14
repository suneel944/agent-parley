"""Checks the service recipes in the operations guide."""

import plistlib
import re
from pathlib import Path

DOC = Path(__file__).resolve().parent.parent / "docs" / "operations.md"


def test_launchd_plist_runs_up_at_load_with_absolute_paths() -> None:
    """The documented launchd property list parses and starts `up`."""
    blocks = re.findall(r"```xml\n(.*?)```", DOC.read_text(), re.DOTALL)
    plists = [block for block in blocks if "<plist" in block]
    assert len(plists) == 1
    data = plistlib.loads(plists[0].encode())
    assert data["RunAtLoad"] is True
    arguments = data["ProgramArguments"]
    assert arguments[-1] == "up"
    assert all(arg.startswith("/") for arg in arguments[:-1])
