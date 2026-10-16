"""Measures lane idleness and waiting from records the runtime already keeps.

Agent Parley exists to keep several lanes working at once, and until now it
never measured how much of that time was actually spent working. These readings
answer two questions with numbers rather than impressions: how long a lane went
without coordination activity, and how long each pending item waited before
somebody answered it.

The same durable log holds what a lane reported and what a peer found when it
reviewed that report, because a verdict is only meaningful beside the claim it
judges. Both are agent claims: neither is independent verification.

Every figure is derived from coordination state: the per-lane hook event log,
the served-call events in the store, the mail timestamps, and the issue ledger.
Nothing here asks a vendor, starts a probe or infers what a native client was
doing inside a turn. An idle figure therefore states observed coordination
inactivity, not native work time, and a reading whose window reaches past what
retention kept is reported as incomplete rather than quietly counted as zero.
"""

from __future__ import annotations

import datetime
import json
import sqlite3
import statistics
import time
import uuid
from pathlib import Path

from agent_parley import (
    attachments,
    checkpoints,
    decisions,
    issues,
    lanes,
    process,
    records,
    roster,
    store,
)
from agent_parley.state import (
    MAX_LOG_BYTES,
    MAX_LOG_RECORDS,
    BridgeError,
    lock,
    trim_log,
)

TURN_END = frozenset({"Stop", "SessionEnd"})
REPORTS = "reports.jsonl"
MAX_REPORT_RECORDS = MAX_LOG_RECORDS
MAX_REPORT_LOG_BYTES = MAX_LOG_BYTES
MAX_REPORT_BYTES = 4096
MAX_WAITS = 64
VERDICTS = ("pass", "fail")
OUTCOME_WINDOW = 7 * 86400.0
CLAIM_STARTS = frozenset({"claim", "accept", "take"})
ACTIVE_STATES = frozenset({lanes.STARTING, lanes.WORKING})
OPERATOR_CAUSES = frozenset({lanes.DIALOG, lanes.PROMPT, lanes.APPROVAL})
OPERATOR_LAUNCHES = frozenset(
    {
        checkpoints.Reason.OPERATOR_RESUMED.value,
        checkpoints.Reason.OPERATOR_RESTARTED.value,
    }
)
RESTARTED_FROM = frozenset({lanes.STOPPED, lanes.DEAD})
TOKEN_MARKS = {
    "claude": b'usage"',
    "codex": b"total_token_usage",
    "copilot": b"session.shutdown",
    "gemini": b'"tokens"',
}
CUMULATIVE = frozenset({"codex", "copilot"})
HOUR = 3600.0


def report_path(directory: Path, name: str) -> Path:
    """Returns the durable report, decision and integration log for one lane.

    Args:
        directory: Private state directory for the common repository.
        name: Participant that owns the lane.

    Returns:
        The append-only log path, which lives in coordination state rather
        than in the target repository.
    """
    return directory / f"{name}-{REPORTS}"


def record_report(directory: Path, name: str, entry: dict) -> dict:
    """Appends one durable report or integration record for a lane.

    A lane's activity file keeps only the latest report, so the seconds
    between reporting ready and being integrated could not be recovered from
    it. This log keeps each report and each integration as its own record, so
    the interval between them is a subtraction rather than a guess. Recording
    is best effort: a failed append never fails the report or the merge it
    describes.

    The log is bounded the way every line log the runtime keeps is bounded.
    Once it passes `MAX_REPORT_LOG_BYTES` it is rewritten in place with only
    the newest `MAX_REPORT_RECORDS` records, which is every record a reader
    would return anyway, so a lane that reports on every turn never leaves
    a file that grows for the life of the project. The append and the
    rewrite share one short lock so a concurrent append is never dropped by
    the rewrite; when that lock is busy the record is appended without it
    and the rewrite waits for a later, uncontended append.

    Args:
        directory: Private state directory for the common repository.
        name: Participant that owns the lane.
        entry: Record fields, without its identifier or timestamp.

    Returns:
        The appended record, including the identifier and the time it was
        recorded.
    """
    record = {"id": uuid.uuid4().hex[:16], "at": time.time(), **entry}
    path = report_path(directory, name)
    line = json.dumps(record) + "\n"
    try:
        try:
            with lock(directory / f"{name}-reports.lock", timeout=0.2):
                with path.open("a", encoding="utf-8") as stream:
                    stream.write(line)
                for dropped in trim_log(
                    path, MAX_REPORT_LOG_BYTES, MAX_REPORT_RECORDS
                ):
                    _drop_attachment(directory, dropped)
        except BridgeError:
            with path.open("a", encoding="utf-8") as stream:
                stream.write(line)
    except OSError:
        return record
    return record


def _drop_attachment(directory: Path, line: str) -> None:
    """Removes the attachment of one report record the rotation dropped.

    Args:
        directory: Private state directory for the common repository.
        line: Serialized record leaving the log; a damaged line is ignored.
    """
    try:
        dropped = json.loads(line)
    except ValueError:
        return
    if isinstance(dropped, dict) and dropped.get("attachment"):
        attachments.remove(directory, str(dropped["attachment"]))


