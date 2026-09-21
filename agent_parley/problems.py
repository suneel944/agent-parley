"""Derives every condition an operator should act on from one status reading.

The view reads. It reuses the status reading, the supervision thresholds and
the store classification; it records nothing, wakes nobody and moves no
ownership. Each row names the lane, the condition, how long it has held and
the one command that clears it, so an operator returning to twenty lanes
reads what needs them now instead of comparing timestamps across `status`,
`top`, `issue list` and `doctor`.
"""

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
BOUNCE = "bounced share"
DRIFT = "branch drift"
DIRTY = "dirty worktree"
BUDGET = "over budget"
WAKE = "wake attention"


def _row(
    condition: str,
    detail: str,
    command: str,
    seconds: int | None = None,
    participant: str = "",
    project: str = "",
) -> dict:
    """Shapes one problem row with every field a report prints."""
    return {
        "project": project,
        "participant": participant,
        "condition": condition,
        "detail": detail,
        "seconds": seconds,
        "command": command,
    }


def _wake(name: str, repo: str, availability: dict) -> str:
    """Names the remedy that fits the lane's recorded session process.

    A lane between turns and a lane whose launcher exited need opposite
    commands. Resuming a lane whose launcher is still running collides with
    the session lock that launcher holds, so a live lane is woken through its
    inbox or its terminal and only a stopped lane is resumed.

    Args:
        name: Participant that owns the lane.
        repo: Rendered `--repo` argument naming the project.
        availability: The lane's presence reading.

    Returns:
        The wake a live launcher takes, or the resume a stopped lane takes.
    """
    if availability["state"] == supervision.STOPPED:
        return f"agent-parley run {name} --resume {repo}"
    return f'agent-parley say {name} "<text>" {repo}'


