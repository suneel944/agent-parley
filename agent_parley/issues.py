"""Local, repository-scoped issue ownership and explicit handoffs."""

import contextlib
import json
import re
import sqlite3
import time
import uuid
from pathlib import Path

from agent_parley import attachments, lifecycle, retries
from agent_parley.state import BridgeError, Transient, lock, write_json

MAX_BLOCKERS = 10
SUPERVISION_ERROR = "supervision-error.json"
OPERATOR = "operator"
PEER = "peer"
MAX_REASON = 2000
MAX_SUMMARY_BYTES = 2048
MAX_REMAINING = 12
MAX_REMAINING_BYTES = 200
MAX_RESERVATIONS = 32
MAX_RESERVATION_BYTES = 240
COMMIT = re.compile(r"[0-9a-f]{7,40}")


def deadline_state(record: dict, now: float = 0.0) -> dict:
    """Derives the deadline and retry state a claim currently reads as.

    A deadline is a reporting device, never a transfer. Past it the claim is
    overdue and says by how much; ownership stays exactly where it was, and
    only an explicit release or an accepted handoff moves it. The state is
    derived from the stored timestamps whenever a record is read, so the
    runtime gains no scheduler and a stopped service produces no phantom
    transitions.

    Args:
        record: Published ledger record for one issue.
        now: Instant to evaluate against; the current time when zero.

    Returns:
        The recorded deadline, the seconds it is overdue by, the attempts
        recorded against it, the budget those attempts are measured against,
        and whether that budget is exceeded.
    """
    stamp = now or time.time()
    deadline = record.get("deadline")
    over = int(stamp - deadline) if deadline and stamp > deadline else 0
    budget = record.get("budget")
    attempts = int(record.get("attempts", 0) or 0)
    return {
        "deadline": deadline,
        "overdue": bool(over),
        "overdue_seconds": over,
        "attempts": attempts,
        "budget": budget,
        "budget_exceeded": bool(budget) and attempts > int(budget or 0),
    }


def offer_state(offer: dict | None, now: float = 0.0) -> dict:
    """Derives the deadline state of one pending handoff offer."""
    stamp = now or time.time()
    deadline = (offer or {}).get("deadline")
    over = int(stamp - deadline) if deadline and stamp > deadline else 0
    return {
        "deadline": deadline,
        "overdue": bool(over),
        "overdue_seconds": over,
    }


def released(record: dict) -> bool:
    """Reports whether an issue carries an explicit release or completion.

    The reading uses recorded transitions only. An issue reads as released
    when its own history ends in a release or its current execution generation
    is verified complete. A closed pull request is neither one. An issue that
    was released and claimed again reads as held, because its history no
    longer ends in a release.

    Args:
        record: Published ledger record for one issue, or an empty mapping.

    Returns:
        Whether the issue is explicitly released or completed.
    """
    history = record.get("history") or []
    if history and history[-1].get("action") in ("release", "resolve"):
        return True
    return lifecycle.state(record)["state"] == lifecycle.COMPLETE


def unresolved_completion(record: dict) -> dict:
    """Derives the unresolved-completion escalation a claim reads as.

    The marker belongs to the ownership generation it was recorded against, so
    a claim that was released and taken again reads as resolved until the
    supervisor observes the new generation the same way.

    Args:
        record: Published ledger record for one issue, or an empty mapping.

    Returns:
        Whether the current generation carries an escalation, the clause that
        states why, the observed pull request state, the unanswered reminders
        counted and the instant that state was observed.
    """
    marker = record.get("unresolved_completion") or {}
    standing = bool(marker) and marker.get("claim_id") == record.get("claim_id")
    return {
        "unresolved": standing,
        "reason": marker.get("reason", "") if standing else "",
        "branch_state": marker.get("state", "") if standing else "",
        "reminders": int(marker.get("reminders", 0) or 0) if standing else 0,
        "observed_at": marker.get("observed_at") if standing else None,
    }


def offer_source(offer: dict | None) -> str:
    """Reports who raised one pending offer.

    Args:
        offer: Pending offer recorded on an issue, or None.

    Returns:
        ``operator`` for an offer the command line recorded, and ``peer`` for
        an offer one lane made to another, including every offer recorded
        before offers carried a source.
    """
    return (offer or {}).get("source") or PEER


