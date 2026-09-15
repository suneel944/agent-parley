"""Renders read-only command results as one machine-readable document.

Every read-only command can print exactly one JSON document instead of its
table. The document carries the fields the table shows plus the identifiers a
table abbreviates, so a script, a shell prompt or another agent reads
coordination state without parsing columns whose widths and order belong to a
terminal. Repeated rows are arrays, never keyed objects, so a reader can page
them without knowing the identifiers in advance.

The renderer only reshapes state a command already read. It opens no store, runs
no Git command and reaches no network, so printing the document cannot fail a
command whose table would have printed.
"""

from __future__ import annotations

import datetime
import json

from agent_parley import issues as issues_state

SCHEMA = "agent-parley/read/v1"

LANE_METRICS: tuple[tuple[str, str, str, str], ...] = (
    (
        "agent_parley_lane_session_alive",
        "gauge",
        "alive",
        "Whether the lane's recorded session process is alive.",
    ),
    (
        "agent_parley_lane_branch_drift",
        "gauge",
        "drift",
        "Whether the lane sits on a branch other than the assigned one.",
    ),
    (
        "agent_parley_lane_issues_held",
        "gauge",
        "issues_held",
        "Issues the participant owns in the ledger.",
    ),
    (
        "agent_parley_lane_offers_pending",
        "gauge",
        "offers",
        "Handoff offers awaiting this participant's answer.",
    ),
    (
        "agent_parley_lane_mail_unread",
        "gauge",
        "unread",
        "Unread messages in the participant's mailbox.",
    ),
    (
        "agent_parley_lane_mail_pending_ack",
        "gauge",
        "pending_ack",
        "Messages awaiting acknowledgement from this participant.",
    ),
    (
        "agent_parley_lane_leases_held",
        "gauge",
        "leases",
        "Advisory reservations the participant holds.",
    ),
    (
        "agent_parley_lane_leases_stale",
        "gauge",
        "stale_leases",
        "Held reservations whose holder has no live session.",
    ),
    (
        "agent_parley_lane_idle_seconds",
        "gauge",
        "idle_seconds",
        "Seconds the lane has been idle in the reported window.",
    ),
    (
        "agent_parley_lane_context_bytes_total",
        "counter",
        "injected_bytes",
        "Coordination context bytes injected in the reported window.",
    ),
    (
        "agent_parley_lane_hook_events_total",
        "counter",
        "hook_events",
        "Hook events recorded in the reported window.",
    ),
    (
        "agent_parley_lane_hook_denials_total",
        "counter",
        "denials",
        "Hook events that denied an action in the reported window.",
    ),
    (
        "agent_parley_lane_served_calls_total",
        "counter",
        "calls",
        "Coordination tool calls served for this participant.",
    ),
    (
        "agent_parley_lane_served_rejections_total",
        "counter",
        "errors",
        "Coordination tool calls rejected for this participant.",
    ),
    (
        "agent_parley_lane_tokens_total",
        "counter",
        "tokens",
        "Tokens the native client counted, never billed spend.",
    ),
)

PROJECT_METRICS: tuple[tuple[str, str, str, str], ...] = (
    (
        "agent_parley_project_participants",
        "gauge",
        "",
        "Participants reported for this project.",
    ),
    (
        "agent_parley_project_idle_seconds",
        "gauge",
        "idle_seconds",
        "Idle seconds summed over the project's reported lanes.",
    ),
    (
        "agent_parley_project_context_bytes_total",
        "counter",
        "injected_bytes",
        "Context bytes injected across the project's reported lanes.",
    ),
    (
        "agent_parley_project_hook_events_total",
        "counter",
        "hook_events",
        "Hook events recorded across the project's reported lanes.",
    ),
    (
        "agent_parley_project_hook_denials_total",
        "counter",
        "denials",
        "Denials recorded across the project's reported lanes.",
    ),
)


