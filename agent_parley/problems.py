"""Derives every condition an operator should act on from one status reading.

The view reads. It reuses the status reading, the supervision thresholds and
the store classification; it records nothing, wakes nobody and moves no
ownership. Each row names one lane and one cause, how many items share that
cause, how long the oldest of them has held, and either a command the
operator can paste or the actor already handling it, so an operator returning
to twenty lanes reads what needs them now instead of comparing timestamps
across `status`, `top`, `issue list` and `doctor`.

A remedy is derived from the lane's recorded state, never from the condition
alone. A lane whose own client holds an unanswered prompt, whose session
process is gone, or whose calls are paused cannot read mail, so it is never
offered `say`. A lane the coordination service is still waking carries what
that loop has already attempted rather than a command that would duplicate
it, and an active lane is never told to complete or stop a session it is
working in. A worktree holding uncommitted work names the worktree and the
files it holds, because retiring the lane would drop its claims to clean one
directory.
"""

import datetime
import json
import time
from pathlib import Path

from agent_parley import issues, roster, store, supervision, tables

STORE = "store"
SERVICE = "service"
STALLED = "stalled"
INACTIVE = "inactive"
OVERDUE = "overdue claim"
OFFER = "unanswered offer"
ACK = "awaiting acknowledgement"
DRIFT = "branch drift"
DIRTY = "dirty worktree"
BUDGET = "over budget"
WAKE = "wake attention"

BY_OPERATOR = "operator"
BY_SERVICE = "service"

DIALOG = "busy:input"
RETRY = "busy:repeat"
ATTENTION = "manual attention required"
PAUSED = "paused"

WAKE_DETAILS = {
    DIALOG: "wake refused because operator input is pending",
    RETRY: (
        "wake refused because the previous accepted wake produced no checkpoint"
    ),
    ATTENTION: "wake requires operator attention",
}


def _row(
    condition: str,
    detail: str,
    command: str,
    seconds: int | None = None,
    participant: str = "",
    project: str = "",
    actor: str = BY_OPERATOR,
    count: int = 1,
) -> dict:
    """Shapes one problem row with every field a report prints.

    Args:
        condition: The cause this row reports.
        detail: What was observed, including the count when items are grouped.
        command: A command the actor can run, or a sentence naming what the
            actor does when no command expresses it.
        seconds: Age of the oldest item behind the row, or None when the span
            is unknown.
        participant: Lane the row belongs to, empty for estate conditions.
        project: Canonical project key.
        actor: Who acts, `BY_OPERATOR` or `BY_SERVICE`.
        count: How many items of this cause the row stands for.

    Returns:
        One row with every field the text and JSON reports print.
    """
    return {
        "project": project,
        "participant": participant,
        "condition": condition,
        "detail": detail,
        "seconds": seconds,
        "count": count,
        "actor": actor,
        "command": command,
    }


def _blocked(record: dict) -> str:
    """Names what stops the lane reading its mail, or an empty string.

    Args:
        record: One participant record from the status reading.

    Returns:
        The session state or recorded wake refusal that keeps delivered mail
        unread, and an empty string when nothing recorded keeps it unread. A
        message sent to a blocked lane is stored and never reaches a turn, so
        no row offers `say` while one of these holds.
    """
    result = str((record.get("wake") or {}).get("result", ""))
    if record["availability"]["state"] == supervision.STOPPED:
        return supervision.STOPPED
    if result in (DIALOG, ATTENTION):
        return result
    if record.get("paused"):
        return PAUSED
    return ""


def _attempted(name: str, record: dict) -> str:
    """States what the service's wake loop has already done for the lane.

    Args:
        name: Participant that owns the lane.
        record: One participant record from the status reading.

    Returns:
        One sentence naming the attempts the service has recorded and when it
        tries again. An active lane is reported as waited for rather than
        woken, because the loop asks for a turn only once a lane has been
        quiet past the inactive threshold.
    """
    if record["availability"]["state"] == supervision.ACTIVE:
        return (
            f"the coordination service wakes {name} once it goes idle; the "
            "lane is active and needs no operator now"
        )
    attempts = int((record.get("wake") or {}).get("attempts") or 0)
    if not attempts:
        return (
            f"the coordination service wakes {name} on its next poll; no "
            "operator action yet"
        )
    plural = "" if attempts == 1 else "s"
    return (
        f"the coordination service has woken {name} {attempts} time{plural} "
        "and wakes it again on its next poll; no operator action yet"
    )


