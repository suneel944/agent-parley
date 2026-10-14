"""Answers ownership questions over the records coordination already keeps.

`status` says who owns an issue now and `events export` writes retained hook
events, but neither answers "who has held issue 42, for how long each, and
through which handoffs", "what did this lane file this week", or "show every
claim, reservation, message and report that belongs to one piece of work".
Those answers already exist across three substrates: the issue ledger, the
per-lane report log, and the store's mail and reservations.

Reading is the whole contract. Every query here opens the store read-only,
takes no lock, writes nothing and changes no ownership. Where a record predates
the correlation it would need, it is reported with an unknown claim rather than
being given an invented one, because a fabricated correlation is worse than an
honest gap.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from agent_parley import issues, metrics, roster, store
from agent_parley.state import BridgeError

KINDS = (
    "claim",
    "handoff",
    "report",
    "approval",
    "reservation",
    "message",
)
CLAIM_ACTIONS = frozenset({"claim", "release", "take"})
HANDOFF_ACTIONS = frozenset(
    {
        "offer",
        "accept",
        "decline",
        "cancel",
        "assign",
        "unassign",
        "authorize",
        "refuse",
    }
)
MAX_RECORDS = 500
_ENDING_ACTIONS = frozenset(
    {"claim", "accept", "take", "release", "complete", "resolve"}
)


def _provider(manifest: dict, participant: str | None) -> str:
    """Names the provider driving one participant, when it is still known."""
    entry = manifest["participants"].get(participant or "")
    return str(entry["provider"]) if entry else ""


def spent(usage: dict) -> str:
    """Formats what one claim used, naming an unknown figure ``n/a``.

    Args:
        usage: Tokens, calls and hours a resolution recorded.

    Returns:
        Text such as ``tokens 412,000, calls 1,830, 2.4 h``.
    """
    tokens, calls = usage.get("tokens"), usage.get("calls")
    hours = usage.get("hours")
    return (
        f"tokens {'n/a' if tokens is None else f'{tokens:,}'}, "
        f"calls {'n/a' if calls is None else f'{calls:,}'}, "
        f"{'n/a' if hours is None else f'{float(hours):g}'} h"
    )


def _detail(action: str, number: str, entry: dict) -> str:
    """Names one ledger transition, carrying any operator-stated reason."""
    detail = f"{action} #{number}"
    pending = entry.get("offer") or entry.get("request") or {}
    reason = pending.get("reason")
    if reason:
        detail = f"{detail}: {reason}"
    if isinstance(entry.get("usage"), dict):
        detail = f"{detail}; {spent(entry['usage'])}"
    return detail


def _ledger_records(directory: Path, manifest: dict) -> list[dict]:
    """Reads claims, handoffs and dependency edges from the issue ledger."""
    records = []
    for number, record in issues.snapshot(directory)["issues"].items():
        for entry in record.get("history", []):
            action = str(entry.get("action", ""))
            if action in CLAIM_ACTIONS:
                kind = "claim"
            elif action in HANDOFF_ACTIONS:
                kind = "handoff"
            else:
                kind = "dependency"
            records.append(
                {
                    "kind": kind,
                    "action": action,
                    "at": float(entry.get("at", 0) or 0),
                    "participant": entry.get("actor"),
                    "provider": _provider(manifest, entry.get("actor")),
                    "issue": int(number),
                    "claim_id": entry.get("claim_id"),
                    "owner": entry.get("owner"),
                    "detail": _detail(action, number, entry),
                    **(
                        {"usage": entry["usage"]}
                        if isinstance(entry.get("usage"), dict)
                        else {}
                    ),
                }
            )
    return records


def holdings(directory: Path, number: str) -> list[dict]:
    """Reports each ownership generation of one issue and how long it lasted.

    Args:
        directory: Private state directory for the common repository.
        number: Repository issue number.

    Returns:
        One record per generation, naming the participant, the claim
        identifier, when the generation started, when it ended, and how long
        it was held. A claim, accept or take ends the generation before it
        and starts its own, because ownership moved without the previous
        holder recording a release; a release, complete or resolve ends the
        open generation. When capped history no longer names the record's
        current owner, a generation for that owner is added from the start
        of the current claim, so the answer always names the live holder. A
        generation that is still held reports no end and the seconds so far.
    """
    record = issues.snapshot(directory)["issues"].get(number)
    if not record:
        return []
    held: list[dict] = []
    for entry in record.get("history", []):
        action = str(entry.get("action", ""))
        at = float(entry.get("at", 0) or 0)
        if action in _ENDING_ACTIONS and held and held[-1]["ended"] is None:
            held[-1].update(ended=at, seconds=int(at - held[-1]["started"]))
        if action in ("claim", "accept", "take"):
            held.append(
                {
                    "participant": entry.get("owner") or entry.get("actor"),
                    "claim_id": entry.get("claim_id"),
                    "started": at,
                    "ended": None,
                    "seconds": 0,
                }
            )
    owner = record.get("owner")
    current = held[-1] if held and held[-1]["ended"] is None else None
    if owner and (current is None or current["participant"] != owner):
        started = issues.claimed_since(record)
        if current is not None:
            current.update(
                ended=started, seconds=int(started - current["started"])
            )
        held.append(
            {
                "participant": owner,
                "claim_id": record.get("claim_id"),
                "started": started,
                "ended": None,
                "seconds": 0,
            }
        )
    stamp = time.time()
    for generation in held:
        if generation["ended"] is None:
            generation["seconds"] = int(stamp - generation["started"])
    return held


def _decided_report(record: dict) -> str:
    """Returns the report identifier an operator decision was bound to."""
    binding = record.get("binding")
    if str(record.get("kind", "")) != "approval":
        return ""
    return str(binding.get("report", "")) if isinstance(binding, dict) else ""


def _decided(record: dict) -> str:
    """Describes one recorded operator decision for the chain."""
    reason = str(record.get("reason", "")).strip()
    who = record.get("operator", "unknown")
    described = f"{record.get('decision', '')} by {who}"
    return f"{described}: {reason}" if reason else described


def _report_records(directory: Path, manifest: dict) -> list[dict]:
    """Reads the durable report, decision and integration log of every lane."""
    records = []
    for name in manifest["participants"]:
        for record in metrics.report_records(directory, name):
            kind = str(record.get("kind", ""))
            detail = f"integrated by {record.get('action', '')}"
            if kind == "report":
                detail = f"reported {record.get('state', '')}"
            elif kind == "approval":
                detail = _decided(record)
            elif kind == "unattended":
                detail = f"unattended integration {record.get('outcome', '')}"
            records.append(
                {
                    "kind": "approval" if kind == "approval" else "report",
                    "action": (
                        record.get("state")
                        or record.get("decision")
                        or record.get("action", "")
                    ),
                    "at": float(record.get("at", 0) or 0),
                    "participant": name,
                    "provider": _provider(manifest, name),
                    "issue": record.get("issue"),
                    "claim_id": record.get("claim_id"),
                    "report_id": _decided_report(record) or record.get("id"),
                    "detail": detail,
                }
            )
    return records


def _scope(
    alias: str, manifest: dict, participant: str, claim: str, since: float
) -> tuple[str, list]:
    """Builds the store filter that runs before the row cap applies.

    Args:
        alias: Table alias of the reservation or message row.
        manifest: Project manifest holding the roster.
        participant: Lane to report; every lane when empty.
        claim: Claim identifier to report; every claim when empty.
        since: Unix time floor; no floor when zero.

    Returns:
        The ``WHERE`` clause through its row limit, and its parameters. The
        limit is one row above ``MAX_RECORDS`` so a caller can tell a capped
        result from one that fits exactly.
    """
    clause = "WHERE p.human_key=?"
    parameters: list = [manifest["root"]]
    if participant:
        entry = manifest["participants"].get(participant) or {}
        clause += " AND a.name=?"
        parameters.append(entry.get("display", participant))
    if claim:
        clause += f" AND {alias}.claim_id=?"
        parameters.append(claim)
    if since:
        clause += f" AND unixepoch({alias}.created_ts)>=?"
        parameters.append(since)
    parameters.append(MAX_RECORDS + 1)
    return f"{clause} ORDER BY {alias}.id DESC LIMIT ?", parameters


def _store_records(
    home: Path,
    manifest: dict,
    *,
    kinds: tuple[str, ...] = (),
    participant: str = "",
    claim: str = "",
    since: float = 0.0,
) -> tuple[list[dict], bool]:
    """Reads reservations and mail for one project, correlated where known.

    The lane, claim and time filters run in SQL before each source is capped
    at ``MAX_RECORDS`` rows, so one lane's rows are never crowded out by rows
    other lanes wrote later.

    Args:
        home: Private bridge state root.
        manifest: Project manifest holding the roster.
        kinds: Record kinds to read; every kind when empty.
        participant: Lane to report; every lane when empty.
        claim: Claim identifier to report; every claim when empty.
        since: Unix time floor; no floor when zero.

    Returns:
        The records, and whether any source had more rows than the cap.
    """
    if not store.present(home):
        return [], False
    lanes = {
        participant["display"]: name
        for name, participant in manifest["participants"].items()
    }
    records: list[dict] = []
    with store.connect(home) as db:
        rows = []
        if not kinds or "reservation" in kinds:
            clause, parameters = _scope(
                "f", manifest, participant, claim, since
            )
            rows = db.execute(
                "SELECT a.name AS identity,f.path_pattern,f.claim_id,"
                "unixepoch(f.created_ts) AS created,"
                "unixepoch(f.released_ts) AS released "
                "FROM file_reservations f "
                "JOIN agents a ON a.id=f.agent_id "
                "JOIN projects p ON p.id=a.project_id " + clause,
                parameters,
            ).fetchall()
        truncated = len(rows) > MAX_RECORDS
        for row in rows[:MAX_RECORDS]:
            name = lanes.get(row["identity"], row["identity"])
            records.append(
                {
                    "kind": "reservation",
                    "action": "released" if row["released"] else "held",
                    "at": float(row["created"] or 0),
                    "participant": name,
                    "provider": _provider(manifest, name),
                    "issue": None,
                    "claim_id": row["claim_id"],
                    "path": row["path_pattern"],
                    "detail": f"reserved {row['path_pattern']}",
                }
            )
        rows = []
        if not kinds or "message" in kinds:
            clause, parameters = _scope(
                "m", manifest, participant, claim, since
            )
            rows = db.execute(
                "SELECT m.id,a.name AS identity,m.thread_id,m.claim_id,"
                "substr(m.subject,1,80) AS subject,"
                "unixepoch(m.created_ts) AS created FROM messages m "
                "JOIN agents a ON a.id=m.sender_id "
                "JOIN projects p ON p.id=a.project_id " + clause,
                parameters,
            ).fetchall()
        truncated = truncated or len(rows) > MAX_RECORDS
        for row in rows[:MAX_RECORDS]:
            name = lanes.get(row["identity"], row["identity"])
            records.append(
                {
                    "kind": "message",
                    "action": "sent",
                    "at": float(row["created"] or 0),
                    "participant": name,
                    "provider": _provider(manifest, name),
                    "issue": None,
                    "claim_id": row["claim_id"],
                    "message_id": row["id"],
                    "thread_id": row["thread_id"],
                    "detail": row["subject"],
                }
            )
    return records, truncated


def query(
    home: Path,
    directory: Path,
    manifest: dict,
    *,
    kinds: tuple[str, ...] = (),
    participant: str = "",
    provider: str = "",
    issue: str = "",
    claim: str = "",
    since: float = 0.0,
) -> tuple[list[dict], bool]:
    """Returns matching coordination events and whether a cap omitted any.

    Takes the same arguments as :func:`records`. Mail and reservations are
    read at most ``MAX_RECORDS`` rows per source after the lane, claim and
    time filters apply, so the flag means older matching rows exist.

    Returns:
        The records of :func:`records`, and whether the store held more
        matching rows than the cap let through.
    """
    try:
        collected = [
            *_ledger_records(directory, manifest),
            *_report_records(directory, manifest),
        ]
        stored, truncated = _store_records(
            home,
            manifest,
            kinds=kinds,
            participant=participant,
            claim=claim,
            since=since,
        )
    except (BridgeError, OSError, sqlite3.Error) as exc:
        raise BridgeError(
            f"Coordination history is unavailable: {exc}"
        ) from exc
    selected = [
        record
        for record in [*collected, *stored]
        if (not kinds or record["kind"] in kinds)
        and (not participant or record["participant"] == participant)
        and (not provider or record["provider"] == provider)
        and (not issue or str(record["issue"] or "") == issue)
        and (not claim or record["claim_id"] == claim)
        and record["at"] >= since
    ]
    return sorted(selected, key=lambda record: record["at"]), truncated


def records(
    home: Path,
    directory: Path,
    manifest: dict,
    *,
    kinds: tuple[str, ...] = (),
    participant: str = "",
    provider: str = "",
    issue: str = "",
    claim: str = "",
    since: float = 0.0,
) -> list[dict]:
    """Returns every recorded coordination event matching the filters.

    Args:
        home: Private bridge state root.
        directory: Private state directory for the common repository.
        manifest: Project manifest holding the roster.
        kinds: Record kinds to report; every kind when empty.
        participant: Lane to report; every lane when empty.
        provider: Provider to report; every provider when empty.
        issue: Issue number to report; every issue when empty.
        claim: Claim identifier to report; every claim when empty.
        since: Unix time floor; older records are omitted.

    Returns:
        Matching records, oldest first. A record that carries no claim
        identifier because it predates the correlation is reported with a null
        claim rather than being attached to a claim it never named.
    """
    return query(
        home,
        directory,
        manifest,
        kinds=kinds,
        participant=participant,
        provider=provider,
        issue=issue,
        claim=claim,
        since=since,
    )[0]


def _claim(claim_id: str | None) -> str:
    """Formats the claim suffix a history line carries.

    Args:
        claim_id: Identifier correlating the row with a claim, if any.

    Returns:
        The rendered suffix, or an empty string when the row names no
        claim, so that message and reservation rows do not report an
        unknown claim they never had.
    """
    return f"; claim {claim_id}" if claim_id else ""


def describe(
    reported: list[dict],
    held: list[dict] | None = None,
    *,
    truncated: bool = False,
) -> str:
    """Formats history for a terminal, oldest first.

    Args:
        reported: Records produced by :func:`records`.
        held: Optional ownership generations to summarize first.
        truncated: Whether the row cap omitted older matching records.

    Returns:
        One line per generation and per record, or a notice that the query
        matched nothing, followed by a notice when older records were
        omitted.
    """
    lines = []
    for generation in held or []:
        ended = "still held" if generation["ended"] is None else "released"
        lines.append(
            f"{generation['participant']} held it for "
            f"{generation['seconds']}s ({ended})"
            f"{_claim(generation['claim_id'])}"
        )
    for record in reported:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(record["at"]))
        issue = f" #{record['issue']}" if record["issue"] else ""
        lines.append(
            f"{stamp} {record['kind']}{issue} "
            f"{record['participant'] or 'unknown'}: {record['detail']}"
            f"{_claim(record['claim_id'])}"
        )
    described = "\n".join(lines) or "No matching history."
    if truncated:
        described += "\nOlder records omitted; narrow with --since."
    return described


def _price(home: Path, provider: str) -> float | None:
    """Returns the operator's recorded price per million tokens, if any."""
    try:
        entry = roster.provider(home, provider)
    except BridgeError:
        return None
    value = (entry.get("price") or {}).get("per_mtok")
    return float(value) if isinstance(value, (int, float)) else None


