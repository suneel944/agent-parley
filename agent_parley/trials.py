"""Side-by-side trials of one issue across provider lanes.

The issue ledger keeps one owner per issue. A trial is the one recorded
exception: the operator assigns the same issue to two or more registered
lanes, each keeping its own provider, credential and worktree, and each
starting from one recorded base commit. The running trial lives on the
issue's ledger record under ``trial``, so it is written under the ledger's
own lock and a claim, an offer or an assignment of the issue is refused while
it runs. Each trial lane counts the issue toward its `max_claims_per_lane`,
and a lane parked on its credential's quota cannot enter one.

Per lane the trial records the lines changed since the base, the tests its
ready evidence reports run and passed, the findings of review rounds returned
on its branch, the token count its own client reported, and the wall time
from the start to its ready report. The token count is the client's own
reading, never billed spend. Review findings are read from the ``reviews``
record and are unknown when no round returned on that lane.

The trial ends only when the operator keeps one lane's branch. That lane
becomes the issue's ordinary owner, carrying its trial claim generation and
any ready commit it reported; every other lane is released with the recorded
outcome. Nothing here deletes a branch or a worktree. The ended trial is
appended to `RECORD_NAME`, which `metrics trials` reads per provider.
"""

import json
import re
import time
import uuid
from pathlib import Path

from agent_parley import lifecycle
from agent_parley.state import BridgeError, lock, write_json

RECORD_NAME = "trials.json"
MAX_ENDED = 500
MIN_LANES = 2
RUNNING = "running"
KEPT = "kept"
RELEASED = "released"
TOKEN_LABEL = "client-reported tokens, not billed spend"
_PASSED = re.compile(r"\b(\d+)\s+passed\b", re.IGNORECASE)
_FAILED = re.compile(r"\b(\d+)\s+(?:failed|errors?)\b", re.IGNORECASE)


def running(record: dict | None) -> dict:
    """Returns the trial running on one ledger record.

    Args:
        record: Issue ledger record, or None.

    Returns:
        The running trial, or an empty mapping when none runs.
    """
    trial = (record or {}).get("trial") or {}
    return trial if trial.get("state") == RUNNING else {}


def member(record: dict | None, name: str) -> bool:
    """Tells whether a lane is in the trial running on a record.

    Args:
        record: Issue ledger record, or None.
        name: Participant name.

    Returns:
        True while the lane works the issue as a trial lane.
    """
    return name in running(record).get("lanes", {})


def tests(evidence: str) -> dict:
    """Reads the test counts a ready report's evidence states.

    Only explicit counts are read, such as pytest's ``12 passed, 1 failed``.

    Args:
        evidence: Evidence text the lane reported.

    Returns:
        ``run`` and ``passed``, each None when the evidence states no count.
    """
    passed = sum(int(value) for value in _PASSED.findall(evidence))
    failed = sum(int(value) for value in _FAILED.findall(evidence))
    if not _PASSED.search(evidence) and not _FAILED.search(evidence):
        return {"run": None, "passed": None}
    return {"run": passed + failed, "passed": passed}


