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

from agent_parley import attachments
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
MAX_HISTORY = 50
_MARKER_ACTIONS = ("claim", "take", "complete")


def append_history(record: dict, entry: dict, cap: int = MAX_HISTORY) -> None:
    """Appends one transition and bounds how much history an issue keeps.

    A lane that claims and releases the same issue hundreds of times would
    otherwise grow this list without limit, and every idempotent retry then
    copies the whole growing list again. Retention keeps the most recent
    `cap` entries, plus the latest claim, take and complete marker when an
    older one would otherwise fall outside that window, so a capped history
    still answers when the current claim began and when the issue last
    completed.

    Args:
        record: Issue record whose history is being extended.
        entry: Transition entry to append.
        cap: Number of most recent entries kept before markers are restored.
    """
    history = record.setdefault("history", [])
    history.append(entry)
    if len(history) <= cap:
        return
    recent = history[-cap:]
    present = {item.get("action") for item in recent}
    older = history[:-cap]
    markers = [
        marker
        for action in _MARKER_ACTIONS
        if action not in present
        for marker in [
            next(
                (
                    item
                    for item in reversed(older)
                    if item.get("action") == action
                ),
                None,
            )
        ]
        if marker is not None
    ]
    markers.sort(key=lambda item: item.get("at", 0))
    record["history"] = markers + recent


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
        "backlog": None,
    }


def drop_offer(directory: Path, record: dict) -> None:
    """Removes the attachments of an offer that is leaving the record.

    Every transition that clears a pending offer calls this first, so a
    declined, cancelled, released, completed or resolved offer takes its
    spilled summary and its attached diff with it. An offer that is only
    dereferenced would leave its diff counted against the offering lane's
    attachment allowance forever.

    Args:
        directory: Private state directory for the common repository.
        record: Ledger record whose pending offer is being cleared.
    """
    offer = record.get("offer") or {}
    for field in ("attachment", "diff"):
        if offer.get(field):
            attachments.remove(directory, str(offer[field]))


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


def released(record: dict, closed: bool = False) -> dict:
    """Returns unfinished work to its authorized queue after release.

    A claim whose issue closed on the forge inside its generation has no
    integration left to wait on, so even ready work may be released. It is
    marked through `end_on_forge` rather than offered again as free work;
    the next claim builds a fresh record without the mark, so a reopened
    issue can still be taken.

    Args:
        record: Mutable issue ledger record.
        closed: Whether the claim's issue closed on the forge inside the
            current ownership generation.

    Returns:
        The execution mapping stored on the record.

    Raises:
        BridgeError: If ready work still awaits verified integration.
    """
    execution = state(record)
    if execution["state"] == READY and not closed:
        raise BridgeError(
            "Ready work must remain claimed until verified integration "
            "completes."
        )
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
    if closed:
        end_on_forge(record)
    return record["execution"]


def end_on_forge(record: dict) -> dict:
    """Marks an unowned issue as work that ended on the forge.

    The mark keeps the issue out of free work and satisfies the issues that
    wait on it. Its queued execution stops reading as claimable: it loses
    the automatic-claim authorization and its next action becomes `none`,
    so neither `issue next` nor a work offer, nor an operator reading the
    record, is told to claim a closed issue. Verified complete work keeps
    its execution unchanged. A later claim starts a fresh authorized
    generation and drops the mark, so a reopened issue can still be taken.

    Args:
        record: Mutable issue ledger record without an owner.

    Returns:
        The execution mapping stored on the record.
    """
    execution = state(record)
    record["ended_on_forge"] = {
        "at": time.time(),
        "claim_id": record.get("claim_id"),
    }
    if execution["state"] != COMPLETE:
        execution.update(
            authorized=False,
            next_action="none",
            updated_at=time.time(),
        )
    record["execution"] = execution
    return execution