def handoff_payload(carried: dict | None) -> dict:
    """Validates the structured work state one handoff transfers.

    A summary is prose a reader interprets. These fields are the same transfer
    stated once, in a shape a receiver can act on without re-deriving it: the
    commit the work stands on, the reservation keys its owner holds, the work
    it states as remaining, and the reference to the diff kept beside the
    offer. Every field is optional, because a lane may know none of them, and
    an absent field is recorded as empty rather than guessed.

    Args:
        carried: Structured fields the command line derived for this offer.

    Returns:
        The fields the offer records, with reservation keys deduplicated and
        ordered so the same holding always reads the same way.

    Raises:
        BridgeError: If the commit is not a Git object name, or the
            reservation or remaining-work lists exceed their bounds.
    """
    carried = carried or {}
    commit = str(carried.get("commit") or "")
    if commit and not COMMIT.fullmatch(commit):
        raise BridgeError(
            "Handoff commit must be a Git object name of 7 to 40 hex digits."
        )
    held = [str(key) for key in carried.get("reservations") or []]
    if len(held) > MAX_RESERVATIONS or any(
        len(key.encode()) > MAX_RESERVATION_BYTES for key in held
    ):
        raise BridgeError(
            f"A handoff carries at most {MAX_RESERVATIONS} reservation keys "
            f"of {MAX_RESERVATION_BYTES} bytes each."
        )
    left = [
        stripped
        for item in carried.get("remaining") or []
        if (stripped := str(item).strip())
    ]
    if len(left) > MAX_REMAINING or any(
        len(item.encode()) > MAX_REMAINING_BYTES for item in left
    ):
        raise BridgeError(
            f"A handoff carries at most {MAX_REMAINING} remaining-work items "
            f"of {MAX_REMAINING_BYTES} bytes each."
        )
    fields: dict = {
        "commit": commit,
        "reservations": sorted(set(held)),
        "remaining": left,
    }
    if diff := str(carried.get("diff") or ""):
        fields["diff"] = attachments.validate(diff)
        fields["diff_bytes"] = int(carried.get("diff_bytes", 0) or 0)
    return fields


def handoff_fields(source: dict | None) -> dict:
    """Reports the structured work state an offer or accepted handoff carries.

    Args:
        source: Pending offer, accepted handoff, or None.

    Returns:
        The commit, reservation keys, remaining work and diff reference, each
        empty where the record carries none. An operator offer and every offer
        recorded before handoffs carried structure read as empty rather than
        absent, so one shape answers for all of them.
    """
    source = source or {}
    return {
        "commit": source.get("commit", ""),
        "reservations": list(source.get("reservations") or []),
        "remaining": list(source.get("remaining") or []),
        "diff": source.get("diff", ""),
        "diff_bytes": int(source.get("diff_bytes", 0) or 0),
    }


def attempt(directory: Path, agent: str, numbers: list[str]) -> dict:
    """Records one more attempt on every issue a lane still holds.

    A lane that reports ``blocked`` on work it still owns has spent an
    attempt. The count is recorded beside the claim so an operator can read
    attempts against the budget; nothing is released, refused or transferred
    by reaching it.

    Args:
        directory: Private state directory for the common repository.
        agent: Lane that reported the blocker.
        numbers: Issue numbers that lane owns.

    Returns:
        The recorded attempt count per issue.

    Raises:
        BridgeError: If the ledger cannot be locked.
    """
    counted: dict[str, int] = {}
    if not numbers:
        return counted
    with lock(directory / "issues.lock", timeout=1):
        state = snapshot(directory)
        for number in numbers:
            record = state["issues"].get(number)
            if not record or record.get("owner") != agent:
                continue
            record["attempts"] = int(record.get("attempts", 0) or 0) + 1
            counted[number] = record["attempts"]
        if counted:
            state["revision"] += 1
            write_json(directory / "issues.json", state)
    return counted


def parse_issue(value: str, label: str = "Issue") -> str:
    """Returns a repository issue number without its optional prefix.

    Args:
        value: Candidate issue number, optionally prefixed with #.
        label: Field name reported when validation fails.

    Returns:
        The bare digits of a valid issue number.

    Raises:
        BridgeError: If the value is not a positive number of up to 18 digits.
    """
    if not re.fullmatch(r"#?[1-9][0-9]{0,17}", value or ""):
        raise BridgeError(
            f"{label} must be a positive number of up to 18 digits, "
            "e.g. 432 or #432."
        )
    return value.lstrip("#")


def snapshot(directory: Path) -> dict:
    """Returns the published issue ledger, or an empty ledger."""
    path = directory / "issues.json"
    return (
        json.loads(path.read_text())
        if path.exists()
        else {"revision": 0, "issues": {}}
    )


def holders(state: dict) -> dict[str, list[str]]:
    """Maps each lane to the issue numbers the ledger records it owning.

    Args:
        state: Published issue ledger.

    Returns:
        Owner name to its owned issue numbers, ordered numerically. A lane
        that owns nothing is absent rather than present with an empty list.
    """
    owned: dict[str, list[str]] = {}
    for number in sorted(state.get("issues", {}), key=int):
        record = state["issues"][number]
        owner = record.get("owner")
        if lifecycle.state(record)["state"] == lifecycle.COMPLETE:
            continue
        if owner:
            owned.setdefault(owner, []).append(number)
    return owned


def waiters(state: dict) -> dict[str, list[str]]:
    """Maps each blocking issue to the owned issues recorded as waiting on it.

    Only owned issues are counted, because finishing a blocker matters when a
    lane is actually held up by it and an unowned waiter is nobody's wait.

    Args:
        state: Published issue ledger.

    Returns:
        Blocker number to the issue numbers waiting on it, ordered
        numerically. An issue nothing waits on is absent.
    """
    waiting: dict[str, list[str]] = {}
    issues = state.get("issues", {})
    for number in sorted(issues, key=int):
        if not issues[number].get("owner"):
            continue
        for blocker in issues[number].get("blocked_by", []):
            waiting.setdefault(blocker, []).append(number)
    return waiting


