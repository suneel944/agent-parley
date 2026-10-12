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
`git worktree list` and attributed to a lane by path, when they sit inside a
lane or its `<lane>-wt` directory, or directly inside the project state
directory or a project directory of the state root the product used before
its rename, or by branch, when
their branch is named after a lane. A worktree no lane accounts for, even
one nested deeper under the state directory, is the operator's and is
never touched. An
attributed one is removed when it is clean, holds no file Git ignores,
carries no commit that neither the base checkout nor its upstream has, and
either the base already holds its head, its lane retired, or it has been
untouched past the inactivity threshold. Uncommitted changes and unpushed
commits keep it unless the operator forces the removal, and a forced
removal first writes a recovery checkpoint holding both; an ignored file
keeps it regardless, because no force can recover what was never tracked.
Regenerable tool output alone, such as a virtual environment, bytecode and
tool caches or package build metadata, keeps nothing: a rerun recreates it.

A lane-made worktree outside its lane's `<lane>-wt` directory is first moved
there with `git worktree move`, which never deletes, so the sweep can read it
by path. A worktree moved underneath the product, by that sweep or by the
operator, is found again by branch, so a move never reads as a loss.
"""

import os
import shlex
import sqlite3
import subprocess
import time
from pathlib import Path, PurePosixPath

from agent_parley import forge, issues, pool, retirement, store
from agent_parley.state import BridgeError, LockBusy, lock

GIT_SECONDS = 5
MAX_REPORTED_PATHS = 10
RENAMED_HOMES = {"agent-parley": "agent-bridge"}
RETIRED_FILES = ("-work.json", "-wake-work.json", "-mcp.json", "-wake.log")
WAKE_LOG_RETENTION = 2 * 24 * 3600

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
ABANDONED = "its lane retired and its commits are all on its upstream"
STALE = (
    "it has been untouched past the inactivity threshold and its commits "
    "are all on its upstream"
)
FOREIGN = "no lane made it"
GONE = "its branch is gone from its upstream"
OUTSIDE = "its worktree is outside the project state directory"
UNREGISTERED = "the base checkout does not register its worktree"
UNREADABLE = "Git could not inspect it"
SESSION = "a session is running in it"
IDLE = "an idle session is still running in it"
CLAIMED = "it still holds a claim"
UNCOMMITTED = "it holds uncommitted changes"
IGNORED = "it holds files Git does not track elsewhere"
UNMERGED = "its branch holds commits the base checkout does not have"
UNUSED = "its branch never left the commit its lane was created from"
UNPUSHED = "its branch holds commits its upstream does not have"
ABSORBED = (
    "its unpushed commits already landed in the base checkout through "
    "another branch"
)
PULL_OPEN = "its pull request is still open"
PUBLISHED = "its upstream still carries its branch"
PULL_CLOSED = "its pull request was closed without merging"
UNLANDED = "nothing shows its branch landed"
MOVED = "its worktree moved to another path Git registers"
STRAYED = "it sits outside its lane and the lane's -wt directory"
REFUSED = "Git refused to move it"
EXTRAS = "-wt"
REMOVABLE = frozenset(
    {
        MERGED,
        GONE,
        STOPPED,
        VANISHED,
        MISSING,
        CONTAINED,
        ABANDONED,
        STALE,
        ABSORBED,
    }
)
REGENERABLE = frozenset(
    {
        ".venv",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
    }
)
BUNDLED = frozenset({ABSORBED})
FORCEABLE = frozenset({UNCOMMITTED, UNPUSHED, RECENT})


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


def _occupied(path: Path) -> bool:
    """Reports whether any process works inside a worktree.

    Moving a directory out from under a process strands its shell or
    session, whoever started it, so every process whose working directory
    is the worktree or lies inside it counts. Linux exposes that through
    `/proc`; a host without it, such as macOS, reports none, and only the
    owning lane's session lock guards the move there.

    Args:
        path: Worktree about to be moved.

    Returns:
        Whether a readable process has its working directory inside it.
    """
    proc = Path("/proc")
    if not proc.is_dir():
        return False
    target = path.resolve()
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if Path(os.readlink(entry / "cwd")).is_relative_to(target):
                return True
        except OSError:
            continue
    return False


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
    idle: bool = False,
) -> dict:
    """Decides what may be done with one lane, and why.

    The order of the conditions is the order of their cost and their
    severity. Containment comes first, because a path the project does not
    own is never touched whatever its contents say. A lane whose worktree
    is gone is decided next, on held work alone, because Git has nothing
    left to read in it; but one whose branch Git registers at another
    existing path was moved, not lost, and is kept with the command that
    moves it back. Registration follows, then held work, then
    uncommitted files, then commits the base checkout or the upstream does
    not carry, then a branch that never left the commit its lane was created
    from, which is reclaimed only once the lane has stopped and stayed
    quiet, and only a lane that survived all of them is measured against
    the forge. The base checkout is read at its own head, which is the
    branch the project's own integration merges a lane into.

    A session that is alive but idle past the project's inactivity window
    does not count as running: the lane is judged on everything else, so
    the report names the condition that actually holds it. A worktree is
    still never removed from under a live process, so a lane that would
    otherwise be reclaimed is kept with that reason, and every such row
    names the command that ends the idle session.

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
        idle: Whether that session has been idle past the inactivity window.

    Returns:
        The participant, its lane, its branch, whether the lane may be
        reclaimed, the condition that decided it and any paths that
        condition names. A lane with an idle session also carries the
        remedy that ends it.
    """
    if busy and idle:
        row = assess(
            directory,
            manifest,
            name,
            registered=registered,
            claimed=claimed,
            busy=False,
            held=held,
            quiet=quiet,
        )
        if row["reclaim"]:
            row.update(_outcome(IDLE))
        row["remedy"] = (
            "its session is idle but alive; `agent-parley participant "
            f"stop {name}` ends it"
        )
        return row
    participant = manifest["participants"][name]
    lane = Path(participant["lane"])
    branch = participant["branch"]
    root = manifest["root"]
    row = {"participant": name, "lane": str(lane), "branch": branch}
    if lane.parent.resolve() != directory.resolve():
        return {**row, **_outcome(OUTSIDE)}
    blocker = SESSION if busy else CLAIMED if claimed else held
    if not lane.exists():
        if moved := relocated(root, lane, branch):
            return {
                **row,
                **_outcome(MOVED, [moved]),
                "remedy": moved_remedy(lane, moved),
            }
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


def plan(
    directory: Path, manifest: dict, idle: frozenset[str] = frozenset()
) -> list[dict]:
    """Assesses every lane a project registers.

    Args:
        directory: Private project state directory holding the lanes.
        manifest: Project manifest naming the root, base and participants.
        idle: Participants whose session is alive but idle past the
            project's inactivity window.

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
                idle=name in idle,
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


