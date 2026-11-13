"""Reads whether a project's lanes message more than they make progress.

Lanes that talk instead of working send mail while their commits,
reservations, reports and handoffs stop. Each of those is already recorded,
so the reading counts recorded events only and never reads what a message
or a commit says.

The reading compares the last `WINDOW` with the one before it. A project is
stagnant when its mail sent per progress event reaches `THRESHOLD` and its
progress did not rise over the earlier window. The reading is shown to the
operator in ``status`` and ``top``; nothing is sent to any lane.
"""

from __future__ import annotations

import math
import sqlite3
import subprocess
import time
from pathlib import Path

from agent_parley import roster
from agent_parley.state import BridgeError

WINDOW = 3600.0
"""Seconds each compared window covers.

One hour holds several rounds of mail for a lane that waits on a peer, so
one quiet stretch between two commits is not read as stagnation, and it is
short enough that the operator sees a stalled project the same hour.
"""
THRESHOLD = 10.0
"""Mail sent per progress event at which a project reads as stagnant.

A working lane sends a few messages around each commit, reservation, report
or handoff: a claim notice, a question, a handoff. Ten messages per event,
with no rise in progress, is talk that no longer moves work. A project with
no progress at all is read as one event, so fewer than ten messages in the
window never flag. Fixed in code so every project is read the same way.
"""
TOP_LANES = 3


def _mail(
    db: sqlite3.Connection, root: str, since: float, until: float
) -> dict[str, int]:
    """Counts the mail each identity sent in one project inside a window.

    The store records whole seconds, so the end is rounded up: mail sent in
    the second the window closes is inside it.
    """
    rows = db.execute(
        "SELECT s.name AS sender, COUNT(*) AS sent FROM messages m "
        "JOIN agents s ON s.id=m.sender_id "
        "JOIN projects p ON p.id=m.project_id "
        "WHERE p.human_key=? "
        "AND CAST(strftime('%s',m.created_ts) AS INTEGER) >= ? "
        "AND CAST(strftime('%s',m.created_ts) AS INTEGER) < ? "
        "GROUP BY s.name",
        (root, int(since), math.ceil(until)),
    ).fetchall()
    return {row["sender"]: row["sent"] for row in rows}


def _reservations(
    db: sqlite3.Connection, root: str, since: float, until: float
) -> dict[str, int]:
    """Counts the reservations and queued requests each identity made."""
    counted: dict[str, int] = {}
    for table in ("file_reservations", "reservation_requests"):
        rows = db.execute(
            f"SELECT a.name AS holder, COUNT(*) AS made FROM {table} r "
            "JOIN agents a ON a.id=r.agent_id "
            "JOIN projects p ON p.id=r.project_id "
            "WHERE p.human_key=? "
            "AND CAST(strftime('%s',r.created_ts) AS INTEGER) >= ? "
            "AND CAST(strftime('%s',r.created_ts) AS INTEGER) < ? "
            "GROUP BY a.name",
            (root, int(since), math.ceil(until)),
        ).fetchall()
        for row in rows:
            counted[row["holder"]] = counted.get(row["holder"], 0) + row["made"]
    return counted


def _reports(directory: Path, name: str, since: float, until: float) -> int:
    """Counts the reports one lane filed inside a window."""
    from agent_parley import metrics

    return sum(
        1
        for record in metrics.report_records(directory, name, since)
        if float(record.get("at", 0) or 0) < until
    )


def _handoffs(ledger: dict, since: float, until: float) -> dict[str, int]:
    """Counts the handoff actions each lane recorded in the issue ledger."""
    from agent_parley import history

    counted: dict[str, int] = {}
    for record in ledger.get("issues", {}).values():
        for entry in record.get("history", []):
            if entry.get("action") not in history.HANDOFF_ACTIONS:
                continue
            if since <= float(entry.get("at", 0) or 0) < until:
                actor = str(entry.get("actor", ""))
                counted[actor] = counted.get(actor, 0) + 1
    return counted


def _commits(
    root: Path, base: str, branch: str, since: float, until: float
) -> int:
    """Counts the commits a lane branch adds to the base inside a window.

    A branch Git cannot read counts no commits, so a removed branch reads
    as no progress rather than failing the reading. The window is applied
    to the commit stamps rather than through ``git log --since``, which
    reads a small Unix time such as ``@0`` as an approximate date.
    """
    from agent_parley.worktrees import git

    if not base or not branch:
        return 0
    try:
        stamps = git(
            root,
            "log",
            "--format=%ct",
            branch,
            "--not",
            base,
        )
    except (BridgeError, OSError, subprocess.TimeoutExpired):
        return 0
    return sum(
        1
        for stamp in stamps.split()
        if stamp.isdigit() and since <= int(stamp) < until
    )