def report_records(
    directory: Path, name: str, since: float = 0.0
) -> list[dict]:
    """Reads one lane's durable report and integration records.

    Args:
        directory: Private state directory for the common repository.
        name: Participant that owns the lane.
        since: Unix time floor; older records are omitted. Zero reads
            everything retained.

    Returns:
        Records oldest first. A damaged line is skipped rather than failing a
        report, and the most recent records are kept when the log is long.
    """
    path = report_path(directory, name)
    if not path.exists():
        return []
    records = []
    try:
        text = path.read_text(errors="ignore")
    except OSError:
        return []
    for line in text.splitlines()[-MAX_REPORT_RECORDS:]:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if (
            isinstance(record, dict)
            and float(record.get("at", 0) or 0) >= since
        ):
            records.append(record)
    return records


def find_report(
    directory: Path, participants: list[str], identifier: str
) -> tuple[str, dict]:
    """Names the lane that recorded one report and returns that record.

    Args:
        directory: Private state directory for the common repository.
        participants: Participants whose report logs are searched.
        identifier: Report record identifier.

    Returns:
        The participant that recorded the report and the record itself.

    Raises:
        BridgeError: If no participant retains a report with that identifier.
    """
    for name in participants:
        for record in reversed(report_records(directory, name)):
            if (
                record.get("id") == identifier
                and record.get("kind") == "report"
            ):
                return name, record
    raise BridgeError(f"No report {identifier} is recorded in this project.")


def record_review(
    directory: Path,
    participants: list[str],
    reviewer: str,
    identifier: str,
    verdict: str,
    evidence: str,
) -> dict:
    """Records one lane's verdict on another lane's report.

    A verdict is recorded in the author's own report log, beside the report it
    judges, so every reader of that report reaches the verdict without a
    second lookup and the rotation that bounds the log bounds the verdicts
    with it. Evidence longer than a record's budget is attached exactly as a
    report's own evidence is, readable by the reviewer that wrote it and by
    the author whose work it judges.

    A verdict is the reviewing lane's own claim about work it did not do. It
    is neither an operator approval nor independent verification, and it moves
    no ownership, records no decision and gates no integration.

    Args:
        directory: Private state directory for the common repository.
        participants: Participants whose report logs hold the report.
        reviewer: Participant recording the verdict.
        identifier: Report record the verdict judges.
        verdict: Reviewed outcome, one of `VERDICTS`.
        evidence: Nonempty account of what the reviewer checked.

    Returns:
        The appended verdict record, naming the report it judges, the lane
        that wrote that report, the reviewer and any attached evidence.

    Raises:
        BridgeError: If the verdict is not one of `VERDICTS`, the evidence is
            empty, no participant recorded the report, or the lane recording
            the verdict is the report's own author.
    """
    if verdict not in VERDICTS:
        raise BridgeError("A review verdict is " + " or ".join(VERDICTS) + ".")
    if not evidence.strip():
        raise BridgeError(
            "A review verdict requires --evidence naming what was checked."
        )
    author, report = find_report(directory, participants, identifier)
    if author == reviewer:
        raise BridgeError(
            f"{reviewer} recorded report {identifier}; a verdict is a peer's "
            "claim about work it did not do, so another lane records it."
        )
    review = uuid.uuid4().hex[:16]
    stored, attached = attachments.spill(
        directory,
        "report",
        review,
        evidence,
        MAX_REPORT_BYTES,
        reviewer,
        [author],
    )
    return record_report(
        directory,
        author,
        {
            "id": review,
            "kind": "review",
            "report_id": report["id"],
            "author": author,
            "reviewer": reviewer,
            "verdict": verdict,
            "evidence": stored,
            "attachment": attached or None,
        },
    )


def latest_review(
    directory: Path, name: str, identifier: str = ""
) -> dict | None:
    """Reads the newest peer verdict recorded against one lane's reports.

    Args:
        directory: Private state directory for the common repository.
        name: Participant that owns the lane and wrote the reports.
        identifier: Report the verdict must judge, or empty for the newest
            verdict against any report this lane still retains.

    Returns:
        The verdict record, or None when no peer recorded one. The record is
        the reviewing lane's own claim and never an independent verification.
    """
    for record in reversed(report_records(directory, name)):
        if record.get("kind") != "review":
            continue
        if not identifier or record.get("report_id") == identifier:
            return record
    return None


def latest_report(directory: Path, name: str) -> dict | None:
    """Reads the newest report one lane filed, with the issue it named.

    A lane's activity file keeps the latest report's text but not the issue
    it was about, so status could not tell a report on current work from one
    on an issue the lane has since released. The durable log records both.

    Args:
        directory: Private state directory for the common repository.
        name: Participant that owns the lane.

    Returns:
        The newest report record, or None when the lane retains none.
    """
    for record in reversed(report_records(directory, name)):
        if record.get("kind") == "report":
            return record
    return None