def extras(lane: Path | str) -> Path:
    """Names the one directory a lane keeps its extra worktrees in.

    Args:
        lane: The lane's own worktree.

    Returns:
        The `<lane>-wt` directory beside the lane, in the same parent.
    """
    return Path(f"{lane}{EXTRAS}")


def relocated(root: str, lane: Path, branch: str) -> str:
    """Finds where Git now registers a worktree whose recorded path is gone.

    A worktree moved with `git worktree move`, by the sweep or by the
    operator, keeps its branch and its registration under a new path, so a
    recorded path that no longer exists is read against the registrations
    by branch before anything treats it as gone.

    Args:
        root: Base checkout the worktree is registered with.
        lane: Recorded worktree path.
        branch: The branch that worktree has checked out.

    Returns:
        The existing path Git registers that branch at, or an empty string
        when the recorded path still exists, the branch is empty, or no
        other existing registration carries it.
    """
    if not branch or lane.exists():
        return ""
    for entry in _entries(root) or []:
        if entry["branch"] == branch and Path(entry["path"]).exists():
            return entry["path"]
    return ""


def moved_remedy(lane: Path, moved: str) -> str:
    """Names the command that returns a moved lane worktree to its place.

    Args:
        lane: The lane's recorded worktree path.
        moved: The path Git registers that worktree at now.

    Returns:
        A sentence naming the move and the `git worktree move` reversing it.
    """
    back = shlex.join(["git", "worktree", "move", moved, str(lane)])
    return (
        f"its worktree now sits at {moved}; Agent Parley keeps the lane, "
        f"and `{back}` returns it"
    )


