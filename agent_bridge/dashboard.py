"""Renders a live read-only operator view of coordination and enforcement."""

import contextlib
import curses
import sqlite3
import sys
import time
from collections.abc import Callable
from pathlib import Path

from agent_bridge import roster, store
from agent_bridge.checkpoints import (
    activity,
    event_summary,
    lane_branch,
    mailbox,
    participant_liveness,
)
from agent_bridge.issues import snapshot
from agent_bridge.state import BridgeError

BRANCH_TTL = 5.0
MAX_PROMPT = 120
COLUMNS = (
    ("PARTICIPANT", 14),
    ("PROVIDER", 16),
    ("STATE", 22),
    ("EVENT", 6),
    ("BRANCH", 18),
    ("ISSUES", 10),
    ("MAIL", 9),
    ("LEASES", 9),
    ("CONTEXT", 9),
    ("DENIALS", 9),
    ("CALLS", 9),
)


def _age(seconds: float) -> str:
    """Formats an age compactly, without ever implying sub-second precision."""
    if seconds < 0:
        return "-"
    if seconds < 90:
        return f"{int(seconds)}s"
    if seconds < 5400:
        return f"{int(seconds / 60)}m"
    return f"{int(seconds / 3600)}h"


def _size(count: int) -> str:
    """Formats a byte count in units an operator can compare at a glance."""
    if count < 1024:
        return f"{count}B"
    if count < 1024 * 1024:
        return f"{count / 1024:.1f}kB"
    return f"{count / 1024 / 1024:.1f}MB"


def _fit(value: str, width: int) -> str:
    """Pads a cell, marking any value the column could not show in full.

    Args:
        value: Cell text.
        width: Column width.

    Returns:
        Text padded to the column width, ending in an ellipsis when clipped,
        so a truncated branch or issue list never reads as complete.
    """
    if len(value) > width:
        return value[: width - 1] + "…"
    return value.ljust(width)


def _branch(lane: Path, cache: dict) -> str:
    """Reads a lane branch at most once per branch refresh interval.

    Args:
        lane: Assigned bridge worktree.
        cache: Caller-owned mapping of lane to its last reading.

    Returns:
        The lane's branch name or an unavailable marker.
    """
    key = str(lane)
    now = time.monotonic()
    cached = cache.get(key)
    if cached and now - cached[0] < BRANCH_TTL:
        return str(cached[1])
    value = lane_branch(lane)
    cache[key] = (now, value)
    return value


def _row(
    home: Path,
    directory: Path,
    data: dict,
    agent: str,
    context: dict,
) -> dict:
    """Assembles one participant row from every read-only source.

    Args:
        home: Private bridge state root.
        directory: Private state directory for the common repository.
        data: Project manifest holding this participant.
        agent: Participant that owns the lane.
        context: Shared per-project readings and the branch cache.

    Returns:
        One row of measured coordination and enforcement state.
    """
    participant = data["participants"][agent]
    state = activity(directory, agent)
    events = event_summary(directory, agent)
    stats = context["usage"].get(participant["display"], {})
    issues = context["issues"]["issues"]
    owned = sorted(
        (
            number
            for number, record in issues.items()
            if record["owner"] == agent
        ),
        key=int,
    )
    offers = sum(
        1
        for record in issues.values()
        if record["offer"] and record["offer"]["to"] == agent
    )
    try:
        mail = mailbox(
            home, data["root"], participant["display"], state.get("cursor", 0)
        )
    except (BridgeError, OSError, sqlite3.Error):
        mail = {}
    branch = _branch(Path(participant["lane"]), context["branches"])
    return {
        "participant": agent,
        "provider": (
            f"{participant['provider']}/"
            f"{participant['credential'] or 'default'}"
        ),
        "state": participant_liveness(directory, agent).split(";")[0],
        "event_age": (
            _age(time.time() - events["last_ts"]) if events["last_ts"] else "-"
        ),
        "branch": branch,
        "drift": branch != participant["branch"],
        "issues": ",".join(f"#{number}" for number in owned) or "-",
        "offers": offers,
        "unread": mail.get("unread", "?"),
        "pending_ack": mail.get("pending_ack", "?"),
        "leases": stats.get("leases", 0),
        "lease_age": stats.get("lease_age", 0),
        "injected_bytes": events["injected_bytes"],
        "hook_events": events["events"],
        "denials": events["denials"],
        "calls": stats.get("calls", 0),
        "errors": stats.get("errors", 0),
        "prompt": str(
            state.get("last_prompt") or state.get("task", "")
        ).replace("\n", " ")[:MAX_PROMPT],
    }


