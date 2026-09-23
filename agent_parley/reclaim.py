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

Two lanes need no forge at all. A lane whose session stopped longer ago than
the inactivity threshold, that holds no claim, no reservation and no pending
offer, and whose clean worktree never left the commit it was created from
holds nothing anybody could lose, so keeping it only keeps a peer that
bounces every share sent to it. A lane whose worktree is already gone has
nothing left to protect either; retiring it keeps a branch that carries
commits, so no work leaves with the row.

Lanes also make worktrees of their own, for pull requests and sub-tasks, and
Git registers each against the project repository. Those are read from
`git worktree list` and removed only when they sit inside the project state
directory, are clean, carry no commit the base checkout lacks and no commit
their upstream lacks, and have not changed within the inactivity threshold.
A worktree outside the state directory is reported and never touched,
because nothing distinguishes it from one the operator made by hand.
"""

import os
import sqlite3
import subprocess
import time
from pathlib import Path

from agent_parley import forge, issues, store
from agent_parley.state import BridgeError, LockBusy, lock

GIT_SECONDS = 5
MAX_REPORTED_PATHS = 10

MERGED = "its pull request merged"
STOPPED = (
    "it stopped, holds no work and never left the commit its lane was "
    "created from"
)
VANISHED = "its worktree is gone and it holds no work"
LEASED = "it still holds a reservation"
OFFERED = "a handoff offer to it is still pending"
LOCKED = "Git holds a lock on it"
MISSING = "its directory is gone"
RECENT = "it changed within the inactivity threshold"
CONTAINED = "its commits are all in the base checkout"
DETACHED = "its commits are not in the base checkout"
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
REMOVABLE = frozenset({MERGED, GONE, STOPPED, VANISHED, MISSING, CONTAINED})


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
        "reclaim": reason in REMOVABLE,
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
    held: str = "",
    quiet: bool = False,
) -> dict:
    """Decides what may be done with one lane, and why.

    The order of the conditions is the order of their cost and their
    severity. Containment comes first, because a path the project does not
    own is never touched whatever its contents say. A lane whose worktree
    is gone is decided next, on held work alone, because Git has nothing
    left to read in it. Registration follows, then held work, then
    uncommitted files, then commits the base checkout or the upstream does
    not carry, then a branch that never left the commit its lane was created
    from, which is reclaimed only once the lane has stopped and stayed
    quiet, and only a lane that survived all of them is measured against
    the forge. The base checkout is read at its own head, which is the
    branch the project's own integration merges a lane into.

    Args:
        directory: Private project state directory holding the lanes.
        manifest: Project manifest naming the root, base and participants.
        name: Participant whose lane is assessed.
        registered: Worktree paths the base checkout registers, or None when
            Git could not list them.
        claimed: Whether the ledger still records this lane owning work.
        busy: Whether a session currently runs in this lane.
        held: The condition naming a reservation or a pending offer this
            lane still holds, or empty when it holds neither.
        quiet: Whether the lane's session stopped and nothing in the lane
            changed for longer than the inactivity threshold.

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
    blocker = SESSION if busy else CLAIMED if claimed else held
    if not lane.exists():
        return {**row, **_outcome(blocker or VANISHED)}
    if registered is None:
        return {**row, **_outcome(UNREADABLE)}
    if str(lane.resolve()) not in registered:
        return {**row, **_outcome(UNREGISTERED)}
    if blocker:
        return {**row, **_outcome(blocker)}
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
        return {**row, **_outcome(STOPPED if quiet else UNUSED)}
    tracking = _upstream(root, branch)
    if tracking is not None:
        remote, head = tracking
        unpushed = _ahead(root, f"refs/remotes/{remote}/{head}", branch)
        if unpushed:
            return {**row, **_outcome(UNPUSHED, unpushed)}
    return {**row, **_outcome(landed(root, branch)[1])}


def _changed(path: Path) -> float:
    """Reads when a worktree directory last changed, zero when it is gone."""
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _quiet(directory: Path, name: str, lane: Path, after: float) -> bool:
    """Reports whether a lane stopped and stayed untouched past a threshold.

    Args:
        directory: Private project state directory holding the lanes.
        name: Participant whose lane is read.
        lane: The participant's worktree.
        after: Inactivity threshold in seconds.

    Returns:
        True when no session process is recorded alive for the lane and
        neither its newest native activity nor its worktree directory
        changed within the threshold. A lane created moments ago and never
        launched is therefore not quiet yet.
    """
    from agent_parley import supervision

    observed = supervision.presence(directory, name, after)
    if observed["state"] != supervision.STOPPED:
        return False
    newest = max(float(observed["last_active"] or 0), _changed(lane))
    return time.time() - newest > after


