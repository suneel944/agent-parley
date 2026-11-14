"""Records verification outcomes and reports the find rate per group.

A review round and a child lane's result report are the two places a second
lane checks work. Each one that finishes leaves one outcome here: clean, or
findings with a count. The record answers whether asking for a review is
worth it for a kind of issue or a kind of lane, from what verification
actually returned rather than from impressions.

An outcome is appended when it happens, never inferred later: `record` runs
when a reviewer's findings are returned (``reviews.returned``) and when a
child's result is relayed to its parent (``children.deliver``). The count is
the one those paths already compute from the capped report text, one finding
per bulleted or numbered line, so no transcript is ever read. The issue's
labels are copied from the cached forge reading in ``status.FORGE_ISSUES`` at
record time, while the issue is still open and listed there. The provider and
model are the verifying lane's, from its participant record: the reviewer of
a round, or the child that reported.

The find rate of a group is the share of its outcomes that returned at least
one finding. A group with fewer than `MIN_OUTCOMES` outcomes in the window
reports no rate, because a rate over two rounds says nothing. The reading is
advisory: nothing here skips, adds or schedules verification.

Outcomes live in `RECORD_NAME`, a line log beside the issue ledger bounded
by ``state.trim_log`` like a lane's report log, under `LOCK_NAME`.
"""

import json
import time
from pathlib import Path

from agent_parley import roster
from agent_parley.state import (
    MAX_LOG_BYTES,
    MAX_LOG_RECORDS,
    BridgeError,
    lock,
    trim_log,
)

RECORD_NAME = "verification.jsonl"
LOCK_NAME = "verification.lock"
REVIEW = "review"
CHILD = "child"
KINDS = (REVIEW, CHILD)
MIN_OUTCOMES = 5
WINDOW = 30 * 86400.0
DEFAULT_MODEL = "default"
LOCK_SECONDS = 0.2


def labels(directory: Path, issue: str) -> list[str]:
    """Reads an issue's labels from the cached forge reading.

    Args:
        directory: Private project state directory holding the cache.
        issue: Bare issue number.

    Returns:
        Sorted label names, empty when the cache is missing, unreadable or
        does not list the issue.
    """
    from agent_parley.status import FORGE_ISSUES

    try:
        cached = json.loads((directory / FORGE_ISSUES).read_text())
        names = cached["issues"][str(issue)].get("labels") or []
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return []
    return sorted({str(name) for name in names if name})


def lane_tier(manifest: dict, lane: str) -> tuple[str, str]:
    """Names the provider and model a lane runs on.

    Args:
        manifest: Project manifest naming the participants.
        lane: Participant name or display name of the verifying lane.

    Returns:
        The provider, empty when the lane is not listed, and the model,
        `DEFAULT_MODEL` when the lane runs on its provider's default.
    """
    for name, entry in manifest.get("participants", {}).items():
        if lane in (name, entry.get("display")):
            return (
                str(entry.get("provider") or ""),
                str(entry.get("model") or DEFAULT_MODEL),
            )
    return "", DEFAULT_MODEL


def record(
    directory: Path,
    manifest: dict | None,
    *,
    kind: str,
    issue: str,
    lane: str,
    findings: int,
    now: float = 0.0,
) -> dict:
    """Appends one verification outcome.

    Recording is best effort, as a lane's report log is: a failed append or
    an unreadable manifest never fails the review or the child report it
    describes.

    Args:
        directory: Private project state directory.
        manifest: Project manifest naming the participants, or None to
            read it here; an unreadable one records no provider.
        kind: `REVIEW` for a review round, `CHILD` for a child report.
        issue: Issue the verified work is on, empty when none is known.
        lane: Lane that did the verification.
        findings: Findings the report returned; zero is a clean outcome.
        now: Unix time; the clock when zero.

    Returns:
        The appended outcome.
    """
    if manifest is None:
        try:
            manifest = roster.read(directory)
        except BridgeError:
            manifest = {}
    provider, model = lane_tier(manifest, lane)
    entry = {
        "at": now or time.time(),
        "kind": kind,
        "issue": str(issue or ""),
        "labels": labels(directory, str(issue)) if issue else [],
        "lane": lane,
        "provider": provider,
        "model": model,
        "findings": max(int(findings), 0),
    }
    path = directory / RECORD_NAME
    line = json.dumps(entry) + "\n"
    try:
        try:
            with lock(directory / LOCK_NAME, timeout=LOCK_SECONDS):
                with path.open("a", encoding="utf-8") as stream:
                    stream.write(line)
                trim_log(path, MAX_LOG_BYTES, MAX_LOG_RECORDS)
        except BridgeError:
            with path.open("a", encoding="utf-8") as stream:
                stream.write(line)
    except OSError:
        return entry
    return entry


def outcomes(directory: Path, since: float = 0.0) -> list[dict]:
    """Reads the recorded outcomes of one project.

    Args:
        directory: Private project state directory.
        since: Unix time floor; older outcomes are omitted.

    Returns:
        Outcomes oldest first, each ``at`` a float; a damaged line, or one
        whose ``at`` is not a number, is skipped.
    """
    try:
        text = (directory / RECORD_NAME).read_text(errors="ignore")
    except OSError:
        return []
    rows = []
    for line in text.splitlines():
        try:
            entry = json.loads(line)
            at = float(entry.get("at") or 0)
        except (AttributeError, TypeError, ValueError):
            continue
        if (
            entry.get("kind") in KINDS
            and isinstance(entry.get("findings"), int)
            and at >= since
        ):
            rows.append({**entry, "at": at})
    return rows


