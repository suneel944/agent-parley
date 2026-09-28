"""Derives lane consumption against advisory budgets and enforced run limits.

A budget is a line an operator draws, not a gate the runtime enforces. The
readings it is compared with already exist: the tokens a lane's own native
client recorded, the coordination calls the store served for it, and the
hours its session process has been alive. No vendor is asked and no price is
applied, so a token budget is a count and never spend. Crossing a limit marks
the lane, prints the figures and sends the lane one notice; nothing is
stopped, revoked or refused, and what to do about it stays the operator's
decision.

A run budget is the one opt-in exception. An operator who records one makes
the same three readings, summed over every lane of the project, a gate on
what the service starts: wakes, work dispatch, capacity retries and
launches. The sum lives in a durable ledger that keeps the highest reading
of each source, a transcript or a session, so a restart, a replaced lane or
a retried session never resets it. Every transcript a lane's client kept is
read from a durable byte offset, so records that ended between polls or
were written while the service was down still count, while history from
before the run budget was recorded does not. A resumed session whose new
transcript replays earlier messages is counted again. Counting is still an
observation, not a meter: tokens appended to a transcript deleted before
the next poll and Codex rollouts outside the most recent ones ``records``
considers are not seen, so the run can exhaust late by that much. Reaching a
limit records one exhaustion that only an operator's resume clears; running
sessions are asked to checkpoint and stop at their next hook event, and
nothing is killed, discarded or approved on a lane's behalf.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import sqlite3
import time
from pathlib import Path

from agent_parley import checkpoints, notify, process, records, roster, store
from agent_parley.state import BridgeError, lock, write_json

UNITS = {"tokens": "tokens", "calls": "calls", "hours": "h"}
LEDGER = "run-budget.json"
RUN = "run"
LOCK_SECONDS = 10.0
RESUME = "an operator resumes it with agent-parley budget resume"


def limits(home: Path, manifest: dict, name: str) -> dict:
    """Resolves the limits that apply to one lane.

    A limit recorded on the participant wins over one recorded on its
    provider, which wins over the project default, field by field, so a
    project can give every lane a ceiling and one lane can still be given
    its own.

    Args:
        home: Private bridge state root.
        manifest: Project manifest holding this participant.
        name: Participant that owns the lane.

    Returns:
        The effective limit per budget field, absent where none applies.
    """
    participant = manifest["participants"][name]
    try:
        inherited = roster.budget(
            dict(
                roster.provider(home, participant["provider"]).get("budget")
                or {}
            )
        )
    except BridgeError:
        inherited = {}
    layers = (
        manifest.get("budget") or {},
        inherited,
        participant.get("budget") or {},
    )
    result: dict = {}
    for layer in layers:
        result.update(layer)
    return result


def consumption(
    home: Path,
    directory: Path,
    manifest: dict,
    name: str,
    usage: dict,
    cache: dict,
    wanted: set[str],
) -> dict:
    """Reads what one lane has consumed, from records that already exist.

    Args:
        home: Private bridge state root.
        directory: Private state directory for the common repository.
        manifest: Project manifest holding this participant.
        name: Participant that owns the lane.
        usage: Served-call statistics per registered identity, as the store
            reports them.
        cache: Caller-owned session-record reading cache.
        wanted: Fields a limit applies to; the others are not read, so a
            lane without a token budget never has its transcript parsed.

    Returns:
        Tokens the lane's client recorded, or None when unreadable or not
        wanted; calls served for it; hours its recorded session process has
        been alive, zero when no live session is recorded.
    """
    participant = manifest["participants"][name]
    hours = 0.0
    if "hours" in wanted:
        state = checkpoints.activity(directory, name)
        started = state.get("session_started")
        if isinstance(started, (int, float)) and process.alive(
            state.get("session_pid"), state.get("session_ticks")
        ):
            hours = max(0.0, time.time() - float(started)) / 3600
    return {
        "tokens": (
            records.reported_tokens(home, participant, cache)
            if "tokens" in wanted
            else None
        ),
        "calls": int(usage.get(participant["display"], {}).get("calls", 0)),
        "hours": round(hours, 2),
    }


def report(
    home: Path,
    directory: Path,
    manifest: dict,
    name: str,
    usage: dict,
    cache: dict | None = None,
) -> dict:
    """Compares one lane's consumption with the limits that apply to it.

    Args:
        home: Private bridge state root.
        directory: Private state directory for the common repository.
        manifest: Project manifest holding this participant.
        name: Participant that owns the lane.
        usage: Served-call statistics per registered identity.
        cache: Session-record reading cache; a fresh one when None.

    Returns:
        The effective limits, the readings, the share consumed per limited
        field as a percentage, the fields whose limit is crossed, and
        whether any is. A lane with no limit is never over budget, and an
        unreadable token count never crosses a token limit.
    """
    applied = limits(home, manifest, name)
    used = consumption(
        home,
        directory,
        manifest,
        name,
        usage,
        {} if cache is None else cache,
        set(applied),
    )
    share = {}
    crossed = []
    for field in roster.BUDGET_FIELDS:
        limit = applied.get(field)
        reading = used[field]
        if limit is None or reading is None:
            continue
        share[field] = int(reading * 100 // limit)
        if reading > limit:
            crossed.append(field)
    return {
        "limits": applied,
        "used": used,
        "share": share,
        "crossed": crossed,
        "over": bool(crossed),
    }


def standing(home: Path, directory: Path, manifest: dict, name: str) -> dict:
    """Compares one lane with its budget, reading the store only when needed.

    Args:
        home: Private bridge state root.
        directory: Private state directory for the common repository.
        manifest: Project manifest holding this participant.
        name: Participant that owns the lane.

    Returns:
        The comparison ``report`` produces. Served-call statistics are read
        only when a call limit applies, and an unreadable store counts no
        calls rather than failing the caller. While the project's run budget
        is exhausted, ``run`` is added to the crossed fields and the
        exhaustion record is attached under ``run``.
    """
    usage: dict = {}
    if "calls" in limits(home, manifest, name):
        try:
            usage = store.usage(home, manifest["root"])
        except sqlite3.Error:
            usage = {}
    reading = report(home, directory, manifest, name, usage)
    if stopped := halted(directory, manifest):
        reading.update(
            crossed=[*reading["crossed"], RUN], over=True, run=stopped
        )
    return reading


def _figure(field: str, value: float | None) -> str:
    """Formats one reading or limit in the units of its field."""
    if value is None:
        return "?"
    text = f"{value:,}" if type(value) is int else f"{value:g}"
    return text + ("h" if field == "hours" else "")


def marker(reading: dict) -> str:
    """Describes a lane's standing against its budget in one line.

    Args:
        reading: Comparison produced by ``report``.

    Returns:
        An empty string when no limit applies; otherwise the share consumed
        per limited field, led by ``over budget`` when any limit is crossed.
    """
    if not reading["limits"]:
        return ""
    parts = []
    for field in roster.BUDGET_FIELDS:
        if field not in reading["limits"]:
            continue
        figures = (
            f"{_figure(field, reading['used'][field])} of "
            f"{_figure(field, reading['limits'][field])}"
        )
        share = reading["share"].get(field)
        parts.append(
            f"{field} {figures}"
            + (f" ({share}%)" if share is not None else "")
            + ("!" if field in reading["crossed"] else "")
        )
    lead = "over budget" if reading["over"] else "budget"
    return f"{lead}; " + ", ".join(parts)


def notice(reading: dict) -> str:
    """Words the one advisory notice a lane receives on crossing a limit.

    Args:
        reading: Comparison produced by ``report``.

    Returns:
        A bounded sentence naming each crossed limit and its reading, and
        stating that nothing is stopped. An exhausted run budget leads with
        the request to checkpoint and stop. Empty when no limit is crossed.
    """
    crossed = [
        field for field in reading["crossed"] if field in roster.BUDGET_FIELDS
    ]
    parts = []
    if RUN in reading["crossed"]:
        parts.append(
            "Run budget exhausted: no new wake, dispatch or retry will be "
            "sent. Finish the current step, commit or checkpoint your work in "
            "this worktree, report it, and stop; start nothing new. Nothing "
            f"was discarded; {RESUME}."
        )
    if crossed:
        figures = ", ".join(
            f"{field} {_figure(field, reading['used'][field])} of "
            f"{_figure(field, reading['limits'][field])}"
            for field in crossed
        )
        parts.append(
            f"Budget notice: this lane is over its advisory budget "
            f"({figures}). Nothing is stopped or refused; the operator "
            "decides. Finish or report your current step and keep "
            "coordination brief."
        )
    return " ".join(parts)


def enforced(manifest: dict) -> dict:
    """Returns the run limits a project opted into enforcing.

    Args:
        manifest: Project manifest.

    Returns:
        The enforced aggregate limit per budget field; empty when the
        project keeps every budget advisory.
    """
    return dict(manifest.get("run_budget") or {})


def _fresh(cursor: int = 0, started: float = 0.0) -> dict:
    """Returns an empty run ledger.

    Args:
        cursor: Highest event identifier already accounted for.
        started: Instant counting began; transcripts untouched since are
            history and not counted.
    """
    return {
        "tokens": {},
        "hours": {},
        "calls": {"cursor": cursor, "total": 0},
        "offset": {},
        "cursors": {},
        "started": started,
        "exhausted": None,
        "missing": [],
        "reserved": 0,
    }


def _baseline(
    home: Path, directory: Path, manifest: dict, ledger: dict, fields: set
) -> None:
    """Starts counting the given fields at the present.

    The call cursor moves to the project's latest served call. Every
    transcript a lane's client already kept is recorded at its current end,
    keeping the tokens counted so far, so a transcript that keeps growing
    counts only what it gains from now on. The hours live sessions have
    already run are folded in and offset.

    Args:
        home: Private bridge state root.
        directory: Private project state directory.
        manifest: Project manifest.
        ledger: Run ledger to update in place.
        fields: Budget fields whose counting starts now.
    """
    if "calls" in fields:
        with contextlib.suppress(sqlite3.Error):
            ledger["calls"]["cursor"] = store.served_since(
                home, manifest["root"], 0
            )[1]
    if "tokens" in fields:
        for name, participant in manifest["participants"].items():
            sources = records.lane_sources(home, participant, {}, math.inf)
            for source in sources or []:
                key = f"{name}:{source['key']}"
                ledger["tokens"].setdefault(key, 0)
                ledger["cursors"][key] = {
                    "offset": source["offset"],
                    "last": "",
                }
    if "hours" in fields:
        kept = {key: ledger[key] for key in ("missing", "reserved")}
        before = _raw(ledger)["hours"]
        _fold(home, directory, manifest, ledger, {"hours": 0})
        ledger.update(kept)
        ledger["offset"]["hours"] = round(
            ledger["offset"].get("hours", 0) + _raw(ledger)["hours"] - before,
            4,
        )


def _begin(home: Path, directory: Path, manifest: dict) -> dict:
    """Returns a run ledger that counts only use from the present on."""
    ledger = _fresh(0, time.time())
    _baseline(home, directory, manifest, ledger, set(roster.BUDGET_FIELDS))
    return ledger


def start(
    home: Path, directory: Path, manifest: dict, before: dict | None = None
) -> None:
    """Starts counting each newly enforced run limit at the present.

    Called before a run budget is recorded, so a ledger that is missing
    while a run budget is enforced always means it was lost. The first
    limit writes a ledger that begins at the present; a limit added to a
    run that already has one moves only that field's counting to the
    present, since its use was not read while it was advisory. Either way
    use from before enforcement never counts.

    Args:
        home: Private bridge state root.
        directory: Private project state directory.
        manifest: Project manifest naming the root, lanes and limits.
        before: The limits enforced until now.
    """
    added = set(enforced(manifest)) - set(before or {})
    with lock(directory / "run-budget.lock", timeout=LOCK_SECONDS):
        if not (directory / LEDGER).exists():
            write_json(directory / LEDGER, _begin(home, directory, manifest))
            return
        ledger, cause = _load(directory, manifest)
        if added and not cause:
            _baseline(home, directory, manifest, ledger, added)
            write_json(directory / LEDGER, ledger)


def _sound(data: object) -> bool:
    """Reports whether a parsed ledger has the shape this module writes."""
    if not isinstance(data, dict):
        return False
    calls = data.get("calls")
    exhausted = data.get("exhausted")
    cursors = data.get("cursors", {})
    return (
        isinstance(cursors, dict)
        and all(
            isinstance(value, dict)
            and type(value.get("offset")) is int
            and isinstance(value.get("last", ""), str)
            for value in cursors.values()
        )
        and type(data.get("started", 0)) in (int, float)
        and all(
            isinstance(data.get(key), dict)
            and all(type(value) in (int, float) for value in data[key].values())
            for key in ("tokens", "hours", "offset")
        )
        and isinstance(calls, dict)
        and all(type(calls.get(key)) is int for key in ("cursor", "total"))
        and (exhausted is None or isinstance(exhausted, dict))
        and isinstance(data.get("missing", []), list)
        and type(data.get("reserved", 0)) is int
    )


def _load(directory: Path, manifest: dict) -> tuple[dict, str]:
    """Reads the run ledger, naming why it could not be trusted.

    Args:
        directory: Private project state directory.
        manifest: Project manifest.

    Returns:
        The ledger, or a fresh one, and a cause that is empty unless the
        recorded ledger was unreadable or malformed, or is missing while a
        run budget is enforced, since ``start`` wrote it before any limit.
    """
    try:
        data = json.loads((directory / LEDGER).read_text())
    except FileNotFoundError:
        if not enforced(manifest):
            return _fresh(), ""
        return _fresh(), "the run ledger was missing, so prior use is unknown"
    except (OSError, ValueError):
        data = None
    if _sound(data):
        return {
            "missing": [],
            "reserved": 0,
            "cursors": {},
            "started": 0.0,
            **data,
        }, ""
    return _fresh(), "the run ledger was unreadable, so prior use is unknown"


def _set_aside(directory: Path) -> None:
    """Moves an untrusted ledger aside rather than overwriting it."""
    with contextlib.suppress(FileNotFoundError):
        os.replace(directory / LEDGER, directory / f"{LEDGER}.corrupt")


def halted(directory: Path, manifest: dict) -> dict | None:
    """Reads the recorded run exhaustion without taking new readings.

    Args:
        directory: Private project state directory.
        manifest: Project manifest.

    Returns:
        The exhaustion record, a record naming the cause when the ledger is
        unreadable or missing, or None when the run may proceed or nothing
        is enforced.
    """
    if not enforced(manifest):
        return None
    ledger, cause = _load(directory, manifest)
    return {"cause": cause} if cause else ledger["exhausted"]


def unmetered(directory: Path, manifest: dict) -> list[str]:
    """Names the lanes the last accounting found it could not meter.

    Args:
        directory: Private project state directory.
        manifest: Project manifest.

    Returns:
        Lanes refused because their use cannot be metered; empty when
        nothing is enforced or the ledger cannot be trusted.
    """
    if not enforced(manifest):
        return []
    ledger, cause = _load(directory, manifest)
    return [] if cause else [str(name) for name in ledger["missing"]]


def _fold(
    home: Path, directory: Path, manifest: dict, ledger: dict, wanted: dict
) -> list[str]:
    """Adds the current readings of every lane to the run ledger.

    Each token source is keyed by lane, transcript path and inode, and each
    session by lane and start time, and only its highest reading is kept,
    so rereading the same source after a restart never counts it twice and
    a replaced lane's sources stay counted after it is gone. Every
    transcript of a lane is advanced from the byte offset the ledger
    recorded for it, so a transcript that stopped growing between polls is
    read to its end. A new transcript is a new source even when it replays
    messages an earlier one recorded, so a resumed session can be counted
    twice. The fold also records the unmetered lanes and releases the calls
    reserved by admissions, whose use it has now read.

    Args:
        home: Private bridge state root.
        directory: Private project state directory.
        manifest: Project manifest.
        ledger: Run ledger to update in place.
        wanted: Enforced limits; unlimited fields are not read.

    Returns:
        Lanes that are not retired and cannot be metered: without readable
        token records while a token limit is enforced, or with a live
        session whose start was never recorded while an hours limit is.
    """
    now = time.time()
    missing = []
    for name, participant in manifest["participants"].items():
        unmetered = False
        if "tokens" in wanted:
            prefix = f"{name}:"
            known = {
                key.removeprefix(prefix): {
                    **cursor,
                    "tokens": ledger["tokens"].get(key, 0),
                }
                for key, cursor in ledger["cursors"].items()
                if key.startswith(prefix)
            }
            sources = records.lane_sources(
                home, participant, known, float(ledger["started"])
            )
            unmetered = sources is None
            for source in sources or []:
                key = prefix + source["key"]
                ledger["tokens"][key] = max(
                    ledger["tokens"].get(key, 0), source["tokens"]
                )
                ledger["cursors"][key] = {
                    "offset": source["offset"],
                    "last": source["last"],
                }
        if "hours" in wanted:
            state = checkpoints.activity(directory, name)
            started = state.get("session_started")
            if process.alive(
                state.get("session_pid"), state.get("session_ticks")
            ):
                if isinstance(started, (int, float)):
                    key = f"{name}:{started}"
                    ledger["hours"][key] = max(
                        ledger["hours"].get(key, 0),
                        round(max(0.0, now - float(started)) / 3600, 4),
                    )
                else:
                    unmetered = True
        if unmetered and not roster.retired(participant):
            missing.append(name)
    if "calls" in wanted:
        served, cursor = store.served_since(
            home, manifest["root"], ledger["calls"]["cursor"]
        )
        ledger["calls"] = {
            "cursor": cursor,
            "total": ledger["calls"]["total"] + served,
        }
    ledger.update(missing=missing, reserved=0)
    return missing


def _raw(ledger: dict) -> dict:
    """Sums the ledger's sources per field, before any reset offset."""
    return {
        "tokens": sum(ledger["tokens"].values()),
        "calls": ledger["calls"]["total"],
        "hours": round(sum(ledger["hours"].values()), 4),
    }


