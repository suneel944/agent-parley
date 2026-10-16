"""Follow-up work a lane suggests and only the operator turns into an issue.

A lane often notices an adjacent problem it was not asked to fix. Mentioning
it in a report or in mail buries it, and filing it itself would let a lane
widen its own scope. The ``suggest_followup`` tool records the suggestion
here instead and returns at once, so the lane keeps the work it holds. The
record files nothing, offers nothing and starts no lane.

The operator reads pending suggestions through `pending` and accepts one with
``agent-parley followup accept ID`` or the Telegram decision button. Only
acceptance files the forge issue, in the active milestone, and offers it to
an idle lane through the same operator offer ``issue assign`` records.
A suggestion nobody accepts expires after `TTL` seconds and is dropped.

Suggestions live in `RECORD_NAME` beside the issue ledger, under
`LOCK_NAME`, and hold no credential. Their text is peer content: it is shown
to the operator and filed only on the operator's word.
"""

import json
import time
from pathlib import Path

from agent_parley.state import BridgeError, lock, write_json

TOOL = "suggest_followup"
KIND = "followup"
ACCEPT = "accept"
RECORD_NAME = "followups.json"
LOCK_NAME = "followups.lock"
PENDING = "pending"
ACCEPTED = "accepted"
TTL = 7 * 86400.0
MAX_PENDING = 50
KEPT_ACCEPTED = 100
MAX_TITLE = 200
MAX_WHY_BYTES = 2000
MAX_PATHS = 16
MAX_PATH = 300
LOCK_SECONDS = 5.0
NOTE = (
    "Recorded for the operator. Nothing was filed and no lane was started; "
    "keep working on what you hold."
)


def _read(directory: Path) -> dict:
    """Reads the suggestion record, treating a missing or damaged file as empty.

    Args:
        directory: Private project state directory.

    Returns:
        The next identifier counter and the suggestions keyed by identifier.
    """
    try:
        value = json.loads((directory / RECORD_NAME).read_text())
    except (OSError, ValueError):
        value = {}
    if not isinstance(value, dict) or not isinstance(
        value.get("suggestions"), dict
    ):
        value = {"next": 1, "suggestions": {}}
    if not isinstance(value.get("next"), int):
        value["next"] = len(value["suggestions"]) + 1
    return value


def _prune(record: dict, now: float) -> None:
    """Drops expired suggestions and the oldest accepted ones past the bound.

    Args:
        record: Suggestion record, changed in place.
        now: Unix time the expiry is judged at.
    """
    kept = record["suggestions"]
    for ident in [
        key
        for key, entry in kept.items()
        if entry.get("state") == PENDING
        and float(entry.get("expires") or 0) <= now
    ]:
        kept.pop(ident)
    accepted = sorted(
        (key for key, entry in kept.items() if entry.get("state") == ACCEPTED),
        key=lambda key: float(kept[key].get("accepted") or 0),
    )
    for ident in accepted[: max(len(accepted) - KEPT_ACCEPTED, 0)]:
        kept.pop(ident)


def _text(value: object, label: str, limit: int) -> str:
    """Validates one required text argument and returns it stripped.

    Raises:
        BridgeError: If the value is not text, is blank or is too long.
    """
    if not isinstance(value, str) or not value.strip():
        raise BridgeError(f"{label} must be non-empty text.")
    stripped = value.strip()
    if len(stripped.encode()) > limit:
        raise BridgeError(f"{label} must be at most {limit} bytes.")
    return stripped


def _paths(value: object) -> list[str]:
    """Validates the repository paths a suggestion names.

    Raises:
        BridgeError: If the value is not a bounded list of bounded text.
    """
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_PATHS:
        raise BridgeError(f"paths must be a list of at most {MAX_PATHS}.")
    return [_text(path, "Each path", MAX_PATH) for path in value]


def _terms(text: str) -> set[str]:
    """Reduces a title to the words a duplicate is matched on."""
    from agent_parley import recommend

    return recommend.terms(text)


def _alike(wanted: set[str], title: str) -> bool:
    """Reports whether a title shares enough words to be the same work.

    Two shared words make a match, or every word when the suggestion has
    fewer than two, so a single common subject word is not a duplicate.
    """
    shared = wanted & _terms(title)
    return bool(shared) and len(shared) >= min(2, len(wanted))


