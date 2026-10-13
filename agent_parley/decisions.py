"""Durable records of the situations that wait on an operator's answer.

A decision is one question an absent operator must answer before a lane can
move on: a handoff to accept, a permission prompt, a dirty worktree, an
integration to verify. Each one is recorded under the project's private state
directory with a stable identifier, the options that answer it with one marked
recommended, and whether acting on it can be undone. Observing the same
situation again refreshes its record rather than opening another, so every
situation holds exactly one open decision however many processes see it.

Delivery is durable. A decision is queued when it opens and is marked sent
only after every transport accepted it; a refused or abandoned send is retried
with a growing backoff on the next supervision poll, so a short-lived hook
that records a decision and exits at once still reaches the operator. A
sender leases the decisions it is about to send, so two processes flushing at
once do not both send them. The decisions one flush finds due go out
together, as one digest message carrying one inline keyboard row per
decision on Telegram and the text reply each takes on every other transport.

Decisions are recorded only while notifications are configured, so a hook in
an unconfigured estate pays nothing for them. The record holds no credential:
its text is composed from coordination fields, and the transport settings
stay in `notify`.
"""

import hashlib
import json
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

from agent_parley.state import BridgeError, lock, write_json

RECORD_NAME = "decisions.json"
LOCK_NAME = "decisions.lock"
REVERSIBLE = "reversible"
IRREVERSIBLE = "irreversible"
REVERSIBILITY = (REVERSIBLE, IRREVERSIBLE)
OPEN = "open"
ANSWERED = "answered"
CLOSED = "closed"
EXPIRED = "expired"
STALE = "stale"
DEFAULT_TTL = 86400.0
BACKOFF_FIRST = 30.0
BACKOFF_CEILING = 3600.0
LEASE_SECONDS = 60.0
KEPT_SETTLED = 200
CALLBACK_PREFIX = "d"
CONFIRMED = "y"
MAX_TEXT_BYTES = 1024


