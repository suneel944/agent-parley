"""Decides which lane worktrees and branches a project may reclaim.

A lane is a Git worktree inside the project's private state directory and a
branch in the operator's repository, and both outlive the claim they were
created for. Nothing removed here can be recovered from the bridge, so this
is a refusal engine before it is a sweep: a lane is reclaimed only when its
work is provably in the project base, provably on its upstream, and the
forge shows the work landed. Every other lane is kept and reported with the
one condition that held it, because a report an operator can read is always
better than a deletion a guess produced.

The evidence a reclaim needs is deliberately narrow. A branch whose newest
pull request merged has landed. A branch whose upstream no longer carries it
has landed as well, because the forge deletes the head branch on merge when
the repository asks it to. An unreachable forge, an unreadable worktree and
an ambiguous answer are all refusals rather than assumptions.
"""

import subprocess
from pathlib import Path

from agent_parley import forge, issues
from agent_parley.state import LockBusy, lock

GIT_SECONDS = 5
MAX_REPORTED_PATHS = 10

MERGED = "its pull request merged"
GONE = "its branch is gone from its upstream"
OUTSIDE = "its worktree is outside the project state directory"
UNREGISTERED = "the base checkout does not register its worktree"
UNREADABLE = "Git could not inspect it"
SESSION = "a session is running in it"
CLAIMED = "it still holds a claim"
UNCOMMITTED = "it holds uncommitted changes"
UNMERGED = "its branch holds commits the base checkout does not have"
UNUSED = "its branch never left the commit its lane was created from"
UNPUSHED = "its branch holds commits its upstream does not have"
PULL_OPEN = "its pull request is still open"
PUBLISHED = "its upstream still carries its branch"
PULL_CLOSED = "its pull request was closed without merging"
UNLANDED = "nothing shows its branch landed"


