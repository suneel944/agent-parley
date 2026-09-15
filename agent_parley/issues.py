"""Local, repository-scoped issue ownership and explicit handoffs."""

import contextlib
import json
import re
import sqlite3
import time
import uuid
from pathlib import Path

from agent_parley import attachments, retries
from agent_parley.state import BridgeError, lock, write_json

MAX_BLOCKERS = 10
SUPERVISION_ERROR = "supervision-error.json"
OPERATOR = "operator"
PEER = "peer"
MAX_REASON = 2000
MAX_SUMMARY_BYTES = 2048


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
    when its own history ends in a release, or when supervision recorded that
    the pull request of the current ownership generation ended. An issue that
    was released and claimed again reads as held, because its history no
    longer ends in a release, and an old pull request on a reused lane branch
    never answers for a later claim.

    Args:
        record: Published ledger record for one issue, or an empty mapping.

    Returns:
        Whether the issue is explicitly released or completed.
    """
    history = record.get("history") or []
    if history and history[-1].get("action") == "release":
        return True
    prompt = record.get("handoff_prompt") or {}
    return prompt.get("trigger") == "pull request ended"


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
        owner = state["issues"][number].get("owner")
        if owner:
            owned.setdefault(owner, []).append(number)
    return owned


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
    waiters: dict[str, int] = {}
    for record in issues.values():
        if not record.get("owner"):
            continue
        for blocker in record.get("blocked_by", []):
            waiters[blocker] = waiters.get(blocker, 0) + 1
    return sorted(
        (
            number
            for number, record in issues.items()
            if not record.get("owner")
            and not record.get("offer")
            and not record.get("blocked_by")
        ),
        key=lambda number: (-waiters.get(number, 0), int(number)),
    )


def _refuse(directory: Path, scope: str, fingerprint: str, detail: str) -> None:
    """Records a refused transition so a retry is refused identically.

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
            **transition,
        )
    except BridgeError as exc:
        _refuse(directory, scope, fingerprint, str(exc))
        raise


def _drop_offer(directory: Path, record: dict) -> None:
    """Removes the attachment of a pending offer that is leaving the record.

    Args:
        directory: Private state directory for the common repository.
        record: Ledger record whose pending offer is being cleared.
    """
    offer = record.get("offer") or {}
    if offer.get("attachment"):
        attachments.remove(directory, str(offer["attachment"]))


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

    Returns:
        The persisted issue record, including transition history.

    Raises:
        BridgeError: If validation, ownership, offer, or lock checks fail.
    """
    issue = parse_issue(issue)
    budgets = defaults or {}
    logged = action
    blocker = ""
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
            if record and record["owner"]:
                if record["owner"] == agent:
                    return record
                raise BridgeError(
                    f"Issue #{issue} is owned by {record['owner']}."
                )
            previous = record or {}
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
            }
            resolved = title if title else previous.get("title")
            if resolved:
                record["title"] = resolved
        elif action == "assign":
            record = _assign(
                record,
                issue,
                recipient=to,
                reason=summary.strip(),
                participants=participants,
                budgets=budgets,
            )
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
                    record.update(
                        owner=agent,
                        request=None,
                        deadline=(time.time() + expected if expected else None),
                        attempts=0,
                        budget=budgets.get("attempts") or None,
                        claim_id=uuid.uuid4().hex[:16],
                    )
                    if offer.get("attachment"):
                        record["attachment"] = offer["attachment"]
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
                    }
                    if attached:
                        record["offer"]["attachment"] = attached
                elif action == "cancel":
                    if not record["offer"]:
                        raise BridgeError("No handoff is pending.")
                    _drop_offer(directory, record)
                    record["offer"] = None
                elif action == "release":
                    attachments.remove(directory, record.get("attachment", ""))
                    record.pop("attachment", None)
                    record.update(
                        owner=None, offer=None, request=None, deadline=None
                    )
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
        record["history"].append(
            {
                "action": logged,
                "actor": agent,
                "at": time.time(),
                "owner": record["owner"],
                "offer": record["offer"],
                "request": record.get("request"),
                "offer_id": offer_id,
                "claim_id": record.get("claim_id"),
            }
        )
        state["issues"][issue] = record
        if scope:
            retries.remember(state, scope, fingerprint, retries.SERVED, record)
        state["revision"] += 1
        write_json(directory / "issues.json", state)
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
        operator as the source when the command line recorded that offer.
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