def satisfied(record: dict) -> bool:
    """Reports whether a blocker no longer holds back the work waiting on it.

    A blocker is satisfied by verified completion, or by release after its
    issue or pull request ended on the forge, which marks the record
    `ended_on_forge`. The operator merging or closing by hand ends the work
    as surely as verified integration does, and nothing else would ever
    clear the edge. A reclaim drops the mark, so a reopened blocker holds
    its dependents again. A blocker whose issue the forge reports closed
    while a lane still holds it is satisfied too: a dead holder never
    releases it, so its dependents would otherwise wait for nothing.

    Args:
        record: Ledger record of the blocking issue, or an empty mapping.

    Returns:
        True when the blocker is complete, ended on the forge, or closed on
        the forge.
    """
    from agent_parley import issues

    return (
        state(record)["state"] == COMPLETE
        or bool(record.get("ended_on_forge"))
        or bool(record and issues.closed(record))
    )


def dependencies_complete(ledger: dict, record: dict) -> bool:
    """Reports whether every issue this record waits on is satisfied.

    Args:
        ledger: Published issue ledger.
        record: Issue record whose dependencies are checked.

    Returns:
        True when every dependency carries verified completion or ended on
        the forge.
    """
    issues = ledger.get("issues", {})
    return all(
        satisfied(issues.get(number, {}))
        for number in record.get("blocked_by", [])
    )


def actionable(ledger: dict, owner: str | None = None) -> list[str]:
    """Lists authorized work a lane can continue or claim now.

    Args:
        ledger: Published issue ledger.
        owner: Existing owner to resume, or None for unowned work.

    Returns:
        Issue numbers ordered numerically. Owner work is limited to its
        current generation and to issues that did not close on the forge
        inside it, so a closed issue is never the lane's next action. Free
        work must be queued, unowned and not released after its issue ended
        on the forge.
    """
    from agent_parley import issues

    found = []
    for number, record in ledger.get("issues", {}).items():
        execution = state(record)
        if not execution["authorized"]:
            continue
        if not dependencies_complete(ledger, record):
            continue
        if owner is None:
            eligible = (
                not record.get("owner")
                and not record.get("ended_on_forge")
                and execution["state"] == QUEUED
            )
        else:
            eligible = (
                record.get("owner") == owner
                and not record.get("orphan")
                and execution["claim_id"] == record.get("claim_id")
                and execution["state"] in ACTIVE
                and not issues.closed(record)
            )
        if eligible:
            found.append(number)
    return sorted(found, key=int)


def operator_blocked(record: dict) -> bool:
    """Reports whether a claim waits on a condition only an operator meets.

    A blocked claim that names no resuming issue recorded an external
    condition, which is an operator action in practice; an older record
    with no condition at all is read the same way. Releasing it on
    retirement would hand the next lane the same block with the context
    stripped, so dispatch and retirement treat it like ready work: it stays
    with the lane that recorded it until that lane resolves, hands on or
    releases it on purpose. A claim waiting on another issue is not kept:
    that issue's completion resumes it for whichever lane holds it then.

    Args:
        record: Published issue ledger record.

    Returns:
        Whether the claim is blocked on anything but another issue.
    """
    execution = state(record)
    if execution["state"] != BLOCKED:
        return False
    condition = execution.get("resume_when") or {}
    kind = condition.get("kind") if isinstance(condition, dict) else condition
    return kind != "issue"


def describe_action(record: dict) -> str:
    """Returns the durable next action for one issue record.

    Args:
        record: Published issue ledger record.

    Returns:
        Persisted action text, normalized for an older ledger record.
    """
    return str(state(record)["next_action"])


def backlog(record: dict) -> int:
    """Counts the work units the owner of one claim still states as remaining.

    The count is whatever the claim's own domain counts: issue families in a
    target project, files to convert, subtasks of a migration. Only the owner
    can state it, so it is recorded by that lane's own progress report and
    bound to the claim generation the report named.

    Args:
        record: Issue ledger record.

    Returns:
        The count last reported, or zero when none was reported. Any other
        stored value reads as zero, because a backlog the runtime cannot
        count decides nothing.
    """
    value = state(record).get("backlog")
    return value if type(value) is int and value > 0 else 0


