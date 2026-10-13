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

import json
import sqlite3
import statistics
import time
import uuid
from pathlib import Path

from agent_parley import attachments, checkpoints, issues, process, store
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
        lane that held the claim and the seconds from the claim to the
        merge, or None where the history names no claim before it.
    """
    merged: dict[str, dict] = {}
    for record in ledger.get("issues", {}).values():
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
        claim-to-merge seconds, with zero for a median over nothing; and
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
                    else 0.0
                ),
                "ci_rounds_unknown": counts["unknown"],
                "ci_rounds_median": (
                    float(statistics.median(counts["rounds"]))
                    if counts["rounds"]
                    else 0.0
                ),
                "claim_to_merge_seconds": (
                    float(statistics.median(counts["seconds"]))
                    if counts["seconds"]
                    else 0.0
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
