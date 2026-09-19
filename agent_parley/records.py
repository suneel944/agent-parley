"""Reads usage and capacity from session records native CLIs already write.

A native client keeps its own session transcript under its config home. That
transcript reports the tokens the client itself counted for the session. Agent
Parley reads those files and never asks a vendor: no network request, no API
key, and no accounting of its own. The number is therefore what one client
reported about one session. It is not billed spend, it is not a price, and two
vendors count differently enough that their numbers do not compare.

Every reading is best effort. Absent, unreadable, malformed or unexpectedly
shaped records report nothing rather than raising, because an operator view
must not fail on a client's private file format.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
from collections.abc import Callable, Iterable
from pathlib import Path

from agent_parley import roster
from agent_parley.state import BridgeError

MAX_READ = 1 << 20
MAX_META = 1 << 16
CODEX_DAYS = 2
CODEX_CANDIDATES = 16
CLAUDE_FIELDS = (
    "input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "output_tokens",
)
DEFAULT_HOMES = {"claude": "~/.claude", "codex": "~/.codex"}
UNSAFE = re.compile(r"[^A-Za-z0-9]")
MAX_TAIL = 1 << 16
EXHAUSTION = re.compile(
    r"usage limit|quota exceed|(?:have|has|ve) hit (?:your|the) limit|"
    r"limit reached",
    re.IGNORECASE,
)
TRANSIENT = re.compile(
    r"rate[ _-]?limit|too many requests|temporarily overloaded",
    re.IGNORECASE,
)

Finder = Callable[[Path, Path], "Path | None"]
Fold = Callable[[dict, dict], None]
CapacityReader = Callable[[dict], "dict | None"]


def _mtime(path: Path) -> float:
    """Returns a modification time, ranking an unreadable file as oldest."""
    try:
        return path.stat().st_mtime
    except OSError:
        return -1.0


def _newest(paths: Iterable[Path]) -> Path | None:
    """Returns the most recently modified readable path, if any exists."""
    newest: Path | None = None
    latest = -1.0
    for path in paths:
        stamp = _mtime(path)
        if stamp > latest:
            newest, latest = path, stamp
    return newest


def _config_home(home: Path, entry: dict, profile: str | None) -> Path | None:
    """Resolves the config home the lane's own launch selected.

    Args:
        home: Private bridge state root.
        entry: Provider definition driving the lane.
        profile: Credential profile name, or None for the default account.

    Returns:
        The directory the native client keeps its records under, or None when
        no directory can be resolved without guessing.

    Raises:
        BridgeError: If the credential profile is undefined or names a home
            the provider cannot apply.
    """
    selected = roster.config_home(home, entry, profile)
    if not selected:
        selected = os.environ.get(entry.get("home_env") or "", "")
    if not selected:
        selected = DEFAULT_HOMES.get(str(entry.get("adapter", "")), "")
    return Path(selected).expanduser() if selected else None


def _claude_records(config: Path, lane: Path) -> Path | None:
    """Finds the newest Claude transcript recorded for one lane."""
    directory = config / "projects" / UNSAFE.sub("-", str(lane))
    return _newest(directory.glob("*.jsonl"))


def _codex_lane(path: Path) -> str:
    """Reads the working directory a Codex rollout recorded for itself."""
    with path.open("rb") as handle:
        line = handle.readline(MAX_META)
    try:
        record = json.loads(line)
    except ValueError:
        return ""
    if not isinstance(record, dict):
        return ""
    payload = record.get("payload")
    source = payload if isinstance(payload, dict) else record
    return str(source.get("cwd", ""))


def _codex_records(config: Path, lane: Path) -> Path | None:
    """Finds a recent Codex rollout whose own record names this lane."""
    root = config / "sessions"
    today = datetime.date.today()
    candidates: list[Path] = []
    for offset in range(CODEX_DAYS):
        day = today - datetime.timedelta(days=offset)
        directory = root / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}"
        candidates.extend(directory.glob("rollout-*.jsonl"))
    ordered = sorted(candidates, key=_mtime, reverse=True)
    for path in ordered[:CODEX_CANDIDATES]:
        try:
            if _codex_lane(path) == str(lane):
                return path
        except OSError:
            continue
    return None


def _fold_claude(record: dict, reading: dict) -> None:
    """Adds one transcript record's reported tokens, once per message."""
    message = record.get("message")
    if not isinstance(message, dict):
        return
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return
    identifier = str(message.get("id", ""))
    if identifier:
        if identifier in reading["seen"]:
            return
        reading["seen"].add(identifier)
    for name in CLAUDE_FIELDS:
        value = usage.get(name)
        if isinstance(value, int) and not isinstance(value, bool):
            reading["tokens"] += value


def _fold_codex(record: dict, reading: dict) -> None:
    """Takes the latest running total a Codex rollout reported."""
    payload = record.get("payload")
    source = payload if isinstance(payload, dict) else record
    info = source.get("info")
    if not isinstance(info, dict):
        return
    usage = info.get("total_token_usage")
    if not isinstance(usage, dict):
        return
    value = usage.get("total_tokens")
    if isinstance(value, int) and not isinstance(value, bool):
        reading["tokens"] = value