def unclaimed(state: dict) -> list[str]:
    """Orders the unclaimed ledger issues no recorded dependency blocks.

    An issue is unclaimed when the ledger records neither an owner nor a
    pending offer for it, and unblocked when it waits on nothing. The ledger
    records no completion, so an issue that waits on anything is left out
    rather than guessed to be ready.

    The order puts first the issues that other owned issues wait on, because
    finishing one of those releases a peer, and falls back to the issue number
    so the same ledger always produces the same list.

    Args:
        state: Published issue ledger.

    Returns:
        Issue numbers a lane could claim, most unblocking first.
    """
    issues = state.get("issues", {})
    waiting = waiters(state)
    return sorted(
        (
            number
            for number, record in issues.items()
            if not record.get("owner")
            and not record.get("offer")
            and not record.get("blocked_by")
            and lifecycle.state(record)["authorized"]
            and lifecycle.state(record)["state"] != lifecycle.COMPLETE
        ),
        key=lambda number: (-len(waiting.get(number, [])), int(number)),
    )


def _refuse(directory: Path, scope: str, fingerprint: str, detail: str) -> None:
    """Records a refused transition so a retry is refused identically.

    Only refusals that describe the ledger reach this record. A transient
    refusal, such as lock contention or recovery evidence that changed, is
    raised without it, so a retry with the same key is evaluated again.

    A refusal changed no ownership, so its ledger write never happened and the
    key is recorded afterwards under the lock again. An interruption before
    that record leaves the key absent, and the retry is evaluated and refused
    again, which costs a repeated evaluation rather than a replayed effect.
    Ownership granted after the refusal never reaches a call carrying the
    refused key, because the recorded refusal answers it.
    """
    with contextlib.suppress(BridgeError, OSError, ValueError):
        with lock(directory / "issues.lock", timeout=1):
            state = snapshot(directory)
            if scope in state.get("retries", {}):
                return
            retries.remember(state, scope, fingerprint, retries.DENIED, detail)
            write_json(directory / "issues.json", state)


def change(
    directory: Path,
    agent: str,
    action: str,
    issue: str,
    *,
    participants: set[str],
    key: str = "",
    to: str | None = None,
    summary: str = "",
    offer_id: str | None = None,
    on: str | None = None,
    title: str | None = None,
    within: float | None = None,
    defaults: dict | None = None,
    carried: dict | None = None,
    take_orphaned: bool = False,
    takeover: dict | None = None,
) -> dict:
    """Applies one issue transition once, however often it is retried.

    A transition carrying an idempotency key is applied once. A repeat from
    the same lane with the same key returns the first result and changes
    nothing, and a repeat carrying different arguments is refused by name, so
    a stale retry cannot land on a different issue or recipient.

    Args:
        directory: Private state directory for the common repository.
        agent: Acting lane, resolved by the CLI from its worktree, or the
            operator identity for an assignment.
        action: Claim, release, offer, accept, decline, cancel, block,
            unblock, assign, or unassign.
        issue: Positive repository issue number, optionally prefixed with #.
        participants: Every participant registered for this project.
        key: Idempotency key. Empty applies the transition without retry
            bookkeeping, which stays correct for transitions that are already
            idempotent, such as reclaiming an issue this lane owns.
        to: Recipient participant for an offer or an operator assignment.
        summary: Peer-provided handoff context, or the operator's stated
            reason for an assignment.
        offer_id: Exact current offer required for acceptance or decline.
        on: Issue this one waits on, for a block or unblock.
        title: Optional forge-supplied issue title recorded on a claim. It is
            display context, so it is excluded from the arguments a key is
            compared against and a changed title never refuses a retry.
        within: Seconds this claim, offer or acknowledgement is expected to
            take, recorded as a deadline beside the record.
        defaults: Project deadline and attempt-budget defaults.
        carried: Structured work state an offer transfers beside its summary.
            It is read from the lane at the moment the offer is made, so like
            a title it is excluded from the arguments a key is compared
            against and a changed diff never refuses a retry.
        take_orphaned: Whether a claim may take an issue whose owner the
            supervisor marked orphaned. Like a title it is excluded from the
            arguments a key is compared against, so a repeat carrying the key
            of a recorded take replays that take rather than claiming again.
        takeover: Revalidated owner generation and durable checkpoint for an
            orphan take. It is excluded from retry arguments because it is
            evidence read at execution time rather than caller intent.

    Returns:
        The persisted issue record, including transition history.

    Raises:
        BridgeError: If validation, ownership, offer, retry, or lock checks
            fail.
    """
    transition: dict = {
        "to": to,
        "summary": summary,
        "offer_id": offer_id,
        "on": on,
        "within": within,
    }
    if not key:
        return _change(
            directory,
            agent,
            action,
            issue,
            participants=participants,
            title=title,
            defaults=defaults,
            carried=carried,
            take_orphaned=take_orphaned,
            takeover=takeover,
            **transition,
        )
    key = retries.validate(key)
    fingerprint = retries.digest(
        action, {"issue": parse_issue(issue), **transition}
    )
    scope = retries.scope(agent, action, key)
    try:
        return _change(
            directory,
            agent,
            action,
            issue,
            participants=participants,
            scope=scope,
            fingerprint=fingerprint,
            title=title,
            defaults=defaults,
            carried=carried,
            take_orphaned=take_orphaned,
            takeover=takeover,
            **transition,
        )
    except Transient:
        raise
    except BridgeError as exc:
        _refuse(directory, scope, fingerprint, str(exc))
        raise


