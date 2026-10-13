"""Keeps the documented state layout in step with the state file names."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SUFFIXES = (".json", ".jsonl", ".lock", ".log", ".log.1", ".sqlite3")


def state_names(source: str) -> set[str]:
    """Returns the module-level string constants that name state files."""
    names = set()
    for node in ast.parse(source).body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        value = node.value
        items = value.elts if isinstance(value, ast.Tuple) else [value]
        for item in items:
            if (
                isinstance(item, ast.Constant)
                and isinstance(item.value, str)
                and item.value.endswith(SUFFIXES)
                and item.value not in SUFFIXES
            ):
                names.add(item.value)
    return names


def test_the_collector_finds_constants_and_tuples():
    source = 'A = "spares.json"\nB = ("-wake.log", "x")\nC = ".jsonl"\n'
    assert state_names(source) == {"spares.json", "-wake.log"}


def test_every_state_file_constant_is_in_the_architecture_layout():
    text = (ROOT / "docs/architecture.md").read_text()
    missing = {
        f"{path.stem}: {name}"
        for path in sorted((ROOT / "agent_parley").glob("*.py"))
        for name in state_names(path.read_text())
        if name not in text
    }
    assert not missing
