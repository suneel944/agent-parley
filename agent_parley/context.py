"""Reads how full a lane's context window is, what it changed and where.

These are the per-lane readings an operator view shows beside the injected
context bytes coordination itself counts. Each is best effort and read-only:
an answer that cannot be read is None, which a view renders as unknown and
never replaces with a guess.

The context-window use is read from the same native session records
`records` reads tokens from. It is reported only where the provider's own
record states both the tokens in the window and the window's size; no
per-model window table is kept here, so a provider whose record omits the
size reads as unknown.

The diffstat compares a lane's worktree with the point its branch forked
from the base head. No fork point is kept between readings, so `git diff
--merge-base` derives it inside the one diff call each lane costs, and the
lanes are read concurrently rather than one after another.
"""

from __future__ import annotations

import json
import socket
import subprocess
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from agent_parley import records, roster
from agent_parley.state import BridgeError

GIT_WORKERS = 8
GIT_SECONDS = 10
CONTEXT_MARK = b"model_context_window"

ContextReader = Callable[[dict], "float | None"]


def _codex_context(record: dict) -> float | None:
    """Reads the context-window use one Codex token-count event states.

    A Codex rollout's ``token_count`` event carries ``last_token_usage``, the
    tokens of the latest request, and ``model_context_window``, the window
    size Codex itself applies to the session's model.

    Args:
        record: One parsed rollout record.

    Returns:
        The latest request's tokens as a percentage of the stated window, or
        None when the record states either value not as a positive integer.
    """
    payload = record.get("payload")
    source = payload if isinstance(payload, dict) else record
    info = source.get("info")
    if source.get("type") != "token_count" or not isinstance(info, dict):
        return None
    window = info.get("model_context_window")
    usage = info.get("last_token_usage")
    tokens = usage.get("total_tokens") if isinstance(usage, dict) else None
    if (
        not isinstance(window, int)
        or not isinstance(tokens, int)
        or isinstance(window, bool)
        or isinstance(tokens, bool)
        or window <= 0
        or tokens < 0
    ):
        return None
    return 100.0 * tokens / window


CONTEXT_READERS: dict[str, ContextReader] = {
    "codex": _codex_context,
}


def reports_context(home: Path, provider: str) -> bool:
    """Reports whether a provider's lanes can read their context-window use.

    Args:
        home: Private bridge state root.
        provider: Provider name a lane runs.

    Returns:
        True when the provider is defined and its adapter has a reader in
        ``CONTEXT_READERS``; every other lane's use is always unknown.
    """
    try:
        entry = roster.provider(home, provider)
    except BridgeError:
        return False
    return str(entry.get("adapter", "")) in CONTEXT_READERS


def lane_context_use(home: Path, participant: dict) -> float | None:
    """Reports how full a lane's context window is, as its client recorded.

    Only the tail of the lane's newest native session record is read, so the
    cost does not grow with the session's history. The latest event stating
    both values wins.

    Args:
        home: Private bridge state root.
        participant: Manifest entry naming the lane, provider and account.

    Returns:
        The percentage of the context window in use, which can exceed 100
        when the client's own count does, or None when the provider records
        no window size or no readable event states one.
    """
    try:
        entry = roster.provider(home, str(participant.get("provider", "")))
        adapter = str(entry.get("adapter", ""))
        reader = CONTEXT_READERS[adapter]
        finder = records.ADAPTERS[adapter][0]
        config = records._config_home(
            home, entry, participant.get("credential")
        )
        if config is None:
            return None
        path = finder(config, Path(str(participant.get("lane", ""))))
        if path is None:
            return None
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(max(0, size - records.MAX_TAIL))
            chunk = handle.read(records.MAX_TAIL)
    except (BridgeError, KeyError, OSError, ValueError):
        return None
    lines = chunk.split(b"\n")[1 if size > records.MAX_TAIL else 0 :]
    for line in reversed(lines):
        if CONTEXT_MARK not in line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and (use := reader(record)) is not None:
            return use
    return None


def context_full(use: float | None, threshold: float) -> bool:
    """Reports whether a lane's context-window use reached the threshold.

    Args:
        use: Percentage `lane_context_use` read, or None when unknown.
        threshold: Percentage at or above which a window counts as full,
            the supervision setting ``context_full_percent``.

    Returns:
        True only for a known use at or above the threshold; an unknown use
        is never flagged.
    """
    return use is not None and use >= threshold


def lane_location(participant: dict) -> dict[str, str]:
    """Names where a lane runs.

    Agent Parley coordinates lanes on one host, so every lane runs on the
    host reading it.

    Args:
        participant: Manifest entry naming the lane.

    Returns:
        The ``host`` name and the lane's ``worktree`` path.
    """
    return {
        "host": socket.gethostname(),
        "worktree": str(participant.get("lane", "")),
    }