def idle_intervals(
    directory: Path,
    name: str,
    since: float = 0.0,
    now: float = 0.0,
    entries: list[dict] | None = None,
) -> dict:
    """Derives the intervals one lane spent without coordination activity.

    An interval opens when a turn ends, which the native client reports as a
    ``Stop`` or ``SessionEnd`` checkpoint, and closes at the lane's next
    recorded activity. The interval still open at the time of the reading is
    reported as open, and only counted while the recorded session process is
    alive, because a stopped lane is not idle: it is stopped. Records the
    service writes on the lane's behalf, such as a wake request, and hook
    calls from another session are not the lane's activity, so they neither
    close an interval nor open one.

    Args:
        directory: Private state directory for the common repository.
        name: Participant that owns the lane.
        since: Unix time floor for the window. Zero covers everything
            retained.
        now: Instant the window ends at; the current time when zero.
        entries: Records the caller already read from this lane's log for
            the same window, so one frame parses the log once; read here
            when None.

    Returns:
        The intervals, their total in seconds, and whether the reading covers
        the whole window. A window that starts before the oldest retained
        record is incomplete, and the total then understates the truth rather
        than pretending to be exact.
    """
    ends = now or time.time()
    if entries is None:
        try:
            entries = checkpoints.read_events(directory, name, since)
        except BridgeError:
            return {"intervals": [], "seconds": 0, "complete": False}
    unobserved = {reason.value for reason in checkpoints.UNOBSERVED}
    stamps = sorted(
        (float(entry.get("ts", 0) or 0), str(entry.get("event", "")))
        for entry in entries
        if entry.get("reason_class") not in unobserved
    )
    intervals: list[dict] = []
    opened: float | None = None
    for stamp, event in stamps:
        if opened is not None and stamp > opened:
            intervals.append(
                {"start": opened, "end": stamp, "seconds": int(stamp - opened)}
            )
            opened = None
        if event in TURN_END:
            opened = stamp
    if opened is not None:
        state = checkpoints.activity(directory, name)
        if (
            process.alive(state.get("session_pid"), state.get("session_ticks"))
            and ends > opened
        ):
            intervals.append(
                {
                    "start": opened,
                    "end": ends,
                    "seconds": int(ends - opened),
                    "open": True,
                }
            )
    oldest = stamps[0][0] if stamps else ends
    return {
        "intervals": intervals,
        "seconds": sum(interval["seconds"] for interval in intervals),
        "complete": bool(stamps) and (since == 0.0 or oldest <= since + 1),
    }


def _mail_waits(
    home: Path,
    root: str,
    display: str,
    since: float,
    db: sqlite3.Connection | None = None,
) -> list[dict]:
    """Reports how long each message waited to be read and acknowledged."""
    if db is None and not (home / store.DATABASE).exists():
        return []
    waits: list[dict] = []
    with store.reading(home, db) as db:
        agent = db.execute(
            "SELECT a.id FROM agents a JOIN projects p ON p.id=a.project_id "
            "WHERE p.human_key=? AND a.name=?",
            (root, display),
        ).fetchone()
        if not agent:
            return []
        rows = db.execute(
            "SELECT m.id,s.name AS sender,m.ack_required,"
            "unixepoch(m.created_ts) AS created,"
            "unixepoch(r.read_ts) AS read_at,unixepoch(r.ack_ts) AS ack_at "
            "FROM message_recipients r JOIN messages m ON m.id=r.message_id "
            "JOIN agents s ON s.id=m.sender_id "
            "WHERE r.agent_id=? AND unixepoch(m.created_ts)>=? "
            "ORDER BY m.id DESC LIMIT ?",
            (agent["id"], int(since), MAX_WAITS),
        ).fetchall()
    stamp = time.time()
    for row in rows:
        waits.append(
            {
                "kind": "message_read",
                "message_id": row["id"],
                "sender": row["sender"],
                "seconds": int((row["read_at"] or stamp) - row["created"]),
                "complete": row["read_at"] is not None,
            }
        )
        if row["ack_required"]:
            waits.append(
                {
                    "kind": "acknowledgement",
                    "message_id": row["id"],
                    "sender": row["sender"],
                    "seconds": int((row["ack_at"] or stamp) - row["created"]),
                    "complete": row["ack_at"] is not None,
                }
            )
    return waits


def _offer_waits(
    directory: Path, name: str, since: float, ledger: dict | None = None
) -> list[dict]:
    """Reports how long each handoff offer waited to be answered."""
    waits = []
    stamp = time.time()
    if ledger is None:
        ledger = issues.snapshot(directory)
    for number, record in ledger["issues"].items():
        opened = 0.0
        for entry in record.get("history", []):
            at = float(entry.get("at", 0) or 0)
            if entry.get("action") == "offer" and entry.get("actor") == name:
                opened = at
            elif entry.get("action") in ("accept", "decline") and opened:
                if at >= since:
                    waits.append(
                        {
                            "kind": "handoff_offer",
                            "issue": int(number),
                            "answer": entry["action"],
                            "seconds": int(at - opened),
                            "complete": True,
                        }
                    )
                opened = 0.0
        offer = record.get("offer")
        if offer and record.get("owner") == name:
            waits.append(
                {
                    "kind": "handoff_offer",
                    "issue": int(number),
                    "answer": "pending",
                    "offer_id": offer["id"],
                    "seconds": int(stamp - float(offer.get("created", stamp))),
                    "complete": False,
                }
            )
    return waits


def _report_waits(directory: Path, name: str, since: float) -> list[dict]:
    """Reports how long each ready report waited to be integrated."""
    waits = []
    stamp = time.time()
    pending: dict | None = None
    for record in report_records(directory, name):
        if record.get("kind") == "report":
            pending = record if record.get("state") == "ready" else None
        elif record.get("kind") == "integration" and pending:
            if float(record.get("at", 0) or 0) >= since:
                waits.append(
                    {
                        "kind": "report_integration",
                        "action": record.get("action", ""),
                        "report_id": pending["id"],
                        "seconds": int(record["at"] - pending["at"]),
                        "complete": True,
                    }
                )
            pending = None
    if pending and float(pending.get("at", 0) or 0) >= since:
        waits.append(
            {
                "kind": "report_integration",
                "action": "pending",
                "report_id": pending["id"],
                "seconds": int(stamp - pending["at"]),
                "complete": False,
            }
        )
    return waits


