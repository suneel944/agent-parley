"""Optional best-effort exchanges with the repository's host forge.

Every call here is optional context, never authority. The local ledger decides
ownership and is written first; a forge lookup or mirror runs afterwards
through the operator's own ``gh`` installation, adds no flag that bypasses a
repository rule, and reports failure instead of raising. Coordination must
keep working with no network, no ``gh`` client and no GitHub remote.
"""

import json
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

MAX_TITLE = 200
MAX_PATHS = 200

GITHUB_REMOTE = re.compile(
    r"^(?:https://|ssh://git@|git@)github\.com[:/]"
    r"(?P<owner>[^/]+)/(?P<name>[^/]+?)(?:\.git)?/?$"
)


def slug(repo: Path) -> str | None:
    """Returns the GitHub owner and name behind a repository's origin remote.

    The forge is optional context, never authority. A checkout without an
    origin remote, a remote on another host, and a failing Git invocation are
    all ordinary outcomes that leave coordination unchanged, so this reports
    absence instead of raising.

    Args:
        repo: Repository or assigned worktree whose origin remote is read.

    Returns:
        The ``owner/name`` slug, or None when origin is missing, is not a
        GitHub remote, or cannot be read.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode:
        return None
    match = GITHUB_REMOTE.match(result.stdout.strip())
    return f"{match['owner']}/{match['name']}" if match else None


def _reachable(repo: Path) -> str | None:
    """Returns the forge project when a usable ``gh`` client is installed."""
    project = slug(repo)
    return project if project and shutil.which("gh") else None


def branch_completion(repo: Path, branch: str) -> tuple[str, float] | None:
    """Reports the newest pull request opened from a lane branch.

    A lane branch outlives the work it was first used for, so the branch name
    alone cannot say whether the current work ended. Only the newest pull
    request describes the current use of the branch; an older merged or closed
    one belongs to a finished generation and must not speak for it. The caller
    correlates the reported creation time with the claim it is asking about.

    Args:
        repo: Repository or assigned worktree that selects the forge project.
        branch: Lane branch whose pull requests are read.

    Returns:
        The newest pull request's state and creation time in Unix seconds, or
        None when the forge is unavailable, the branch has no pull request, or
        the response cannot be read.
    """
    project = _reachable(repo)
    if not project:
        return None
    output = _run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            project,
            "--head",
            branch,
            "--state",
            "all",
            "--limit",
            "10",
            "--json",
            "state,createdAt",
        ],
        5,
    )
    try:
        records = json.loads(output or "[]")
        newest = max(
            (
                (str(record["state"]), _epoch(record["createdAt"]))
                for record in records
                if record.get("state") and record.get("createdAt")
            ),
            key=lambda entry: entry[1],
            default=None,
        )
    except (ValueError, TypeError, AttributeError, KeyError):
        return None
    return newest


def _epoch(value: str) -> float:
    """Converts a forge timestamp to Unix seconds, or raises ValueError."""
    return datetime.fromisoformat(value).timestamp()


def _run(args: list[str], timeout: int) -> str | None:
    """Runs the GitHub CLI, reporting absence instead of raising."""
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    return None if result.returncode else result.stdout


def issue_title(repo: Path, number: str) -> str | None:
    """Returns the host forge's title for an issue, when one is reachable.

    The lookup is best effort and read only. It is skipped entirely without a
    GitHub origin or without the ``gh`` client, and any failure of the client,
    its authentication, or its output reports absence. A resolved title is
    peer-supplied display context; it never decides ownership, so coordination
    must keep working with no network, no ``gh``, and no GitHub remote.

    Args:
        repo: Repository or assigned worktree that selects the forge project.
        number: Bare repository issue number.

    Returns:
        The issue title, clipped to 200 characters, or None when the forge is
        unavailable, refuses the request, or reports no usable title.
    """
    project = _reachable(repo)
    if project is None:
        return None
    output = _run(
        ["gh", "issue", "view", number, "--repo", project, "--json", "title"],
        15,
    )
    if output is None:
        return None
    try:
        title = json.loads(output)["title"]
    except (ValueError, TypeError, KeyError, IndexError):
        return None
    return title[:MAX_TITLE] if isinstance(title, str) else None


def issue_pull_request_paths(repo: Path, number: str) -> list[str]:
    """Lists the files the pull requests that closed an issue touched.

    A claimed issue that already had pull requests names, through those pull
    requests, the paths the work tends to touch. The reading is best effort
    and read only: it is skipped without a GitHub origin or the ``gh`` client,
    and a slow or refusing forge reports nothing rather than raising.

    Args:
        repo: Repository or assigned worktree that selects the forge project.
        number: Bare repository issue number.

    Returns:
        Sorted unique repository-relative paths, at most ``MAX_PATHS``, or an
        empty list when the forge is unavailable or no pull request refers to
        the issue.
    """
    project = _reachable(repo)
    if project is None:
        return []
    output = _run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            project,
            "--search",
            f"closes #{number}",
            "--state",
            "all",
            "--limit",
            "10",
            "--json",
            "files",
        ],
        15,
    )
    if output is None:
        return []
    paths: set[str] = set()
    try:
        for record in json.loads(output):
            for entry in record.get("files") or []:
                if isinstance(entry.get("path"), str):
                    paths.add(entry["path"])
    except (ValueError, TypeError, AttributeError):
        return []
    return sorted(paths)[:MAX_PATHS]


def assign(repo: Path, number: str) -> bool:
    """Records the operator's forge account as an issue's assignee.

    A claim is recorded in the local ledger first; this mirrors it onto the
    host forge so a reader outside Agent Parley can see that the issue is
    being worked. The mirror is best effort and carries no authority: the
    ledger stays correct with no network, no ``gh`` and no GitHub remote, and
    a rejected write changes no coordination state.

    The forge sees one assignee, the operator's own account, because every
    lane runs under that account. A handoff between participants therefore
    changes the ledger owner without changing the forge assignee.

    Args:
        repo: Repository or assigned worktree that selects the forge project.
        number: Bare repository issue number.

    Returns:
        True when the forge accepted the assignment, False when the forge is
        unavailable or refused it.
    """
    project = _reachable(repo)
    if project is None:
        return False
    return (
        _run(
            [
                "gh",
                "issue",
                "edit",
                number,
                "--repo",
                project,
                "--add-assignee",
                "@me",
            ],
            20,
        )
        is not None
    )


def unassign(repo: Path, number: str) -> bool:
    """Removes the operator's forge account from a released issue.

    This is the counterpart of :func:`assign` and carries the same best-effort
    contract. It removes only the account Agent Parley added, so an assignee
    a person set by hand is left in place.

    Args:
        repo: Repository or assigned worktree that selects the forge project.
        number: Bare repository issue number.

    Returns:
        True when the forge accepted the removal, False when the forge is
        unavailable or refused it.
    """
    project = _reachable(repo)
    if project is None:
        return False
    return (
        _run(
            [
                "gh",
                "issue",
                "edit",
                number,
                "--repo",
                project,
                "--remove-assignee",
                "@me",
            ],
            20,
        )
        is not None
    )


def comment(repo: Path, number: str, body: str) -> bool:
    """Adds one comment to an issue on the host forge.

    The comment reproduces what a participant reported and states that a
    reported state is the participant's own account rather than review. It is
    best effort: an unreachable forge leaves the report recorded locally and
    unchanged.

    Args:
        repo: Repository or assigned worktree that selects the forge project.
        number: Bare repository issue number.
        body: Comment text, already shaped by the caller.

    Returns:
        True when the forge accepted the comment, False when the forge is
        unavailable or refused it.
    """
    project = _reachable(repo)
    if project is None:
        return False
    return (
        _run(
            [
                "gh",
                "issue",
                "comment",
                number,
                "--repo",
                project,
                "--body",
                body,
            ],
            20,
        )
        is not None
    )
