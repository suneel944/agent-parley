"""Flags a lane that keeps reading coordination state without progress.

A lane that calls ``status``, ``issue next``, ``mail`` or a read-only
coordination tool in a loop looks busy in every column while it moves no
work, and every one of those reads spends the turn that could have moved
it. The reads are counted from the store's ``events`` table, never from a
transcript: a served read-only tool is recorded there already, and the
command line records its own reads through `record`. A lane blocked in
``wait_for_message`` is waiting for mail rather than polling for it, so
that call never counts.

Each supervision poll compares a lane's reads over the last `WINDOW`
seconds with its progress over the same window: a claim it took or
reported on, a reservation, a handoff or other message it sent, a report
it filed and a move of its worktree's ``HEAD``, which a commit on its
branch is. A lane at `THRESHOLD` reads or more with none of those starts
a polling episode. It is marked polling in ``status`` and ``top`` and
receives one message naming the pattern, which the ordinary wake path
delivers. A lane that goes on to make `THRESHOLD` more reads inside the
window after that message is parked through the quota parking path: its
wakes, Stop-hook continuations and work offers are deferred exactly like
a quota park, and a running turn is never stopped. The episode, and with
it the flag and the park, ends at the first progress after it began or at
the first message the operator sends the lane after the notice. Reads
made before an episode ended never start the next one.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path

from agent_parley import issues, store
from agent_parley.roster import OPERATOR
from agent_parley.state import BridgeError, lock, write_json

STATE = "polling.json"
LOCK = "polling.lock"
LOCK_SECONDS = 10.0
TOKEN_ENV = "AGENT_PARLEY_TOKEN"
WINDOW = 300.0
"""Seconds of history a lane's reads and progress are compared over.

Five minutes is long enough to hold several turns of a lane that is
genuinely working, which reads its inbox or the board between steps and
commits, reserves or reports at least once, and short enough that a lane
looping on reads is named within one supervision cycle of a few polls.
"""
THRESHOLD = 30
"""Coordination reads in one `WINDOW` that, with no progress, are polling.