def _remedy(
    name: str, repo: str, record: dict, waking: bool
) -> tuple[str, str]:
    """Names who can give the lane its next turn and how.

    A lane between turns and a lane whose launcher exited need opposite
    commands. Resuming a lane whose launcher is still running collides with
    the session lock that launcher holds, so only a stopped lane is resumed,
    and a lane that cannot read mail is never handed a delivery command.

    Args:
        name: Participant that owns the lane.
        repo: Rendered `--repo` argument naming the project.
        record: One participant record from the status reading.
        waking: Whether the wake loop is enabled for this lane.

    Returns:
        The remedy text and the actor it belongs to. The service owns the row
        while its wake loop is enabled and still has attempts left for this
        backlog; otherwise the operator owns it, with the command that fits
        the lane's recorded state.
    """
    blocked = _blocked(record)
    if blocked == supervision.STOPPED:
        return f"agent-parley run {name} --resume {repo}", BY_OPERATOR
    if blocked == DIALOG:
        return (
            f"answer the prompt open in {name}'s own client; it reads no "
            "mail until that prompt is cleared",
            BY_OPERATOR,
        )
    if blocked == ATTENTION:
        return (
            f"take the turn waiting in {name}'s own client; the service "
            "stopped asking after its refusals",
            BY_OPERATOR,
        )
    if blocked == PAUSED:
        return f"agent-parley participant resume {name} {repo}", BY_OPERATOR
    attempts = int((record.get("wake") or {}).get("attempts") or 0)
    if waking and attempts < supervision.WORK_WAKE_ATTEMPTS:
        return _attempted(name, record), BY_SERVICE
    return f'agent-parley say {name} "<text>" {repo}', BY_OPERATOR


def _recorded(value: str | None, now: float) -> float:
    """Reads an instant the status reading carries as Unix seconds.

    Args:
        value: RFC 3339 instant from the reading, or None when none was
            recorded.
        now: Fallback used when nothing readable was recorded.

    Returns:
        The instant in Unix seconds, and the fallback when the reading carries
        no readable instant, so an unrecorded time reads as new rather than as
        an age measured from the Unix epoch.
    """
    try:
        return datetime.datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return now


def _listed(paths: list[str]) -> str:
    """Names the first few changed paths and counts the rest."""
    shown = ", ".join(paths[:3])
    return shown if len(paths) <= 3 else f"{shown} and {len(paths) - 3} more"


def _claim_rows(record: dict, name: str, repo: str, root: str) -> list[dict]:
    """Groups one lane's overdue claims into a single row.

    Args:
        record: One participant record from the status reading.
        name: Participant that owns the lane.
        repo: Rendered `--repo` argument naming the project.
        root: Canonical project key.

    Returns:
        One row naming the oldest overdue claim and counting the rest, or no
        row when the lane holds none. Ownership never moves on a deadline, so
        the release stays the operator's.
    """
    overdue = sorted(
        (claim for claim in record["claims"] if claim["overdue"]),
        key=lambda claim: -claim["overdue_seconds"],
    )
    if not overdue:
        return []
    oldest = overdue[0]
    numbers = ", ".join(f"#{claim['issue']}" for claim in overdue)
    detail = (
        f"issue #{oldest['issue']} is past its deadline"
        if len(overdue) == 1
        else f"{len(overdue)} claims are past their deadline: {numbers}"
    )
    return [
        _row(
            OVERDUE,
            detail,
            f"agent-parley issue release {oldest['issue']} {repo}",
            oldest["overdue_seconds"],
            name,
            root,
            BY_OPERATOR,
            len(overdue),
        )
    ]


