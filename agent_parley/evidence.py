"""Builds review evidence from retained coordination records."""

import collections
import hashlib
import json
import time
from pathlib import Path

from agent_parley import checkpoints, issues, store
from agent_parley.state import write_json


def collect(
    home: Path, directory: Path, manifest: dict, name: str, head: str
) -> dict:
    """Snapshots the current claim window without copying private mail.

    Args:
        home: Private coordination store root.
        directory: Project state directory beside the lane.
        manifest: Validated project manifest.
        name: Participant whose work is under review.
        head: Exact commit being reviewed.

    Returns:
        Evidence with retained denials, reservations, conflict counts and a
        portable event slice. Missing or expired telemetry is not proof that
        an event never happened.
    """
    starts = []
    for record in issues.snapshot(directory)["issues"].values():
        if record.get("owner") != name:
            continue
        starts.extend(
            [
                event["at"]
                for event in reversed(record.get("history", []))
                if event["action"] in {"claim", "accept"}
                and event.get("owner") == name
            ][:1]
        )
    since = min(starts) if starts else time.time()
    events = checkpoints.read_events(directory, name, since)
    denials = collections.Counter(
        event.get("reason_class", "unknown")
        for event in events
        if event.get("decision") in {"deny", "block"}
    )
    reservations = []
    conflicts = 0
    available = (home / store.DATABASE).exists()
    if available:
        with store.connect(home) as db:
            actor = db.execute(
                "SELECT a.id FROM agents a JOIN projects p "
                "ON p.id=a.project_id WHERE p.human_key=? AND a.name=?",
                (manifest["root"], manifest["participants"][name]["display"]),
            ).fetchone()
            if actor:
                reservations = [
                    dict(row)
                    for row in db.execute(
                        "SELECT path_pattern,created_ts,released_ts "
                        "FROM file_reservations WHERE agent_id=? AND "
                        "(released_ts IS NULL OR "
                        "released_ts>=datetime(?,'unixepoch')) ORDER BY id",
                        (actor["id"], since),
                    )
                ]
                conflicts = db.execute(
                    "SELECT count(*) FROM events WHERE agent_id=? AND "
                    "tool='file_reservation_paths' AND outcome='conflict' "
                    "AND created_ts>=datetime(?,'unixepoch')",
                    (actor["id"], since),
                ).fetchone()[0]
    return {
        "head": head,
        "since": since,
        "until": time.time(),
        "denials": dict(denials),
        "reservations": reservations,
        "reservation_conflicts": conflicts,
        "store_available": available,
        "events": events,
    }


def publish(directory: Path, record: dict) -> str:
    """Writes a private evidence artifact and renders its review summary.

    Args:
        directory: Project state directory beside participant worktrees.
        record: Collected evidence and independently executed gate result.

    Returns:
        Markdown naming the measurements and the artifact's SHA-256 digest.
    """
    encoded = "".join(json.dumps(event) + "\n" for event in record["events"])
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    export = directory / f"review-{record['head']}-events.jsonl"
    export.write_text(encoded, encoding="utf-8")
    write_json(directory / f"review-{record['head']}.json", record)
    gate = record.get("gate")
    gate_text = (
        f"Verification executed in the lane: `{gate['command']}`; "
        f"exit status {gate['exit_status']}."
        if gate
        else "No verification command configured; no gate was executed."
    )
    reasons = (
        ", ".join(
            f"{reason}: {count}"
            for reason, count in sorted(record["denials"].items())
        )
        or "none recorded"
    )
    paths = (
        ", ".join(
            json.dumps(path)
            for path in sorted(
                {row["path_pattern"] for row in record["reservations"]}
            )
        )
        or "none recorded"
    )
    return (
        "## Recorded review evidence\n\n"
        f"Commit: `{record['head']}`. {gate_text}\n\n"
        f"Retained hook denials by reason: {reasons}.\n\n"
        f"Advisory reservations held during the claim: {paths}. "
        f"Recorded conflicting requests: {record['reservation_conflicts']}.\n\n"
        f"Claim-window event export: `{export.name}` beside the worktree in "
        f"private project state; SHA-256 `{digest}`. "
        "This local artifact is not uploaded automatically.\n\n"
        "These figures come from retained coordination records. Old records "
        "can expire, and telemetry may be unavailable; zero recorded events "
        "does not prove no events occurred. Historical conflict outcomes "
        "from older installations were not distinguished.\n"
    )