def record_report(
    directory: Path,
    agent: str,
    outcome: str,
    commit: str,
    remaining: str,
    issue: str = "",
    claim_id: str = "",
    resume_on: str = "",
    backlog_count: int | None = None,
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
        backlog_count: Work units the lane still states as remaining on this
            claim. None leaves the recorded count as it stands, because a
            report that does not restate the count says nothing about it.

    Returns:
        Issue numbers whose current execution state changed.

    Raises:
        BridgeError: If a ready report has no valid commit, or a stated
            backlog is not a count of zero or more.
    """
    if outcome == READY and not COMMIT.fullmatch(commit):
        raise BridgeError("Ready work must name its exact Git commit.")
    if backlog_count is not None and (
        type(backlog_count) is not int or backlog_count < 0
    ):
        raise BridgeError("A reported backlog must be a count of zero or more.")
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
            if reaches(ledger["issues"], resumed_by, number):
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
        if backlog_count is not None:
            execution["backlog"] = backlog_count
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

    Completion closes the ownership generation it names, so the mail that
    generation sent is retired with it: nothing it asked for can still be
    answered, and its senders stop waiting for acknowledgements of it.

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
        if execution["state"] not in (READY, RECOVERY):
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
        drop_offer(directory, record)
        record.update(
            owner=None,
            offer=None,
            request=None,
            execution=execution,
            completed_by=owner,
            completed_at=now,
        )
        append_history(
            record,
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
            },
        )
        _reconcile_dependents(ledger, issue, now)
        ledger["revision"] += 1
        write_json(directory / "issues.json", ledger)
    from agent_parley import store

    store.supersede_project_claim(
        directory, claim_id, f"issue #{issue} completed"
    )
    return record


def integration_failed(
    directory: Path, issue: str, claim_id: str, reason: str, result: str
) -> bool:
    """Returns integrated work to its owner as repair work.

    The claim stays with its current owner and generation: repair is that
    owner's work, and moving it is left to the ordinary claim, handoff and
    recovery rules, so it is never taken from a live lane here. The issue
    moves to the recovery state, which its owner resumes and which is never
    complete, so everything waiting on it stays held. Only the generation
    the integration named is changed, and recording the same failure again
    changes nothing.

    Args:
        directory: Private project state directory.
        issue: Issue number whose integration failed.
        claim_id: Ownership generation the integration carried.
        reason: Bounded account of the failure.
        result: Base commit left unverified, or empty for a conflict.

    Returns:
        Whether the ledger changed.

    Raises:
        BridgeError: If the ledger cannot be locked.
    """
    with lock(directory / "issues.lock", timeout=1):
        ledger = _snapshot(directory)
        record = ledger["issues"].get(issue)
        if not record or record.get("claim_id") != claim_id:
            return False
        execution = state(record)
        blocker = {"reason": reason, "commit": result}
        if execution["state"] not in (READY, RECOVERY) or (
            execution["state"] == RECOVERY and execution["blocker"] == blocker
        ):
            return False
        execution.update(
            state=RECOVERY,
            next_action="repair integration",
            updated_at=time.time(),
            blocker=blocker,
        )
        record["execution"] = execution
        ledger["revision"] += 1
        write_json(directory / "issues.json", ledger)
    return True


def settle_dependencies(directory: Path) -> list[tuple[str, str]]:
    """Drops dependency edges whose blocker is satisfied or no longer recorded.

    Completion frees its dependents at the instant it is recorded, which
    misses an edge added afterwards and an edge to an issue the ledger does
    not hold. A blocker released after it ended on the forge is satisfied
    too, yet no completion is ever recorded for it. Such an edge can never
    clear on its own, and while it stands
    the waiting issue can neither report ready nor be listed as unclaimed,
    whether or not anybody still owns it. The supervisor calls this on every
    poll, so the edge is reconciled whoever holds the issue.

    Args:
        directory: Private state directory for the common repository.

    Returns:
        The waiting issue and dropped blocker of every removed edge.

    Raises:
        BridgeError: If the ledger cannot be locked.
    """
    with lock(directory / "issues.lock", timeout=1):
        ledger = _snapshot(directory)
        records = ledger["issues"]
        stale = sorted(
            {
                blocker
                for record in records.values()
                for blocker in record.get("blocked_by", [])
                if blocker not in records or satisfied(records[blocker])
            },
            key=int,
        )
        dropped = [
            (number, blocker)
            for number, record in records.items()
            for blocker in record.get("blocked_by", [])
            if blocker in stale
        ]
        if not dropped:
            return []
        now = time.time()
        for blocker in stale:
            _reconcile_dependents(ledger, blocker, now)
        ledger["revision"] += 1
        write_json(directory / "issues.json", ledger)
    return sorted(dropped, key=lambda edge: (int(edge[0]), int(edge[1])))


def _reconcile_dependents(ledger: dict, issue: str, now: float) -> None:
    """Frees the issues that waited on one that has reached a terminal state.

    Args:
        ledger: Mutable issue ledger being written.
        issue: Issue number that has just become complete.
        now: Instant the terminal transition was recorded at.
    """
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
            if isinstance(condition, dict) and condition.get("kind") == "issue"
            else None
        )
        if (
            not waiting["blocked_by"]
            and waiting_execution["state"] == BLOCKED
            and dependency
            and satisfied(dependency)
        ):
            waiting_execution.update(
                state=RUNNING if waiting.get("owner") else QUEUED,
                next_action=("resume" if waiting.get("owner") else "claim"),
                updated_at=now,
                blocker="",
                resume_when="",
            )
            waiting["execution"] = waiting_execution


def resolve(
    directory: Path,
    issue: str,
    *,
    evidence: dict,
    outcome: str,
    actor: str,
    reason: str = "",
    claim_id: str | None = None,
) -> dict:
    """Ends a claim whose holder never answered its completion reminder.

    This is the operator's or the service's transition, not the holder's,
    and it is recorded as its own action so history never reads as though
    the lane filed the work itself. The operator reaches it only for a claim
    the supervisor has escalated as an unresolved completion, so a holder
    that answers is never resolved out from under it. The supervisor reaches
    it without an escalation by naming the claim its forge reading observed,
    once a merged pull request closed the issue inside that generation, so a
    claim taken again since that reading is never ended by it. Either way
    the caller has already correlated the evidence with the current
    ownership generation.

    A merged pull request names the commit that carries the work, so the
    ``complete`` outcome records that commit and frees the issues waiting on
    it. The supervisor may also complete a claim whose holder reported ready
    once the forge reports its issue closed without such a pull request,
    and then records the commit the ready report named. A closed pull
    request integrated nothing, so the ``release`` outcome
    returns the work to the queue instead; it supersedes a stale ready state,
    because the generation that reported ready has ended on the forge.

    Args:
        directory: Private project state directory.
        issue: Issue number being resolved.
        evidence: Forge observation justifying the transition, carrying the
            branch, the pull request state, its merge commit where one exists
            and the instant it was observed.
        outcome: ``complete`` for merged work, ``release`` to requeue it.
        actor: Operator identity recording the transition.
        reason: Operator rationale kept beside the evidence.
        claim_id: Claim the supervisor observed ending on the forge, which
            stands in for the escalation and must still be current.

    Returns:
        The resolved issue record.

    Raises:
        BridgeError: If the issue is unheld, carries no escalation for its
            current generation, holds a claim other than `claim_id`, or the
            evidence does not support the outcome.
    """
    if outcome not in ("complete", "release"):
        raise BridgeError("Resolution outcome must be complete or release.")
    commit = str(evidence.get("commit") or "")
    merged = evidence.get("state") == "MERGED" and COMMIT.fullmatch(commit)
    refusal = (
        "Completion needs a merged pull request naming its merge commit; "
        "release the claim instead."
    )
    if outcome == "complete" and not merged and claim_id is None:
        raise BridgeError(refusal)
    with lock(directory / "issues.lock", timeout=1):
        ledger = _snapshot(directory)
        record = ledger["issues"].get(issue)
        if not record or not record.get("owner"):
            raise BridgeError(f"Issue #{issue} has no owner.")
        escalation = record.get("unresolved_completion") or {}
        if claim_id is not None:
            if claim_id != record.get("claim_id"):
                raise BridgeError(
                    f"Issue #{issue} was claimed again after the forge "
                    "reading that ended it."
                )
        elif escalation.get("claim_id") != record.get("claim_id"):
            raise BridgeError(
                f"Issue #{issue} has no unresolved completion; the supervisor "
                "escalates only after the holder leaves its completion "
                "reminders unanswered."
            )
        holder = record["owner"]
        now = time.time()
        execution = state(record)
        if outcome == "complete" and not merged:
            commit = str(execution.get("commit") or "")
            if (
                evidence.get("state") not in ("MERGED", "CLOSED")
                or execution["state"] != READY
                or execution["claim_id"] != record.get("claim_id")
                or not COMMIT.fullmatch(commit)
            ):
                raise BridgeError(refusal)
        if outcome == "complete":
            execution.update(
                state=COMPLETE,
                next_action="none",
                updated_at=now,
                commit=commit,
                integrated_commit=commit,
                gate={
                    "command": [],
                    "status": "observed on the forge",
                    "commit": commit,
                    "at": now,
                },
                blocker="",
                resume_when="",
            )
            record.update(completed_by=holder, completed_at=now)
        else:
            execution.update(
                state=QUEUED,
                claim_id=None,
                next_action="claim",
                updated_at=now,
                blocker="",
                resume_when="",
                commit="",
                gate=None,
            )
        drop_offer(directory, record)
        record.update(
            owner=None,
            offer=None,
            request=None,
            deadline=None,
            execution=execution,
            resolution={
                "outcome": outcome,
                "actor": actor,
                "holder": holder,
                "reason": reason,
                "at": now,
                "claim_id": record.get("claim_id"),
                "evidence": dict(evidence),
            },
        )
        record.pop("unresolved_completion", None)
        append_history(
            record,
            {
                "action": "resolve",
                "actor": actor,
                "at": now,
                "owner": None,
                "offer": None,
                "request": None,
                "offer_id": None,
                "claim_id": record.get("claim_id"),
                "outcome": outcome,
                "holder": holder,
                "evidence": dict(evidence),
            },
        )
        if outcome == "complete":
            _reconcile_dependents(ledger, issue, now)
        ledger["revision"] += 1
        write_json(directory / "issues.json", ledger)
    from agent_parley import store

    store.supersede_project_claim(
        directory, str(record.get("claim_id") or ""), f"issue #{issue} resolved"
    )
    return record


def _issue_number(value: str, label: str = "Issue") -> str:
    """Returns one positive decimal issue number."""
    number = value[1:] if value.startswith("#") else value
    if not number.isdigit() or int(number) < 1:
        raise BridgeError(f"{label} must be a positive issue number.")
    return str(int(number))


def reaches(records: dict, start: str, target: str) -> bool:
    """Reports whether dependency edges lead from start to target.

    Args:
        records: Every issue record in the ledger, keyed by number.
        start: Issue the walk starts from.
        target: Issue whose reachability is asked about.

    Returns:
        True when following ``blocked_by`` edges from start arrives at target,
        which is exactly when an edge from target to start closes a cycle.
    """
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
