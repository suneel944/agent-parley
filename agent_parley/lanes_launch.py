"""Opens several lanes at once, one terminal multiplexer window per lane.

`run --lanes` claims one distinct issue of a milestone for each named lane,
then opens each lane's ordinary `run` in its own tmux window. Every window
runs the same `run NAME` command a person would type, so each lane keeps its
own worktree, credentials, hooks and native permissions; nothing here adds a
flag to the native client. Without tmux on ``PATH`` the same commands are
returned for the caller to print, one per terminal.
"""

from __future__ import annotations

import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from agent_parley.state import BridgeError

if TYPE_CHECKING:
    from agent_parley.cli import Bridge

MULTIPLEXER = "tmux"
CANDIDATES = 1000
TMUX_TIMEOUT = 10
PROBE_TIMEOUT = 2


def session_name(root: str) -> str:
    """Names the tmux session of a project after its base checkout.

    tmux reads ``.`` and ``:`` in a target as separators, so every character
    other than a letter, digit, underscore or hyphen becomes a hyphen.

    Args:
        root: Project base checkout.

    Returns:
        The session name.
    """
    return re.sub(r"[^\w-]", "-", Path(root).name) or "agent-parley"


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


def _tmux(arguments: list[str], timeout: int = TMUX_TIMEOUT) -> int:
    """Runs one tmux command and refuses with its own words on failure.

    Args:
        arguments: Arguments after ``tmux``.
        timeout: Seconds the command may take.

    Returns:
        The exit status, which is nonzero only for ``has-session``.

    Raises:
        BridgeError: If any other tmux command fails.
    """
    done = subprocess.run(
        [MULTIPLEXER, *arguments],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if done.returncode and arguments[0] != "has-session":
        raise BridgeError(
            f"tmux {arguments[0]} failed: "
            f"{(done.stderr or done.stdout).strip() or done.returncode}"
        )
    return done.returncode


def open_lanes(
    bridge: Bridge,
    repo: Path,
    names: list[str],
    milestone: str,
    which: Callable[[str], str | None] | None = None,
) -> dict:
    """Claims distinct milestone issues and opens one window per lane.

    Each lane is registered the way `run` registers it, after its native
    CLI is found on ``PATH``, so a missing client refuses before any lane
    is added. The lanes then claim in the order named: each reads `issue
    next` from its own worktree, keeps the candidates the milestone lists,
    and claims the first one no earlier lane of this launch took. The
    ledger's claim refuses an issue another lane owns, so two lanes never
    hold one issue; a lane whose claim fails opens without an issue and the
    reason is returned. Each window then runs `run NAME --issue N`, whose
    own claim of an issue the lane already holds changes nothing.

    With tmux on ``PATH`` each lane gets its own window, detached, in the
    session `session_name` names, created on first use. A window holds
    only its own lane's process, so attaching, detaching or closing it
    leaves the other lanes running. Without tmux nothing is opened.

    Args:
        bridge: Command object holding the private state root.
        repo: Any checkout of the target repository.
        names: Participant names, one lane each.
        milestone: Milestone title the issues come from.
        which: Executable lookup; ``shutil.which`` as read at call time
            when None.

    Returns:
        The project root, the tmux session or an empty string without tmux,
        and per lane its name, issue, the reason it has none, the command
        to type and the argument vector its window runs.

    Raises:
        BridgeError: If no name or a repeated name is given, a lane cannot
            be registered or its native CLI is missing, or a tmux command
            fails.
    """
    from agent_parley import forge, roster
    from agent_parley.launch import native_executable

    if not names or len(set(names)) != len(names):
        raise BridgeError("run --lanes needs distinct participant names.")
    directory = bridge.project(repo, create=False)[1]
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
    members = forge.milestone_issues(Path(root), milestone)
    taken: set[str] = set()
    lanes: list[dict] = []
    for name in names:
        number, reason = _claimed(
            bridge,
            Path(data["participants"][name]["lane"]),
            milestone,
            members,
            taken,
        )
        if number:
            taken.add(number)
        issue = ["--issue", number] if number else []
        lanes.append(
            {
                "participant": name,
                "issue": number,
                "reason": reason,
                "command": shlex.join(
                    ["agent-parley", "run", name, *issue, "--repo", root]
                ),
                "argv": [
                    sys.executable,
                    "-m",
                    "agent_parley",
                    "--home",
                    str(bridge.home),
                    "run",
                    name,
                    *issue,
                    "--repo",
                    root,
                ],
            }
        )
    if (which or shutil.which)(MULTIPLEXER) is None:
        return {"root": root, "session": "", "lanes": lanes}
    session = session_name(root)
    exists = _tmux(["has-session", "-t", f"={session}"]) == 0
    for lane in lanes:
        place = (
            ["new-window", "-d", "-t", f"={session}:"]
            if exists
            else ["new-session", "-d", "-s", session]
        )
        _tmux([*place, "-n", lane["participant"], "-c", root, *lane["argv"]])
        exists = True
    return {"root": root, "session": session, "lanes": lanes}


def lines(opened: dict) -> list[str]:
    """Words what `open_lanes` did for the terminal.

    Args:
        opened: What `open_lanes` returned.

    Returns:
        One line per lane and the attach command, or without tmux one
        typed `run` command per lane after a line saying so.
    """
    if not opened["session"]:
        return [
            "No terminal multiplexer on PATH; run each lane in its own "
            "terminal:",
            *(lane["command"] for lane in opened["lanes"]),
        ]
    shown = [
        f"{lane['participant']}: window {lane['participant']}, "
        + (
            f"issue #{lane['issue']}"
            if lane["issue"]
            else f"no issue: {lane['reason']}"
        )
        for lane in opened["lanes"]
    ]
    return [*shown, f"Attach with: tmux attach -t {opened['session']}"]


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
        live = _tmux(["has-session", "-t", f"={session}"], PROBE_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return ""
    return "" if live else f"Lanes session: tmux attach -t {session}"