def _lane_rows(
    record: dict, participant: dict, root: str, ack_after: float, now: float
) -> list[dict]:
    """Derives the rows one lane record carries.

    Args:
        record: One participant record from the status reading.
        participant: The lane's manifest entry, naming its worktree.
        root: Canonical project key.
        ack_after: Seconds after which an unacknowledged message is a row.
        now: Unix time the reading is compared against.

    Returns:
        Zero or more rows, one per condition the record shows. A lane that has
        recorded no native activity carries no age on the rows that report one,
        because the span it has been quiet for is unknown rather than long.
        A lane owing several acknowledgements carries one row naming the
        oldest, so a broadcast costs one row per lane rather than one per
        message it created.
    """
    name = record["participant"]
    repo = f"--repo {root}"
    rows: list[dict] = []
    idle = record["idle"]
    availability = record["availability"]
    quiet = availability["state"] != "active"
    wake = record.get("wake") or {}
    wake_result = wake.get("result", "")
    wake_details = {
        "busy:input": "wake refused because operator input is pending",
        "busy:repeat": (
            "wake refused because the previous accepted wake produced no "
            "checkpoint"
        ),
        "manual attention required": "wake requires operator attention",
    }
    if wake_result in wake_details:
        command = (
            f"return to {name}'s terminal and complete or stop the session"
            if availability["process_alive"] is not False
            else f"agent-parley run {name} {repo}"
        )
        rows.append(
            _row(
                WAKE,
                wake_details[wake_result],
                command,
                wake.get("age_seconds"),
                name,
                root,
            )
        )
    if idle["stalled"]:
        rows.append(
            _row(
                STALLED,
                supervision.stall_marker(idle),
                _wake(name, repo, availability),
                int(idle["age_seconds"]),
                name,
                root,
            )
        )
    elif quiet and availability["process_alive"]:
        rows.append(
            _row(
                INACTIVE,
                "alive but no native activity past the inactive threshold",
                _wake(name, repo, availability),
                availability["age_seconds"],
                name,
                root,
            )
        )
    for claim in record["claims"]:
        if claim["overdue"]:
            rows.append(
                _row(
                    OVERDUE,
                    f"issue #{claim['issue']} is past its deadline",
                    f"agent-parley issue release {claim['issue']} {repo}",
                    claim["overdue_seconds"],
                    name,
                    root,
                )
            )
    waiting = [
        pending
        for pending in (record.get("mail") or {}).get("outstanding_ack", [])
        if pending.get("age_seconds", 0) >= ack_after
    ]
    if waiting:
        oldest = max(waiting, key=lambda pending: pending.get("age_seconds", 0))
        detail = (
            f"message {oldest.get('message_id')} from "
            f"{oldest.get('sender')} awaits acknowledgement"
        )
        if len(waiting) > 1:
            detail = (
                f"{len(waiting)} messages await acknowledgement, "
                f"the oldest {oldest.get('message_id')} from "
                f"{oldest.get('sender')}"
            )
        rows.append(
            _row(
                ACK,
                detail,
                _wake(name, repo, availability),
                oldest.get("age_seconds"),
                name,
                root,
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
    elif quiet and supervision.dirty_paths(participant["lane"]):
        rows.append(
            _row(
                DIRTY,
                "uncommitted work in the worktree and no recent activity",
                f"agent-parley participant retire {name} {repo}",
                availability["age_seconds"],
                name,
                root,
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
    """Derives one row per handoff offer nobody has answered."""
    rows = []
    for record in project["issues"]:
        offer = record["offer"]
        if not offer:
            continue
        number = record["issue"]
        repo = f"--repo {project['root']}"
        command = (
            f"agent-parley issue assign {number} {offer['to']} --unassign "
            f"{repo}"
            if issues.offer_source(offer) == issues.OPERATOR
            else f"agent-parley issue cancel {number} {repo}"
        )
        created = float(offer.get("created") or now)
        rows.append(
            _row(
                OFFER,
                f"issue #{number} offered to {offer['to']} "
                f"by {issues.offer_source(offer)}",
                command,
                max(0, int(now - created)),
                offer["to"],
                project["root"],
            )
        )
    return rows


def _bounce_rows(
    home: Path, directory: Path, data: dict, project: dict
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

    Returns:
        Zero or more rows, oldest share first.
    """
    availability = {
        record["participant"]: record.get("availability") or {}
        for record in project["participants"]
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
        rows.append(
            _row(
                BOUNCE,
                f"share {share['message_id']} returned unanswerable: {listed}",
                _wake(
                    first,
                    repo,
                    availability.get(first) or {"state": supervision.UNKNOWN},
                ),
                share["waiting_seconds"],
                share["sender_lane"] or share["sender"],
                project["root"],
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
        Rows ordered by how long each has held, longest first. A store or
        service row carries no age and leads the list, because no other
        row can be acted on until the store is usable and the service is
        up. A row reports; nothing here revokes, releases or wakes.
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
        after = (
            ack_after or supervision.configuration(home, data)["stalled_after"]
        )
        for record in project["participants"]:
            aged.extend(
                _lane_rows(
                    record,
                    data["participants"][record["participant"]],
                    project["root"],
                    after,
                    stamp,
                )
            )
        aged.extend(_offer_rows(project, stamp))
        aged.extend(_bounce_rows(home, directory, data, project))
    aged.sort(key=lambda row: -(row["seconds"] or 0))
    return rows + aged


def lines(rows: list[dict]) -> list[str]:
    """Renders the rows as one line each, or one line saying there are none."""
    if not rows:
        return ["No problems: every lane, claim and store reading is clear."]
    width = max(len(row["condition"]) for row in rows)
    named = max(len(row["participant"] or "-") for row in rows)
    return [
        f"{tables.age(-1 if row['seconds'] is None else row['seconds']):>4}  "
        f"{(row['participant'] or '-').ljust(named)}  "
        f"{row['condition'].ljust(width)}  {row['detail']}; {row['command']}"
        for row in rows
    ]


def rendered(rows: list[dict]) -> dict:
    """Shapes the rows for the machine-readable document."""
    return {"problems": rows, "count": len(rows)}