def waits(
    home: Path,
    directory: Path,
    manifest: dict,
    name: str,
    since: float = 0.0,
    *,
    db: sqlite3.Connection | None = None,
    ledger: dict | None = None,
) -> list[dict]:
    """Reports every recorded wait for one lane inside a window.

    Args:
        home: Private bridge state root.
        directory: Private state directory for the common repository.
        manifest: Project manifest holding this participant.
        name: Participant that owns the lane.
        since: Unix time floor for the window. Zero covers everything
            retained.
        db: Open read transaction to answer mail waits from, or ``None`` to
            open one.
        ledger: Issue snapshot already read for this project, or ``None`` to
            read one.

    Returns:
        One record per wait, each naming its kind, the item it belongs to, the
        seconds waited, and whether the wait has ended. A wait that has not
        ended reports the seconds so far and ``complete`` false, so a pending
        item is never read as an answered one.
    """
    display = manifest["participants"][name]["display"]
    try:
        mail = _mail_waits(home, manifest["root"], display, since, db)
    except (BridgeError, OSError, sqlite3.Error):
        mail = []
    return [
        *mail,
        *_offer_waits(directory, name, since, ledger),
        *_report_waits(directory, name, since),
    ]


def pending(reported: list[dict]) -> list[dict]:
    """Returns only the waits that have not ended, longest first."""
    return sorted(
        (wait for wait in reported if not wait["complete"]),
        key=lambda wait: wait["seconds"],
        reverse=True,
    )


def ci_rounds(reading: dict | None) -> int | None:
    """Reads the CI round count stored on one pull request reading.

    Supervision owns counting the rounds and stores them as `rounds`, one
    per finished head commit; this is the one place outcome metrics read
    the stored count, so a change to how it is stored changes only this
    function.

    Args:
        reading: One pull request reading from the pull request record, open
            or archived, or None when supervision never recorded it.

    Returns:
        The stored count of CI rounds, or None when none is recorded.
    """
    value = (reading or {}).get("rounds")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def _merges(ledger: dict, since: float) -> dict[str, dict]:
    """Lists the pull requests a claim's resolution recorded as merged.

    Args:
        ledger: Published issue ledger.
        since: Unix time the window starts.

    Returns:
        One entry per pull request number merged inside the window: the
        lane that held the claim, the issue the claim was on and the
        seconds from the claim to the merge, or None where the history
        names no claim before it.
    """
    merged: dict[str, dict] = {}
    for number, record in ledger.get("issues", {}).items():
        started = 0.0
        for entry in record.get("history") or []:
            if entry.get("action") in CLAIM_STARTS:
                started = float(entry.get("at") or 0)
                continue
            evidence = entry.get("evidence") or {}
            if (
                entry.get("action") != "resolve"
                or entry.get("outcome") != "complete"
                or evidence.get("state") != "MERGED"
                or not evidence.get("pull_request")
                or not entry.get("holder")
            ):
                continue
            at = float(evidence.get("closed_at") or entry.get("at") or 0)
            if at < since:
                continue
            merged[str(int(evidence["pull_request"]))] = {
                "lane": str(entry["holder"]),
                "issue": str(number),
                "seconds": at - started if started else None,
            }
    return merged


def _first_pass(reading: dict, rounds: int) -> bool:
    """Reports whether a merged pull request passed on its first attempt."""
    return rounds == 1 and not any(
        review.get("state") == "CHANGES_REQUESTED"
        for review in reading.get("reviews") or []
        if isinstance(review, dict)
    )