def _drop_offer(directory: Path, record: dict) -> None:
    """Removes the attachments of an offer that is leaving the record.

    A declined or cancelled offer takes its spilled summary and its attached
    diff with it, so an answered handoff leaves no unreferenced body behind.

    Args:
        directory: Private state directory for the common repository.
        record: Ledger record whose pending offer is being cleared.
    """
    offer = record.get("offer") or {}
    for field in ("attachment", "diff"):
        if offer.get(field):
            attachments.remove(directory, str(offer[field]))


def _clear_recovery(record: dict) -> dict:
    """Removes the previous generation's take and orphan marker.

    Both describe one ownership generation. Left on the record past a change
    of owner, a take makes the next claim restore a dead owner's checkpoint
    over work handed on since, and an orphan marker hides the new owner's
    continue offers and names an owner that no longer holds the issue.

    Args:
        record: Mutable issue record whose ownership is changing.

    Returns:
        The removed fields, for the transition's history entry.
    """
    return {
        field: record.pop(field)
        for field in ("taken", "orphan")
        if record.get(field)
    }


def _unseen() -> dict:
    """Returns an empty record for an issue the ledger has not seen yet."""
    return {
        "owner": None,
        "offer": None,
        "request": None,
        "blocked_by": [],
        "history": [],
        "deadline": None,
        "attempts": 0,
        "budget": None,
        "execution": lifecycle.initial(),
    }


def _taken(
    directory: Path,
    agent: str,
    record: dict,
    issue: str,
    orphan: dict,
    takeover: dict | None,
) -> dict:
    """Records which owner an orphaned claim was taken from, and why.

    The previous owner is read from the marker the supervisor wrote rather
    than from the taking lane, so a take states an observation the runtime
    made and never a peer's opinion of who is alive.

    Args:
        directory: Private state directory for the common repository.
        agent: Participant receiving the ownership generation.
        record: Published record for the issue being taken.
        issue: Repository issue number the take names.
        orphan: Orphan marker the record carries, if any.
        takeover: Revalidated claim and orphan identities plus its checkpoint.

    Returns:
        The previous owner, the reason it was marked orphaned, the instant of
        the take and the reservation keys that owner still held.

    Raises:
        BridgeError: If the issue carries no orphan marker, or the marker
            names an owner other than the lane that currently holds it.
    """
    if not orphan or orphan.get("owner") != record["owner"]:
        raise BridgeError(
            f"Issue #{issue} is owned by {record['owner']}, which does not "
            "read as orphaned; ask that lane for a handoff instead."
        )
    takeover = takeover or {}
    if (
        takeover.get("claim_id") != record.get("claim_id")
        or takeover.get("orphan_id") != orphan.get("id")
        or not takeover.get("checkpoint")
    ):
        raise Transient(
            f"Issue #{issue} recovery evidence changed; inspect and retry."
        )
    from agent_parley import recovery

    fence = recovery.commit_takeover(
        directory, agent, takeover, issue, record, orphan
    )
    return {
        "from": record["owner"],
        "reason": orphan.get("reason", ""),
        "at": time.time(),
        "reservations": list(orphan.get("reservations", [])),
        "checkpoint": takeover["checkpoint"],
        "claim_id": record["claim_id"],
        "orphan_id": orphan.get("id", ""),
        "fence": fence["id"],
    }


def _assign(
    record: dict | None,
    issue: str,
    *,
    recipient: str | None,
    reason: str,
    participants: set[str],
    budgets: dict,
) -> dict:
    """Records an operator offer, or a request to the issue's current owner.

    An operator directs work by offering it. An unheld issue receives the
    offer directly and the named lane answers it like any other. A held issue
    is not taken from its owner: the operator's wish is recorded as a request
    the owner answers, and only that answer creates the offer to the named
    lane. Ownership therefore still moves on an explicit acceptance alone.

    Args:
        record: Published record for this issue, or None when it has none.
        issue: Repository issue number the offer is recorded against.
        recipient: Participant the operator offers the issue to.
        reason: Operator-provided rationale, travelling with the offer.
        participants: Every participant registered for this project.
        budgets: Project deadline and attempt-budget defaults.

    Returns:
        The record carrying either the recorded offer or the recorded request.

    Raises:
        BridgeError: If the recipient is not a participant, already owns the
            issue, the reason is too long, or an offer or request is pending.
    """
    if recipient not in participants:
        raise BridgeError(
            "Choose a participant in this project; "
            "run agent-parley participant list."
        )
    if len(reason.encode()) > MAX_REASON:
        raise BridgeError(
            f"Assignment reason must be at most {MAX_REASON} bytes."
        )
    record = record or _unseen()
    if record["owner"] == recipient:
        raise BridgeError(f"Issue #{issue} is already owned by {recipient}.")
    if record["offer"]:
        raise BridgeError(
            "An offer is pending on this issue; answer or cancel it first."
        )
    if record.get("request"):
        raise BridgeError(
            "An operator request is pending on this issue; "
            "withdraw it with issue assign --unassign."
        )
    entry = {
        "id": uuid.uuid4().hex,
        "to": recipient,
        "reason": reason,
        "created": time.time(),
        "source": OPERATOR,
    }
    if record["owner"]:
        record["request"] = entry
        return record
    record["offer"] = _operator_offer(entry, issue, budgets)
    return record


