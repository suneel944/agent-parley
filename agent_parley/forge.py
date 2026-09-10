"""Optional read-only lookups against the repository's host forge."""

import json
import re
import shutil
import subprocess
from pathlib import Path

MAX_TITLE = 200

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
    project = slug(repo)
    if project is None or shutil.which("gh") is None:
        return None
    try:
        result = subprocess.run(
            [
                "gh",
                "issue",
                "view",
                number,
                "--repo",
                project,
                "--json",
                "title",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode:
        return None
    try:
        title = json.loads(result.stdout)["title"]
    except (ValueError, TypeError, KeyError, IndexError):
        return None
    return title[:MAX_TITLE] if isinstance(title, str) else None