def pull_request_outcomes(record: object, ledger: dict, since: float) -> dict:
    """Measures each lane's pull request outcomes inside a window.

    The readings come from what supervision already keeps: the pull request
    record, holding every open pull request and an archive of those that
    left the open list, and the ledger, whose claim resolutions name the
    pull request that merged. A pull request is opened when supervision
    first saw it, merged when a resolution names it, and closed unmerged
    when it left the open list without one. One whose reading names no lane
    and no claim names either is excluded and counted as such.

    Args:
        record: Parsed pull request record, or anything else when it could
            not be read, which reports nothing.
        ledger: Published issue ledger.
        since: Unix time the window starts.

    Returns:
        ``lanes`` mapping each participant with an outcome to its counts,
        first-pass rate over merged pull requests with a known CI round
        count, the count without one, the median CI rounds and the median
        claim-to-merge seconds, with None for a rate or a median over
        nothing, so a lane without the data reports no value; and
        ``excluded``, the pull requests no lane owns.
    """
    stored = record if isinstance(record, dict) else {}
    opened = stored.get("pull_requests")
    archived = stored.get("history")
    current = [
        item
        for item in (opened.values() if isinstance(opened, dict) else [])
        if isinstance(item, dict)
    ]
    gone = [
        item
        for item in (archived if isinstance(archived, list) else [])
        if isinstance(item, dict)
    ]
    readings = {str(item.get("number")): item for item in [*gone, *current]}
    merged = _merges(ledger, since)
    every = _merges(ledger, 0.0)
    tallies: dict[str, dict] = {}
    excluded: set[str] = set()
    counted: set[str] = set()

    def tally(name: str) -> dict:
        """Returns the running counts of one lane."""
        return tallies.setdefault(
            name,
            {
                "opened": 0,
                "merged": 0,
                "closed": 0,
                "first": 0,
                "rounds": [],
                "unknown": 0,
                "seconds": [],
            },
        )

    for item in [*gone, *current]:
        number = str(item.get("number"))
        if (
            not item.get("opened_at")
            or float(item["opened_at"]) < since
            or number in counted
        ):
            continue
        counted.add(number)
        owner = item.get("lane") or every.get(number, {}).get("lane")
        if owner:
            tally(str(owner))["opened"] += 1
        else:
            excluded.add(number)
    for number, item in readings.items():
        if (
            number in every
            or not item.get("gone_at")
            or float(item["gone_at"]) < since
        ):
            continue
        if item.get("lane"):
            tally(str(item["lane"]))["closed"] += 1
        else:
            excluded.add(number)
    for number, merge in merged.items():
        counts = tally(merge["lane"])
        counts["merged"] += 1
        if merge["seconds"] is not None:
            counts["seconds"].append(merge["seconds"])
        reading = readings.get(number)
        rounds = ci_rounds(reading)
        if reading is None or rounds is None:
            counts["unknown"] += 1
            continue
        counts["rounds"].append(rounds)
        counts["first"] += int(_first_pass(reading, rounds))
    return {
        "lanes": {
            name: {
                "pull_requests_opened": counts["opened"],
                "pull_requests_merged": counts["merged"],
                "pull_requests_closed": counts["closed"],
                "first_pass": counts["first"],
                "first_pass_known": len(counts["rounds"]),
                "first_pass_rate": (
                    counts["first"] / len(counts["rounds"])
                    if counts["rounds"]
                    else None
                ),
                "ci_rounds_unknown": counts["unknown"],
                "ci_rounds_median": (
                    float(statistics.median(counts["rounds"]))
                    if counts["rounds"]
                    else None
                ),
                "claim_to_merge_seconds": (
                    float(statistics.median(counts["seconds"]))
                    if counts["seconds"]
                    else None
                ),
            }
            for name, counts in tallies.items()
        },
        "excluded": len(excluded),
    }


def outcome_line(outcomes: dict, window: str) -> str:
    """Summarizes a project's pull request outcomes on one line.

    Args:
        outcomes: Reading from `pull_request_outcomes`.
        window: The window the reading covers, already formatted.

    Returns:
        The opened, merged and closed counts, the first-pass share over
        merged pull requests with a known CI round count and the unowned
        count, or an empty string when the window holds no pull request.
    """
    lanes = outcomes.get("lanes") or {}

    def total(key: str) -> int:
        """Sums one count over the lanes."""
        return sum(int(counts.get(key) or 0) for counts in lanes.values())

    excluded = int(outcomes.get("excluded") or 0)
    counted = [
        total(key)
        for key in (
            "pull_requests_opened",
            "pull_requests_merged",
            "pull_requests_closed",
        )
    ]
    if not any(counted) and not excluded:
        return ""
    known = total("first_pass_known")
    unknown = total("ci_rounds_unknown")
    return (
        f"Pull requests (last {window}): "
        f"{counted[0]} opened, {counted[1]} merged, "
        f"{counted[2]} closed unmerged, first pass "
        f"{total('first_pass')}/{known}"
        + (f" ({unknown} without CI rounds)" if unknown else "")
        + (f", {excluded} unowned excluded" if excluded else "")
    )


def _bucket(state: str, cause: str) -> str:
    """Names the lane-hour bucket one recorded lane state is charged to."""
    if state in ACTIVE_STATES:
        return "active"
    if state == lanes.IDLE:
        return "idle"
    if state == lanes.BLOCKED:
        return "operator_wait" if cause in OPERATOR_CAUSES else "blocked"
    return ""


def lane_hours(
    db: sqlite3.Connection | None,
    root: str,
    name: str,
    since: float,
    now: float,
) -> dict:
    """Divides one lane's window into the states its transitions recorded.

    The lane state record keeps every transition with the instant it took
    effect, so the seconds a lane spent in a state are the gaps between
    consecutive transitions. Starting and working are active, idle is idle,
    and blocked is split by its cause: a dialog, a prompt or an approval
    waits on the operator, while capacity waits on the provider. Stopped,
    dead and reclaimed lanes are charged to nothing.

    A transition into starting from stopped or dead is a lane start. Its
    record names no cause, so nothing tells an operator's `run` from the
    service's own relaunch; such starts are counted as unattributed rather
    than guessed at.

    Args:
        db: Open read transaction, or None when no store exists.
        root: Canonical project key the lane events are filed under.
        name: Participant that owns the lane.
        since: Unix time the window starts.
        now: Unix time the window ends.

    Returns:
        Seconds per bucket, the count of unattributed starts, and whether
        the retained history names the lane's state at the window start.
        A lane whose oldest retained transition falls inside the window,
        and is not its first ever, is reported incomplete, so its hours
        understate the window rather than pretend to cover it.
    """
    seconds = {"active": 0.0, "idle": 0.0, "operator_wait": 0.0, "blocked": 0.0}
    empty = {"seconds": seconds, "starts": 0, "complete": False}
    if db is None:
        return empty
    try:
        rows = db.execute(
            "SELECT source,target,cause,ts FROM lane_events WHERE project=? "
            "AND lane=? AND kind='transition' AND ts<=? ORDER BY id",
            (root, name, now),
        ).fetchall()
    except sqlite3.OperationalError:
        return empty
    state = cause = ""
    opened = since
    starts = 0
    complete = False
    for row in rows:
        at = float(row["ts"])
        if at <= since:
            state, cause = str(row["target"]), str(row["cause"])
            complete = True
            continue
        if not row["source"]:
            complete = True
        if bucket := _bucket(state, cause):
            seconds[bucket] += max(0.0, at - opened)
        if row["target"] == lanes.STARTING and row["source"] in RESTARTED_FROM:
            starts += 1
        state, cause, opened = str(row["target"]), str(row["cause"]), at
    if bucket := _bucket(state, cause):
        seconds[bucket] += max(0.0, now - opened)
    return {"seconds": seconds, "starts": starts, "complete": complete}