def size(path: Path) -> int:
    """Totals the bytes of every file under a directory, once per inode.

    Worktrees and clones on one filesystem share Git objects through hard
    links, so a file reached under several names is counted the first time
    only. The total is then the space the files take, as `du -sb` reports
    it, rather than the sum of every name.

    Args:
        path: Directory measured; symbolic links are counted, not followed.

    Returns:
        The total in bytes, counting only what could be read.
    """
    total = 0
    seen: set[tuple[int, int]] = set()
    for folder, _, files in os.walk(path, onerror=lambda error: None):
        for file in files:
            try:
                found = os.lstat(os.path.join(folder, file))
            except OSError:
                continue
            if found.st_nlink > 1:
                inode = (found.st_dev, found.st_ino)
                if inode in seen:
                    continue
                seen.add(inode)
            total += found.st_size
    return total


def _within(path: Path, parent: Path) -> bool:
    """Reports whether a path lies inside, or is, another directory.

    Both paths are already resolved, so a string prefix answers without
    building the parent chain of every registered worktree for every lane.
    """
    inside = str(path)
    outside = str(parent)
    return inside == outside or inside.startswith(outside + os.sep)


def owned_roots(directory: Path) -> list[Path]:
    """Names the directories whose direct children the project itself made.

    The product places every lane and every worktree a lane adds for a
    sub-task directly inside a project state directory, so only a direct
    child is the project's. A worktree nested deeper, such as one an
    operator adds under a directory of their own beside the lanes, is
    theirs. The project state directory is one such directory. The
    project directories under the state root the product used before its
    rename are the others: lanes created then were registered with the
    same repository from beneath them, and no lane of the current
    manifest accounts for them any longer. They are only claimed when the
    current state root carries the product's own default name, so a
    relocated state root never reaches into a sibling the operator owns.

    Args:
        directory: Private project state directory.

    Returns:
        Resolved directories, the project state directory first.
    """
    home = directory.parent.parent
    found = [directory.resolve()]
    if renamed := RENAMED_HOMES.get(home.name):
        legacy = (home.parent / renamed / "projects").resolve()
        found.extend(sorted(path for path in legacy.glob("*") if path.is_dir()))
    return found


def _unpushed(root: str, entry: dict, ahead: list[str]) -> list[str] | None:
    """Returns the commits beyond the base that no upstream carries either.

    Args:
        root: Base checkout the worktree is registered with.
        entry: Parsed worktree entry.
        ahead: Commits the worktree's head has and the base checkout lacks.

    Returns:
        The commits only this worktree holds, or None when Git could not
        answer. Without an upstream every commit beyond the base counts.
    """
    tracking = _upstream(root, entry["branch"]) if entry["branch"] else None
    if tracking is None:
        return ahead
    remote, branch = tracking
    reference = f"refs/remotes/{remote}/{branch}"
    if _read(root, "rev-parse", "--verify", "--quiet", reference) is None:
        return ahead
    return _ahead(root, reference, entry["head"])


def _absorbed(root: str, head: str) -> bool:
    """Reports whether the base checkout already holds a head's changes.

    A branch squash-merged or cherry-picked through another branch keeps
    commits no ref of the base carries, yet merging it into the base
    changes nothing. Git's own merge is the test: the head is absorbed
    only when the merge applies cleanly and leaves the base's tree exactly
    as it is. A conflict, an older Git without `merge-tree --write-tree`
    and any other failure are all no.

    Args:
        root: Base checkout whose head is merged into.
        head: Commit whose changes are looked for.

    Returns:
        True only when the merge result is the base head's own tree.
    """
    merged = _read(root, "merge-tree", "--write-tree", "HEAD", head)
    if not merged:
        return False
    base = _read(root, "rev-parse", "HEAD^{tree}")
    return bool(base) and merged.splitlines()[0] == base


def _regenerable(path: str) -> bool:
    """Reports whether an ignored path is tool output a rerun recreates.

    Args:
        path: Worktree-relative path Git ignores.

    Returns:
        Whether any component of the path is a virtual environment, a
        Python bytecode or tool cache, or package build metadata. Every
        other ignored path, such as `.env` or a data directory, may hold
        content that exists nowhere else.
    """
    return any(
        part in REGENERABLE or part.endswith(".egg-info")
        for part in PurePosixPath(path).parts
    )