def counts(
    db: sqlite3.Connection | None,
    directory: Path,
    manifest: dict,
    ledger: dict,
    window: tuple[float, float],
) -> dict[str, dict[str, int]]:
    """Counts each lane's mail sent and progress events inside a window.

    Args:
        db: Open coordination store connection, or None when no store
            exists, which counts no mail and no reservations.
        directory: Private project state directory.
        manifest: Project manifest naming the root, base and roster.
        ledger: The project's issue ledger.
        window: Start, inclusive, and end, exclusive, in Unix time.

    Returns:
        Lane name to ``mail_sent`` and ``progress``, the commits on its
        branch past the base, reservations and reservation requests,
        reports and handoff actions it recorded inside the window.
    """
    since, until = window
    root = manifest["root"]
    mail: dict[str, int] = {}
    reserved: dict[str, int] = {}
    if db is not None:
        try:
            mail = _mail(db, root, since, until)
            reserved = _reservations(db, root, since, until)
        except sqlite3.Error:
            mail, reserved = {}, {}
    handed = _handoffs(ledger, since, until)
    measured = {}
    for name in sorted(manifest["participants"]):
        participant = manifest["participants"][name]
        identity = str(participant.get("display", name))
        measured[name] = {
            "mail_sent": mail.get(identity, 0),
            "progress": reserved.get(identity, 0)
            + _reports(directory, name, since, until)
            + handed.get(name, 0)
            + _commits(
                Path(root),
                str(manifest.get("base", "")),
                str(participant.get("branch", "")),
                since,
                until,
            ),
        }
    return measured


def ratio(mail_sent: int, progress: int) -> float:
    """Divides mail sent by progress events, reading no progress as one.

    Args:
        mail_sent: Messages sent.
        progress: Progress events recorded.

    Returns:
        Mail per progress event, rounded to two places.
    """
    return round(mail_sent / max(progress, 1), 2)


def reading(
    db: sqlite3.Connection | None,
    directory: Path,
    manifest: dict,
    ledger: dict,
    now: float = 0.0,
) -> dict:
    """Reads one project's stagnation signal over the last two windows.

    Retired lanes are left out of both windows.

    Args:
        db: Open coordination store connection, or None.
        directory: Private project state directory.
        manifest: Project manifest naming the root, base and roster.
        ledger: The project's issue ledger.
        now: Unix time the last window ends; the current time when zero.
            It is rounded up to a whole second, because the store records
            whole seconds and mail sent this second belongs to this window.

    Returns:
        ``window`` in seconds, project ``mail_sent``, ``progress``,
        ``previous_progress`` and ``ratio``, ``stagnant``, and ``lanes``,
        the names with the highest ratio first, each with its own counts.
    """
    ends = math.ceil(now or time.time())
    live = {
        **manifest,
        "participants": {
            name: participant
            for name, participant in manifest["participants"].items()
            if not roster.retired(participant)
        },
    }
    current = counts(db, directory, live, ledger, (ends - WINDOW, ends))
    earlier = counts(
        db, directory, live, ledger, (ends - 2 * WINDOW, ends - WINDOW)
    )
    sent = sum(lane["mail_sent"] for lane in current.values())
    progress = sum(lane["progress"] for lane in current.values())
    previous = sum(lane["progress"] for lane in earlier.values())
    measured = ratio(sent, progress)
    ranked = [
        {"lane": name, **lane, "ratio": ratio(**lane)}
        for name, lane in sorted(
            current.items(),
            key=lambda item: (-ratio(**item[1]), item[0]),
        )
        if lane["mail_sent"]
    ]
    return {
        "window": WINDOW,
        "mail_sent": sent,
        "progress": progress,
        "previous_progress": previous,
        "ratio": measured,
        "stagnant": measured >= THRESHOLD and progress <= previous,
        "lanes": ranked,
    }


def line(signal: dict) -> str:
    """Words a stagnant reading as the one line ``status`` and ``top`` show.

    Args:
        signal: Result of `reading`.

    Returns:
        The line naming the window, the project counts and the lanes with
        the highest ratio, or an empty string when the project is not
        stagnant.
    """
    if not signal.get("stagnant"):
        return ""
    minutes = int(signal["window"] // 60)
    lanes = ", ".join(
        f"{lane['lane']} {lane['mail_sent']}/{lane['progress']}"
        for lane in signal["lanes"][:TOP_LANES]
    )
    return (
        f"Stagnation: last {minutes} min, {signal['mail_sent']} mail per "
        f"{signal['progress']} progress events (ratio {signal['ratio']:g}, "
        f"threshold {THRESHOLD:g}; previous {minutes} min "
        f"{signal['previous_progress']} events); most mail per progress: "
        f"{lanes}. Nothing was sent to lanes."
    )