def _count(usage: dict, names: tuple[str, ...]) -> int:
    """Sums the integer token fields one usage object reports."""
    return sum(
        value
        for value in (usage.get(name) for name in names)
        if isinstance(value, int) and not isinstance(value, bool)
    )


def _record_tokens(adapter: str, record: dict) -> tuple[str, int, int] | None:
    """Reads the input and output tokens one native record reports.

    Args:
        adapter: Adapter whose record layout applies.
        record: One parsed line of a native session record.

    Returns:
        The message identifier, empty for a running total, and the input
        and output tokens, or None when the record reports no usage. Claude
        input includes the cache creation and cache read tokens; Codex and
        Copilot CLI report running session totals.
    """
    if adapter == "claude":
        message = record.get("message")
        usage = message.get("usage") if isinstance(message, dict) else None
        if not isinstance(message, dict) or not isinstance(usage, dict):
            return None
        return (
            str(message.get("id", "")),
            _count(
                usage,
                (
                    "input_tokens",
                    "cache_creation_input_tokens",
                    "cache_read_input_tokens",
                ),
            ),
            _count(usage, ("output_tokens",)),
        )
    if adapter == "gemini":
        tokens = record.get("tokens")
        identifier = record.get("id")
        if (
            record.get("type") != "gemini"
            or not isinstance(tokens, dict)
            or not isinstance(identifier, str)
        ):
            return None
        return (
            identifier,
            _count(tokens, ("input",)),
            _count(tokens, ("output",)),
        )
    if adapter == "codex":
        payload = record.get("payload")
        source = payload if isinstance(payload, dict) else record
        info = source.get("info")
        usage = (
            info.get("total_token_usage") if isinstance(info, dict) else None
        )
        if not isinstance(usage, dict):
            return None
        return (
            "",
            _count(usage, ("input_tokens",)),
            _count(usage, ("output_tokens",)),
        )
    data = record.get("data")
    if record.get("type") != "session.shutdown" or not isinstance(data, dict):
        return None
    models = data.get("modelMetrics")
    if not isinstance(models, dict):
        return None
    usages = [
        metric["usage"]
        for metric in models.values()
        if isinstance(metric, dict) and isinstance(metric.get("usage"), dict)
    ]
    return (
        "",
        sum(_count(usage, ("inputTokens",)) for usage in usages),
        sum(_count(usage, ("outputTokens",)) for usage in usages),
    )