def _operator_offer(entry: dict, issue: str, budgets: dict) -> dict:
    """Shapes an operator offer so it reads exactly like a peer offer.

    Args:
        entry: Recorded operator intent, carrying its identifier, recipient
            and reason.
        issue: Repository issue number the offer names.
        budgets: Project deadline and attempt-budget defaults.

    Returns:
        A pending offer carrying the operator's reason as its summary and the
        identifier the operator was given, so one identifier names the work
        from the command that recorded it to the lane that answers it. The
        project's answer deadline applies when one is configured, measured
        from the moment the offer started waiting.
    """
    answer = budgets.get("offer")
    return {
        **entry,
        "summary": entry["reason"] or f"Operator offered issue #{issue}.",
        "created": time.time(),
        "deadline": time.time() + answer if answer else None,
    }


def _withdraw(record: dict | None, issue: str) -> dict:
    """Withdraws an operator offer or request that nobody has accepted.

    Args:
        record: Published record for this issue, or None when it has none.
        issue: Repository issue number the withdrawal names.

    Returns:
        The record with the pending operator offer or request removed.

    Raises:
        BridgeError: If no operator offer is pending, naming the lane that
            holds the issue when one was already accepted.
    """
    if record and record.get("request"):
        record["request"] = None
        return record
    if record and offer_source(record["offer"]) == OPERATOR:
        record["offer"] = None
        return record
    if record and record["owner"]:
        raise BridgeError(
            f"No operator offer is pending on issue #{issue}; "
            f"{record['owner']} accepted it and only that lane can hand it on."
        )
    raise BridgeError(f"No operator offer is pending on issue #{issue}.")


def _answer_request(
    record: dict, agent: str, issue: str, action: str, budgets: dict
) -> dict:
    """Applies the owner's answer to an operator handoff request.

    Args:
        record: Published record carrying the pending request.
        agent: Lane answering the request.
        issue: Repository issue number the request names.
        action: Accept or decline.
        budgets: Project deadline and attempt-budget defaults.

    Returns:
        The record carrying the offer the owner authorized, or the record with
        the request removed when the owner declined.

    Raises:
        BridgeError: If a lane other than the owner answers.
    """
    request = record["request"]
    if record["owner"] != agent:
        raise BridgeError(f"Only {record['owner']} can answer this request.")
    if action == "accept":
        record["offer"] = _operator_offer(request, issue, budgets)
    record["request"] = None
    return record