A working lane reads coordination state a handful of times per turn, so
even a busy one stays well under one read every ten seconds sustained over
five minutes. Thirty reads in that window is a loop rather than a check,
and it is the same count again after the notice that turns the flag into a
park, so a lane that heeds the notice and reads a few times to find its
work is never parked.
"""
READS = (
    *(tool for tool in store.READ_ONLY if tool != "wait_for_message"),
    "cli:status",
    "cli:issue-next",
    "cli:mail",
)
PROGRESS = (
    "send_message",
    "file_reservation_paths",
    "request_reservation",
    "release_file_reservations",
    "review_report",
)
NOTICE = "polling-"


def record(home: Path, tool: str) -> None:
    """Records one command-line coordination read for the calling lane.

    The lane is the one whose credential the launcher put in the session's
    environment, so an operator's own command records nothing. The write
    never waits for another writer and loses the record rather than delay
    the command, the same way a served read-only call is recorded.

    Args:
        home: Private bridge state root.
        tool: Name the read is counted under, such as ``cli:status``.
    """
    token = os.environ.get(TOKEN_ENV, "")
    if not token or not (home / store.DATABASE).exists():
        return
    with contextlib.suppress(BridgeError, OSError, sqlite3.Error):
        with store.connect(home, write=True, timeout=0) as db:
            actor = db.execute(
                "SELECT id,project_id FROM agents WHERE token_digest=?",
                (hashlib.sha256(token.encode()).hexdigest(),),
            ).fetchone()
            if not actor:
                return
            db.execute(
                "INSERT INTO events(project_id,agent_id,tool,outcome,"
                "duration_ms,result_bytes) VALUES (?,?,?,'ok',0,0)",
                (actor["project_id"], actor["id"], tool),
            )
            db.execute(
                "DELETE FROM events WHERE project_id=? AND id<=(SELECT id "
                "FROM events WHERE project_id=? ORDER BY id DESC LIMIT 1 "
                "OFFSET ?)",
                (
                    actor["project_id"],
                    actor["project_id"],
                    store.MAX_EVENT_ROWS,
                ),
            )


def read(directory: Path) -> dict[str, dict]:
    """Reads every lane's polling record, or none.

    Args:
        directory: Private project state directory.

    Returns:
        Per lane its ``head`` reading, when that ``moved_at``, the event
        ``floor`` reads are counted above, and its open ``episode``.
    """
    try:
        value = json.loads((directory / STATE).read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _cause(reads: int) -> str:
    """Words the pattern a polling lane is flagged and parked for."""
    return (
        f"polling: {reads} coordination reads in {int(WINDOW)}s with no "
        "claim, reservation, commit, report or handoff"
    )


def marker(directory: Path, name: str) -> str:
    """Describes a lane's open polling episode in one line.

    Args:
        directory: Private project state directory.
        name: Participant that owns the lane.

    Returns:
        The pattern and whether the lane was told or parked, or an empty
        string when the lane is not polling.
    """
    episode = (read(directory).get(name) or {}).get("episode")
    if not isinstance(episode, dict):
        return ""
    stage = (
        "parked until progress or an operator message"
        if episode.get("parked")
        else "lane told"
        if episode.get("notice")
        else "lane not told yet"
    )
    return f"{_cause(int(episode.get('reads', 0)))}; {stage}"


def _reading(
    db: sqlite3.Connection, root: str, display: str, lane: dict, start: float
) -> dict | None:
    """Reads one lane's counted reads, progress and operator mail."""
    agent = db.execute(
        "SELECT a.id FROM agents a JOIN projects p ON p.id=a.project_id "
        "WHERE p.human_key=? AND a.name=?",
        (root, display),
    ).fetchone()
    if not agent:
        return None
    episode = lane.get("episode") or {}
    marks = ",".join("?" * len(READS))
    reads = db.execute(
        "SELECT COUNT(*) AS reads, COALESCE(SUM(id>?),0) AS later "
        f"FROM events WHERE agent_id=? AND id>? AND tool IN ({marks}) "
        "AND created_ts>=datetime(?,'unixepoch')",
        (
            int(episode.get("after", 0) or 0),
            agent["id"],
            int(lane.get("floor", 0) or 0),
            *READS,
            int(start),
        ),
    ).fetchone()
    marks = ",".join("?" * len(PROGRESS))
    progress = db.execute(
        "SELECT MAX(id) AS last, MAX(CASE WHEN "
        "created_ts>=datetime(?,'unixepoch') THEN id END) AS recent "
        f"FROM events WHERE agent_id=? AND outcome='ok' AND tool IN ({marks})",
        (int(start), agent["id"], *PROGRESS),
    ).fetchone()
    mail = db.execute(
        "SELECT MAX(m.id) AS last FROM messages m "
        "JOIN message_recipients r ON r.message_id=m.id "
        "JOIN agents s ON s.id=m.sender_id "
        "WHERE r.agent_id=? AND s.name=? "
        "AND COALESCE(m.dedup_key,'') NOT LIKE ?",
        (agent["id"], OPERATOR, f"{NOTICE}%"),
    ).fetchone()
    top = db.execute("SELECT MAX(id) AS top FROM events").fetchone()
    return {
        "reads": int(reads["reads"]),
        "later": int(reads["later"]),
        "progress": int(progress["last"] or 0),
        "recent": progress["recent"] is not None,
        "mail": int(mail["last"] or 0),
        "top": int(top["top"] or 0),
    }


def _progressed_at(directory: Path, name: str, lane: dict) -> float:
    """Returns when a lane last made progress outside the events table."""
    from agent_parley import checkpoints

    stamps = [float(lane.get("moved_at", 0) or 0)]
    reported = checkpoints.activity(directory, name).get("reported_at")
    if isinstance(reported, int | float) and not isinstance(reported, bool):
        stamps.append(float(reported))
    with contextlib.suppress(BridgeError, OSError, ValueError):
        stamps.extend(
            issues.last_progress(item)
            for item in issues.snapshot(directory)["issues"].values()
            if item.get("owner") == name
        )
    return max(stamps)