def _open_titles(directory: Path) -> dict[str, str]:
    """Reads the open forge issues the supervisor's cached reading lists.

    Args:
        directory: Private project state directory holding the cache.

    Returns:
        Issue number to title; empty when the cache is missing or stale.
    """
    from agent_parley.status import FORGE_ISSUES, FORGE_STALE

    try:
        cached = json.loads((directory / FORGE_ISSUES).read_text())
        listed = cached["issues"]
        fresh = time.time() - float(cached.get("read_at") or 0) < FORGE_STALE
    except (OSError, ValueError, TypeError, KeyError):
        return {}
    if not fresh or not isinstance(listed, dict):
        return {}
    return {
        str(number): str(
            (item.get("title") if isinstance(item, dict) else item) or ""
        )
        for number, item in listed.items()
    }


def duplicates(directory: Path, title: str) -> list[dict]:
    """Lists the open issues and claims a suggested title already describes.

    The match is shallow on purpose: shared title words, as `recommend`
    reads them, against the claims the ledger records and the open issues
    the supervisor last read from the forge. It flags, it never refuses;
    the operator reads the issue and decides.

    Args:
        directory: Private project state directory.
        title: Suggested title.

    Returns:
        Each matching issue with its number, title and owner, lowest first.
    """
    from agent_parley import issues

    wanted = _terms(title)
    if not wanted:
        return []
    try:
        ledger = issues.snapshot(directory).get("issues", {})
    except (OSError, ValueError):
        ledger = {}
    owned = {
        number
        for numbers in issues.holders({"issues": ledger}).values()
        for number in numbers
    }
    titles = _open_titles(directory)
    found = []
    for number in sorted(owned | set(titles), key=int):
        record = ledger.get(number) or {}
        named = titles.get(number) or str(record.get("title") or "")
        if _alike(wanted, named):
            found.append(
                {
                    "issue": number,
                    "title": named,
                    "owner": str(record.get("owner") or ""),
                }
            )
    return found


def suggest(
    directory: Path,
    lane: str,
    title: object,
    why: object,
    paths: object = None,
    now: float = 0.0,
) -> dict:
    """Records one lane's suggestion for the operator, filing nothing.

    The same lane suggesting the same title again while it is pending gets
    the recorded suggestion back, so a retried call does not queue twice.

    Args:
        directory: Private project state directory.
        lane: Participant the suggestion comes from.
        title: Proposed issue title.
        why: What the lane noticed and why it matters.
        paths: Repository paths the work would touch, if known.
        now: Unix time; the clock when zero.

    Returns:
        The suggestion, ``repeated`` when it was already recorded, the
        duplicates it matches and a note that nothing was filed.

    Raises:
        BridgeError: If an argument is invalid or the project already holds
            `MAX_PENDING` pending suggestions.
    """
    named = _text(title, "title", MAX_TITLE)
    reason = _text(why, "why", MAX_WHY_BYTES)
    listed = _paths(paths)
    stamp = now or time.time()
    with lock(directory / LOCK_NAME, timeout=LOCK_SECONDS):
        record = _read(directory)
        _prune(record, stamp)
        waiting = [
            entry
            for entry in record["suggestions"].values()
            if entry.get("state") == PENDING
        ]
        same = next(
            (
                entry
                for entry in waiting
                if entry.get("lane") == lane
                and entry.get("title", "").casefold() == named.casefold()
            ),
            None,
        )
        if same is None and len(waiting) >= MAX_PENDING:
            raise BridgeError(
                f"The operator already has {MAX_PENDING} suggestions to "
                "review; mention this one in your report instead."
            )
        if same is None:
            ident = f"f{record['next']}"
            record["next"] += 1
            same = {
                "id": ident,
                "lane": lane,
                "title": named,
                "why": reason,
                "paths": listed,
                "created": stamp,
                "expires": stamp + TTL,
                "state": PENDING,
                "issue": "",
                "offered_to": "",
                "decision": "",
            }
            record["suggestions"][ident] = same
            repeated = False
        else:
            repeated = True
        write_json(directory / RECORD_NAME, record)
    return {
        **same,
        "repeated": repeated,
        "duplicates": duplicates(directory, named),
        "note": NOTE,
    }