def _stray(
    root: str,
    entry: dict,
    *,
    busy: bool,
    retired: bool,
    after: float,
) -> tuple[str, list[str]]:
    """Decides whether one worktree a lane made may be removed, and why.

    Args:
        root: Base checkout the worktree is registered with.
        entry: Parsed worktree entry.
        busy: Whether a session runs in that owning lane.
        retired: Whether the owning lane retired.
        after: Inactivity threshold in seconds.

    Returns:
        The condition that decided it and any paths or commits it names.
        Only a condition in `REMOVABLE` allows a removal, and only one in
        `FORCEABLE` allows a forced one. A worktree holding files Git
        ignores is always kept: `git worktree remove` would delete them
        with the tree, and no force writes a checkpoint of untracked
        content, so a forced removal could not protect them either. Only
        regenerable tool output, as `_regenerable` decides, is exempt.
    """
    path = Path(entry["path"])
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
    ignored = retirement.ignored_files(str(path))
    if ignored is None:
        return UNREADABLE, []
    kept = [name for name in ignored if not _regenerable(name)]
    if kept:
        return IGNORED, kept
    head = entry["head"]
    if not head:
        return UNREADABLE, []
    ahead = _ahead(root, "HEAD", head)
    if ahead is None:
        return UNREADABLE, []
    if ahead:
        unpushed = _unpushed(root, entry, ahead)
        if unpushed is None:
            return UNREADABLE, []
        if unpushed:
            quiet = retired or time.time() - _changed(path) > after
            if quiet and _absorbed(root, head):
                return ABSORBED, unpushed
            return UNPUSHED, unpushed
    if retired:
        return ABANDONED, []
    if time.time() - _changed(path) <= after:
        return RECENT, []
    return (STALE if ahead else CONTAINED), []


def _owner(
    path: Path,
    branch: str,
    lanes: dict[str, str],
    prefixes: list[tuple[str, str]],
) -> str:
    """Attributes one worktree to the lane that made it.

    Args:
        path: Resolved worktree path.
        branch: Its checked-out branch, empty when detached.
        lanes: Resolved lane worktree path to participant name.
        prefixes: Branch prefix and the participant it names, longest
            prefix first; each lane contributes its name and its branch.

    Returns:
        The lane whose worktree, or whose `<lane>-wt` directory, contains
        it, else the lane its branch is named after, else an empty string.
    """
    for lane, name in lanes.items():
        if _within(path, Path(lane)) or _within(path, extras(lane)):
            return name
    for prefix, name in prefixes:
        if branch == prefix or branch.startswith((f"{prefix}-", f"{prefix}/")):
            return name
    return ""


def _owner_index(
    manifest: dict,
) -> tuple[dict[str, str], list[tuple[str, str]]]:
    """Builds the lookups `_owner` needs from a project manifest.

    Args:
        manifest: Project manifest naming the root and participants.

    Returns:
        Resolved lane worktree path to participant name, and branch prefix
        to participant name, longest prefix first.
    """
    participants = manifest["participants"]
    lanes = {
        str(Path(participant["lane"]).resolve()): name
        for name, participant in participants.items()
    }
    prefixes = sorted(
        {
            (prefix, name)
            for name, participant in participants.items()
            for prefix in (name, participant.get("branch") or name)
        },
        key=lambda pair: len(pair[0]),
        reverse=True,
    )
    return lanes, prefixes


def child_worktrees(directory: Path, manifest: dict, name: str) -> list[Path]:
    """Lists the worktrees one lane made beyond its own, under the project.

    A lane can run a project's own tooling from its shell and have that
    tooling add further Git worktrees, for a pull request or a sub-task,
    directly inside the project state directory beside every lane. Git
    registers each one against the project repository the same way it
    registers a lane, so the same attribution `strays` uses names which
    lane made it: by path, when the worktree sits inside that lane or its
    `<lane>-wt` directory, or by
    branch, when its branch is named after that lane. Only worktrees Git
    already registers are read; nothing here scans the filesystem.

    Args:
        directory: Private project state directory holding the lanes.
        manifest: Project manifest naming the root and participants.
        name: Participant whose own worktrees are listed.

    Returns:
        Resolved paths Git registers and attributes to this lane, its own
        lane worktree excluded, empty when Git could not be read.
    """
    return children_by_lane(manifest).get(name, [])