def start(
    directory: Path,
    manifest: dict,
    issue: str,
    names: list[str],
    *,
    base: str,
    readings: dict[str, dict | None],
    cap: int | None,
    now: float = 0.0,
) -> dict:
    """Records a trial of one issue across several lanes.

    Args:
        directory: Private project state directory.
        manifest: Project manifest naming the participants.
        issue: Bare repository issue number.
        names: Participants that each work the issue.
        base: Base commit every lane starts from.
        readings: Each lane's consumption reading at the start.
        cap: The project's `max_claims_per_lane`, or None for no cap.
        now: Unix time; the clock when zero.

    Returns:
        The running trial.

    Raises:
        BridgeError: If fewer than two distinct participants are named, one
            is unknown, parked on its quota or at its claim cap, or the issue
            is owned, offered, requested, complete or already in a trial.
    """
    from agent_parley import issues, quota

    now = now or time.time()
    chosen = list(dict.fromkeys(name.strip() for name in names if name.strip()))
    if len(chosen) < MIN_LANES:
        raise BridgeError(f"A trial needs at least {MIN_LANES} lanes.")
    participants = manifest["participants"]
    for name in chosen:
        if name not in participants:
            raise BridgeError(
                f"{name} is not a participant in this project; "
                "run agent-parley participant list.",
                next_command="agent-parley participant list",
            )
        parked = quota.held(directory, name, now)
        if parked:
            raise BridgeError(
                f"{name} is parked on its credential's quota until "
                f"{time.strftime('%H:%M:%S', time.localtime(parked['until']))}"
                "; start the trial without it or after the reset."
            )
    with lock(directory / "issues.lock", timeout=1):
        ledger = issues.snapshot(directory)
        record = ledger["issues"].get(issue) or issues._unseen()
        if running(record):
            raise BridgeError(f"Issue #{issue} is already in a trial.")
        if record.get("owner"):
            raise BridgeError(
                f"Issue #{issue} is owned by {record['owner']}; a trial "
                "needs an unclaimed issue."
            )
        if record.get("offer") or record.get("request"):
            raise BridgeError(
                f"Issue #{issue} has a pending offer or request; withdraw it "
                "with issue assign --unassign first."
            )
        if lifecycle.state(record)["state"] == lifecycle.COMPLETE:
            raise BridgeError("Verified complete work cannot be trialled.")
        for name in chosen:
            issues._within_cap(ledger["issues"], name, cap)
        trial = {
            "id": uuid.uuid4().hex[:16],
            "issue": issue,
            "state": RUNNING,
            "base": base,
            "started": now,
            "ended": None,
            "kept": "",
            "lanes": {
                name: {
                    "provider": str(participants[name].get("provider", "")),
                    "credential": str(
                        participants[name].get("credential") or ""
                    ),
                    "branch": str(participants[name].get("branch", "")),
                    "claim_id": uuid.uuid4().hex[:16],
                    "usage_start": readings.get(name),
                    "outcome": "",
                }
                for name in chosen
            },
        }
        record["trial"] = trial
        lifecycle.authorize(record)
        lifecycle.append_history(
            record,
            {
                "action": "trial",
                "actor": issues.OPERATOR,
                "at": now,
                "owner": None,
                "offer": None,
                "request": None,
                "offer_id": None,
                "claim_id": None,
                "lanes": chosen,
            },
        )
        ledger["issues"][issue] = record
        ledger["revision"] += 1
        write_json(directory / "issues.json", ledger)
    return trial


def reported(
    directory: Path,
    participant: dict,
    name: str,
    issue: str,
    *,
    outcome: str,
    commit: str,
    evidence: str,
    usage: dict | None,
    now: float = 0.0,
) -> dict:
    """Records one trial lane's report against its trial entry.

    A ready report fixes the lane's measured figures: the commit, the lines
    changed since the trial's base, the test counts its evidence states, the
    client-reported tokens since the start and the wall time to ready. A
    later ready report replaces them; any other report only notes its time.

    Args:
        directory: Private project state directory.
        participant: Manifest entry naming the lane's worktree.
        name: Reporting participant.
        issue: Bare issue number the trial runs on.
        outcome: Reported state.
        commit: Lane HEAD at the report.
        evidence: Evidence text of the report.
        usage: The lane's consumption reading now, or None.
        now: Unix time; the clock when zero.

    Returns:
        The lane's trial entry.

    Raises:
        BridgeError: If the issue no longer runs a trial with this lane.
    """
    from agent_parley import context, issues

    now = now or time.time()
    with lock(directory / "issues.lock", timeout=1):
        ledger = issues.snapshot(directory)
        record = ledger["issues"].get(issue)
        trial = running(record)
        if name not in trial.get("lanes", {}):
            raise BridgeError(
                f"Issue #{issue} no longer runs a trial with {name}."
            )
        entry = trial["lanes"][name]
        entry["reported"] = {"state": outcome, "at": now}
        if outcome == lifecycle.READY:
            spent = (
                lifecycle.usage_between(entry.get("usage_start"), usage)
                if usage
                else {"tokens": None, "basis": ""}
            )
            entry.update(
                ready_at=now,
                commit=commit,
                diff=_size(context.lane_diffstat(participant, trial["base"])),
                tests=tests(evidence),
                tokens=spent["tokens"],
                token_basis=spent["basis"],
            )
        ledger["revision"] += 1
        write_json(directory / "issues.json", ledger)
    return entry


