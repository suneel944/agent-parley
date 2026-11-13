"""Opens several lanes at once, one terminal multiplexer window per lane.

`run --lanes` claims one distinct issue of a milestone for each named lane,
then opens each lane's ordinary `run` in its own tmux window. Every window
runs the same `run NAME` command a person would type, so each lane keeps its
own worktree, credentials, hooks and native permissions; nothing here adds a
flag to the native client. Without tmux on ``PATH`` the same commands are
returned for the caller to print, one per terminal.
"""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from agent_parley.state import BridgeError, LockBusy, lock

if TYPE_CHECKING:
    from agent_parley.cli import Bridge

MULTIPLEXER = "tmux"
CANDIDATES = 1000
TMUX_TIMEOUT = 10
PROBE_TIMEOUT = 2
HASH_DIGITS = 6
FORWARDED = ("PATH",)
LIVE = "live"
OPEN = "open"
STARTED = "started"
FAILED = "failed"


def session_name(root: str) -> str:
    """Names the tmux session of a project after its base checkout.

    tmux reads ``.`` and ``:`` in a target as separators, so every character
    other than a letter, digit, underscore or hyphen becomes a hyphen. Two
    projects whose checkouts share a directory name would share that name,
    so the first hex digits of the resolved root's SHA-256 follow it.

    Args:
        root: Project base checkout.

    Returns:
        The session name.
    """
    resolved = Path(root).resolve()
    digest = hashlib.sha256(str(resolved).encode()).hexdigest()
    base = re.sub(r"[^\w-]", "-", resolved.name) or "agent-parley"
    return f"{base}-{digest[:HASH_DIGITS]}"


def _claimed(
    bridge: Bridge,
    lane: Path,
    milestone: str,
    members: set[str] | None,
    taken: set[str],
) -> tuple[str, str]:
    """Claims the first free issue of the milestone `issue next` ranks.

    Args:
        bridge: Command object holding the private state root.
        lane: The lane's worktree, which names the claiming lane.
        milestone: Milestone title.
        members: Open issue numbers of the milestone, or None when the
            forge could not list them.
        taken: Issues this launch already gave to an earlier lane.

    Returns:
        The claimed issue number and an empty reason, or an empty number
        and the reason the lane opens without an issue.
    """
    if members is None:
        return "", f"the forge did not list the issues of {milestone!r}"
    ranked = bridge.issue_next(lane, CANDIDATES)["candidates"]
    number = next(
        (
            str(record["issue"])
            for record in ranked
            if str(record["issue"]) in members
            and str(record["issue"]) not in taken
        ),
        "",
    )
    if not number:
        return "", f"no unclaimed issue of {milestone!r} is free"
    try:
        bridge.issue(lane, "claim", number)
    except BridgeError as exc:
        return "", f"the claim of #{number} failed: {exc}"
    return number, ""