def usage(
    home: Path, directory: Path, manifest: dict, since: float = 0.0
) -> dict:
    """Reports what each resolved claim used, with its pull request outcome.

    Every resolution the issue ledger still holds is one row: the issue,
    the lane that held it and its provider, the outcome, the pull request
    the forge evidence named and its state, and the tokens, calls and hours
    the resolution recorded. A resolution recorded before usage was kept,
    or for a provider with no token reader, reports those figures as null
    rather than zero.

    Money appears only where the operator recorded a price for the lane's
    provider with ``provider price``. It is the recorded token count times
    that price, an estimate and never a bill: the count is the client's own,
    one figure for input and output alike.

    Args:
        home: Private bridge state root.
        directory: Private state directory for the common repository.
        manifest: Project manifest holding the roster.
        since: Unix time floor; older resolutions are omitted.

    Returns:
        ``rows`` oldest first and ``totals`` summing each known figure.
        ``estimated_spend`` appears on a row and in the totals only when a
        price applies to at least one row.
    """
    rows = []
    prices: dict[str, float | None] = {}
    for number, record in issues.snapshot(directory)["issues"].items():
        for entry in record.get("history", []):
            at = float(entry.get("at", 0) or 0)
            if entry.get("action") != "resolve" or at < since:
                continue
            used = entry.get("usage") or {}
            evidence = entry.get("evidence") or {}
            holder = str(entry.get("holder") or "")
            provider = _provider(manifest, holder)
            row = {
                "issue": int(number),
                "title": record.get("title") or "",
                "claim_id": entry.get("claim_id"),
                "participant": holder,
                "provider": provider,
                "outcome": entry.get("outcome", ""),
                "at": at,
                "pull_request": evidence.get("pull_request"),
                "pull_request_state": evidence.get("state", ""),
                "tokens": used.get("tokens"),
                "calls": used.get("calls"),
                "hours": used.get("hours"),
            }
            if provider not in prices:
                prices[provider] = _price(home, provider) if provider else None
            price = prices[provider]
            if price is not None and row["tokens"] is not None:
                row["estimated_spend"] = round(
                    row["tokens"] * price / 1_000_000, 2
                )
            rows.append(row)
    rows.sort(key=lambda row: row["at"])
    totals: dict = {
        field: sum(row[field] for row in rows if row[field] is not None)
        for field in ("tokens", "calls", "hours")
    }
    totals["hours"] = round(totals["hours"], 2)
    priced = [
        row["estimated_spend"] for row in rows if "estimated_spend" in row
    ]
    if priced:
        totals["estimated_spend"] = round(sum(priced), 2)
    return {"since": since, "rows": rows, "totals": totals}


def describe_usage(reading: dict) -> str:
    """Formats a usage report for a terminal.

    Args:
        reading: Report produced by :func:`usage`.

    Returns:
        One line per resolved claim and a totals line, or a notice that no
        claim was resolved in the window. Spend is labeled an estimate.
    """
    if not reading["rows"]:
        return "No claim was resolved in this window."
    lines = []
    for row in reading["rows"]:
        pull = (
            f"PR #{row['pull_request']} {row['pull_request_state'].lower()}"
            if row["pull_request"]
            else "no PR"
        )
        money = (
            f", est. ${row['estimated_spend']:,.2f}"
            if "estimated_spend" in row
            else ""
        )
        lines.append(
            f"#{row['issue']} {row['participant'] or 'unknown'} "
            f"({row['provider'] or 'unknown'}) {row['outcome']}, {pull}: "
            f"{spent(row)}{money}"
        )
    totals = reading["totals"]
    money = (
        f", est. ${totals['estimated_spend']:,.2f} (an estimate from "
        "recorded prices, not a bill)"
        if "estimated_spend" in totals
        else ""
    )
    lines.append(f"Total: {spent(totals)}{money}")
    return "\n".join(lines)