def _holds(
    ledger: dict, reservations: dict[str, list[str]], name: str, display: str
) -> str:
    """Names the reservation or pending offer a lane still holds, if any."""
    if reservations.get(display):
        return LEASED
    for record in ledger.get("issues", {}).values():
        if (record.get("offer") or {}).get("to") == name:
            return OFFERED
    return ""


def plan(directory: Path, manifest: dict) -> list[dict]:
    """Assesses every lane a project registers.

    Args:
        directory: Private project state directory holding the lanes.
        manifest: Project manifest naming the root, base and participants.

    Returns:
        One assessment per participant, in manifest order.
    """
    from agent_parley import supervision

    home = directory.parent.parent
    registered = worktrees(manifest["root"])
    ledger = issues.snapshot(directory)
    owned = issues.holders(ledger)
    after = supervision.configuration(home, manifest)["inactive_after"]
    reservations: dict[str, list[str]] | None
    try:
        reservations = store.active_reservations(home, manifest["root"])
    except (BridgeError, OSError, sqlite3.Error):
        reservations = None
    rows = []
    for name, participant in manifest["participants"].items():
        held = (
            UNREADABLE
            if reservations is None
            else _holds(ledger, reservations, name, participant["display"])
        )
        rows.append(
            assess(
                directory,
                manifest,
                name,
                registered=registered,
                claimed=bool(owned.get(name)),
                busy=_busy(directory, name),
                held=held,
                quiet=_quiet(directory, name, Path(participant["lane"]), after),
            )
        )
    return rows


def _entries(root: str) -> list[dict] | None:
    """Parses every worktree the base checkout registers, with its state.

    Args:
        root: Canonical project key, which is the base checkout's path.

    Returns:
        One entry per worktree naming its resolved path, its checked-out
        branch or an empty string when detached, its head commit and
        whether Git holds a lock on it, or None when Git could not answer.
    """
    listing = _read(root, "worktree", "list", "--porcelain")
    if listing is None:
        return None
    found: list[dict] = []
    for line in listing.splitlines():
        key, _, value = line.partition(" ")
        if key == "worktree":
            found.append(
                {
                    "path": str(Path(value).resolve()),
                    "branch": "",
                    "head": "",
                    "locked": False,
                }
            )
        elif found and key == "branch":
            found[-1]["branch"] = value.removeprefix("refs/heads/")
        elif found and key == "HEAD":
            found[-1]["head"] = value
        elif found and key == "locked":
            found[-1]["locked"] = True
    return found


def size(path: Path) -> int:
    """Totals the bytes of every regular file under a directory.

    Args:
        path: Directory measured; symbolic links are counted, not followed.

    Returns:
        The total in bytes, counting only what could be read.
    """
    total = 0
    for folder, _, files in os.walk(path, onerror=lambda error: None):
        for file in files:
            try:
                total += os.lstat(os.path.join(folder, file)).st_size
            except OSError:
                continue
    return total


def _within(path: Path, parent: Path) -> bool:
    """Reports whether a path lies inside, or is, another directory."""
    return path == parent or parent in path.parents


def _stray(
    root: str,
    directory: Path,
    entry: dict,
    *,
    busy: bool,
    after: float,
) -> tuple[str, list[str]]:
    """Decides whether one worktree a lane made may be removed, and why.

    Args:
        root: Base checkout the worktree is registered with.
        directory: Private project state directory.
        entry: Parsed worktree entry.
        busy: Whether a session runs in that owning lane.
        after: Inactivity threshold in seconds.

    Returns:
        The condition that decided it and any paths or commits it names.
        Only a condition in `REMOVABLE` allows a removal.
    """
    path = Path(entry["path"])
    if not _within(path, directory.resolve()):
        return OUTSIDE, []
    if entry["locked"]:
        return LOCKED, []
    if not path.exists():
        return MISSING, []
    if busy:
        return SESSION, []
    changed = _uncommitted(str(path))
    if changed is None:
        return UNREADABLE, []
    if changed:
        return UNCOMMITTED, changed
    head = entry["head"]
    if not head:
        return UNREADABLE, []
    ahead = _ahead(root, "HEAD", head)
    if ahead is None:
        return UNREADABLE, []
    if ahead:
        return DETACHED, ahead
    tracking = _upstream(root, entry["branch"]) if entry["branch"] else None
    if tracking is not None:
        remote, branch = tracking
        unpushed = _ahead(root, f"refs/remotes/{remote}/{branch}", head)
        if unpushed:
            return UNPUSHED, unpushed
    if time.time() - _changed(path) <= after:
        return RECENT, []
    return CONTAINED, []