def _used(ledger: dict) -> dict:
    """Reports consumption since the last reset per field."""
    return {
        field: max(0, value - ledger["offset"].get(field, 0))
        for field, value in _raw(ledger).items()
    }


def _over(used: dict, wanted: dict) -> list[str]:
    """Names the enforced fields whose allowance is used up."""
    return [
        field
        for field in roster.BUDGET_FIELDS
        if field in wanted and used[field] >= wanted[field]
    ]


def account(home: Path, directory: Path, manifest: dict) -> dict:
    """Folds current readings into the run ledger and records exhaustion.

    The supervision poll calls this once per tick. The ledger is read,
    updated and written under one lock, so concurrent polls, admissions
    and operator commands see one sequence of readings, and the first of
    them to find an allowance used up records the exhaustion every later
    one refuses on. A ledger that cannot be read, or is missing while a run
    budget is enforced, is moved aside rather than overwritten, rebuilt
    from the evidence that remains, and recorded as exhausted, because an
    unknown prior use must not read as none. The exhaustion is escalated
    once, when it is first recorded.

    Args:
        home: Private bridge state root.
        directory: Private project state directory.
        manifest: Project manifest.

    Returns:
        The enforced ``limits``, the ``used`` amounts, the unmetered lanes
        ``missing`` and the ``exhausted`` record or None; all empty when
        nothing is enforced.
    """
    wanted = enforced(manifest)
    if not wanted:
        return {"limits": {}, "used": {}, "missing": [], "exhausted": None}
    path = directory / LEDGER
    with lock(directory / "run-budget.lock", timeout=LOCK_SECONDS):
        ledger, cause = _load(directory, manifest)
        if cause:
            _set_aside(directory)
        missing = _fold(home, directory, manifest, ledger, wanted)
        used = _used(ledger)
        crossed = _over(used, wanted)
        fresh = ledger["exhausted"] is None and bool(crossed or cause)
        if fresh:
            ledger["exhausted"] = {
                "at": time.time(),
                "fields": crossed,
                "used": used,
                "limits": wanted,
                "cause": cause
                or ", ".join(
                    f"{field} {_figure(field, used[field])} of "
                    f"{_figure(field, wanted[field])}"
                    for field in crossed
                ),
            }
        write_json(path, ledger)
    if fresh:
        with contextlib.suppress(BridgeError, OSError):
            notify.deliver(
                directory,
                "run-budget",
                notify.Event.RUN_BUDGET_EXHAUSTED,
                {
                    "repo": manifest["root"],
                    "since": ledger["exhausted"]["at"],
                    "detail": f"{ledger['exhausted']['cause']}; {RESUME}",
                },
            )
    return {
        "limits": wanted,
        "used": used,
        "missing": missing,
        "exhausted": ledger["exhausted"],
    }


