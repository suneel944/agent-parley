"""Parks lanes that share one credential before its quota window runs out.

Lanes launched on one explicit credential profile draw on one provider plan,
so they exhaust it together. Two signals are read. The first real one is a
vendor rate-limit refusal that named a reset time on any lane of the
credential: every lane on that credential is parked until that reset. The
second is optional and estimated: an operator who records the credential's
window length and token limit with ``agent-parley quota set`` has the
native token counts of its lanes summed over that window, and crossing the
configured threshold parks the lowest-priority lanes first, every lane but
the highest-priority one, while reaching the limit parks every lane until
the window ends. No vendor limit is invented: without a recorded window
only the vendor's own refusal parks anything.

Parking only defers what the service starts for a lane: wakes, Stop-hook
continuations and work offers. It never moves a lane to another provider or
credential, never touches its claims or reservations, and never stops a
running turn. Each poll recomputes the parked set, so a lane resumes on the
first poll past its reset with no operator action, and a parked record
whose time has passed reads as not parked even before that poll.

Grouping follows the capacity sharing rule: only lanes that name the same
credential profile on the same provider adapter share a quota, and a lane
on the default account shares nothing. Usage is counted per project, so a
credential also used by another project's lanes is under-counted there;
that is one reason the derived figures are always marked estimated.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TypeGuard

from agent_parley import budgets, issues, roster
from agent_parley.state import BridgeError, lock, write_json

WINDOWS = "quotas.json"
LEDGER = "quota-ledger.json"
LOCK_SECONDS = 10.0
_CACHE: dict = {}


def windows(home: Path) -> dict[str, dict]:
    """Reads the quota windows the operator recorded per credential.

    Args:
        home: Private bridge state root.

    Returns:
        Each credential profile's ``hours``, ``tokens`` and ``threshold``;
        empty when nothing was recorded or the file is unreadable.
    """
    try:
        value = json.loads((home / WINDOWS).read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(value, dict):
        return {}
    return {
        str(name): entry
        for name, entry in value.items()
        if isinstance(entry, dict)
    }


def set_window(
    home: Path,
    credential: str,
    hours: float,
    tokens: int,
    threshold: float = 1.0,
) -> dict:
    """Records or removes the quota window of one credential profile.

    Args:
        home: Private bridge state root.
        credential: Credential profile the window belongs to.
        hours: Window length in hours.
        tokens: Native tokens the plan allows per window; 0 removes the
            window.
        threshold: Fraction of ``tokens`` at which lower-priority lanes
            start to park; 1.0 parks only at the limit.

    Returns:
        Every recorded window after the change.

    Raises:
        BridgeError: If the profile is undefined or a value is unusable.
    """
    roster.credential(home, credential)
    if tokens != 0 and (tokens < 0 or not hours > 0 or not 0 < threshold <= 1):
        raise BridgeError(
            "A quota window needs positive --hours and --tokens and a "
            "--threshold above 0 and at most 1."
        )
    with lock(home / "quotas.lock", timeout=LOCK_SECONDS):
        recorded = windows(home)
        if tokens == 0:
            recorded.pop(credential, None)
        else:
            recorded[credential] = {
                "hours": float(hours),
                "tokens": int(tokens),
                "threshold": float(threshold),
            }
        write_json(home / WINDOWS, recorded)
    return recorded


def accounts(home: Path, manifest: dict) -> dict[str, dict]:
    """Groups lanes by the provider account an explicit credential names.

    Args:
        home: Private bridge state root.
        manifest: Project manifest.

    Returns:
        Per ``adapter:credential`` key, the ``credential``, ``adapter`` and
        sorted ``lanes``. Lanes on the default account or an unknown
        provider are left out.
    """
    groups: dict[str, dict] = {}
    for name, participant in manifest["participants"].items():
        credential = participant.get("credential")
        if not credential:
            continue
        try:
            adapter = str(
                roster.provider(home, participant["provider"])["adapter"]
            )
        except (BridgeError, KeyError):
            continue
        group = groups.setdefault(
            f"{adapter}:{credential}",
            {"credential": credential, "adapter": adapter, "lanes": []},
        )
        group["lanes"].append(name)
    for group in groups.values():
        group["lanes"].sort()
    return groups


def _instant(value: object) -> TypeGuard[int | float]:
    """Reports whether a value is a usable Unix time."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def vendor_reset(
    lanes: list[str], observed: Mapping[str, dict], now: float
) -> tuple[str, float] | None:
    """Finds the latest reset a vendor refusal named that is still ahead.

    Args:
        lanes: Lanes sharing one credential.
        observed: Durable capacity observation per lane.
        now: Current Unix time.

    Returns:
        The lane that reported it and the reset time, or None.
    """
    found = [
        (float(reading["reset_at"]), name)
        for name in lanes
        if (reading := observed.get(name) or {}).get("state") == "exhausted"
        and _instant(reading.get("reset_at"))
        and float(reading["reset_at"]) > now
    ]
    if not found:
        return None
    reset_at, name = max(found)
    return name, reset_at


