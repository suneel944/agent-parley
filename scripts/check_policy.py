"""Enforces documentation, dependency, and release metadata boundaries."""

import ast
import io
import json
import re
import subprocess
import sys
import tokenize
import tomllib
from pathlib import Path


def has_attribution(text: str) -> bool:
    """Detects assistant credits and generator signatures in contribution text.

    Args:
        text: File contents, commit message, or public contribution text.

    Returns:
        Whether the text contains a prohibited authorship credit.
    """
    actor = (
        r"(?:ai\b|claude\b|codex\b|chatgpt\b|copilot\b|openai\b|anthropic\b)"
    )
    patterns = (
        r"\b(?:generated|written|created|authored|assisted|powered)\s+"
        r"(?:with|by)\s+(?:(?:an?|the)\s+)?" + actor,
        r"^co-authored-by:\s*[^\n]*" + actor,
        r"\bthis\s+pr\s+was\s+generated\s+with\b",
        r"\U0001f916|:robo[t]:|\bbeep\s*\*?\s*boop\b",
    )
    return any(re.search(pattern, text, re.I | re.M) for pattern in patterns)


def contribution_errors(root: Path) -> list[str]:
    """Checks tracked text and commit messages for prohibited credits.

    Args:
        root: Repository root; source archives without Git metadata are skipped.

    Returns:
        Paths or commit IDs containing prohibited attribution.
    """
    if not (root / ".git").exists():
        return []
    files = (
        subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=root,
            check=True,
            capture_output=True,
            timeout=10,
        )
        .stdout.decode()
        .split("\0")
    )
    errors = []
    for filename in filter(None, files):
        path = root / filename
        if path.is_file() and has_attribution(
            path.read_bytes().decode("utf-8", errors="ignore")
        ):
            errors.append(f"{filename}: prohibited attribution")
    history = subprocess.run(
        ["git", "log", "--format=%H%x00%B%x00"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.split("\0")
    for index in range(0, len(history) - 1, 2):
        if has_attribution(history[index + 1]):
            errors.append(f"Commit {history[index].strip()}: prohibited credit")
    return errors


def main() -> None:
    """Rejects undocumented code, inline comments and runtime dependencies."""
    root = Path(__file__).resolve().parents[1]
    metadata = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    errors = contribution_errors(root)
    for message in sys.argv[1:]:
        if has_attribution(Path(message).read_text()):
            errors.append("Commit message contains prohibited attribution.")
    if metadata["dependencies"]:
        errors.append("Runtime dependencies must remain empty.")
    for directory in ("agent_bridge", "scripts"):
        for path in sorted((root / directory).glob("*.py")):
            text = path.read_text()
            tree = ast.parse(text)
            for node in ast.walk(tree):
                if isinstance(
                    node,
                    (
                        ast.Module,
                        ast.ClassDef,
                        ast.FunctionDef,
                        ast.AsyncFunctionDef,
                    ),
                ) and not ast.get_docstring(node):
                    errors.append(
                        f"{path.name}:{getattr(node, 'lineno', 1)}: "
                        "missing docstring"
                    )
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                for name in names:
                    allowed = sys.stdlib_module_names | {"agent_bridge"}
                    if directory == "scripts":
                        allowed |= {"scripts"}
                    if name.split(".")[0] not in allowed:
                        errors.append(f"{path.name}: non-stdlib import {name}")
            for token in tokenize.generate_tokens(io.StringIO(text).readline):
                if token.type == tokenize.COMMENT:
                    errors.append(
                        f"{path.name}:{token.start[0]}: "
                        "use a docstring, not an inline comment"
                    )
    for client in ("codex", "claude"):
        path = root / "plugins/agent-bridge" / f".{client}-plugin/plugin.json"
        if json.loads(path.read_text())["version"] != metadata["version"]:
            errors.append(f"{client} plugin version differs from package")
    marketplace = json.loads(
        (root / ".claude-plugin/marketplace.json").read_text()
    )
    if marketplace["plugins"][0]["version"] != metadata["version"]:
        errors.append("Claude marketplace version differs from package")
    if errors:
        raise SystemExit("\n".join(errors))
    print(
        "Policy: documented code, no inline comments, "
        "stdlib runtime, aligned versions"
    )


if __name__ == "__main__":
    main()