def children_by_lane(manifest: dict) -> dict[str, list[Path]]:
    """Attributes every registered worktree beyond the lanes in one read.

    `child_worktrees` answers for one lane; a surface that asks for every
    lane of a project, as `problems` does, would otherwise list the
    repository's worktrees and attribute each of them once per lane, and
    a project with hundreds of registrations made that the whole cost of
    the command. This lists and attributes them once.

    Args:
        manifest: Project manifest naming the root and participants.

    Returns:
        Participant name to the resolved worktree paths attributed to it,
        the base checkout and every lane's own worktree excluded; empty when
        Git could not be read.
    """
    entries = _entries(manifest["root"])
    if entries is None:
        return {}
    lanes, prefixes = _owner_index(manifest)
    excluded = {str(Path(manifest["root"]).resolve()), *lanes}
    found: dict[str, list[Path]] = {}
    for entry in entries:
        if entry["path"] in excluded:
            continue
        path = Path(entry["path"])
        if owner := _owner(path, entry["branch"], lanes, prefixes):
            found.setdefault(owner, []).append(path)
    return found


def _lane_made(
    directory: Path, manifest: dict, entries: list[dict]
) -> list[dict]:
    """Drops the registrations no lane made beside its own worktree.

    Args:
        directory: Private project state directory holding the spares.
        manifest: Project manifest naming the root and participants.
        entries: Parsed registrations of the project repository.

    Returns:
        The entries left once the base checkout, every lane's own worktree,
        the recorded spares and any worktree on the branch of a lane that
        has not retired are dropped. The last is a lane worktree an
        operator moved, which is the lane itself and never a stray.
    """
    lanes, _ = _owner_index(manifest)
    skipped = {
        str(Path(manifest["root"]).resolve()),
        *lanes,
        *(
            str(Path(spare["path"]).resolve())
            for spare in pool.spares(directory)
        ),
    }
    branches = {
        participant["branch"]
        for participant in manifest["participants"].values()
        if not participant.get("retired")
    }
    return [
        entry
        for entry in entries
        if entry["path"] not in skipped and entry["branch"] not in branches
    ]


