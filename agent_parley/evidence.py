"""Builds review evidence from retained coordination records."""

import collections
import hashlib
import json
import time
from pathlib import Path

from agent_parley import checkpoints, issues, store
from agent_parley.state import write_json

SECTION_START = "<!-- agent-parley:evidence:start -->"
SECTION_END = "<!-- agent-parley:evidence:end -->"
SECTION_HEADING = "## Recorded review evidence"


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


def section(rendered: str) -> str:
    """Wraps rendered evidence in the markers that make it replaceable.

    Args:
        rendered: Review summary produced by :func:`publish`.

    Returns:
        The summary delimited so a later push can replace exactly this text.
    """
    return f"{SECTION_START}\n{rendered.rstrip()}\n{SECTION_END}\n"


def refresh(body: str, rendered: str) -> str:
    """Replaces the managed evidence section and preserves every human edit.

    A pull request body is written once at creation and then belongs to the
    people reviewing it. Only the delimited evidence section is owned here, so
    a later push rewrites that span and leaves reviewer prose, checklists and
    added headings untouched. A body written before the markers existed is
    recognized by the evidence heading and its trailing span is replaced, and
    a body carrying neither marker nor heading gains the section at the end
    rather than losing anything.

    Args:
        body: Current pull-request body, including any human edits.
        rendered: Review summary produced by :func:`publish`.

    Returns:
        The body with exactly one current, delimited evidence section.
    """
    replacement = section(rendered)
    start = body.find(SECTION_START)
    end = body.find(SECTION_END)
    if start != -1 and end > start:
        tail = body[end + len(SECTION_END) :].lstrip("\n")
        return body[:start] + replacement + tail
    heading = body.find(SECTION_HEADING)
    if heading != -1:
        return body[:heading] + replacement
    return body.rstrip("\n") + "\n\n" + replacement


def publish(directory: Path, record: dict) -> str:
    """Writes a private evidence artifact and renders its review summary.

    Args:
        directory: Project state directory beside participant worktrees.
        record: Collected evidence, the independently executed gate result
            and, for a pull request a repository policy let the lane open
            itself, the conditions that authorized it.

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
    authorization = record.get("authorization")
    authorized = (
        f"Opened by {authorization['participant']} itself under the "
        f"repository policy `{authorization['policy']}`: a ready report, "
        f"the gate `{authorization['gate']}` above, "
        f"{authorization['changed_paths']} changed paths clear of the "
        "reservations held by "
        + (", ".join(authorization["peers_holding_reservations"]) or "no peer")
        + f", and the lane still on `{authorization['branch']}`.\n\n"
        if authorization
        else ""
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
        f"{SECTION_HEADING}\n\n"
        f"Commit: `{record['head']}`. {gate_text}\n\n"
        f"{authorized}"
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