def timestamp(value: float | str | None) -> str | None:
    """Converts a recorded time to RFC 3339 in UTC.

    Coordination state records time in two shapes: Unix seconds written by the
    lane state files, and the UTC text SQLite stores for mail and reservations.
    Both are reported in one shape so a reader never has to know which
    substrate answered.

    Args:
        value: Unix seconds, stored UTC text, or None when nothing was
            recorded.

    Returns:
        An RFC 3339 instant in UTC, or None when no time was recorded or the
        recorded value cannot be read as one.
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        moment = datetime.datetime.fromtimestamp(float(value), datetime.UTC)
        return moment.isoformat(timespec="seconds").replace("+00:00", "Z")
    try:
        parsed = datetime.datetime.fromisoformat(str(value).replace(" ", "T"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.UTC)
    return (
        parsed.astimezone(datetime.UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def document(kind: str, payload: dict) -> dict:
    """Wraps one command's fields in the shared snapshot envelope.

    Args:
        kind: Command the document reports, such as ``status`` or ``top``.
        payload: Fields that command reports.

    Returns:
        One document carrying the schema identifier, the command it reports,
        the instant it was taken, and the command's own fields.
    """
    return {
        "schema": SCHEMA,
        "kind": kind,
        "generated_at": timestamp(
            datetime.datetime.now(datetime.UTC).timestamp()
        ),
        **payload,
    }


def render(kind: str, payload: dict) -> str:
    """Serializes one snapshot document for standard output.

    Args:
        kind: Command the document reports.
        payload: Fields that command reports.

    Returns:
        The document as indented JSON text, without a trailing newline.
    """
    return json.dumps(document(kind, payload), indent=2, ensure_ascii=False)


def offer(value: dict | None) -> dict | None:
    """Reports one pending handoff offer with its identifier and deadline.

    The structured work state travels beside the summary, so one document
    carries the commit, the reservation keys, the remaining work and the
    attached diff along with the identifier the recipient answers.
    """
    if not value:
        return None
    waiting = issues_state.offer_state(value)
    return {
        "offer_id": value["id"],
        "to": value["to"],
        "source": issues_state.offer_source(value),
        "summary": value["summary"],
        "created_at": timestamp(value.get("created")),
        "deadline_at": timestamp(waiting["deadline"]),
        "overdue": waiting["overdue"],
        "overdue_seconds": waiting["overdue_seconds"],
        **issues_state.handoff_fields(value),
    }


def handoff(value: dict | None) -> dict | None:
    """Reports the work state an accepted handoff moved onto a record.

    Args:
        value: Accepted handoff recorded on an issue, or None.

    Returns:
        The lane the work came from, the instant it was accepted and the
        structured fields it carried, or None for an issue never handed over.
    """
    if not value:
        return None
    return {
        "from": value.get("from"),
        "accepted_at": timestamp(value.get("at")),
        **issues_state.handoff_fields(value),
    }


def request(value: dict | None) -> dict | None:
    """Reports one operator request that the issue's owner has not answered."""
    if not value:
        return None
    return {
        "offer_id": value["id"],
        "to": value["to"],
        "source": value.get("source") or issues_state.OPERATOR,
        "reason": value.get("reason", ""),
        "created_at": timestamp(value.get("created")),
    }


def doctor(reported: dict) -> dict:
    """Reports the launcher, plugin and store versions and their fit.

    Args:
        reported: Component report produced by the launcher.

    Returns:
        The protocol this build speaks, the protocols it accepts, the store
        schema it writes, one record per component, and whether the set is
        consistent. Each component record carries the state this build puts it
        in and the one command that state needs, so a script gating on this
        output can report the cause rather than only the verdict. The store
        schema is reported as `store_schema`, because `schema` already names
        the document's own schema identifier. No credential and no profile
        path is included. The platform record names the kernel release, the
        WSL generation or `none`, and whether `pidfd_open` is available.
    """
    return {
        "protocol": reported["protocol"],
        "supported": reported["supported"],
        "store_schema": reported["schema"],
        "platform": reported["platform"],
        "components": reported["components"],
        "consistent": reported["consistent"],
    }


def work_plan(applied: dict) -> dict:
    """Reports an applied work-order plan beside current ownership.

    Args:
        applied: Plan description returned by `plan.describe`.

    Returns:
        The plan's identity and the operator that applied it, one record per
        planned issue carrying its owner and the issues it waits on, the
        groups as an array, each marked when every member is reported ready,
        and every edge recorded by hand after the apply.
    """
    return {
        "plan": applied["plan"],
        "digest": applied["digest"],
        "applied_by": applied["applied_by"],
        "applied_at": timestamp(applied["applied_at"]),
        "versions": applied["versions"],
        "issues": applied["issues"],
        "groups": [
            {
                "group": name,
                "issues": members,
                "ready": name in applied.get("ready_groups", []),
            }
            for name, members in sorted(applied["groups"].items())
        ],
        "unplanned": [
            {"issue": issue, "waits_on": blocker}
            for issue, blocker in applied["unplanned"]
        ],
    }


def plan_diff(reported: dict) -> dict:
    """Reports the edges a plan file would add, and those it does not name."""
    return {
        "plan": reported["plan"],
        "digest": reported["digest"],
        "add": [
            {"issue": issue, "waits_on": blocker}
            for issue, blocker in reported["add"]
        ],
        "unlisted": [
            {"issue": issue, "waits_on": blocker}
            for issue, blocker in reported["unlisted"]
        ],
    }


