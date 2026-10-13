"""Runs Git for the base checkout and prepares the lanes created from it."""

from __future__ import annotations

import os
import shlex
import subprocess
import time
from pathlib import Path

from agent_parley.state import BridgeError

VERIFY_TIMEOUT = 1800
INIT_OUTPUT_LINES = 20
GIT_SECONDS = 30


def git(repo: Path, *args: str) -> str:
    """Runs Git in a repository and returns stripped stdout.

    Args:
        repo: Working directory for Git.
        *args: Individual Git arguments, never shell-expanded.

    Returns:
        Command output with surrounding whitespace removed.

    Raises:
        BridgeError: If Git exits unsuccessfully.
        subprocess.TimeoutExpired: If Git exceeds the command timeout.
    """
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=(
            None
            if args and args[0] in {"push", "pull", "fetch"}
            else GIT_SECONDS
        ),
        check=False,
    )
    if result.returncode:
        raise BridgeError(result.stderr.strip() or "Git command failed.")
    return result.stdout.strip()


def has_branch(repo: Path, branch: str) -> bool:
    """Reports whether a branch still exists in a repository."""
    return bool(
        git(
            repo,
            "for-each-ref",
            "--format=%(refname:short)",
            f"refs/heads/{branch}",
        )
    )


def preserve_pending(root: Path) -> str | None:
    """Stashes pending base-checkout work so lanes can start from HEAD.

    Registration reads committed HEAD, so pending changes would otherwise
    never reach a lane. The changes are stashed rather than discarded. The
    stash stack is shared by every worktree of the repository, so the entry
    carries a unique message and the returned account restores it by name
    rather than by position. The name is the full object name: Git reads a
    bare decimal argument to ``git stash apply`` as a reflog position, so an
    abbreviation made only of digits would restore some other entry or fail
    outright.

    Args:
        root: Common repository root, which is always the base checkout.

    Returns:
        An account of the preserved entry, or None if nothing was pending.

    Raises:
        BridgeError: If Git leaves changes in the checkout after stashing.
        subprocess.TimeoutExpired: If Git exceeds the command timeout.
    """
    if not git(root, "status", "--porcelain"):
        return None
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    git(
        root,
        "stash",
        "push",
        "--include-untracked",
        "--message",
        f"agent-parley pending work {stamp}",
    )
    if git(root, "status", "--porcelain"):
        raise BridgeError(
            f"The checkout at {root} still holds changes that Git cannot "
            "stash. Commit or preserve them first; worktrees start at HEAD.",
            next_command=f"git -C {shlex.quote(str(root))} status --short",
        )
    entry = git(root, "rev-parse", "refs/stash")
    return (
        f"Preserved your pending changes as stash entry {entry}; worktrees "
        f"start at HEAD. Restore them with `git -C "
        f"{shlex.quote(str(root))} stash apply {entry}`, which names this "
        "entry rather than whichever one is on top of the shared stack."
    )


def drift(name: str, participant: dict, actual: str) -> str:
    """Builds an actionable message for a lane that left its branch.

    Args:
        name: Participant that owns the lane.
        participant: Manifest entry holding the lane and assigned branch.
        actual: Branch the lane currently has.

    Returns:
        A message naming both branches and the repair commands.
    """
    return (
        f"{name} lane is on {actual!r}, expected "
        f"{participant['branch']!r}. Run "
        f"`agent-parley participant restore {name}` to return it, or "
        f"`agent-parley participant retire {name}` to drop the lane. "
        "Both preserve committed and uncommitted work; neither discards."
    )


def commit_all(lane: Path) -> str:
    """Builds the command that commits every pending change in a lane.

    A lane branch is private to its participant, so a work-in-progress
    commit there keeps the changes on the branch the lane already owns.
    Untracked files are staged too, because a commit of tracked files alone
    leaves the checkout dirty and the refusal would repeat.

    Args:
        lane: Worktree holding the pending changes.

    Returns:
        A shell command line that stages and commits everything pending.
    """
    quoted = shlex.quote(str(lane))
    return f"git -C {quoted} add -A && git -C {quoted} commit -m wip"


def verify_base(
    root: Path, command: list[str], integrated: bool = False
) -> None:
    """Runs a repository's verification command in the base checkout.

    Executing a configured command is a different trust decision from reading
    Git state, so the gate is a separate step that never rewrites, resets or
    stages anything itself. Run before a merge it reports the checkout as it
    stands, which is not a claim about the merged result; run after one it
    reports the integrated result itself. The command is run as an argument
    list without a shell, and no flag skips it: a repository that configures
    a gate always pays it.

    Args:
        root: Common repository root, which is always the base checkout.
        command: Argument tokens recorded in the project manifest.
        integrated: Whether the run follows a merge, which decides whether a
            failure reports that nothing was merged or that the merge stands
            and is unverified. Nothing is ever reset or reverted either way.

    Raises:
        BridgeError: If the command cannot run, if it exits non-zero, or if
            it exceeds its timeout. A timeout is a failed gate like any
            other, so every caller records it the same way.
    """
    quoted = shlex.join(command)
    outcome = (
        "The merge commits already recorded stand and are unverified; "
        "nothing was reset or reverted."
        if integrated
        else "Nothing was merged."
    )
    try:
        result = subprocess.run(
            command,
            cwd=root,
            text=True,
            timeout=VERIFY_TIMEOUT,
            check=False,
        )
    except OSError as exc:
        raise BridgeError(
            f"The verification command for the base checkout at {root} could "
            f"not run: {exc}. Correct it with `agent-parley verify set`, then "
            "rerun; merge never skips verification."
        ) from None
    except subprocess.TimeoutExpired:
        raise BridgeError(
            f"Verification timed out in the base checkout at {root}: "
            f"`{quoted}` ran past {VERIFY_TIMEOUT} seconds. Make it finish "
            "within the limit and rerun; merge never skips verification. "
            f"{outcome}"
        ) from None
    if not result.returncode:
        return
    raise BridgeError(
        f"Verification failed in the base checkout at {root}: `{quoted}` "
        f"exited {result.returncode}. Fix it and rerun; merge never skips "
        f"verification. {outcome} See the command output above."
    )