ADAPTERS: dict[str, tuple[Finder, Fold]] = {
    "claude": (_claude_records, _fold_claude),
    "codex": (_codex_records, _fold_codex),
}


def _stamp(value: object) -> float | None:
    """Parses the ISO 8601 instant a native client wrote on a record."""
    try:
        moment = datetime.datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.UTC)
    return moment.timestamp()


def _capacity_state(text: str) -> str | None:
    """Classifies provider-authored refusal text without reading user prose."""
    if TRANSIENT.search(text):
        return "retryable"
    if EXHAUSTION.search(text):
        return "exhausted"
    return None


def _claude_capacity(record: dict) -> dict | None:
    """Reads one validated Claude capacity observation.

    Claude marks a request its API refused on the transcript record itself and
    keeps the refusal text in the message content. A later successful assistant
    response proves that this session could make another provider request.

    Args:
        record: One parsed transcript record.

    Returns:
        Capacity state and observation time, or None when this record does not
        establish provider capacity.
    """
    at = _stamp(record.get("timestamp"))
    if at is None:
        return None
    message = record.get("message")
    if not isinstance(message, dict):
        return None
    if record.get("isApiErrorMessage"):
        content = json.dumps(message.get("content") or "")
        state = _capacity_state(content)
        return {"state": state, "observed_at": at} if state else None
    usage = message.get("usage")
    if record.get("type") == "assistant" and isinstance(usage, dict):
        identifier = str(message.get("id", ""))
        if identifier:
            return {
                "state": "available",
                "observed_at": at,
                "progress": identifier,
            }
    return None


def _reset_at(rate_limits: dict, reached: str) -> float | None:
    """Reads the reset instant for the rate-limit window that was reached."""
    windows = (
        [rate_limits.get(reached)]
        if reached in {"primary", "secondary", "individual_limit"}
        else [
            rate_limits.get("primary"),
            rate_limits.get("secondary"),
            rate_limits.get("individual_limit"),
        ]
    )
    resets = []
    for window in windows:
        if not isinstance(window, dict):
            continue
        value = window.get("resets_at")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            resets.append(float(value))
    return max(resets) if resets else None


def _codex_capacity(record: dict) -> dict | None:
    """Reads one validated Codex capacity observation.

    Codex wraps each rollout record in a payload naming its own kind, so the
    reader matches refusal text only on an error. Token-count events carry
    structured rate limits and prove a successful request.

    Args:
        record: One parsed rollout record.

    Returns:
        Capacity state and observation time, or None when this record does not
        establish provider capacity.
    """
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None
    at = _stamp(record.get("timestamp") or payload.get("timestamp"))
    if at is None:
        return None
    kind = str(payload.get("type", ""))
    if kind in ("error", "stream_error"):
        state = _capacity_state(str(payload.get("message", "")))
        return {"state": state, "observed_at": at} if state else None
    if kind != "token_count":
        return None
    info = payload.get("info")
    usage = info.get("total_token_usage") if isinstance(info, dict) else None
    progress = usage.get("total_tokens") if isinstance(usage, dict) else None
    if not isinstance(progress, int) or isinstance(progress, bool):
        progress = None
    limits = payload.get("rate_limits")
    if not isinstance(limits, dict):
        return (
            {"state": "available", "observed_at": at, "progress": progress}
            if progress is not None
            else None
        )
    reached = str(limits.get("rate_limit_reached_type") or "")
    exhausted = bool(reached or limits.get("spend_control_reached"))
    if not exhausted:
        exhausted = any(
            isinstance(window, dict)
            and isinstance(window.get("used_percent"), (int, float))
            and not isinstance(window.get("used_percent"), bool)
            and float(window["used_percent"]) >= 100
            for window in (
                limits.get("primary"),
                limits.get("secondary"),
                limits.get("individual_limit"),
            )
        )
    if exhausted:
        return {
            "state": "exhausted",
            "observed_at": at,
            "reset_at": _reset_at(limits, reached),
            "progress": progress,
        }
    return (
        {"state": "available", "observed_at": at, "progress": progress}
        if progress is not None
        else None
    )


CAPACITY_READERS: dict[str, CapacityReader] = {
    "claude": _claude_capacity,
    "codex": _codex_capacity,
}