def link_decision(directory: Path, ident: str, decision: str) -> None:
    """Records which operator decision carries a suggestion's accept button.

    Args:
        directory: Private project state directory.
        ident: Suggestion identifier.
        decision: Decision identifier `decisions.open_or_refresh` returned.
    """
    with lock(directory / LOCK_NAME, timeout=LOCK_SECONDS):
        record = _read(directory)
        entry = record["suggestions"].get(ident)
        if entry is None:
            return
        entry["decision"] = decision
        write_json(directory / RECORD_NAME, record)


def pending(directory: Path, now: float = 0.0) -> list[dict]:
    """Lists the suggestions still waiting for the operator, oldest first.

    This is the reading `status` and the web view render. It writes
    nothing, calls no forge and reads only the private state directory.

    Args:
        directory: Private project state directory.
        now: Unix time the expiry is judged at; the clock when zero.

    Returns:
        Each unexpired pending suggestion with the open issues and claims
        it duplicates under ``duplicates``.
    """
    stamp = now or time.time()
    record = _read(directory)
    waiting = sorted(
        (
            entry
            for entry in record["suggestions"].values()
            if entry.get("state") == PENDING
            and float(entry.get("expires") or 0) > stamp
        ),
        key=lambda entry: (float(entry.get("created") or 0), entry["id"]),
    )
    return [
        {**entry, "duplicates": duplicates(directory, entry["title"])}
        for entry in waiting
    ]


def by_decision(directory: Path, decision: str) -> str:
    """Names the suggestion an operator decision was opened for.

    Args:
        directory: Private project state directory.
        decision: Decision identifier.

    Returns:
        The suggestion identifier, or an empty string when none matches.
    """
    return next(
        (
            ident
            for ident, entry in _read(directory)["suggestions"].items()
            if decision and entry.get("decision") == decision
        ),
        "",
    )


def body(entry: dict) -> str:
    """Words the forge issue an accepted suggestion files.

    The body carries what the lane wrote and nothing about which lane or
    provider wrote it, as `forge.report_comment` does.

    Args:
        entry: Accepted suggestion.

    Returns:
        Markdown for the issue body.
    """
    lines = ["## Why", "", entry["why"]]
    if entry.get("paths"):
        lines += ["", "## Paths", ""]
        lines += [f"- `{path}`" for path in entry["paths"]]
    lines += [
        "",
        "Suggested during coordinated work and filed when the operator "
        "accepted it.",
    ]
    return "\n".join(lines)


def idle_lane(directory: Path, manifest: dict) -> str:
    """Picks a running lane that holds no claim and has no offer waiting.

    Args:
        directory: Private project state directory.
        manifest: Project manifest naming the participants.

    Returns:
        The first such participant by name, or an empty string.
    """
    from agent_parley import checkpoints, issues, supervision

    ledger = issues.snapshot(directory)
    busy = set(issues.holders(ledger))
    busy |= {
        str((record.get("offer") or {}).get("to") or "")
        for record in ledger.get("issues", {}).values()
    }
    for name in sorted(manifest.get("participants", {})):
        if name in busy:
            continue
        derived = supervision.lane_state(checkpoints.activity(directory, name))
        if derived.get("process_alive"):
            return name
    return ""