def _git(checkout: str, *arguments: str) -> str | None:
    """Runs one read-only Git command, returning None when it fails."""
    try:
        result = subprocess.run(
            ["git", "-C", checkout, *arguments],
            capture_output=True,
            text=True,
            timeout=GIT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return None if result.returncode else result.stdout


def lane_diffstat(participant: dict, base: str) -> dict | None:
    """Counts the lines a lane changed since its branch forked from a base.

    The comparison runs from the merge base of ``base`` and the lane's head
    to the lane's worktree, so commits on the lane's branch and uncommitted
    edits to tracked files both count; untracked files do not.

    Args:
        participant: Manifest entry naming the lane worktree.
        base: Base head commit the lane forked from.

    Returns:
        ``files``, one ``{"path", "added", "deleted"}`` entry per changed
        path in Git's order, with None counts for a binary file, and the
        ``added`` and ``deleted`` line totals; or None when Git could not
        answer.
    """
    listing = _git(
        str(participant.get("lane", "")),
        "diff",
        "--numstat",
        "--no-renames",
        "--merge-base",
        base,
    )
    if listing is None:
        return None
    files = []
    for line in listing.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        added, deleted, path = parts
        files.append(
            {
                "path": path,
                "added": int(added) if added.isdigit() else None,
                "deleted": int(deleted) if deleted.isdigit() else None,
            }
        )
    return {
        "files": files,
        "added": sum(entry["added"] or 0 for entry in files),
        "deleted": sum(entry["deleted"] or 0 for entry in files),
    }


def lane_diffstats(manifest: dict) -> dict[str, dict | None]:
    """Reads the diffstat of every working lane in a project.

    The base head is read once per project; each lane then costs one Git
    call, up to ``GIT_WORKERS`` of them at once. A retired lane is skipped.

    Args:
        manifest: Project manifest naming the base checkout and the roster.

    Returns:
        Mapping of participant name to its `lane_diffstat`, every value None
        when the base head could not be read.
    """
    participants = {
        name: participant
        for name, participant in manifest["participants"].items()
        if not roster.retired(participant)
    }
    head = (_git(manifest["root"], "rev-parse", "HEAD") or "").strip()
    if not head:
        return dict.fromkeys(participants)
    with ThreadPoolExecutor(max_workers=GIT_WORKERS) as pool:
        stats = pool.map(
            lambda participant: lane_diffstat(participant, head),
            participants.values(),
        )
        return dict(zip(participants, stats, strict=True))


def lane_fields(
    home: Path,
    participant: dict,
    diffstat: dict | None,
    full_percent: float,
) -> dict:
    """Builds the additive row fields every operator view shows per lane.

    Args:
        home: Private bridge state root.
        participant: Manifest entry naming the lane, provider and account.
        diffstat: The lane's entry from `lane_diffstats`, or None when it
            could not be read.
        full_percent: Percentage at or above which the window is flagged,
            the supervision setting ``context_full_percent``.

    Returns:
        ``context_percent`` (None when unknown), ``context_full``,
        ``diff_files`` with ``diff_added`` and ``diff_deleted`` (None when
        unknown), ``diff_paths``, and the ``host`` and ``worktree`` the
        lane runs in.
    """
    use = lane_context_use(home, participant)
    stat = diffstat or {}
    return {
        "context_percent": use,
        "context_full": context_full(use, full_percent),
        "diff_files": len(stat["files"]) if diffstat else None,
        "diff_added": stat.get("added"),
        "diff_deleted": stat.get("deleted"),
        "diff_paths": [entry["path"] for entry in stat.get("files", [])],
        **lane_location(participant),
    }


def window_cell(row: dict) -> str:
    """Formats a lane's context-window use for one table cell.

    Args:
        row: A row carrying the `lane_fields` keys.

    Returns:
        ``unknown`` when the provider reports no use, else the whole-number
        percentage, followed by ``!`` once it reaches the threshold.
    """
    use = row.get("context_percent")
    if use is None:
        return "unknown"
    return f"{use:.0f}%" + ("!" if row.get("context_full") else "")


def diff_cell(row: dict) -> str:
    """Formats a lane's diffstat for one table cell.

    Args:
        row: A row carrying the `lane_fields` keys.

    Returns:
        ``-`` when the diffstat is unknown, else ``+added/-deleted`` and the
        count of changed files.
    """
    if row.get("diff_files") is None:
        return "-"
    return f"+{row['diff_added']}/-{row['diff_deleted']} {row['diff_files']}f"


def location_cell(row: dict) -> str:
    """Formats where a lane runs for one table cell.

    Args:
        row: A row carrying the `lane_fields` keys.

    Returns:
        ``host:worktree``, or ``-`` when the row names no worktree.
    """
    worktree = row.get("worktree")
    return f"{row.get('host', '')}:{worktree}" if worktree else "-"
