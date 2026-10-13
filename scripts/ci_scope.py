"""Decides which legs of the Check workflow a pull request needs.

Every change runs the full test suite on one Ubuntu leg. The remaining Python
versions and macOS run only when a change touches what can behave differently
across platforms or interpreters, and the WSL leg only when package, launch or
process code changes. A path this module does not recognise, a push to
``main``, a manual run and any failure to read the diff all select every leg,
so uncertainty never skips a check.

One exception narrows the WSL leg: a file whose only edits are version lines,
as in the release automation's version bump commit, counts as tooling rather
than package code. The commit before the bump already ran the WSL leg on the
same code, and the WSL leg is the slowest one, so a bump pushed straight after
a crossing merge would otherwise hold ``main`` for another full suite. Every
Ubuntu and macOS leg still runs for it.

The ruleset on ``main`` requires the macOS and WSL check names, so the
workflow skips those jobs with a job-level condition rather than a trigger
filter: a skipped job still reports its check, while a workflow that never
starts leaves the check pending.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

PYTHON_VERSIONS = ("3.12", "3.13", "3.14")
WSL = "wsl"
FULL = "full"
LIGHT = "light"
VERSION_LINE = re.compile(r'(__version__|version) = "[^"]*"')
WSL_PATHS = (
    "agent_parley/",
    "tests/test_wsl.py",
    "pyproject.toml",
    "uv.lock",
    ".github/workflows/check.yml",
)
FULL_PATHS = (
    "plugins/",
    "scripts/",
    "tests/",
    "Makefile",
    ".github/actions/",
    ".claude-plugin/",
    ".agents/",
    ".release-manifest.json",
    "server.json",
    "glama.json",
    ".gitignore",
    "LICENSE",
)
LIGHT_PATHS = (
    "docs/",
    ".github/DISCUSSION_TEMPLATE/",
    ".github/ISSUE_TEMPLATE/",
    ".github/PULL_REQUEST_TEMPLATE.md",
    ".github/CODEOWNERS",
    ".github/dependabot.yml",
    ".github/release-history.json",
    ".github/workflows/issue-hygiene.yml",
    ".github/workflows/pages.yml",
    ".github/workflows/pr-hygiene.yml",
    ".github/workflows/release.yml",
    ".github/workflows/release-version.yml",
    "_config.yml",
    ".pre-commit-config.yaml",
)


def matches(path: str, prefixes: tuple[str, ...]) -> bool:
    """Tells whether a path is one of the entries or inside one of them.

    Args:
        path: Repository-relative path with forward slashes.
        prefixes: Exact file paths, or directory paths ending in a slash.

    Returns:
        True when the path equals a file entry or lies under a directory one.
    """
    return any(
        path.startswith(prefix) if prefix.endswith("/") else path == prefix
        for prefix in prefixes
    )


def category(path: str) -> str | None:
    """Names the widest set of legs a changed path needs.

    Args:
        path: Repository-relative path with forward slashes.

    Returns:
        ``wsl`` for package, launch or process code, ``full`` for other code
        and tooling, ``light`` for documentation and workflows other than
        Check, or None for a path no list recognises.
    """
    if matches(path, WSL_PATHS):
        return WSL
    if matches(path, FULL_PATHS):
        return FULL
    if matches(path, LIGHT_PATHS) or ("/" not in path and path.endswith(".md")):
        return LIGHT
    return None


def plan(
    paths: list[str] | None,
    bumps: frozenset[str] = frozenset(),
    every: bool = False,
) -> dict[str, str]:
    """Chooses the Check legs for a set of changed paths.

    Args:
        paths: Changed paths, or None when the change could not be read.
        bumps: Changed paths whose only edits are version lines.
        every: True for a push or manual run, which runs every leg unless
            the change is a version bump and nothing else needs WSL.

    Returns:
        Workflow outputs: ``pythons``, a JSON list of Ubuntu Python versions,
        and ``macos`` and ``wsl``, each ``true`` or ``false``.
    """
    found = {FULL if path in bumps else category(path) for path in paths or ()}
    wide = not paths or None in found
    full = every or wide or bool(found & {WSL, FULL})
    wsl = wide or WSL in found or (every and not bumps)
    versions = PYTHON_VERSIONS if full else PYTHON_VERSIONS[:1]
    return {
        "pythons": json.dumps(list(versions)),
        "macos": json.dumps(full),
        "wsl": json.dumps(wsl),
    }


def changed_paths(base: str) -> list[str] | None:
    """Lists the paths a pull request changes against its base.

    Args:
        base: Commit the pull request targets.

    Returns:
        The changed paths, or None when Git cannot compare the commits.
    """
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", f"{base}...HEAD"],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.splitlines()


def version_only(base: str, path: str) -> bool:
    """Tells whether a change edits nothing in a file but version lines.

    Args:
        base: Commit the change is compared against.
        path: Repository-relative path the change touches.

    Returns:
        True when every added and removed line of the file is a version
        assignment, False when any other line changes or Git cannot compare.
    """
    try:
        result = subprocess.run(
            ["git", "diff", "-U0", "--no-color", f"{base}...HEAD", "--", path],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    edits = [
        line[1:].strip()
        for line in result.stdout.splitlines()
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    ]
    return bool(edits) and all(VERSION_LINE.fullmatch(e) for e in edits)


def main() -> None:
    """Writes the chosen legs to the step outputs, or prints them."""
    base = os.environ.get("BASE_SHA", "")
    pull = os.environ.get("GITHUB_EVENT_NAME") == "pull_request"
    paths = changed_paths(base) if base else None
    bumps = frozenset(
        path
        for path in paths or ()
        if category(path) == WSL and version_only(base, path)
    )
    outputs = plan(paths, bumps, every=not pull)
    lines = "".join(f"{name}={value}\n" for name, value in outputs.items())
    target = os.environ.get("GITHUB_OUTPUT")
    if target:
        with Path(target).open("a") as output:
            output.write(lines)
    sys.stdout.write(lines)


if __name__ == "__main__":
    main()