def keep(
    directory: Path,
    issue: str,
    name: str,
    *,
    defaults: dict,
    now: float = 0.0,
) -> dict:
    """Ends a trial by keeping one lane's branch.

    The kept lane becomes the issue's owner under its trial claim
    generation, ready for integration when it reported ready. Every other
    lane is released with the recorded outcome; its branch and worktree are
    left exactly as they are.

    Args:
        directory: Private project state directory.
        issue: Bare issue number the trial runs on.
        name: Lane whose branch the operator keeps.
        defaults: Project deadline and attempt-budget defaults.
        now: Unix time; the clock when zero.

    Returns:
        The ended trial.

    Raises:
        BridgeError: If no trial runs on the issue or the lane is not in it.
    """
    from agent_parley import issues

    now = now or time.time()
    with lock(directory / "issues.lock", timeout=1):
        ledger = issues.snapshot(directory)
        record = ledger["issues"].get(issue)
        trial = running(record)
        if not trial:
            raise BridgeError(f"Issue #{issue} runs no trial.")
        if name not in trial["lanes"]:
            raise BridgeError(
                f"{name} is not in the trial of #{issue}; it runs "
                + ", ".join(sorted(trial["lanes"]))
                + "."
            )
        assert record is not None
        for lane, entry in trial["lanes"].items():
            entry["outcome"] = KEPT if lane == name else RELEASED
            if lane != name:
                entry["reason"] = f"operator kept {name}"
        trial.update(state=KEPT, kept=name, ended=now)
        entry = trial["lanes"][name]
        expected = defaults.get("claim")
        record.update(
            owner=name,
            offer=None,
            request=None,
            deadline=now + expected if expected else None,
            attempts=0,
            budget=defaults.get("attempts") or None,
            claim_id=entry["claim_id"],
        )
        execution = lifecycle.claimed(
            record, entry["claim_id"], entry.get("usage_start")
        )
        if entry.get("ready_at") and entry.get("commit"):
            execution.update(
                state=lifecycle.READY,
                next_action="verify and integrate",
                updated_at=now,
                commit=entry["commit"],
                source_commit=entry["commit"],
            )
        record.pop("trial", None)
        lifecycle.append_history(
            record,
            {
                "action": "claim",
                "actor": issues.OPERATOR,
                "at": now,
                "owner": name,
                "offer": None,
                "request": None,
                "offer_id": None,
                "claim_id": entry["claim_id"],
                "trial": trial["id"],
                "released": sorted(
                    lane for lane in trial["lanes"] if lane != name
                ),
            },
        )
        archive = _read(directory)
        if not any(item.get("id") == trial["id"] for item in archive):
            archive.append(trial)
        write_json(directory / RECORD_NAME, {"trials": archive[-MAX_ENDED:]})
        ledger["revision"] += 1
        write_json(directory / "issues.json", ledger)
    return trial


def ended(directory: Path) -> list[dict]:
    """Lists the trials the operator ended, oldest first.

    Args:
        directory: Private project state directory.

    Returns:
        Each ended trial with its per-lane entries and outcomes.
    """
    return _read(directory)


def find(directory: Path, issue: str) -> dict:
    """Returns the trial running on an issue, or the latest one that ended.

    Args:
        directory: Private project state directory.
        issue: Bare issue number.

    Returns:
        The trial.

    Raises:
        BridgeError: If the issue was never trialled.
    """
    from agent_parley import issues

    trial = running(issues.snapshot(directory)["issues"].get(issue))
    if trial:
        return trial
    for item in reversed(_read(directory)):
        if item.get("issue") == issue:
            return item
    raise BridgeError(f"Issue #{issue} has no trial recorded.")


def comparison(directory: Path, manifest: dict, issue: str) -> dict:
    """Compares the lanes of an issue's trial side by side.

    A running lane that has not reported ready shows its live diff and no
    wall time. Findings are the sum over review rounds that returned on the
    lane's work on this issue, and None when none returned.

    Args:
        directory: Private project state directory.
        manifest: Project manifest naming the participants.
        issue: Bare issue number.

    Returns:
        The trial's identity, state and base, and one row per lane.

    Raises:
        BridgeError: If the issue was never trialled.
    """
    from agent_parley import context, reviews

    trial = find(directory, issue)
    rounds = reviews.rounds(directory, issue=issue)
    rows = []
    for name, entry in sorted(trial["lanes"].items()):
        participant = manifest["participants"].get(name) or {}
        display = participant.get("display", name)
        returned = [
            item
            for item in rounds
            if item.get("builder") in (name, display)
            and item.get("state") == reviews.RETURNED
        ]
        diff = entry.get("diff")
        if diff is None and trial["state"] == RUNNING and participant:
            diff = _size(context.lane_diffstat(participant, trial["base"]))
        ready = entry.get("ready_at")
        counts = entry.get("tests") or {"run": None, "passed": None}
        rows.append(
            {
                "lane": name,
                "provider": entry.get("provider", ""),
                "credential": entry.get("credential") or "default",
                "branch": entry.get("branch", ""),
                "outcome": entry.get("outcome") or trial["state"],
                "commit": entry.get("commit", ""),
                "diff": diff,
                "tests_run": counts["run"],
                "tests_passed": counts["passed"],
                "findings": (
                    sum(int(item.get("findings") or 0) for item in returned)
                    if returned
                    else None
                ),
                "client_tokens": entry.get("tokens"),
                "ready_seconds": (
                    round(float(ready) - float(trial["started"]))
                    if ready
                    else None
                ),
            }
        )
    return {
        "issue": int(issue),
        "id": trial["id"],
        "state": trial["state"],
        "base": trial["base"],
        "started": trial["started"],
        "ended": trial.get("ended"),
        "kept": trial.get("kept", ""),
        "token_label": TOKEN_LABEL,
        "lanes": rows,
    }


