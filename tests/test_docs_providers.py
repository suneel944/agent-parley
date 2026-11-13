"""Checks that the provider capability table agrees with the roster."""

from pathlib import Path

from agent_parley import roster

DOC = Path(__file__).resolve().parent.parent / "docs" / "providers.md"
ROW = "| Service-resume opt-ins applied |"


def test_resume_optin_row_matches_roster_approval() -> None:
    """Each adapter's opt-in cell says yes exactly when APPROVAL is set."""
    line = next(
        row for row in DOC.read_text().splitlines() if row.startswith(ROW)
    )
    cells = [cell.strip() for cell in line.strip("|").split("|")][1:]
    adapters = ["claude", "codex", "copilot", "gemini", "opencode", "amp"]
    assert len(cells) == len(adapters)
    for adapter, cell in zip(adapters, cells, strict=True):
        assert cell.startswith("yes") == (
            roster.APPROVAL[adapter] is not None
        ), adapter