def _notify(home: Path, root: str, display: str, episode: dict) -> None:
    """Sends a lane the one message naming its polling pattern."""
    with contextlib.suppress(BridgeError, OSError, sqlite3.Error):
        sent = store.speak(
            home,
            root,
            display,
            "Polling without progress",
            f"You made {episode['reads']} coordination reads (status, issue "
            f"next, mail, inbox or board) in the last {int(WINDOW)}s with no "
            "claim, reservation, commit, report or handoff. Reading again "
            "does not move work. Take the next step on your claim, or end "
            "your turn; wait_for_message waits for mail without polling. "
            f"{THRESHOLD} more reads with no progress park your wakes and "
            "work offers until you make progress or the operator writes to "
            "you. Nothing was revoked or stopped.",
            f"{NOTICE}{display}-{episode['top']}",
        )
        episode["notice"] = int(sent["id"])


def observe(
    home: Path, directory: Path, manifest: dict, now: float | None = None
) -> dict[str, dict]:
    """Settles every lane's polling episode once and names the parked lanes.

    The supervision poll calls this once per tick, before `quota.account`,
    which writes the returned records into the quota ledger's ``parked``
    map that every wake, continuation and offer path already consults.

    Args:
        home: Private bridge state root.
        directory: Private project state directory.
        manifest: Project manifest.
        now: Current Unix time; the clock when omitted.

    Returns:
        Per parked lane its ``cause``, ``until`` one window ahead, which the
        next poll renews while the episode lasts, and ``estimated`` false.
    """
    from agent_parley import checkpoints

    current = time.time() if now is None else now
    start = current - WINDOW
    root = manifest["root"]
    parked: dict[str, dict] = {}
    with lock(directory / LOCK, timeout=LOCK_SECONDS):
        state = read(directory)
        for name, participant in sorted(manifest["participants"].items()):
            lane = dict(state.get(name) or {})
            head = (
                checkpoints.head_moves(Path(participant["lane"]))
                if participant.get("lane")
                else 0
            )
            if "head" in lane and head != lane["head"]:
                lane["moved_at"] = current
            lane["head"] = head
            try:
                with store.connect(home) as db:
                    reading = _reading(
                        db,
                        root,
                        participant.get("display") or name,
                        lane,
                        start,
                    )
            except (BridgeError, OSError, sqlite3.Error):
                reading = None
            if reading is None:
                state[name] = lane
                continue
            progressed = _progressed_at(directory, name, lane)
            episode = lane.get("episode")
            if isinstance(episode, dict):
                if (
                    progressed > float(episode["since"])
                    or reading["progress"] > int(episode["top"])
                    or reading["mail"] > int(episode["mail"])
                ):
                    lane.pop("episode")
                    lane["floor"] = reading["top"]
                    state[name] = lane
                    continue
            elif (
                reading["reads"] >= THRESHOLD
                and not reading["recent"]
                and progressed < start
            ):
                episode = {
                    "since": current,
                    "reads": reading["reads"],
                    "top": reading["top"],
                    "after": reading["top"],
                    "mail": reading["mail"],
                    "parked": False,
                }
            else:
                state[name] = lane
                continue
            if not episode.get("notice"):
                _notify(home, root, participant.get("display") or name, episode)
            elif reading["later"] >= THRESHOLD:
                episode["parked"] = True
            if episode["parked"]:
                parked[name] = {
                    "cause": f"parked for {_cause(int(episode['reads']))}",
                    "until": current + WINDOW,
                    "estimated": False,
                }
            lane["episode"] = episode
            state[name] = lane
        write_json(directory / STATE, state)
    return parked