def render(compared: dict) -> str:
    """Formats a trial comparison for a terminal.

    Args:
        compared: Result of `comparison`.

    Returns:
        A header line, one row per lane and the token label.
    """

    def cell(value: object) -> str:
        """Shows an unknown figure as ``?``."""
        return "?" if value is None else str(value)

    lines = [
        f"Trial #{compared['issue']} {compared['state']}"
        + (f", kept {compared['kept']}" if compared["kept"] else "")
        + f"; base {compared['base'][:12]}",
        f"{'LANE':<14} {'PROVIDER':<10} {'OUTCOME':<9} {'DIFF':>11} "
        f"{'TESTS':>9} {'FINDINGS':>8} {'CLIENT_TOK':>11} {'READY_S':>8}",
    ]
    for row in compared["lanes"]:
        diff = row["diff"]
        changed = (
            f"+{diff['added']}/-{diff['deleted']}" if diff is not None else "?"
        )
        tested = (
            f"{row['tests_passed']}/{row['tests_run']}"
            if row["tests_run"] is not None
            else "?"
        )
        lines.append(
            f"{row['lane'][:14]:<14} {row['provider'][:10]:<10} "
            f"{row['outcome'][:9]:<9} {changed:>11} {tested:>9} "
            f"{cell(row['findings']):>8} {cell(row['client_tokens']):>11} "
            f"{cell(row['ready_seconds']):>8}"
        )
    lines.append(
        f"CLIENT_TOK: {compared['token_label']}. TESTS: passed/run from ready "
        "evidence. ?: unknown."
    )
    return "\n".join(lines) + "\n"


def outcomes(trials: list[dict], since: float = 0.0) -> dict[str, dict]:
    """Aggregates ended trials per provider.

    Args:
        trials: Ended trials, as `ended` lists them.
        since: Count only trials that ended at or after this Unix time.

    Returns:
        Per provider: lanes entered, kept, released and reported ready, the
        mean wall seconds to ready, and the summed client-reported tokens,
        None when no lane of that provider recorded a count.
    """
    totals: dict[str, dict] = {}
    for trial in trials:
        if float(trial.get("ended") or 0.0) < since:
            continue
        for entry in trial.get("lanes", {}).values():
            counts = totals.setdefault(
                str(entry.get("provider", "")),
                {
                    "entered": 0,
                    "kept": 0,
                    "released": 0,
                    "ready": 0,
                    "ready_seconds": [],
                    "client_tokens": None,
                },
            )
            counts["entered"] += 1
            if entry.get("outcome") in (KEPT, RELEASED):
                counts[entry["outcome"]] += 1
            if entry.get("ready_at"):
                counts["ready"] += 1
                counts["ready_seconds"].append(
                    float(entry["ready_at"]) - float(trial["started"])
                )
            if entry.get("tokens") is not None:
                counts["client_tokens"] = (counts["client_tokens"] or 0) + int(
                    entry["tokens"]
                )
    for counts in totals.values():
        waited = counts.pop("ready_seconds")
        counts["mean_ready_seconds"] = (
            round(sum(waited) / len(waited)) if waited else None
        )
    return dict(sorted(totals.items()))


def _size(diff: dict | None) -> dict | None:
    """Keeps the totals of a diffstat, dropping its per-file entries."""
    if diff is None:
        return None
    return {
        "files": len(diff["files"]),
        "added": diff["added"],
        "deleted": diff["deleted"],
    }


def _read(directory: Path) -> list[dict]:
    """Reads the ended-trial record, treating a damaged file as empty."""
    try:
        value = json.loads((directory / RECORD_NAME).read_text())
    except (OSError, ValueError):
        return []
    found = value.get("trials") if isinstance(value, dict) else None
    if not isinstance(found, list):
        return []
    return [item for item in found if isinstance(item, dict)]