def preparation(worktree: float, init: float) -> dict:
    """Records how long one lane's preparation took before its launch.

    The record is kept on the lane's roster entry with ``start`` unset. The
    launch that follows completes it, which is how that launch tells a lane
    it prepared itself from one an earlier run left behind.

    Args:
        worktree: Seconds spent creating the worktree and its branch.
        init: Seconds spent running the project's ``init`` command.

    Returns:
        The partial launch timing, in seconds rounded to milliseconds.
    """
    return {
        "worktree": round(worktree, 3),
        "init": round(init, 3),
        "start": None,
    }


def launched(prepared: object, total: float) -> dict:
    """Completes a lane's launch timing at the moment its CLI starts.

    Args:
        prepared: Timing recorded on the roster entry, if any. Only one that
            no launch has completed yet counts toward this launch; a lane an
            earlier run prepared cost this run neither step.
        total: Seconds from the start of ``run`` to the native CLI's start.

    Returns:
        The worktree, ``init`` and CLI start shares, their total, and the
        wall-clock time the CLI started. The CLI start share is everything
        else ``run`` did first: registration, the service and the native
        configuration.
    """
    fresh = prepared if isinstance(prepared, dict) else {}
    if fresh.get("start") is not None:
        fresh = {}
    worktree = float(fresh.get("worktree") or 0.0)
    init = float(fresh.get("init") or 0.0)
    return {
        "worktree": worktree,
        "init": init,
        "start": round(max(total - worktree - init, 0.0), 3),
        "total": round(total, 3),
        "at": time.time(),
    }


def launch_line(timing: object) -> str:
    """Describes a lane's last launch timing for ``participant show``.

    Args:
        timing: Launch timing from the lane's roster entry, if any.

    Returns:
        One line splitting the time to CLI start, or an empty string when no
        launch has completed a timing yet.
    """
    if not isinstance(timing, dict) or timing.get("total") is None:
        return ""
    return (
        f"Last launch: {timing['total']:.2f}s to CLI start (worktree "
        f"{timing['worktree']:.2f}s, init {timing['init']:.2f}s, CLI start "
        f"{timing['start']:.2f}s)"
    )


def initialize_lane(lane: Path, command: list[str], base: Path) -> None:
    """Prepares a newly created lane before its native client starts.

    Every real repository needs more than a bare checkout before an agent can
    work in it: dependencies installed, an untracked environment file copied,
    a database migrated. Doing that once here costs the same setup once per
    lane instead of spending the first turns of every session on it, and makes
    every lane start from the same state.

    The command runs as an argument list without a shell, exactly as the
    verification gate does, and no flag skips it. It runs only when a lane is
    created, never on a resume. The base checkout is offered through
    AGENT_PARLEY_BASE so a command can copy a file Git does not track. A
    non-zero exit refuses the launch and leaves the worktree in place, because
    an operator needs to look at what the command did before it failed.

    Args:
        lane: Freshly created worktree the command runs in.
        command: Argument tokens recorded in the project manifest.
        base: Common repository root the lane was created from.

    Raises:
        BridgeError: If the command cannot run, or if it exits non-zero.
        subprocess.TimeoutExpired: If initialization exceeds its timeout.
    """
    quoted = shlex.join(command)
    try:
        result = subprocess.run(
            command,
            cwd=lane,
            env={**os.environ, "AGENT_PARLEY_BASE": str(base)},
            capture_output=True,
            text=True,
            timeout=VERIFY_TIMEOUT,
            check=False,
        )
    except OSError as exc:
        raise BridgeError(
            f"The lane initialization command could not run in {lane}: {exc}. "
            "Correct it with `agent-parley init set`, then rerun. The "
            "worktree is left in place for inspection."
        ) from None
    if not result.returncode:
        return
    tail = "\n".join(
        (result.stdout + result.stderr).splitlines()[-INIT_OUTPUT_LINES:]
    )
    raise BridgeError(
        f"Lane initialization failed in {lane}: `{quoted}` exited "
        f"{result.returncode}, so the lane was not started. The worktree is "
        "left in place for inspection. Last output:\n" + tail
    )