def _timestamp(value: object) -> float | None:
    """Reads a native record's ISO 8601 timestamp as a Unix time."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def _file_tokens(
    path: Path, adapter: str, since: float, now: float
) -> tuple[int, int]:
    """Reads the input and output tokens one session record spent in a window.

    A per-message record is counted once per message identifier, from its
    first copy, when that copy's timestamp falls inside the window. A
    running total is the last total inside the window less the last total
    before it. A record without a timestamp cannot be placed in the window
    and is not counted.

    Args:
        path: Native session record file.
        adapter: Adapter whose record layout applies.
        since: Unix time the window starts.
        now: Unix time the window ends.

    Returns:
        Input and output tokens spent inside the window.
    """
    mark = TOKEN_MARKS[adapter]
    seen: set[str] = set()
    counted = [0, 0]
    before = (0, 0)
    latest: tuple[int, int] | None = None
    with path.open("rb") as handle:
        for line in handle:
            if mark not in line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            read = (
                _record_tokens(adapter, record)
                if isinstance(record, dict)
                else None
            )
            at = _timestamp(record.get("timestamp")) if read else None
            if read is None or at is None or at > now:
                continue
            identifier, entered, produced = read
            if adapter in CUMULATIVE:
                if at < since:
                    before = (entered, produced)
                else:
                    latest = (entered, produced)
                continue
            if identifier in seen:
                continue
            if identifier:
                seen.add(identifier)
            if at >= since:
                counted[0] += entered
                counted[1] += produced
    if latest is not None:
        counted = [max(0, latest[0] - before[0]), max(0, latest[1] - before[1])]
    return counted[0], counted[1]


def native_tokens(
    home: Path, participant: dict, since: float, now: float
) -> dict | None:
    """Reads the input and output tokens a lane's native client recorded.

    Every session record the lane's client kept under the config home its
    launch selected is read, as `records.lane_sources` finds them, and only
    usage stamped inside the window is counted. The counts are the client's
    own and never a bill. Sessions started in other worktrees the lane made
    are not read.

    Args:
        home: Private bridge state root.
        participant: Manifest entry naming the lane, provider and account.
        since: Unix time the window starts.
        now: Unix time the window ends.

    Returns:
        ``input`` and ``output`` token counts, or None when the provider has
        no reader that tells input from output, such as OpenCode, whose
        database reports one combined total.
    """
    try:
        entry = roster.provider(home, str(participant.get("provider", "")))
        adapter = str(entry.get("adapter", ""))
        if adapter not in TOKEN_MARKS:
            return None
        config = records._config_home(
            home, entry, participant.get("credential")
        )
        if config is None:
            return None
        paths = records.SOURCES[adapter](
            config, Path(str(participant.get("lane", "")))
        )
    except (BridgeError, KeyError, OSError, ValueError):
        return None
    entered = produced = 0
    for path in paths:
        try:
            if path.stat().st_mtime < since:
                continue
            spent = _file_tokens(path, adapter, since, now)
        except OSError:
            continue
        entered += spent[0]
        produced += spent[1]
    return {"input": entered, "output": produced}


def _operator_mail(
    db: sqlite3.Connection | None, root: str, since: float, now: float
) -> tuple[dict[str, int], int]:
    """Counts the mail the operator sent inside a window, per recipient.

    Returns:
        Messages per recipient identity, and the distinct messages sent, so
        one broadcast is one message in the project total.
    """
    if db is None:
        return {}, 0
    rows = db.execute(
        "SELECT m.id,ra.name AS recipient FROM messages m "
        "JOIN agents s ON s.id=m.sender_id "
        "JOIN projects p ON p.id=m.project_id "
        "LEFT JOIN message_recipients r ON r.message_id=m.id "
        "LEFT JOIN agents ra ON ra.id=r.agent_id "
        "WHERE p.human_key=? AND s.name=? "
        "AND unixepoch(m.created_ts) BETWEEN ? AND ?",
        (root, roster.OPERATOR, int(since), int(now) + 1),
    ).fetchall()
    received: dict[str, int] = {}
    for row in rows:
        if row["recipient"]:
            received[row["recipient"]] = received.get(row["recipient"], 0) + 1
    return received, len({row["id"] for row in rows})


def _answered_decisions(
    directory: Path, since: float, now: float
) -> dict[str, int]:
    """Counts the decisions the operator answered inside a window, per lane.

    A decision is recorded only while notifications are configured, and
    one later closed keeps its answer but moves its settled time to the
    close, which is the instant counted.
    """
    answered: dict[str, int] = {}
    for record in decisions._peek(directory).values():
        if not isinstance(record, dict) or not record.get("answer"):
            continue
        if since <= float(record.get("settled") or 0) <= now:
            lane = str(record.get("lane", ""))
            answered[lane] = answered.get(lane, 0) + 1
    return answered


def _lane_throughput(
    home: Path,
    directory: Path,
    manifest: dict,
    name: str,
    window: tuple[float, float],
    context: dict,
) -> dict:
    """Measures one lane's throughput counters inside a window."""
    since, now = window
    participant = manifest["participants"][name]
    hours = lane_hours(context["db"], manifest["root"], name, since, now)
    try:
        events = checkpoints.read_events(directory, name, since)
    except BridgeError:
        events = []
    approvals = sum(
        1
        for record in report_records(directory, name, since)
        if record.get("kind") == "approval"
        and float(record.get("at", 0) or 0) <= now
    )
    resumes = sum(
        1
        for entry in events
        if entry.get("reason_class") in OPERATOR_LAUNCHES
        and float(entry.get("ts", 0) or 0) <= now
    )
    mail = context["mail"].get(str(participant.get("display", name)), 0)
    answered = context["decisions"].get(name, 0)
    queued = _report_waits(directory, name, since)
    tokens = native_tokens(home, participant, since, now)
    return {
        "provider": str(participant.get("provider", "")),
        "merged_issues": len(
            {
                merge["issue"]
                for merge in context["merges"].values()
                if merge["lane"] == name
            }
        ),
        **{
            f"{bucket}_hours": round(seconds / HOUR, 2)
            for bucket, seconds in hours["seconds"].items()
        },
        "history_complete": hours["complete"],
        "input_tokens": None if tokens is None else tokens["input"],
        "output_tokens": None if tokens is None else tokens["output"],
        "approvals": approvals,
        "decisions": answered,
        "operator_mail": mail,
        "resumes": resumes,
        "operator_touches": approvals + answered + mail + resumes,
        "unattributed_starts": hours["starts"],
        "merge_waits": len(queued),
        "merge_waits_pending": sum(
            1 for wait in queued if not wait["complete"]
        ),
        "merge_wait_hours": round(
            sum(wait["seconds"] for wait in queued) / HOUR, 2
        ),
    }


def _manifests(home: Path) -> list[tuple[Path, dict]]:
    """Reads every registered project manifest that names its root."""
    found = []
    for path in sorted((home / "projects").glob("*/project.json")):
        try:
            manifest = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if (
            isinstance(manifest, dict)
            and manifest.get("root")
            and isinstance(manifest.get("participants"), dict)
        ):
            found.append((path.parent, manifest))
    return found


