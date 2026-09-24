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

from agent_parley import dialogs, issues, roster, store, supervision, tables

STORE = "store"
SERVICE = "service"
SUPERVISING = "supervision failing"
STALLED = "stalled"
INACTIVE = "inactive"
OVERDUE = "overdue claim"
OFFER = "unanswered offer"
UNRESOLVED = "unresolved completion"
ACK = "awaiting acknowledgement"
BOUNCE = "bounced share"
DRIFT = "branch drift"
DIRTY = "dirty worktree"
BUDGET = "over budget"
WAKE = "wake attention"
APPROVAL = "waiting on approval"
HELD = "held by a native dialog"
READY = "ready to retire"
HOLDING = "holding a refused key"

BY_OPERATOR = "operator"
BY_SERVICE = "service"

DIALOG = "busy:input"
RETRY = "busy:repeat"
APPROVAL = "busy:approval"
ATTENTION = "manual attention required"
PAUSED = "paused"

WAKE_DETAILS = {
    DIALOG: "wake refused because operator input is pending",
    RETRY: (
        "wake refused because the previous accepted wake produced no checkpoint"
    ),
    APPROVAL: (
        "wake refused because the client is waiting for a native approval"
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


def _retire_rows(
    record: dict, name: str, repo: str, root: str, ceiling: float
) -> list[dict]:
    """Reports once that a lane holding only orphaned claims may retire.

    A lane whose session died keeps its claims, and the supervisor marks
    them orphaned so a peer can take them. A marker that has stood past the
    ceiling means nobody took them and the lane did not return, so the
    lane is ready to retire. The row only names the command: the sweep
    never releases held work on its own.

    Args:
        record: One participant record from the status reading.
        name: Participant that owns the lane.
        repo: Rendered `--repo` argument naming the project.
        root: Canonical project key.
        ceiling: Seconds an orphan marker stands before the row appears.

    Returns:
        One row naming the orphaned claims and the age of the oldest
        marker, or no row while the lane holds any claim not orphaned or
        every marker is younger than the ceiling.
    """
    claims = record["claims"]
    if not claims or not all(claim.get("orphaned") for claim in claims):
        return []
    oldest = max(
        int(claim.get("orphan_recorded_seconds") or 0) for claim in claims
    )
    if oldest <= ceiling:
        return []
    numbers = ", ".join(f"#{claim['issue']}" for claim in claims)
    return [
        _row(
            READY,
            f"orphaned claims {numbers} stood unclaimed past the ceiling",
            f"agent-parley participant retire {name} {repo}",
            oldest,
            name,
            root,
            BY_OPERATOR,
            len(claims),
        )
    ]


def _unresolved_rows(
    record: dict, name: str, repo: str, root: str, now: float
) -> list[dict]:
    """Groups one lane's unresolved completions into a single row.

    Args:
        record: One participant record from the status reading.
        name: Participant that owns the lane.
        repo: Rendered `--repo` argument naming the project.
        root: Canonical project key.
        now: Unix time the observation ages are measured against.

    Returns:
        One row naming the oldest observed-complete claim its holder never
        released and counting the rest, or no row when the lane holds none.
        Ownership never moves on an observation, so the resolution stays the
        operator's.
    """
    unresolved = sorted(
        (claim for claim in record["claims"] if claim.get("unresolved")),
        key=lambda claim: float(claim.get("observed_at") or now),
    )
    if not unresolved:
        return []
    oldest = unresolved[0]
    numbers = ", ".join(f"#{claim['issue']}" for claim in unresolved)
    detail = (
        f"issue #{oldest['issue']}: {oldest.get('reason', '')}"
        if len(unresolved) == 1
        else f"{len(unresolved)} claims are complete but never released: "
        f"{numbers}"
    )
    observed = float(oldest.get("observed_at") or now)
    return [
        _row(
            UNRESOLVED,
            detail,
            f"agent-parley issue resolve {oldest['issue']} {repo}",
            max(0, int(now - observed)),
            name,
            root,
            BY_OPERATOR,
            len(unresolved),
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
    record: dict,
    participant: dict,
    root: str,
    config: dict,
    ack_after: float,
    now: float,
) -> list[dict]:
    """Derives the rows one lane record carries, one per cause.

    Args:
        record: One participant record from the status reading.
        participant: The lane's manifest entry, naming its worktree.
        root: Canonical project key.
        config: Resolved supervision settings for the project.
        ack_after: Seconds after which an unacknowledged message is a row.
        now: Unix time the observation ages are measured against.

    Returns:
        Zero or more rows, one per cause the record shows, each carrying how
        many items share that cause and the age of the oldest. A lane that has
        recorded no native activity carries no age on the rows that report one,
        because the span it has been quiet for is unknown rather than long.
        A lane owing several acknowledgements carries one row naming the
        oldest, so a broadcast costs one row per lane rather than one per
        message it created. A native approval prompt is reported once it has
        stood unanswered past the same bound an unacknowledged message uses,
        because a prompt the operator is about to answer needs no row. A
        native dialog the launcher escalated is reported at once by name,
        with the options it offers, because nothing will answer it but the
        operator. A quiet lane that refused a peer a key it still holds is
        reported with the lanes it refused and how long it has been quiet,
        because the refused lane saw the refusal and nobody else did. A
        lane that retired reports only the worktree it kept,
        because its quiet is the state the operator asked for and every
        other remedy here would wake a lane that has given its work back.
    """
    name = record["participant"]
    repo = f"--repo {root}"
    rows: list[dict] = []
    if roster.retired(participant):
        if supervision.dirty_paths(participant["lane"]):
            rows.append(
                _row(
                    DIRTY,
                    "uncommitted work kept when this lane retired",
                    f"agent-parley participant add {name} {repo}",
                    record.get("retired_age_seconds"),
                    name,
                    root,
                )
            )
        return rows
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
    held = record.get("dialog") or {}
    since = held.get("since")
    if held.get("name") == dialogs.PERMISSION and isinstance(
        since, (int, float)
    ):
        waited = max(0, int(now - float(since)))
        if waited >= ack_after:
            tool = str(held.get("tool", "")) or "a tool"
            rows.append(
                _row(
                    APPROVAL,
                    f"the client is waiting for approval of {tool}",
                    f"answer the prompt in {name}'s terminal",
                    waited,
                    name,
                    root,
                )
            )
    elif held.get("escalated"):
        shown = str(held.get("label", "")) or "a native prompt"
        offered = [str(item) for item in held.get("options") or []]
        if offered:
            shown = f"{shown} ({'; '.join(offered)})"
        at = held.get("at")
        rows.append(
            _row(
                HELD,
                f"the client is held by {shown}",
                f"answer the prompt in {name}'s terminal",
                (
                    max(0, int(now - float(at)))
                    if isinstance(at, (int, float))
                    else None
                ),
                name,
                root,
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
    rows.extend(
        _retire_rows(record, name, repo, root, config["orphan_retire_after"])
    )
    rows.extend(_unresolved_rows(record, name, repo, root, now))
    rows.extend(_ack_rows(record, name, repo, root, ack_after, waking))
    refused = (record.get("mail") or {}).get("refused") or []
    if quiet and refused:
        command, actor = _remedy(name, repo, record, waking)
        rows.append(
            _row(
                HOLDING,
                f"idle while holding a key refused to {_listed(refused)}",
                command,
                availability["age_seconds"],
                name,
                root,
                actor,
                len(refused),
            )
        )
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


def _bounce_rows(
    home: Path, directory: Path, data: dict, project: dict, config: dict
) -> list[dict]:
    """Derives one row per share whose recipients cannot act on it.

    The row sits on the sender's lane, because that is the lane still holding
    work it believed it had shared. The command names what the first blocked
    recipient needs, since clearing that condition is what lets the share be
    answered at all. An operator's own request carries no row here; it is
    already reported as awaiting acknowledgement.

    Args:
        home: Private bridge state root.
        directory: Private project state directory.
        data: Project manifest holding every participant.
        project: One project's status reading.
        config: Resolved supervision settings for the project.

    Returns:
        Zero or more rows, oldest share first.
    """
    records = {
        record["participant"]: record for record in project["participants"]
    }
    availability = {
        name: record.get("availability") or {}
        for name, record in records.items()
    }
    repo = f"--repo {project['root']}"
    rows = []
    for share in supervision.bounced_shares(
        home, directory, data, availability
    ):
        blocked = share["blocked"]
        listed = ", ".join(
            f"{entry['recipient']} {entry['reason']}" for entry in blocked[:3]
        )
        first = blocked[0]["lane"]
        record = records.get(first) or {
            "availability": availability.get(first)
            or {"state": supervision.UNKNOWN}
        }
        waking = bool(
            config["wake"]
            and (data["participants"].get(first) or {}).get("wake", True)
        )
        command, actor = _remedy(first, repo, record, waking)
        rows.append(
            _row(
                BOUNCE,
                f"share {share['message_id']} returned unanswerable: {listed}",
                command,
                share["waiting_seconds"],
                share["sender_lane"] or share["sender"],
                project["root"],
                actor,
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
        data["root"]: (path.parent, data)
        for path in (home / "projects").glob("*/project.json")
        for data in [roster.normalize(json.loads(path.read_text()))]
    }
    aged: list[dict] = []
    for project in report["projects"]:
        directory, data = manifests[project["root"]]
        if failing := issues.supervision_error(directory):
            rows.append(
                _row(
                    SUPERVISING,
                    f"supervision poll failing; last: {failing['detail']}",
                    "read server.log in the state directory and fix the "
                    "stage it names; the next clean poll clears this row",
                    max(int(stamp - failing["since"]), 0),
                    project=project["root"],
                )
            )
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
                    stamp,
                )
            )
        aged.extend(_offer_rows(project, stamp))
        aged.extend(_bounce_rows(home, directory, data, project, config))
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