def roll(
    entry: dict,
    window: Mapping | None,
    counts: Mapping[str, int | None],
    now: float,
) -> dict:
    """Folds the latest native token counts into one credential's window.

    A lane's count is its client's running total, so the window keeps the
    count it started from and charges only growth. A count that falls,
    because records rotated or a new session began, carries what was
    already charged and restarts from the lower count. The window restarts
    when its length has passed, or at a vendor reset seen inside it, so a
    reported reset realigns the estimate with the vendor's own window.

    Args:
        entry: The credential's previous ledger entry, possibly empty.
        window: The recorded window, or None when none applies.
        counts: Native token count per lane, None when unreadable.
        now: Current Unix time.

    Returns:
        The updated entry with ``started``, ``readings`` and ``used``.
    """
    if window is None:
        return {}
    length = float(window["hours"]) * 3600
    previous = entry.get("started")
    readings = dict(entry.get("readings") or {})
    seen = entry.get("vendor_reset")
    started = float(previous) if _instant(previous) else now
    if _instant(seen) and started < float(seen) <= now:
        started = float(seen)
    elif _instant(previous) and now >= started + length:
        started = now
    restart = started if started != previous else None
    if restart is not None:
        readings = {
            name: {"base": reading["last"], "last": reading["last"]}
            for name, reading in readings.items()
        }
    for name, count in counts.items():
        if count is None:
            continue
        reading = readings.get(name)
        if reading is None:
            base = count if restart == now and not entry else 0
            reading = {"base": base, "last": count}
        elif count < reading["last"]:
            reading = {
                "base": count,
                "last": count,
                "carried": reading.get("carried", 0)
                + reading["last"]
                - reading["base"],
            }
        else:
            reading = {**reading, "last": count}
        readings[name] = reading
    used = sum(
        reading.get("carried", 0) + reading["last"] - reading["base"]
        for reading in readings.values()
    )
    return {
        "started": float(started),
        "ends": float(started) + length,
        "readings": readings,
        "used": used,
        "vendor_reset": seen,
    }


def _priorities(directory: Path, lanes: list[str]) -> list[str]:
    """Orders lanes from lowest to highest parking priority.

    A lane holding more unfinished claims has more work in flight, so it
    keeps running longest; ties are broken by name.

    Args:
        directory: Private project state directory.
        lanes: Lanes sharing one credential.

    Returns:
        The lanes, lowest priority first.
    """
    held = dict.fromkeys(lanes, 0)
    for record in issues.snapshot(directory)["issues"].values():
        owner = record.get("owner")
        if owner in held and not issues.delivered(record):
            held[owner] += 1
    return sorted(lanes, key=lambda name: (held[name], name))


