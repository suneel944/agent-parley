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
PROGRESS_HANDOFFS = frozenset({"offer", "accept", "assign"})
"""Handoff actions that count as progress.

An offer, an acceptance or an assignment moves work toward a lane. A
decline, cancellation, refusal or unassignment refuses or undoes a handoff,
so a lane that only turns work away reads as making no progress.
"""


def _mail(
    db: sqlite3.Connection, root: str, since: float, until: float
) -> dict[str, list[int]]:
    """Reads when each identity sent mail in one project inside a span.

    The stored text stamps are compared directly, so an index on
    ``created_ts`` can serve the query. The store records whole seconds, so
    the start is rounded down and the end up: mail sent in the second the
    span closes is inside it.

    Returns:
        Sender name to the Unix second of each message it sent.
    """
    rows = db.execute(
        "SELECT s.name AS sender, "
        "CAST(strftime('%s',m.created_ts) AS INTEGER) AS at, "
        "COUNT(*) AS sent FROM messages m "
        "JOIN agents s ON s.id=m.sender_id "
        "JOIN projects p ON p.id=m.project_id "
        "WHERE p.human_key=? "
        "AND m.created_ts>=datetime(?,'unixepoch') "
        "AND m.created_ts<datetime(?,'unixepoch') "
        "GROUP BY s.name,m.created_ts",
        (root, int(since), math.ceil(until)),
    ).fetchall()
    stamps: dict[str, list[int]] = {}
    for row in rows:
        stamps.setdefault(row["sender"], []).extend([row["at"]] * row["sent"])
    return stamps


def _reservations(
    db: sqlite3.Connection, root: str, since: float, until: float
) -> dict[str, list[int]]:
    """Reads when each identity made reservations and queued requests.

    Returns:
        Holder name to the Unix second of each reservation or request it
        made inside the span, rounded as `_mail` rounds it.
    """
    stamps: dict[str, list[int]] = {}
    for table in ("file_reservations", "reservation_requests"):
        rows = db.execute(
            "SELECT a.name AS holder, "
            "CAST(strftime('%s',r.created_ts) AS INTEGER) AS at, "
            f"COUNT(*) AS made FROM {table} r "
            "JOIN agents a ON a.id=r.agent_id "
            "JOIN projects p ON p.id=r.project_id "
            "WHERE p.human_key=? "
            "AND r.created_ts>=datetime(?,'unixepoch') "
            "AND r.created_ts<datetime(?,'unixepoch') "
            "GROUP BY a.name,r.created_ts",
            (root, int(since), math.ceil(until)),
        ).fetchall()
        for row in rows:
            stamps.setdefault(row["holder"], []).extend(
                [row["at"]] * row["made"]
            )
    return stamps


def _reports(
    directory: Path, name: str, since: float, until: float
) -> list[float]:
    """Reads when one lane filed each report inside a span."""
    from agent_parley import metrics

    return [
        at
        for record in metrics.report_records(directory, name, since)
        if (at := float(record.get("at", 0) or 0)) < until
    ]


def _handoffs(
    ledger: dict, since: float, until: float
) -> dict[str, list[float]]:
    """Reads when each lane recorded a progress handoff in the issue ledger.

    Only the actions in `PROGRESS_HANDOFFS` are read.
    """
    stamps: dict[str, list[float]] = {}
    for record in ledger.get("issues", {}).values():
        for entry in record.get("history", []):
            if entry.get("action") not in PROGRESS_HANDOFFS:
                continue
            if since <= (at := float(entry.get("at", 0) or 0)) < until:
                stamps.setdefault(str(entry.get("actor", "")), []).append(at)
    return stamps


def _commits(root: Path, base: str, branch: str) -> list[int]:
    """Reads the author time of each commit a lane branch adds to the base.

    Author time survives a rebase or an amend, which reset the committer
    time, so rewritten work is not read as new progress. A branch Git cannot
    read has no commits, so a removed branch reads as no progress rather
    than failing the reading. Callers apply their windows to the stamps
    rather than through ``git log --since``, which reads a small Unix time
    such as ``@0`` as an approximate date.
    """
    from agent_parley.worktrees import git

    if not base or not branch:
        return []
    try:
        stamps = git(
            root,
            "log",
            "--format=%at",
            branch,
            "--not",
            base,
        )
    except (BridgeError, OSError, subprocess.TimeoutExpired):
        return []
    return [int(stamp) for stamp in stamps.split() if stamp.isdigit()]


def _events(
    db: sqlite3.Connection | None,
    directory: Path,
    manifest: dict,
    ledger: dict,
    span: tuple[float, float],
) -> dict[str, dict[str, list[float]]]:
    """Reads each lane's mail and progress stamps once for a whole span.

    Every source is read once, so a reading of two windows costs one query
    per table and one ``git log`` per lane.

    Returns:
        Lane name to ``mail``, ``recorded``, the reservation stamps the
        store keeps in whole seconds, and ``progress``, the exact stamps of
        its reports, handoffs and commits.
    """
    since, until = span
    root = manifest["root"]
    mail: dict[str, list[int]] = {}
    reserved: dict[str, list[int]] = {}
    if db is not None:
        try:
            mail = _mail(db, root, since, until)
            reserved = _reservations(db, root, since, until)
        except sqlite3.Error:
            mail, reserved = {}, {}
    handed = _handoffs(ledger, since, until)
    events: dict[str, dict[str, list[float]]] = {}
    for name in sorted(manifest["participants"]):
        participant = manifest["participants"][name]
        identity = str(participant.get("display", name))
        commits = _commits(
            Path(root),
            str(manifest.get("base", "")),
            str(participant.get("branch", "")),
        )
        events[name] = {
            "mail": list(mail.get(identity, [])),
            "recorded": list(reserved.get(identity, [])),
            "progress": [
                *_reports(directory, name, since, until),
                *handed.get(name, []),
                *(stamp for stamp in commits if since <= stamp < until),
            ],
        }
    return events


def _bucket(
    events: dict[str, dict[str, list[float]]], window: tuple[float, float]
) -> dict[str, dict[str, int]]:
    """Counts each lane's stamps that fall inside one window.

    Stamps the store keeps in whole seconds count from the second holding
    the start through the second holding the end, as `_mail` reads them.

    Returns:
        Lane name to ``mail_sent`` and ``progress`` inside the window.
    """
    since, until = window
    first, last = int(since), math.ceil(until)
    return {
        name: {
            "mail_sent": sum(1 for at in lane["mail"] if first <= at < last),
            "progress": sum(1 for at in lane["recorded"] if first <= at < last)
            + sum(1 for at in lane["progress"] if since <= at < until),
        }
        for name, lane in events.items()
    }


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
        reports and the offers, acceptances and assignments it recorded
        inside the window.
    """
    return _bucket(_events(db, directory, manifest, ledger, window), window)


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

    Retired lanes are left out of both windows. Each source is read once
    for both windows and its stamps are split between them.

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
    events = _events(db, directory, live, ledger, (ends - 2 * WINDOW, ends))
    current = _bucket(events, (ends - WINDOW, ends))
    earlier = _bucket(events, (ends - 2 * WINDOW, ends - WINDOW))
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
