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
    """Reports one pending handoff offer with its identifier and deadline."""
    if not value:
        return None
    waiting = issues_state.offer_state(value)
    return {
        "offer_id": value["id"],
        "to": value["to"],
        "summary": value["summary"],
        "created_at": timestamp(value.get("created")),
        "deadline_at": timestamp(waiting["deadline"]),
        "overdue": waiting["overdue"],
        "overdue_seconds": waiting["overdue_seconds"],
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
        path is included.
    """
    return {
        "protocol": reported["protocol"],
        "supported": reported["supported"],
        "store_schema": reported["schema"],
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
        groups as an array, and every edge recorded by hand after the apply.
    """
    return {
        "plan": applied["plan"],
        "digest": applied["digest"],
        "applied_by": applied["applied_by"],
        "applied_at": timestamp(applied["applied_at"]),
        "versions": applied["versions"],
        "issues": applied["issues"],
        "groups": [
            {"group": name, "issues": members}
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
        identifier, and any unanswered completion reminder.
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
