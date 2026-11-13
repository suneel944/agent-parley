"""Records the Git worktrees a lane's native client works in beside its own.

The ``claude`` CLI can run a session, or a subagent, in a worktree of its
own: by default under ``<lane>/.claude/worktrees/``, or wherever the user
confirms. Its hook documentation states that a configured ``WorktreeCreate``
hook replaces the CLI's own worktree creation and must print the path it
made, and that ``WorktreeRemove`` fires only for worktrees such a hook made.
Installing either would hand checkout creation to this package, so neither
is installed. Registration is by observation instead: a hook event whose cwd
is a worktree of the lane's own repository is recorded on the lane's roster
entry, and nothing here ever creates, moves or prunes a checkout.

The path is recorded because unregistered checkouts are a litter source:
one audit found 971 stray worktrees across machines, made by sessions no
coordinator knew of. A recorded worktree is listed with its lane in ``top``,
``flagged`` when it lies outside ``<lane>/.claude/worktrees/`` (the place
the CLI's own confirmation prompt names), and closed once its directory is
gone. Its ``base`` reads ``lane`` when the lane branch tip is an ancestor of
the worktree's ``HEAD`` and ``default`` otherwise, the case where the CLI's
``worktree.baseRef`` setting cut it from ``origin/<default>`` rather than
from the lane.

A recorded worktree is not a participant: it has no provider, credential,
display name or launch, and the lane's identity and process handling are
unchanged. On first registration the lane reserves the worktree's
repository-relative path through the store's ``file_reservation_paths``
tool, so a peer reserving that path is told of the conflict. Reservations
are advisory. A worktree outside the repository has no repository-relative
path, and the store's named-resource keys cannot carry an absolute path, so
such a worktree is recorded and flagged but not reserved. The store
releases a lane's leases only all at once, so closing a record leaves its
lease to the lane's own release.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import time
from pathlib import Path

from agent_parley import roster, store
from agent_parley.state import BridgeError, LockBusy, lock, write_json

KEY = "worktrees"
CLI_WORKTREES = Path(".claude") / "worktrees"
GIT_SECONDS = 3
LOCK_SECONDS = 1.0
REFRESH_SECONDS = 60.0
MAX_CLOSED = 16
REASON = "worktree observed in this lane's session"


def _git(where: Path, *args: str) -> subprocess.CompletedProcess | None:
    """Runs one bounded read-only Git command.

    Args:
        where: Directory Git runs in.
        *args: Git arguments after ``-C <where>``.

    Returns:
        The completed process, or None when Git could not run or exceeded
        `GIT_SECONDS`, the same bound the lane's branch inspection uses.
    """
    try:
        return subprocess.run(
            ["git", "-C", str(where), *args],
            capture_output=True,
            text=True,
            timeout=GIT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _lines(result: subprocess.CompletedProcess | None) -> list[str]:
    """Reads the output lines of a Git command that succeeded.

    Args:
        result: Completed process from `_git`.

    Returns:
        The stripped output lines, or an empty list for a failure.
    """
    if result is None or result.returncode:
        return []
    return [line.strip() for line in result.stdout.splitlines()]


def _inspect(lane: Path, cwd: Path) -> dict | None:
    """Reads the worktree a cwd lies in, when it shares the lane's repository.

    Args:
        lane: Resolved lane worktree.
        cwd: Resolved working directory a hook event reported.

    Returns:
        A new record with ``path``, ``branch``, ``base`` and ``flagged``,
        or None when the cwd is not in a worktree of the lane's repository,
        is the lane itself, or Git cannot answer.
    """
    mine = _lines(
        _git(
            cwd,
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
            "--show-toplevel",
            "HEAD",
        )
    )
    if len(mine) != 3:
        return None
    theirs = _lines(
        _git(
            lane,
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
            "HEAD",
        )
    )
    if len(theirs) != 2:
        return None
    top = Path(mine[1]).resolve()
    if Path(mine[0]).resolve() != Path(theirs[0]).resolve() or top == lane:
        return None
    branch = _lines(_git(top, "branch", "--show-current"))
    ancestry = _git(top, "merge-base", "--is-ancestor", theirs[1], mine[2])
    return {
        "path": str(top),
        "branch": branch[0] if branch else "",
        "base": "lane" if ancestry and not ancestry.returncode else "default",
        "flagged": not top.is_relative_to(lane / CLI_WORKTREES),
    }


def known(manifest: dict, name: str, cwd: Path) -> dict | None:
    """Finds the open recorded worktree a cwd lies in.

    The reading is pure, so the deadline path, which may not run Git, can
    accept a cwd the full decision registered earlier.

    Args:
        manifest: Project manifest.
        name: Participant whose records are read.
        cwd: Resolved working directory.

    Returns:
        The open record whose path holds the cwd, or None.
    """
    entry = manifest["participants"].get(name) or {}
    for found in entry.get(KEY) or []:
        if not found.get("closed") and cwd.is_relative_to(found["path"]):
            return found
    return None


def _reserve(
    home: Path, directory: Path, agent: str, path: Path, lane: Path
) -> None:
    """Reserves a newly registered worktree's path for its lane.

    A failure to reserve never fails the hook event: the record stands and
    the lane can still reserve the path itself.

    Args:
        home: Private bridge state root.
        directory: Private project state directory.
        agent: Lane that works in the worktree.
        path: Resolved worktree path.
        lane: Resolved lane worktree.
    """
    if not path.is_relative_to(lane):
        return
    try:
        identity = json.loads(
            (directory / f"{agent}-identity.json").read_text()
        )
        actor = store.authenticate(home, identity["registration_token"])
        if actor is not None:
            store.call(
                home,
                actor,
                "file_reservation_paths",
                {
                    "paths": [path.relative_to(lane).as_posix()],
                    "reason": REASON,
                },
            )
    except (BridgeError, OSError, ValueError, KeyError, sqlite3.Error):
        return


def register(
    directory: Path,
    agent: str,
    lane: Path,
    cwd: Path,
    *,
    home: Path | None = None,
) -> dict | None:
    """Records the worktree of the lane's repository a hook event came from.

    Only a cwd outside the lane, or inside ``<lane>/.claude/worktrees/``,
    is inspected, so an event from the lane itself runs no Git. A cwd in a
    worktree already recorded and open runs no Git either, and its ``seen``
    time is rewritten under the setup lock at most once per
    `REFRESH_SECONDS`. A worktree inspected while another writer holds the
    setup lock is returned unrecorded, so the event is still accepted and
    the next one records it.

    Args:
        directory: Private project state directory.
        agent: Lane the event belongs to.
        lane: Resolved lane worktree.
        cwd: Resolved working directory the event reported.
        home: Private bridge state root; when given, a new record's path is
            reserved for the lane.

    Returns:
        A copy of the record with ``path``, ``branch``, ``base``,
        ``flagged``, ``seen`` and ``closed``, or None when the cwd is not a
        worktree of the lane's repository other than the lane.
    """
    if cwd.is_relative_to(lane) and not cwd.is_relative_to(
        lane / CLI_WORKTREES
    ):
        return None
    try:
        manifest = roster.read(directory)
    except (BridgeError, OSError, ValueError):
        return None
    moment = time.time()
    current = known(manifest, agent, cwd)
    if current is not None and (
        moment - float(current.get("seen") or 0.0) < REFRESH_SECONDS
    ):
        return dict(current)
    found = current if current is not None else _inspect(lane, cwd)
    if found is None:
        return None
    fresh = False
    try:
        with lock(directory / "setup.lock", timeout=LOCK_SECONDS):
            manifest = roster.read(directory)
            entry = manifest["participants"].get(agent)
            if entry is None:
                return None
            current = known(manifest, agent, cwd)
            if current is None:
                fresh = True
                current = {**found, "closed": None}
                entry.setdefault(KEY, []).append(current)
            current["seen"] = moment
            write_json(directory / "project.json", manifest)
    except LockBusy:
        return {"closed": None, **found, "seen": moment}
    except (BridgeError, OSError, ValueError):
        return None
    if fresh and home is not None:
        _reserve(home, directory, agent, Path(current["path"]), lane)
    return dict(current)


def close(directory: Path, agent: str) -> list[str]:
    """Closes every open record whose worktree directory is gone.

    The manifest is read without the setup lock first, and the lock is taken
    only when a recorded path is missing. Only the newest `MAX_CLOSED`
    closed records are kept, so the roster entry stays bounded.

    Args:
        directory: Private project state directory.
        agent: Lane whose records are checked.

    Returns:
        The paths closed now, in record order.
    """
    unlocked = roster.read(directory)
    if not any(
        not found.get("closed") and not Path(found["path"]).exists()
        for found in of(unlocked, agent)
    ):
        return []
    closed: list[str] = []
    with lock(directory / "setup.lock", timeout=LOCK_SECONDS):
        manifest = roster.read(directory)
        entry = manifest["participants"].get(agent) or {}
        records = entry.get(KEY) or []
        moment = time.time()
        for found in records:
            if not found.get("closed") and not Path(found["path"]).exists():
                found["closed"] = moment
                closed.append(found["path"])
        if closed:
            ended = [found for found in records if found.get("closed")]
            dropped = ended[: max(0, len(ended) - MAX_CLOSED)]
            entry[KEY] = [found for found in records if found not in dropped]
            write_json(directory / "project.json", manifest)
    return closed


def of(manifest: dict, name: str) -> list[dict]:
    """Lists one lane's recorded worktrees for a status or web view.

    Args:
        manifest: Project manifest.
        name: Participant whose records are listed.

    Returns:
        A copy of every open and closed record, in the order recorded.
    """
    entry = manifest["participants"].get(name) or {}
    return [dict(found) for found in entry.get(KEY) or []]
