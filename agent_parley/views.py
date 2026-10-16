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
import shlex

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
        "Held reservations past their declared time to live.",
    ),
    (
        "agent_parley_lane_idle_seconds",
        "gauge",
        "idle_seconds",
        "Seconds the lane has been idle in the reported window.",
    ),
    (
        "agent_parley_lane_context_bytes_window",
        "gauge",
        "injected_bytes",
        "Coordination context bytes injected in the reported window.",
    ),
    (
        "agent_parley_lane_hook_events_window",
        "gauge",
        "hook_events",
        "Hook events recorded in the reported window.",
    ),
    (
        "agent_parley_lane_hook_denials_window",
        "gauge",
        "denials",
        "Hook events that denied an action in the reported window.",
    ),
    (
        "agent_parley_lane_served_calls_retained",
        "gauge",
        "calls",
        "Retained coordination tool calls served for this participant.",
    ),
    (
        "agent_parley_lane_served_rejections_retained",
        "gauge",
        "errors",
        "Retained coordination tool calls rejected for this participant.",
    ),
    (
        "agent_parley_lane_tokens_total",
        "counter",
        "tokens",
        "Tokens the native client counted, never billed spend.",
    ),
    (
        "agent_parley_lane_side_questions_retained",
        "gauge",
        "side_questions",
        "Side questions answered for the operator by agent-parley ask.",
    ),
    (
        "agent_parley_lane_side_question_tokens_retained",
        "gauge",
        "side_question_tokens",
        "Tokens the lane's side questions counted, part of its budget.",
    ),
    (
        "agent_parley_lane_pull_requests_opened_window",
        "gauge",
        "pull_requests_opened",
        "Pull requests first seen open in the outcome window.",
    ),
    (
        "agent_parley_lane_pull_requests_merged_window",
        "gauge",
        "pull_requests_merged",
        "Pull requests a claim resolution recorded as merged in the outcome "
        "window.",
    ),
    (
        "agent_parley_lane_pull_requests_closed_window",
        "gauge",
        "pull_requests_closed",
        "Pull requests that left the open list without a recorded merge.",
    ),
    (
        "agent_parley_lane_first_pass_ratio",
        "gauge",
        "first_pass_rate",
        "Share of merged pull requests with a known CI round count that "
        "merged after one round and no requested change.",
    ),
    (
        "agent_parley_lane_ci_rounds_unknown",
        "gauge",
        "ci_rounds_unknown",
        "Merged pull requests in the outcome window with no CI round count.",
    ),
    (
        "agent_parley_lane_ci_rounds_median",
        "gauge",
        "ci_rounds_median",
        "Median CI rounds over merged pull requests with a known count.",
    ),
    (
        "agent_parley_lane_claim_to_merge_seconds_median",
        "gauge",
        "claim_to_merge_seconds",
        "Median seconds from a claim to the merge of its pull request.",
    ),
)

NO_OUTCOMES: dict[str, int | float | None] = {
    "pull_requests_opened": 0,
    "pull_requests_merged": 0,
    "pull_requests_closed": 0,
    "first_pass": 0,
    "first_pass_known": 0,
    "first_pass_rate": None,
    "ci_rounds_unknown": 0,
    "ci_rounds_median": None,
    "claim_to_merge_seconds": None,
}

DENIAL_CAUSES = "agent_parley_lane_hook_denials_by_cause_window"

