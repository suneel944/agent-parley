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

from agent_parley import issues, metrics, store
from agent_parley.state import BridgeError

KINDS = (
    "claim",
    "handoff",
    "report",
    "approval",
    "reservation",
    "message",
)
CLAIM_ACTIONS = frozenset({"claim", "release"})
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


def _provider(manifest: dict, participant: str | None) -> str:
    """Names the provider driving one participant, when it is still known."""
    entry = manifest["participants"].get(participant or "")
    return str(entry["provider"]) if entry else ""


def _detail(action: str, number: str, entry: dict) -> str:
    """Names one ledger transition, carrying any operator-stated reason."""
    detail = f"{action} #{number}"
    pending = entry.get("offer") or entry.get("request") or {}
    reason = pending.get("reason")
    return f"{detail}: {reason}" if reason else detail


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
        it was held. A generation that is still held reports no end and the
        seconds so far.
    """
    record = issues.snapshot(directory)["issues"].get(number)
    if not record:
        return []
    held: list[dict] = []
    for entry in record.get("history", []):
        action = str(entry.get("action", ""))
        at = float(entry.get("at", 0) or 0)
        if action in ("claim", "accept"):
            held.append(
                {
                    "participant": entry.get("actor"),
                    "claim_id": entry.get("claim_id"),
                    "started": at,
                    "ended": None,
                    "seconds": 0,
                }
            )
        elif action == "release" and held and held[-1]["ended"] is None:
            held[-1].update(ended=at, seconds=int(at - held[-1]["started"]))
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


def _store_records(home: Path, manifest: dict) -> list[dict]:
    """Reads reservations and mail for one project, correlated where known."""
    if not (home / store.DATABASE).exists():
        return []
    lanes = {
        participant["display"]: name
        for name, participant in manifest["participants"].items()
    }
    records = []
    with store.connect(home) as db:
        for row in db.execute(
            "SELECT a.name AS identity,f.path_pattern,f.claim_id,"
            "unixepoch(f.created_ts) AS created,"
            "unixepoch(f.released_ts) AS released FROM file_reservations f "
            "JOIN agents a ON a.id=f.agent_id "
            "JOIN projects p ON p.id=a.project_id WHERE p.human_key=? "
            "ORDER BY f.id DESC LIMIT ?",
            (manifest["root"], MAX_RECORDS),
        ):
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
        for row in db.execute(
            "SELECT m.id,a.name AS identity,m.thread_id,m.claim_id,"
            "substr(m.subject,1,80) AS subject,"
            "unixepoch(m.created_ts) AS created FROM messages m "
            "JOIN agents a ON a.id=m.sender_id "
            "JOIN projects p ON p.id=a.project_id WHERE p.human_key=? "
            "ORDER BY m.id DESC LIMIT ?",
            (manifest["root"], MAX_RECORDS),
        ):
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
    return records


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
    try:
        collected = [
            *_ledger_records(directory, manifest),
            *_report_records(directory, manifest),
            *_store_records(home, manifest),
        ]
    except (BridgeError, OSError, sqlite3.Error) as exc:
        raise BridgeError(
            f"Coordination history is unavailable: {exc}"
        ) from exc
    selected = [
        record
        for record in collected
        if (not kinds or record["kind"] in kinds)
        and (not participant or record["participant"] == participant)
        and (not provider or record["provider"] == provider)
        and (not issue or str(record["issue"] or "") == issue)
        and (not claim or record["claim_id"] == claim)
        and record["at"] >= since
    ]
    return sorted(selected, key=lambda record: record["at"])


def describe(reported: list[dict], held: list[dict] | None = None) -> str:
    """Formats history for a terminal, oldest first.

    Args:
        reported: Records produced by :func:`records`.
        held: Optional ownership generations to summarize first.

    Returns:
        One line per generation and per record, or a notice that the query
        matched nothing.
    """
    lines = []
    for generation in held or []:
        ended = "still held" if generation["ended"] is None else "released"
        lines.append(
            f"{generation['participant']} held it for "
            f"{generation['seconds']}s ({ended}); "
            f"claim {generation['claim_id'] or 'unknown'}"
        )
    for record in reported:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(record["at"]))
        issue = f" #{record['issue']}" if record["issue"] else ""
        lines.append(
            f"{stamp} {record['kind']}{issue} "
            f"{record['participant'] or 'unknown'}: {record['detail']}; "
            f"claim {record['claim_id'] or 'unknown'}"
        )
    return "\n".join(lines) or "No matching history."