def decide(
    group: Mapping,
    entry: dict,
    window: Mapping | None,
    reported: tuple[str, float] | None,
    ranked: list[str],
) -> dict[str, dict]:
    """Chooses which lanes of one credential park and until when.

    Args:
        group: The credential's ``credential`` and ``lanes``.
        entry: The credential's rolled window entry, empty without one.
        window: The recorded window, or None.
        reported: Lane and reset of a vendor refusal still ahead, or None.
        ranked: The credential's lanes, lowest priority first.

    Returns:
        Per parked lane its ``credential``, ``cause``, ``until`` and whether
        the time is ``estimated``.
    """
    credential = group["credential"]
    if reported is not None:
        source, until = reported
        cause = (
            f"parked on credential {credential}: {source} reached its rate "
            f"limit until {int(until)}"
        )
        return {
            name: {
                "credential": credential,
                "cause": cause,
                "until": until,
                "estimated": False,
            }
            for name in group["lanes"]
        }
    if window is None or not entry:
        return {}
    limit = int(window["tokens"])
    used = int(entry["used"])
    threshold = float(window.get("threshold", 1.0))
    if used >= limit:
        chosen = ranked
    elif used >= threshold * limit:
        chosen = ranked[:-1]
    else:
        return {}
    cause = (
        f"parked on credential {credential}: an estimated {used} of {limit} "
        f"window tokens used until {int(entry['ends'])}"
    )
    return {
        name: {
            "credential": credential,
            "cause": cause,
            "until": entry["ends"],
            "estimated": True,
        }
        for name in chosen
    }


def account(
    home: Path,
    directory: Path,
    manifest: dict,
    observe: Callable[[Path, str], dict],
) -> dict:
    """Recomputes every credential's window and the parked lanes once.

    The supervision poll calls this once per tick. Token counts are read
    only for a credential with a recorded window, so a project that records
    none parses no transcript here.

    Args:
        home: Private bridge state root.
        directory: Private project state directory.
        manifest: Project manifest.
        observe: Reads one lane's durable capacity observation.

    Returns:
        The ledger written: per credential its window entry, the vendor
        reset still ahead, and the parked lanes.
    """
    now = time.time()
    recorded = windows(home)
    with lock(directory / "quota.lock", timeout=LOCK_SECONDS):
        previous = read(directory)
        credentials: dict[str, dict] = {}
        parked: dict[str, dict] = {}
        for key, group in accounts(home, manifest).items():
            lanes = group["lanes"]
            window = recorded.get(group["credential"])
            reported = vendor_reset(
                lanes, {name: observe(directory, name) for name in lanes}, now
            )
            entry = dict(previous["credentials"].get(key) or {})
            if reported is not None:
                entry["vendor_reset"] = reported[1]
            counts = (
                {
                    name: budgets.consumption(
                        home, directory, manifest, name, {}, _CACHE, {"tokens"}
                    )["tokens"]
                    for name in lanes
                }
                if window is not None
                else {}
            )
            rolled = roll(entry, window, counts, now)
            ranked = _priorities(directory, lanes)
            parked.update(decide(group, rolled, window, reported, ranked))
            credentials[key] = {
                **group,
                "window": dict(window) if window is not None else None,
                "entry": rolled,
                "reported": list(reported) if reported is not None else None,
            }
        ledger = {"at": now, "credentials": credentials, "parked": parked}
        write_json(directory / LEDGER, ledger)
    return ledger


def read(directory: Path) -> dict:
    """Reads the last quota ledger, or an empty one.

    Args:
        directory: Private project state directory.

    Returns:
        The ledger with ``credentials`` and ``parked`` always present.
    """
    try:
        value = json.loads((directory / LEDGER).read_text())
    except (OSError, ValueError):
        value = {}
    if not isinstance(value, dict):
        value = {}
    return {
        "at": value.get("at"),
        "credentials": dict(value.get("credentials") or {}),
        "parked": dict(value.get("parked") or {}),
    }