def _change(
    directory: Path,
    agent: str,
    action: str,
    issue: str,
    *,
    participants: set[str],
    scope: str = "",
    fingerprint: str = "",
    to: str | None = None,
    summary: str = "",
    offer_id: str | None = None,
    on: str | None = None,
    title: str | None = None,
    within: float | None = None,
    defaults: dict | None = None,
    carried: dict | None = None,
    take_orphaned: bool = False,
    takeover: dict | None = None,
) -> dict:
    """Applies one issue transition while holding the repository lock.

    Args:
        directory: Private state directory for the common repository.
        agent: Acting lane, resolved by the CLI from its worktree, or the
            operator identity for an assignment.
        action: Claim, release, offer, accept, decline, cancel, block,
            unblock, assign, or unassign.
        issue: Positive repository issue number, optionally prefixed with #.
        participants: Every participant registered for this project.
        scope: Retained key this transition is recorded under. Empty records
            no key and applies the transition directly.
        fingerprint: Digest of the arguments the key was issued against.
        to: Recipient participant for an offer or an operator assignment.
        summary: Peer-provided handoff context, or the operator's stated
            reason for an assignment.
        offer_id: Exact current offer required for acceptance or decline.
        on: Issue this one waits on, for a block or unblock.
        title: Optional forge-supplied issue title recorded on a claim. It is
            display context only, never ownership authority, and an absent
            title leaves any previously recorded one in place.
        within: Seconds this claim, offer or acknowledgement is expected to
            take, recorded as a deadline beside the record. None takes the
            project default, and a project without one records no deadline.
        defaults: Project deadline and attempt-budget defaults.
        carried: Structured work state an offer transfers beside its summary.
            An acceptance moves those fields onto the record as the accepted
            handoff, so the receiver reads the commit, the reservation keys
            and the remaining work it inherited without re-deriving them.
        take_orphaned: Whether a claim may take an issue the supervisor
            marked orphaned. Only the recorded owner of that marker is taken
            from, and the take is recorded as its own transition naming that
            owner and the reason the marker gave.
        takeover: Revalidated owner generation and durable checkpoint for an
            orphan take.

    A transition that ends an ownership generation, by releasing it, by
    handing it to another lane, by taking it from an orphaned owner or by the
    owner re-claiming its own orphan-marked issue, also retires the mail that
    generation sent, and moves the generation's take and orphan marker into
    the transition's history entry. Supersession is written where the
    claim changes rather than derived when a lane is woken, because only the
    transition knows which generation stopped mattering and why.

    Returns:
        The persisted issue record, including transition history.

    Raises:
        BridgeError: If validation, ownership, offer, or lock checks fail.
    """
    issue = parse_issue(issue)
    budgets = defaults or {}
    logged = action
    blocker = ""
    retired = ""
    retired_reason = ""
    cleared: dict = {}
    if action in ("block", "unblock"):
        blocker = parse_issue(on or "", "Blocker")
        if blocker == issue:
            raise BridgeError("An issue cannot wait on itself.")
    with lock(directory / "issues.lock", timeout=1):
        state = snapshot(directory)
        if scope and (recorded := state.get("retries", {}).get(scope)):
            return retries.replayed(
                recorded, action, scope.rsplit("\x00", 1)[1], fingerprint
            )
        record = state["issues"].get(issue)
        if action == "claim":
            orphan = (record or {}).get("orphan") or {}
            taken: dict = {}
            if record and record["owner"]:
                if record["owner"] == agent:
                    if not orphan:
                        return record
                elif take_orphaned:
                    taken = _taken(
                        directory, agent, record, issue, orphan, takeover
                    )
                    logged = "take"
                    retired = str(record.get("claim_id") or "")
                    retired_reason = (
                        f"issue #{issue} taken from {record['owner']}"
                    )
                else:
                    raise BridgeError(
                        f"Issue #{issue} is owned by {record['owner']}."
                        + (
                            " That lane reads as orphaned; take it with "
                            f"issue claim {issue} --take-orphaned."
                            if orphan
                            else ""
                        )
                    )
            elif take_orphaned:
                raise BridgeError(
                    f"Issue #{issue} has no orphaned owner to take it from; "
                    "claim it without --take-orphaned."
                )
            previous = record or {}
            if not taken:
                cleared = _clear_recovery(dict(previous))
            reclaimed = previous.get("owner") == agent
            if reclaimed:
                retired = str(previous.get("claim_id") or "")
                retired_reason = f"issue #{issue} re-claimed by {agent}"
            budget = budgets.get("attempts")
            expected = within if within is not None else budgets.get("claim")
            record = {
                "owner": agent,
                "offer": None,
                "request": None,
                "blocked_by": previous.get("blocked_by", []),
                "history": previous.get("history", []),
                "deadline": time.time() + expected if expected else None,
                "attempts": 0,
                "budget": budget or None,
                "claim_id": uuid.uuid4().hex[:16],
                "execution": previous.get("execution"),
            }
            lifecycle.claimed(record, record["claim_id"])
            resolved = title if title else previous.get("title")
            if resolved:
                record["title"] = resolved
            if taken or reclaimed:
                if inherited := previous.get("handoff"):
                    record["handoff"] = inherited
            if reclaimed and previous.get("attachment"):
                record["attachment"] = previous["attachment"]
            if taken:
                record["taken"] = taken
        elif action == "assign":
            record = _assign(
                record,
                issue,
                recipient=to,
                reason=summary.strip(),
                participants=participants,
                budgets=budgets,
            )
            lifecycle.authorize(record)
        elif action == "unassign":
            record = _withdraw(record, issue)
        else:
            answering = action in ("accept", "decline")
            if not record or not (
                record["owner"] or (answering and record["offer"])
            ):
                raise BridgeError(f"Issue #{issue} has no owner.")
            request = record.get("request")
            if answering and request and request["id"] == offer_id:
                record = _answer_request(record, agent, issue, action, budgets)
                logged = "authorize" if action == "accept" else "refuse"
            elif answering:
                offer = record["offer"]
                if not offer or offer["id"] != offer_id:
                    raise BridgeError(
                        "Handoff changed or was cancelled; inspect issue list."
                    )
                if offer["to"] != agent:
                    raise BridgeError(
                        "Only the named recipient can answer this handoff."
                    )
                if action == "accept":
                    expected = (
                        within if within is not None else budgets.get("claim")
                    )
                    inherited = {
                        **handoff_fields(offer),
                        "from": record["owner"],
                        "at": time.time(),
                    }
                    retired = str(record.get("claim_id") or "")
                    retired_reason = f"issue #{issue} reassigned to {agent}"
                    record.update(
                        owner=agent,
                        request=None,
                        deadline=(time.time() + expected if expected else None),
                        attempts=0,
                        budget=budgets.get("attempts") or None,
                        claim_id=uuid.uuid4().hex[:16],
                    )
                    lifecycle.claimed(record, record["claim_id"])
                    cleared = _clear_recovery(record)
                    if offer.get("attachment"):
                        record["attachment"] = offer["attachment"]
                    record["handoff"] = inherited
                else:
                    _drop_offer(directory, record)
                record["offer"] = None
            else:
                if record["owner"] != agent:
                    raise BridgeError(
                        f"Only {record['owner']} can change issue #{issue}."
                    )
                if action == "offer":
                    recipient = to
                    summary = summary.strip()
                    if recipient not in participants or recipient == agent:
                        raise BridgeError(
                            "Choose another participant in this project; "
                            "run agent-parley participant list."
                        )
                    if not summary:
                        raise BridgeError(
                            "Handoff summary must contain at least one "
                            "character."
                        )
                    if record["offer"]:
                        raise BridgeError(
                            "A handoff is pending; "
                            "cancel it before replacing it."
                        )
                    structured = handoff_payload(carried)
                    answer = (
                        within if within is not None else budgets.get("offer")
                    )
                    offered = uuid.uuid4().hex
                    summary, attached = attachments.spill(
                        directory,
                        "offer",
                        offered,
                        summary,
                        MAX_SUMMARY_BYTES,
                        agent,
                        [recipient],
                    )
                    record["offer"] = {
                        "id": offered,
                        "to": recipient,
                        "summary": summary,
                        "created": time.time(),
                        "deadline": time.time() + answer if answer else None,
                        **structured,
                    }
                    if attached:
                        record["offer"]["attachment"] = attached
                elif action == "cancel":
                    if not record["offer"]:
                        raise BridgeError("No handoff is pending.")
                    _drop_offer(directory, record)
                    record["offer"] = None
                elif action == "release":
                    retired = str(record.get("claim_id") or "")
                    retired_reason = f"issue #{issue} released"
                    lifecycle.released(record)
                    attachments.remove(directory, record.get("attachment", ""))
                    record.pop("attachment", None)
                    inherited = record.pop("handoff", None) or {}
                    attachments.remove(directory, inherited.get("diff", ""))
                    record.update(
                        owner=None, offer=None, request=None, deadline=None
                    )
                    cleared = _clear_recovery(record)
                elif action == "block":
                    waiting = record.get("blocked_by", [])
                    if blocker in waiting:
                        return record
                    if len(waiting) >= MAX_BLOCKERS:
                        raise BridgeError(
                            f"Issue #{issue} already waits on {MAX_BLOCKERS} "
                            "issues; drop one with issue unblock."
                        )
                    record["blocked_by"] = sorted([*waiting, blocker], key=int)
                elif action == "unblock":
                    waiting = record.get("blocked_by", [])
                    if blocker not in waiting:
                        raise BridgeError(
                            f"Issue #{issue} does not wait on #{blocker}."
                        )
                    record["blocked_by"] = [
                        number for number in waiting if number != blocker
                    ]
                else:
                    raise BridgeError("Unknown issue action.")
        history = {
            "action": logged,
            "actor": agent,
            "at": time.time(),
            "owner": record["owner"],
            "offer": record["offer"],
            "request": record.get("request"),
            "offer_id": offer_id,
            "claim_id": record.get("claim_id"),
        }
        if logged == "take":
            history["taken"] = dict(record["taken"])
        if cleared:
            history["cleared"] = cleared
        record["history"].append(history)
        state["issues"][issue] = record
        if scope:
            retries.remember(state, scope, fingerprint, retries.SERVED, record)
        state["revision"] += 1
        write_json(directory / "issues.json", state)
    if retired:
        from agent_parley import store

        store.supersede_project_claim(directory, retired, retired_reason)
    if action == "release":
        from agent_parley import roster, supervision

        try:
            manifest = roster.read(directory)
            if supervision.configuration(directory.parent.parent, manifest)[
                "prompts"
            ]:
                supervision.reminders(directory, manifest, set())
        except (BridgeError, OSError, ValueError, sqlite3.Error) as exc:
            note_supervision_error(directory, f"release #{issue}: {exc}")
    return record