def _project_throughput(
    home: Path,
    directory: Path,
    manifest: dict,
    window: tuple[float, float],
    db: sqlite3.Connection | None,
) -> dict:
    """Measures every lane of one project and the project's totals."""
    since, now = window
    try:
        ledger = issues.snapshot(directory)
    except (OSError, ValueError):
        ledger = {"issues": {}}
    mail, sent = _operator_mail(db, manifest["root"], since, now)
    answered = _answered_decisions(directory, since, now)
    merges = _merges(ledger, since)
    context = {
        "db": db,
        "mail": mail,
        "decisions": answered,
        "merges": merges,
    }
    measured = {
        name: _lane_throughput(home, directory, manifest, name, window, context)
        for name in sorted(manifest["participants"])
    }
    totals: dict = {}
    for counts in measured.values():
        for key, value in counts.items():
            if isinstance(value, bool) or not isinstance(value, int | float):
                continue
            totals[key] = round(totals.get(key, 0) + value, 2)
    for key in ("input_tokens", "output_tokens"):
        if all(counts[key] is None for counts in measured.values()):
            totals[key] = None
    totals["merged_issues"] = len({merge["issue"] for merge in merges.values()})
    totals["operator_mail"] = sent
    totals["decisions"] = sum(answered.values())
    totals["operator_touches"] = (
        totals.get("approvals", 0)
        + totals["decisions"]
        + sent
        + totals.get("resumes", 0)
    )
    totals["history_complete"] = all(
        counts["history_complete"] for counts in measured.values()
    )
    return {"root": manifest["root"], "lanes": measured, "totals": totals}


def throughput(home: Path, since: float, now: float = 0.0) -> dict:
    """Measures what every project's lanes delivered inside a window.

    Every figure is read from coordination state and the native clients'
    own session records: the issue ledger for merged issues, the lane
    state transitions for active, idle and blocked hours, the report log
    for approvals and the ready-to-integrated merge-queue wait, the
    decision record, the operator's mail, the lane event log for operator
    resumes and restarts, and the session records for tokens. Nothing is
    asked of a model or a forge, so the same command reads a baseline and
    a later target the same way.

    Operator touches are approvals, answered decisions, operator mail and
    operator resumes or restarts. A project total counts a broadcast once
    and a decision answered for a lane no longer listed as well.

    Args:
        home: Private bridge state root.
        since: Unix time the window starts.
        now: Unix time the window ends; the current time when zero.

    Returns:
        ``since``, ``until`` and one entry per project with its ``root``,
        the counters of each lane under ``lanes`` and their ``totals``.
    """
    ends = now or time.time()
    window = (since, ends)
    found = _manifests(home)
    if store.present(home):
        with store.connect(home) as db:
            projects = [
                _project_throughput(home, directory, manifest, window, db)
                for directory, manifest in found
            ]
    else:
        projects = [
            _project_throughput(home, directory, manifest, window, None)
            for directory, manifest in found
        ]
    return {"since": since, "until": ends, "projects": projects}


def _tokens(value: int | None) -> str:
    """Formats a token count, naming an unread one ``n/a``."""
    return "n/a" if value is None else f"{value:,}"


def describe_throughput(reading: dict) -> str:
    """Formats a throughput reading for a terminal.

    A project whose lanes record nothing inside the window is summarized
    in one closing line rather than printed as a table of zeros.

    Args:
        reading: Result of `throughput`.

    Returns:
        One table per active project, a lane row each and a total row,
        followed by the operator touches and any incompleteness.
    """

    def stamp(value: float) -> str:
        """Formats one window bound in UTC."""
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(value))

    lines = [
        f"Throughput {stamp(reading['since'])} to {stamp(reading['until'])}"
    ]
    quiet = 0
    header = (
        f"{'LANE':<16} {'MERGED':>6} {'ACTIVE_H':>8} {'IDLE_H':>7} "
        f"{'OPWAIT_H':>8} {'BLOCK_H':>7} {'IN_TOK':>13} {'OUT_TOK':>11} "
        f"{'TOUCH':>5} {'QUEUE_H':>7}"
    )

    def row(label: str, counts: dict) -> str:
        """Formats one lane or total row."""
        return (
            f"{label[:16]:<16} {counts['merged_issues']:>6} "
            f"{counts['active_hours']:>8.2f} {counts['idle_hours']:>7.2f} "
            f"{counts['operator_wait_hours']:>8.2f} "
            f"{counts['blocked_hours']:>7.2f} "
            f"{_tokens(counts['input_tokens']):>13} "
            f"{_tokens(counts['output_tokens']):>11} "
            f"{counts['operator_touches']:>5} "
            f"{counts['merge_wait_hours']:>7.2f}"
        )

    def busy(counts: dict) -> bool:
        """Reports whether a lane or project recorded anything."""
        return any(
            value
            for key, value in counts.items()
            if key not in ("provider", "history_complete")
        )

    for project in reading["projects"]:
        totals = project["totals"]
        if not busy(totals):
            quiet += 1
            continue
        lines += ["", f"Project {project['root']}", header]
        lines += [
            row(name, counts)
            for name, counts in project["lanes"].items()
            if busy(counts)
        ]
        lines.append(row("total", totals))
        lines.append(
            f"Operator touches: {totals['approvals']} approvals, "
            f"{totals['decisions']} decisions, {totals['operator_mail']} "
            f"mail, {totals['resumes']} resumes or restarts; "
            f"{totals['unattributed_starts']} lane starts unattributed; "
            f"{totals['merge_waits']} merge-queue waits, "
            f"{totals['merge_waits_pending']} pending"
        )
        partial = sorted(
            name
            for name, counts in project["lanes"].items()
            if busy(counts) and not counts["history_complete"]
        )
        if partial:
            lines.append(
                "Lane history starts inside the window for "
                f"{', '.join(partial)}; their hours understate it."
            )
    if quiet:
        lines += ["", f"{quiet} projects recorded nothing in the window."]
    return "\n".join(lines) + "\n"
