"""Durable execution state for authorized repository issues.

Ownership says who may change an issue. Execution state says what that owner
still owes. The state is stored inside the issue ledger so an ownership
transition and its new claim generation publish atomically.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from agent_parley.state import BridgeError, lock, write_json

QUEUED = "queued"
RUNNING = "running"
BLOCKED = "blocked"
READY = "ready"
COMPLETE = "complete"
RECOVERY = "recovery"

ACTIVE = (QUEUED, RUNNING, RECOVERY)
STATES = (*ACTIVE, BLOCKED, READY, COMPLETE)
COMMIT = re.compile(r"[0-9a-f]{7,40}")


def initial(*, authorized: bool = False) -> dict:
    """Creates execution state for an issue with no claim.

    Args:
        authorized: Whether an operator-approved source added the issue.

    Returns:
        A queued execution record with no claim generation.
    """
    return {
        "authorized": authorized,
        "state": QUEUED,
        "claim_id": None,
        "next_action": "claim" if authorized else "await authorization",
        "updated_at": time.time(),
        "blocker": "",
        "resume_when": "",
        "commit": "",
        "source_commit": "",
        "integrated_commit": "",
        "gate": None,
        "progress": None,
    }


def state(record: dict) -> dict:
    """Returns normalized execution state without changing a ledger record.

    Older ledgers have no execution field. An existing claim is authorized
    because a lane explicitly claimed or accepted it. An unowned old record
    stays unauthorized until a plan, assignment, or later claim authorizes it.

    Args:
        record: Issue ledger record.

    Returns:
        Complete execution mapping, including compatibility defaults.
    """
    current = dict(record.get("execution") or {})
    owner = record.get("owner")
    authorized = bool(current.get("authorized") or owner)
    phase = current.get("state")
    if phase not in STATES:
        phase = RUNNING if owner else QUEUED
    defaults = initial(authorized=authorized)
    defaults.update(current)
    defaults.update(
        authorized=authorized,
        state=phase,
        claim_id=current.get("claim_id") or record.get("claim_id"),
    )
    if not current.get("next_action"):
        defaults["next_action"] = _next_action(defaults)
    return defaults


def _next_action(execution: dict) -> str:
    """Derives the persisted next action for one execution state."""
    phase = execution["state"]
    if phase == QUEUED:
        return "resume" if execution.get("claim_id") else "claim"
    if phase in (RUNNING, RECOVERY):
        return "resume"
    if phase == BLOCKED:
        condition = execution.get("resume_when") or {}
        kind = (
            condition.get("kind") if isinstance(condition, dict) else condition
        )
        return "wait for " + str(kind or "recorded condition")
    if phase == READY:
        return "verify and integrate"
    return "none"


def authorize(record: dict) -> dict:
    """Authorizes one ledger issue for automatic claim or continuation.

    Verified work remains complete when a later plan mentions it. Every other
    state retains its current generation and becomes available to dispatch.

    Args:
        record: Mutable issue ledger record.

    Returns:
        The execution mapping stored on the record.
    """
    execution = state(record)
    execution["authorized"] = True
    execution["updated_at"] = time.time()
    execution["next_action"] = _next_action(execution)
    record["execution"] = execution
    return execution


def claimed(record: dict, claim_id: str) -> dict:
    """Starts a new authorized execution generation on a claim.

    Args:
        record: Mutable issue ledger record carrying earlier execution state.
        claim_id: Newly generated ownership identifier.

    Returns:
        The execution mapping stored on the record.

    Raises:
        BridgeError: If verified work is claimed again.
    """
    previous = state(record)
    if previous["state"] == COMPLETE:
        raise BridgeError("Verified complete work cannot be claimed again.")
    execution = initial(authorized=True)
    execution.update(
        state=RUNNING,
        claim_id=claim_id,
        next_action="resume",
        progress=previous.get("progress"),
    )
    record["execution"] = execution
    return execution


def released(record: dict) -> dict:
    """Returns unfinished work to its authorized queue after release.

    Args:
        record: Mutable issue ledger record.

    Returns:
        The execution mapping stored on the record.
    """
    execution = state(record)
    if execution["state"] != COMPLETE:
        execution.update(
            state=QUEUED,
            claim_id=None,
            next_action="claim",
            updated_at=time.time(),
            blocker="",
            resume_when="",
            commit="",
            gate=None,
        )
    record["execution"] = execution
    return execution


def dependencies_complete(ledger: dict, record: dict) -> bool:
    """Reports whether every issue this record waits on is verified complete.

    Args:
        ledger: Published issue ledger.
        record: Issue record whose dependencies are checked.

    Returns:
        True when every dependency carries verified completion.
    """
    issues = ledger.get("issues", {})
    return all(
        state(issues.get(number, {}))["state"] == COMPLETE
        for number in record.get("blocked_by", [])
    )


def actionable(ledger: dict, owner: str | None = None) -> list[str]:
    """Lists authorized work a lane can continue or claim now.

    Args:
        ledger: Published issue ledger.
        owner: Existing owner to resume, or None for unowned work.

    Returns:
        Issue numbers ordered numerically. Owner work is limited to its
        current generation. Free work must be queued and unowned.
    """
    found = []
    for number, record in ledger.get("issues", {}).items():
        execution = state(record)
        if not execution["authorized"]:
            continue
        if not dependencies_complete(ledger, record):
            continue
        if owner is None:
            eligible = not record.get("owner") and execution["state"] == QUEUED
        else:
            eligible = (
                record.get("owner") == owner
                and not record.get("orphan")
                and execution["claim_id"] == record.get("claim_id")
                and execution["state"] in ACTIVE
            )
        if eligible:
            found.append(number)
    return sorted(found, key=int)


def describe_action(record: dict) -> str:
    """Returns the durable next action for one issue record.

    Args:
        record: Published issue ledger record.

    Returns:
        Persisted action text, normalized for an older ledger record.
    """
    return str(state(record)["next_action"])


def record_report(
    directory: Path,
    agent: str,
    outcome: str,
    commit: str,
    remaining: str,
    issue: str = "",
    claim_id: str = "",
    resume_on: str = "",
) -> list[str]:
    """Binds a lane report to one exact current claim generation.

    A report can make current work blocked or ready for verification. It
    cannot verify completion. A report from an earlier generation cannot be
    applied after that generation has left the ledger.

    Args:
        directory: Private project state directory.
        agent: Reporting lane.
        outcome: Partial, blocked, or ready.
        commit: Exact lane HEAD at report time.
        remaining: Recorded blocker or unfinished work.
        issue: Exact owned issue, inferred only when ownership is unambiguous.
        claim_id: Expected ownership generation, when already observed.
        resume_on: Existing authorized issue whose completion resumes a block.

    Returns:
        Issue numbers whose current execution state changed.

    Raises:
        BridgeError: If a ready report has no valid commit.
    """
    if outcome == READY and not COMMIT.fullmatch(commit):
        raise BridgeError("Ready work must name its exact Git commit.")
    if resume_on and outcome != BLOCKED:
        raise BridgeError("--resume-on is valid only for blocked reports.")
    resumed_by = _issue_number(resume_on, "Resume issue") if resume_on else ""
    with lock(directory / "issues.lock", timeout=1):
        ledger = _snapshot(directory)
        owned = [
            number
            for number, record in ledger["issues"].items()
            if record.get("owner") == agent
            and state(record)["claim_id"] == record.get("claim_id")
        ]
        if issue:
            number = _issue_number(issue)
            if number not in owned:
                raise BridgeError(
                    f"Issue #{number} is not owned by {agent} in its current "
                    "claim generation."
                )
        elif len(owned) > 1:
            raise BridgeError(
                "A report must name one issue when a lane owns multiple claims."
            )
        elif not owned:
            return []
        else:
            number = owned[0]
        record = ledger["issues"][number]
        if claim_id and record.get("claim_id") != claim_id:
            raise BridgeError(
                f"Issue #{number} changed ownership generation before its "
                "report was recorded."
            )
        phase = {
            "partial": RUNNING,
            BLOCKED: BLOCKED,
            READY: READY,
        }.get(outcome)
        if phase is None:
            raise BridgeError("Unknown lifecycle report state.")
        if phase == READY and record.get("blocked_by"):
            raise BridgeError(
                f"Issue #{number} still has incomplete dependencies."
            )
        if resumed_by:
            dependency = ledger["issues"].get(resumed_by)
            if not dependency or not state(dependency)["authorized"]:
                raise BridgeError(
                    f"Resume issue #{resumed_by} is not authorized work."
                )
            if resumed_by == number:
                raise BridgeError("An issue cannot wait on itself.")
            if state(dependency)["state"] == COMPLETE:
                raise BridgeError(
                    f"Resume issue #{resumed_by} is already complete."
                )
            blockers = set(record.get("blocked_by", [])) | {resumed_by}
            if _reaches(ledger["issues"], resumed_by, number):
                raise BridgeError(
                    f"Issue #{number} waiting on #{resumed_by} would form "
                    "a dependency cycle."
                )
            record["blocked_by"] = sorted(blockers, key=int)
        execution = state(record)
        execution.update(
            state=phase,
            next_action={
                RUNNING: "resume",
                BLOCKED: "wait for recorded condition",
                READY: "verify and integrate",
            }[phase],
            updated_at=time.time(),
            blocker={"reason": remaining.strip()} if phase == BLOCKED else "",
            resume_when=(
                {"kind": "issue", "issue": resumed_by}
                if resumed_by
                else (
                    {"kind": "external", "detail": remaining.strip()}
                    if phase == BLOCKED
                    else ""
                )
            ),
            commit=commit if phase == READY else execution.get("commit", ""),
            source_commit=(
                commit if phase == READY else execution.get("source_commit", "")
            ),
        )
        execution["progress"] = {"token": commit, "at": time.time()}
        record["execution"] = execution
        ledger["revision"] += 1
        write_json(directory / "issues.json", ledger)
    return [number]


def complete(
    directory: Path,
    issue: str,
    claim_id: str,
    commit: str,
    gate_command: list[str],
    source_commit: str = "",
) -> dict:
    """Records verified integration and reconciles dependent issues.

    Args:
        directory: Private project state directory.
        issue: Issue number being completed.
        claim_id: Exact ownership generation being integrated.
        commit: Exact integrated repository commit that passed the gate.
        gate_command: Configured command that passed, or an empty list when
            the repository requires no gate.
        source_commit: Exact reported lane commit that was integrated.

    Returns:
        Completed issue record.

    Raises:
        BridgeError: If ownership changed, work is not ready, or the commit
            is not a Git object name.
    """
    if not COMMIT.fullmatch(commit):
        raise BridgeError("Verified completion must name its exact Git commit.")
    with lock(directory / "issues.lock", timeout=1):
        ledger = _snapshot(directory)
        record = ledger["issues"].get(issue)
        if not record or record.get("claim_id") != claim_id:
            raise BridgeError(
                f"Issue #{issue} changed ownership generation before "
                "completion."
            )
        execution = state(record)
        if execution["state"] != READY:
            raise BridgeError(
                f"Issue #{issue} is {execution['state']}, not ready for "
                "verification."
            )
        if record.get("blocked_by"):
            raise BridgeError(
                f"Issue #{issue} still waits on "
                + ", ".join(f"#{item}" for item in record["blocked_by"])
                + "."
            )
        reported = str(
            execution.get("source_commit") or execution.get("commit") or ""
        )
        source_commit = source_commit or reported
        if not COMMIT.fullmatch(source_commit) or source_commit != reported:
            raise BridgeError(
                f"Issue #{issue} is ready at {reported}, not "
                f"{source_commit or 'an unknown commit'}."
            )
        owner = record.get("owner")
        now = time.time()
        execution.update(
            state=COMPLETE,
            next_action="none",
            updated_at=now,
            commit=commit,
            source_commit=source_commit,
            integrated_commit=commit,
            gate={
                "command": list(gate_command),
                "status": "passed" if gate_command else "not required",
                "commit": commit,
                "at": now,
            },
            blocker="",
            resume_when="",
        )
        record.update(
            owner=None,
            offer=None,
            request=None,
            execution=execution,
            completed_by=owner,
            completed_at=now,
        )
        record.setdefault("history", []).append(
            {
                "action": "complete",
                "actor": "operator",
                "at": now,
                "owner": None,
                "offer": None,
                "request": None,
                "offer_id": None,
                "claim_id": claim_id,
                "commit": commit,
            }
        )
        for waiting in ledger["issues"].values():
            blockers = waiting.get("blocked_by", [])
            if issue not in blockers:
                continue
            waiting["blocked_by"] = [
                number for number in blockers if number != issue
            ]
            waiting_execution = state(waiting)
            condition = waiting_execution.get("resume_when") or {}
            dependency = (
                ledger["issues"].get(str(condition.get("issue")))
                if isinstance(condition, dict)
                and condition.get("kind") == "issue"
                else None
            )
            if (
                not waiting["blocked_by"]
                and waiting_execution["state"] == BLOCKED
                and dependency
                and state(dependency)["state"] == COMPLETE
            ):
                waiting_execution.update(
                    state=RUNNING if waiting.get("owner") else QUEUED,
                    next_action=("resume" if waiting.get("owner") else "claim"),
                    updated_at=now,
                    blocker="",
                    resume_when="",
                )
                waiting["execution"] = waiting_execution
        ledger["revision"] += 1
        write_json(directory / "issues.json", ledger)
        return record


def _issue_number(value: str, label: str = "Issue") -> str:
    """Returns one positive decimal issue number."""
    number = value[1:] if value.startswith("#") else value
    if not number.isdigit() or int(number) < 1:
        raise BridgeError(f"{label} must be a positive issue number.")
    return str(int(number))


def _reaches(records: dict, start: str, target: str) -> bool:
    """Reports whether dependency edges lead from start to target."""
    pending = [start]
    seen = set()
    while pending:
        number = pending.pop()
        if number == target:
            return True
        if number in seen:
            continue
        seen.add(number)
        pending.extend(records.get(number, {}).get("blocked_by", []))
    return False


def _snapshot(directory: Path) -> dict:
    """Reads the issue ledger while its caller holds the issue lock."""
    path = directory / "issues.json"
    if not path.exists():
        return {"revision": 0, "issues": {}}
    return json.loads(path.read_text())