def note_supervision_error(directory: Path, detail: str) -> None:
    """Records an optional supervision failure beside the issue ledger.

    Reminder generation runs after the ownership transaction has committed and
    released its lock, so its failure cannot undo the transition and must not
    be reported as one. The diagnostic is written where an operator and the
    supervisor can both see it, and the supervisor clears it once a later poll
    regenerates reminders successfully. Writing the diagnostic is itself best
    effort: failing to record a note must not fail a committed release.

    Args:
        directory: Private state directory for the common repository.
        detail: Operation and failure text to retain for an operator.
    """
    with contextlib.suppress(OSError):
        write_json(
            directory / SUPERVISION_ERROR,
            {"at": time.time(), "detail": detail},
        )


def _carried(source: dict) -> str:
    """Formats the structured work state one handoff carries, if any.

    Args:
        source: Pending offer or accepted handoff.

    Returns:
        One indented line per recorded field, and an empty string when the
        handoff carries none, so a record without structure reads exactly as
        it did before handoffs carried any.
    """
    fields = handoff_fields(source)
    lines = []
    if fields["commit"]:
        lines.append(f"  Commit: {fields['commit']}")
    if fields["reservations"]:
        lines.append("  Reservations: " + ", ".join(fields["reservations"]))
    for item in fields["remaining"]:
        lines.append(f"  Remaining: {item}")
    if fields["diff"]:
        lines.append(f"  Diff: {fields['diff']} ({fields['diff_bytes']} bytes)")
    return "".join(f"\n{line}" for line in lines)


