"""Checks that the troubleshooting page quotes only text the source has."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "docs" / "troubleshooting.md"


def _normal(text: str) -> str:
    """Folds case and underscores so `ROOT GONE` matches `root gone`."""
    return text.casefold().replace("_", " ")


def test_every_quoted_message_exists_in_the_source() -> None:
    """Each code span in a table row is a string in `agent_parley/*.py`."""
    modules = (ROOT / "agent_parley").glob("*.py")
    source = _normal("\n".join(path.read_text() for path in modules))
    rows = [
        line for line in PAGE.read_text().splitlines() if line.startswith("|")
    ]
    spans = [span for row in rows for span in re.findall(r"`([^`]+)`", row)]
    assert spans
    missing = [span for span in spans if _normal(span) not in source]
    assert not missing