def admit(directory: Path, manifest: dict, name: str) -> str:
    """Decides whether the service may start one more turn for a lane.

    No reading is taken here: the supervision poll folds every lane once
    per tick through ``account``, and admission reads what it recorded,
    so a poll that wakes many lanes costs one fold, not one per lane.
    While a calls limit is enforced, each admitted turn reserves one call
    under the ledger lock until the next fold reads its use, so concurrent
    admissions can never share the last call of the allowance.

    Args:
        directory: Private project state directory.
        manifest: Project manifest.
        name: Lane the wake, dispatch, retry or resume is for.

    Returns:
        Why the turn is refused, empty when it may start or nothing is
        enforced. An exhausted run, or a ledger that is missing or
        unreadable, refuses every lane; a lane the last fold found without
        readable token records while a token limit is enforced, or with a
        live session started outside ``agent-parley run`` while an hours
        limit is, is refused, because an unmetered lane would otherwise be
        unlimited.

    Raises:
        LockBusy: If the ledger lock stays held for ``LOCK_SECONDS``.
    """
    wanted = enforced(manifest)
    if not wanted:
        return ""
    with lock(directory / "run-budget.lock", timeout=LOCK_SECONDS):
        ledger, cause = _load(directory, manifest)
        stopped = {"cause": cause} if cause else ledger["exhausted"]
        if stopped:
            return f"run budget exhausted ({stopped['cause']}); {RESUME}"
        if name in ledger["missing"]:
            return (
                f"run budget: {name} has no readable token records or a live "
                "session with no recorded start, so its use cannot be "
                "metered"
            )
        if "calls" in wanted:
            if _used(ledger)["calls"] + ledger["reserved"] >= wanted["calls"]:
                return (
                    "run budget: the remaining calls are reserved by turns "
                    "already admitted; the next poll releases them once "
                    "their use is read"
                )
            ledger["reserved"] += 1
            write_json(directory / LEDGER, ledger)
    return ""