def collect(home: Path, running: bool, branches: dict) -> dict:
    """Reads one snapshot of every registered project without changing state.

    Args:
        home: Private bridge state root.
        running: Whether the recorded coordination server process is alive.
        branches: Caller-owned branch cache, refreshed on its own interval.

    Returns:
        Server health, per-project participant rows, and project totals.
    """
    projects = []
    totals = {"participants": 0, "events": 0, "denials": 0, "context": 0}
    for path in sorted((home / "projects").glob("*/project.json")):
        try:
            data = roster.read(path.parent)
        except (BridgeError, OSError, ValueError):
            continue
        try:
            usage = store.usage(home, data["root"])
        except sqlite3.Error:
            usage = {}
        context = {
            "usage": usage,
            "issues": snapshot(path.parent),
            "branches": branches,
        }
        rows = [
            _row(home, path.parent, data, agent, context)
            for agent in sorted(data["participants"])
        ]
        for row in rows:
            totals["participants"] += 1
            totals["events"] += row["hook_events"]
            totals["denials"] += row["denials"]
            totals["context"] += row["injected_bytes"]
        projects.append({"root": data["root"], "rows": rows})
    return {
        "running": running,
        "home": str(home),
        "projects": projects,
        "totals": totals,
    }


def render(view: dict) -> list[str]:
    """Formats a snapshot as plain lines that survive being piped to a file.

    Args:
        view: Snapshot produced by ``collect``.

    Returns:
        Header, one line per participant, and an indented prompt line.
    """
    totals = view["totals"]
    rate = (
        f"{100 * totals['denials'] / totals['events']:.0f}%"
        if totals["events"]
        else "0%"
    )
    lines = [
        f"agent-bridge top  server: "
        f"{'running' if view['running'] else 'not running'}  "
        f"state: {view['home']}",
        f"projects {len(view['projects'])}  "
        f"participants {totals['participants']}  "
        f"hook events {totals['events']}  "
        f"denials {totals['denials']} ({rate})  "
        f"context {_size(totals['context'])}",
    ]
    header = "  ".join(name.ljust(width) for name, width in COLUMNS)
    for project in view["projects"]:
        lines.extend(["", f"project {project['root']}", header.rstrip()])
        if not project["rows"]:
            lines.append("  no participants")
        for row in project["rows"]:
            lines.append(
                "  ".join(
                    _fit(value, width)
                    for value, (_, width) in zip(
                        (
                            row["participant"],
                            row["provider"],
                            row["state"],
                            row["event_age"],
                            row["branch"] + ("!" if row["drift"] else ""),
                            row["issues"]
                            + (f"+{row['offers']}" if row["offers"] else ""),
                            f"{row['unread']}/{row['pending_ack']}",
                            f"{row['leases']}"
                            + (
                                f" {_age(row['lease_age'])}"
                                if row["leases"]
                                else ""
                            ),
                            _size(row["injected_bytes"]),
                            f"{row['denials']}/{row['hook_events']}",
                            f"{row['calls']}"
                            + (f"!{row['errors']}" if row["errors"] else ""),
                        ),
                        COLUMNS,
                        strict=True,
                    )
                ).rstrip()
            )
            if row["prompt"]:
                lines.append(f"    last: {row['prompt']}")
    lines.append("")
    lines.append(
        "Columns: MAIL unread/pending acknowledgement; DENIALS denied or "
        "blocked of retained hook events; CALLS served MCP calls, !rejected. "
        "A branch marked ! left its assigned bridge branch."
    )
    return lines


def _loop(
    screen: "curses.window",
    home: Path,
    running: Callable[[], bool],
    interval: float,
) -> None:
    """Redraws the snapshot until the operator quits; never writes state."""
    branches: dict = {}
    with contextlib.suppress(curses.error):
        curses.curs_set(0)
    screen.timeout(max(100, int(interval * 1000)))
    while True:
        lines = render(collect(home, running(), branches))
        height, width = screen.getmaxyx()
        screen.erase()
        for index, line in enumerate(lines[: height - 1]):
            with contextlib.suppress(curses.error):
                screen.addnstr(index, 0, line, max(1, width - 1))
        with contextlib.suppress(curses.error):
            screen.addnstr(
                height - 1,
                0,
                "q quits; this view never writes state",
                width - 1,
            )
        screen.refresh()
        key = screen.getch()
        if key in (ord("q"), ord("Q"), 27):
            return


def run(
    home: Path,
    running: Callable[[], bool],
    once: bool = False,
    interval: float = 1.0,
) -> None:
    """Shows the dashboard, printing a plain snapshot when it cannot draw.

    Args:
        home: Private bridge state root.
        running: Reports whether the recorded server process is alive.
        once: Print one snapshot instead of drawing a live view.
        interval: Seconds between redraws of the live view.
    """
    if once or not sys.stdout.isatty():
        for line in render(collect(home, running(), {})):
            print(line)
        return
    curses.wrapper(_loop, home, running, interval)
