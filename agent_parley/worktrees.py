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
            "stash. Commit or preserve them first; worktrees start at HEAD."
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
        BridgeError: If the command cannot run, or if it exits non-zero.
        subprocess.TimeoutExpired: If verification exceeds its timeout.
    """
    quoted = shlex.join(command)
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
    if not result.returncode:
        return
    outcome = (
        "The merge commits already recorded stand and are unverified; "
        "nothing was reset or reverted."
        if integrated
        else "Nothing was merged."
    )
    raise BridgeError(
        f"Verification failed in the base checkout at {root}: `{quoted}` "
        f"exited {result.returncode}. Fix it and rerun; merge never skips "
        f"verification. {outcome} See the command output above."
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