def rate(rows: list[dict]) -> dict:
    """Summarizes one group of outcomes.

    Args:
        rows: Outcomes in the group.

    Returns:
        ``outcomes``, ``found`` (outcomes with at least one finding),
        ``findings`` (their total) and ``rate``, the share found, or None
        below `MIN_OUTCOMES` outcomes.
    """
    found = sum(1 for entry in rows if entry["findings"] > 0)
    return {
        "outcomes": len(rows),
        "found": found,
        "findings": sum(entry["findings"] for entry in rows),
        "rate": found / len(rows) if len(rows) >= MIN_OUTCOMES else None,
    }


def grouped(rows: list[dict]) -> dict:
    """Groups outcomes by issue label and by provider and model.

    An outcome counts once in each of its labels' groups, and in the
    ``unlabelled`` group when its issue had none.

    Args:
        rows: Outcomes to group.

    Returns:
        ``labels`` and ``models``, each a sorted mapping of group name to
        its `rate` summary; a model group is named ``provider/model``.
    """
    by_label: dict[str, list[dict]] = {}
    by_model: dict[str, list[dict]] = {}
    for entry in rows:
        for name in entry.get("labels") or ["unlabelled"]:
            by_label.setdefault(str(name), []).append(entry)
        tier = f"{entry.get('provider') or 'unknown'}/{entry.get('model')}"
        by_model.setdefault(tier, []).append(entry)
    return {
        "labels": {name: rate(by_label[name]) for name in sorted(by_label)},
        "models": {name: rate(by_model[name]) for name in sorted(by_model)},
    }


def reading(home: Path, window: float = WINDOW, now: float = 0.0) -> dict:
    """Reads every project's outcomes inside a window and groups them.

    Args:
        home: Private bridge state root.
        window: Seconds counted back from `now`.
        now: Unix time the window ends; the clock when zero.

    Returns:
        ``since``, ``until``, ``minimum`` (`MIN_OUTCOMES`), the total
        ``outcomes`` and the `grouped` summaries.
    """
    ends = now or time.time()
    since = ends - window
    rows = [
        entry
        for path in sorted((home / "projects").glob("*/project.json"))
        for entry in outcomes(path.parent, since)
        if float(entry["at"]) <= ends
    ]
    return {
        "since": since,
        "until": ends,
        "minimum": MIN_OUTCOMES,
        "outcomes": len(rows),
        **grouped(rows),
    }


def _cell(summary: dict) -> str:
    """Formats one group's rate, or ``-`` below the minimum."""
    if summary["rate"] is None:
        return "-"
    return f"{summary['rate']:.0%}"


def describe(result: dict) -> str:
    """Formats a `reading` for a terminal.

    Args:
        result: Result of `reading`.

    Returns:
        A label table and a provider and model table, or the header line
        and one line stating that no outcome was recorded in the window.
    """
    days = (result["until"] - result["since"]) / 86400.0
    lines = [
        f"Verification outcomes over {days:g}d: {result['outcomes']} "
        f"(rate shown from {result['minimum']} outcomes)"
    ]
    if not result["outcomes"]:
        return lines[0] + "\nNo review round or child report recorded.\n"
    for title, key in (("LABEL", "labels"), ("PROVIDER/MODEL", "models")):
        lines += [
            "",
            f"{title:<32} {'OUTCOMES':>8} {'FOUND':>5} "
            f"{'FINDINGS':>8} {'RATE':>5}",
        ]
        for name, summary in result[key].items():
            lines.append(
                f"{name[:32]:<32} {summary['outcomes']:>8} "
                f"{summary['found']:>5} {summary['findings']:>8} "
                f"{_cell(summary):>5}"
            )
    return "\n".join(lines) + "\n"


def reason(
    directory: Path,
    names: list[str],
    window: float = WINDOW,
    now: float = 0.0,
    rows: list[dict] | None = None,
) -> str:
    """States the find rate of an issue's labels for ``issue next``.

    Args:
        directory: Private project state directory.
        names: The issue's labels.
        window: Seconds counted back from `now`.
        now: Unix time the window ends; the clock when zero.
        rows: Outcomes already read by `outcomes` for this window, so a
            caller stating reasons for many issues reads the log once; None
            reads them here.

    Returns:
        One reason naming each label with at least `MIN_OUTCOMES` outcomes
        in the window and its find rate, or an empty string when none has.
    """
    if not names:
        return ""
    if rows is None:
        ends = now or time.time()
        rows = outcomes(directory, ends - window)
    summaries = grouped(rows)["labels"]
    said = [
        f"{name} {summary['rate']:.0%} of {summary['outcomes']}"
        for name in sorted(set(names))
        if (summary := summaries.get(name)) and summary["rate"] is not None
    ]
    if not said:
        return ""
    return "verification find rate " + ", ".join(said)