def held(directory: Path, name: str, now: float | None = None) -> dict | None:
    """Reports whether a lane is parked on its credential's quota now.

    Args:
        directory: Private project state directory.
        name: Participant that owns the lane.
        now: Current Unix time; the clock when omitted.

    Returns:
        The parked record with its ``cause`` and ``until``, or None when the
        lane is not parked or its time has passed.
    """
    record = read(directory)["parked"].get(name)
    current = time.time() if now is None else now
    if (
        not isinstance(record, dict)
        or not _instant(record.get("until"))
        or float(record["until"]) <= current
    ):
        return None
    return record


def headroom(ledger: Mapping, now: float) -> list[dict]:
    """Derives the remaining headroom of every credential from a ledger.

    This is a pure function of the ledger ``account`` wrote and the time.
    A vendor refusal still ahead reports no headroom until its reset and is
    not estimated. A recorded window reports the limit less the summed
    native counts and is always estimated, because those counts are the
    clients' own and not the vendor's meter. Without either, headroom is
    unknown.

    Args:
        ledger: A ledger as `read` returns it.
        now: Current Unix time.

    Returns:
        Per credential, sorted by key: ``credential``, ``adapter``,
        ``lanes``, ``state`` (``exhausted``, ``spent``, ``near``,
        ``available`` or ``unknown``), ``remaining``, ``used`` and ``limit``
        in tokens or None, ``reset_at`` or None, ``estimated`` and the
        currently ``parked`` lanes.
    """
    parked = {
        name
        for name, record in (ledger.get("parked") or {}).items()
        if isinstance(record, dict)
        and _instant(record.get("until"))
        and float(record["until"]) > now
    }
    result = []
    for _, item in sorted((ledger.get("credentials") or {}).items()):
        window = item.get("window")
        entry = item.get("entry") or {}
        reported = item.get("reported")
        row = {
            "credential": item["credential"],
            "adapter": item["adapter"],
            "lanes": list(item["lanes"]),
            "state": "unknown",
            "remaining": None,
            "used": None,
            "limit": None,
            "reset_at": None,
            "estimated": False,
            "parked": sorted(parked & set(item["lanes"])),
        }
        if reported and float(reported[1]) > now:
            row.update(state="exhausted", remaining=0, reset_at=reported[1])
        elif window and entry and float(entry["ends"]) > now:
            limit = int(window["tokens"])
            used = int(entry["used"])
            threshold = float(window.get("threshold", 1.0))
            row.update(
                state=(
                    "spent"
                    if used >= limit
                    else "near"
                    if used >= threshold * limit
                    else "available"
                ),
                remaining=max(0, limit - used),
                used=used,
                limit=limit,
                reset_at=entry["ends"],
                estimated=True,
            )
        elif window:
            row.update(
                state="available",
                remaining=int(window["tokens"]),
                used=0,
                limit=int(window["tokens"]),
                estimated=True,
            )
        result.append(row)
    return result


def lines(rows: list[dict]) -> list[str]:
    """Words each credential's headroom as one line of plain text.

    Args:
        rows: Headroom rows as `headroom` returns them.

    Returns:
        One line per credential, or one line saying none is shared.
    """
    if not rows:
        return ["No lane names a credential profile; no quota is shared."]
    shown = []
    for row in rows:
        if row["remaining"] is None:
            left = "headroom unknown"
        else:
            marker = "estimated " if row["estimated"] else ""
            left = f"{marker}{row['remaining']} tokens left"
            if row["limit"] is not None:
                left += f" of {row['limit']}"
            if row["state"] == "exhausted":
                left = "rate limit reached"
        if row["reset_at"] is not None:
            left += f", resets {int(row['reset_at'])}"
        parked = (
            f"; parked: {', '.join(row['parked'])}" if row["parked"] else ""
        )
        shown.append(
            f"{row['credential']} ({row['adapter']}): {row['state']}, "
            f"{left}; lanes: {', '.join(row['lanes'])}{parked}"
        )
    return shown