def orphan_age(orphan: dict) -> int:
    """Reports how long ago an orphan marker recorded its observation.

    A marker states what the supervisor saw when it was written, which is not
    a reading of the owner at the moment it is displayed. Readers carry the
    age so an operator sees the observation together with its instant instead
    of a claim about the present.

    Args:
        orphan: Orphan marker a ledger record carries.

    Returns:
        Whole seconds since the marker was written, and zero for a marker
        that recorded no instant.
    """
    created = float(orphan.get("created") or 0.0)
    if not created:
        return 0
    return int(max(0.0, time.time() - created))


def describe(state: dict, liveness: dict[str, str] | None = None) -> str:
    """Formats active ownership and pending offers without changing state.

    Args:
        state: Published issue ledger.
        liveness: Optional session state per participant, reported beside the
            owner. Liveness is information for an operator; silence, idleness,
            and a stopped session never transfer ownership.

    Returns:
        One line for each owned issue, carrying any recorded forge title as
        display context, naming any issue it waits on and who holds that
        issue, or a notice that none are claimed. An unclaimed issue appears
        only while an offer waits on it, reported as unclaimed and naming the
        operator as the source when the command line recorded that offer. A
        claim whose owner the supervisor marked orphaned states that marker
        and how long ago it was recorded rather than a reading of that owner
        now, the reservations that owner still holds and the command a peer
        takes it with; the issue stays owned until that take is recorded.
    """
    lines = []
    for number, record in sorted(
        state["issues"].items(), key=lambda item: int(item[0])
    ):
        if prompt := record.get("handoff_prompt"):
            if not prompt.get("responded_at"):
                age = max(0, int(time.time() - prompt["created"]))
                lines.append(
                    f"Handoff reminder unanswered {age}s: {prompt['text']}"
                )
        if not record["owner"] and not record["offer"]:
            continue
        line = f"#{number}: {record['owner'] or 'unclaimed'}"
        if liveness and record["owner"] in liveness:
            line += f" ({liveness[record['owner']]})"
        if title := record.get("title"):
            line += f" — {title}"
        if orphan := record.get("orphan"):
            line += (
                f"; marked orphaned {orphan_age(orphan)}s ago, "
                f"{orphan['reason']}; still owned until a peer runs issue "
                f"claim {number} --take-orphaned"
            )
            if keys := orphan.get("reservations"):
                line += "\n  Held reservations: " + ", ".join(keys)
        if taken := record.get("taken"):
            line += f"\n  Taken from {taken['from']}: {taken['reason']}"
        timing = deadline_state(record)
        if timing["overdue"]:
            line += f"; overdue {timing['overdue_seconds']}s, still owned"
        if timing["budget"]:
            line += f"; attempts {timing['attempts']}/{timing['budget']}"
            if timing["budget_exceeded"]:
                line += " (budget exceeded, still owned)"
        elif timing["attempts"]:
            line += f"; attempts {timing['attempts']}"
        if waiting := record.get("blocked_by"):
            line += "; waits on " + ", ".join(
                f"#{blocker} ({holder['owner']})"
                if (holder := state["issues"].get(blocker, {})).get("owner")
                else f"#{blocker} (unclaimed)"
                for blocker in waiting
            )
        if offer := record["offer"]:
            age = max(0, int(time.time() - offer["created"]))
            waiting = offer_state(offer)
            operator = offer_source(offer) == OPERATOR
            label = "operator offer" if operator else "handoff"
            line += (
                f"; {label} to {offer['to']} pending {age}s; "
                f"offer {offer['id']}"
            )
            if waiting["overdue"]:
                line += f"; overdue {waiting['overdue_seconds']}s"
            note = (
                "Operator-stated reason"
                if operator
                else "Peer-provided summary"
            )
            line += f"\n  {note}: " + json.dumps(offer["summary"])
            line += _carried(offer)
        if inherited := record.get("handoff"):
            line += f"\n  Accepted from {inherited.get('from') or 'a peer'}"
            line += _carried(inherited)
        if request := record.get("request"):
            age = max(0, int(time.time() - request["created"]))
            line += (
                f"; operator asked {record['owner']} to hand it to "
                f"{request['to']} {age}s ago; offer {request['id']}"
            )
            if request["reason"]:
                line += "\n  Operator-stated reason: " + json.dumps(
                    request["reason"]
                )
        lines.append(line)
    return "\n".join(lines) or "No issues claimed."