def _tmux(
    arguments: list[str], timeout: int = TMUX_TIMEOUT, *, check: bool = True
) -> subprocess.CompletedProcess[str]:
    """Runs one tmux command and refuses with its own words on failure.

    Args:
        arguments: Arguments after ``tmux``.
        timeout: Seconds the command may take.
        check: Whether a nonzero exit status refuses.

    Returns:
        The finished command with its exit status and output.

    Raises:
        BridgeError: If tmux cannot be started, outlasts ``timeout``, or
            exits nonzero while ``check`` holds.
    """
    try:
        done = subprocess.run(
            [MULTIPLEXER, *arguments],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BridgeError(f"tmux {arguments[0]} failed: {exc}") from exc
    if done.returncode and check:
        raise BridgeError(
            f"tmux {arguments[0]} failed: "
            f"{(done.stderr or done.stdout).strip() or done.returncode}"
        )
    return done


def _refusal(bridge: Bridge, directory: Path) -> str:
    """Names the project or host gate that would refuse every lane's run.

    These are the gates `run` checks before it registers a lane: an
    exhausted run budget and a host past its launch load threshold.

    Args:
        bridge: Command object holding the private state root.
        directory: Private state directory of the project.

    Returns:
        The refusal, or empty text when a run may start.
    """
    from agent_parley import budgets, process, roster, supervision

    recorded = (
        roster.read(directory) if (directory / "project.json").exists() else {}
    )
    stopped = budgets.halted(directory, recorded) if recorded else None
    if stopped:
        return (
            f"the run budget is exhausted ({stopped['cause']}); "
            f"{budgets.RESUME}"
        )
    limits = supervision.configuration(bridge.home, recorded)
    pressure = process.overloaded(
        process.host_load(),
        limits["max_load_ratio"],
        limits["min_available_mib"],
    )
    if pressure:
        return f"the host is past its launch threshold ({pressure})"
    return ""


def _live(lane: Path, name: str) -> bool:
    """Reports whether a session holds the lane's session lock now.

    Args:
        lane: The lane's worktree.
        name: Participant that owns the lane.

    Returns:
        Whether the lock `run` holds for its session is taken.
    """
    try:
        with lock(lane.parent / f"{name}.session.lock"):
            return False
    except (LockBusy, OSError):
        return True


def _home_arguments(home: Path) -> list[str]:
    """Names the state root on a typed command unless it is the default.

    Args:
        home: Private state root of this invocation.

    Returns:
        ``--home HOME``, or nothing for the default root.
    """
    from agent_parley.start import DEFAULT_HOME

    if home == Path(DEFAULT_HOME).expanduser().resolve():
        return []
    return ["--home", str(home)]


def _environment(home: Path) -> list[str]:
    """Builds tmux ``-e`` options carrying the caller's environment.

    A tmux window inherits the server's environment, not the caller's, so
    the state root and ``PATH`` are passed explicitly. Nothing else is
    forwarded, so no token, key or secret reaches a command line.

    Args:
        home: Private state root of this invocation.

    Returns:
        Alternating ``-e`` flags and ``NAME=VALUE`` words.
    """
    pairs = [("AGENT_PARLEY_HOME", str(home))] + [
        (name, os.environ[name]) for name in FORWARDED if name in os.environ
    ]
    return [word for name, value in pairs for word in ("-e", f"{name}={value}")]


def _released(bridge: Bridge, lane: dict) -> str:
    """Releases a claim this launch took for a lane whose window failed.

    Args:
        bridge: Command object holding the private state root.
        lane: The lane record `open_lanes` built.

    Returns:
        A clause saying what happened to the claim, or empty text when this
        launch took none.
    """
    if not lane["claimed"]:
        return ""
    number = lane["issue"]
    try:
        bridge.issue(Path(lane["lane"]), "release", number)
    except BridgeError as exc:
        return f"; the release of #{number} failed: {exc}"
    lane["issue"] = ""
    lane["claimed"] = False
    return f"; released #{number}"


def open_lanes(
    bridge: Bridge,
    repo: Path,
    names: list[str],
    milestone: str,
    which: Callable[[str], str | None] | None = None,
) -> dict:
    """Claims distinct milestone issues and opens one window per lane.

    The launch first checks the gates every `run` checks, an exhausted run
    budget and a host past its load threshold, and refuses before anything
    is registered or claimed. Each lane is then registered the way `run`
    registers it, after its native CLI is found on ``PATH``, so a missing
    client refuses before any lane is added.

    A rerun is idempotent. A lane whose session lock is held, or whose
    window already exists in the session, is skipped and claims nothing.
    A lane that already owns an open issue of the milestone keeps it. The
    others claim in the order named: each reads `issue next` from its own
    worktree, keeps the candidates the milestone lists, and claims the
    first one no earlier lane of this launch holds. The ledger's claim
    refuses an issue another lane owns, so two lanes never hold one issue;
    a lane whose claim fails opens without an issue and the reason is
    returned. Each window then runs `run NAME --issue N`, whose own claim
    of an issue the lane already holds changes nothing.

    With tmux on ``PATH`` each lane gets its own window, detached, in the
    session `session_name` names, created on first use. A window holds
    only its own lane's process, so attaching, detaching or closing it
    leaves the other lanes running. A window tmux fails to open releases
    the claim this launch took for it and the remaining lanes still open.
    Without tmux nothing is opened.

    Args:
        bridge: Command object holding the private state root.
        repo: Any checkout of the target repository.
        names: Participant names, one lane each.
        milestone: Milestone title the issues come from.
        which: Executable lookup; ``shutil.which`` as read at call time
            when None.

    Returns:
        The project root, the tmux session or an empty string without tmux,
        and per lane its name, worktree, issue, whether this launch claimed
        it, the reason it has none or was skipped, its state (``live``,
        ``open``, ``started``, ``failed``, or empty when nothing was
        opened), the command to type and the argument vector its window
        runs.

    Raises:
        BridgeError: If no name or a repeated name is given, a run gate
            refuses, a lane cannot be registered or its native CLI is
            missing, or tmux cannot report the session before any claim.
    """
    from agent_parley import forge, issues, roster
    from agent_parley.launch import native_executable

    if not names or len(set(names)) != len(names):
        raise BridgeError("run --lanes needs distinct participant names.")
    directory = bridge.project(repo, create=False)[1]
    if refusal := _refusal(bridge, directory):
        raise BridgeError(
            f"run --lanes refused: {refusal}. No lane was claimed or opened."
        )
    recorded = (
        roster.read(directory).get("participants", {})
        if (directory / "project.json").exists()
        else {}
    )
    providers = roster.providers(bridge.home)
    for name in names:
        known = providers.get(recorded.get(name, {}).get("provider") or name)
        if known is not None:
            native_executable(known["command"], name)
    data: dict = {}
    for name in names:
        data = bridge.add_participant(repo, name)
    root = str(data["root"])
    session = ""
    exists = False
    windows: set[str] = set()
    if (which or shutil.which)(MULTIPLEXER) is not None:
        session = session_name(root)
        target = f"={session}"
        probe = _tmux(["has-session", "-t", target], check=False)
        exists = not probe.returncode
        if exists:
            listed = _tmux(
                ["list-windows", "-t", target, "-F", "#{window_name}"]
            )
            windows = set(listed.stdout.splitlines())
    members = forge.milestone_issues(Path(root), milestone)
    owned = issues.holders(issues.snapshot(directory))
    lanes: list[dict] = []
    for name in names:
        lane = Path(data["participants"][name]["lane"])
        state, reason = "", ""
        if _live(lane, name):
            state, reason = LIVE, "a session already runs in this lane"
        elif name in windows:
            state, reason = OPEN, f"window {name} is already open"
        kept = next(
            (
                number
                for number in owned.get(name, [])
                if members is not None and number in members
            ),
            "",
        )
        lanes.append(
            {
                "participant": name,
                "lane": str(lane),
                "issue": kept if not state else "",
                "claimed": False,
                "reason": reason,
                "state": state,
            }
        )
    taken = {lane["issue"] for lane in lanes if lane["issue"]}
    for entry in lanes:
        if entry["state"] or entry["issue"]:
            continue
        number, entry["reason"] = _claimed(
            bridge, Path(entry["lane"]), milestone, members, taken
        )
        if number:
            taken.add(number)
            entry["issue"], entry["claimed"] = number, True
    home = _home_arguments(bridge.home)
    for entry in lanes:
        issue = ["--issue", entry["issue"]] if entry["issue"] else []
        entry["command"] = shlex.join(
            ["agent-parley", *home, "run", entry["participant"], *issue]
            + ["--repo", root]
        )
        entry["argv"] = [
            sys.executable,
            "-m",
            "agent_parley",
            "--home",
            str(bridge.home),
            "run",
            entry["participant"],
            *issue,
            "--repo",
            root,
        ]
    if not session:
        return {"root": root, "session": "", "lanes": lanes}
    environment = _environment(bridge.home)
    for entry in lanes:
        if entry["state"]:
            continue
        place = (
            ["new-window", "-d", "-t", f"={session}:"]
            if exists
            else ["new-session", "-d", "-s", session]
        )
        try:
            _tmux(
                [*place, *environment, "-n", entry["participant"]]
                + ["-c", root, *entry["argv"]]
            )
        except BridgeError as exc:
            entry["state"] = FAILED
            entry["reason"] = f"{exc}{_released(bridge, entry)}"
            continue
        entry["state"] = STARTED
        exists = True
    return {"root": root, "session": session, "lanes": lanes}


def failed(opened: dict) -> list[str]:
    """Names the lanes whose window `open_lanes` failed to open.

    Args:
        opened: What `open_lanes` returned.

    Returns:
        The failed participant names, in the order named.
    """
    return [
        lane["participant"]
        for lane in opened["lanes"]
        if lane["state"] == FAILED
    ]


def _line(lane: dict) -> str:
    """Words one lane's outcome for the terminal."""
    name = lane["participant"]
    if lane["state"] == FAILED:
        return f"{name}: failed: {lane['reason']}"
    if lane["state"] in (LIVE, OPEN):
        return f"{name}: skipped: {lane['reason']}"
    return f"{name}: window {name}, " + (
        f"issue #{lane['issue']}"
        if lane["issue"]
        else f"no issue: {lane['reason']}"
    )


def lines(opened: dict) -> list[str]:
    """Words what `open_lanes` did for the terminal.

    Args:
        opened: What `open_lanes` returned.

    Returns:
        One line per lane, a summary of started and failed lanes and the
        attach command, or without tmux one typed `run` command per lane
        that was not skipped after a line saying so.
    """
    if not opened["session"]:
        return [
            "No terminal multiplexer on PATH; run each lane in its own "
            "terminal:",
            *(lane["command"] for lane in opened["lanes"] if not lane["state"]),
        ]
    started = [
        lane["participant"]
        for lane in opened["lanes"]
        if lane["state"] == STARTED
    ]
    return [
        *(_line(lane) for lane in opened["lanes"]),
        f"Started: {', '.join(started) or 'none'}; "
        f"failed: {', '.join(failed(opened)) or 'none'}",
        f"Attach with: tmux attach -t {opened['session']}",
    ]


def attach_line(root: str) -> str:
    """Names the tmux session `run --lanes` opened for a project, if live.

    Args:
        root: Project base checkout.

    Returns:
        The attach command, or an empty string when tmux is absent or the
        session is not running.
    """
    if shutil.which(MULTIPLEXER) is None:
        return ""
    session = session_name(root)
    try:
        live = _tmux(
            ["has-session", "-t", f"={session}"], PROBE_TIMEOUT, check=False
        ).returncode
    except BridgeError:
        return ""
    return "" if live else f"Lanes session: tmux attach -t {session}"