def issues(state: dict) -> list[dict]:
    """Reports the issue ledger as an array ordered by issue number.

    Args:
        state: Published issue ledger.

    Returns:
        One record per issue, carrying its owner, any recorded forge title,
        the issues it waits on, its pending offer with that offer's
        identifier, source and structured work state, the handoff it was last
        accepted through, any unanswered operator request to its owner, and
        any unanswered completion reminder.
    """
    reported = []
    for number, record in sorted(
        state["issues"].items(), key=lambda item: int(item[0])
    ):
        prompt = record.get("handoff_prompt") or {}
        timing = issues_state.deadline_state(record)
        reported.append(
            {
                "issue": int(number),
                "owner": record.get("owner"),
                "title": record.get("title"),
                "deadline_at": timestamp(timing["deadline"]),
                "overdue": timing["overdue"],
                "overdue_seconds": timing["overdue_seconds"],
                "attempts": timing["attempts"],
                "attempt_budget": timing["budget"],
                "budget_exceeded": timing["budget_exceeded"],
                "blocked_by": [
                    int(other) for other in record.get("blocked_by", [])
                ],
                "offer": offer(record.get("offer")),
                "handoff": handoff(record.get("handoff")),
                "request": request(record.get("request")),
                "reminder": (
                    {
                        "text": prompt["text"],
                        "holder": prompt["holder"],
                        "waiting": list(prompt.get("waiting", [])),
                        "created_at": timestamp(prompt.get("created")),
                        "responded_at": timestamp(prompt.get("responded_at")),
                    }
                    if prompt
                    else None
                ),
            }
        )
    return reported


def history(reported: dict) -> dict:
    """Reports recorded history as arrays with RFC 3339 instants.

    Args:
        reported: Reading produced by the history command.

    Returns:
        The subject queried, each ownership generation of an issue, and one
        record per matching coordination event. A record that carries no claim
        identifier reports null: the correlation was never recorded, and
        history does not invent one.
    """
    return {
        "subject": reported["subject"],
        "value": reported["value"],
        "holdings": [
            {
                **generation,
                "started_at": timestamp(generation["started"]),
                "ended_at": timestamp(generation["ended"]),
            }
            for generation in reported["holdings"]
        ],
        "records": [
            {**record, "at": timestamp(record["at"])}
            for record in reported["records"]
        ],
    }


def ledger(state: dict) -> dict:
    """Reports the whole issue ledger with the revision it was read at."""
    return {"revision": state["revision"], "issues": issues(state)}


def participants(manifest: dict) -> list[dict]:
    """Reports the project roster as an array ordered by participant name.

    Args:
        manifest: Project manifest using the participant roster layout.

    Returns:
        One record per participant, naming its provider, its account profile,
        its registered identity and its assigned bridge branch. No credential
        value is reported; a profile name is not a credential.
    """
    return [
        {
            "participant": name,
            "identity": participant["display"],
            "provider": participant["provider"],
            "credential": participant["credential"],
            "branch": participant["branch"],
            "lane": participant["lane"],
            "paused": participant.get("paused", False),
            "wake": participant.get("wake", True),
            "budget": participant.get("budget") or {},
        }
        for name, participant in sorted(manifest["participants"].items())
    ]


def _count(value: object) -> int | None:
    """Reports a mail count, or None where the mailbox could not be read."""
    return value if isinstance(value, int) else None


def _row(row: dict) -> dict:
    """Reports one live view row with the identifiers its cells abbreviate."""
    return {
        "participant": row["participant"],
        "provider": row["provider_name"],
        "credential": row["credential"],
        "state": row["state"],
        "stalled": row["stalled"],
        "stall": row["stall"],
        "operator_edits": list(row["operator_edits"]),
        "last_event_at": timestamp(row["last_event_ts"] or None),
        "branch": row["branch"],
        "drift": row["drift"],
        "issues": [int(number) for number in row["owned"]],
        "offers": row["offers"],
        "unread": _count(row["unread"]),
        "pending_ack": _count(row["pending_ack"]),
        "leases": row["leases"],
        "stale_leases": row["stale_leases"],
        "lease_age_seconds": row["lease_age"],
        "injected_bytes": row["injected_bytes"],
        "hook_events": row["hook_events"],
        "denials": row["denials"],
        "calls": row["calls"],
        "errors": row["errors"],
        "tokens": row["tokens"],
        "idle_seconds": row["idle_seconds"],
        "idle_complete": row["idle_complete"],
        "over_budget": row.get("over_budget", False),
        "budget": row.get("budget") or {},
        "fit": row["fit"],
        "unfit_reason": row["unfit"] or None,
        "work_offer": row["offer_kind"] or None,
        "prompt": row["prompt"],
    }