def resume(
    home: Path, directory: Path, manifest: dict, reset: bool = False
) -> str:
    """Clears a recorded run exhaustion on an operator's authority.

    Current readings are folded in before anything is decided, and a resume
    is refused on the run's present standing. A reset begins a new ledger at
    the present, as enforcement does, so no earlier call or transcript use
    is read again, and offsets the hours live sessions have already run.

    Args:
        home: Private bridge state root.
        directory: Private project state directory.
        manifest: Project manifest.
        reset: Start a new accounting period, so consumption so far no
            longer counts against the limits.

    Returns:
        An account of what was cleared.

    Raises:
        BridgeError: If the run is still at or over an enforced limit, or
            its ledger is unreadable, and no reset was asked for.
    """
    wanted = enforced(manifest)
    path = directory / LEDGER
    with lock(directory / "run-budget.lock", timeout=LOCK_SECONDS):
        ledger, cause = _load(directory, manifest)
        if cause and not reset:
            raise BridgeError(
                f"Cannot resume: {cause}. Pass --reset to start a new "
                "accounting period."
            )
        if cause:
            _set_aside(directory)
        prior = ledger["exhausted"] or cause
        if reset:
            ledger = _begin(home, directory, manifest)
        _fold(home, directory, manifest, ledger, wanted)
        if reset:
            ledger["offset"] = _raw(ledger)
        crossed = _over(_used(ledger), wanted)
        if crossed:
            raise BridgeError(
                "Cannot resume: the run is still at or over its enforced "
                + ", ".join(crossed)
                + " limit. Raise it with agent-parley budget enforce, or "
                "pass --reset to start a new accounting period."
            )
        ledger.update(exhausted=None, resumed_at=time.time(), reset=reset)
        write_json(path, ledger)
    return (
        ("Resumed the run" if prior else "The run was not exhausted")
        + (" and started a new accounting period" if reset else "")
        + "; the service may wake and dispatch lanes again."
    )


def run_account(home: Path, directory: Path, manifest: dict) -> str:
    """Words the enforced run limits and the run's standing against them.

    Args:
        home: Private bridge state root.
        directory: Private project state directory.
        manifest: Project manifest.

    Returns:
        One paragraph for the operator.
    """
    reading = account(home, directory, manifest)
    if not reading["limits"]:
        return (
            f"{manifest['root']} enforces no run budget; every budget is "
            "advisory."
        )
    figures = ", ".join(
        f"{field} {_figure(field, reading['used'][field])} of "
        f"{_figure(field, reading['limits'][field])}"
        for field in roster.BUDGET_FIELDS
        if field in reading["limits"]
    )
    stopped = reading["exhausted"]
    return (
        f"{manifest['root']} enforces a run budget: {figures} used."
        + (
            " Unmetered, not woken: " + ", ".join(reading["missing"]) + "."
            if reading["missing"]
            else ""
        )
        + (
            f" Exhausted: {stopped['cause']}; {RESUME}."
            if stopped
            else " Standing: within limits."
        )
        + " Counts are what Parley observes, not billed spend."
    )