DEPRECATED_ALIASES: dict[str, str] = {
    "agent_parley_lane_context_bytes_window": (
        "agent_parley_lane_context_bytes_total"
    ),
    "agent_parley_lane_hook_events_window": (
        "agent_parley_lane_hook_events_total"
    ),
    "agent_parley_lane_hook_denials_window": (
        "agent_parley_lane_hook_denials_total"
    ),
    "agent_parley_lane_served_calls_retained": (
        "agent_parley_lane_served_calls_total"
    ),
    "agent_parley_lane_served_rejections_retained": (
        "agent_parley_lane_served_rejections_total"
    ),
    "agent_parley_lane_pull_requests_opened_window": (
        "agent_parley_lane_pull_requests_opened_total"
    ),
    "agent_parley_lane_pull_requests_merged_window": (
        "agent_parley_lane_pull_requests_merged_total"
    ),
    "agent_parley_lane_pull_requests_closed_window": (
        "agent_parley_lane_pull_requests_closed_total"
    ),
    DENIAL_CAUSES: "agent_parley_lane_hook_denials_by_cause_total",
    "agent_parley_project_context_bytes_window": (
        "agent_parley_project_context_bytes_total"
    ),
    "agent_parley_project_hook_events_window": (
        "agent_parley_project_hook_events_total"
    ),
    "agent_parley_project_hook_denials_window": (
        "agent_parley_project_hook_denials_total"
    ),
}

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
        "agent_parley_project_context_bytes_window",
        "gauge",
        "injected_bytes",
        "Context bytes injected across the project's reported lanes.",
    ),
    (
        "agent_parley_project_hook_events_window",
        "gauge",
        "hook_events",
        "Hook events recorded across the project's reported lanes.",
    ),
    (
        "agent_parley_project_hook_denials_window",
        "gauge",
        "denials",
        "Denials recorded across the project's reported lanes.",
    ),
    (
        "agent_parley_project_pull_requests_excluded",
        "gauge",
        "pull_requests_excluded",
        "Pull requests in the outcome window that no lane owns.",
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
    """Reports one takeover or operator request the owner has not answered.

    Past its deadline the request is not granted by this reading alone; the
    supervisor's grace window still decides that. The deadline only makes an
    unanswered request read as overdue, the same way an unanswered offer does.
    """
    if not value:
        return None
    waiting = issues_state.offer_state(value)
    return {
        "offer_id": value["id"],
        "to": value["to"],
        "source": value.get("source") or issues_state.OPERATOR,
        "reason": value.get("reason", ""),
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
        path is included. The platform record names the kernel release, the
        WSL generation or `none`, and whether `pidfd_open` is available.
        `plugins` lists one record per CLI and config home with its `cli`,
        its `home` label (`default`, `shell` or a profile name, never a
        path), the installed `version`, the launcher version it is
        `expected` to match and its `state`; `server` carries the running
        service's `version`, empty when none answers, and the `expected`
        launcher version.
    """
    return {
        "protocol": reported["protocol"],
        "supported": reported["supported"],
        "store_schema": reported["schema"],
        "platform": reported["platform"],
        "components": reported["components"],
        "plugins": reported["plugins"],
        "server": reported["server"],
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
        "satisfied": [
            {"issue": issue, "waits_on": blocker}
            for issue, blocker in reported.get("satisfied", [])
        ],
        "unlisted": [
            {"issue": issue, "waits_on": blocker}
            for issue, blocker in reported["unlisted"]
        ],
        "revoked": [
            {"issue": issue, "waits_on": blocker, "proposal": proposal}
            for issue, blocker, proposal in reported["revoked"]
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
        accepted through, any unanswered operator request to its owner, any
        unanswered completion reminder, the orphan marker its owner carries
        with the reservations that owner still holds, and the take an earlier
        orphaned claim was moved by.
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
                "orphan": (
                    {
                        "owner": orphan["owner"],
                        "reason": orphan["reason"],
                        "reservations": list(orphan.get("reservations", [])),
                        "created_at": timestamp(orphan.get("created")),
                    }
                    if (orphan := record.get("orphan"))
                    else None
                ),
                "taken": (
                    {
                        "from": taken["from"],
                        "reason": taken["reason"],
                        "at": timestamp(taken.get("at")),
                    }
                    if (taken := record.get("taken"))
                    else None
                ),
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
        history does not invent one. ``truncated`` is true when the row cap
        omitted older matching mail or reservations.
    """
    return {
        "subject": reported["subject"],
        "value": reported["value"],
        "truncated": bool(reported.get("truncated")),
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


def usage(reading: dict) -> dict:
    """Reports per-claim usage with RFC 3339 instants.

    Args:
        reading: Report produced by ``history.usage``.

    Returns:
        The window floor, null for all recorded history, one row per
        resolved claim and the totals. ``estimated_spend`` is present only
        where the operator recorded a price, and is an estimate.
    """
    return {
        "since": timestamp(reading["since"]) if reading["since"] else None,
        "rows": [
            {**row, "at": timestamp(row["at"])} for row in reading["rows"]
        ],
        "totals": reading["totals"],
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
            "retired_at": timestamp(participant.get("retired")),
            "budget": participant.get("budget") or {},
        }
        for name, participant in sorted(manifest["participants"].items())
    ]


def issue_detail(
    state: dict, number: int, reservations: list[str], reported: dict
) -> dict:
    """Reports one issue with its ownership, its blockers and its history.

    Args:
        state: Published issue ledger.
        number: Issue the reading was taken for.
        reservations: Advisory reservation keys the current owner holds.
        reported: History reading for the same issue.

    Returns:
        The ledger record for that issue, or None where the ledger has never
        recorded it, beside the reservations its owner holds and every
        recorded record and ownership generation.
    """
    records = issues(state)
    found = [record for record in records if record["issue"] == number]
    return {
        "issue": number,
        "revision": state["revision"],
        "record": found[0] if found else None,
        "reservations": list(reservations),
        "history": history(reported),
    }


def participant_detail(record: dict, participant: dict) -> dict:
    """Reports one lane's reading beside the roster entry that defines it.

    Args:
        record: One lane record from the status reading.
        participant: Roster entry for the same participant.

    Returns:
        Every field the status reading carries for that lane, with the
        worktree, the advisory limits, the wake setting and the last launch
        timing the manifest records. No credential value is reported; a
        profile name is not a credential.
    """
    return {
        **record,
        "worktree": participant["lane"],
        "budget_limits": participant.get("budget") or {},
        "wake_enabled": participant.get("wake", True),
        "launch_timing": participant.get("launch") or {},
    }


def provider_detail(name: str, entry: dict) -> dict:
    """Reports one provider definition with the name it is selected by."""
    return {"provider": name, **entry}


def redacted(values: dict) -> list[str]:
    """Reports environment overrides by name, never by value.

    Args:
        values: Recorded overrides, keyed by environment variable name.

    Returns:
        One ``NAME=<redacted>`` entry per override in name order, so a
        profile can be read on a shared terminal without printing what it
        carries.
    """
    return [f"{name}=<redacted>" for name in sorted(values)]


def credential_detail(name: str, entry: dict) -> dict:
    """Reports one account profile with every recorded value redacted.

    Args:
        name: Profile name used by ``run --credentials``.
        entry: Stored profile.

    Returns:
        The config home the native CLI signs in under, the override names the
        launcher sets, and the variables it requires from the caller's shell.
        No recorded value is reported. A profile recorded before empty
        profiles were refused sets none of the three and launches on the
        default account, so it also carries ``account`` reading ``selects no
        account``.
    """
    shown = {
        "credential": name,
        "config_home": entry.get("home", ""),
        "env": redacted(entry.get("env") or {}),
        "require_env": list(entry.get("require_env") or []),
    }
    if not (shown["config_home"] or shown["env"] or shown["require_env"]):
        shown["account"] = "selects no account"
    return shown


def setup_text(manifest: dict, suggestion: list[str] | None = None) -> str:
    """Summarizes a registered project as labeled lines a reader can act on.

    An empty merge gate is stated rather than left silent, because
    ``participant merge`` then runs no check at all.

    Args:
        manifest: Project manifest ``setup`` created or verified.
        suggestion: Check command the repository's committed files declare,
            printed as the ``verify set`` call to record it while the gate
            is empty; None when nothing was detected.

    Returns:
        Root, abbreviated base commit, forge, merge gate and lane
        initialization, the suggested ``verify set`` call where one applies,
        then the command that starts the first lane.
    """
    verify = manifest.get("verify") or []
    initialize = manifest.get("initialize") or []
    lines = [
        f"Root: {manifest['root']}",
        f"Base: {manifest['base'][:12]}",
        f"Forge: {manifest.get('forge') or 'none'}",
        "Verify: "
        + (
            shlex.join(verify)
            if verify
            else "none; participant merge runs no gate"
        ),
    ]
    if not verify and suggestion:
        lines.append(
            f'Suggested: agent-parley verify set "{shlex.join(suggestion)}"'
        )
    lines += [
        f"Init: {shlex.join(initialize) if initialize else 'none'}",
        "Next: agent-parley run claude",
    ]
    return "\n".join(lines)


def _settings(entry: dict) -> list[str]:
    """Names the environment a definition sets and requires, never values."""
    shown = []
    if names := sorted(entry.get("env") or {}):
        shown.append(f"sets {', '.join(names)}")
    if required := list(entry.get("require_env") or []):
        shown.append(f"requires {', '.join(required)}")
    return shown


def credential_text(name: str, entry: dict) -> str:
    """Confirms one defined account profile without printing its values.

    Args:
        name: Profile name used by ``run --credentials``.
        entry: Stored profile.

    Returns:
        One sentence naming the config home and the environment variables
        the profile sets and requires, then the command that launches with
        it.
    """
    parts = [f"config home {entry['home']}"] if entry.get("home") else []
    parts += _settings(entry)
    detail = f" ({'; '.join(parts)})" if parts else ""
    return (
        f"Defined credential profile {name}{detail}.\n"
        f"Next: agent-parley run claude --credentials {name}"
    )


def provider_text(name: str, entry: dict) -> str:
    """Confirms one defined provider in a sentence and a next command.

    Args:
        name: Provider name used by ``run --provider``.
        entry: Stored provider definition.

    Returns:
        The adapter and executable the provider launches, the environment
        variables it sets and requires, then the command that prints it.
    """
    parts = [f"{entry['adapter']} adapter", f"runs {entry['command']}"]
    parts += _settings(entry)
    return (
        f"Defined provider {name} ({'; '.join(parts)}).\n"
        f"Next: agent-parley provider show {name}"
    )


def resolution(result: dict) -> dict:
    """Reports an operator resolution with its observation time in RFC 3339.

    Args:
        result: Outcome ``issue resolve`` returned.

    Returns:
        The same fields, with the forge evidence's ``observed_at`` rendered
        the way every other document renders a time.
    """
    evidence = dict(result.get("evidence") or {})
    if "observed_at" in evidence:
        evidence["observed_at"] = timestamp(evidence["observed_at"])
    return {**result, "evidence": evidence}


def resolution_text(result: dict) -> str:
    """Says what ``issue resolve`` ended, on which forge evidence.

    Args:
        result: Outcome ``issue resolve`` returned.

    Returns:
        One sentence naming the issue, the outcome recorded, the former
        holder and the pull request state, then the command that shows it.
    """
    evidence = result.get("evidence") or {}
    ended = (
        "returned to the queue"
        if result["outcome"] == "release"
        else "recorded complete"
    )
    return (
        f"Resolved issue #{result['issue']}: {ended}, ending "
        f"{result['holder']}'s claim on {evidence.get('branch', '')} "
        f"({str(evidence.get('state', '')).lower()}).\n"
        f"Next: agent-parley issue show {result['issue']}"
    )


def recovery(approval: dict) -> dict:
    """Reports a recovery approval with its approval time in RFC 3339."""
    return {
        **approval,
        "authorized_at": timestamp(approval.get("authorized_at")),
    }


def recovery_text(approval: dict) -> str:
    """Says which live claim ``issue recover`` approved stopping.

    Args:
        approval: Approval ``issue recover`` recorded.

    Returns:
        One sentence naming the issue and its owner, and that the approval
        alone moves nothing until a matching capacity observation arrives.
    """
    return (
        f"Recovery approved for issue #{approval['issue']}: "
        f"{approval['owner']}'s claim stops once a matching capacity "
        "observation is published; nothing has moved yet."
    )


def _count(value: object) -> int | None:
    """Reports a mail count, or None where the mailbox could not be read."""
    return value if isinstance(value, int) else None


def _row(row: dict) -> dict:
    """Reports one live view row with the identifiers its cells abbreviate."""
    from agent_parley.status import bucket_of

    return {
        "participant": row["participant"],
        "bucket": dict(bucket_of(row)),
        "recap": row.get("recap"),
        "provider": row["provider_name"],
        "credential": row["credential"],
        "state": row["state"],
        "stalled": row["stalled"],
        "stall": row["stall"],
        "operator_edits": list(row["operator_edits"]),
        "base_advance_paths": list(row["base_advance_paths"]),
        "base_advance_told_at": timestamp(row["base_advance_told_at"]),
        "last_event_at": timestamp(row["last_event_ts"] or None),
        "branch": row["branch"],
        "drift": row["drift"],
        "review": row["review"] or None,
        "reviewer": row["reviewer"] or None,
        "reviewed_report": row["reviewed_report"] or None,
        "issues": [int(number) for number in row["owned"]],
        "offers": row["offers"],
        "unread": _count(row["unread"]),
        "pending_ack": _count(row["pending_ack"]),
        "leases": row["leases"],
        "stale_leases": row["stale_leases"],
        "lease_age_seconds": row["lease_age"],
        "stale_lease_age_seconds": row.get("stale_lease_age", 0),
        "queued_requests": row["queued"],
        "queued_by": list(row["queued_by"]),
        "injected_bytes": row["injected_bytes"],
        "injected_bytes_per_hour": row.get("injected_per_hour", 0),
        "hook_events": row["hook_events"],
        "denials": row["denials"],
        "denied_by": [dict(item) for item in row.get("denied_by") or []],
        "calls": row["calls"],
        "errors": row["errors"],
        "tokens": row["tokens"],
        "unmeasured": list(row.get("unmeasured") or []),
        "idle_seconds": row["idle_seconds"],
        "idle_complete": row["idle_complete"],
        "over_budget": row.get("over_budget", False),
        "budget": row.get("budget") or {},
        "fit": row["fit"],
        "unfit_reason": row["unfit"] or None,
        "work_offer": row["offer_kind"] or None,
        "work_dispatch": dict(row.get("work_dispatch") or {}),
        "pull_requests": {
            key: row.get(key, value) for key, value in NO_OUTCOMES.items()
        },
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
    every project series by project alone; a project series the project
    itself carries, such as its excluded pull requests, is read from the
    project rather than summed over its rows. Denials are also counted by the
    reason class and the tool that drew them. A field that states nothing could
    be read, such as tokens on an unreadable session record, contributes no
    sample rather than a zero, so a reader never mistakes an unread value for
    a measured one.

    Args:
        view: Snapshot produced by the dashboard collector.

    Returns:
        One record per metric family carrying its ``name``, ``type``,
        ``help`` and its ``samples``, each sample carrying its ``labels`` and
        its ``value``. Each renamed family is followed by its deprecated
        0.18.x alias, as ``_with_aliases`` describes.
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
    reported.append(
        {
            "name": DENIAL_CAUSES,
            "type": "gauge",
            "help": "Hook denials in the reported window by reason and tool.",
            "samples": [
                {
                    "labels": {
                        "project": project["root"],
                        "participant": row["participant"],
                        "provider": row["provider_name"],
                        "reason": item["reason"],
                        "tool": item["tool"],
                    },
                    "value": item["count"],
                }
                for project in view["projects"]
                for row in project["rows"]
                for item in row.get("denied_by") or []
            ],
        }
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
                        "value": (
                            float(project[key] or 0)
                            if key in project
                            else _total(project["rows"], key)
                        ),
                    }
                    for project in view["projects"]
                ],
            }
        )
    return _with_aliases(reported)


def _with_aliases(reported: list[dict]) -> list[dict]:
    """Adds the deprecated 0.18.x name after each family that replaced one.

    0.19.0 renamed twelve counters to gauges suffixed ``_window`` or
    ``_retained``. Each old name is exported again, typed ``counter`` as
    0.18.x typed it, with the same labels and values as its replacement, so a
    dashboard written against 0.18.x keeps reading data. Removing an alias
    needs a release marked breaking.

    Args:
        reported: Families in export order, under their current names.

    Returns:
        The same families, each replaced one followed by its alias, which
        carries ``deprecated`` and the ``replacement`` it stands in for.
    """
    expanded = []
    for family in reported:
        expanded.append(family)
        alias = DEPRECATED_ALIASES.get(family["name"])
        if alias is None:
            continue
        expanded.append(
            {
                "name": alias,
                "type": "counter",
                "help": (
                    f"Deprecated: use {family['name']}; removal needs a "
                    f"release marked breaking. {family['help']}"
                ),
                "samples": family["samples"],
                "deprecated": True,
                "replacement": family["name"],
            }
        )
    return expanded


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