def _ack_rows(
    record: dict,
    name: str,
    repo: str,
    root: str,
    ack_after: float,
    waking: bool,
) -> list[dict]:
    """Groups the messages one lane has left unacknowledged into one row.

    Args:
        record: One participant record from the status reading.
        name: Participant that owns the lane.
        repo: Rendered `--repo` argument naming the project.
        root: Canonical project key.
        ack_after: Seconds after which an unacknowledged message counts.
        waking: Whether the wake loop is enabled for this lane.

    Returns:
        One row naming the oldest unacknowledged message and counting the
        rest, or no row when none is old enough. A parked lane owes one cause,
        not one cause per message it never read.
    """
    pending = sorted(
        (
            item
            for item in (record.get("mail") or {}).get("outstanding_ack", [])
            if item["age_seconds"] >= ack_after
        ),
        key=lambda item: -item["age_seconds"],
    )
    if not pending:
        return []
    oldest = pending[0]
    detail = (
        f"message {oldest['message_id']} from {oldest['sender']} awaits "
        "acknowledgement"
        if len(pending) == 1
        else f"{len(pending)} messages await acknowledgement, the oldest "
        f"{oldest['message_id']} from {oldest['sender']}"
    )
    command, actor = _remedy(name, repo, record, waking)
    return [
        _row(
            ACK,
            detail,
            command,
            oldest["age_seconds"],
            name,
            root,
            actor,
            len(pending),
        )
    ]


def _lane_rows(
    record: dict, participant: dict, root: str, config: dict, ack_after: float
) -> list[dict]:
    """Derives the rows one lane record carries, one per cause.

    Args:
        record: One participant record from the status reading.
        participant: The lane's manifest entry, naming its worktree.
        root: Canonical project key.
        config: Resolved supervision settings for the project.
        ack_after: Seconds after which an unacknowledged message is a row.

    Returns:
        Zero or more rows, one per cause the record shows, each carrying how
        many items share that cause and the age of the oldest. A lane that has
        recorded no native activity carries no age on the rows that report one,
        because the span it has been quiet for is unknown rather than long.
    """
    name = record["participant"]
    repo = f"--repo {root}"
    rows: list[dict] = []
    idle = record["idle"]
    availability = record["availability"]
    quiet = availability["state"] != supervision.ACTIVE
    waking = bool(config["wake"] and participant.get("wake", True))
    wake = record.get("wake") or {}
    if wake.get("result", "") in WAKE_DETAILS:
        command, actor = _remedy(name, repo, record, waking)
        rows.append(
            _row(
                WAKE,
                WAKE_DETAILS[wake["result"]],
                command,
                wake.get("age_seconds"),
                name,
                root,
                actor,
            )
        )
    if idle["stalled"]:
        command, actor = _remedy(name, repo, record, waking)
        rows.append(
            _row(
                STALLED,
                supervision.stall_marker(idle),
                command,
                int(idle["age_seconds"]),
                name,
                root,
                actor,
            )
        )
    elif quiet and availability["process_alive"]:
        command, actor = _remedy(name, repo, record, waking)
        rows.append(
            _row(
                INACTIVE,
                "alive but no native activity past the inactive threshold",
                command,
                availability["age_seconds"],
                name,
                root,
                actor,
            )
        )
    rows.extend(_claim_rows(record, name, repo, root))
    rows.extend(_ack_rows(record, name, repo, root, ack_after, waking))
    if record["drift"]:
        rows.append(
            _row(
                DRIFT,
                f"on {record['branch']} instead of {record['assigned_branch']}",
                f"agent-parley participant restore {name} {repo}",
                availability["age_seconds"],
                name,
                root,
            )
        )
    elif quiet and (changed := supervision.dirty_paths(participant["lane"])):
        lane = participant["lane"]
        rows.append(
            _row(
                DIRTY,
                f"uncommitted work in {lane} and no recent activity: "
                f"{_listed(changed)}",
                f"commit or stash the work in {lane}; retiring {name} would "
                "drop the claims it holds to clean one directory",
                availability["age_seconds"],
                name,
                root,
                BY_OPERATOR,
                len(changed),
            )
        )
    if (record.get("budget") or {}).get("over"):
        rows.append(
            _row(
                BUDGET,
                record["budget"]["marker"],
                f"agent-parley participant budget {name} {repo}",
                None,
                name,
                root,
            )
        )
    return rows


