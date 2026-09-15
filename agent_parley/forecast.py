"""Forecasts a reservation collision from the project's co-change history.

A reservation names the paths a lane intends to edit. Git history names the
paths that habitually change together with them, and a peer may hold one of
those already. Reading that before the lane starts turns a merge conflict a
week later into a one-line notice today. Everything here is advisory: a
forecast never denies a reservation or a claim, and a history Git cannot read
inside its timeout is no forecast rather than a failed call.
"""

import json
import subprocess
from collections import Counter
from collections.abc import Callable
from pathlib import Path

WINDOW = 500
THRESHOLD = 3
TIMEOUT = 10
CACHE = "cochanges.json"
MAX_FORECAST = 16


def _git(root: str, *args: str) -> str | None:
    """Runs one bounded Git read in the base checkout, or reports None."""
    try:
        result = subprocess.run(
            ["git", "-C", root, *args],
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return None if result.returncode else result.stdout


def history(root: str, directory: Path) -> list[list[str]]:
    """Reads the files each recent commit of the base checkout touched.

    The reading covers the last ``WINDOW`` commits reachable from the base
    checkout's head. It is cached as JSON in the project state directory,
    keyed by that head commit, so a lane's reservation costs one ``rev-parse``
    while the base stands still and one ``git log`` when it moves. The cache
    lives beside the manifest, never inside the repository.

    Args:
        root: Canonical project key, which is the base checkout's path.
        directory: Private project state directory that holds the cache.

    Returns:
        One list of repository-relative paths per commit, newest first.
        Empty when Git cannot answer inside its timeout, the checkout has no
        commit, or the cache cannot be written.
    """
    head = (_git(root, "rev-parse", "--verify", "HEAD") or "").strip()
    if not head:
        return []
    cache = directory / CACHE
    try:
        cached = json.loads(cache.read_text())
        if cached.get("base") == head and isinstance(
            cached.get("commits"), list
        ):
            return cached["commits"]
    except (OSError, ValueError, AttributeError):
        pass
    output = _git(
        root,
        "log",
        "--name-only",
        "--format=%x00",
        f"--max-count={WINDOW}",
        "HEAD",
    )
    if output is None:
        return []
    commits = [
        sorted({line for line in block.splitlines() if line})
        for block in output.split("\x00")
    ]
    commits = [files for files in commits if files]
    try:
        directory.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"base": head, "commits": commits}))
    except OSError:
        pass
    return commits


def cochanges(
    commits: list[list[str]],
    paths: list[str],
    overlap: Callable[[str, str], bool],
    threshold: int = THRESHOLD,
) -> dict[str, int]:
    """Counts the files that changed in the same commit as a requested path.

    Args:
        commits: Files per commit, as ``history`` reports them.
        paths: Reservation keys or repository-relative paths being taken.
        overlap: Rule that says whether a committed path matches a key; the
            store's reservation overlap rule, so a glob key matches the files
            under it exactly as a competing reservation would.
        threshold: Fewest shared commits that make a file worth reporting.

    Returns:
        Mapping of co-changed file to how many of the recent commits changed
        it together with one of the requested paths. A file that itself
        matches a requested path is left out, because the reservation already
        covers it.
    """
    keys = [key for key in paths if ":" not in key.partition("/")[0]]
    if not keys or threshold < 1:
        return {}
    counts: Counter[str] = Counter()
    for files in commits:
        matched = {
            file for file in files if any(overlap(file, key) for key in keys)
        }
        if matched:
            counts.update(file for file in files if file not in matched)
    return {file: n for file, n in counts.items() if n >= threshold}


def collisions(
    counts: dict[str, int],
    held: dict[str, list[str]],
    overlap: Callable[[str, str], bool],
) -> list[dict]:
    """Names the co-changed files a peer currently reserves.

    Args:
        counts: Co-changed files and their counts, as ``cochanges`` reports.
        held: Active reservation keys per peer identity, the caller's own
            identity already excluded.
        overlap: The store's reservation overlap rule.

    Returns:
        One record per file and peer, carrying ``path``, ``peer`` and
        ``count``, most frequent first and then by path and peer, clipped to
        ``MAX_FORECAST`` records.
    """
    found: list[dict] = [
        {"path": path, "peer": peer, "count": count}
        for path, count in counts.items()
        for peer, patterns in held.items()
        if any(overlap(path, pattern) for pattern in patterns)
    ]
    found.sort(
        key=lambda entry: (-entry["count"], entry["path"], entry["peer"])
    )
    return found[:MAX_FORECAST]