def strays(directory: Path, manifest: dict, *, sizes: bool) -> list[dict]:
    """Assesses the worktrees lanes made beside their own lane worktrees.

    Every worktree the project repository registers is read, except the
    base checkout, the participants' own lanes, wherever an operator moved
    them, and the recorded spares,
    which `pool.sweep` assesses by their own rule. Each is attributed to a
    lane by path or branch; one directly inside the project state
    directory, or directly inside a project directory under the state root
    the product used before its rename, belongs to the project even when
    no single lane accounts for it, and one nothing
    accounts for is the operator's and is never removed. An
    attributed worktree is removed only when Git holds no lock on it, no
    session runs in its lane, it is clean, it holds no file Git ignores,
    every commit beyond the base is on its upstream, and the base holds its
    head, its lane retired, or it has not changed within the inactivity
    threshold. A registration whose directory is already gone is reclaimed
    by dropping that registration.

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
    participants = manifest["participants"]
    lanes, prefixes = _owner_index(manifest)
    owned = owned_roots(directory)
    rows = []
    for entry in _lane_made(directory, manifest, entries):
        path = Path(entry["path"])
        owner = _owner(path, entry["branch"], lanes, prefixes)
        paths: list[str]
        if not owner and path.parent not in owned:
            reason, paths = FOREIGN, []
        else:
            reason, paths = _stray(
                manifest["root"],
                entry,
                busy=bool(owner) and _busy(directory, owner),
                retired=bool(owner)
                and bool(participants[owner].get("retired")),
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


def _free(folder: Path, name: str, taken: set[Path]) -> Path:
    """Picks a destination inside a folder that nothing occupies yet.

    Args:
        folder: Directory the destination is placed in.
        name: Preferred final path component.
        taken: Destinations already planned in this pass.

    Returns:
        `folder/name`, or the first `folder/name-N` from 2 upward that
        neither exists nor is already planned.
    """
    candidate = folder / name
    number = 2
    while candidate in taken or candidate.exists() or candidate.is_symlink():
        candidate = folder / f"{name}-{number}"
        number += 1
    return candidate


def relocations(directory: Path, manifest: dict) -> list[dict]:
    """Plans moving every lane-made worktree outside its lane's place.

    Every worktree a lane makes belongs in that lane's `<lane>-wt`
    directory, where `strays` attributes it by path and can reclaim it once
    its work lands. One a lane made elsewhere, in the operator's home, beside
    the repository or in a temporary directory, is attributed by its branch
    and planned into `<lane>-wt/<name>`, with a numeric suffix when that
    name is taken. A move never deletes anything: Git renames the directory,
    so uncommitted edits, unpushed commits and ignored files travel with it.
    The base checkout, the lanes themselves, spares, a worktree already
    inside its lane or its `-wt` directory, one whose directory is gone and
    one no lane accounts for are not planned at all. A locked worktree, one
    whose lane runs a session and one any process works inside are planned
    but refused.

    Args:
        directory: Private project state directory holding the lanes.
        manifest: Project manifest naming the root and participants.

    Returns:
        One row per worktree naming its path, its branch, its lane, the
        destination, whether it may be moved and the condition that
        decided it; empty when Git could not list the worktrees.
    """
    entries = _entries(manifest["root"])
    if entries is None:
        return []
    lanes, prefixes = _owner_index(manifest)
    homes = {name: Path(lane) for lane, name in lanes.items()}
    taken: set[Path] = set()
    rows = []
    for entry in _lane_made(directory, manifest, entries):
        path = Path(entry["path"])
        owner = _owner(path, entry["branch"], lanes, prefixes)
        if not owner or not path.exists():
            continue
        lane = homes[owner]
        if _within(path, lane) or _within(path, extras(lane)):
            continue
        destination = _free(extras(lane), path.name, taken)
        taken.add(destination)
        if entry["locked"]:
            reason = LOCKED
        elif _busy(directory, owner) or _occupied(path):
            reason = SESSION
        elif _within(destination, path):
            reason = REFUSED
        else:
            reason = STRAYED
        rows.append(
            {
                "worktree": entry["path"],
                "branch": entry["branch"],
                "participant": owner,
                "destination": str(destination),
                "relocate": reason == STRAYED,
                "reason": reason,
            }
        )
    return rows


def relocate(root: str, row: dict) -> dict:
    """Moves one planned worktree through Git's own refusals.

    `git worktree move` without force refuses a locked worktree, a
    destination that exists and a move across filesystems, so a change
    made between the plan and the move leaves the worktree in place.

    Args:
        root: Base checkout the worktree is registered with.
        row: One plan `relocations` produced.

    Returns:
        The row with whether Git moved it. A move Git refused, or a
        destination directory that could not be created, is reported as
        `REFUSED` and moves nothing.
    """
    if not row["relocate"]:
        return row
    destination = Path(row["destination"])
    refused = {**row, "relocate": False, "reason": REFUSED, "moved": False}
    if destination.exists() or destination.is_symlink():
        return refused
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return refused
    moved = _read(root, "worktree", "move", row["worktree"], str(destination))
    if moved is None:
        return refused
    return {**row, "moved": True}


def prune(directory: Path, manifest: dict, now: float) -> list[str]:
    """Deletes lane files nothing reads any more, and old wake logs.

    A retired lane is never offered work, woken or launched until an
    operator re-admits it, and a re-admitted lane writes these files anew,
    so its published work, its wake prompt, its client configuration, which
    carries a registration token, and its wake log only take space. The
    lane's identity, activity, events, reports and capacity stay: they are
    the history an archive exports, and capacity is read for every lane
    sharing the same account. Lock files stay too, because unlinking a lock
    another process holds would let a third take it. A lane whose session
    still holds its session lock keeps everything.

    A wake log records the relaunches the supervisor spawned and is only
    ever appended to, so one untouched for `WAKE_LOG_RETENTION` seconds is
    deleted whoever wrote it, including lanes no longer in the manifest.

    Args:
        directory: Private project state directory.
        manifest: Project manifest naming the participants.
        now: Unix time the wake log retention is measured against.

    Returns:
        The names of the files deleted, sorted.
    """
    doomed: set[Path] = set()
    for name, participant in manifest["participants"].items():
        if participant.get("retired") and not _busy(directory, name):
            doomed.update(directory / f"{name}{end}" for end in RETIRED_FILES)
    for log in directory.glob("*-wake.log"):
        if now - _changed(log) > WAKE_LOG_RETENTION:
            doomed.add(log)
    removed = []
    for path in sorted(doomed):
        try:
            path.unlink()
        except OSError:
            continue
        removed.append(path.name)
    return removed


def forceable(row: dict) -> bool:
    """Reports whether a forced sweep may remove one kept worktree."""
    return "worktree" in row and row["reason"] in FORCEABLE


def remove(
    root: str, directory: Path, row: dict, *, force: bool = False
) -> dict:
    """Removes one worktree through Git's own refusals.

    `git worktree remove` without force refuses a worktree that is dirty or
    locked, so a change made between the assessment and the removal keeps
    the worktree. A registration whose directory is gone is dropped by the
    same command, which touches no other registration.
    A forced removal of a worktree kept for its uncommitted changes, its
    unpushed commits or a recent change first writes a recovery checkpoint
    holding all of them, and removes nothing when that checkpoint fails.
    A worktree whose unpushed commits already landed through another
    branch is bundled the same way before its ordinary removal, so the
    commits no ref of the base carries survive it too. Branches are left
    in place.

    Args:
        root: Base checkout the worktree is registered with.
        directory: Private project state directory holding checkpoints.
        row: One assessment `strays` produced.
        force: Whether a worktree in `FORCEABLE` is removed as well.

    Returns:
        The row with whether Git removed it, and the checkpoint written
        before a forced or bundled removal.
    """
    from agent_parley import recovery

    if row["reclaim"] and row["reason"] in BUNDLED:
        try:
            saved = recovery.preserve(directory, Path(row["worktree"]))
        except (BridgeError, OSError):
            return {**row, "removed": False}
        removed = _read(root, "worktree", "remove", row["worktree"])
        gone = removed is not None
        return {**row, "removed": gone, "checkpoint": saved["id"]}
    if row["reclaim"]:
        removed = _read(root, "worktree", "remove", row["worktree"])
        return {**row, "removed": removed is not None}
    if not (force and forceable(row)):
        return row
    try:
        saved = recovery.preserve(directory, Path(row["worktree"]))
    except (BridgeError, OSError):
        return {**row, "removed": False}
    removed = _read(root, "worktree", "remove", "--force", row["worktree"])
    return {**row, "removed": removed is not None, "checkpoint": saved["id"]}


def reclaimable(rows: list[dict]) -> list[dict]:
    """Returns only the assessments that decided in favour of a reclaim."""
    return [row for row in rows if row["reclaim"]]


def summary_line(summary: dict) -> str:
    """Renders what the last sweep measured for one project.

    Args:
        summary: The state directory size and worktree counts the last
            sweep published.

    Returns:
        One line naming the state directory's size and the worktrees a
        reclaim would remove, or an empty string before the first sweep.
    """
    if summary.get("state_bytes") is None:
        return ""
    line = (
        f"State directory: {summary['state_bytes'] / 1_000_000:.1f} MB; "
        f"{summary.get('reclaimable') or 0} worktrees to reclaim"
    )
    if forced := summary.get("forceable"):
        line += f", {forced} more with --force"
    return line


def lines(rows: list[dict]) -> list[str]:
    """Renders a sweep's assessments as one readable line each.

    Args:
        rows: Assessments, each already carrying its decision.

    Returns:
        A line per lane or worktree naming what happened or what held it,
        with the paths a refusal reported and the size when it was measured,
        and a line per planned move naming its destination or what held it.
    """
    rendered = []
    for row in rows:
        if "destination" in row:
            if row.get("moved"):
                verb = f"moved to {row['destination']}"
            elif row["relocate"]:
                verb = f"to move to {row['destination']}"
            else:
                verb = "left in place"
            rendered.append(
                f"worktree {row['worktree']} of {row['participant']}: "
                f"{verb} ({row['reason']})"
            )
            continue
        if row.get("removed"):
            verb = "reclaimed"
        elif row["reclaim"]:
            verb = "reclaimable"
        else:
            verb = "kept"
        detail = f"; {', '.join(row['paths'])}" if row["paths"] else ""
        if "bytes" in row:
            detail += f"; {row['bytes'] / 1_000_000:.1f} MB"
        if "checkpoint" in row:
            bundled = row["reason"] in BUNDLED
            action = "bundled to" if bundled else "forced after"
            detail += f"; {action} checkpoint {row['checkpoint']}"
        if row.get("remedy"):
            detail += f"; {row['remedy']}"
        label = row["participant"]
        if "worktree" in row:
            label = f"worktree {row['worktree']}"
            if row["participant"]:
                label += f" of {row['participant']}"
        rendered.append(f"{label}: {verb} ({row['reason']}){detail}")
    return rendered