def strays(directory: Path, manifest: dict, *, sizes: bool) -> list[dict]:
    """Assesses the worktrees lanes made beside their own lane worktrees.

    Every worktree the project repository registers is read, except the
    base checkout and the participants' own lanes. Each is attributed to
    the lane whose worktree contains it, and removed only when it sits
    inside the project state directory, Git holds no lock on it, no session
    runs in its lane, it is clean, the base checkout carries every commit it
    has, its upstream carries every commit its branch has, and it has not
    changed within the inactivity threshold. A registration whose directory
    is already gone is reclaimed by pruning it.

    Args:
        directory: Private project state directory holding the lanes.
        manifest: Project manifest naming the root and participants.
        sizes: Whether each worktree's size on disk is measured, which
            walks every file and is left to an operator's report.

    Returns:
        One row per worktree naming its path, its branch, the lane it was
        attributed to, whether it may be reclaimed, the condition that
        decided it, any paths or commits that condition names, and its
        size in bytes when measured.
    """
    from agent_parley import supervision

    entries = _entries(manifest["root"])
    if entries is None:
        return []
    after = supervision.configuration(directory.parent.parent, manifest)[
        "inactive_after"
    ]
    base = str(Path(manifest["root"]).resolve())
    lanes = {
        str(Path(participant["lane"]).resolve()): name
        for name, participant in manifest["participants"].items()
    }
    rows = []
    for entry in entries:
        if entry["path"] == base or entry["path"] in lanes:
            continue
        path = Path(entry["path"])
        owner = next(
            (name for lane, name in lanes.items() if _within(path, Path(lane))),
            "",
        )
        busy = bool(owner) and _busy(directory, owner)
        reason, paths = _stray(
            manifest["root"],
            directory,
            entry,
            busy=busy,
            after=after,
        )
        row = {
            "worktree": entry["path"],
            "branch": entry["branch"],
            "participant": owner,
            **_outcome(reason, paths),
        }
        if sizes and path.exists():
            row["bytes"] = size(path)
        rows.append(row)
    return rows


def remove(root: str, row: dict) -> dict:
    """Removes one reclaimable worktree through Git's own refusals.

    `git worktree remove` without force refuses a worktree that is dirty or
    locked, so a change made between the assessment and the removal keeps
    the worktree. A registration whose directory is gone is pruned instead.
    Branches are left in place.

    Args:
        root: Base checkout the worktree is registered with.
        row: One assessment `strays` decided in favour of a reclaim.

    Returns:
        The row with whether Git removed it.
    """
    if row["reason"] == MISSING:
        removed = _read(root, "worktree", "prune") is not None
    else:
        removed = _read(root, "worktree", "remove", row["worktree"]) is not None
    return {**row, "removed": removed}


def reclaimable(rows: list[dict]) -> list[dict]:
    """Returns only the assessments that decided in favour of a reclaim."""
    return [row for row in rows if row["reclaim"]]


def lines(rows: list[dict]) -> list[str]:
    """Renders a sweep's assessments as one readable line each.

    Args:
        rows: Assessments, each already carrying its decision.

    Returns:
        A line per lane or worktree naming what happened or what held it,
        with the paths a refusal reported and the size when it was measured.
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
        if "bytes" in row:
            detail += f"; {row['bytes'] / 1_000_000:.1f} MB"
        label = row["participant"]
        if "worktree" in row:
            label = f"worktree {row['worktree']}"
            if row["participant"]:
                label += f" of {row['participant']}"
        rendered.append(f"{label}: {verb} ({row['reason']}){detail}")
    return rendered