def capacity_observation(home: Path, participant: dict) -> dict | None:
    """Reports the latest capacity event in a lane's native session record.

    The reading comes from the same session records the reported token count
    is parsed from, so it costs no vendor request, no API key and no account
    of its own. Only provider-authored error envelopes, successful response
    records, and structured rate-limit fields can produce an observation.
    Ordinary transcript prose is ignored.

    Every provider whose client publishes nothing has no reader here and
    reports None, which a caller must treat as no opinion rather than as a
    lane in good standing.

    Args:
        home: Private bridge state root.
        participant: Manifest entry naming the lane, provider and account.

    Returns:
        Latest capacity observation with its evidence source, record identity
        and session identity, or None when no validated event could be read.
    """
    try:
        entry = roster.provider(home, str(participant.get("provider", "")))
        adapter = str(entry.get("adapter", ""))
        finder = ADAPTERS[adapter][0]
        reader = CAPACITY_READERS[adapter]
        config = _config_home(home, entry, participant.get("credential"))
        if config is None:
            return None
        path = finder(config, Path(str(participant.get("lane", ""))))
        if path is None:
            return None
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(max(0, size - MAX_TAIL))
            chunk = handle.read(MAX_TAIL)
    except (BridgeError, KeyError, OSError, ValueError):
        return None
    latest: dict | None = None
    last_progress: str | int | None = None
    seen_progress: set[str | int] = set()
    for line in chunk.split(b"\n")[1 if size > MAX_TAIL else 0 :]:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        observation = reader(record) if isinstance(record, dict) else None
        if observation is None:
            continue
        state = observation["state"]
        marker = observation.get("progress")
        progressed = False
        if state == "available":
            if not isinstance(marker, (str, int)) or isinstance(marker, bool):
                continue
            if marker in seen_progress:
                continue
            if adapter == "codex":
                progressed = (
                    isinstance(marker, int)
                    and isinstance(last_progress, int)
                    and marker > last_progress
                )
            else:
                progressed = marker is not None and marker != last_progress
            seen_progress.add(marker)
            if (
                latest is not None
                and latest["state"] in {"exhausted", "retryable"}
                and adapter == "codex"
                and not progressed
            ):
                last_progress = marker
                continue
            observation["progressed"] = progressed
            last_progress = marker
        elif marker is None:
            observation["progress"] = last_progress
        if (
            latest is None
            or observation["observed_at"] >= latest["observed_at"]
        ):
            latest = observation
            latest["source"] = f"{adapter}-session-record"
            latest["session_id"] = path.stem
            latest["observation_id"] = hashlib.sha256(line).hexdigest()[:16]
    return latest


def reported_refusal(home: Path, participant: dict) -> float | None:
    """Reports the latest validated refusal time for compatibility callers."""
    observation = capacity_observation(home, participant)
    if observation and observation["state"] in {"exhausted", "retryable"}:
        return float(observation["observed_at"])
    return None


def _advance(path: Path, fold: Fold, reading: dict) -> dict:
    """Folds only the records appended since the previous reading.

    A live session appends to its record continuously, so re-reading the whole
    file on every refresh would make the view pay for the session's history
    again each second. The reading remembers the byte offset it stopped at and
    resumes there, consuming at most ``MAX_READ`` bytes and stopping on the
    last complete line, so a partly written record is never parsed. A record
    that grew by more than the budget catches up over later refreshes. A
    replaced or truncated file starts a new reading.

    Args:
        path: Session record file.
        fold: Adapter-specific accumulator for one parsed record.
        reading: Previous reading for this lane, or an empty mapping.

    Returns:
        The updated reading.
    """
    stat = path.stat()
    if (
        reading.get("path") != str(path)
        or reading.get("inode") != stat.st_ino
        or stat.st_size < int(reading.get("offset", 0))
    ):
        reading = {
            "path": str(path),
            "inode": stat.st_ino,
            "offset": 0,
            "tokens": 0,
            "seen": set(),
        }
    if stat.st_size == reading["offset"]:
        return reading
    with path.open("rb") as handle:
        handle.seek(reading["offset"])
        chunk = handle.read(MAX_READ)
    end = chunk.rfind(b"\n")
    if end < 0:
        return reading
    reading["offset"] += end + 1
    for line in chunk[:end].split(b"\n"):
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            fold(record, reading)
    return reading


def reported_tokens(home: Path, participant: dict, cache: dict) -> int | None:
    """Reports the tokens a lane's own native client recorded for itself.

    The value is read from the client's session records under the config home
    that lane was launched with, so a credential profile that relocates the
    config home is followed rather than assumed away. It is the client's own
    count of the tokens its session consumed: not billed spend, not a price,
    and not comparable between vendors. It is also unrelated to the injected
    bytes the view reports, which measure only what coordination itself adds.

    Args:
        home: Private bridge state root.
        participant: Manifest entry naming the lane, provider and account.
        cache: Caller-owned mapping of lane to its previous reading, which
            keeps a live refresh reading only newly appended records.

    Returns:
        The reported token count, or None when this lane has no readable
        session records, which the view renders as a blank cell.
    """
    key = str(participant.get("lane", ""))
    try:
        entry = roster.provider(home, str(participant.get("provider", "")))
        finder, fold = ADAPTERS[str(entry.get("adapter", ""))]
        config = _config_home(home, entry, participant.get("credential"))
        if config is None:
            return None
        path = finder(config, Path(key))
        if path is None:
            cache.pop(key, None)
            return None
        reading = _advance(path, fold, cache.get(key, {}))
    except (BridgeError, KeyError, OSError, ValueError):
        return None
    cache[key] = reading
    return int(reading["tokens"])