def _offer_rows(project: dict, now: float) -> list[dict]:
    """Groups the handoff offers nobody has answered, one row per recipient.

    Args:
        project: One project block from the status reading.
        now: Unix time the offer ages are measured against.

    Returns:
        One row per lane holding unanswered offers, naming the oldest offer
        and counting the rest, so a lane ignoring five offers reads as one
        condition rather than five.
    """
    repo = f"--repo {project['root']}"
    waiting: dict[str, list[tuple[int, int, dict]]] = {}
    for record in project["issues"]:
        offer = record["offer"]
        if not offer:
            continue
        created = _recorded(offer.get("created_at"), now)
        waiting.setdefault(offer["to"], []).append(
            (max(0, int(now - created)), record["issue"], offer)
        )
    rows = []
    for recipient, items in waiting.items():
        items.sort(key=lambda item: -item[0])
        age, number, offer = items[0]
        source = issues.offer_source(offer)
        command = (
            f"agent-parley issue assign {number} {recipient} --unassign {repo}"
            if source == issues.OPERATOR
            else f"agent-parley issue cancel {number} {repo}"
        )
        detail = (
            f"issue #{number} offered to {recipient} by {source}"
            if len(items) == 1
            else f"{len(items)} offers await {recipient}, the oldest issue "
            f"#{number} by {source}"
        )
        rows.append(
            _row(
                OFFER,
                detail,
                command,
                age,
                recipient,
                project["root"],
                BY_OPERATOR,
                len(items),
            )
        )
    return rows


def derive(
    home: Path, report: dict, ack_after: float = 0.0, now: float = 0.0
) -> list[dict]:
    """Lists every condition an operator should act on, oldest first.

    Args:
        home: Private bridge state root.
        report: The status reading the launcher produced.
        ack_after: Seconds after which a message awaiting acknowledgement is
            reported; the project's stall interval when zero.
        now: Unix time to compare against; the clock when zero.

    Returns:
        One row per lane per cause, ordered by how long the oldest item behind
        each has held, longest first. A store or service row carries no age and
        leads the list, because no other row can be acted on until the store is
        usable and the service is up. Each row names its actor: the rows the
        coordination service already handles report what its wake loop has
        attempted, and the rest carry what the operator can run. A row reports;
        nothing here revokes, releases or wakes.
    """
    stamp = now or time.time()
    schema = store.schema_state(store.schema_version(home))
    rows: list[dict] = []
    if repair := store.remedy(schema):
        rows.append(_row(STORE, f"schema is {schema}", repair))
    if not report["server"]["ready"]:
        rows.append(
            _row(SERVICE, "coordination server is not ready", "agent-parley up")
        )
    manifests = {
        data["root"]: data
        for path in (home / "projects").glob("*/project.json")
        for data in [roster.normalize(json.loads(path.read_text()))]
    }
    aged: list[dict] = []
    for project in report["projects"]:
        data = manifests[project["root"]]
        config = supervision.configuration(home, data)
        after = ack_after or config["stalled_after"]
        for record in project["participants"]:
            aged.extend(
                _lane_rows(
                    record,
                    data["participants"][record["participant"]],
                    project["root"],
                    config,
                    after,
                )
            )
        aged.extend(_offer_rows(project, stamp))
    aged.sort(key=lambda row: -(row["seconds"] or 0))
    return rows + aged


def lines(rows: list[dict]) -> list[str]:
    """Renders the rows as one line each, or one line saying there are none.

    Args:
        rows: Rows `derive` produced.

    Returns:
        One line per row, closed by a line counting what needs an operator
        against what the coordination service is handling whenever any row
        belongs to the service, so a screen of rows still says how much of it
        is someone's work.
    """
    if not rows:
        return ["No problems: every lane, claim and store reading is clear."]
    width = max(len(row["condition"]) for row in rows)
    named = max(len(row["participant"] or "-") for row in rows)
    listed = [
        f"{tables.age(-1 if row['seconds'] is None else row['seconds']):>4}  "
        f"{(row['participant'] or '-').ljust(named)}  "
        f"{row['condition'].ljust(width)}  {row['detail']}; {row['command']}"
        for row in rows
    ]
    handled = sum(row["actor"] == BY_SERVICE for row in rows)
    if handled:
        listed.append(
            f"{len(rows) - handled} need an operator; {handled} the "
            "coordination service is handling."
        )
    return listed


def rendered(rows: list[dict]) -> dict:
    """Shapes the rows for the machine-readable document.

    Args:
        rows: Rows `derive` produced.

    Returns:
        Every row, the row count, and the split between the rows an operator
        owns and the rows the coordination service is already handling.
    """
    handled = sum(row["actor"] == BY_SERVICE for row in rows)
    return {
        "problems": rows,
        "count": len(rows),
        "operator": len(rows) - handled,
        "service": handled,
    }