def accept(
    directory: Path,
    manifest: dict,
    repo: Path,
    ident: str,
    *,
    milestone: str = "",
    to: str = "",
) -> dict:
    """Files one suggestion as a forge issue and offers it to an idle lane.

    The suggestion lock is held across the filing, so two acceptances of
    one suggestion cannot both file it, and an accepted suggestion answers
    a repeated acceptance with the issue it already filed. The offer is the
    operator offer ``issue assign`` records, answered by the lane like any
    other. With no lane named and none idle, the issue is left unclaimed
    for the next lane that pulls work.

    Args:
        directory: Private project state directory.
        manifest: Project manifest naming the participants and deadlines.
        repo: Checkout that selects the forge project.
        ident: Suggestion identifier.
        milestone: Milestone title to file under; the active one when empty.
        to: Participant to offer the issue to; an idle one when empty.

    Returns:
        The accepted suggestion with ``issue``, ``milestone``,
        ``offered_to`` and the offer identifier when one was recorded.

    Raises:
        BridgeError: If the suggestion is unknown or expired, the named lane
            is not a participant, or the forge refused the filing.
    """
    from agent_parley import forge, issues, roster

    if to and to not in manifest.get("participants", {}):
        raise BridgeError(
            f"{to} is not a participant; run agent-parley participant list."
        )
    with lock(directory / LOCK_NAME, timeout=LOCK_SECONDS):
        record = _read(directory)
        _prune(record, time.time())
        entry = record["suggestions"].get(ident)
        if entry is None:
            write_json(directory / RECORD_NAME, record)
            raise BridgeError(
                f"No pending suggestion {ident}; it was never recorded or "
                "expired. Run agent-parley followup list."
            )
        if entry.get("state") == ACCEPTED:
            return {**entry, "repeated": True, "offer_id": ""}
        chosen = milestone or forge.active_milestone(repo) or ""
        number = forge.file_issue(repo, entry["title"], body(entry), chosen)
        if number is None:
            failure = forge.last_failure() or {}
            raise BridgeError(
                f"The forge did not file {ident}: "
                f"{failure.get('detail') or 'no reachable GitHub project'}. "
                "The suggestion stays pending; fix the forge and retry."
            )
        entry.update(
            state=ACCEPTED, issue=number, milestone=chosen, accepted=time.time()
        )
        write_json(directory / RECORD_NAME, record)
    recipient = to or idle_lane(directory, manifest)
    offer_id = ""
    if recipient:
        changed = issues.change(
            directory,
            roster.OPERATOR,
            "assign",
            number,
            participants=set(manifest["participants"]),
            to=recipient,
            summary=f"Accepted follow-up {ident}: {entry['title']}"[:200],
            defaults=manifest.get("deadlines"),
        )
        offer_id = str((changed.get("offer") or {}).get("id") or "")
        with lock(directory / LOCK_NAME, timeout=LOCK_SECONDS):
            record = _read(directory)
            if ident in record["suggestions"]:
                record["suggestions"][ident]["offered_to"] = recipient
                write_json(directory / RECORD_NAME, record)
        entry["offered_to"] = recipient
    return {**entry, "repeated": False, "offer_id": offer_id}


def accept_decision(directory: Path, decision: dict) -> dict:
    """Accepts the suggestion an answered operator decision was opened for.

    This is the Telegram accept button's path into `accept`: the decision
    names the project checkout and, through `link_decision`, the suggestion.

    Args:
        directory: Private project state directory holding the decision.
        decision: The decision record being answered.

    Returns:
        What `accept` returned.

    Raises:
        BridgeError: If no suggestion carries this decision, or `accept`
            refused it.
    """
    from agent_parley import roster

    ident = by_decision(directory, str(decision.get("id") or ""))
    if not ident:
        raise BridgeError(
            "This suggestion is no longer pending; run agent-parley "
            "followup list."
        )
    return accept(
        directory,
        roster.read(directory),
        Path(str(decision.get("project") or "")),
        ident,
    )


def accepted_text(accepted: dict) -> str:
    """Formats the outcome of ``followup accept``.

    Args:
        accepted: What `accept` returned.

    Returns:
        Which issue was filed, under which milestone, and who it was
        offered to.
    """
    filed = "already filed" if accepted.get("repeated") else "filed"
    where = (
        f" in milestone {accepted['milestone']}"
        if accepted.get("milestone")
        else " with no milestone"
    )
    offered = (
        f"; offered to {accepted['offered_to']}"
        if accepted.get("offered_to")
        else "; no idle lane, left unclaimed for the next lane that pulls"
    )
    return f"{accepted['id']} {filed} as #{accepted['issue']}{where}{offered}."


def render(rows: list[dict]) -> str:
    """Formats pending suggestions for ``followup list``.

    Args:
        rows: Suggestions `pending` returned.

    Returns:
        One block per suggestion, or one line when none is pending.
    """
    if not rows:
        return "No pending follow-up suggestions."
    lines = []
    for entry in rows:
        lines.append(f"{entry['id']}  {entry['lane']}: {entry['title']}")
        lines.append(f"    why: {entry['why']}")
        if entry.get("paths"):
            lines.append(f"    paths: {', '.join(entry['paths'])}")
        for match in entry.get("duplicates") or []:
            held = f", held by {match['owner']}" if match["owner"] else ""
            lines.append(
                f"    duplicate of #{match['issue']} {match['title']}{held}"
            )
        expires = time.strftime(
            "%Y-%m-%d %H:%M UTC", time.gmtime(float(entry["expires"]))
        )
        lines.append(f"    expires {expires}")
    lines.append("Accept one with: agent-parley followup accept ID")
    return "\n".join(lines)