def identifier(project: str, lane: str, kind: str, key: str) -> str:
    """Derives the stable identifier one situation keeps across observations.

    Args:
        project: Canonical project root.
        lane: Lane the situation belongs to, empty for estate situations.
        kind: Situation kind, such as a notification or problem name.
        key: Fields that tell two situations of one kind apart.

    Returns:
        Twelve hexadecimal characters, short enough to type as a reply and
        to fit a Telegram callback payload beside an option index.
    """
    text = "\x00".join((project, lane, kind, key))
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def _read(directory: Path) -> dict[str, dict]:
    """Reads every decision record of one project.

    Args:
        directory: Private project state directory.

    Returns:
        Records keyed by identifier, or an empty mapping when none is stored
        or the file cannot be decoded.
    """
    try:
        value = json.loads((directory / RECORD_NAME).read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write(directory: Path, records: dict[str, dict]) -> None:
    """Writes the records back, keeping only the newest settled ones.

    Args:
        directory: Private project state directory.
        records: Every record keyed by identifier.
    """
    settled = sorted(
        (record for record in records.values() if record.get("state") != OPEN),
        key=lambda record: record.get("settled", 0.0),
    )
    for record in settled[: max(len(settled) - KEPT_SETTLED, 0)]:
        records.pop(record["id"], None)
    write_json(directory / RECORD_NAME, records)


def _expire(records: dict[str, dict], now: float) -> None:
    """Moves every open record past its expiry to the expired state.

    Args:
        records: Every record keyed by identifier, changed in place.
        now: Unix time to compare against.
    """
    for record in records.values():
        if record.get("state") == OPEN and record.get("expires", now) < now:
            record["state"] = EXPIRED
            record["settled"] = now


def _clipped(text: str) -> str:
    """Cuts text to the record's byte cap without splitting a character.

    Args:
        text: Text to store.

    Returns:
        The text, at most `MAX_TEXT_BYTES` bytes long.
    """
    return text.encode()[:MAX_TEXT_BYTES].decode(errors="ignore")


def open_or_refresh(
    directory: Path,
    *,
    project: str,
    lane: str,
    kind: str,
    key: str,
    question: str,
    options: Sequence[str],
    recommended: str = "",
    issue: str = "",
    reversibility: str = REVERSIBLE,
    detail: str = "",
    confirm: Sequence[str] = (),
    ttl: float = DEFAULT_TTL,
    now: float = 0.0,
) -> dict:
    """Opens the decision one situation asks for, or refreshes the open one.

    A repeat observation of an open situation extends its expiry and keeps
    its identifier, its delivery state and its text, so the operator is not
    asked twice. A situation whose previous decision was answered, closed or
    expired opens a fresh one.

    Args:
        directory: Private project state directory.
        project: Canonical project root.
        lane: Lane the situation belongs to, empty for estate situations.
        kind: Situation kind.
        key: Fields that tell two situations of one kind apart.
        question: One line saying what the operator is asked.
        options: The answers the operator can give, at least one.
        recommended: The option to mark recommended; the first when empty.
        issue: Issue number the situation concerns, if any.
        reversibility: `REVERSIBLE` or `IRREVERSIBLE`.
        detail: Further lines describing the situation, without any secret.
        confirm: Options that take a confirming answer even when the
            decision is reversible, such as one that widens a permission.
        ttl: Seconds the decision stays open unless observed again.
        now: Unix time; the clock when zero.

    Returns:
        The open decision record.

    Raises:
        BridgeError: If no option is given, the recommendation is not one of
            the options, or the reversibility class is unknown.
    """
    choices = [str(option) for option in options if str(option)]
    if not choices:
        raise BridgeError("A decision needs at least one option.")
    chosen = recommended or choices[0]
    if chosen not in choices:
        raise BridgeError(f"Recommended option {chosen!r} is not offered.")
    if reversibility not in REVERSIBILITY:
        raise BridgeError(f"Unknown reversibility {reversibility!r}.")
    stamp = now or time.time()
    name = identifier(project, lane, kind, key)
    with lock(directory / LOCK_NAME, timeout=5):
        records = _read(directory)
        _expire(records, stamp)
        record = records.get(name)
        if record and record.get("state") == OPEN:
            record["refreshed"] = stamp
            record["expires"] = stamp + ttl
        else:
            record = {
                "id": name,
                "project": project,
                "lane": lane,
                "issue": str(issue),
                "kind": kind,
                "question": _clipped(question),
                "detail": _clipped(detail),
                "options": choices,
                "recommended": chosen,
                "reversibility": reversibility,
                "confirm": [option for option in choices if option in confirm],
                "state": OPEN,
                "created": stamp,
                "refreshed": stamp,
                "expires": stamp + ttl,
                "answer": "",
                "answered_by": "",
                "settled": 0.0,
                "sent": 0.0,
                "attempts": 0,
                "retry_at": stamp,
                "error": "",
                "note": "",
            }
            records[name] = record
        _write(directory, records)
    return dict(record)


def get(directory: Path, name: str) -> dict:
    """Reads one decision record.

    Args:
        directory: Private project state directory.
        name: Decision identifier.

    Returns:
        The record, or an empty mapping when no decision has the identifier.
    """
    return dict(_read(directory).get(name) or {})


def find(home: Path, name: str) -> tuple[Path, dict]:
    """Locates one decision among every project of the state root.

    A button tap or a chat reply carries only the decision identifier, so
    the project it belongs to is read from whichever project directory
    holds it.

    Args:
        home: Private bridge state root.
        name: Decision identifier.

    Returns:
        The project state directory and the record.

    Raises:
        BridgeError: If no project holds a decision with the identifier.
    """
    for path in sorted((home / "projects").glob(f"*/{RECORD_NAME}")):
        record = get(path.parent, name)
        if record:
            return path.parent, record
    raise BridgeError(f"No decision {name}.")


def list_open(directory: Path, now: float = 0.0) -> list[dict]:
    """Lists the decisions still waiting on an answer, oldest first.

    Args:
        directory: Private project state directory.
        now: Unix time to judge expiry by; the clock when zero.

    Returns:
        Every open record whose expiry has not passed.
    """
    stamp = now or time.time()
    return sorted(
        (
            dict(record)
            for record in _read(directory).values()
            if record.get("state") == OPEN
            and record.get("expires", stamp) >= stamp
        ),
        key=lambda record: record.get("created", 0.0),
    )


def refusal(record: Mapping[str, object], option: str, now: float) -> str:
    """Says why one option cannot answer one decision.

    Args:
        record: Decision record.
        option: The chosen option.
        now: Unix time to judge expiry by.

    Returns:
        The refusal, naming who answered first when the decision was already
        answered, or an empty string when the option can answer it.
    """
    name = record.get("id")
    state = record.get("state")
    if state == ANSWERED:
        return (
            f"Decision {name} was already answered {record.get('answer')} "
            f"by {record.get('answered_by')}."
        )
    if state == OPEN and float(str(record.get("expires", now))) < now:
        state = EXPIRED
    if state != OPEN:
        return f"Decision {name} is already {state}."
    offered = options(record)
    if option not in offered:
        return f"Decision {name} offers {', '.join(offered)}; not {option!r}."
    return ""


def answer(
    directory: Path,
    name: str,
    option: str,
    answered_by: str,
    now: float = 0.0,
    note: str = "",
) -> dict:
    """Records the operator's answer to one open decision.

    The check and the write run under the project's decision lock, so of two
    answers racing on one decision the first is recorded and the second is
    refused with who answered first.

    Args:
        directory: Private project state directory.
        name: Decision identifier.
        option: The chosen option, exactly as offered.
        answered_by: Who answered, such as ``telegram:42`` or ``cli``.
        now: Unix time; the clock when zero.
        note: Free text the operator attached, stored as a quotation only;
            when empty, a note `hold` kept is attached instead.

    Returns:
        The answered record.

    Raises:
        BridgeError: If no decision has the identifier, it is no longer open,
            or the option is not one it offered.
    """
    stamp = now or time.time()
    with lock(directory / LOCK_NAME, timeout=5):
        records = _read(directory)
        _expire(records, stamp)
        record = records.get(name)
        if not record:
            raise BridgeError(f"No decision {name}.")
        if refused := refusal(record, option, stamp):
            raise BridgeError(refused)
        record.update(
            state=ANSWERED,
            answer=option,
            answered_by=answered_by,
            note=_clipped(note) or str(record.get("note", "")),
            settled=stamp,
        )
        _write(directory, records)
    return dict(record)


def hold(directory: Path, name: str, note: str) -> None:
    """Keeps the note an unconfirmed irreversible answer arrived with.

    The confirming tap carries only the option, so the note typed with the
    first answer is kept on the open record and attached when the answer is
    confirmed.

    Args:
        directory: Private project state directory.
        name: Decision identifier.
        note: Free text the operator attached.
    """
    with lock(directory / LOCK_NAME, timeout=5):
        records = _read(directory)
        record = records.get(name)
        if not record or record.get("state") != OPEN:
            return
        record["note"] = _clipped(note)
        _write(directory, records)


def close(directory: Path, name: str, now: float = 0.0) -> bool:
    """Closes an open decision whose situation cleared without an answer.

    Args:
        directory: Private project state directory.
        name: Decision identifier.
        now: Unix time; the clock when zero.

    Returns:
        True when an open decision was closed, False otherwise.
    """
    stamp = now or time.time()
    with lock(directory / LOCK_NAME, timeout=5):
        records = _read(directory)
        record = records.get(name)
        if not record or record.get("state") != OPEN:
            return False
        record.update(state=CLOSED, settled=stamp)
        _write(directory, records)
    return True


def confirming(record: Mapping[str, object], option: str) -> bool:
    """Reports whether an answer takes a second, confirming answer.

    Args:
        record: The open decision.
        option: The option chosen.

    Returns:
        True when the decision is irreversible or the option is one the
        decision names as needing confirmation.
    """
    named = record.get("confirm")
    return record.get("reversibility") == IRREVERSIBLE or (
        isinstance(named, list) and option in named
    )


def applied(directory: Path, name: str, now: float = 0.0) -> bool:
    """Claims an answered decision for the one step that carries it out.

    The claim is taken under the decision lock, so of two passes that read
    the same answer only one acts on it.

    Args:
        directory: Private project state directory.
        name: Decision identifier.
        now: Unix time; the clock when zero.

    Returns:
        True when this call claimed the answer, False when the decision is
        unknown, not answered, or already carried out.
    """
    with lock(directory / LOCK_NAME, timeout=5):
        records = _read(directory)
        record = records.get(name)
        if (
            not record
            or record.get("state") != ANSWERED
            or record.get("applied")
        ):
            return False
        record["applied"] = now or time.time()
        _write(directory, records)
    return True


def stale(directory: Path, name: str, now: float = 0.0) -> bool:
    """Retires a decision whose situation changed before it was carried out.

    An open decision, or an answered one not yet carried out, becomes
    `STALE`: a later answer is refused, and an answer already given is
    never acted on. The situation now showing is asked about afresh.

    Args:
        directory: Private project state directory.
        name: Decision identifier.
        now: Unix time; the clock when zero.

    Returns:
        True when the decision was retired, False otherwise.
    """
    with lock(directory / LOCK_NAME, timeout=5):
        records = _read(directory)
        record = records.get(name)
        if not record or record.get("applied"):
            return False
        if record.get("state") not in (OPEN, ANSWERED):
            return False
        record.update(state=STALE, settled=now or time.time())
        _write(directory, records)
    return True


def options(record: Mapping[str, object]) -> list[str]:
    """Reads the options one decision offers.

    Args:
        record: Decision record.

    Returns:
        The offered options in order, empty when the record holds none.
    """
    offered = record.get("options")
    if not isinstance(offered, list):
        return []
    return [str(option) for option in offered]


def callback(name: str, index: int, confirmed: bool = False) -> str:
    """Encodes the Telegram callback payload for one option.

    Args:
        name: Decision identifier.
        index: Position of the option in the record's options.
        confirmed: Whether this is the second tap an irreversible option
            asks for.

    Returns:
        A payload well inside Telegram's 64-byte limit.
    """
    suffix = f":{CONFIRMED}" if confirmed else ""
    return f"{CALLBACK_PREFIX}:{name}:{index}{suffix}"


def tapped(data: str) -> tuple[str, int, bool]:
    """Decodes a callback payload `callback` encoded.

    Args:
        data: The ``callback_data`` a button tap carried.

    Returns:
        The decision identifier, the option index and whether the tap
        confirms an irreversible option.

    Raises:
        BridgeError: If the payload is not one this module encodes.
    """
    parts = data.split(":")
    if (
        len(parts) not in (3, 4)
        or parts[0] != CALLBACK_PREFIX
        or not parts[2].isdigit()
        or (len(parts) == 4 and parts[3] != CONFIRMED)
    ):
        raise BridgeError("Not a decision button.")
    return parts[1], int(parts[2]), len(parts) == 4


def keyboard(records: Sequence[Mapping[str, object]]) -> dict:
    """Builds one inline keyboard row per decision.

    Args:
        records: Decisions to offer, in the order the message lists them.

    Returns:
        A Telegram ``InlineKeyboardMarkup`` document; the recommended option
        is marked with a trailing star.
    """
    rows = []
    for record in records:
        rows.append(
            [
                {
                    "text": (
                        f"{option} *"
                        if option == record.get("recommended")
                        else option
                    ),
                    "callback_data": callback(str(record["id"]), index),
                }
                for index, option in enumerate(options(record))
            ]
        )
    return {"inline_keyboard": rows}


def _block(record: Mapping[str, object]) -> str:
    """Renders one decision as the lines a digest carries for it.

    Args:
        record: Decision to render.

    Returns:
        The question, the situation detail, its reversibility, the options
        with the recommended one marked, and the text reply that answers it.
    """
    lines = [str(record.get("question", ""))]
    if record.get("detail"):
        lines.append(str(record["detail"]))
    lines.append(f"decision: {record['id']} ({record.get('reversibility')})")
    lines.append(
        "options: "
        + ", ".join(
            f"{option} (recommended)"
            if option == record.get("recommended")
            else option
            for option in options(record)
        )
    )
    lines.append(f"reply: decide {record['id']} <option> [note]")
    return "\n".join(lines)


def compose(records: Sequence[Mapping[str, object]]) -> tuple[str, str]:
    """Renders one or several decisions as a subject and a bounded body.

    Args:
        records: Decisions to send, oldest first.

    Returns:
        The subject and the body, each clipped to the notification caps.
    """
    from agent_parley import checkpoints, notify

    if len(records) == 1:
        record = records[0]
        lane = str(record.get("lane", ""))
        subject = f"Agent Parley: {record.get('question', '')}" + (
            f" ({lane})" if lane else ""
        )
    else:
        subject = f"Agent Parley: {len(records)} decisions are waiting"
    return (
        checkpoints.clip(subject, notify.MAX_SUBJECT_BYTES),
        checkpoints.clip(
            "\n\n".join(_block(record) for record in records),
            notify.MAX_MESSAGE_BYTES,
        ),
    )


def _lease(directory: Path, now: float) -> list[dict]:
    """Takes the open, unsent decisions whose delivery is due.

    Each one taken has its retry time pushed past the lease, so a second
    process flushing before this one finishes finds nothing due.

    Args:
        directory: Private project state directory.
        now: Unix time.

    Returns:
        The records taken, oldest first.
    """
    with lock(directory / LOCK_NAME, timeout=5):
        records = _read(directory)
        _expire(records, now)
        taken = sorted(
            (
                record
                for record in records.values()
                if record.get("state") == OPEN
                and not record.get("sent")
                and record.get("retry_at", 0.0) <= now
            ),
            key=lambda record: record.get("created", 0.0),
        )
        for record in taken:
            record["retry_at"] = now + LEASE_SECONDS
        if taken:
            _write(directory, records)
    return [dict(record) for record in taken]


def flush(directory: Path, config: dict, now: float = 0.0) -> dict:
    """Delivers every due decision as one digest and records the outcome.

    Decisions are marked sent only when every transport accepted the digest.
    A refused digest leaves them queued with a retry time that doubles per
    attempt up to an hour, and records the failure text on each.

    Args:
        directory: Private project state directory.
        config: Resolved notification configuration.
        now: Unix time; the clock when zero.

    Returns:
        The identifiers sent and one result per transport, both empty when
        nothing was due or no transport is configured.
    """
    from agent_parley import notify

    stamp = now or time.time()
    if not config.get("transports"):
        return {"sent": [], "results": []}
    pending = _lease(directory, stamp)
    if not pending:
        return {"sent": [], "results": []}
    subject, body = compose(pending)
    markup = {**config, "markup": keyboard(pending)}
    results = notify.send(markup, subject, body)
    failures = [
        f"{result['transport']}: {result['error']}"
        for result in results
        if not result["ok"]
    ]
    names = [record["id"] for record in pending]
    with lock(directory / LOCK_NAME, timeout=5):
        records = _read(directory)
        for name in names:
            record = records.get(name)
            if not record:
                continue
            if failures:
                attempts = int(record.get("attempts", 0)) + 1
                delay = BACKOFF_FIRST * 2 ** (attempts - 1)
                record.update(
                    attempts=attempts,
                    retry_at=stamp + min(delay, BACKOFF_CEILING),
                    error="; ".join(failures),
                )
            else:
                record.update(sent=stamp, error="")
        _write(directory, records)
    return {"sent": [] if failures else names, "results": results}