def frame(view: dict) -> dict:
    """Reports one live view snapshot as fields rather than columns.

    Args:
        view: Snapshot produced by the dashboard collector.

    Returns:
        Server state, the window the counts cover, the totals over the
        reported rows, and one array of participants per project.
    """
    return {
        "server": {"running": view["running"]},
        "state_directory": view["home"],
        "window_seconds": view["window"] or None,
        "providers": list(view.get("providers") or []),
        "totals": dict(view["totals"]),
        "projects": [
            {
                "root": project["root"],
                "participants": [_row(row) for row in project["rows"]],
            }
            for project in view["projects"]
        ],
    }


def _measured(value: object) -> float | None:
    """Reports one measured field as a number, or None where nothing was read.

    Args:
        value: Field taken from a snapshot row.

    Returns:
        The field as a float, with a boolean reported as one or zero, or None
        where the field states that nothing could be read.
    """
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _total(rows: list[dict], key: str) -> float:
    """Sums one measured field over the rows a project reports.

    Args:
        rows: Snapshot rows the project reports.
        key: Field to sum, or the empty string to count the rows themselves.

    Returns:
        The sum over the rows, counting an unreadable field as zero so a
        project total never disappears because one lane could not be read.
    """
    if not key:
        return float(len(rows))
    return sum(_measured(row.get(key)) or 0.0 for row in rows)


def _number(value: float) -> str:
    """Formats one sample value without a decimal part it does not need."""
    return str(int(value)) if value == int(value) else repr(value)


def _label(value: object) -> str:
    """Escapes one label value for the text exposition format."""
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
    )


def _description(text: str) -> str:
    """Escapes one metric description for the text exposition format."""
    return text.replace("\\", "\\\\").replace("\n", "\\n")


def families(view: dict) -> list[dict]:
    """Reports the exported counters and gauges of one live snapshot.

    Every lane series is labelled by project, participant and provider, and
    every project series by project alone. A field that states nothing could
    be read, such as tokens on an unreadable session record, contributes no
    sample rather than a zero, so a reader never mistakes an unread value for
    a measured one.

    Args:
        view: Snapshot produced by the dashboard collector.

    Returns:
        One record per metric family carrying its ``name``, ``type``,
        ``help`` and its ``samples``, each sample carrying its ``labels`` and
        its ``value``.
    """
    reported = []
    for name, kind, key, text in LANE_METRICS:
        samples = []
        for project in view["projects"]:
            for row in project["rows"]:
                value = _measured(row.get(key))
                if value is None:
                    continue
                samples.append(
                    {
                        "labels": {
                            "project": project["root"],
                            "participant": row["participant"],
                            "provider": row["provider_name"],
                        },
                        "value": value,
                    }
                )
        reported.append(
            {"name": name, "type": kind, "help": text, "samples": samples}
        )
    for name, kind, key, text in PROJECT_METRICS:
        reported.append(
            {
                "name": name,
                "type": kind,
                "help": text,
                "samples": [
                    {
                        "labels": {"project": project["root"]},
                        "value": _total(project["rows"], key),
                    }
                    for project in view["projects"]
                ],
            }
        )
    return reported


def exposition(view: dict) -> str:
    """Formats one live snapshot in the Prometheus text exposition format.

    Each family prints its description and type once, followed by its
    samples, so a textfile collector or any scraper that reads a file parses
    the frame without a listener. A family with nothing to report still
    prints its description and type, which states that the metric exists and
    measured nothing.

    Args:
        view: Snapshot produced by the dashboard collector.

    Returns:
        The frame as exposition text ending in a newline.
    """
    lines = []
    for family in families(view):
        lines.append(f"# HELP {family['name']} {_description(family['help'])}")
        lines.append(f"# TYPE {family['name']} {family['type']}")
        for sample in family["samples"]:
            labels = ",".join(
                f'{name}="{_label(value)}"'
                for name, value in sample["labels"].items()
            )
            value = _number(sample["value"])
            lines.append(f"{family['name']}{{{labels}}} {value}")
    return "\n".join(lines) + "\n"


def measurements(view: dict) -> dict:
    """Reports the exported counters and gauges as one object.

    Args:
        view: Snapshot produced by the dashboard collector.

    Returns:
        The state directory the snapshot was read from, the window its counts
        cover, the providers it reports, and the same families the exposition
        text carries.
    """
    return {
        "state_directory": view["home"],
        "window_seconds": view["window"] or None,
        "providers": list(view.get("providers") or []),
        "metrics": families(view),
    }