def _read(root: str, *arguments: str) -> str | None:
    """Runs one read-only Git command against a checkout.

    Args:
        root: Checkout the command runs in.
        *arguments: Git arguments following the checkout selection.

    Returns:
        Standard output without surrounding whitespace, or None when Git
        refused the command, could not be run, or exceeded its timeout. An
        answer Git cannot give is no opinion rather than an empty one.
    """
    try:
        result = subprocess.run(
            ["git", "-C", root, *arguments],
            capture_output=True,
            text=True,
            timeout=GIT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode:
        return None
    return result.stdout.strip()


def worktrees(root: str) -> set[str] | None:
    """Lists the worktree paths the base checkout registers.

    Args:
        root: Canonical project key, which is the base checkout's path.

    Returns:
        Resolved worktree paths, or None when Git could not answer.
    """
    listing = _read(root, "worktree", "list", "--porcelain")
    if listing is None:
        return None
    found = set()
    for line in listing.splitlines():
        if line.startswith("worktree "):
            found.add(str(Path(line[len("worktree ") :]).resolve()))
    return found


def _lines(value: str | None) -> list[str] | None:
    """Splits Git output into entries, preserving an unreadable answer."""
    if value is None:
        return None
    return [entry for entry in value.splitlines() if entry]


def _uncommitted(lane: str) -> list[str] | None:
    """Lists the paths Git reports as changed inside one lane worktree.

    Args:
        lane: Lane worktree the status is read from.

    Returns:
        Worktree-relative paths, empty when the lane is clean, or None when
        Git could not inspect the lane.
    """
    listing = _read(lane, "status", "--porcelain", "-uall")
    entries = _lines(listing)
    if entries is None:
        return None
    paths = []
    for entry in entries:
        if len(entry) < 4:
            continue
        path = entry[3:]
        if entry[0] in "RC" and " -> " in path:
            path = path.split(" -> ", 1)[1]
        paths.append(path.rstrip("/"))
    return paths


def _ahead(root: str, start: str, end: str) -> list[str] | None:
    """Lists the commits on one ref that another ref does not carry.

    Args:
        root: Checkout holding both refs.
        start: Ref the comparison starts from.
        end: Ref whose extra commits are reported.

    Returns:
        Short commit lines, empty when nothing is ahead, or None when Git
        could not compare the two refs.
    """
    return _lines(_read(root, "log", "--oneline", f"{start}..{end}"))


def _upstream(root: str, branch: str) -> tuple[str, str] | None:
    """Names the remote and remote branch a lane branch tracks.

    Args:
        root: Checkout holding the branch configuration.
        branch: Lane branch whose upstream is read.

    Returns:
        The remote name and the remote branch's short name, or None when the
        branch tracks nothing.
    """
    remote = _read(root, "config", "--get", f"branch.{branch}.remote")
    merge = _read(root, "config", "--get", f"branch.{branch}.merge")
    if not remote or not merge:
        return None
    return remote, merge.removeprefix("refs/heads/")


def _upstream_gone(root: str, remote: str, branch: str) -> bool | None:
    """Reports whether the upstream no longer carries a lane branch.

    Args:
        root: Checkout the remote is queried from.
        remote: Configured remote name.
        branch: Remote branch name the lane tracks.

    Returns:
        True when the remote answers without listing the branch, False when
        it still lists it, or None when the remote could not be reached.
    """
    listing = _read(root, "ls-remote", "--heads", remote, branch)
    if listing is None:
        return None
    return not listing.strip()


def landed(root: str, branch: str) -> tuple[bool, str]:
    """Decides whether a lane branch's work has provably landed.

    Args:
        root: Base checkout the forge project and remote are read from.
        branch: Lane branch whose newest pull request is read.

    Returns:
        Whether the branch may be reclaimed and the condition that decided
        it. A closed unmerged pull request, an open one, an unreachable
        forge and a remote that still carries the branch all decide against
        reclaiming.
    """
    completion = forge.branch_completion(Path(root), branch)
    if completion is not None:
        state = completion[0].upper()
        if state == "MERGED":
            return True, MERGED
        if state == "CLOSED":
            return False, PULL_CLOSED
        return False, PULL_OPEN
    tracking = _upstream(root, branch)
    if tracking is None:
        return False, UNLANDED
    gone = _upstream_gone(root, *tracking)
    if gone is None:
        return False, UNLANDED
    return (True, GONE) if gone else (False, PUBLISHED)


def _outcome(reason: str, paths: list[str] | None = None) -> dict:
    """Shapes the decision half of one lane assessment."""
    return {
        "reclaim": reason in {MERGED, GONE},
        "reason": reason,
        "paths": (paths or [])[:MAX_REPORTED_PATHS],
    }


def _busy(directory: Path, name: str) -> bool:
    """Reports whether a session currently holds the lane's session lock."""
    try:
        with lock(directory / f"{name}.session.lock"):
            return False
    except LockBusy:
        return True
    except OSError:
        return True


def assess(
    directory: Path,
    manifest: dict,
    name: str,
    *,
    registered: set[str] | None,
    claimed: bool,
    busy: bool,
) -> dict:
    """Decides what may be done with one lane, and why.

    The order of the conditions is the order of their cost and their
    severity. Containment and registration come first, because a path the
    project does not own is never touched whatever its contents say. Held
    work comes next, then uncommitted files, then commits the base checkout
    or the upstream does not carry, then a branch that never left the commit
    its lane was created from, and only a lane that survived all of them is
    measured against the forge. The base checkout is read at its own head,
    which is the branch the project's own integration merges a lane into.

    Args:
        directory: Private project state directory holding the lanes.
        manifest: Project manifest naming the root, base and participants.
        name: Participant whose lane is assessed.
        registered: Worktree paths the base checkout registers, or None when
            Git could not list them.
        claimed: Whether the ledger still records this lane owning work.
        busy: Whether a session currently runs in this lane.

    Returns:
        The participant, its lane, its branch, whether the lane may be
        reclaimed, the condition that decided it and any paths that
        condition names.
    """
    participant = manifest["participants"][name]
    lane = Path(participant["lane"])
    branch = participant["branch"]
    root = manifest["root"]
    row = {"participant": name, "lane": str(lane), "branch": branch}
    if lane.parent.resolve() != directory.resolve():
        return {**row, **_outcome(OUTSIDE)}
    if registered is None:
        return {**row, **_outcome(UNREADABLE)}
    if str(lane.resolve()) not in registered:
        return {**row, **_outcome(UNREGISTERED)}
    if busy:
        return {**row, **_outcome(SESSION)}
    if claimed:
        return {**row, **_outcome(CLAIMED)}
    changed = _uncommitted(str(lane))
    if changed is None:
        return {**row, **_outcome(UNREADABLE)}
    if changed:
        return {**row, **_outcome(UNCOMMITTED, changed)}
    unmerged = _ahead(root, "HEAD", branch)
    if unmerged is None:
        return {**row, **_outcome(UNREADABLE)}
    if unmerged:
        return {**row, **_outcome(UNMERGED, unmerged)}
    tip = _read(root, "rev-parse", branch)
    if tip is None:
        return {**row, **_outcome(UNREADABLE)}
    if tip == _read(root, "rev-parse", manifest["base"]):
        return {**row, **_outcome(UNUSED)}
    tracking = _upstream(root, branch)
    if tracking is not None:
        remote, head = tracking
        unpushed = _ahead(root, f"refs/remotes/{remote}/{head}", branch)
        if unpushed:
            return {**row, **_outcome(UNPUSHED, unpushed)}
    return {**row, **_outcome(landed(root, branch)[1])}


def plan(directory: Path, manifest: dict) -> list[dict]:
    """Assesses every lane a project registers.

    Args:
        directory: Private project state directory holding the lanes.
        manifest: Project manifest naming the root, base and participants.

    Returns:
        One assessment per participant, in manifest order.
    """
    registered = worktrees(manifest["root"])
    owned = issues.holders(issues.snapshot(directory))
    return [
        assess(
            directory,
            manifest,
            name,
            registered=registered,
            claimed=bool(owned.get(name)),
            busy=_busy(directory, name),
        )
        for name in manifest["participants"]
    ]


def reclaimable(rows: list[dict]) -> list[dict]:
    """Returns only the assessments that decided in favour of a reclaim."""
    return [row for row in rows if row["reclaim"]]


def lines(rows: list[dict]) -> list[str]:
    """Renders a sweep's assessments as one readable line each.

    Args:
        rows: Assessments, each already carrying its decision.

    Returns:
        A line per lane naming what happened or what held it, with the paths
        a refusal reported.
    """
    rendered = []
    for row in rows:
        if row.get("removed"):
            verb = "reclaimed"
        elif row["reclaim"]:
            verb = "reclaimable"
        else:
            verb = "kept"
        detail = f"; {', '.join(row['paths'])}" if row["paths"] else ""
        rendered.append(
            f"{row['participant']}: {verb} ({row['reason']}){detail}"
        )
    return rendered
