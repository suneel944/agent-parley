"""Launches native agents with isolated worktrees and in-house coordination."""

from __future__ import annotations

import math
import os
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, cast

if TYPE_CHECKING:
    import argparse
    import contextlib
    import datetime
    import hashlib as hashlib
    import json
    import secrets as secrets
    import shlex
    import shutil
    import socket as socket
    import sqlite3
    import string
    import subprocess
    import textwrap
    import uuid as uuid
    from types import ModuleType

    from agent_parley import amp as amp
    from agent_parley import (
        approvals,
        archive,
        budgets,
        checkpoints,
        completion,
        convergence,
        evidence,
        followups,
        forge,
        history,
        inbound,
        issues,
        metrics,
        notify,
        plan,
        problems,
        process,
        protocol,
        recaps,
        reclaim,
        recommend,
        roster,
        state,
        store,
        supervision,
        terminal,
        views,
        worktrees,
    )
    from agent_parley import attachments as attachments
    from agent_parley import delivery as delivery
    from agent_parley import dialogs as dialogs
    from agent_parley import forecast as forecast
    from agent_parley import gemini as gemini
    from agent_parley import lanes as lanes
    from agent_parley import lifecycle as lifecycle
    from agent_parley import merges as merges
    from agent_parley import opencode as opencode
    from agent_parley import retries as retries
    from agent_parley import tables as tables
    from agent_parley import watch as stream
    from agent_parley.checkpoints import (
        activity,
        branch_head,
        current_branch,
        lane_branch,
        mailbox,
        participant_liveness,
        read_events,
    )
    from agent_parley.copilot import configure_copilot as configure_copilot
    from agent_parley.copilot import release_copilot
    from agent_parley.forge import report_comment as report_comment
    from agent_parley.issues import attempt as change_attempt
    from agent_parley.issues import (
        change,
        deadline_state,
        describe,
        handoff_fields,
        held_claim,
        offer_state,
        orphan_age,
        parse_issue,
        snapshot,
    )
    from agent_parley.issues import exact_claim as exact_claim
    from agent_parley.issues import merge_claim as merge_claim
    from agent_parley.merges import attributed_commits
    from agent_parley.merges import group_lanes as group_lanes
    from agent_parley.merges import group_refusal as group_refusal
    from agent_parley.merges import lane_dependencies as lane_dependencies
    from agent_parley.merges import lane_refusals as lane_refusals
    from agent_parley.merges import lane_session as lane_session
    from agent_parley.merges import merge_branch as merge_branch
    from agent_parley.merges import merge_preview as merge_preview
    from agent_parley.merges import (
        outside_prerequisites as outside_prerequisites,
    )
    from agent_parley.merges import ready_lanes as ready_lanes
    from agent_parley.merges import reported_ready as reported_ready
    from agent_parley.merges import session_busy as session_busy
    from agent_parley.merges import unattempted as unattempted
    from agent_parley.retries import operator_key as operator_key
    from agent_parley.state import lock, write_json, write_text
    from agent_parley.worktrees import (
        drift,
        git,
        has_branch,
        initialize_lane,
        launch_line,
        preparation,
        verify_base,
    )
    from agent_parley.worktrees import launched as launched
    from agent_parley.worktrees import preserve_pending as preserve_pending

from agent_parley import BridgeError, refusal
from agent_parley.claims import ClaimsMixin
from agent_parley.context import diff_cell, location_cell, window_cell
from agent_parley.integration import IntegrationMixin
from agent_parley.launch import LaunchMixin
from agent_parley.settings import SettingsMixin
from agent_parley.status import StatusMixin

DEFERRED_MODULES = (
    "amp",
    "approvals",
    "archive",
    "attachments",
    "budgets",
    "checkpoints",
    "completion",
    "convergence",
    "copilot",
    "delivery",
    "dialogs",
    "evidence",
    "followups",
    "forecast",
    "forge",
    "gemini",
    "history",
    "inbound",
    "issues",
    "lanes",
    "lifecycle",
    "merges",
    "metrics",
    "notify",
    "opencode",
    "plan",
    "problems",
    "process",
    "protocol",
    "recaps",
    "reclaim",
    "recommend",
    "retries",
    "roster",
    "state",
    "store",
    "supervision",
    "tables",
    "terminal",
    "views",
    "watch",
    "worktrees",
)
MOVED_CALLABLES = {
    "copilot": (
        "bridge_hook",
        "configure_copilot",
        "owned_hook",
        "release_copilot",
    ),
    "forge": ("report_comment",),
    "issues": ("exact_claim", "held_claim", "merge_claim"),
    "merges": (
        "attributed_commits",
        "group_lanes",
        "group_refusal",
        "lane_dependencies",
        "lane_refusals",
        "lane_session",
        "merge_branch",
        "merge_preview",
        "outside_prerequisites",
        "ready_lanes",
        "reported_ready",
        "session_busy",
        "unattempted",
    ),
    "retries": ("operator_key",),
    "worktrees": (
        "drift",
        "git",
        "has_branch",
        "initialize_lane",
        "launch_line",
        "launched",
        "preparation",
        "preserve_pending",
        "verify_base",
    ),
}
MOVED_CONSTANTS = {"GIT_SECONDS": "worktrees"}
DEFERRED_STANDARD_MODULES = (
    "argparse",
    "contextlib",
    "datetime",
    "hashlib",
    "json",
    "secrets",
    "shlex",
    "shutil",
    "socket",
    "sqlite3",
    "string",
    "subprocess",
    "textwrap",
    "uuid",
)
DEFERRED_ALIASES = {"watch": "stream"}


def deferred_module(qualified: str) -> ModuleType:
    """Binds one module without executing it yet.

    A launcher process runs one command, and no command touches more than a
    few of these modules, so importing all of them before the command is even
    parsed is the largest fixed cost the command line pays. The returned
    module is a real module object that executes on its first attribute
    access, which keeps every call site, monkeypatch and `from` import that
    already names it working unchanged.

    Args:
        qualified: Fully qualified module name to bind.

    Returns:
        The submodule, already loaded if something else loaded it first, and
        otherwise a module that loads itself when first read.
    """
    import importlib.util

    loaded = sys.modules.get(qualified)
    if loaded is not None:
        return loaded
    spec = importlib.util.find_spec(qualified)
    if spec is None or spec.loader is None:
        raise BridgeError(f"Missing module {qualified}")
    spec.loader = importlib.util.LazyLoader(spec.loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualified] = module
    package_name, _, attribute = qualified.rpartition(".")
    package = sys.modules.get(package_name)
    if package is not None:
        setattr(package, attribute, module)
    spec.loader.exec_module(module)
    return module


def deferred(name: str) -> ModuleType:
    """Binds one coordination module without executing it yet."""
    return deferred_module(f"agent_parley.{name}")


class _DeferredCallable:
    """Calls one attribute of a deferred coordination module."""

    def __init__(self, module: ModuleType, attribute: str) -> None:
        """Records the deferred module and attribute name."""
        self.module = module
        self.attribute = attribute

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """Loads and calls the recorded attribute."""
        return getattr(self.module, self.attribute)(*args, **kwargs)


if not TYPE_CHECKING:
    for _deferred_name in DEFERRED_STANDARD_MODULES:
        globals()[_deferred_name] = deferred_module(_deferred_name)
    for _deferred_name in DEFERRED_MODULES:
        globals()[DEFERRED_ALIASES.get(_deferred_name, _deferred_name)] = (
            deferred(_deferred_name)
        )
    activity = _DeferredCallable(checkpoints, "activity")
    branch_head = _DeferredCallable(checkpoints, "branch_head")
    current_branch = _DeferredCallable(checkpoints, "current_branch")
    lane_branch = _DeferredCallable(checkpoints, "lane_branch")
    mailbox = _DeferredCallable(checkpoints, "mailbox")
    participant_liveness = _DeferredCallable(
        checkpoints, "participant_liveness"
    )
    read_events = _DeferredCallable(checkpoints, "read_events")
    change_attempt = _DeferredCallable(issues, "attempt")
    change = _DeferredCallable(issues, "change")
    deadline_state = _DeferredCallable(issues, "deadline_state")
    describe = _DeferredCallable(issues, "describe")
    handoff_fields = _DeferredCallable(issues, "handoff_fields")
    offer_state = _DeferredCallable(issues, "offer_state")
    orphan_age = _DeferredCallable(issues, "orphan_age")
    parse_issue = _DeferredCallable(issues, "parse_issue")
    snapshot = _DeferredCallable(issues, "snapshot")
    lock = _DeferredCallable(state, "lock")
    write_json = _DeferredCallable(state, "write_json")
    write_text = _DeferredCallable(state, "write_text")
    for _deferred_name, _moved in MOVED_CALLABLES.items():
        for _moved_name in _moved:
            globals()[_moved_name] = _DeferredCallable(
                globals()[_deferred_name], _moved_name
            )


def __getattr__(name: str) -> Any:
    """Reads a constant that moved to a command module on first use.

    Args:
        name: Attribute requested from this module.

    Returns:
        The constant from the module that now owns it.

    Raises:
        AttributeError: If no moved constant has that name.
    """
    owner = MOVED_CONSTANTS.get(name)
    if owner is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(deferred(owner), name)


MAX_OVERLAPS = 5
CHANGE_TYPE = frozenset(
    {
        "bug",
        "enhancement",
        "documentation",
        "dependencies",
        "ci",
        "security",
        "performance",
        "release",
    }
)
JSON_HELP = (
    "Print one JSON document on standard output instead of the table. "
    "Field names are documented in docs/operations.md."
)
REPO_HELP = (
    "Repository root or any of its worktrees; defaults to the current "
    "directory."
)
RETRY_HELP = (
    "Idempotency key. Retry a failed command with the key it first used and "
    "the repeat returns the first result without applying the change again. "
    "The same key with different arguments is refused."
)
COPILOT_EVENTS = frozenset(
    {
        "SessionStart",
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
        "PermissionRequest",
        "Stop",
        "SessionEnd",
    }
)


MAX_HEALTH_BYTES = 65536
START_SECONDS = 25.0
LAUNCH_LOCK_SECONDS = 10.0


def duration(text: str) -> float:
    """Converts a compact retention or reporting window into seconds.

    Args:
        text: A count followed by ``s``, ``m``, ``h`` or ``d``. A bare count
            is read as seconds.

    Returns:
        The window in seconds.

    Raises:
        ValueError: If the text does not name a positive window.
    """
    scale = {"s": 1, "m": 60, "h": 3600, "d": 86400}.get(text[-1:], 0)
    try:
        seconds = float(text[:-1] if scale else text) * (scale or 1)
    except ValueError:
        seconds = 0.0
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError(f"{text!r} is not a window; use 45m, 6h or 7d.")
    return seconds


def clock(text: str) -> float:
    """Converts a wall-clock time of day into the next instant it names.

    The time of day is read in the timezone of the machine the operator types
    on, and the resulting instant is recorded absolutely. A later timezone
    change, a daylight-saving transition or a service restart therefore moves
    nothing: the item keeps the instant it was recorded for. The offset is
    the one the zone has on the target day, so a target past a
    daylight-saving transition lands on the wall-clock time typed. A time
    that carries its own offset, such as ``15:00+02:00``, is read in that
    offset instead.

    Args:
        text: A 24-hour time of day such as ``15:00``, optionally with a
            UTC offset.

    Returns:
        The next instant matching that time of day, today while it is still
        ahead and tomorrow once it has passed.

    Raises:
        ValueError: If the text does not name a time of day.
    """
    try:
        moment = datetime.time.fromisoformat(text)
    except ValueError:
        raise ValueError(f"{text!r} is not a time of day; use 15:00.") from None
    now = datetime.datetime.now(moment.tzinfo)
    target = datetime.datetime.combine(now.date(), moment)
    if target <= now:
        tomorrow = now.date() + datetime.timedelta(days=1)
        target = datetime.datetime.combine(tomorrow, moment)
    return target.timestamp()


def instant(text: str) -> float:
    """Converts the start of a reporting window into a Unix time.

    Args:
        text: An ISO 8601 instant such as ``2026-10-15T09:01:00Z``, read in
            the machine's timezone when it carries no offset, or a window
            such as ``24h`` or ``7d`` counted back from now.

    Returns:
        The instant the window starts.

    Raises:
        ValueError: If the text is neither an instant nor a window.
    """
    try:
        return datetime.datetime.fromisoformat(text).timestamp()
    except ValueError:
        pass
    try:
        return time.time() - duration(text)
    except ValueError:
        raise ValueError(
            f"{text!r} is not an instant or a window; use "
            "2026-10-15T09:01:00Z or 7d."
        ) from None


def repeat_plan(
    after: float | None,
    at: float | None,
    every: float | None,
    until: float | None,
) -> tuple[float | None, int]:
    """Resolves the first delivery instant and how many deliveries follow.

    Args:
        after: Delay in seconds before the first delivery, or None.
        at: Absolute instant of the first delivery, or None.
        every: Repeat interval in seconds, or None for a single delivery.
        until: Absolute instant after which the repeat stops, or None.

    Returns:
        The first delivery instant, or None when nothing waits on a clock, and
        the number of deliveries the repeat is bounded to.

    Raises:
        BridgeError: If the repeat is unbounded or ends before it starts.
    """
    first = (
        at
        if at is not None
        else (time.time() + after if after is not None else None)
    )
    if every is None:
        if until is not None:
            raise BridgeError("--until bounds a repeat; add --every.")
        return first, 1
    if until is None:
        raise BridgeError(
            "A repeat must be bounded; add --until, such as --until 18:00."
        )
    first = first if first is not None else time.time() + every
    if until <= first:
        raise BridgeError("--until must fall after the first delivery.")
    return first, min(int((until - first) // every) + 1, store.MAX_REPEATS)


def assignment(result: dict) -> str:
    """Says which of the two operator assignment paths was recorded.

    Args:
        result: Outcome of one issue assignment or withdrawal.

    Returns:
        One line naming what was recorded and the identifier the answering
        lane quotes, with a second line when the request's notice did not
        reach the owner's inbox.
    """
    issue = result["issue"]
    owner = result["owner"]
    if result["recorded"] == "withdrawal":
        return f"Withdrew the operator offer on issue #{issue}."
    if result["recorded"] == "offer":
        return (
            f"Offered issue #{issue} to {result['to']}; offer "
            f"{result['offer_id']}. Ownership moves when {result['to']} "
            "accepts it."
        )
    line = (
        f"Asked {owner} to hand issue #{issue} to {result['to']}; offer "
        f"{result['offer_id']}. {owner} still owns it."
    )
    if not result["delivered"]:
        line += (
            f"\nThe notice did not reach {owner}: {result['detail']} "
            "The request stands and is listed by issue list."
        )
    return line


class Selection(NamedTuple):
    """Narrows a status reading to the lanes an operator asked about.

    Every field is a filter, and filters combine: a lane is reported only
    when it satisfies all of them. The same selection narrows the table and
    the machine-readable document, so a script and an operator never disagree
    about which lanes matched.

    Attributes:
        participant: Single participant to report in full detail.
        project: Repository root to report, as a path or as recorded.
        providers: Providers to report; every provider when empty.
        outcome: Reported outcome to report, such as ``ready``.
        drifted: Report only lanes away from their assigned branch.
        pending: Report only lanes holding unread mail, unanswered
            acknowledgements, an offer, or a reservation past its declared
            time to live.
        idle: Report only live lanes that served no coordination call inside
            the window.
        since: Seconds of coordination inactivity `idle` requires; the
            project's configured interval when zero.
        over_budget: Report only lanes over any of their advisory token,
            call or hour limits.
        issue: Issue number a lane must hold or be offered.
        workspace: Recorded project roots of a named workspace; every
            project when empty.
    """

    participant: str = ""
    project: str = ""
    providers: tuple[str, ...] = ()
    outcome: str = ""
    drifted: bool = False
    pending: bool = False
    idle: bool = False
    since: float = 0.0
    over_budget: bool = False
    issue: int = 0
    workspace: tuple[str, ...] = ()

    def filtered(self) -> bool:
        """Reports whether the operator narrowed the reading at all."""
        return bool(
            self.participant
            or self.project
            or self.providers
            or self.outcome
            or self.drifted
            or self.pending
            or self.idle
            or self.over_budget
            or self.issue
        )

    def describe(self) -> str:
        """Names the filters that were applied, for an empty result."""
        applied = []
        if self.participant:
            applied.append(f"participant {self.participant}")
        if self.project:
            applied.append(f"--project {self.project}")
        applied.extend(f"--provider {name}" for name in self.providers)
        if self.outcome:
            applied.append(f"--outcome {self.outcome}")
        if self.drifted:
            applied.append("--drifted")
        if self.pending:
            applied.append("--pending")
        if self.idle:
            applied.append("--idle")
        if self.over_budget:
            applied.append("--over-budget")
        if self.issue:
            applied.append(f"--issue {self.issue}")
        return " ".join(applied)

    def holds_project(self, root: str) -> bool:
        """Reports whether one project root satisfies the project filters."""
        if self.workspace and root not in self.workspace:
            return False
        if not self.project:
            return True
        return root in (
            self.project,
            str(Path(self.project).expanduser().resolve()),
        )

    def holds(self, record: dict, offers: tuple[int, ...]) -> bool:
        """Reports whether one lane satisfies every applied filter.

        Args:
            record: One lane record from the status reading.
            offers: Issue numbers offered to this lane and still pending.

        Returns:
            Whether the lane is reported. A mailbox that could not be read
            answers no pending work rather than inventing a count.
        """
        mail = record["mail"] or {}
        held = [claim["issue"] for claim in record["claims"]]
        waiting = bool(
            mail.get("unread")
            or mail.get("pending_ack")
            or mail.get("stale_reservations")
            or offers
        )
        return (
            (not self.participant or record["participant"] == self.participant)
            and (not self.providers or record["provider"] in self.providers)
            and (not self.outcome or record["outcome"] == self.outcome)
            and (not self.drifted or record["drift"])
            and (not self.pending or waiting)
            and (not self.idle or self._inactive(record))
            and (
                not self.over_budget
                or (record.get("budget") or {}).get("over", False)
            )
            and (not self.issue or self.issue in held or self.issue in offers)
        )

    def _inactive(self, record: dict) -> bool:
        """Reports whether a live lane served no call inside the window."""
        if not record["availability"]["process_alive"]:
            return False
        if self.since:
            return record["idle"]["served_age_seconds"] >= self.since
        return record["idle"]["stalled"]


def pending_offers(project: dict, agent: str) -> tuple[int, ...]:
    """Reports the issues offered to one participant and still unanswered.

    Args:
        project: One project record from the status reading.
        agent: Participant the offers are addressed to.

    Returns:
        Issue numbers in ledger order. An offer is a request: it moves no
        ownership until the participant accepts it.
    """
    return tuple(
        record["issue"]
        for record in project["issues"]
        if record["offer"] and record["offer"]["to"] == agent
    )


def narrow(report: dict, selection: Selection) -> dict:
    """Applies a selection to a status reading without changing its shape.

    Args:
        report: Reading produced by `Bridge.status_snapshot`.
        selection: Filters the operator asked for.

    Returns:
        The same document with each project holding only the lanes that
        matched, and without the projects the project filter excluded. Every
        other field is carried through, so the machine-readable contract is
        the unfiltered one with fewer rows.
    """
    projects = []
    for project in report["projects"]:
        if not selection.holds_project(project["root"]):
            continue
        projects.append(
            {
                **project,
                "participants": [
                    record
                    for record in project["participants"]
                    if selection.holds(
                        record, pending_offers(project, record["participant"])
                    )
                ],
            }
        )
    return {**report, "projects": projects}


def reviewed_line(review: dict) -> str:
    """States one peer verdict as the claim it is.

    Args:
        review: Verdict record a peer wrote against a lane's report.

    Returns:
        One line naming the verdict, the peer that recorded it and the report
        it judges, and saying that a verdict is that peer's own claim rather
        than independent verification.
    """
    return (
        f"Peer review: {review.get('verdict', '')} by "
        f"{review.get('reviewer', '')} on report "
        f"{review.get('report_id', '')}; a verdict is the reviewing lane's "
        "own claim about work it did not do, not independent verification."
    )


def supervision_failure(error: dict) -> str:
    """States that the project's supervision poll is failing, and since when.

    Args:
        error: Failure record read beside the project's issue ledger.

    Returns:
        One line naming how long polls have been failing and the last failure,
        so a supervisor that fails on every tick never reads as healthy.
    """
    since = max(int(time.time() - error["since"]), 0)
    return f"Supervision: failing for {since}s; last: {error['detail']}"


def supervision_liveness(polled: dict) -> str:
    """States when supervision last polled the project and how long it took.

    A supervision thread that died leaves this reading ageing, so the line
    distinguishes a supervisor that stopped from one that is merely quiet.

    Args:
        polled: Poll record written beside the project's issue ledger.

    Returns:
        One line naming the age and wall time of the last poll, its slowest
        stage, and the age of the last poll in which no step failed.
    """
    now = time.time()
    line = (
        f"Supervision: last poll {max(int(now - polled['at']), 0)}s ago "
        f"in {float(polled.get('seconds', 0)):.2f}s"
    )
    stages = polled.get("stages")
    if isinstance(stages, dict) and stages:
        slowest = max(stages, key=lambda label: float(stages[label]))
        line += f", slowest {slowest} {float(stages[slowest]):.2f}s"
    clean = polled.get("clean_at")
    if clean is None:
        return line + "; no clean poll recorded"
    if clean != polled["at"]:
        return line + f"; last clean {max(int(now - clean), 0)}s ago"
    return line


def wake_schedule(wake: dict) -> str:
    """States when a parked lane is asked again, or why it is not.

    Args:
        wake: Wake reading from a participant record.

    Returns:
        One line naming the next attempt and the cause that postponed it, or
        the exhausted budget with the last cause. A record written before the
        schedule existed reports that it is still to be re-decided rather than
        inventing a time.
    """
    cause = wake.get("blocked") or wake.get("result") or "unknown"
    seconds = wake.get("next_seconds")
    if wake.get("exhausted"):
        exhausted = f"Wake budget exhausted; last cause: {cause}"
        if seconds is None:
            return exhausted
        return f"{exhausted}; backing off, next wake in {seconds}s"
    if seconds is None:
        return "Next wake: due on the next re-evaluation"
    blocked = f"; blocked: {wake['blocked']}" if wake.get("blocked") else ""
    return f"Next wake in {seconds}s at {wake['next_at']}{blocked}"


def review_fields(review: dict | None) -> dict | None:
    """Reports one peer verdict in the shape every snapshot carries it.

    Args:
        review: Verdict record a peer wrote, or None when none is recorded.

    Returns:
        The verdict, its reviewer, the report it judges, its evidence and its
        age, with ``independent_verification`` false so a machine reader
        carries the same limit the printed views state. None when no peer
        recorded a verdict.
    """
    if not review:
        return None
    at = float(review.get("at", 0) or 0)
    return {
        "review_id": review.get("id", ""),
        "report_id": review.get("report_id", ""),
        "reviewer": review.get("reviewer", ""),
        "verdict": review.get("verdict", ""),
        "evidence": review.get("evidence", ""),
        "recorded_at": views.timestamp(at or None),
        "age_seconds": int(time.time() - at) if at else None,
        "independent_verification": False,
    }


def shown_record(record: dict) -> str:
    """Formats one shown message or report for a terminal.

    The record's fields print one per line, then the stored body or
    evidence, then the whole attachment when it was read.

    Args:
        record: Message or report record, optionally carrying
            ``attachment_body``.

    Returns:
        The text to print.
    """
    body = record.get("body_md", record.get("evidence", ""))
    attached = record.get("attachment_body")
    review = record.get("review")
    fields = [
        f"{name}: {value}"
        for name, value in record.items()
        if name not in ("body_md", "evidence", "attachment_body", "review")
    ]
    text = "\n".join(fields) + "\n\n" + str(body)
    if review:
        text += f"\n\n{reviewed_line(review)}\n{review.get('evidence', '')}"
    if attached is not None:
        text += f"\n\n--- attachment {record.get('attachment')} ---\n{attached}"
    return text


CLIPPED_HINT = (
    "Lines ending in … were clipped to fit; rerun with --json for full values."
)
DETAIL_LABELS = {
    "home_env": "Config home variable",
    "config_home": "Config home",
    "env": "Environment",
    "require_env": "Required from shell",
    "unavailable_hooks": "Hooks unavailable",
}


def fitted(lines: Sequence[str], width: int | None) -> str:
    """Joins output lines, clipping each to the terminal it prints on.

    Args:
        lines: Lines to print, each already free of line breaks.
        width: Columns available, or None for a pipe or a file, which
            receives every value in full.

    Returns:
        The text to print, ending in a hint naming ``--json`` whenever a line
        was clipped, so a shortened value never reads as complete.
    """
    if width is None or all(len(line) <= width for line in lines):
        return "\n".join(lines)
    shown = [tables.fit(line, width).rstrip() for line in lines]
    return "\n".join([*shown, CLIPPED_HINT])


def detail_lines(record: dict) -> list[str]:
    """Formats one definition as labeled fields, identity first.

    Args:
        record: Provider or credential document, already redacted.

    Returns:
        One ``Label: value`` line per field, with lists and mappings joined
        on one line and an empty value reported as ``none``.
    """
    lines = []
    for name, value in record.items():
        if isinstance(value, dict):
            joined = ", ".join(f"{key}={item}" for key, item in value.items())
        elif isinstance(value, list):
            joined = ", ".join(str(item) for item in value)
        else:
            joined = "" if value is None else str(value)
        label = DETAIL_LABELS.get(name, name.replace("_", " ").capitalize())
        lines.append(f"{label}: {' '.join(joined.split()) or 'none'}")
    return lines


def decision_lines(page: dict) -> list[str]:
    """Formats one page of the decision log as compact rows.

    Args:
        page: Decision page as ``Bridge.decisions`` reports it.

    Returns:
        A heading line and a body preview per decision, newest first, or one
        sentence saying no decision matched.
    """
    if not page["messages"]:
        scope = f" matching {page['query']!r}" if page["query"] else ""
        if page["since_seconds"]:
            scope += f" in the last {tables.age(page['since_seconds'])}"
        return [f"No decisions recorded{scope}."]
    lines = []
    for row in page["messages"]:
        subject = " ".join(str(row["subject"]).split())
        lines.append(
            f"Decision {row['id']}  {row['created_ts']}  "
            f"{row['sender']}  {subject}"
        )
        lines.append("  " + " ".join(str(row["body_md"]).split()))
    if page["has_more"]:
        lines.append("More decisions match; raise --limit or narrow the query.")
    return lines


def reported_lanes(report: dict) -> int:
    """Counts the lanes a narrowed status reading still holds."""
    return sum(len(project["participants"]) for project in report["projects"])


def recap_lines(
    directory: Path, name: str, now: float | None = None
) -> list[str]:
    """Renders the stored turn recap of one lane for its detail reading.

    Args:
        directory: Private project state directory.
        name: Participant that owns the lane.
        now: Unix time to age the recap against, or None for the clock.

    Returns:
        Indented lines naming the recap's age and its content, or no lines
        when the lane has no stored recap.
    """
    recap = recaps.lane_recap(directory, name)
    text = recaps.format_recap(recap, recaps.MAX_RECAP_BYTES)
    if not recap or not text:
        return []
    moment = time.time() if now is None else now
    age = max(int(moment - float(recap.get("at", moment))), 0)
    return [f"    Recap taken {age}s ago:"] + [
        f"      {line}" for line in text.splitlines()
    ]


PLAN_MARKS = {"completed": "[x]", "in_progress": "[>]", "pending": "[ ]"}


def plan_lines(plan: dict | None) -> list[str]:
    """Lists a lane's recorded task plan with completed steps checked.

    Args:
        plan: Plan as `lanes.lane_task_plan` reads it, or None.

    Returns:
        The indented lines to print under the lane's detail; none when the
        lane recorded no plan.
    """
    if plan is None:
        return []
    lines = [f"    Task plan (from {plan['source']}):"]
    lines += [
        f"      {PLAN_MARKS[item['status']]} {item['text']}"
        for item in plan["items"]
    ]
    if plan["truncated"]:
        lines.append("      (clipped to the plan cap)")
    return lines


def lane_detail(record: dict, data: dict) -> None:
    """Prints one lane's full reading under its table row.

    Args:
        record: One lane record from the status reading.
        data: Project manifest holding the participant.
    """
    agent = record["participant"]
    account = record["credential"] or "default account"
    print(
        f"  {agent} ({record['identity']}): {record['session']}\n"
        f"    Provider: {record['provider']}; {account}"
    )
    print(
        f"    Availability: {record['availability']['state']}; "
        "session process alive: "
        f"{record['availability']['process_alive']}"
    )
    if record["drift"]:
        print(
            "    " + drift(agent, data["participants"][agent], record["branch"])
        )
    if timing := launch_line(data["participants"][agent].get("launch")):
        print(f"    {timing}")
    if record["idle"]["stalled"]:
        print(f"    {record['idle']['marker']}")
    if record["budget"]["marker"]:
        print(f"    {record['budget']['marker']}")
    print(
        "    Observed coordination inactivity: "
        f"{record['idle_seconds']}s"
        + ("" if record["idle_complete"] else " (incomplete)")
    )
    for wait in record["waiting"]:
        item = wait.get("message_id") or wait.get("issue") or ""
        print(
            f"    Waiting {wait['seconds']}s: {wait['kind']}"
            + (f" {item}" if item else "")
        )
    for claim in record["claims"]:
        if claim.get("orphaned"):
            held = claim.get("orphan_reservations") or []
            recorded = claim.get("orphan_recorded_seconds") or 0
            print(
                f"    Issue #{claim['issue']} was marked orphaned "
                f"{recorded}s ago: {claim['orphan_reason']}; still owned "
                f"until a peer runs issue claim {claim['issue']} "
                "--take-orphaned"
                + (f"; holds {', '.join(held)}" if held else "")
            )
        if claim.get("unresolved"):
            print(
                f"    Issue #{claim['issue']} has an unresolved completion: "
                f"{claim.get('reason', '')}; still owned until the operator "
                f"runs issue resolve {claim['issue']}"
            )
        if claim["overdue"]:
            print(
                f"    Issue #{claim['issue']} is overdue by "
                f"{claim['overdue_seconds']}s and still owned."
            )
        if claim["budget"]:
            print(
                f"    Issue #{claim['issue']} attempts "
                f"{claim['attempts']}/{claim['budget']}"
                + (
                    "; budget exceeded and still owned"
                    if claim["budget_exceeded"]
                    else ""
                )
            )
        if claim.get("blueprint"):
            print(f"    Blueprint {claim['blueprint']}")
    print(f"    Reported outcome: {record['outcome']}")
    approval = record["approval"] or {}
    if approval.get("state", approvals.UNREPORTED) != approvals.UNREPORTED:
        detail = approval["detail"]
        print(
            f"    Approval: {approval['state']}"
            + (f"; {detail}" if detail else "")
        )
    print(
        f"    Context delivered: {record['injected_bytes']} "
        f"UTF-8 bytes in {record['injections']} notices; "
        f"{record.get('injected_per_hour', 0)} bytes per hour over the "
        "last day"
    )
    print(
        f"    Context window: {window_cell(record)}; diff "
        f"{diff_cell(record)}; runs at {location_cell(record)}"
    )
    if paths := record.get("diff_paths"):
        print(f"    Changed files: {', '.join(paths)}")
    if record["report_age_seconds"] is not None:
        print(f"    Report age: {record['report_age_seconds']}s")
    if record["summary"]:
        print(f"    Summary: {record['summary']}")
    if record["remaining"]:
        print(f"    Remaining: {record['remaining']}")
    if record["evidence"]:
        print(f"    Reported verification: {record['evidence']}")
    if review := record["review"]:
        print(f"    {reviewed_line(review)}")
        if review["evidence"]:
            print(f"    Peer review evidence: {review['evidence']}")
    if wake := record["wake"]:
        print(
            f"    Runtime wake: {wake['result']}; "
            f"attempt {wake['attempts']}"
            + (f"/{wake['budget']}" if wake.get("budget") else "")
            + f"; {wake['age_seconds']}s ago"
        )
        print(f"    {wake_schedule(wake)}")
    if foreign := record.get("foreign_session"):
        print(
            f"    Second session: {foreign['session_id'] or 'unnamed'} "
            f"(pid {foreign['pid']}) sends hooks as this lane; "
            "its events are ignored"
        )
    mail = record["mail"] or {}
    if "error" in mail:
        print(f"    Coordination unavailable: {mail['error']}")
        return
    stale = mail["stale_reservations"]
    age = mail.get("stale_reservation_age", 0)
    print(
        f"    Unread: {mail['unread']}"
        + (
            f" ({mail['superseded']} superseded)"
            if mail.get("superseded")
            else ""
        )
        + f"; pending acknowledgements: {mail['pending_ack']}; "
        + f"active reservations: {mail['reservations'] - stale}"
        + (
            f"; expired: {stale}, oldest {age}s past its deadline"
            if stale
            else ""
        )
    )
    if edited := record["operator_edits"]:
        print("    " + supervision.operator_edit_marker(edited))
    if record["base_advance_paths"]:
        print("    " + record["base_advance"])
    if mail["named_resources"]:
        print("    Named resources held: " + ", ".join(mail["named_resources"]))
    if topics := mail.get("unread_topics"):
        print(
            "    Unread by topic: "
            + ", ".join(f"{topic} {count}" for topic, count in topics.items())
        )
    if mail.get("queued_requests"):
        print(
            "    Reservation requests queued on its keys: "
            f"{mail['queued_requests']}"
            + (
                " (" + ", ".join(mail["queued_by"]) + ")"
                if mail.get("queued_by")
                else ""
            )
        )
    if mail.get("refused"):
        print("    Refused a key it holds: " + ", ".join(mail["refused"]))
    print(f"    Last coordination: {mail['last_coordination_at']}")
    for pending in mail["outstanding_ack"]:
        print(
            "    Awaiting acknowledgement: "
            f"message {pending['message_id']} "
            f"from {pending['sender']}; "
            f"{pending['age_seconds']}s"
        )
    print(f"    Latest prompt/task (reported): {mail['task']}")
    if mail["awaiting_delivery"]:
        print(
            "    Awaiting checkpoint delivery: "
            f"{mail['awaiting_delivery']} "
            "(batch capped at 3)"
        )


def terminal_width() -> int | None:
    """Reports the columns a table may use on this stream.

    Returns:
        The width of the attached terminal, or None when standard output is
        a file or a pipe, which receives every column instead of a table
        shaped for a terminal that is not there.
    """
    if not sys.stdout.isatty():
        return None
    return max(1, shutil.get_terminal_size().columns)


def inbound_status(home: Path | None = None) -> dict:
    """Describes inbound status without loading its transport when disabled.

    Args:
        home: Private state root whose stored notification settings apply,
            or None to read the process environment alone.

    Returns:
        Whether the reader was asked for, the fault, if any, stopping it,
        and the Unix time a passcode lock ends, zero when none holds.
    """
    values = notify.environment(home)
    if not values.get("AGENT_PARLEY_INBOUND", "").strip():
        return {"enabled": False, "fault": "", "locked_until": 0.0}
    return inbound.reported(values, home)


def notification_line(outbound: dict, received: dict) -> str:
    """Says whether outbound and inbound notification are active, and why not.

    Args:
        outbound: Reading from `notify.reported`.
        received: Reading from `inbound_status`.

    Returns:
        One ``Notify:`` line for status and ``up``.
    """
    if outbound["fault"]:
        sent = f"outbound off: {outbound['fault']}"
    else:
        sent = "outbound on (" + ", ".join(outbound["transports"]) + ")"
    if received["fault"]:
        read = f"inbound off: {received['fault']}"
    elif received["enabled"] and received.get("locked_until"):
        moment = time.strftime(
            "%Y-%m-%d %H:%M UTC", time.gmtime(received["locked_until"])
        )
        read = f"inbound locked until {moment} after wrong passcodes"
    elif received["enabled"]:
        read = "inbound on"
    else:
        read = "inbound off: AGENT_PARLEY_INBOUND is not set"
    return f"Notify: {sent}; {read}"


def add_selector(
    command: argparse.ArgumentParser, *, everything: bool = True
) -> None:
    """Adds the shared lane selector and its one confirmation to a command.

    The filters read the same lane facts `status` reports, and a lane matches
    when every given filter holds, so they narrow rather than widen.

    Args:
        command: Subcommand that otherwise acts on one named participant.
        everything: Whether to add ``--all``. A command that already declares
            it in a scope group of its own passes False.
    """
    selection = command.add_argument_group(
        "lane selection",
        "Act on several lanes instead of one. The command prints the lanes "
        "it matched, asks once for the whole set, and reports each lane.",
    )
    if everything:
        selection.add_argument(
            "--all",
            action="store_true",
            help="Select every lane the other filters leave.",
        )
    selection.add_argument(
        "--provider",
        default="",
        metavar="NAME",
        help="Select only the lanes a named provider drives.",
    )
    selection.add_argument(
        "--outcome",
        default="",
        metavar="STATE",
        help=(
            "Select only the lanes whose own latest report is this state. A "
            "reported state is the lane's account, never a review."
        ),
    )
    selection.add_argument(
        "--drifted",
        action="store_true",
        help="Select only the lanes sitting off the branch they were given.",
    )
    selection.add_argument(
        "--idle",
        action="store_true",
        help="Select only the lanes supervision currently reads as stalled.",
    )
    selection.add_argument(
        "--over-budget",
        action="store_true",
        help=(
            "Select only the lanes over any of their advisory token, call "
            "or hour limits."
        ),
    )
    selection.add_argument(
        "--yes",
        action="store_true",
        help="Skip the single confirmation covering the whole selected set.",
    )


def add_status_filters(command: argparse.ArgumentParser) -> None:
    """Adds the participant and every filter a status reading accepts.

    The declaration lives here rather than inside the command-line builder so
    that a second reader of the same reading, such as the inbound Telegram
    query, parses the identical set of filters and cannot drift from what the
    command line accepts.

    Args:
        command: Parser that reports status, whether it is the `status`
            subcommand or a reader built for one query.
    """
    command.add_argument(
        "participant",
        nargs="?",
        default="",
        help=(
            "Report this participant alone, as the whole reading rather than "
            "one table row."
        ),
    )
    command.add_argument(
        "--repo",
        dest="project",
        metavar="ROOT",
        default="",
        help="Report only the project at this repository root.",
    )
    command.add_argument(
        "--project",
        dest="project",
        metavar="ROOT",
        default="",
        help=argparse.SUPPRESS,
    )
    command.add_argument(
        "--provider",
        action="append",
        metavar="NAME",
        help=(
            "Report only participants driven by this provider. Repeat the "
            "flag to report several."
        ),
    )
    command.add_argument(
        "--outcome",
        choices=("ready", "blocked", "unknown"),
        default="",
        help="Report only lanes that reported this outcome.",
    )
    command.add_argument(
        "--drifted",
        action="store_true",
        help=(
            "Report only lanes away from their assigned branch. The command "
            "exits non-zero when one matches."
        ),
    )
    command.add_argument(
        "--pending",
        action="store_true",
        help=(
            "Report only lanes holding unread mail, unanswered "
            "acknowledgements, an offer, or a reservation past its declared "
            "time to live. The command exits non-zero when one matches."
        ),
    )
    command.add_argument(
        "--idle",
        action="store_true",
        help=(
            "Report only live lanes that served no coordination call inside "
            "the window. This measures coordination inactivity, not what a "
            "native client was doing inside a turn."
        ),
    )
    command.add_argument(
        "--since",
        type=duration,
        default=0.0,
        metavar="WINDOW",
        help=(
            "Inactivity an idle lane must show, such as 45m, 6h or 7d. The "
            "project's configured interval decides by default."
        ),
    )
    command.add_argument(
        "--over-budget",
        action="store_true",
        help=(
            "Report only lanes over any of their advisory token, call or "
            "hour limits. A budget informs and does not gate; the command "
            "exits non-zero when one matches."
        ),
    )
    command.add_argument(
        "--issue",
        type=int,
        default=0,
        metavar="N",
        help="Report only lanes holding or offered this issue.",
    )


def selected_status(args: argparse.Namespace) -> Selection:
    """Reads one parsed status command line as a lane selection.

    Args:
        args: Namespace produced by a parser `add_status_filters` built.

    Returns:
        The filters that command line asked for.
    """
    return Selection(
        participant=args.participant,
        project=args.project,
        providers=tuple(args.provider or ()),
        outcome=args.outcome,
        drifted=args.drifted,
        pending=args.pending,
        idle=args.idle,
        since=args.since,
        over_budget=args.over_budget,
        issue=args.issue,
    )


def add_budget_flags(command: argparse.ArgumentParser) -> None:
    """Adds the three advisory limit flags a budget command accepts.

    Args:
        command: Parser for a participant, provider or project budget.
    """
    for field, kind, described in (
        ("tokens", int, "tokens the lane's own client may record"),
        ("calls", int, "coordination calls the lane may be served"),
        ("hours", float, "hours the lane's session may stay alive"),
    ):
        command.add_argument(
            f"--{field}",
            type=kind,
            metavar="N",
            help=(
                f"Advisory limit on the {described}; 0 removes it. Crossing "
                "it marks the lane and sends one notice, and stops nothing."
            ),
        )


def budget_changes(args: argparse.Namespace) -> dict | None:
    """Reads the budget flags, or None when none was given."""
    changes = {field: getattr(args, field) for field in roster.BUDGET_FIELDS}
    if all(value is None for value in changes.values()):
        return None
    return changes


def unmeasured_lanes(
    bridge: Bridge,
    repository: Path,
    args: argparse.Namespace,
    names: list[str] | None = None,
) -> list[str]:
    """Warns about the project lanes a token limit just given cannot count.

    Args:
        bridge: Bridge whose state root defines the providers.
        repository: Repository whose project the limit applies to.
        args: Parsed budget flags; only a positive ``--tokens`` is checked.
        names: Lanes the limit covers; every participant when omitted.

    Returns:
        Sorted active lanes without a token reader, each named on one
        stderr warning line, or an empty list when no positive token limit
        was given.
    """
    from agent_parley import records

    if not getattr(args, "tokens", None):
        return []
    _, directory = bridge.project(repository, create=False)
    manifest = roster.read(directory)
    participants = manifest["participants"]
    lanes = records.unmeasured(bridge.home, manifest, names)
    ended = [
        lane
        for lane in lanes
        if records.counts_at_end(
            bridge.home, records.lane_provider(participants, lane)
        )
    ]
    if lanes:
        print(records.unmeasured_warning(lanes, ended), file=sys.stderr)
    return lanes


def selected(args: argparse.Namespace) -> bool:
    """Reports whether the command line carries any lane selector."""
    return bool(
        args.all
        or args.provider
        or args.outcome
        or args.drifted
        or args.idle
        or getattr(args, "over_budget", False)
    )


def matching_lanes(
    home: Path, directory: Path, data: dict, args: argparse.Namespace
) -> list[str]:
    """Names every participant the command line's lane selector matched.

    The filters read the provider that drives a lane, the lane's own latest
    reported outcome, whether its checkout sits on the branch it was assigned,
    whether supervision currently reads it as stalled and whether it is over
    an advisory budget. `--all` adds no filter of its own, so it selects
    whatever the others leave. A retired lane is never matched: it has no
    session to act on and no worktree whose branch could be compared.

    Args:
        home: Private bridge state root.
        directory: Private state directory for the common repository.
        data: Project manifest holding the roster.
        args: Parsed arguments carrying the selector flags.

    Returns:
        The matched participants in roster order.
    """
    import sqlite3

    stalled_after = supervision.configuration(home, data)["stalled_after"]
    over_budget = getattr(args, "over_budget", False)
    usage = {}
    if over_budget:
        try:
            usage = store.usage(home, data["root"])
        except sqlite3.Error:
            usage = {}
    names = []
    for name in sorted(data["participants"]):
        participant = data["participants"][name]
        if roster.retired(participant):
            continue
        if args.provider and participant["provider"] != args.provider:
            continue
        reported = activity(directory, name).get("outcome", "unknown")
        if args.outcome and reported != args.outcome:
            continue
        branch = lane_branch(Path(participant["lane"]))
        if args.drifted and branch == participant["branch"]:
            continue
        stalled = supervision.stall(home, directory, data, name, stalled_after)
        if args.idle and not stalled["stalled"]:
            continue
        if (
            over_budget
            and not budgets.report(home, directory, data, name, usage)["over"]
        ):
            continue
        names.append(name)
    return names


def selection_plan(
    action: str, names: Sequence[str], notes: Sequence[str] = ()
) -> str:
    """Lists the lanes a selector matched and what the command will do.

    Args:
        action: What happens to each matched lane, in the infinitive.
        names: Matched participants in the order they will be acted on.
        notes: Extra lines reported before the confirmation, such as the
            prerequisites that lie outside the selected set.

    Returns:
        The plan an operator reads before the single confirmation.
    """
    if not names:
        return "Selector matched no lane, so nothing was done."
    plural = "" if len(names) == 1 else "s"
    lines = [f"Plan: {action} {len(names)} lane{plural}."]
    lines += [f"- {name}: {action}" for name in names]
    lines += list(notes)
    return "\n".join(lines)


def confirmed(proposal: str, assume_yes: bool) -> bool:
    """Prints one plan and asks once for the whole selected set.

    Args:
        proposal: The plan the operator reads before answering.
        assume_yes: Whether `--yes` already confirmed the whole set.

    Returns:
        Whether the operator confirmed. A closed or empty answer declines.
    """
    print(proposal)
    if assume_yes:
        return True
    try:
        answer = input("Proceed with these lanes? [y/N]: ")
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")


def ignored_paths(lane: Path) -> list[str]:
    """Names the ignored files and folders removing a worktree deletes.

    Args:
        lane: Existing worktree to inspect.

    Returns:
        Worktree-relative ignored paths, a wholly ignored folder named once
        with its trailing slash.
    """
    listing = git(
        lane,
        "ls-files",
        "--others",
        "--ignored",
        "--exclude-standard",
        "--directory",
    )
    return listing.splitlines()


SHOWN_PATHS = 20


def shown_paths(paths: Sequence[str]) -> list[str]:
    """Bounds a path list an operator reads to its first entries.

    Args:
        paths: Every path the caller acts on.

    Returns:
        The first `SHOWN_PATHS` paths, followed by a count of the rest when
        any were left out.
    """
    shown = list(paths[:SHOWN_PATHS])
    if len(paths) > SHOWN_PATHS:
        shown.append(f"and {len(paths) - SHOWN_PATHS} more")
    return shown


def retired_lane(
    bridge: Bridge, repo: Path, name: str, assume_yes: bool
) -> str:
    """Retires one lane after confirming the ignored files it deletes.

    Args:
        bridge: Launcher holding the private coordination state.
        repo: Repository the command was given.
        name: Participant whose lane is retired.
        assume_yes: Whether `--yes` already confirmed deleting the ignored
            files.

    Returns:
        The account the retirement produced.

    Raises:
        BridgeError: If the operator declines or leaves the confirmation
            unanswered, or if the retirement is refused.
    """
    _, data = lane_roster(bridge, repo)
    participant = data["participants"].get(name)
    lane = Path(participant["lane"]) if participant else None
    ignored = ignored_paths(lane) if lane and lane.exists() else []
    if ignored and not confirmed(
        f"Retiring {name} deletes these ignored files:\n"
        + "\n".join(f"- {path}" for path in shown_paths(ignored)),
        assume_yes,
    ):
        raise BridgeError("Declined: nothing was retired.")
    return bridge.retire(repo, name, discard=ignored)


def retired_ready(bridge: Bridge, repo: Path, assume_yes: bool) -> int:
    """Retires every lane `problems` reports as ready to retire.

    The lanes are the project's `ready to retire` rows, so a lane awaiting
    integration or running a session is never selected. Each lane takes the
    single-lane path, including its own ignored-files question, and a lane
    that refuses or fails is reported while the rest are still attempted.

    Args:
        bridge: Launcher holding the private coordination state.
        repo: Repository the command was given.
        assume_yes: Whether `--yes` already confirmed deleting each lane's
            ignored files.

    Returns:
        0 when every ready lane was retired or none was ready, 1 when any
        lane refused or failed.
    """
    _, data = lane_roster(bridge, repo)
    names = problems.ready(bridge.problems(), data["root"])
    if not names:
        print("No lane is ready to retire, so nothing was done.")
        return 0
    return bulk_lanes(
        "retire",
        names,
        lambda name: retired_lane(bridge, repo, name, assume_yes),
        assume_yes=True,
    )


def bulk_lanes(
    action: str,
    names: Sequence[str],
    step: Callable[[str], str],
    *,
    assume_yes: bool = False,
    notes: Sequence[str] = (),
) -> int:
    """Runs one operator action over a selected set after one confirmation.

    Independent operations continue past a lane that refuses: the refusal is
    printed beside its lane and every remaining lane is still attempted. The
    closing tally names what was done and what refused. Integration does not
    take this path, because an ordered merge run stops at the first refusal
    and leaves the lanes that wait on it unattempted.

    Args:
        action: What happens to each matched lane, in the infinitive.
        names: Matched participants in the order they are acted on.
        step: Runs the action for one lane and returns that lane's account.
        assume_yes: Whether `--yes` already confirmed the whole set.
        notes: Extra lines the plan reports before the confirmation.

    Returns:
        0 when every matched lane was done or when nothing matched; 1 when
        the operator declined or left the confirmation unanswered, or when
        any lane refused or failed.
    """
    proposal = selection_plan(action, names, notes)
    if not names:
        print(proposal)
        return 0
    if not confirmed(proposal, assume_yes):
        print("Declined: nothing was done.")
        return 1
    done: list[str] = []
    refused: list[str] = []
    for name in names:
        try:
            print(f"- {name}: {step(name)}")
            done.append(name)
        except (
            BridgeError,
            OSError,
            ValueError,
            subprocess.TimeoutExpired,
        ) as failure:
            print(f"- {name}: refused. {failure}")
            refused.append(name)
    tally = f"Done {len(done)} of {len(names)}: " + (", ".join(done) or "none")
    if refused:
        tally += f". Refused or failed: {', '.join(refused)}"
    print(f"{tally}.")
    return 1 if refused else 0


def operator_message(delivered: dict, name: str) -> str:
    """Reports what one operator message did for one lane.

    Args:
        delivered: Result of the send, either delivered or recorded.
        name: Lane the message was addressed to.

    Returns:
        The account an operator reads for that lane.
    """
    if "kind" in delivered:
        return (
            f"Operator item {delivered['id']} recorded for {name}; "
            f"{delivered['repeats_left']} delivery(s) pending."
        )
    state = "already delivered" if delivered.get("duplicate") else "delivered"
    return f"Operator message {delivered['id']} {state} to {name}."


def lane_roster(bridge: Bridge, repo: Path) -> tuple[Path, dict]:
    """Reads the private state directory and roster a selector resolves in."""
    _, directory = bridge.project(repo, create=False)
    return directory, roster.read(directory)


def spoken_lanes(bridge: Bridge, repo: Path, args: argparse.Namespace) -> int:
    """Sends one operator message to every lane a selector matched.

    Args:
        bridge: Launcher holding the private coordination state.
        repo: Repository the command was given.
        args: Parsed `say` arguments carrying the selector.

    Returns:
        0 when every matched lane received the message, 1 otherwise.

    Raises:
        BridgeError: If a participant name and a selector are both given, if
            the message text is missing, or if one idempotency key is offered
            for several lanes.
    """
    if args.participant and args.text:
        raise BridgeError(
            "`say` addresses one named lane or a selected set, never both. "
            "Drop the participant name to message a set."
        )
    text = args.text or args.participant
    if not text:
        raise BridgeError("`say` needs the message text to send.")
    if args.key:
        raise BridgeError(
            "--key names one message, so it cannot cover several lanes. The "
            "default key already distinguishes each lane's own copy."
        )
    directory, data = lane_roster(bridge, repo)
    return bulk_lanes(
        "send an operator message to",
        matching_lanes(bridge.home, directory, data, args),
        lambda name: operator_message(
            bridge.say(
                repo,
                name,
                text,
                args.subject,
                "",
                args.ack,
                args.within,
                after=args.after,
                at=args.at,
                when_released=args.when_released,
                unless_reported=args.unless_reported,
                every=args.every,
                until=args.until,
            ),
            name,
        ),
        assume_yes=args.yes,
    )


BULK_PARTICIPANT = {
    "stop": "stop",
    "pause": "pause",
    "resume": "resume",
    "pr": "open a pull request from",
}


def participant_lanes(
    bridge: Bridge, repo: Path, args: argparse.Namespace
) -> int:
    """Runs one participant action over every lane a selector matched.

    Args:
        bridge: Launcher holding the private coordination state.
        repo: Repository the command was given.
        args: Parsed `participant` arguments carrying the selector.

    Returns:
        0 when every matched lane was done, 1 when any refused or failed.

    Raises:
        BridgeError: If a participant name and a selector are both given.
    """
    if args.name:
        raise BridgeError(
            f"`participant {args.action}` acts on one named lane or on a "
            "selected set, never both. Drop the participant name to act on "
            "a set."
        )
    steps: dict[str, Callable[[str], str]] = {
        "stop": lambda name: bridge.stop(repo, name),
        "pause": lambda name: bridge.pause(repo, name),
        "resume": lambda name: bridge.pause(repo, name, resume=True),
        "pr": lambda name: bridge.pull_request(repo, name),
    }
    directory, data = lane_roster(bridge, repo)
    return bulk_lanes(
        BULK_PARTICIPANT[args.action],
        matching_lanes(bridge.home, directory, data, args),
        steps[args.action],
        assume_yes=args.yes,
    )


def assigned_lanes(
    bridge: Bridge, repo: Path, args: argparse.Namespace
) -> list[str]:
    """Resolves a lane selector to the one lane an issue is offered to.

    One issue carries one offer, so a selector stands in for a lane's name
    only while it matches a single lane. A wider match is refused and every
    matched lane is named, because choosing among them is the operator's
    decision and never this command's.

    Args:
        bridge: Launcher holding the private coordination state.
        repo: Repository the command was given.
        args: Parsed `issue assign` arguments carrying the selector.

    Returns:
        The single matched lane, or nothing when the selector matched nobody.

    Raises:
        BridgeError: If a lane name or ``--unassign`` accompanies a selector,
            or if the selector matched more than one lane.
    """
    if args.name:
        raise BridgeError(
            "`issue assign` offers an issue to one named lane or to the one "
            "lane a selector matches, never both."
        )
    if args.unassign:
        raise BridgeError(
            "--unassign withdraws the offer recorded on one issue, so it "
            "takes no lane selector."
        )
    directory, data = lane_roster(bridge, repo)
    names = matching_lanes(bridge.home, directory, data, args)
    if len(names) > 1:
        raise BridgeError(
            f"Selector matched {len(names)} lanes: {', '.join(names)}. One "
            "issue is offered to one lane, so narrow the selector."
        )
    return names


def assigned_selection(
    bridge: Bridge, repo: Path, args: argparse.Namespace
) -> int:
    """Offers one issue to the single lane a selector matched.

    Args:
        bridge: Launcher holding the private coordination state.
        repo: Repository the command was given.
        args: Parsed `issue assign` arguments carrying the selector.

    Returns:
        0 when the offer or request was recorded, 1 when it was refused.
    """
    return bulk_lanes(
        f"offer #{args.number} to",
        assigned_lanes(bridge, repo, args),
        lambda name: assignment(
            bridge.issue_assign(repo, args.number, name, reason=args.reason)
        ),
        assume_yes=args.yes,
    )


def merged_lanes(
    bridge: Bridge, repo: Path, args: argparse.Namespace, preview: bool
) -> str:
    """Runs the merge the command line selected, one lane or a set.

    A selected set is ordered, planned and confirmed once before anything is
    merged. The filters narrow the lanes that report ready, because bulk
    integration only ever considers lanes whose own report is ready.

    Args:
        bridge: Launcher holding the private coordination state.
        repo: Repository the command was given.
        args: Parsed `participant merge` arguments.
        preview: Whether the run only reports what a merge would do.

    Returns:
        The account the selected merge produced.

    Raises:
        BridgeError: If the selection is ambiguous or names nothing, if the
            operator declines or leaves the plan unanswered, or if the
            ordered run stops on a refusal or a failure.
    """
    narrowing = bool(
        args.provider
        or args.outcome
        or args.drifted
        or args.idle
        or getattr(args, "over_budget", False)
    )
    issue = getattr(args, "issue", "")
    if issue and (
        not args.name
        or selected(args)
        or args.group
        or getattr(args, "verify_recovery", False)
    ):
        raise BridgeError(
            "`participant merge --issue` picks a claim of one named lane; "
            "name the lane and drop --all, --group, --verify-recovery and "
            "the selectors."
        )
    if getattr(args, "verify_recovery", False):
        if (
            preview
            or args.name
            or selected(args)
            or args.group
            or getattr(args, "renew_recovery", False)
        ):
            raise BridgeError(
                "`participant merge --verify-recovery` verifies the base "
                "checkout as it stands and names no lane; drop the lane name, "
                "--preview, --renew-recovery, --all, --group and the "
                "selectors."
            )
        return bridge.verify_recovery(repo)
    renew = getattr(args, "renew_recovery", False)
    if renew and (preview or not args.name or selected(args) or args.group):
        raise BridgeError(
            "`participant merge --renew-recovery` retries one named lane's "
            "recorded integration; drop --preview, --all, --group and the "
            "selectors."
        )
    if selected(args) or args.group:
        if args.name:
            raise BridgeError(
                "`participant merge` integrates one named lane, every ready "
                "lane with --all, a selected set, or one group with --group "
                "NAME. Drop the participant name to integrate a set."
            )
        if args.group and narrowing:
            raise BridgeError(
                "`participant merge --group NAME` integrates the whole group, "
                "so it takes no lane selector. Drop --provider, --outcome, "
                "--drifted, --idle and --over-budget, or drop --group."
            )
        names: list[str] | None = None
        if narrowing:
            directory, data = lane_roster(bridge, repo)
            names = matching_lanes(bridge.home, directory, data, args)
            if not names:
                return "Selector matched no lane, so nothing merged."
        if preview:
            return bridge.integrate(
                repo, group=args.group, preview=True, lanes=names
            )
        proposed = bridge.integration_plan(repo, group=args.group, lanes=names)
        if not proposed["sequence"]:
            subject = proposed["subject"]
            return f"{subject}: no lane to integrate, so nothing merged."
        if not confirmed(
            selection_plan(
                "integrate", proposed["sequence"], proposed["outside"]
            ),
            args.yes,
        ):
            raise BridgeError("Declined: nothing was merged.")
        return bridge.integrate(
            repo,
            group=args.group,
            lanes=names,
            confirmed=proposed["sequence"],
        )
    if not args.name:
        raise BridgeError(
            "`participant merge` needs a participant name, --all, a "
            "selector or --group NAME."
        )
    return (
        bridge.preview_merge(repo, args.name)
        if preview
        else bridge.merge(repo, args.name, renew=renew, issue=issue)
    )


def gh(cwd: Path, *args: str) -> str:
    """Runs the operator's GitHub CLI and returns stripped stdout.

    Authentication, host selection and repository permissions stay with the
    native `gh` installation. Agent Parley passes no token, reads no
    credential, and adds no flag that would bypass a repository rule. The
    origin remote selects the repository, matching the forge integration.

    Args:
        cwd: Checkout the command runs in, which selects the repository.
        *args: Individual gh arguments, never shell-expanded.

    Returns:
        Command output with surrounding whitespace removed.

    Raises:
        BridgeError: If gh is not installed or exits unsuccessfully.
        subprocess.TimeoutExpired: If gh exceeds the command timeout.
    """
    executable = shutil.which("gh")
    if executable is None:
        raise BridgeError(
            "Install and sign in to the native gh CLI first; Agent Parley "
            "uses your own GitHub authentication and never stores a token."
        )
    repository = forge.slug(cwd)
    if repository is None:
        raise BridgeError("The origin remote must name a GitHub repository.")
    result = subprocess.run(
        [executable, *args, "--repo", repository],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode:
        raise BridgeError(result.stderr.strip() or "GitHub CLI call failed.")
    return result.stdout.strip()


def hygiene_metadata(
    cwd: Path, issues: list[str], policy: dict | None = None
) -> tuple[list[str], str]:
    """Reads the ownership metadata the claimed issues already carry.

    The repository requires every pull request to declare a change type and
    to match the milestone of the issue it references. Both facts already
    exist on the issue, so they are mirrored rather than invented: a lane
    does not get to classify its own work, and an unclassified issue is
    reported instead of being given a guessed label.

    Args:
        cwd: Checkout the GitHub CLI runs in, which selects the repository.
        issues: Repository issue numbers the lane claims.
        policy: Validated project metadata settings.

    Returns:
        The change-type labels the issues carry, and the single milestone
        title they agree on, or an empty string when none carries one.

    Raises:
        BridgeError: If no claimed issue carries a change-type label, if the
            claimed issues carry conflicting milestones, or if the GitHub CLI
            cannot read an issue.
        subprocess.TimeoutExpired: If gh exceeds the command timeout.
    """
    policy = roster.pull_request_policy(policy or {})
    change_types = set(policy.get("change_type_labels", CHANGE_TYPE))
    labels: set[str] = set()
    milestones: set[str] = set()
    for number in issues:
        record = json.loads(
            gh(cwd, "issue", "view", number, "--json", "labels,milestone")
        )
        labels |= {
            label["name"] for label in record.get("labels") or []
        } & change_types
        if (milestone := record.get("milestone")) and policy.get(
            "milestone", "match"
        ) != "ignore":
            milestones.add(milestone["title"])
        elif policy.get("milestone") == "required":
            raise BridgeError(f"Claimed issue #{number} requires a milestone.")
    if not labels and policy.get("require_label", False):
        raise BridgeError(
            "No claimed issue carries a change-type label, and the pull "
            "request takes its classification from the issue rather than "
            "choosing one. Label "
            + ", ".join(f"#{number}" for number in issues)
            + " with one of: "
            + ", ".join(sorted(change_types))
            + "."
        )
    if len(milestones) > 1:
        raise BridgeError(
            "The claimed issues carry different milestones ("
            + ", ".join(sorted(milestones))
            + "), so one pull request cannot match them all. Split the work "
            "or align the issues first."
        )
    return sorted(labels), milestones.pop() if milestones else ""


def pull_request_body(
    state: dict,
    issues: list[str],
    template: str = "",
    review: dict | None = None,
) -> str:
    """Shapes one lane's recorded report into the repository template.

    The body reproduces what the participant reported and invents nothing
    of its own, so a reviewer reads the lane's own account. It carries the
    three headings of `.github/PULL_REQUEST_TEMPLATE.md` and an explicit
    reference to every issue the lane still claims, which is what the
    repository hygiene gate requires of a pull request.

    A verdict a peer recorded against that report travels with it under the
    verification heading, labelled as the reviewing lane's own claim, so a
    human reader of the pull request is never left treating a peer's check
    as independent verification.

    Args:
        state: Recorded lane activity holding the reported outcome.
        issues: Repository issue numbers the lane claims.
        template: Optional project or repository Markdown template. Supports
            dollar placeholders for summary, evidence, remaining, issues,
            outcome and review.
        review: Latest peer verdict on the lane's report, or None when no
            peer recorded one.

    Returns:
        Markdown for the pull-request body.
    """
    references = " ".join(f"Refs #{number}" for number in issues)
    reviewed = (
        f"{reviewed_line(review)}\n\n{review.get('evidence', '')}".strip()
        if review
        else "No peer recorded a review verdict on this report."
    )
    evidence = (
        state.get("evidence", "").strip()
        or "The lane recorded no verification evidence."
    )
    remaining = (
        state.get("remaining", "").strip()
        or "The lane recorded no remaining work."
    )
    report = (
        "## Problem and result\n\n"
        f"{state['summary'].strip()}\n\n"
        f"Reported state: {state.get('outcome', 'unknown')}. A reported "
        "state is the participant's own account of its lane; it is neither "
        "review nor independent verification.\n\n"
        f"{references}\n\n"
        "## Verification\n\n"
        f"{evidence}\n\n"
        f"{reviewed}\n\n"
        "## Compatibility and risks\n\n"
        f"{remaining}\n"
    )
    if not template:
        return report
    rendered = string.Template(template).safe_substitute(
        summary=state["summary"].strip(),
        evidence=evidence,
        remaining=remaining,
        issues=references,
        outcome=state.get("outcome", "unknown"),
        review=reviewed,
    )
    return rendered.rstrip() + "\n\n" + report


def reserved_overlaps(
    changed: list[str], held: dict[str, list[str]], *, fold: bool = False
) -> list[str]:
    """Names the peer reservations a lane's own changed paths run into.

    Args:
        changed: Repository-relative paths the lane's branch changed.
        held: Active reservation keys per peer identity, with the lane's own
            identity already removed.
        fold: Whether paths are compared without regard to case.

    Returns:
        One sentence per overlap, ordered by path and then by peer, naming
        the peer, the key it holds and the changed path that key covers.
    """
    return [
        f"{peer} reserves {pattern!r}, which covers {path!r}"
        for path in sorted(changed)
        for peer, patterns in sorted(held.items())
        for pattern in sorted(patterns)
        if store.overlapping(path, pattern, fold=fold)
    ]


class Bridge(
    SettingsMixin,
    IntegrationMixin,
    ClaimsMixin,
    StatusMixin,
    LaunchMixin,
):
    """Coordinates native agent worktrees using one private local state root.

    Each command group is a base class in its own module; this class joins
    them into the one command object the parser dispatches to.

    Attributes:
        home: Resolved private state directory.
        config: Local HTTP port and bearer credential.
        url: Loopback HTTP origin of the mail server.
    """

    def restore(self, repo: Path, name: str) -> str:
        """Returns a drifted lane to its branch without discarding work.

        Args:
            repo: Any checkout of the target repository.
            name: Participant whose lane must return to its bridge branch.

        Returns:
            An account of what changed.

        Raises:
            BridgeError: If the lane is busy, dirty, missing, or holds commits
                the assigned branch does not.
        """
        directory, data, participant = self._lane(repo, name)
        lane = Path(participant["lane"])
        branch = participant["branch"]
        with lock(
            directory / f"{name}.session.lock",
            f"{name} has a running session; stop that terminal first.",
        ):
            if not lane.exists():
                if moved := reclaim.relocated(data["root"], lane, branch):
                    raise BridgeError(
                        f"{name}: {reclaim.moved_remedy(lane, moved)}."
                    )
                raise BridgeError(
                    f"{name} has no worktree at {lane}. Retire the "
                    "participant, then add it again."
                )
            actual = lane_branch(lane)
            if actual == branch:
                return f"{name} is already on {branch}."
            if not has_branch(lane, branch):
                raise BridgeError(
                    f"Branch {branch} no longer exists. Recover it from the "
                    f"reflog, or retire {name}, follow any kept-branch "
                    "recovery instructions, then add it again."
                )
            if git(lane, "status", "--porcelain"):
                raise BridgeError(
                    f"{name} has uncommitted changes on {actual}. Commit or "
                    "preserve them first; restore never discards work.",
                    next_command=worktrees.commit_all(lane),
                )
            unmerged = git(lane, "log", "--oneline", f"{branch}..HEAD")
            if unmerged:
                head = git(lane, "rev-parse", "HEAD")
                raise BridgeError(
                    f"{name} holds commits that {branch} does not:\n"
                    f"{unmerged}\nKeep them first with `git -C "
                    f"{shlex.quote(str(lane))} branch KEEP_NAME {head}`, then "
                    "rerun; restore never discards work."
                )
            git(lane, "switch", branch)
            return f"{name} restored to {branch} from {actual}."

    def retire(self, repo: Path, name: str, discard: Sequence[str] = ()) -> str:
        """Removes a participant's lane while preserving any work it holds.

        Deliveries still unread or unacknowledged by the lane are marked
        superseded, because a lane that left answers nothing; the messages
        themselves stay readable. The lane's issue claims return to the pool
        and its advisory reservations are released, granting what peers
        queued, as a self-retirement does; a retired name is never swept
        later, so anything it kept would stay held. Ready work refuses the
        retirement before anything is removed. Removing the worktree deletes
        its ignored files, which `git status` never reports, so they are
        deleted only when every one of them was named in ``discard``. The
        lane's session and checkpoint locks are both taken before the first
        irreversible step, so a busy lane refuses the retirement with its
        worktree, branch, credential and record intact rather than failing
        halfway.

        Args:
            repo: Any checkout of the target repository.
            name: Participant whose lane is retired.
            discard: Ignored paths the operator agreed to delete.

        Returns:
            An account of what was removed and what was kept.

        Raises:
            BridgeError: If the lane is busy, holds uncommitted changes,
                holds an ignored file ``discard`` does not name, or holds
                ready work awaiting integration.
        """
        from agent_parley import retirement

        root, directory = self.project(repo, create=False)
        roster.read(directory)
        with lock(directory / "setup.lock"):
            data = self._project(root, directory, verify=set())
            participant = data["participants"].get(name)
            if participant is None:
                raise BridgeError(
                    f"{name} is not a participant in this project; "
                    "run agent-parley participant list.",
                    next_command="agent-parley participant list",
                )
            lane = Path(participant["lane"])
            branch = participant["branch"]
            with (
                lock(
                    directory / f"{name}.session.lock",
                    f"{name} has a running session; stop that terminal first.",
                ),
                lock(directory / f"{name}-checkpoint.lock", timeout=1),
            ):
                if lane.exists():
                    if git(lane, "status", "--porcelain"):
                        raise BridgeError(
                            f"{name} has uncommitted changes. Commit or "
                            "preserve them first; retire never discards work.",
                            next_command=worktrees.commit_all(lane),
                        )
                    kept = [
                        path
                        for path in ignored_paths(lane)
                        if path not in discard
                    ]
                    if kept:
                        raise BridgeError(
                            f"{name}'s worktree holds ignored files that "
                            f"removing it deletes: "
                            f"{', '.join(shown_paths(kept))}. Move "
                            "them first, or run `agent-parley participant "
                            f"retire {name}` to confirm deleting them."
                        )
                retirement.return_work(directory, data, name)
                if lane.exists():
                    git(root, "worktree", "remove", str(lane))
                git(root, "worktree", "prune")
                note = f"Branch {branch} was already gone."
                if has_branch(root, branch):
                    if git(
                        root, "log", "--oneline", f"{data['base']}..{branch}"
                    ):
                        note = (
                            f"Branch {branch} kept; it holds commits the "
                            "project base does not. Nothing renames or "
                            "deletes it, and a new lane takes the next free "
                            "branch name, so adding this participant again "
                            "leaves those commits exactly where they are."
                        )
                    else:
                        git(root, "branch", "-d", branch)
                        note = f"Branch {branch} deleted; it added no commits."
                store.release_dead_holder(
                    self.home,
                    data["root"],
                    participant["display"],
                    f"{name} was retired by the operator",
                )
                store.revoke(self.home, data["root"], participant["display"])
                store.supersede_recipient(
                    self.home,
                    data["root"],
                    participant["display"],
                    f"{name} retired",
                )
                metrics.record_report(
                    directory,
                    name,
                    {
                        "kind": "integration",
                        "action": "retire",
                        **held_claim(directory, name),
                    },
                )
                for suffix in (
                    "identity.json",
                    "activity.json",
                    "mcp.json",
                    "events.jsonl",
                    "events.1.jsonl",
                    "events.jsonl.tmp",
                    "events.1.jsonl.tmp",
                    "gemini-settings.json",
                    "amp-settings.json",
                ):
                    (directory / f"{name}-{suffix}").unlink(missing_ok=True)
                shutil.rmtree(
                    directory / f"{name}-opencode", ignore_errors=True
                )
                release_copilot(self.home, participant, name)
                del data["participants"][name]
                write_json(directory / "project.json", data)
            (directory / f"{name}-checkpoint.lock").unlink(missing_ok=True)
            (directory / f"{name}-integration.lock").unlink(missing_ok=True)
            (directory / f"{name}.session.lock").unlink(missing_ok=True)
            return f"Retired {name}. {note} Messages are preserved."

    def _record_operator(
        self, directory: Path, name: str, reason: checkpoints.Reason, note: str
    ) -> None:
        """Writes one operator lifecycle decision into the lane's event log."""
        checkpoints.record(
            directory,
            name,
            {"hook_event_name": "OperatorCommand"},
            reason,
            None,
            note,
        )

    def pause(self, repo: Path, name: str, *, resume: bool = False) -> str:
        """Refuses or restores a lane's coordination without ending it.

        A paused lane keeps its session, its claims and its reservations. It
        is refused the ability to act: every served coordination call and
        every native tool use comes back denied, naming the operator as the
        cause. Nothing is released on the lane's behalf, because pausing is
        not a handoff.

        Args:
            repo: Any checkout of the target repository.
            name: Participant whose lane is paused or restored.
            resume: Whether to clear the pause instead of setting it.

        Returns:
            An account of the lane's new state and what it still holds.

        Raises:
            BridgeError: If the participant does not exist.
        """
        directory, _, _ = self._lane(repo, name)
        with lock(directory / "setup.lock"):
            data = roster.read(directory)
            participant = data["participants"].get(name)
            if participant is None:
                raise BridgeError(
                    f"{name} was retired while this command ran; nothing "
                    "changed."
                )
            if participant.get("paused", False) is not resume:
                state = "paused" if not resume else "not paused"
                return f"{name} is already {state}; nothing changed."
            participant["paused"] = not resume
            write_json(directory / "project.json", data)
        self._record_operator(
            directory,
            name,
            (
                checkpoints.Reason.OPERATOR_RESUMED
                if resume
                else checkpoints.Reason.OPERATOR_PAUSED
            ),
            "paused" if not resume else "",
        )
        if resume:
            return f"{name} is resumed and serves coordination calls again."
        held = self._holdings(directory, name)
        return (
            f"{name} is paused. Its session, claims and reservations are "
            f"retained and nothing was released. {held}"
        )

    def _holdings(self, directory: Path, name: str) -> str:
        """Describes what one lane still owns, for an operator to act on."""
        owned = sorted(
            (
                number
                for number, record in snapshot(directory)["issues"].items()
                if record["owner"] == name
            ),
            key=int,
        )
        claims = (
            "It still owns " + ", ".join(f"#{number}" for number in owned)
            if owned
            else "It owns no issue"
        )
        return (
            f"{claims}. Ownership moves only through an explicit release or "
            "an accepted handoff."
        )

    def stop(self, repo: Path, name: str) -> str:
        """Ends one lane's native session from the base checkout.

        The recorded session process is signalled exactly as a normal exit
        signals it and given a bounded time to leave; a process that ignores
        that signal is sent `SIGKILL`, and the session record is cleared only
        once the process is verified gone. No notice is mailed to the lane:
        the session is gone before it could read one, so the mail would only
        reach the next session, including the one `restart` launches, as an
        instruction to end, and would count as backlog. Identity is the recorded
        process ID together with its kernel creation time, checked here and
        again inside the platform's terminate step, so a recycled process ID
        is never signalled. The command-line check used to recognize the
        coordination server does not apply: a lane runs a native client, not
        this package.

        Claims and reservations stay owned. Ending a session is not a handoff,
        so what the lane still holds is reported for the operator to move
        deliberately. The command is recorded either way, including when it
        finds no session to end, so the ledger shows every operator action
        rather than only the ones that changed something.

        The activity record is marked `operator_stopped` in both cases, and
        the service does not wake or resume the lane until its next launch
        clears the mark. Without it the stopped label reads as a lane owed a
        turn, the next sweep resumes it, and the operator's own `run` meets a
        lock held by a session with no terminal attached.

        Args:
            repo: Any checkout of the target repository.
            name: Participant whose session is ended.

        Returns:
            An account of what was stopped and what the lane still holds.

        Raises:
            BridgeError: If the participant does not exist, or the recorded
                process did not exit even after `SIGKILL`.
        """
        directory, _, _ = self._lane(repo, name)
        path = directory / f"{name}-activity.json"
        state = json.loads(path.read_text()) if path.exists() else {}
        pid = state.get("session_pid")
        ticks = str(state.get("session_ticks") or "")
        if (
            type(pid) is not int
            or not process.alive(pid, ticks)
            or supervision.rebooted(state)
        ):
            with lock(directory / f"{name}-checkpoint.lock", timeout=1):
                state = json.loads(path.read_text()) if path.exists() else {}
                state.update(operator_stopped=True)
                write_json(path, state)
            self._record_operator(
                directory,
                name,
                checkpoints.Reason.OPERATOR_STOPPED,
                "no verified session",
            )
            return (
                f"{name} has no verified running session to stop. "
                f"{self._holdings(directory, name)}"
            )
        process.ServerProcess(pid, ticks).stop()
        if process.alive(pid, ticks):
            raise BridgeError(
                f"{name}'s session process {pid} is still running after "
                "SIGTERM and SIGKILL; its session record is kept."
            )
        with lock(directory / f"{name}-checkpoint.lock", timeout=1):
            state = json.loads(path.read_text()) if path.exists() else {}
            state.update(
                activity="stopped", operator_stopped=True, updated=time.time()
            )
            state.pop("session_pid", None)
            state.pop("session_ticks", None)
            state.pop("session_boot", None)
            state.pop("session_started", None)
            state.pop("launcher_pid", None)
            state.pop("launcher_ticks", None)
            write_json(path, state)
        self._record_operator(
            directory, name, checkpoints.Reason.OPERATOR_STOPPED, "stopped"
        )
        return (
            f"{name}'s session was ended from the base checkout. "
            f"{self._holdings(directory, name)}"
        )

    def restart(self, repo: Path, name: str, task: str = "") -> int:
        """Starts one lane again in its own worktree on its own branch.

        A restart is refused while a session is alive and current, because two
        clients in one worktree would fight over it. A session whose process
        is alive but whose evidence is stale past the project's
        `inactive_after` is a wedged client, such as one left in a native
        dialog or behind a system hang, so it is ended first through the same
        escalating stop the operator command uses.

        The lane must be on its assigned branch. Uncommitted work on that
        branch is the previous session's own and is exactly what a crash
        leaves behind, so it is not a reason to refuse: every claim the lane
        owns is captured into a recovery checkpoint first, the worktree is
        left as it is, and the new session is told where the checkpoint is.
        Nothing here resets, cleans, stashes or force-switches. Any recorded
        lane initialization command runs again, because a restart recreates
        the starting state.

        Args:
            repo: Any checkout of the target repository.
            name: Participant whose lane is started again.
            task: Opening instruction for the new session.

        Returns:
            The native client's exit status.

        Raises:
            BridgeError: If a current session is alive, a stale one cannot be
                ended, the uncommitted work cannot be captured, or the lane
                is not on its assigned branch.
        """
        from agent_parley import recovery

        directory, data, participant = self._lane(repo, name)
        state = checkpoints.activity(directory, name)
        if process.alive(
            state.get("session_pid"), state.get("session_ticks")
        ) and not supervision.rebooted(state):
            window = supervision.configuration(self.home, data)
            if not supervision.lane_state(state, window["inactive_after"])[
                "stale"
            ]:
                raise BridgeError(
                    f"{name} still has a live session. Run `agent-parley "
                    f"participant stop {name}` first; a restart never runs "
                    "two clients in one worktree.",
                    next_command=f"agent-parley participant stop {name}",
                )
            self.stop(repo, name)
        lane = Path(participant["lane"])
        actual = current_branch(lane)
        if actual != participant["branch"]:
            raise BridgeError(
                drift(name, participant, actual),
                next_command=f"agent-parley participant restore {name}",
            )
        opening = task or terminal.PROMPT
        if git(lane, "status", "--porcelain"):
            saved = recovery.capture(directory, data, name)
            opening += (
                "\nThe previous session in this worktree ended with "
                "uncommitted changes; they were left in place, not reset. "
            )
            if saved:
                opening += "Recovery checkpoints: " + "; ".join(
                    f"#{item['issue']} {item['id']} at "
                    f"{directory / recovery.RECOVERY_FOLDER}"
                    f"/{item['artifact']['reference']}"
                    for item in saved
                )
        if data.get("initialize"):
            started = time.monotonic()
            initialize_lane(lane, data["initialize"], Path(data["root"]))
            self.record_launch(
                directory, name, preparation(0.0, time.monotonic() - started)
            )
        self._record_operator(
            directory, name, checkpoints.Reason.OPERATOR_RESTARTED, "starting"
        )
        return self.launch(
            name,
            repo,
            opening,
            participant["provider"],
            participant["credential"],
        )

    def pull_request(self, repo: Path, name: str) -> str:
        """Opens a verified pull request while excluding a live lane launch.

        A repository whose ``pull_request`` policy turns ``self_service`` on
        also lets a lane run this command for its own work from its own
        worktree. That path excludes a second self-service attempt instead
        of a live lane session, because there the lane running the command
        is that session. It is refused with the unmet condition named unless
        every condition of the policy holds. Every other caller keeps the
        operator path, which still excludes a live lane launch, so a project
        that leaves the setting off behaves exactly as before.

        Args:
            repo: Any checkout of the target repository.
            name: Participant whose committed work is reviewed.

        Returns:
            The pushed branch and opened or existing pull request.

        Raises:
            BridgeError: If the lane is active or integration is refused.
        """
        directory, data, participant = self._lane(repo, name)
        if self._self_opened(repo, data, participant):
            return self.self_service_pull_request(repo, name)
        with lock(directory / f"{name}.session.lock"):
            return self._pull_request(repo, name)

    def self_service_pull_request(self, repo: Path, name: str) -> str:
        """Opens or updates a lane's pull request under its self-service policy.

        The lane's own `pull-request` command and a blueprint's
        ``pull-request`` node both come here, so either one is admitted only
        while every condition of `_authorize` holds and only one attempt per
        lane runs at a time.

        Args:
            repo: Any checkout of the target repository.
            name: Participant whose committed work is reviewed.

        Returns:
            The pushed branch and opened or refreshed pull request.

        Raises:
            BridgeError: If a condition of the policy does not hold, another
                attempt is in flight, or integration is refused.
        """
        directory, data, _ = self._lane(repo, name)
        with lock(
            directory / f"{name}-integration.lock",
            f"{name} is already opening its own pull request.",
        ):
            return self._pull_request(
                repo, name, self._authorize(directory, data, name)
            )

    def _self_opened(self, repo: Path, data: dict, participant: dict) -> bool:
        """Reports whether a lane is opening the pull request for its own work.

        Args:
            repo: Checkout the command was run from.
            data: Project manifest holding the repository policy.
            participant: Manifest entry for the named participant.

        Returns:
            Whether the repository authorizes self-service pull requests and
            this command runs inside the named participant's own worktree.
        """
        if not data.get("pull_request", {}).get("self_service"):
            return False
        try:
            here = Path(git(repo, "rev-parse", "--show-toplevel")).resolve()
        except (BridgeError, subprocess.TimeoutExpired):
            return False
        return here == Path(participant["lane"]).resolve()

    def _authorize(self, directory: Path, data: dict, name: str) -> dict:
        """Checks what a repository requires before a lane opens its own work.

        The conditions are the repository's own: the lane reported ready, a
        verification command is configured for the gate that runs during the
        push, the lane still sits on its assigned branch, and no peer holds
        an advisory reservation over the paths the branch changed. Advisory
        reservations are coordination signals, not filesystem locks, so an
        overlap is a refusal to proceed unattended rather than a denial of
        access. Reservation state that cannot be read is a refusal too,
        because an unreadable store rules no overlap out.

        Args:
            directory: Private state directory for the common repository.
            data: Project manifest holding this participant.
            name: Participant opening the pull request for its own work.

        Returns:
            The conditions that authorized the pull request, recorded with
            the review evidence so a reader sees what the policy stood on.

        Raises:
            BridgeError: Naming the first condition that does not hold.
        """
        import sqlite3

        participant = data["participants"][name]
        branch = participant["branch"]
        refused = (
            f"{name} opens its own pull request only while every condition "
            "of this repository's self-service policy holds."
        )
        path = directory / f"{name}-activity.json"
        state = json.loads(path.read_text()) if path.exists() else {}
        if state.get("outcome") != "ready":
            reported = state.get("outcome") or "nothing"
            raise BridgeError(
                f"{refused} {name} reported {reported}, not ready. Record a "
                "ready report first."
            )
        if not (command := list(data.get("verify") or [])):
            raise BridgeError(
                f"{refused} This repository configures no verification "
                "command, so no green gate can authorize the pull request. "
                "Set one with `agent-parley verify set`."
            )
        actual = current_branch(Path(participant["lane"]))
        if actual != branch:
            raise BridgeError(f"{refused} {drift(name, participant, actual)}")
        changed = [
            line
            for line in git(
                Path(data["root"]),
                "diff",
                "--name-only",
                f"{data['base']}...{branch}",
            ).splitlines()
            if line
        ]
        try:
            held = store.active_reservations(self.home, data["root"])
        except (BridgeError, OSError, sqlite3.Error) as exc:
            raise BridgeError(
                f"{refused} The advisory reservations could not be read, so "
                f"no overlap with a peer can be ruled out: {exc}"
            ) from exc
        held.pop(participant["display"], None)
        if overlaps := reserved_overlaps(
            changed, held, fold=roster.case_insensitive(data)
        ):
            raise BridgeError(
                f"{refused} A peer reservation covers what {branch} changed: "
                + "; ".join(overlaps[:MAX_OVERLAPS])
                + ". Hand the work over or wait for the release."
            )
        return {
            "policy": "pull_request.self_service",
            "participant": name,
            "branch": branch,
            "reported_at": state.get("reported_at"),
            "gate": shlex.join(command),
            "changed_paths": len(changed),
            "peers_holding_reservations": sorted(held),
        }

    def _pull_request(
        self, repo: Path, name: str, authorization: dict | None = None
    ) -> str:
        """Pushes one lane's branch and opens its pull request.

        The pull request carries the lane's recorded report, so the summary,
        the verification evidence and the issues the lane claimed reach
        review as the participant reported them. Pushing is the only network
        side effect in the coordination runtime and it happens here alone:
        recording a report or reading status never reaches a remote. The
        title is the first commit the lane added, which already follows the
        target repository's own commit rules. An open pull request for the
        branch is refreshed rather than replaced by a second one: the branch
        advances, so its delimited evidence section is rewritten for the exact
        commit that was just pushed while every human edit around it survives.
        Old verification is never left presented as verification of a new
        head. A refresh that the forge refuses is reported as such after the
        successful push, and rerunning the command retries it without opening
        a second pull request.

        The pull request also opens owned and classified. The operator's own
        GitHub account becomes its assignee, and its change-type labels and
        milestone are mirrored from the issues the lane claims, so a lane
        never classifies its own work and the repository's own metadata rules
        are met at creation rather than repaired afterwards. Metadata is
        resolved before the branch is pushed, so a refusal leaves no remote
        branch behind.

        Args:
            repo: Any checkout of the target repository.
            name: Participant whose bridge branch becomes a pull request.
            authorization: Conditions a repository policy admitted a
                lane-opened pull request under, or None for the operator
                path. When present it is kept with the review evidence and
                with the integration record, so a self-opened pull request
                always carries what authorized it.

        Returns:
            An account naming the pushed branch and the pull request.

        Raises:
            BridgeError: If the project, participant, report, branch, claimed
                issue, issue classification, base checkout, push, or GitHub
                CLI cannot support a pull request.
            subprocess.TimeoutExpired: If Git or gh exceeds its timeout.
        """
        directory, data, participant = self._lane(repo, name)
        root = Path(data["root"])
        branch = participant["branch"]
        chosen = forge.select(root, data)
        if chosen != "github":
            raise BridgeError(
                f"{root} coordinates over the {chosen} forge, which opens no "
                "pull requests, so nothing was pushed."
            )
        self._require_approval(directory, data, name, "pr")
        path = directory / f"{name}-activity.json"
        state = json.loads(path.read_text()) if path.exists() else {}
        if not state.get("summary", "").strip():
            raise BridgeError(
                f"{name} has recorded no report, and a pull request carries "
                "that report. Run `agent-parley report --state ready "
                '--summary "..." --evidence "..."` in the lane first.'
            )
        if not has_branch(root, branch):
            raise BridgeError(
                f"Branch {branch} no longer exists. Recover it from the "
                f"reflog, or retire {name} and add it again."
            )
        if not git(root, "log", "--oneline", f"{data['base']}..{branch}"):
            raise BridgeError(
                f"{branch} adds no commits to the project base, so {name} "
                "has nothing to open a pull request for."
            )
        refusals = attributed_commits(root, data["base"], branch)
        if refusals:
            raise BridgeError(
                "\n".join(refusals)
                + "\nRewrite those commit messages in the lane and rerun; "
                "nothing was pushed and no pull request was opened."
            )
        claimed = sorted(
            (
                number
                for number, record in snapshot(directory)["issues"].items()
                if record["owner"] == name
            ),
            key=int,
        )
        if not claimed:
            raise BridgeError(
                f"{name} claims no issue, so the pull request would carry no "
                "issue reference. Run `agent-parley issue claim NUMBER` in "
                "the lane first."
            )
        policy = data.get("pull_request", {})
        labels, milestone = hygiene_metadata(root, claimed, policy)
        template = policy.get("body_template", "")
        if not template:
            for relative in (
                ".github/PULL_REQUEST_TEMPLATE.md",
                ".github/pull_request_template.md",
                "docs/pull_request_template.md",
                "pull_request_template.md",
            ):
                candidate = root / relative
                if candidate.is_file():
                    if candidate.stat().st_size > 20000:
                        raise BridgeError(
                            "Pull request template exceeds 20000 bytes."
                        )
                    template = candidate.read_text(encoding="utf-8")
                    break
        base = current_branch(root)
        if base in ("<detached HEAD>", branch):
            raise BridgeError(
                f"The base checkout at {root} is on {base}, which cannot "
                "receive this pull request. Switch it to the branch the "
                "pull request should target, then rerun."
            )
        lane = Path(participant["lane"])
        head = git(root, "rev-parse", branch)
        if current_branch(lane) != branch:
            raise BridgeError("Lane branch drifted; restore it before review.")
        measured = evidence.collect(self.home, directory, data, name, head)
        if authorization:
            measured["authorization"] = authorization
        if command := data.get("verify"):
            verify_base(lane, command)
            measured["gate"] = {
                "command": shlex.join(command),
                "exit_status": 0,
            }
        if git(root, "rev-parse", branch) != head or git(
            lane, "status", "--porcelain"
        ):
            raise BridgeError(
                "Lane changed during verification; review and retry."
            )
        recorded = evidence.publish(directory, measured)
        git(root, "push", "--set-upstream", "origin", branch)
        listed = json.loads(
            gh(
                root,
                "pr",
                "list",
                "--head",
                branch,
                "--state",
                "open",
                "--json",
                "url,number,body",
            )
            or "[]"
        )
        if listed:
            existing = listed[0]
            try:
                gh(
                    root,
                    "pr",
                    "edit",
                    str(existing["number"]),
                    "--body",
                    evidence.refresh(existing.get("body") or "", recorded),
                )
            except (BridgeError, subprocess.TimeoutExpired) as exc:
                return (
                    f"Pushed {branch} at {head}. A pull request is already "
                    f"open for it: {existing['url']}. Its recorded evidence "
                    f"still describes an earlier commit, because updating it "
                    f"failed: {exc}. Rerun this command to retry; no second "
                    "pull request is opened."
                )
            return (
                f"Pushed {branch} and refreshed the recorded evidence of "
                f"{existing['url']} for {head}."
            )
        title = git(
            root,
            "log",
            "--reverse",
            "--format=%s",
            f"{data['base']}..{branch}",
        ).splitlines()[0]
        arguments = [
            "pr",
            "create",
            "--base",
            base,
            "--head",
            branch,
            "--title",
            title,
            "--body",
            pull_request_body(
                state,
                claimed,
                template,
                metrics.latest_review(directory, name),
            )
            + "\n"
            + evidence.section(recorded),
            "--assignee",
            "@me",
        ]
        for label in labels:
            arguments.extend(["--label", label])
        if milestone:
            arguments.extend(["--milestone", milestone])
        created = gh(root, *arguments)
        opened = created.splitlines()[-1] if created else "a pull request"
        metrics.record_report(
            directory,
            name,
            {
                "kind": "integration",
                "action": "pull_request",
                "pull_request": opened,
                **({"authorization": authorization} if authorization else {}),
                **held_claim(directory, name),
            },
        )
        if authorization:
            return (
                f"Pushed {branch} and opened {opened} under this "
                "repository's self-service policy; the conditions that "
                "authorized it are recorded with the review evidence."
            )
        return f"Pushed {branch} and opened {opened}"

    def reclaim(self, repo: Path, *, apply: bool = False) -> list[dict]:
        """Reports, and optionally removes, the lanes whose work has landed.

        A lane whose pull request merged holds nothing the operator still
        needs, and a project that never reclaims one accumulates a worktree
        and a branch for every claim it ever ran. The removal itself is the
        ordinary retirement, so a reclaimed lane leaves exactly the state a
        retired lane leaves and takes the same refusals: a lane that is
        busy, dirty or ahead of the base checkout is kept and reported
        instead of being removed.

        Retirement keeps a branch that carries commits the recorded project
        base does not, which is every branch that ever did work. The sweep
        has already established that the base checkout and the branch's own
        upstream carry those commits, so it then asks Git to delete the
        branch under Git's own merged-branch rule. A branch Git refuses
        leaves the lane reclaimed and the branch in place.

        Args:
            repo: Any checkout of the target repository.
            apply: Whether the assessed lanes are removed; the default only
                reports what a sweep would do.

        Returns:
            One row per lane, naming the lane, its branch, whether it was
            removed, whether its branch was deleted, the condition that
            decided it and any paths that condition names.
        """
        root, directory = self.project(repo, create=False)
        manifest = roster.read(directory)
        inactive = supervision.configuration(self.home, manifest)[
            "inactive_after"
        ]
        idle = set()
        for name in manifest["participants"]:
            observed = supervision.presence(directory, name, inactive)
            if observed["process_alive"] is True and observed["stale"]:
                idle.add(name)
        rows = reclaim.plan(directory, manifest, frozenset(idle))
        if not apply:
            return rows
        for row in rows:
            if not row["reclaim"]:
                continue
            lane = Path(manifest["participants"][row["participant"]]["lane"])
            try:
                self.retire(
                    repo,
                    row["participant"],
                    discard=ignored_paths(lane) if lane.exists() else (),
                )
            except BridgeError as exc:
                row.update(reclaim=False, removed=False, reason=str(exc))
                continue
            with contextlib.suppress(BridgeError):
                git(root, "branch", "-d", row["branch"])
            row.update(
                removed=True, branch_removed=not has_branch(root, row["branch"])
            )
        return rows

    def reclaim_worktrees(
        self,
        repo: Path,
        *,
        apply: bool = False,
        sizes: bool = False,
        force: bool = False,
    ) -> list[dict]:
        """Reports, and optionally removes, worktrees lanes made themselves.

        Lanes add worktrees for pull requests and sub-tasks that no lane
        root accounts for. Each one the project repository registers is
        assessed by `reclaim.strays`, whether or not a lane made it, and
        one no lane made that a process works inside, outside the project
        state directory, is kept. Git's own removal refuses a dirty or
        locked worktree, so nothing with uncommitted work is lost unless
        the operator forces it, and a forced removal writes a recovery
        checkpoint first. The spare worktrees a project pool keeps are
        assessed by `pool.sweep` in the same pass: a stale spare or one
        beyond the pool size is removed with its branch, and a usable one
        is kept.

        Args:
            repo: Any checkout of the target repository.
            apply: Whether the reclaimable worktrees are removed.
            sizes: Whether each worktree's size on disk is measured.
            force: Whether a worktree kept only for uncommitted changes,
                unpushed commits or a recent change is removed as well,
                after its checkpoint. Applies only with `apply`.

        Returns:
            One row per worktree, as `reclaim.strays` shapes it, carrying
            whether it was removed when applied and any checkpoint written.
        """
        from agent_parley import pool

        root, directory = self.project(repo, create=False)
        manifest = roster.read(directory)
        rows = reclaim.strays(directory, manifest, sizes=sizes)
        spares = pool.sweep(root, directory, manifest, apply=apply)
        if not apply:
            return rows + spares
        return [
            reclaim.remove(str(root), directory, row, force=force)
            for row in rows
        ] + spares

    def relocate_worktrees(
        self, repo: Path, *, apply: bool = False
    ) -> list[dict]:
        """Reports, and optionally performs, moves of stray worktrees.

        A worktree a lane made outside its `<lane>-wt` directory is planned
        into it, and one no lane accounts for into the project's `strays`
        directory when it sits inside the repository, under the state root
        or in a temporary directory, by `reclaim.relocations` and moved with
        `git worktree move`, which never deletes. It runs before the
        reclaim sweeps, so a moved worktree is attributed by path in the
        same pass.

        Args:
            repo: Any checkout of the target repository.
            apply: Whether the planned moves are performed.

        Returns:
            One row per planned move, as `reclaim.relocations` shapes it,
            carrying whether Git moved it when applied.
        """
        _, directory = self.project(repo, create=False)
        manifest = roster.read(directory)
        rows = reclaim.relocations(directory, manifest)
        if not apply:
            return rows
        return [
            reclaim.relocate(manifest["root"], directory, row) for row in rows
        ]


COMMAND_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "Coordination",
        (
            "issue",
            "followup",
            "mail",
            "say",
            "ask",
            "decide",
            "decision",
            "report",
            "participant",
            "approve",
            "reject",
        ),
    ),
    (
        "Policy",
        (
            "approval",
            "verify",
            "unattended",
            "timeout",
            "init",
            "pool",
            "blueprint",
            "branch",
            "forge",
            "deadlines",
            "budget",
            "resources",
            "provider",
            "credentials",
            "quota",
        ),
    ),
    (
        "Observability",
        (
            "status",
            "top",
            "title",
            "watch",
            "metrics",
            "history",
            "events",
            "problems",
            "doctor",
            "notify",
        ),
    ),
    (
        "Lifecycle",
        (
            "up",
            "down",
            "setup",
            "run",
            "projects",
            "workspace",
            "plan",
            "state",
            "store",
            "gc",
            "version",
            "plugins",
            "completion",
            "demo",
        ),
    ),
)


START_HERE: tuple[str, tuple[str, ...]] = (
    "Start here",
    ("run", "status", "top", "problems", "demo", "doctor"),
)
"""Leading root-help group of the commands a new operator reaches first.

Its commands stay listed in their own groups too, and a name this build does
not declare is skipped, so the group can name a command before it lands.
"""


class Absorbed:
    """Accepts a command's declarations without building a parser for them.

    Argparse builds a parser and every argument, group and nested subcommand
    it is given, and an invocation can only ever parse with one of them. A
    command that was not typed is declared against this instead: every
    builder call it makes is accepted and answered with the same object, so
    the declaration reads exactly as it does for the typed command and
    nothing argparse charges for is created.
    """

    def __getattr__(self, name: str) -> Callable[..., Absorbed]:
        """Answers any builder call argparse's parsers offer.

        Args:
            name: Attribute the declaration reached for.

        Returns:
            A callable that accepts the declaration and answers with this
            same object, so nested subcommands absorb in turn.
        """
        return self.absorb

    def absorb(self, *args: Any, **kwargs: Any) -> Absorbed:
        """Accepts one declaration and answers with this same object.

        Args:
            *args: Positional arguments the declaration passed.
            **kwargs: Keyword arguments the declaration passed.

        Returns:
            This object.
        """
        return self


WHOLE_PARSER = frozenset({"completion", "__complete"})


class CommandIndex:
    """Adds subcommands while recording the help the root listing prints.

    Argparse lists subcommands in the order they were declared and offers no
    grouping of its own. The index records each one-line help as its parser
    is created, so the root help can print the same commands under the
    headings an operator thinks in while every parser keeps the arguments it
    declared. A command whose help is suppressed stays out of the listing.

    Only the command an invocation typed needs a parser argparse can parse
    with. The rest are recorded and absorbed, which keeps the listing and the
    declarations whole while the cost of the other commands is not paid. The
    shell completion commands read the whole parser, so they are built with
    every command present.
    """

    def __init__(
        self,
        action: argparse._SubParsersAction,
        typed: str | None = None,
    ) -> None:
        """Wraps the subparsers action every command is added through.

        Args:
            action: Subparsers action created on the root parser.
            typed: Command that was typed, or ``None`` to build them all.
        """
        self.action = action
        self.summaries: dict[str, str] = {}
        self.declared: set[str] = set()
        self.typed = None if typed in WHOLE_PARSER else typed

    def add_parser(self, name: str, **kwargs: Any) -> argparse.ArgumentParser:
        """Creates one subcommand parser and records its one-line help.

        Args:
            name: Subcommand name the operator types.
            **kwargs: Arguments argparse's own ``add_parser`` accepts.

        Returns:
            The created subcommand parser, or an object that absorbs the
            declarations of a command this invocation did not type.
        """
        summary = kwargs.get("help")
        if isinstance(summary, str) and summary != argparse.SUPPRESS:
            self.summaries[name] = summary
        self.declared.add(name)
        if self.typed is not None and self.typed != name:
            return cast(argparse.ArgumentParser, Absorbed())
        return self.action.add_parser(name, **kwargs)


def command_help(index: CommandIndex) -> str:
    """Renders the root help's command listing under its groups.

    Args:
        index: Index holding every declared command and its one-line help.

    Returns:
        The grouped listing, with any command outside a declared group under
        a final heading so a new command is never silently unlisted.
    """
    width = max(len(name) for name in index.summaries) + 2
    listed: set[str] = set()
    blocks = [
        (
            START_HERE[0],
            [name for name in START_HERE[1] if name in index.summaries],
        )
    ]
    for title, names in COMMAND_GROUPS:
        members = [name for name in names if name in index.summaries]
        listed.update(members)
        blocks.append((title, members))
    blocks.append(
        ("Other", [name for name in index.summaries if name not in listed])
    )
    lines = []
    for title, members in blocks:
        if not members:
            continue
        lines.append(f"{title}:")
        for name in members:
            lines.extend(
                textwrap.wrap(
                    index.summaries[name],
                    78,
                    initial_indent=f"  {name.ljust(width)}",
                    subsequent_indent=" " * (width + 2),
                )
            )
        lines.append("")
    lines.append("Run `agent-parley COMMAND --help` for one command's flags.")
    return "\n".join(lines)


def add_reader_argument(command: argparse.ArgumentParser) -> None:
    """Declares the reader selector the read-only mail verbs share.

    Args:
        command: Parser receiving the argument.
    """
    command.add_argument(
        "--as",
        dest="reader",
        default="",
        metavar="PARTICIPANT",
        help=(
            "Read this lane's mail from the main checkout instead of its "
            "worktree. It reads only: nothing is sent, acknowledged or "
            "marked read for that lane."
        ),
    )


def add_say_arguments(command: argparse.ArgumentParser) -> None:
    """Declares the operator message arguments `say` and `mail send` share.

    Args:
        command: Parser receiving the arguments.
    """
    command.add_argument(
        "participant",
        nargs="?",
        default="",
        help=(
            "Participant whose inbox receives the message. A lane selector "
            "replaces it, and the message text is then the only positional."
        ),
    )
    command.add_argument(
        "text",
        nargs="?",
        default="",
        help="Message body the participant reads.",
    )
    command.add_argument(
        "--repo", type=Path, default=Path.cwd(), help=REPO_HELP
    )
    command.add_argument(
        "--subject",
        default="",
        help="Subject line shown in the lane's inbox.",
    )
    command.add_argument(
        "--key",
        default="",
        help=(
            "Idempotency key. Without one the key follows the message text, "
            "so repeating the same message delivers nothing further."
        ),
    )
    command.add_argument(
        "--ack",
        action="store_true",
        help="Require the participant to acknowledge the message.",
    )
    command.add_argument(
        "--within",
        type=duration,
        metavar="WINDOW",
        help=(
            "Record a deadline for the acknowledgement, such as 15m. Past it "
            "the acknowledgement reads overdue; nothing is resent, escalated "
            "or acknowledged for the lane."
        ),
    )
    command.add_argument(
        "--after",
        type=duration,
        metavar="WINDOW",
        help=(
            "Hold the message until this much time has passed, such as 30m. "
            "The supervision poll delivers it; nothing delivers while the "
            "service is stopped and nothing is lost."
        ),
    )
    command.add_argument(
        "--at",
        type=clock,
        metavar="HH:MM",
        help=(
            "Hold the message until this time of day in the local timezone, "
            "today while it is still ahead and tomorrow once it has passed."
        ),
    )
    command.add_argument(
        "--when-released",
        default="",
        metavar="NUMBER",
        help=(
            "Hold the message until this issue is explicitly released or its "
            "pull request is recorded as ended."
        ),
    )
    command.add_argument(
        "--unless-reported",
        action="store_true",
        help=(
            "Drop a delayed message if the lane files a report of its own "
            "before its time arrives."
        ),
    )
    command.add_argument(
        "--every",
        type=duration,
        metavar="WINDOW",
        help=(
            "Repeat the message on this interval, such as 1h. A repeat must "
            "be bounded by --until and is capped at "
            f"{store.MAX_REPEATS} deliveries."
        ),
    )
    command.add_argument(
        "--until",
        type=clock,
        metavar="HH:MM",
        help="Stop a repeat at this time of day in the local timezone.",
    )
    command.add_argument("--json", action="store_true", help=JSON_HELP)
    add_selector(command)


def typed_parser(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> argparse.ArgumentParser:
    """Finds the parser of the command the parsed arguments name.

    A usage error raised through it prints that command's usage rather
    than the root parser's.

    Args:
        parser: Root parser the arguments were parsed with.
        args: Parsed arguments.

    Returns:
        The deepest subcommand parser the arguments selected.
    """
    current = parser
    while True:
        nested = next(
            (
                action
                for action in current._actions
                if isinstance(action, argparse._SubParsersAction)
            ),
            None,
        )
        if nested is None:
            return current
        chosen = getattr(args, nested.dest, None)
        if chosen not in nested.choices:
            return current
        current = nested.choices[chosen]


def spoken(
    bridge: Bridge, parser: argparse.ArgumentParser, args: argparse.Namespace
) -> int:
    """Delivers one operator message, to a named lane or to a selected set.

    Args:
        bridge: Launcher holding the private coordination state.
        parser: Root parser, whose `say` or `mail send` parser reports a
            usage error.
        args: Parsed `say` or `mail send` arguments.

    Returns:
        0 when the message was delivered, 1 when a selected lane refused it.
    """
    repo = args.repo.resolve()
    if selected(args):
        return spoken_lanes(bridge, repo, args)
    if not args.participant or not args.text:
        typed_parser(parser, args).error(
            "say needs a participant and a message, or a lane selector and "
            "a message."
        )
    delivered = bridge.say(
        repo,
        args.participant,
        args.text,
        args.subject,
        args.key,
        args.ack,
        args.within,
        after=args.after,
        at=args.at,
        when_released=args.when_released,
        unless_reported=args.unless_reported,
        every=args.every,
        until=args.until,
    )
    print(
        views.render(
            "say", {"participant": args.participant, "message": delivered}
        )
        if args.json
        else operator_message(delivered, args.participant)
    )
    return 0


def lane_reading(bridge: Bridge, repo: Path, name: str) -> tuple[dict, dict]:
    """Reads one lane's status record beside the roster entry defining it.

    Args:
        bridge: Launcher holding the private coordination state.
        repo: Any checkout of the target repository.
        name: Participant that owns the lane.

    Returns:
        The lane's record from the status reading and its roster entry.

    Raises:
        BridgeError: If the project has no participant of that name.
    """
    _, directory = bridge.project(repo, create=False)
    data = roster.read(directory)
    if name not in data["participants"]:
        raise BridgeError(
            f"No participant named {name!r} in {data['root']}; run "
            "agent-parley participant list.",
            next_command="agent-parley participant list",
        )
    for project in bridge.status_snapshot()["projects"]:
        if project["root"] != data["root"]:
            continue
        for record in project["participants"]:
            if record["participant"] == name:
                return record, data["participants"][name]
    raise BridgeError(f"No lane reading for {name!r} yet.")


def issue_lines(reading: dict) -> str:
    """Renders one issue's ownership, blockers, reservations and history.

    Args:
        reading: Reading produced by the issue show command.

    Returns:
        The issue's current state as text, ending with its recorded history.
    """
    record = reading["record"]
    number = reading["issue"]
    if record is None:
        return f"Issue #{number} is not in the ledger of {reading['root']}."
    timing = deadline_state(record)
    owner = record.get("owner") or "unclaimed"
    title = record.get("title") or ""
    lines = [f"Issue #{number}: {owner}" + (f" — {title}" if title else "")]
    if timing["deadline"]:
        lines.append(
            f"Deadline: {views.timestamp(timing['deadline'])}"
            + (
                f"; overdue by {timing['overdue_seconds']}s"
                if timing["overdue"]
                else ""
            )
        )
    if timing["budget"]:
        lines.append(
            f"Attempts: {timing['attempts']}/{timing['budget']}"
            + ("; budget exceeded" if timing["budget_exceeded"] else "")
        )
    if blocked := record.get("blocked_by"):
        lines.append(
            "Blocked by: " + ", ".join(f"#{other}" for other in blocked)
        )
    if offered := record.get("offer"):
        lines.append(
            f"Offer {offered['id']} to {offered['to']}: {offered['summary']}"
        )
    lines.append(
        "Reservations held: "
        + (", ".join(reading["reservations"]) or "none recorded")
    )
    if reading.get("convergence"):
        lines.append(convergence.describe(reading["convergence"]))
    reported = reading["history"]
    lines.append(
        history.describe(
            reported["records"],
            reported["holdings"],
            truncated=bool(reported.get("truncated")),
        )
    )
    return "\n".join(lines)


def notification_report(report: dict) -> str:
    """Describes the outcome of one notification transport test.

    Args:
        report: Project root and per-transport results of the test send.

    Returns:
        One line per configured transport, carrying the refusal text when a
        transport did not accept the message.
    """
    lines = [f"Notification test for {report['root']}:"]
    lines += [
        f"  {item['transport']}: "
        + ("sent" if item["ok"] else f"failed: {item['error']}")
        for item in report["results"]
    ]
    received = report.get("inbound") or {}
    if received.get("fault"):
        lines.append(f"  inbound: off: {received['fault']}")
    elif received.get("polling"):
        lines.append("  inbound: the service is long polling Telegram")
    elif received.get("enabled"):
        lines.append(
            "  inbound: configured but not running; restart the service: "
            "agent-parley down && agent-parley up"
        )
    return "\n".join(lines)


def secret(prompt: str) -> str:
    """Reads one secret without echo, or one line of piped standard input.

    Args:
        prompt: Text shown on a terminal before the hidden input.

    Returns:
        The secret with surrounding whitespace removed.
    """
    import getpass

    if sys.stdin.isatty():
        return getpass.getpass(prompt).strip()
    return sys.stdin.readline().strip()


def secret_settings(
    chat: str, answering: bool, users: str = ""
) -> dict[str, str]:
    """Reads the notification secrets without placing them in argv.

    A terminal is prompted without echo; otherwise each secret is one line
    of standard input, the bot token first and the inbound passcode second.

    Args:
        chat: Telegram chat id the notifications go to.
        answering: Whether to read and store the inbound passcode as well.
        users: Comma-separated Telegram user ids allowed to answer, empty
            to allow the chat itself.

    Returns:
        The settings to store, keyed by environment variable name.

    Raises:
        BridgeError: If the token is empty, the passcode is too short, or
            inbound answers are asked for in a group chat without users.
    """
    if answering and chat.strip().startswith("-") and not users.strip():
        raise BridgeError(
            "A group chat needs --users with the Telegram user ids allowed "
            "to answer; nothing stored."
        )
    token = secret("Telegram bot token: ")
    if not token:
        raise BridgeError("The Telegram bot token is empty; nothing stored.")
    values = {
        "AGENT_PARLEY_NOTIFY": "telegram",
        "AGENT_PARLEY_TELEGRAM_CHAT": chat.strip(),
        "AGENT_PARLEY_TELEGRAM_TOKEN": token,
    }
    if users.strip():
        values["AGENT_PARLEY_TELEGRAM_USERS"] = users.strip()
    if answering:
        passcode = secret("Inbound passcode: ")
        if len(passcode) < inbound.MINIMUM_PASSCODE:
            raise BridgeError(
                "The inbound passcode must be at least "
                f"{inbound.MINIMUM_PASSCODE} characters; nothing stored."
            )
        values["AGENT_PARLEY_INBOUND"] = "telegram"
        values["AGENT_PARLEY_INBOUND_PASSCODE"] = passcode
    return values


def webhook_settings(
    current: dict[str, str], answering: bool
) -> dict[str, str]:
    """Reads a webhook URL and signing secret without placing them in argv.

    A webhook URL is a bearer secret, so it is read like the bot token: from
    a prompt without echo, or as the first line of standard input, with the
    optional signing secret on the second. The webhook is added to the
    transports already stored, so a Telegram setup keeps notifying.

    Args:
        current: Settings already stored in the state root.
        answering: Whether inbound answering was requested, which only
            Telegram carries.

    Returns:
        The settings to store, keyed by environment variable name.

    Raises:
        BridgeError: If inbound answering is requested, or the URL is empty
            or not allowed.
    """
    if answering:
        raise BridgeError(
            "Inbound answering is Telegram only; run `agent-parley notify "
            "setup --chat CHAT_ID --inbound`."
        )
    url = secret("Webhook URL: ")
    fault = notify.webhook_fault(url)
    if fault:
        raise BridgeError(fault + " Nothing stored.")
    names = [
        name.strip()
        for name in current.get("AGENT_PARLEY_NOTIFY", "").split(",")
        if name.strip() and name.strip() != "webhook"
    ]
    return {
        "AGENT_PARLEY_NOTIFY": ",".join([*names, "webhook"]),
        "AGENT_PARLEY_WEBHOOK_URL": url,
        "AGENT_PARLEY_WEBHOOK_SECRET": secret(
            "Webhook signing secret (empty for none): "
        ),
    }


def declare(parser: argparse.ArgumentParser, commands: CommandIndex) -> None:
    """Declares every command and renders the root help's listing.

    The declarations run on every invocation because the root listing names
    each command and its one-line help. What they produce is what changes:
    the index builds an argparse parser for the command that was typed and
    absorbs the arguments of the rest, so an invocation pays for one command
    rather than for forty.

    Args:
        parser: Root parser whose epilog carries the command listing.
        commands: Index the subcommands are declared through.
    """
    completing = commands.add_parser(
        "completion",
        help="Print a shell completion script for this command.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Install it for the current shell session:\n"
            '  bash: eval "$(agent-parley completion bash)"\n'
            '  zsh:  eval "$(agent-parley completion zsh)"\n'
            "  fish: agent-parley completion fish | source"
        ),
    )
    completing.add_argument(
        "shell",
        choices=completion.SHELLS,
        help="Shell to print the script for.",
    )
    candidates = commands.add_parser("__complete", help=argparse.SUPPRESS)
    candidates.add_argument(
        "kind",
        choices=completion.KINDS,
        help="Kind of value to list from local state.",
    )
    released = commands.add_parser(
        "version",
        help="Print the installed version and the state directory in use.",
    )
    released.add_argument("--json", action="store_true", help=JSON_HELP)
    commands.add_parser(
        "demo",
        help=(
            "Run the coordination story with stub lanes in a throwaway "
            "sandbox; no native CLI, model or network."
        ),
        description=(
            "Run the coordination story with stub lanes in a throwaway "
            "sandbox: lanes claim, collide, are refused, hand off and appear "
            "in top. No native CLI, model, account or network is used, and "
            "your state and repositories are never touched. It takes about "
            "30 seconds; press q or Ctrl-C to stop early, and the sandbox is "
            "removed either way."
        ),
    )
    starting = commands.add_parser(
        "up", help="Start the local coordination server in the background."
    )
    starting.add_argument("--json", action="store_true", help=JSON_HELP)
    starting.add_argument(
        "--workspace",
        metavar="NAME",
        default="",
        help=(
            "Also resume, each in a detached launcher, the stopped lanes of "
            "every project this recorded workspace groups."
        ),
    )
    listing = commands.add_parser(
        "projects",
        help="List registered projects, most recently used first.",
    )
    listing.add_argument("--json", action="store_true", help=JSON_HELP)
    grouping = commands.add_parser(
        "workspace",
        help="Record, list or forget named groups of projects.",
    )
    groups = grouping.add_subparsers(dest="action")
    groups.add_parser("list", help="List every recorded workspace.")
    recording = groups.add_parser(
        "set", help="Record a workspace, replacing one of the same name."
    )
    recording.add_argument("name", help="Workspace name.")
    recording.add_argument(
        "projects",
        nargs="+",
        metavar="PROJECT",
        help="Project name `projects` lists, or its root path.",
    )
    forgetting = groups.add_parser(
        "remove", help="Forget a workspace; its projects stay registered."
    )
    forgetting.add_argument("name", help="Workspace name.")
    stopping = commands.add_parser(
        "down",
        help="Stop the coordination server; retain all data and worktrees.",
    )
    stopping.add_argument("--json", action="store_true", help=JSON_HELP)
    health = commands.add_parser(
        "status",
        help=(
            "Show this project's open work and the service's health; "
            "`top` draws the live dashboard and `watch` follows one lane."
        ),
    )
    health.add_argument("--json", action="store_true", help=JSON_HELP)
    add_status_filters(health)
    health.add_argument(
        "--all",
        action="store_true",
        help=(
            "Also list claims whose issue or pull request ended on the forge, "
            "and lanes dead past the project's fold_after with no claim."
        ),
    )
    health.add_argument(
        "--all-projects",
        action="store_true",
        help=(
            "Report every project, not only the one the working directory "
            "belongs to, with dormant projects listed last."
        ),
    )
    health.add_argument(
        "--workspace",
        metavar="NAME",
        default="",
        help="Report every project this recorded workspace groups.",
    )
    health.add_argument(
        "--table",
        action="store_true",
        help=(
            "Print the full per-lane table instead of the open-work view. "
            "Any lane filter or a named participant prints it too."
        ),
    )
    commands.add_parser(
        "title",
        help=(
            "Print the current lane's name, state and claim progress for "
            "a native status line; prints nothing outside a lane."
        ),
    )
    watch = commands.add_parser(
        "top",
        help=(
            "Draw the dashboard of every participant's live coordination "
            "state; `status` prints one table and `watch` follows one "
            "lane's events as a stream."
        ),
    )
    watch.add_argument(
        "--json",
        action="store_true",
        help="Print one JSON frame and exit instead of drawing a live view.",
    )
    watch.add_argument(
        "--once",
        action="store_true",
        help="Print one plain snapshot instead of drawing a live view.",
    )
    watch.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="Seconds between redraws of the live view.",
    )
    watch.add_argument(
        "--serve",
        type=int,
        nargs="?",
        const=0,
        metavar="PORT",
        help=(
            "Serve a read-only web page, its JSON frame and Prometheus "
            "metrics on 127.0.0.1 at PORT, a free one when omitted, "
            "behind a random path token the command prints."
        ),
    )
    watch.add_argument(
        "--provider",
        action="append",
        metavar="NAME",
        help=(
            "Report only participants driven by this provider. Repeat the "
            "flag to report several."
        ),
    )
    watch.add_argument(
        "--since",
        type=duration,
        default=0.0,
        metavar="WINDOW",
        help=(
            "Count only enforcement history inside this window, such as 45m, "
            "6h or 7d. The whole retained log is counted by default."
        ),
    )
    watch.add_argument(
        "--sort",
        metavar="COLUMN",
        help=(
            "Order rows by a column, such as IDLE or DENIALS. Counted "
            "columns order from the largest value down."
        ),
    )
    watch.add_argument(
        "--reverse",
        action="store_true",
        help="Reverse the reported order.",
    )
    watch.add_argument(
        "--repo",
        dest="project",
        action="append",
        metavar="ROOT",
        help=(
            "Report only this repository, by path or by directory name. "
            "Repeat the flag to report several."
        ),
    )
    watch.add_argument(
        "--project",
        dest="project",
        action="append",
        metavar="ROOT",
        help=argparse.SUPPRESS,
    )
    watch.add_argument(
        "--participant",
        action="append",
        metavar="NAME",
        help=(
            "Report only this participant. Repeat the flag to report several."
        ),
    )
    watch.add_argument(
        "--columns",
        metavar="LIST",
        help=(
            "Show only these columns, comma separated, such as "
            "PARTICIPANT,STATE,IDLE, or all for every column. The default "
            "leaves out PROVIDER, EVENT, REVIEW, CONTEXT, CALLS, UNUSED and "
            "FIT."
        ),
    )
    watch.add_argument(
        "--no-operator-edits",
        action="store_true",
        help=(
            "Skip reading the base checkout for operator edits on reserved "
            "paths, for a repository whose base checkout is always dirty."
        ),
    )
    watch.add_argument(
        "--all",
        dest="everything",
        action="store_true",
        help=(
            "Also show stopped lanes that hold nothing and projects whose "
            "root is gone. The view counts them in its header otherwise."
        ),
    )
    measured = commands.add_parser(
        "metrics",
        help="Print the counters and gauges the live view computes.",
    )
    measured.add_argument(
        "--json",
        action="store_true",
        help=(
            "Print the same values as one JSON document instead of the "
            "Prometheus text exposition format."
        ),
    )
    measured.add_argument(
        "--output",
        type=Path,
        metavar="PATH",
        help=(
            "Write the frame to this file, replaced atomically, instead of "
            "printing it."
        ),
    )
    measured.add_argument(
        "--every",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help="Rewrite the file on this interval until interrupted.",
    )
    measured.add_argument(
        "--provider",
        action="append",
        metavar="NAME",
        help=(
            "Report only participants driven by this provider. Repeat the "
            "flag to report several."
        ),
    )
    measured.add_argument(
        "--since",
        type=duration,
        default=0.0,
        metavar="WINDOW",
        help=(
            "Count only enforcement history inside this window, such as 45m, "
            "6h or 7d. The whole retained log is counted by default."
        ),
    )
    readings = measured.add_subparsers(dest="reading")
    delivered = readings.add_parser(
        "throughput",
        help=(
            "Report what each project and lane delivered since an instant: "
            "merged issues, lane-hours, tokens, operator touches and waits."
        ),
    )
    delivered.add_argument(
        "--since",
        dest="start",
        type=instant,
        required=True,
        metavar="TIME",
        help=(
            "Start of the window, such as 2026-10-15T09:01:00Z, or a window "
            "counted back from now, such as 7d."
        ),
    )
    delivered.add_argument("--json", action="store_true", help=JSON_HELP)
    follow = commands.add_parser(
        "watch",
        help=(
            "Follow one lane's coordination events as a stream; `top` draws "
            "the dashboard, and the agent's conversation is never shown."
        ),
    )
    follow.add_argument("participant", help="Participant name to follow.")
    follow.add_argument("--repo", type=Path, default=Path.cwd(), help=REPO_HELP)
    follow.add_argument(
        "--json",
        action="store_true",
        help="Print JSON Lines, one event object per line.",
    )
    follow.add_argument(
        "--kind",
        action="append",
        choices=stream.KINDS,
        metavar="KIND",
        help=(
            "Print only this kind of event: "
            + ", ".join(stream.KINDS)
            + ". Repeat the flag to print several."
        ),
    )
    follow.add_argument(
        "--since",
        type=duration,
        default=0.0,
        metavar="WINDOW",
        help=(
            "Start from every event inside this window, such as 45m or 1h, "
            "instead of the most recent twenty."
        ),
    )
    follow.add_argument(
        "--interval",
        type=float,
        default=stream.INTERVAL,
        help="Seconds between reads of the store and the event log.",
    )
    past = commands.add_parser(
        "history",
        help="Read the recorded history of an issue, a lane or a claim.",
    )
    subjects = past.add_subparsers(dest="subject")
    for subject, argument, summary in (
        ("issue", "number", "List every record that touched an issue."),
        ("participant", "name", "List everything one lane filed."),
        (
            "claim",
            "claim_id",
            "Follow one claim to the pull request that ended it.",
        ),
    ):
        listing = subjects.add_parser(subject, help=summary)
        listing.add_argument(argument, help=f"The {subject} to read.")
        listing.add_argument(
            "--repo", type=Path, default=Path.cwd(), help=REPO_HELP
        )
        listing.add_argument("--json", action="store_true", help=JSON_HELP)
        listing.add_argument(
            "--kind",
            action="append",
            choices=history.KINDS,
            metavar="KIND",
            help=(
                "Report only this kind of record: "
                + ", ".join(history.KINDS)
                + ". Repeat the flag to report several."
            ),
        )
        listing.add_argument(
            "--participant",
            default="",
            help="Report only records this lane filed.",
        )
        listing.add_argument(
            "--provider",
            default="",
            help="Report only records of lanes on this provider.",
        )
        listing.add_argument(
            "--issue", default="", help="Report only records of this issue."
        )
        listing.add_argument(
            "--output",
            type=Path,
            metavar="PATH",
            help=(
                "Export the reading to this file as one JSON document "
                "instead of printing it, as `events` and `state` export."
            ),
        )
        listing.add_argument(
            "--since",
            type=duration,
            default=0.0,
            metavar="WINDOW",
            help=(
                "Report only records inside this window, such as 45m, 6h or "
                "7d. Everything recorded is reported by default."
            ),
        )
    events = commands.add_parser(
        "events", help="Export retained enforcement history for a repository."
    )
    records = events.add_subparsers(dest="action", required=True)
    for action in ("export", "show"):
        export = records.add_parser(
            action,
            help=(
                "Write the retained records as JSON Lines."
                if action == "export"
                else "Print the retained records on standard output."
            ),
        )
        export.add_argument(
            "--repo", type=Path, default=Path.cwd(), help=REPO_HELP
        )
        export.add_argument(
            "--participant",
            action="append",
            metavar="NAME",
            help=(
                "Export only this participant. Repeat the flag to export "
                "several; every participant is exported by default."
            ),
        )
        export.add_argument(
            "--since",
            type=duration,
            default=0.0,
            metavar="WINDOW",
            help=(
                "Export only records inside this window, such as 45m, 6h or "
                "7d. Everything still retained is exported by default."
            ),
        )
        if action == "export":
            export.add_argument(
                "--output",
                type=Path,
                help=(
                    "Destination file; JSON Lines go to standard output "
                    "otherwise."
                ),
            )
    archived = commands.add_parser(
        "state",
        help="Export, inspect or import the coordination state as one archive.",
    )
    archives = archived.add_subparsers(dest="action", required=True)
    exporting = archives.add_parser(
        "export", help="Write the state, or one project, as a tar archive."
    )
    exporting.add_argument(
        "--output", type=Path, required=True, help="Archive path to create."
    )
    exporting.add_argument(
        "--project",
        type=Path,
        metavar="ROOT",
        help="Export only the project registered for this checkout.",
    )
    showing = archives.add_parser(
        "show", help="List what an archive holds without importing it."
    )
    showing.add_argument(
        "archive", type=Path, help="Archive written by state export."
    )
    showing.add_argument("--json", action="store_true", help=JSON_HELP)
    importing = archives.add_parser(
        "import", help="Restore an archive into the state directory."
    )
    importing.add_argument(
        "archive", type=Path, help="Archive written by state export."
    )
    importing.add_argument(
        "--project",
        type=Path,
        metavar="ROOT",
        help="Restore only the archived project registered for this root.",
    )
    importing.add_argument(
        "--merge",
        action="store_true",
        help="Add archived projects beside existing state.",
    )
    storing_state = commands.add_parser(
        "store", help="Check or repair the coordination store."
    )
    store_actions = storing_state.add_subparsers(dest="action", required=True)
    store_actions.add_parser(
        "repair",
        help=(
            "Keep a damaged store aside as <name>.corrupt-<time> and create "
            "an empty one."
        ),
    )
    setup = commands.add_parser(
        "setup",
        help="Register a repository for coordination from committed HEAD.",
    )
    setup.add_argument(
        "repo",
        type=Path,
        help="Repository root or any of its worktrees to register.",
    )
    setup.add_argument(
        "--verify",
        dest="verify_command",
        metavar="COMMAND",
        help=(
            "Also record the command every merge must pass, as "
            "`verify set` does and under the same guards."
        ),
    )
    setup.add_argument(
        "--init",
        dest="init_command",
        metavar="COMMAND",
        help=(
            "Also record the command every new lane runs before it starts, "
            "as `init set` does and under the same guards."
        ),
    )
    setup.add_argument("--json", action="store_true", help=JSON_HELP)
    run = commands.add_parser(
        "run", help="Launch one participant's native CLI in this terminal."
    )
    run.add_argument(
        "participant",
        help="Participant name; a new name creates its own worktree lane.",
    )
    run.add_argument(
        "--provider",
        help="Provider definition; defaults to the participant name.",
    )
    run.add_argument(
        "--credentials", help="Credential profile selecting one account."
    )
    target = run.add_mutually_exclusive_group()
    target.add_argument("--repo", type=Path, default=Path.cwd(), help=REPO_HELP)
    target.add_argument(
        "--project",
        metavar="NAME",
        default="",
        help=(
            "Registered project to launch in instead of the working "
            "directory's, by the name `projects` lists or its root."
        ),
    )
    run.add_argument(
        "--resume",
        action="store_true",
        help=(
            "Resume the lane's last recorded native session instead of "
            "starting a new one."
        ),
    )
    run.add_argument(
        "--task",
        default="Check shared coordination state and await my task.",
        help=(
            "Opening prompt for the session, not an issue; use --issue to "
            "claim one."
        ),
    )
    run.add_argument(
        "--issue",
        default="",
        help=(
            "Claim this issue for the lane and open with a bounded digest of "
            "it fetched from the forge; replaces --task."
        ),
    )
    run.add_argument(
        "--blueprint",
        default="",
        help=(
            "Run the --issue claim through this recorded blueprint; without "
            "it, a default mapped to one of the issue's labels applies. "
            "Base checkout only."
        ),
    )
    run.add_argument("--json", action="store_true", help=JSON_HELP)
    report = commands.add_parser(
        "report",
        help=(
            "Record a partial, blocked, ready-for-review or waiting handoff."
        ),
    )
    report.add_argument("--repo", type=Path, default=Path.cwd(), help=REPO_HELP)
    report.add_argument(
        "--state",
        choices=("partial", "blocked", "ready", "waiting"),
        help="Where the work stands.",
    )
    report.add_argument(
        "--summary", default="", help="One line on what was done."
    )
    records = report.add_subparsers(dest="action")
    showing_report = records.add_parser(
        "show", help="Print one recorded report of this lane."
    )
    showing_report.add_argument("report_id", help="Report to print.")
    showing_report.add_argument(
        "--repo", type=Path, default=Path.cwd(), help=REPO_HELP
    )
    showing_report.add_argument(
        "--full",
        action="store_true",
        help="Print the whole attached evidence after the record.",
    )
    showing_report.add_argument("--json", action="store_true", help=JSON_HELP)
    usage_report = records.add_parser(
        "usage",
        help=(
            "List what each resolved claim used: tokens, calls, hours and "
            "its pull request outcome."
        ),
    )
    usage_report.add_argument(
        "--repo", type=Path, default=Path.cwd(), help=REPO_HELP
    )
    usage_report.add_argument(
        "--since",
        type=duration,
        default=0.0,
        metavar="WINDOW",
        help="Report only claims resolved inside this window, such as 7d.",
    )
    usage_report.add_argument("--json", action="store_true", help=JSON_HELP)
    reviewing_report = records.add_parser(
        "review",
        help="Record this lane's verdict on another lane's report.",
    )
    reviewing_report.add_argument("report_id", help="Report to review.")
    reviewing_report.add_argument(
        "--repo", type=Path, default=Path.cwd(), help=REPO_HELP
    )
    reviewing_report.add_argument(
        "--verdict",
        choices=metrics.VERDICTS,
        required=True,
        help=(
            "What this lane found. The verdict is this lane's own claim "
            "about work it did not do, not independent verification."
        ),
    )
    reviewing_report.add_argument(
        "--evidence",
        default="",
        help="What was checked; longer evidence is attached as a report's is.",
    )
    reviewing_report.add_argument("--json", action="store_true", help=JSON_HELP)
    report.add_argument(
        "--remaining", default="", help="What is still left to do."
    )
    report.add_argument(
        "--evidence",
        default="",
        help="What was checked; longer evidence is attached to the report.",
    )
    report.add_argument(
        "--issue",
        default="",
        metavar="NUMBER",
        help="Bind this report to one exact owned issue.",
    )
    report.add_argument(
        "--resume-on",
        default="",
        metavar="NUMBER",
        help=(
            "For a blocked report, resume automatically after this existing "
            "authorized issue completes."
        ),
    )
    report.add_argument(
        "--backlog",
        type=int,
        default=None,
        metavar="COUNT",
        help=(
            "Work units still remaining on this claim, in whatever it counts: "
            "issue families, files, subtasks. Recording the count lets the "
            "supervisor offer a split once this lane goes idle on the claim."
        ),
    )
    report.add_argument(
        "--until",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help=(
            "Seconds until this lane's next check, required for "
            "--state waiting. Capped by the project's wait deadline."
        ),
    )
    report.add_argument(
        "--pid",
        type=int,
        default=None,
        metavar="PID",
        help=(
            "Process still doing this report's work; while it runs the "
            "supervisor holds the reported claim."
        ),
    )
    report.add_argument(
        "--output-file",
        default="",
        metavar="PATH",
        help=(
            "File the running work writes; a write after this report holds "
            "the reported claim while it is recent."
        ),
    )
    report.add_argument(
        "--idempotency-key", default="", metavar="KEY", help=RETRY_HELP
    )
    steer = commands.add_parser(
        "say",
        help=(
            "Send one lane a coordination message as the operator; "
            "`mail send` is a compatibility alias."
        ),
    )
    add_say_arguments(steer)
    asking = commands.add_parser(
        "ask",
        help=(
            "Ask a lane a side question from a separate read-only session; "
            "the lane's session and context are untouched."
        ),
    )
    asking.add_argument("participant", help="Lane the question is about.")
    asking.add_argument("question", help="The question to answer.")
    asking.add_argument("--repo", type=Path, default=Path.cwd(), help=REPO_HELP)
    asking.add_argument(
        "--checkpoint",
        action="store_true",
        help=(
            "Answer from the lane's checkpoint even where the provider can "
            "fork the lane's session."
        ),
    )
    issue = commands.add_parser(
        "issue", help="Claim issues and explicitly hand off ownership."
    )
    actions = issue.add_subparsers(dest="action", required=True)
    for action, summary in (
        ("list", "Show claims, dependencies and handoff offers."),
        ("claim", "Claim an available issue from this lane."),
        ("release", "Release ownership without closing the issue."),
        ("offer", "Pause work and offer ownership to another lane."),
        ("accept", "Accept the offer addressed to this lane."),
        ("decline", "Decline the offer addressed to this lane."),
        ("cancel", "Cancel this lane's pending handoff offer."),
        ("block", "Record an advisory dependency on another issue."),
        ("unblock", "Remove a recorded issue dependency."),
        ("request", "Ask the holder of an issue to hand it to this lane."),
    ):
        command = actions.add_parser(action, help=summary)
        command.add_argument(
            "--repo", type=Path, default=Path.cwd(), help=REPO_HELP
        )
        if action == "list":
            command.add_argument("--json", action="store_true", help=JSON_HELP)
        else:
            command.add_argument("number", help="Issue number.")
            command.add_argument(
                "--idempotency-key",
                default="",
                metavar="KEY",
                help=RETRY_HELP,
            )
        if action in ("claim", "offer", "accept", "request"):
            command.add_argument(
                "--within",
                type=duration,
                metavar="WINDOW",
                help=(
                    "Record a deadline for this work, such as 45m, 6h or 7d. "
                    "Past it the record reads overdue and states the seconds "
                    "over; ownership never moves on a deadline."
                ),
            )
        if action == "claim":
            command.add_argument(
                "--take-orphaned",
                action="store_true",
                help=(
                    "Take an issue whose owner the supervisor marked "
                    "orphaned, recording that owner and the reason. It "
                    "moves that claim's reservations to the new owner; a "
                    "lane that is merely idle is never orphaned."
                ),
            )
        if action == "offer":
            command.add_argument(
                "--to", required=True, help="Lane the ownership is offered to."
            )
            command.add_argument(
                "--summary",
                required=True,
                help="Where the work stands, for the receiving lane.",
            )
            command.add_argument(
                "--remaining",
                action="append",
                default=[],
                metavar="ITEM",
                help=(
                    "One item of work still to do, repeatable. It is recorded "
                    "beside the head commit, the reservations this lane holds "
                    "and the diff against the project base, so the receiver "
                    "reads the transfer instead of re-deriving it."
                ),
            )
            command.add_argument(
                "--when-released",
                default="",
                metavar="NUMBER",
                help=(
                    "Record the offer and apply it once this issue is "
                    "explicitly released or its pull request is recorded as "
                    "ended."
                ),
            )
        if action == "request":
            command.add_argument(
                "--summary",
                default="",
                metavar="REASON",
                help=(
                    "Why this lane asks to take the issue over. The holder "
                    "answers with issue accept or decline; a holder that "
                    "neither answers nor records progress within the "
                    "project's takeover_grace has the request granted as an "
                    "offer to this lane."
                ),
            )
        if action in ("accept", "decline"):
            command.add_argument(
                "--offer-id",
                required=True,
                help="Offer being answered, as issue list prints it.",
            )
        if action in ("block", "unblock"):
            command.add_argument(
                "--on",
                required=True,
                metavar="NUMBER",
                help="Issue this one depends on.",
            )
    recovering = actions.add_parser(
        "recover",
        help=(
            "Authorize stopping the current live owner when a matching "
            "capacity observation is later published."
        ),
    )
    recovering.add_argument("number", help="Issue number.")
    recovering.add_argument(
        "--repo", type=Path, default=Path.cwd(), help=REPO_HELP
    )
    recovering.add_argument(
        "--reason",
        required=True,
        help="Operator rationale persisted with this exact claim approval.",
    )
    recovering.add_argument("--json", action="store_true", help=JSON_HELP)
    resolving = actions.add_parser(
        "resolve",
        help=(
            "End a claim whose holder never filed the completion its pull "
            "request already landed, recording the forge evidence."
        ),
    )
    resolving.add_argument("number", help="Issue number.")
    resolving.add_argument(
        "--repo", type=Path, default=Path.cwd(), help=REPO_HELP
    )
    resolving.add_argument(
        "--reason",
        default="",
        metavar="TEXT",
        help="Operator rationale kept beside the forge evidence.",
    )
    resolving.add_argument(
        "--release",
        action="store_true",
        help=(
            "Return the work to the queue instead of recording it complete. "
            "It is required when the pull request was closed without merging, "
            "because nothing was integrated."
        ),
    )
    resolving.add_argument("--json", action="store_true", help=JSON_HELP)
    choosing = actions.add_parser(
        "next",
        help=(
            "Rank the unclaimed issues this lane could take next, with the "
            "reason for each; it claims nothing."
        ),
    )
    choosing.add_argument(
        "--repo", type=Path, default=Path.cwd(), help=REPO_HELP
    )
    choosing.add_argument("--json", action="store_true", help=JSON_HELP)
    choosing.add_argument(
        "--limit",
        type=int,
        default=recommend.MAX_SHORTLIST,
        metavar="COUNT",
        help="Most candidates to list; five when omitted.",
    )
    matching = actions.add_parser(
        "match",
        help=(
            "List the open issues a stated goal already describes, so work "
            "the forge tracks is claimed rather than opened twice."
        ),
    )
    matching.add_argument(
        "--repo", type=Path, default=Path.cwd(), help=REPO_HELP
    )
    matching.add_argument("--json", action="store_true", help=JSON_HELP)
    matching.add_argument(
        "goal",
        help="What this lane intends to do, in your own words.",
    )
    matching.add_argument(
        "--limit",
        type=int,
        default=recommend.MAX_SHORTLIST,
        metavar="COUNT",
        help="Most matches to list; five when omitted.",
    )
    assigning = actions.add_parser(
        "assign",
        help="Offer an issue to a lane as the operator, or withdraw it.",
    )
    assigning.add_argument(
        "--repo", type=Path, default=Path.cwd(), help=REPO_HELP
    )
    assigning.add_argument("number", help="Issue number.")
    assigning.add_argument(
        "name",
        nargs="?",
        default="",
        help="Lane the issue is offered to.",
    )
    assigning.add_argument(
        "--reason",
        default="",
        metavar="TEXT",
        help=(
            "Why the work is moving. It travels with the offer and is kept "
            "on the record."
        ),
    )
    assigning.add_argument(
        "--unassign",
        action="store_true",
        help=(
            "Withdraw an operator offer no lane has accepted. An offer that "
            "was accepted is refused, naming the lane that holds the issue."
        ),
    )
    add_selector(assigning)
    inspecting = actions.add_parser(
        "show",
        help=(
            "Print one issue with its owner, deadline, blockers, pending "
            "offer, held reservations and recorded history."
        ),
    )
    inspecting.add_argument("number", help="Issue number.")
    inspecting.add_argument(
        "--repo", type=Path, default=Path.cwd(), help=REPO_HELP
    )
    inspecting.add_argument("--json", action="store_true", help=JSON_HELP)
    following = commands.add_parser(
        "followup",
        help="Review follow-up work lanes suggested, and file what you accept.",
    )
    followup_actions = following.add_subparsers(dest="action", required=True)
    listing_followups = followup_actions.add_parser(
        "list",
        help="List pending suggestions with the open issues they duplicate.",
    )
    accepting = followup_actions.add_parser(
        "accept",
        help=(
            "File one suggestion as an issue in the active milestone and "
            "offer it to an idle lane."
        ),
    )
    accepting.add_argument("id", help="Suggestion identifier, such as f3.")
    accepting.add_argument(
        "--milestone",
        default="",
        metavar="TITLE",
        help="Milestone to file under; the active open milestone if omitted.",
    )
    accepting.add_argument(
        "--to",
        default="",
        metavar="LANE",
        help="Lane to offer the issue to; an idle running lane if omitted.",
    )
    for followup_parser in (listing_followups, accepting):
        followup_parser.add_argument(
            "--repo", type=Path, default=Path.cwd(), help=REPO_HELP
        )
        followup_parser.add_argument(
            "--json", action="store_true", help=JSON_HELP
        )
    checking = commands.add_parser(
        "doctor",
        help="Report launcher, plugin and store versions and their fit.",
    )
    checking.add_argument("--json", action="store_true", help=JSON_HELP)
    plugging = commands.add_parser(
        "plugins",
        help="Add the plugin to each supported native CLI on PATH.",
    )
    plugin_actions = plugging.add_subparsers(dest="action", required=True)
    plugin_actions.add_parser(
        "install",
        help=(
            "Add the marketplace and plugin to claude and codex where "
            "missing, refreshing what is already present."
        ),
    )
    plugin_actions.add_parser(
        "status",
        help="Report whether each supported CLI has the plugin; writes none.",
    )
    triaging = commands.add_parser(
        "problems",
        help=(
            "List every lane, claim and store condition that needs an "
            "operator, oldest first; exit 1 when there is any."
        ),
    )
    triaging.add_argument("--json", action="store_true", help=JSON_HELP)
    triaging.add_argument(
        "--ack-after",
        type=float,
        default=0.0,
        help=(
            "Seconds a message may await acknowledgement before it is "
            "listed; the project's stalled_after when omitted."
        ),
    )
    triaging.add_argument(
        "--all",
        action="store_true",
        help=(
            "Also list the rows of lanes dead past the project's fold_after "
            "that are older than it, which one line otherwise counts."
        ),
    )
    triage = triaging.add_subparsers(dest="action")
    acking = triage.add_parser(
        "ack",
        help=(
            "Record your own acknowledgement of one message a lane has left "
            "unanswered, clearing that condition and nothing else."
        ),
    )
    acking.add_argument(
        "message_id", type=int, help="Message to acknowledge, as listed."
    )
    acking.add_argument("--repo", type=Path, default=Path.cwd(), help=REPO_HELP)
    acking.add_argument("--json", action="store_true", help=JSON_HELP)
    collecting = commands.add_parser(
        "gc",
        aliases=["reclaim"],
        help=(
            "Reclaim the lane worktrees and branches whose work has landed, "
            "keeping and reporting every lane that still holds any; "
            "`reclaim` is a compatibility alias."
        ),
    )
    collecting.add_argument(
        "--repo", type=Path, default=Path.cwd(), help=REPO_HELP
    )
    sweeping = collecting.add_mutually_exclusive_group()
    sweeping.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Remove the reclaimable lanes; without it the sweep only reports "
            "what it would remove and what it would keep."
        ),
    )
    sweeping.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Only report what would be removed and what would be kept, with "
            "each worktree's size on disk; the default."
        ),
    )
    sweeping.add_argument(
        "--preserved",
        action="store_true",
        help=(
            "List the recovery checkpoints written before a sweep removed "
            "or rebuilt a worktree, with each one's source path and age."
        ),
    )
    sweeping.add_argument(
        "--restore",
        default="",
        metavar="ID",
        help=(
            "Recreate the worktree a preserved checkpoint holds, its "
            "branch head and staged and unstaged changes, at --to PATH."
        ),
    )
    collecting.add_argument(
        "--to",
        type=Path,
        metavar="PATH",
        help="With --restore, the path the worktree is recreated at.",
    )
    collecting.add_argument(
        "--force",
        action="store_true",
        help=(
            "With --apply, also remove worktrees a lane made itself that "
            "are kept only for uncommitted changes, unpushed commits or a "
            "recent change, after writing a recovery checkpoint of each."
        ),
    )
    collecting.add_argument(
        "--project",
        default="",
        metavar="ROOT",
        help=(
            "Instead of sweeping lanes, remove the private state of the "
            "project whose root checkout ROOT is gone, once the service "
            "retired its lanes and no session of it runs; its recovery "
            "checkpoints move to the state root's recovered folder."
        ),
    )
    collecting.add_argument(
        "--yes",
        action="store_true",
        help="With --project, delete the recovery checkpoints too.",
    )
    collecting.add_argument("--json", action="store_true", help=JSON_HELP)
    notifying = commands.add_parser(
        "notify",
        help=(
            "Store the notification settings, or verify the configured "
            "transports; exit 1 when one of them refuses the message."
        ),
    )
    notices = notifying.add_subparsers(dest="action", required=True)
    probing = notices.add_parser(
        "test",
        help=(
            "Send one test message on each configured transport and say "
            "whether the service is reading inbound queries."
        ),
    )
    probing.add_argument(
        "--repo", type=Path, default=Path.cwd(), help=REPO_HELP
    )
    probing.add_argument("--json", action="store_true", help=JSON_HELP)
    storing = notices.add_parser(
        "setup",
        help=(
            "Store the Telegram chat and bot token, or a webhook URL, in the "
            "state root, owner-only; secrets are read from a prompt or "
            "standard input."
        ),
    )
    destination = storing.add_mutually_exclusive_group(required=True)
    destination.add_argument("--chat", help="Telegram chat id.")
    destination.add_argument(
        "--webhook",
        action="store_true",
        help=(
            "Post to a Slack, Discord, Teams or other HTTPS webhook; the URL "
            "and an optional signing secret are read after it."
        ),
    )
    storing.add_argument(
        "--inbound",
        action="store_true",
        help=(
            "Also answer status queries from the chat; the passcode is read "
            "after the token."
        ),
    )
    storing.add_argument(
        "--users",
        default="",
        help=(
            "Comma-separated Telegram user ids allowed to answer; required "
            "with --inbound for a group chat."
        ),
    )
    planning = commands.add_parser(
        "plan", help="Apply, compare or show the recorded work-order plan."
    )
    steps = planning.add_subparsers(dest="action", required=True)
    for action, summary in (
        ("apply", "Record a TOML work order as advisory dependencies."),
        ("diff", "Preview what applying a TOML work order would change."),
        ("show", "Print the applied plan as a tree with owners."),
    ):
        command = steps.add_parser(action, help=summary)
        command.add_argument(
            "--repo", type=Path, default=Path.cwd(), help=REPO_HELP
        )
        if action != "show":
            command.add_argument(
                "path", type=Path, help="TOML plan file to read."
            )
        if action != "apply":
            command.add_argument("--json", action="store_true", help=JSON_HELP)
        else:
            command.add_argument(
                "--force",
                action="store_true",
                help="Restore edges accepted revisions removed.",
            )
    revising = {
        "propose": "File a revision that adds or removes plan edges.",
        "approve": "Approve and apply one open revision as operator.",
        "reject": "Reject one open revision as operator.",
        "proposals": "List the plan version, envelope and revisions.",
    }
    for action, summary in revising.items():
        command = steps.add_parser(action, help=summary)
        command.add_argument(
            "--repo", type=Path, default=Path.cwd(), help=REPO_HELP
        )
        command.add_argument("--json", action="store_true", help=JSON_HELP)
        if action in ("approve", "reject"):
            command.add_argument(
                "proposal_id", help="Revision, as plan proposals lists it."
            )
            command.add_argument(
                "--reason", default="", help="Rationale kept with the verdict."
            )
        elif action == "propose":
            command.add_argument(
                "--base",
                type=int,
                required=True,
                help="Plan version read from `plan proposals`.",
            )
            for edge in ("add", "remove"):
                command.add_argument(
                    f"--{edge}",
                    action="append",
                    default=[],
                    metavar="ISSUE:BLOCKER",
                    help=f"Dependency edge to {edge}; repeatable.",
                )
            command.add_argument(
                "--reason", required=True, help="Why the plan should change."
            )
            command.add_argument(
                "--evidence",
                action="append",
                default=[],
                help="What supports the change; repeatable.",
            )
    updating = steps.add_parser(
        "update",
        help="Replace this lane's own task list, which only the operator sees.",
    )
    updating.add_argument(
        "--repo", type=Path, default=Path.cwd(), help=REPO_HELP
    )
    updating.add_argument(
        "step",
        nargs="*",
        metavar="STATUS:TEXT",
        help=(
            "One step, where STATUS is pending, in_progress or completed; "
            "give the whole list each time."
        ),
    )
    updating.add_argument(
        "--stdin",
        action="store_true",
        help='Read the list as JSON, [{"step": "...", "status": "..."}].',
    )
    mail = commands.add_parser(
        "mail",
        help=(
            "Read mail, or list and cancel pending operator items; "
            "`mail send` is an alias of `say`."
        ),
    )
    letters = mail.add_subparsers(dest="action", required=True)
    reading = letters.add_parser(
        "thread", help="Read this lane's messages in one thread."
    )
    reading.add_argument("thread_id", help="Thread to read.")
    reading.add_argument(
        "--repo", type=Path, default=Path.cwd(), help=REPO_HELP
    )
    reading.add_argument(
        "--after-id",
        type=int,
        default=0,
        help="Read only messages after this message id.",
    )
    add_reader_argument(reading)
    reading.add_argument("--json", action="store_true", help=JSON_HELP)
    showing_mail = letters.add_parser(
        "show", help="Print one message you sent or received."
    )
    showing_mail.add_argument("message_id", type=int, help="Message to print.")
    showing_mail.add_argument(
        "--repo", type=Path, default=Path.cwd(), help=REPO_HELP
    )
    showing_mail.add_argument(
        "--full",
        action="store_true",
        help="Print the whole attachment after the stored body.",
    )
    add_reader_argument(showing_mail)
    showing_mail.add_argument("--json", action="store_true", help=JSON_HELP)
    finding = letters.add_parser("search", help="Search this lane's mail.")
    finding.add_argument("query", help="Text to search for.")
    finding.add_argument(
        "--repo", type=Path, default=Path.cwd(), help=REPO_HELP
    )
    finding.add_argument(
        "--limit",
        type=int,
        default=store.MAX_SEARCH_HITS,
        help="Most messages to list.",
    )
    add_reader_argument(finding)
    finding.add_argument("--json", action="store_true", help=JSON_HELP)
    inbox = letters.add_parser(
        "list",
        help=(
            "List this lane's own mail, newest first, without a search "
            "query to write."
        ),
    )
    inbox.add_argument("--repo", type=Path, default=Path.cwd(), help=REPO_HELP)
    inbox.add_argument(
        "--limit",
        type=int,
        default=store.MAX_SEARCH_HITS,
        help="Most messages to list.",
    )
    add_reader_argument(inbox)
    inbox.add_argument("--json", action="store_true", help=JSON_HELP)
    sending = letters.add_parser(
        "send",
        help=(
            "Compatibility alias of `say`, the canonical command; it takes "
            "the same arguments and sends the same message."
        ),
    )
    add_say_arguments(sending)
    waiting = letters.add_parser(
        "pending", help="List operator items recorded but not delivered."
    )
    waiting.add_argument(
        "--repo", type=Path, default=Path.cwd(), help=REPO_HELP
    )
    waiting.add_argument("--json", action="store_true", help=JSON_HELP)
    dropping = letters.add_parser(
        "cancel", help="Remove one recorded operator item before delivery."
    )
    dropping.add_argument(
        "item_id", type=int, help="Pending item, as mail pending lists it."
    )
    dropping.add_argument(
        "--repo", type=Path, default=Path.cwd(), help=REPO_HELP
    )
    dropping.add_argument("--json", action="store_true", help=JSON_HELP)
    deciding = commands.add_parser(
        "decide",
        help=(
            "Record one decision every lane of the project reads; "
            "`decision` queries the recorded ones."
        ),
    )
    deciding.add_argument("text", help="Decision text participants read.")
    deciding.add_argument(
        "--repo", type=Path, default=Path.cwd(), help=REPO_HELP
    )
    deciding.add_argument(
        "--subject", default="", help="Subject the decision is found under."
    )
    deciding.add_argument(
        "--key",
        default="",
        help=(
            "Idempotency key. Without one the key follows the text, so "
            "recording the same decision again records nothing further."
        ),
    )
    deciding.add_argument("--json", action="store_true", help=JSON_HELP)
    decision = commands.add_parser(
        "decision",
        help=(
            "Read the decisions recorded for this project; `decide` records "
            "a new one."
        ),
    )
    decision_actions = decision.add_subparsers(dest="action", required=True)
    decision_list = decision_actions.add_parser(
        "list", help="List or search the decisions recorded for this project."
    )
    decision_list.add_argument(
        "query",
        nargs="?",
        default="",
        help="Text to match; without it the newest decisions are listed.",
    )
    decision_list.add_argument(
        "--repo", type=Path, default=Path.cwd(), help=REPO_HELP
    )
    decision_list.add_argument(
        "--limit",
        type=int,
        default=store.MAX_SEARCH_HITS,
        help="Most decisions to list.",
    )
    decision_list.add_argument(
        "--since",
        type=duration,
        default=0.0,
        metavar="WINDOW",
        help=(
            "Age a reported decision may reach, such as 45m, 6h or 7d. The "
            "whole log is read by default."
        ),
    )
    decision_list.add_argument("--json", action="store_true", help=JSON_HELP)
    participant = commands.add_parser(
        "participant", help="Inspect or add participants for a repository."
    )
    roles = participant.add_subparsers(dest="action", required=True)
    listing = roles.add_parser(
        "list", help="List the project's lanes and their identities."
    )
    listing.add_argument(
        "--repo", type=Path, default=Path.cwd(), help=REPO_HELP
    )
    listing.add_argument("--json", action="store_true", help=JSON_HELP)
    reporting_lane = roles.add_parser(
        "show",
        help=(
            "Print one lane's branch, worktree, provider, advisory budget, "
            "current claims and last coordination."
        ),
    )
    reporting_lane.add_argument("name", help="Lane to print.")
    reporting_lane.add_argument(
        "--repo", type=Path, default=Path.cwd(), help=REPO_HELP
    )
    reporting_lane.add_argument("--json", action="store_true", help=JSON_HELP)
    joining = roles.add_parser(
        "add", help="Create a lane with an optional provider and credentials."
    )
    joining.add_argument("name", help="Name of the new lane.")
    joining.add_argument(
        "--provider",
        help="Provider definition; defaults to the participant name.",
    )
    joining.add_argument(
        "--credentials", help="Credential profile selecting one account."
    )
    joining.add_argument(
        "--repo", type=Path, default=Path.cwd(), help=REPO_HELP
    )
    for action, summary in (
        ("restore", "Restore the assigned branch while preserving work."),
        ("retire", "Retire a lane while preserving recoverable work."),
        ("merge", "Run the configured gate and merge a lane's ready work."),
        ("pr", "Push the lane branch and open or locate its pull request."),
        ("pause", "Refuse a lane's calls; keep its session and claims."),
        ("resume", "Let a paused lane act again."),
        ("stop", "End a lane's session; keep its claims."),
        ("restart", "Start a lane again; keep its uncommitted work."),
    ):
        command = roles.add_parser(action, help=summary)
        if action == "retire":
            chosen = command.add_mutually_exclusive_group(required=True)
            chosen.add_argument(
                "name", nargs="?", default="", help="Lane to act on."
            )
            chosen.add_argument(
                "--ready",
                action="store_true",
                help=(
                    "Retire every lane `problems` lists as ready to retire, "
                    "one at a time, continuing past a lane that refuses."
                ),
            )
        elif action in BULK_PARTICIPANT or action == "merge":
            command.add_argument(
                "name",
                nargs="?",
                default="",
                help="Lane to act on; a lane selector may replace it.",
            )
        else:
            command.add_argument("name", help="Lane to act on.")
        command.add_argument(
            "--repo", type=Path, default=Path.cwd(), help=REPO_HELP
        )
        if action == "merge":
            command.add_argument(
                "--preview",
                action="store_true",
                help="Report what the merge would do; change nothing.",
            )
            command.add_argument(
                "--renew-recovery",
                action="store_true",
                help=(
                    "Grant the named lane's recorded unverified integration "
                    "a fresh set of attempts, then retry it. Accepted only "
                    "from the base checkout."
                ),
            )
            command.add_argument(
                "--verify-recovery",
                action="store_true",
                help=(
                    "Run the recorded gate on the base checkout as it stands "
                    "and clear the unverified-integration record only if it "
                    "passes and leaves the tree clean. Merges nothing; "
                    "accepted only from the base checkout."
                ),
            )
            command.add_argument(
                "--issue",
                default="",
                help=(
                    "Claimed issue whose work the named lane merges when it "
                    "holds several; defaults to its only ready claim."
                ),
            )
            scope = command.add_mutually_exclusive_group()
            scope.add_argument(
                "--all",
                action="store_true",
                help=(
                    "Integrate every lane reported ready, ordered by the "
                    "recorded dependency edges, stopping at the first "
                    "refusal or failure."
                ),
            )
            scope.add_argument(
                "--group",
                default="",
                metavar="NAME",
                help=(
                    "Integrate one group of the applied plan. Preflight "
                    "admits every member or none; execution is ordered, not "
                    "atomic, and stops at the first refusal or failure."
                ),
            )
            add_selector(command, everything=False)
        elif action in BULK_PARTICIPANT:
            add_selector(command)
        if action == "restart":
            command.add_argument(
                "--task",
                default="",
                help="Opening prompt for the new session.",
            )
        if action == "retire":
            command.add_argument(
                "--yes",
                action="store_true",
                help=(
                    "Delete the ignored files the lane's worktree holds "
                    "without asking first."
                ),
            )
    limiting = roles.add_parser(
        "budget",
        help=(
            "Show or set a lane's advisory token, call and hour limits; "
            "crossing one marks the lane and stops nothing."
        ),
    )
    limiting.add_argument("name", help="Lane whose limits are shown or set.")
    limiting.add_argument(
        "--repo", type=Path, default=Path.cwd(), help=REPO_HELP
    )
    add_budget_flags(limiting)
    granting = commands.add_parser(
        "approve",
        help="Record that you approve a lane's ready report for integration.",
    )
    granting.add_argument(
        "participant", help="Participant whose ready report you approve."
    )
    granting.add_argument(
        "--repo", type=Path, default=Path.cwd(), help=REPO_HELP
    )
    granting.add_argument("--json", action="store_true", help=JSON_HELP)
    refusing = commands.add_parser(
        "reject",
        help="Record that you reject a lane's ready report, and say why.",
    )
    refusing.add_argument(
        "participant", help="Participant whose ready report you reject."
    )
    refusing.add_argument(
        "reason", help="Explanation delivered to the lane as operator mail."
    )
    refusing.add_argument(
        "--repo", type=Path, default=Path.cwd(), help=REPO_HELP
    )
    refusing.add_argument("--json", action="store_true", help=JSON_HELP)
    requirement = commands.add_parser(
        "approval",
        help="Show or set the steps that require a recorded approval first.",
    )
    requirements = requirement.add_subparsers(dest="action", required=True)
    stating = requirements.add_parser(
        "show", help="Show which steps require a recorded approval first."
    )
    stating.add_argument(
        "--repo", type=Path, default=Path.cwd(), help=REPO_HELP
    )
    stating.add_argument("--json", action="store_true", help=JSON_HELP)
    requiring = requirements.add_parser(
        "set", help="Require an approval before merge, pr, both or none."
    )
    requiring.add_argument(
        "steps",
        nargs="*",
        metavar="STEP",
        help=(
            "Steps refused without a recorded operator approval of the "
            "lane's current ready report, from "
            + ", ".join(roster.APPROVAL_STEPS)
            + "; pass none to require no approval."
        ),
    )
    requiring.add_argument(
        "--repo", type=Path, default=Path.cwd(), help=REPO_HELP
    )
    resuming = requirements.add_parser(
        "resume",
        help=(
            "Show or record whether the service may resume a claude lane "
            "without a terminal."
        ),
    )
    resuming.add_argument(
        "--bridge-tools",
        choices=("on", "off"),
        help="Record approve_bridge_tools: allow this bridge's own tools.",
    )
    resuming.add_argument(
        "--auto-mode",
        choices=("on", "off"),
        help="Record auto_mode: start in the client's auto permission mode.",
    )
    resuming.add_argument(
        "--participant",
        default="",
        help="Lane to record on; the project default when omitted.",
    )
    resuming.add_argument(
        "--repo", type=Path, default=Path.cwd(), help=REPO_HELP
    )
    gate = commands.add_parser(
        "verify",
        help="Show or set the command a repository requires before a merge.",
    )
    gates = gate.add_subparsers(dest="action", required=True)
    showing = gates.add_parser(
        "show", help="Show the project's configured pre-merge command."
    )
    showing.add_argument(
        "--repo", type=Path, default=Path.cwd(), help=REPO_HELP
    )
    showing.add_argument("--json", action="store_true", help=JSON_HELP)
    setting = gates.add_parser(
        "set", help="Set the pre-merge command; empty removes it."
    )
    setting.add_argument(
        "command_line",
        metavar="COMMAND",
        help=(
            "Command run in the base checkout before every merge; pass an "
            "empty string to remove the gate. No flag skips it."
        ),
    )
    setting.add_argument(
        "--repo", type=Path, default=Path.cwd(), help=REPO_HELP
    )
    standing = commands.add_parser(
        "unattended",
        help="Show, set or run the project's unattended integration policy.",
    )
    standings = standing.add_subparsers(dest="action", required=True)
    standing_show = standings.add_parser(
        "show", help="Show the unattended integration policy."
    )
    standing_show.add_argument(
        "--repo", type=Path, default=Path.cwd(), help=REPO_HELP
    )
    standing_set = standings.add_parser(
        "set", help="Authorize unattended integration of named issues."
    )
    standing_set.add_argument(
        "issues",
        nargs="*",
        metavar="ISSUE",
        help=(
            "Issues whose ready work may be integrated without an operator; "
            "pass none to remove the policy and stay operator-only."
        ),
    )
    standing_set.add_argument(
        "--target",
        help="Branch the base checkout must have checked out.",
    )
    standing_set.add_argument(
        "--auto",
        action="store_true",
        help=(
            "Let the service integrate ready listed lanes on its own "
            "instead of waiting for `unattended run`."
        ),
    )
    standing_set.add_argument(
        "--repo", type=Path, default=Path.cwd(), help=REPO_HELP
    )
    standing_run = standings.add_parser(
        "run", help="Integrate one eligible lane under the policy."
    )
    standing_run.add_argument(
        "name", help="Participant whose ready work is integrated."
    )
    standing_run.add_argument(
        "--issue",
        default="",
        help=(
            "Claimed issue to integrate when the lane holds several; "
            "defaults to its only ready claim."
        ),
    )
    standing_run.add_argument(
        "--repo", type=Path, default=Path.cwd(), help=REPO_HELP
    )
    waiting_kind = commands.add_parser(
        "timeout",
        help=(
            "Show or set how long a reversible decision waits before its "
            "recommended default is applied."
        ),
    )
    waiting_kinds = waiting_kind.add_subparsers(dest="action", required=True)
    waiting_show = waiting_kinds.add_parser(
        "show", help="List every decision kind with its timeout."
    )
    waiting_show.add_argument(
        "--repo", type=Path, default=Path.cwd(), help=REPO_HELP
    )
    waiting_set = waiting_kinds.add_parser(
        "set", help="Make a reversible kind always ask, or wait longer."
    )
    waiting_set.add_argument("kind", help="Decision kind the entry governs.")
    waiting_choice = waiting_set.add_mutually_exclusive_group()
    waiting_choice.add_argument(
        "--ask",
        action="store_true",
        help="Always ask; never apply the default.",
    )
    waiting_choice.add_argument(
        "--after",
        type=duration,
        default=None,
        metavar="WINDOW",
        help=(
            "Longer wait than the default, such as 2h. Without --ask or "
            "--after the kind returns to its default."
        ),
    )
    waiting_set.add_argument(
        "--repo", type=Path, default=Path.cwd(), help=REPO_HELP
    )
    blueprint = commands.add_parser(
        "blueprint",
        help="Set, show or run the ordered steps a claim runs through.",
    )
    blueprint_actions = blueprint.add_subparsers(dest="action", required=True)
    blueprint_list = blueprint_actions.add_parser(
        "list", help="List the recorded blueprints."
    )
    blueprint_show = blueprint_actions.add_parser(
        "show", help="Show one blueprint node by node, or list them all."
    )
    blueprint_show.add_argument(
        "name",
        nargs="?",
        metavar="NAME",
        help="Blueprint to show; all are listed when omitted.",
    )
    blueprint_templates = blueprint_actions.add_parser(
        "templates", help="List the starter blueprints shipped with the tool."
    )
    blueprint_templates.add_argument(
        "--json", action="store_true", help=JSON_HELP
    )
    blueprint_set = blueprint_actions.add_parser(
        "set",
        help="Record a blueprint from a JSON file or a starter template. "
        "Base checkout only.",
    )
    blueprint_set.add_argument(
        "name", metavar="NAME", help="Name to record the blueprint under."
    )
    blueprint_source = blueprint_set.add_mutually_exclusive_group(required=True)
    blueprint_source.add_argument(
        "file",
        type=Path,
        nargs="?",
        metavar="FILE",
        help="JSON file holding the blueprint.",
    )
    blueprint_source.add_argument(
        "--template",
        metavar="TEMPLATE",
        help="Starter blueprint to record; see blueprint templates.",
    )
    blueprint_set.add_argument(
        "--verify",
        metavar="CMD",
        help="Command the template's verify steps run; the project's "
        "verification command when omitted.",
    )
    blueprint_remove = blueprint_actions.add_parser(
        "remove", help="Remove a recorded blueprint. Base checkout only."
    )
    blueprint_remove.add_argument(
        "name", metavar="NAME", help="Blueprint to remove."
    )
    blueprint_run = blueprint_actions.add_parser(
        "run",
        help="Start a lane's claim on a blueprint. Base checkout only.",
    )
    blueprint_run.add_argument(
        "participant", metavar="NAME", help="Lane whose claim runs."
    )
    blueprint_run.add_argument(
        "blueprint", metavar="BLUEPRINT", help="Recorded blueprint to run."
    )
    blueprint_run.add_argument(
        "--issue", required=True, help="Claimed issue the run belongs to."
    )
    blueprint_advance = blueprint_actions.add_parser(
        "advance",
        help="Move a claim's run on after its lane reports. Base checkout "
        "only.",
    )
    blueprint_advance.add_argument(
        "--issue", required=True, help="Claimed issue whose run moves on."
    )
    blueprint_progress = blueprint_actions.add_parser(
        "progress", help="Show each run's current node and nodes passed."
    )
    blueprint_progress.add_argument(
        "--issue", help="Show only the run of this issue."
    )
    blueprint_default = blueprint_actions.add_parser(
        "default",
        help="Map an issue label to the blueprint run --issue starts; omit "
        "BLUEPRINT to clear it. Base checkout only.",
    )
    blueprint_default.add_argument(
        "label", metavar="LABEL", help="Issue label to map."
    )
    blueprint_default.add_argument(
        "blueprint",
        nargs="?",
        metavar="BLUEPRINT",
        help="Blueprint the label starts; omit to clear the mapping.",
    )
    for blueprint_action in (
        blueprint_list,
        blueprint_show,
        blueprint_set,
        blueprint_remove,
        blueprint_run,
        blueprint_advance,
        blueprint_progress,
        blueprint_default,
    ):
        blueprint_action.add_argument(
            "--repo", type=Path, default=Path.cwd(), help=REPO_HELP
        )
    preparation = commands.add_parser(
        "init",
        help="Show or set the command every new lane runs before it starts.",
    )
    preparations = preparation.add_subparsers(dest="action", required=True)
    reporting = preparations.add_parser(
        "show", help="Show the command every new lane runs before it starts."
    )
    reporting.add_argument(
        "--repo", type=Path, default=Path.cwd(), help=REPO_HELP
    )
    reporting.add_argument("--json", action="store_true", help=JSON_HELP)
    recording = preparations.add_parser(
        "set", help="Set the command every new lane runs before it starts."
    )
    recording.add_argument(
        "command_line",
        metavar="COMMAND",
        help=(
            "Command run in every new lane before its agent starts; pass an "
            "empty string to remove it. AGENT_PARLEY_BASE names the base "
            "checkout while it runs. No flag skips it."
        ),
    )
    recording.add_argument(
        "--repo", type=Path, default=Path.cwd(), help=REPO_HELP
    )
    pooling = commands.add_parser(
        "pool",
        help="Show or set how many prepared spare worktrees new lanes take.",
    )
    pools = pooling.add_subparsers(dest="action", required=True)
    pool_show = pools.add_parser(
        "show", help="Show the pool size and the condition of each spare."
    )
    pool_show.add_argument(
        "--repo", type=Path, default=Path.cwd(), help=REPO_HELP
    )
    pool_show.add_argument("--json", action="store_true", help=JSON_HELP)
    pool_set = pools.add_parser(
        "set", help="Set how many spare worktrees to keep prepared."
    )
    pool_set.add_argument(
        "size",
        type=int,
        metavar="N",
        help=(
            "Spare worktrees kept on fresh branches from the current base "
            "with init already run; 0 keeps none."
        ),
    )
    pool_set.add_argument(
        "--repo", type=Path, default=Path.cwd(), help=REPO_HELP
    )
    pool_fill = pools.add_parser(
        "fill", help="Prepare missing spares now and remove stale ones."
    )
    pool_fill.add_argument(
        "--repo", type=Path, default=Path.cwd(), help=REPO_HELP
    )
    naming = commands.add_parser(
        "branch",
        help="Show or set the prefix new lane branches are created under.",
    )
    namings = naming.add_subparsers(dest="action", required=True)
    naming_show = namings.add_parser(
        "show", help="Show the lane branch prefix and integration branch."
    )
    naming_show.add_argument(
        "--repo", type=Path, default=Path.cwd(), help=REPO_HELP
    )
    naming_show.add_argument("--json", action="store_true", help=JSON_HELP)
    naming_set = namings.add_parser(
        "set", help="Set the prefix new lane branches are created under."
    )
    naming_set.add_argument(
        "prefix",
        metavar="PREFIX",
        help=(
            "Prefix for new lane branches, such as parley. Existing lanes "
            "keep the branch they were created with."
        ),
    )
    naming_set.add_argument(
        "--repo", type=Path, default=Path.cwd(), help=REPO_HELP
    )
    naming_base = namings.add_parser(
        "integration",
        help=(
            "Record the branch a milestone's pull requests merge into; "
            "operator-only."
        ),
    )
    naming_base.add_argument(
        "integration",
        metavar="BRANCH",
        help=(
            "Integration branch, such as integration/1.0.0; a pull request "
            "merged into it that closes a claimed issue lands the claim. An "
            "empty string removes it."
        ),
    )
    naming_base.add_argument(
        "--repo", type=Path, default=Path.cwd(), help=REPO_HELP
    )
    tracker = commands.add_parser(
        "forge",
        help="Show or set the issue tracker this project coordinates over.",
    )
    trackers = tracker.add_subparsers(dest="action", required=True)
    tracker_show = trackers.add_parser(
        "show", help="Show the issue tracker this project coordinates over."
    )
    tracker_show.add_argument(
        "--repo", type=Path, default=Path.cwd(), help=REPO_HELP
    )
    tracker_show.add_argument("--json", action="store_true", help=JSON_HELP)
    tracker_set = trackers.add_parser(
        "set", help="Select the issue tracker; only github opens PRs."
    )
    tracker_set.add_argument(
        "name",
        metavar="NAME",
        choices=forge.FORGES,
        help=(
            "github speaks through gh, beads through bd when the repository "
            "carries a .beads/ ledger, and null keeps issue numbers bare."
        ),
    )
    tracker_set.add_argument(
        "--repo", type=Path, default=Path.cwd(), help=REPO_HELP
    )
    budgets = commands.add_parser(
        "deadlines",
        help="Show or set this project's deadline and attempt defaults.",
    )
    budget_actions = budgets.add_subparsers(dest="action", required=True)
    budget_show = budget_actions.add_parser(
        "show", help="Show the deadline and attempt defaults."
    )
    budget_show.add_argument(
        "--repo", type=Path, default=Path.cwd(), help=REPO_HELP
    )
    budget_show.add_argument("--json", action="store_true", help=JSON_HELP)
    budget_set = budget_actions.add_parser(
        "set", help="Set the deadline windows and attempt limit."
    )
    budget_set.add_argument(
        "--repo", type=Path, default=Path.cwd(), help=REPO_HELP
    )
    for field, described in (
        ("claim", "a claim"),
        ("offer", "a handoff offer"),
        ("request", "a takeover request"),
        ("ack", "an acknowledgement"),
        ("wait", "a lane's self-declared wait"),
    ):
        budget_set.add_argument(
            f"--{field}",
            type=duration,
            metavar="WINDOW",
            help=(
                f"Default window for {described}, such as 45m, 6h or 7d. "
                "An overdue record is reported, never transferred."
            ),
        )
    budget_set.add_argument(
        "--attempts",
        type=int,
        help=(
            "Attempts a claim may report blocked before the budget reads as "
            "exceeded. Exceeding it releases nothing."
        ),
    )
    ceiling = commands.add_parser(
        "budget",
        help=(
            "Show or set the advisory token, call and hour limits every "
            "lane of this project inherits."
        ),
    )
    ceiling_actions = ceiling.add_subparsers(dest="action", required=True)
    ceiling_show = ceiling_actions.add_parser(
        "show", help="Show the advisory limits every lane inherits."
    )
    ceiling_show.add_argument(
        "--repo", type=Path, default=Path.cwd(), help=REPO_HELP
    )
    ceiling_show.add_argument("--json", action="store_true", help=JSON_HELP)
    ceiling_set = ceiling_actions.add_parser(
        "set", help="Set the advisory limits every lane inherits."
    )
    ceiling_set.add_argument(
        "--repo", type=Path, default=Path.cwd(), help=REPO_HELP
    )
    add_budget_flags(ceiling_set)
    ceiling_enforce = ceiling_actions.add_parser(
        "enforce",
        help=(
            "Show or set the opt-in run budget: aggregate limits across every "
            "lane that stop new wakes, dispatch, retries and launches."
        ),
    )
    ceiling_enforce.add_argument(
        "--repo", type=Path, default=Path.cwd(), help=REPO_HELP
    )
    for field, kind in (("tokens", int), ("calls", int), ("hours", float)):
        ceiling_enforce.add_argument(
            f"--{field}",
            type=kind,
            metavar="N",
            help=(
                f"Enforced limit on the {field} every lane of the run "
                "consumes together; 0 removes it."
            ),
        )
    ceiling_enforce.add_argument(
        "--allow-unmeasured",
        action="store_true",
        help=(
            "Enforce a token limit even though some lanes run a provider "
            "whose tokens are not read."
        ),
    )
    ceiling_resume = ceiling_actions.add_parser(
        "resume",
        help="Clear an exhausted run budget so the service dispatches again.",
    )
    ceiling_resume.add_argument(
        "--repo", type=Path, default=Path.cwd(), help=REPO_HELP
    )
    ceiling_resume.add_argument(
        "--reset",
        action="store_true",
        help="Start a new accounting period; use so far stops counting.",
    )
    shared = commands.add_parser(
        "resources",
        help="Show or declare the named resources lanes may reserve.",
    )
    declarations = shared.add_subparsers(dest="action", required=True)
    declared_show = declarations.add_parser(
        "show", help="Show the named resources lanes may reserve."
    )
    declared_show.add_argument(
        "--repo", type=Path, default=Path.cwd(), help=REPO_HELP
    )
    declared_show.add_argument("--json", action="store_true", help=JSON_HELP)
    declared_set = declarations.add_parser(
        "set", help="Declare the named resources lanes may reserve."
    )
    declared_set.add_argument(
        "names",
        metavar="NAMES",
        help=(
            "Space-separated resource names such as 'port:5432 db:local'; "
            "pass an empty string to accept any well-formed name again."
        ),
    )
    declared_set.add_argument(
        "--repo", type=Path, default=Path.cwd(), help=REPO_HELP
    )
    provider = commands.add_parser(
        "provider", help="Inspect or define providers that drive a native CLI."
    )
    definitions = provider.add_subparsers(dest="action", required=True)
    definitions.add_parser(
        "list", help="List built-in presets and local overrides."
    ).add_argument("--json", action="store_true", help=JSON_HELP)
    definitions.add_parser(
        "remove", help="Delete a local definition, restoring any preset."
    ).add_argument("name", help="Provider definition to delete.")
    provider_show = definitions.add_parser(
        "show", help="Print one provider definition and its hook support."
    )
    provider_show.add_argument("name", help="Provider to print.")
    provider_show.add_argument("--json", action="store_true", help=JSON_HELP)
    defining = definitions.add_parser(
        "add", help="Define a provider; warn when shadowing a preset."
    )
    defining.add_argument("name", help="Name of the provider to define.")
    defining.add_argument(
        "--adapter",
        choices=roster.ADAPTERS,
        required=True,
        help="Native CLI family whose launch and hooks the provider uses.",
    )
    defining.add_argument(
        "--executable",
        required=True,
        help="Command resolved on PATH that starts the native CLI.",
    )
    defining.add_argument(
        "--home-env",
        default="",
        metavar="NAME",
        help=(
            "Variable naming the CLI's config home, which a credential "
            "profile's --config-home sets."
        ),
    )
    defining.add_argument(
        "--env",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="Variable set for every launch; repeatable, never a secret.",
    )
    defining.add_argument(
        "--require-env",
        action="append",
        default=[],
        metavar="NAME",
        help="Variable the launching shell must export; repeatable.",
    )
    defining.add_argument("--json", action="store_true", help=JSON_HELP)
    capping = definitions.add_parser(
        "budget",
        help=(
            "Show or set the advisory limits every lane on this provider "
            "inherits unless its own budget says otherwise."
        ),
    )
    capping.add_argument("name", help="Provider whose limits are shown or set.")
    add_budget_flags(capping)
    pricing = definitions.add_parser(
        "price",
        help=(
            "Show or record the operator's price per million tokens, which "
            "report usage turns into an estimated spend."
        ),
    )
    pricing.add_argument("name", help="Provider whose price is shown or set.")
    pricing.add_argument(
        "--per-mtok",
        type=float,
        metavar="AMOUNT",
        help=(
            "Price per million recorded tokens; 0 removes it. Never "
            "fetched, and refused from a lane."
        ),
    )
    accounts = commands.add_parser(
        "credentials", help="Inspect or define per-account profiles."
    )
    profiles = accounts.add_subparsers(dest="action", required=True)
    profiles.add_parser(
        "list", help="List native account profiles."
    ).add_argument("--json", action="store_true", help=JSON_HELP)
    profiles.add_parser(
        "remove", help="Delete a profile; keep native files and logins."
    ).add_argument("name", help="Profile to delete.")
    credential_show = profiles.add_parser(
        "show",
        help=("Print one account profile with every recorded value redacted."),
    )
    credential_show.add_argument("name", help="Profile to print.")
    credential_show.add_argument("--json", action="store_true", help=JSON_HELP)
    profile = profiles.add_parser(
        "add", help="Define a config home and environment requirements."
    )
    profile.add_argument(
        "name", help="Profile name that run --credentials selects."
    )
    profile.add_argument(
        "--config-home",
        default="",
        metavar="DIR",
        help=(
            "Separate config directory for this account, set as the "
            "provider's config-home variable at launch; sign in there once."
        ),
    )
    profile.add_argument(
        "--env",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="Variable set for this account's launches; repeatable.",
    )
    profile.add_argument(
        "--require-env",
        action="append",
        default=[],
        metavar="NAME",
        help="Variable the launching shell must export; repeatable.",
    )
    profile.add_argument("--json", action="store_true", help=JSON_HELP)
    quotas = commands.add_parser(
        "quota",
        help=(
            "Show each credential's quota headroom or record its window; "
            "lanes near the window park until it resets."
        ),
    )
    quota_actions = quotas.add_subparsers(dest="action", required=True)
    quota_show = quota_actions.add_parser(
        "show", help="Show remaining headroom per credential profile."
    )
    quota_show.add_argument(
        "--repo", type=Path, default=Path.cwd(), help=REPO_HELP
    )
    quota_show.add_argument("--json", action="store_true", help=JSON_HELP)
    quota_set = quota_actions.add_parser(
        "set", help="Record one credential's window length and token limit."
    )
    quota_set.add_argument("name", help="Credential profile the plan uses.")
    quota_set.add_argument(
        "--hours",
        type=float,
        default=0.0,
        help="Length of the plan's usage window in hours.",
    )
    quota_set.add_argument(
        "--tokens",
        type=int,
        required=True,
        help="Native tokens the plan allows per window; 0 removes it.",
    )
    quota_set.add_argument(
        "--threshold",
        type=float,
        default=1.0,
        help=(
            "Fraction of the limit at which every lane but the "
            "highest-priority one parks; 1 parks only at the limit."
        ),
    )
    parser.epilog = command_help(commands)


def selected_command(arguments: Sequence[str]) -> str | None:
    """Reports the command a raw argument vector asks for.

    Only the two options the root parser declares are stepped over, and any
    other leading option, including an abbreviation argparse would accept,
    answers that the command is unknown. An unknown command is answered with
    the whole parser, so a reading this function is unsure about costs time
    and never changes what argparse does with the arguments.

    Args:
        arguments: Arguments as typed, without the program name.

    Returns:
        The command name, or ``None`` when every command is needed.
    """
    index = 0
    while index < len(arguments):
        token = arguments[index]
        if token in ("-V", "--version"):
            index += 1
        elif token == "--home":
            index += 2
        elif token.startswith("--home="):
            index += 1
        elif token.startswith("-"):
            return None
        else:
            return token
    return None


def root_parser(
    typed: str | None,
) -> tuple[argparse.ArgumentParser, CommandIndex]:
    """Builds the root parser around the command that was typed.

    Args:
        typed: Command to build a parser for, or ``None`` for all of them.

    Returns:
        The root parser and the index recording what was declared.
    """
    parser = argparse.ArgumentParser(
        prog="agent-parley",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        usage="agent-parley [--home DIR] COMMAND [ARGUMENTS]",
    )
    parser.add_argument(
        "-V",
        "--version",
        action="store_true",
        help="Print the installed version and exit.",
    )
    parser.add_argument(
        "--home",
        type=Path,
        default=Path(
            os.environ.get("AGENT_PARLEY_HOME", "~/.local/state/agent-parley")
        ),
        help="Private state directory (or AGENT_PARLEY_HOME).",
    )
    commands = CommandIndex(
        parser.add_subparsers(
            dest="command",
            metavar="COMMAND",
            help=argparse.SUPPRESS,
            prog="agent-parley",
        ),
        typed,
    )
    declare(parser, commands)
    return parser, commands


def _plain_status() -> int:
    """Runs the bare status command without building its parser.

    The reading is the open-work view scoped to the project the working
    directory belongs to, or every live project outside any project.
    """
    home = Path(
        os.environ.get("AGENT_PARLEY_HOME", "~/.local/state/agent-parley")
    )
    try:
        bridge = Bridge(home)
        bridge.board(
            Selection(project=bridge.project_at(Path.cwd())), terminal_width()
        )
    except (BridgeError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"agent-parley: {refusal(exc)}", file=sys.stderr)
        return 1
    return 0


def _title() -> int:
    """Prints the working directory's lane summary for a native status line.

    A status line runs this on every refresh, so it reads only the lane's
    private records and never builds the full parser. Outside a lane it
    prints nothing and still succeeds, so a status line configured for every
    session stays empty in sessions the bridge did not launch.
    """
    summary = terminal.lane_summary(Path.cwd())
    if summary:
        print(summary)
    return 0


def main() -> int:
    """Dispatches the CLI and returns an operational exit status."""
    if sys.argv[1:] == ["status"]:
        return _plain_status()
    if sys.argv[1:] == ["title"]:
        return _title()
    if not sys.argv[1:]:
        from agent_parley import start

        return start.show()
    typed = selected_command(sys.argv[1:])
    parser, commands = root_parser(typed)
    if typed is not None and typed not in commands.declared:
        parser, commands = root_parser(None)
    args = parser.parse_args()
    home = args.home.expanduser()
    if args.version:
        print(protocol.launcher_version())
        return 0
    if args.command is None:
        from agent_parley import start

        return start.show(home)
    if args.command == "completion":
        print(completion.script(parser, args.shell), end="")
        return 0
    if args.command == "__complete":
        print("\n".join(completion.candidates(home, args.kind)))
        return 0
    if args.command == "title":
        return _title()
    if args.command == "version":
        installed = protocol.launcher_version()
        print(
            views.render(
                "version",
                {"version": installed, "state_directory": str(home)},
            )
            if args.json
            else f"agent-parley {installed}\nState: {home}"
        )
        return 0
    if args.command == "plugins":
        from agent_parley import plugins

        if args.action == "status":
            print("\n".join(plugins.status(home)))
            return 0
        outcome, succeeded = plugins.install(home)
        print("\n".join(outcome))
        return 0 if succeeded else 1
    if args.command == "demo":
        from agent_parley import demo

        return demo.main()
    try:
        bridge = Bridge(args.home)
        if args.command == "up":
            from agent_parley import projects

            roots = (
                projects.members(bridge.home, args.workspace)
                if args.workspace
                else []
            )
            bridge.up()
            started = {
                root: projects.start_lanes(bridge.home, root) for root in roots
            }
            print(
                views.render(
                    "up",
                    {
                        "url": f"{bridge.url}/mcp/",
                        "state_directory": str(bridge.home),
                        "ready": bridge.ready(),
                        **({"lanes": started} if args.workspace else {}),
                    },
                )
                if args.json
                else f"Coordination server ready at {bridge.url}/mcp/\n"
                + notification_line(
                    notify.reported(notify.environment(bridge.home)),
                    inbound_status(bridge.home),
                )
                + "".join(
                    f"\n{root}\n"
                    + "".join(
                        f"  {name}: {outcome}\n"
                        for name, outcome in outcomes.items()
                    )
                    for root, outcomes in started.items()
                )
            )
        elif args.command == "projects":
            from agent_parley import projects

            known = projects.listed(bridge.home)
            print(
                views.render("projects", {"projects": known})
                if args.json
                else projects.describe(known)
            )
        elif args.command == "workspace":
            from agent_parley import projects

            if args.action == "set":
                projects.set_workspace(bridge.home, args.name, args.projects)
            elif args.action == "remove":
                projects.remove_workspace(bridge.home, args.name)
            grouped = projects.workspaces(bridge.home)
            print(
                "\n".join(
                    f"{name}: {', '.join(members)}"
                    for name, members in sorted(grouped.items())
                )
                or "No workspaces; `agent-parley workspace set NAME "
                "PROJECT...` records one."
            )
        elif args.command == "down":
            bridge.down()
            print(
                views.render(
                    "down",
                    {
                        "stopped": True,
                        "state_directory": str(bridge.home),
                    },
                )
                if args.json
                else "Coordination server stopped. Worktrees and messages "
                "retained."
            )
        elif args.command == "top":
            from agent_parley import dashboard

            names = tuple(
                name.strip().upper()
                for name in (args.columns or "").replace(",", " ").split()
            )
            unknown = [
                name
                for name in names
                if name != "ALL" and name not in dict(dashboard.COLUMNS)
            ]
            if unknown:
                parser.error(
                    f"Unknown column {', '.join(unknown)}. Choose from: "
                    f"{', '.join(name for name, _ in dashboard.COLUMNS)}."
                )
            if args.sort and args.sort.upper() not in dashboard.SORT_KEYS:
                parser.error(
                    f"Unknown sort column {args.sort}. Choose from: "
                    f"{', '.join(dashboard.SORT_KEYS)}."
                )
            if args.serve is not None and (args.json or args.once):
                parser.error(
                    "--serve cannot be combined with --json or --once."
                )
            if args.serve is not None and not 0 <= args.serve <= 65535:
                parser.error("--serve takes a port from 0 to 65535.")

            def top_view() -> dict:
                """Reads one frame narrowed and ordered by the top filters."""
                return dashboard.select(
                    dashboard.collect(
                        bridge.home,
                        bool(bridge.server_process()),
                        {},
                        tuple(args.provider or ()),
                        args.since,
                        operator_edits=not args.no_operator_edits,
                    ),
                    args.sort or "",
                    args.reverse,
                    tuple(args.project or ()),
                    tuple(args.participant or ()),
                )

            if args.serve is not None:
                from agent_parley import webview

                webview.serve(args.serve, top_view, args.interval)
            elif args.json:
                print(views.render("top", views.frame(top_view())))
            else:
                dashboard.run(
                    bridge.home,
                    lambda: bool(bridge.server_process()),
                    args.once,
                    args.interval,
                    tuple(args.provider or ()),
                    args.since,
                    args.sort or "",
                    args.reverse,
                    tuple(args.project or ()),
                    tuple(args.participant or ()),
                    names,
                    not args.no_operator_edits,
                    lambda: problems.lines(bridge.problems()),
                    args.everything,
                )
        elif args.command == "metrics" and args.reading == "throughput":
            reading = metrics.throughput(bridge.home, args.start)
            if args.json:
                print(json.dumps(reading, indent=2))
            else:
                print(metrics.describe_throughput(reading), end="")
        elif args.command == "metrics":
            from agent_parley import dashboard

            if args.every and not args.output:
                parser.error("--every needs --output.")
            with contextlib.suppress(KeyboardInterrupt):
                while True:
                    frame = dashboard.export(
                        bridge.home,
                        bool(bridge.server_process()),
                        tuple(args.provider or ()),
                        args.since,
                        args.json,
                    )
                    if args.output:
                        write_text(args.output, frame)
                    else:
                        print(frame, end="")
                    if not args.every:
                        break
                    time.sleep(args.every)
        elif args.command == "watch":
            _, directory = bridge.project(args.repo.resolve(), create=False)
            stream.run(
                bridge.home,
                directory,
                roster.read(directory),
                args.participant,
                since=args.since,
                kinds=tuple(args.kind or ()),
                json_lines=args.json,
                interval=args.interval,
            )
        elif args.command == "history":
            if args.subject is None:
                typed_parser(parser, args).error(
                    "history takes issue, participant or claim."
                )
            reported = bridge.history(
                args.repo.resolve(),
                args.subject,
                getattr(args, "number", "")
                or getattr(args, "name", "")
                or getattr(args, "claim_id", ""),
                kinds=tuple(args.kind or ()),
                participant=args.participant,
                provider=args.provider,
                issue=args.issue,
                window=args.since,
            )
            document = views.render("history", views.history(reported))
            if args.output:
                write_text(args.output, f"{document}\n")
                print(
                    f"Exported {len(reported['records'])} records for "
                    f"{args.subject} {reported['value']} to {args.output}."
                )
            else:
                print(
                    document
                    if args.json
                    else history.describe(
                        reported["records"],
                        reported["holdings"],
                        truncated=reported["truncated"],
                    )
                )
        elif args.command == "events":
            output = getattr(args, "output", None)
            message = bridge.export_events(
                args.repo.resolve(),
                tuple(args.participant or ()),
                args.since,
                output,
            )
            print(
                message,
                file=sys.stdout if output else sys.stderr,
            )
        elif args.command == "state" and args.action == "show" and args.json:
            print(
                views.render(
                    "state",
                    {
                        "archive": str(args.archive),
                        "manifest": archive.read_manifest(args.archive),
                    },
                )
            )
        elif args.command == "state":
            print(bridge.state_archive(args))
        elif args.command == "store":
            fault, damaged_copies = store.repair(bridge.home)
            print(
                f"Store was damaged ({fault}); kept "
                f"{', '.join(map(str, damaged_copies))} and created an "
                "empty store."
                if fault
                else "Store passed its integrity check; nothing was moved."
            )
        elif args.command == "setup":
            from agent_parley.core import suggested_gate

            repository = args.repo.resolve()
            registered = bridge.setup(repository)
            from agent_parley import projects

            with contextlib.suppress(BridgeError, OSError):
                projects.touch(bridge.home, registered["root"])
            if args.verify_command is not None:
                bridge.verification(repository, args.verify_command)
            if args.init_command is not None:
                bridge.initialization(repository, args.init_command)
            configured = bridge.commands(repository)
            registered["verify"] = configured["verify"]
            registered["initialize"] = configured["initialize"]
            print(
                views.render("setup", registered)
                if args.json
                else views.setup_text(
                    registered,
                    None
                    if registered["verify"]
                    else suggested_gate(Path(registered["root"])),
                )
            )
        elif args.command == "run":
            from agent_parley import projects

            if args.project:
                args.repo = Path(projects.resolve(bridge.home, args.project))
            used = bridge.project_at(args.repo.resolve())
            if used:
                with contextlib.suppress(BridgeError, OSError):
                    projects.touch(bridge.home, used)
            status = bridge.launch(
                args.participant,
                args.repo.resolve(),
                args.task,
                args.provider,
                args.credentials,
                resume=args.resume,
                issue=args.issue,
                blueprint=args.blueprint,
            )
            if bridge.preserved and sys.stdin.isatty():
                print(bridge.preserved, file=sys.stderr, flush=True)
            if args.json:
                print(
                    views.render(
                        "run",
                        {
                            "participant": args.participant,
                            "provider": args.provider or args.participant,
                            "credential": args.credentials or "",
                            "repo": str(args.repo.resolve()),
                            "resumed": args.resume,
                            "status": status,
                        },
                    )
                )
            return status
        elif args.command == "report" and args.action == "show":
            record = bridge.show_report(
                args.repo.resolve(), args.report_id, args.full
            )
            print(
                views.render("report_show", record)
                if args.json
                else shown_record(record)
            )
        elif args.command == "report" and args.action == "usage":
            _, directory = bridge.project(args.repo.resolve(), create=False)
            spent = history.usage(
                bridge.home,
                directory,
                roster.read(directory),
                time.time() - args.since if args.since else 0.0,
            )
            print(
                views.render("report_usage", views.usage(spent))
                if args.json
                else history.describe_usage(spent)
            )
        elif args.command == "report" and args.action == "review":
            recorded = bridge.review_report(
                args.repo.resolve(),
                args.report_id,
                args.verdict,
                args.evidence,
            )
            print(
                views.render("report_review", recorded)
                if args.json
                else shown_record(recorded)
            )
        elif args.command == "report":
            if not args.state or not args.summary:
                typed_parser(parser, args).error(
                    "report needs --state and --summary."
                )
            if args.state == "waiting" and args.until <= 0:
                parser.error("report --state waiting needs --until SECONDS.")
            owed = bridge.report(
                args.repo.resolve(),
                args.state,
                args.summary,
                args.remaining,
                args.evidence,
                key=args.idempotency_key,
                issue=args.issue,
                resume_on=args.resume_on,
                backlog=args.backlog,
                until=args.until,
                pid=args.pid,
                output=args.output_file,
            )
            print(f"Recorded outcome: {args.state}")
            if owed:
                print(owed)
        elif args.command == "say" or (
            args.command == "mail" and args.action == "send"
        ):
            return spoken(bridge, parser, args)
        elif args.command == "ask":
            return bridge.ask(
                args.participant,
                args.repo.resolve(),
                args.question,
                checkpoint=args.checkpoint,
            )
        elif args.command == "issue" and args.action == "show":
            detail = bridge.issue_reading(args.repo.resolve(), args.number)
            print(
                views.render(
                    "issue",
                    {
                        **views.issue_detail(
                            detail["ledger"],
                            int(detail["issue"]),
                            detail["reservations"],
                            detail["history"],
                        ),
                        "convergence": detail["convergence"],
                    },
                )
                if args.json
                else issue_lines(detail)
            )
        elif (
            args.command == "issue"
            and args.action == "assign"
            and selected(args)
        ):
            return assigned_selection(bridge, args.repo.resolve(), args)
        elif args.command == "issue" and args.action == "next":
            ranked = bridge.issue_next(args.repo.resolve(), args.limit)
            print(
                views.render("issue_next", ranked)
                if args.json
                else recommend.render(ranked)
            )
        elif args.command == "issue" and args.action == "match":
            goal_matches = bridge.issue_match(
                args.repo.resolve(), args.goal, args.limit
            )
            print(
                views.render("issue_match", goal_matches)
                if args.json
                else recommend.render_match(goal_matches)
            )
        elif args.command == "issue" and args.action == "assign":
            if args.unassign and args.name:
                parser.error("issue assign takes a lane or --unassign.")
            if not args.unassign and not args.name:
                parser.error("issue assign needs a lane, or --unassign.")
            print(
                assignment(
                    bridge.issue_assign(
                        args.repo.resolve(),
                        args.number,
                        args.name,
                        reason=args.reason,
                        withdraw=args.unassign,
                    )
                )
            )
        elif args.command == "followup" and args.action == "list":
            suggested = bridge.followup_list(args.repo.resolve())
            print(
                views.render("followup_list", {"suggestions": suggested})
                if args.json
                else followups.render(suggested)
            )
        elif args.command == "followup" and args.action == "accept":
            accepted = bridge.followup_accept(
                args.repo.resolve(),
                args.id,
                milestone=args.milestone,
                to=args.to,
            )
            print(
                views.render("followup_accept", accepted)
                if args.json
                else followups.accepted_text(accepted)
            )
        elif args.command == "issue" and args.action == "resolve":
            ended = bridge.issue_resolve(
                args.repo.resolve(),
                args.number,
                reason=args.reason,
                release=args.release,
            )
            print(
                views.render("issue_resolve", views.resolution(ended))
                if args.json
                else views.resolution_text(ended)
            )
        elif args.command == "issue" and args.action == "recover":
            approved = bridge.authorize_recovery(
                args.repo.resolve(), args.number, args.reason
            )
            print(
                views.render("issue_recover", views.recovery(approved))
                if args.json
                else views.recovery_text(approved)
            )
        elif args.command == "issue":
            result = bridge.issue(
                args.repo.resolve(),
                args.action,
                getattr(args, "number", ""),
                to=getattr(args, "to", None),
                summary=getattr(args, "summary", ""),
                offer_id=getattr(args, "offer_id", None),
                on=getattr(args, "on", None),
                within=getattr(args, "within", None),
                key=getattr(args, "idempotency_key", ""),
                when_released=getattr(args, "when_released", ""),
                remaining=getattr(args, "remaining", None),
                take_orphaned=getattr(args, "take_orphaned", False),
            )
            if args.action != "list":
                print(json.dumps(result, indent=2))
            elif args.json:
                print(views.render("issues", views.ledger(result)))
            else:
                print(describe(result, bridge.liveness(args.repo.resolve())))
        elif args.command == "doctor":
            reported = bridge.doctor()
            print(
                views.render("doctor", views.doctor(reported))
                if args.json
                else protocol.render(reported)
            )
            return 0 if reported["consistent"] else 1
        elif args.command == "problems" and args.action == "ack":
            recorded = bridge.acknowledge(args.repo.resolve(), args.message_id)
            print(
                views.render("problems_ack", recorded)
                if args.json
                else f"Acknowledged message {recorded['id']} for "
                + ", ".join(recorded["participants"])
                + "."
            )
        elif args.command == "problems":
            found = bridge.problems(args.ack_after)
            print(
                views.render("problems", problems.rendered(found, args.all))
                if args.json
                else "\n".join(problems.lines(found, args.all))
            )
            return 1 if problems.shown(found, args.all) else 0
        elif args.command in ("gc", "reclaim"):
            if args.force and not args.apply:
                parser.error("--force needs --apply.")
            if args.yes and not args.project:
                parser.error("--yes needs --project.")
            if bool(args.restore) != bool(args.to):
                parser.error("--restore and --to need each other.")
            if args.preserved or args.restore:
                from agent_parley import recovery

                _, directory = bridge.project(args.repo.resolve(), create=False)
                if args.restore:
                    restored = recovery.restore_preserved(
                        directory,
                        Path(roster.read(directory)["root"]),
                        args.restore,
                        args.to.absolute(),
                    )
                    print(
                        views.render("gc", {"restored": restored})
                        if args.json
                        else f"Restored {args.restore} to "
                        f"{restored['restored_to']}"
                    )
                    return 0
                kept = recovery.preserved(directory)
                listed = []
                for saved in kept:
                    age = time.time() - float(saved.get("captured_at") or 0)
                    listed.append(
                        f"{saved['id']}: {saved.get('source_worktree')} "
                        f"({saved.get('branch') or 'detached'}), "
                        f"{age / 3600:.1f} hours old"
                    )
                print(
                    views.render("gc", {"preserved": kept})
                    if args.json
                    else "\n".join(listed) or "No preserved worktree."
                )
                return 0
            if args.project:
                if args.force:
                    parser.error("--force does not apply to --project.")
                forgot = supervision.forget(
                    bridge.home,
                    args.project,
                    apply=args.apply,
                    discard=args.yes,
                )
                print(
                    views.render("gc", {"project": forgot})
                    if args.json
                    else "\n".join(supervision.forgotten_lines(forgot))
                )
                return 0
            moved = bridge.relocate_worktrees(
                args.repo.resolve(), apply=args.apply
            )
            swept = bridge.reclaim(args.repo.resolve(), apply=args.apply)
            made = bridge.reclaim_worktrees(
                args.repo.resolve(),
                apply=args.apply,
                sizes=not args.apply,
                force=args.force,
            )
            print(
                views.render(
                    "gc",
                    {"lanes": swept, "worktrees": made, "relocations": moved},
                )
                if args.json
                else "\n".join(reclaim.lines(moved + swept + made))
                or "No lane to reclaim."
            )
        elif args.command == "notify" and args.action == "setup":
            path = notify.store(
                bridge.home,
                webhook_settings(notify.stored(bridge.home), args.inbound)
                if args.webhook
                else secret_settings(args.chat, args.inbound, args.users),
            )
            print(
                f"Notification settings stored in {path}.\n"
                + notification_line(
                    notify.reported(notify.environment(bridge.home)),
                    inbound_status(bridge.home),
                )
            )
            if args.inbound:
                print(
                    "A running service reads inbound settings at start: "
                    "agent-parley down && agent-parley up"
                )
            if args.inbound and args.chat.strip().startswith("-"):
                print(
                    "In a group chat, typed queries reach the bot only after "
                    "its privacy mode is disabled with BotFather."
                )
        elif args.command == "notify":
            probed = notify.probe(args.repo.resolve().name, bridge.home)
            received = inbound_status(bridge.home)
            probed["inbound"] = {
                **received,
                "polling": received["enabled"]
                and not received["fault"]
                and inbound.polling(bridge.home),
            }
            print(
                views.render("notify", probed)
                if getattr(args, "json", False)
                else notification_report(probed)
            )
            return 0 if all(item["ok"] for item in probed["results"]) else 1
        elif args.command == "plan" and args.action == "update":
            recorded = bridge.task_plan(
                args.repo.resolve(),
                args.step,
                sys.stdin.read() if args.stdin else "",
            )
            clipped = (
                " (clipped to the plan cap)" if recorded["truncated"] else ""
            )
            print(f"Recorded {len(recorded['items'])} plan steps{clipped}.")
        elif args.command == "plan" and args.action in (
            "propose",
            "approve",
            "reject",
            "proposals",
        ):
            revised = bridge.plan_revision(
                args.repo.resolve(),
                args.action,
                getattr(args, "proposal_id", ""),
                base=getattr(args, "base", 0),
                add=getattr(args, "add", None),
                remove=getattr(args, "remove", None),
                reason=getattr(args, "reason", ""),
                evidence=getattr(args, "evidence", None),
            )
            listing = args.action == "proposals"
            print(
                views.render(
                    "plan_proposals" if listing else "plan_revision", revised
                )
                if args.json
                else plan.render_revisions(revised)
                if listing
                else plan.render_proposal(revised)
            )
            if args.action == "approve" and revised["status"] != "accepted":
                return 1
        elif args.command == "plan":
            applied = bridge.work_plan(
                args.repo.resolve(),
                args.action,
                getattr(args, "path", None),
                force=getattr(args, "force", False),
            )
            if args.action == "apply" and applied.get("replayed"):
                print(
                    f"Plan {applied['name']} is already applied; "
                    "nothing changed."
                )
            elif args.action == "apply":
                print(
                    f"Applied plan {applied['name']} "
                    f"({applied['digest'][:12]}): "
                    f"{len(applied['added'])} dependencies recorded."
                )
            elif args.action == "show":
                print(
                    views.render("plan", views.work_plan(applied))
                    if args.json
                    else plan.render(applied)
                )
            else:
                print(
                    views.render("plan_diff", views.plan_diff(applied))
                    if args.json
                    else plan.render_diff(applied)
                )
        elif args.command == "mail":
            page = bridge.mail(
                args.repo.resolve(),
                args.action,
                thread=getattr(args, "thread_id", ""),
                query=getattr(args, "query", ""),
                after=getattr(args, "after_id", 0),
                limit=getattr(args, "limit", store.MAX_SEARCH_HITS),
                identifier=getattr(args, "item_id", 0)
                or getattr(args, "message_id", 0),
                full=getattr(args, "full", False),
                participant=getattr(args, "reader", ""),
            )
            if args.action == "cancel":
                state = "cancelled" if page["cancelled"] else "not pending"
                print(
                    views.render("mail_cancel", page)
                    if args.json
                    else f"Operator item {page['id']}: {state}."
                )
            elif args.action == "show" and not args.json:
                print(shown_record(page))
            else:
                print(
                    views.render(f"mail_{args.action}", page)
                    if getattr(args, "json", False)
                    else json.dumps(page, indent=2)
                )
        elif args.command == "participant" and args.action == "show":
            repository = args.repo.resolve()
            record, entry = lane_reading(bridge, repository, args.name)
            if args.json:
                print(
                    views.render(
                        "participant",
                        views.participant_detail(record, entry),
                    )
                )
            else:
                _, directory = bridge.project(repository, create=False)
                lane_detail(record, roster.read(directory))
                for line in recap_lines(directory, args.name):
                    print(line)
                for line in plan_lines(
                    lanes.lane_task_plan(directory, args.name)
                ):
                    print(line)
        elif args.command == "decide":
            recorded = bridge.decide(
                args.repo.resolve(), args.text, args.subject, args.key
            )
            print(
                views.render("decide", recorded)
                if args.json
                else f"Recorded decision {recorded['id']}."
            )
        elif args.command == "decision":
            decisions = bridge.decisions(
                args.repo.resolve(), args.query, args.limit, args.since
            )
            print(
                views.render("decision_list", decisions)
                if args.json
                else fitted(decision_lines(decisions), terminal_width())
            )
        elif args.command == "participant":
            repository = args.repo.resolve()
            preview = getattr(args, "preview", False)
            if args.action in BULK_PARTICIPANT and selected(args):
                return participant_lanes(bridge, repository, args)
            if args.action in BULK_PARTICIPANT and not args.name:
                parser.error(
                    f"participant {args.action} needs a participant name or "
                    "a lane selector."
                )
            if args.action == "add":
                added = bridge.add_participant(
                    repository, args.name, args.provider, args.credentials
                )
                if gap := supervision.opt_in_adapter(
                    bridge.home, added, args.name
                ):
                    print(
                        supervision.OPT_IN_WARNING.format(
                            name=args.name, adapter=gap
                        ),
                        file=sys.stderr,
                    )
            elif args.action == "restore":
                print(bridge.restore(repository, args.name))
            elif args.action == "retire" and args.ready:
                return retired_ready(bridge, repository, args.yes)
            elif args.action == "retire":
                print(retired_lane(bridge, repository, args.name, args.yes))
            elif args.action == "merge":
                print(merged_lanes(bridge, repository, args, preview))
            elif args.action == "pr":
                print(bridge.pull_request(repository, args.name))
            elif args.action in ("pause", "resume"):
                print(
                    bridge.pause(
                        repository,
                        args.name,
                        resume=args.action == "resume",
                    )
                )
            elif args.action == "stop":
                print(bridge.stop(repository, args.name))
            elif args.action == "restart":
                return bridge.restart(repository, args.name, args.task)
            elif args.action == "budget":
                print(
                    bridge.budget(
                        repository,
                        "participant",
                        args.name,
                        budget_changes(args),
                    )
                )
                unmeasured_lanes(bridge, repository, args, [args.name])
                return 0
            if getattr(args, "json", False):
                _, directory = bridge.project(repository)
                data = roster.read(directory)
                print(
                    views.render(
                        "participants",
                        {
                            "root": data["root"],
                            "participants": views.participants(data),
                        },
                    )
                )
            elif not preview:
                _, directory = bridge.project(repository)
                print(roster.describe(roster.read(directory)))
        elif args.command == "branch":
            repository = args.repo.resolve()
            if getattr(args, "json", False):
                _, directory = bridge.project(repository, create=False)
                data = roster.read(directory)
                print(
                    views.render(
                        "branch",
                        {
                            "root": data["root"],
                            "prefix": data["branch_prefix"],
                            "integration_base": data.get(
                                "integration_base", ""
                            ),
                        },
                    )
                )
            elif args.action == "integration":
                print(bridge.integration_branch(repository, args.integration))
            else:
                print(
                    bridge.branch_naming(
                        repository, getattr(args, "prefix", None)
                    )
                )
        elif args.command == "forge":
            repository = args.repo.resolve()
            if getattr(args, "json", False):
                _, directory = bridge.project(repository, create=False)
                data = roster.read(directory)
                print(
                    views.render(
                        "forge", {"root": data["root"], "forge": data["forge"]}
                    )
                )
            else:
                print(bridge.tracker(repository, getattr(args, "name", None)))
        elif args.command == "deadlines":
            repository = args.repo.resolve()
            if getattr(args, "json", False):
                _, directory = bridge.project(repository, create=False)
                data = roster.read(directory)
                print(
                    views.render(
                        "deadlines",
                        {
                            "root": data["root"],
                            "deadlines": data["deadlines"],
                        },
                    )
                )
            elif args.action == "set":
                print(
                    bridge.budgets(
                        repository,
                        {
                            "claim": args.claim,
                            "offer": args.offer,
                            "request": args.request,
                            "ack": args.ack,
                            "wait": args.wait,
                            "attempts": args.attempts,
                        },
                    )
                )
            else:
                print(bridge.budgets(repository))
        elif args.command == "budget":
            repository = args.repo.resolve()
            if getattr(args, "json", False):
                _, directory = bridge.project(repository, create=False)
                data = roster.read(directory)
                print(
                    views.render(
                        "budget",
                        {"root": data["root"], "budget": data["budget"]},
                    )
                )
            elif args.action == "enforce":
                if (
                    unmeasured_lanes(bridge, repository, args)
                    and not args.allow_unmeasured
                ):
                    raise BridgeError(
                        "An enforced token limit could not stop the lanes "
                        "named above while they run, so it is not recorded; "
                        "pass --allow-unmeasured to enforce it anyway."
                    )
                print(bridge.enforce(repository, budget_changes(args)))
            elif args.action == "resume":
                from agent_parley import unattended

                root, directory = bridge.project(repository, create=False)
                data = roster.read(directory)
                unattended.operator_only(
                    repository, root, data, "A run budget is resumed"
                )
                print(budgets.resume(bridge.home, directory, data, args.reset))
            else:
                print(
                    bridge.budget(
                        repository,
                        "project",
                        "",
                        budget_changes(args) if args.action == "set" else None,
                    )
                )
                unmeasured_lanes(bridge, repository, args)
        elif args.command == "resources":
            repository = args.repo.resolve()
            if getattr(args, "json", False):
                _, directory = bridge.project(repository, create=False)
                data = roster.read(directory)
                print(
                    views.render(
                        "resources",
                        {
                            "root": data["root"],
                            "resources": data["resources"],
                            "declared": bool(data["resources"]),
                        },
                    )
                )
            else:
                print(
                    bridge.resources(repository, getattr(args, "names", None))
                )
        elif args.command == "approve":
            decided = bridge.approve(args.repo.resolve(), args.participant)
            print(
                views.render(
                    "approve",
                    {
                        "participant": args.participant,
                        "decision": "approved",
                        "detail": decided,
                    },
                )
                if args.json
                else decided
            )
        elif args.command == "reject":
            decided = bridge.reject(
                args.repo.resolve(), args.participant, args.reason
            )
            print(
                views.render(
                    "reject",
                    {
                        "participant": args.participant,
                        "decision": "rejected",
                        "reason": args.reason,
                        "detail": decided,
                    },
                )
                if args.json
                else decided
            )
        elif args.command == "approval":
            repository = args.repo.resolve()
            if args.action == "resume":
                print(
                    bridge.resume_opt_in(
                        repository,
                        args.participant,
                        (
                            None
                            if args.bridge_tools is None
                            else args.bridge_tools == "on"
                        ),
                        (
                            None
                            if args.auto_mode is None
                            else args.auto_mode == "on"
                        ),
                    )
                )
            elif getattr(args, "json", False):
                _, directory = bridge.project(repository, create=False)
                data = roster.read(directory)
                print(
                    views.render(
                        "approval",
                        {
                            "root": data["root"],
                            "approval": list(data["approval"]),
                            "required": bool(data["approval"]),
                        },
                    )
                )
            else:
                print(
                    bridge.approval_policy(
                        repository,
                        args.steps if args.action == "set" else None,
                    )
                )
        elif args.command == "timeout":
            from agent_parley import timeouts

            repository = args.repo.resolve()
            if args.action == "set":
                print(
                    timeouts.configure(
                        bridge,
                        repository,
                        args.kind,
                        ask=args.ask,
                        seconds=args.after,
                    )
                )
            else:
                print(timeouts.show(bridge, repository))
        elif args.command == "blueprint":
            from agent_parley import blueprints

            repository = getattr(args, "repo", Path.cwd()).resolve()
            if args.action == "templates":
                account = blueprints.catalog(args.json)
            elif args.action == "set":
                account = blueprints.define(
                    bridge,
                    repository,
                    args.name,
                    args.file,
                    args.template,
                    args.verify,
                )
            elif args.action == "remove":
                account = blueprints.define(bridge, repository, args.name, None)
            elif args.action == "run":
                account = blueprints.start(
                    bridge,
                    repository,
                    args.participant,
                    args.blueprint,
                    args.issue,
                )
            elif args.action == "advance":
                account = blueprints.advance(bridge, repository, args.issue)
            elif args.action == "progress":
                account = blueprints.progress(bridge, repository, args.issue)
            elif args.action == "default":
                account = blueprints.default(
                    bridge, repository, args.label, args.blueprint
                )
            else:
                account = blueprints.describe(
                    bridge, repository, getattr(args, "name", None)
                )
            print(account)
        elif args.command == "unattended":
            from agent_parley import unattended

            repository = args.repo.resolve()
            if args.action == "set":
                print(
                    unattended.configure(
                        bridge,
                        repository,
                        args.target,
                        args.issues,
                        args.auto,
                    )
                )
            elif args.action == "run":
                print(
                    unattended.integrate(
                        bridge, repository, args.name, args.issue
                    )
                )
            else:
                print(unattended.describe(bridge, repository))
        elif args.command == "pool":
            from agent_parley import pool

            repository = args.repo.resolve()
            if args.action == "fill":
                print(bridge.fill_pool(repository))
            elif args.action == "show" and args.json:
                _, directory = bridge.project(repository, create=False)
                print(
                    views.render(
                        "pool",
                        pool.reading(directory, roster.read(directory)),
                    )
                )
            else:
                print(bridge.pool(repository, getattr(args, "size", None)))
        elif args.command in ("verify", "init"):
            repository = args.repo.resolve()
            if getattr(args, "json", False):
                configured = bridge.commands(repository)
                stored = configured[
                    "verify" if args.command == "verify" else "initialize"
                ]
                print(
                    views.render(
                        args.command,
                        {
                            "root": configured["root"],
                            "command": stored,
                            "configured": bool(stored),
                        },
                    )
                )
            elif args.command == "verify":
                print(
                    bridge.verification(
                        repository, getattr(args, "command_line", None)
                    )
                )
            else:
                print(
                    bridge.initialization(
                        repository, getattr(args, "command_line", None)
                    )
                )
        elif args.command == "provider" and args.action == "show":
            inspected = roster.inspect(bridge.home).get(args.name)
            if inspected is None:
                raise BridgeError(
                    f"Unknown provider {args.name!r}; run "
                    "agent-parley provider list."
                )
            print(
                views.render(
                    "provider", views.provider_detail(args.name, inspected)
                )
                if args.json
                else fitted(
                    detail_lines(views.provider_detail(args.name, inspected)),
                    terminal_width(),
                )
            )
        elif args.command == "quota":
            from agent_parley import quota

            if args.action == "set":
                recorded = quota.set_window(
                    bridge.home,
                    args.name,
                    args.hours,
                    args.tokens,
                    args.threshold,
                )
                print(json.dumps({args.name: recorded.get(args.name)}))
            else:
                _, directory = bridge.project(args.repo.resolve(), create=False)
                rows = quota.headroom(quota.read(directory), time.time())
                print(
                    views.render("quota", {"credentials": rows})
                    if args.json
                    else "\n".join(quota.lines(rows))
                )
        elif args.command == "credentials" and args.action == "show":
            profile_shown = views.credential_detail(
                args.name, roster.credential(bridge.home, args.name)
            )
            print(
                views.render("credentials_show", profile_shown)
                if args.json
                else fitted(detail_lines(profile_shown), terminal_width())
            )
        elif args.command == "provider":
            if args.action == "add":
                roster.define_provider(
                    bridge.home,
                    args.name,
                    args.adapter,
                    args.executable,
                    args.home_env,
                    args.env,
                    args.require_env,
                )
                if args.name in roster.PRESETS:
                    print(
                        f"Warning: {args.name!r} shadows a built-in preset; "
                        "provider remove restores it.",
                        file=sys.stderr,
                    )
            elif args.action == "remove":
                roster.remove(bridge.home, "provider", args.name)
            elif args.action == "budget":
                print(
                    bridge.budget(
                        Path.cwd(), "provider", args.name, budget_changes(args)
                    )
                )
                from agent_parley import records

                late = records.counts_at_end(bridge.home, args.name)
                if args.tokens and (
                    late or not records.measures_tokens(bridge.home, args.name)
                ):
                    print(
                        records.unmeasured_warning(
                            [args.name], [args.name] if late else []
                        ),
                        file=sys.stderr,
                    )
                return 0
            elif args.action == "price":
                print(bridge.price(Path.cwd(), args.name, args.per_mtok))
                return 0
            defined = roster.inspect(bridge.home)
            print(
                views.render(
                    "providers",
                    {
                        "providers": [
                            {"name": name, **entry}
                            for name, entry in sorted(defined.items())
                        ]
                    },
                )
                if getattr(args, "json", False)
                else views.provider_text(args.name, defined[args.name])
                if args.action == "add"
                else json.dumps(defined, indent=2)
            )
        elif args.command == "credentials":
            if args.action == "add":
                if not (args.config_home or args.env or args.require_env):
                    parser.error(roster.EMPTY_CREDENTIAL)
                roster.define_credential(
                    bridge.home,
                    args.name,
                    args.config_home,
                    args.env,
                    args.require_env,
                )
            elif args.action == "remove":
                roster.remove(bridge.home, "credentials", args.name)
            registered = {
                name: {**entry, "env": views.redacted(entry.get("env") or {})}
                for name, entry in roster.credentials(bridge.home).items()
            }
            print(
                views.render(
                    "credentials",
                    {
                        "credentials": [
                            {"name": name, **entry}
                            for name, entry in sorted(registered.items())
                        ]
                    },
                )
                if getattr(args, "json", False)
                else views.credential_text(
                    args.name, roster.credential(bridge.home, args.name)
                )
                if args.action == "add"
                else json.dumps(registered, indent=2)
            )
        else:
            selection = selected_status(args)
            workspace = getattr(args, "workspace", "")
            if workspace:
                from agent_parley import projects

                selection = selection._replace(
                    workspace=tuple(projects.members(bridge.home, workspace))
                )
            if args.json:
                narrowed = narrow(bridge.status_snapshot(), selection)
                print(views.render("status", narrowed))
                matched = reported_lanes(narrowed)
            else:
                every_project = bool(workspace) or getattr(
                    args, "all_projects", False
                )
                if not selection.project and not every_project:
                    selection = selection._replace(
                        project=bridge.project_at(Path.cwd())
                    )
                if (
                    getattr(args, "table", False)
                    or selection._replace(project="").filtered()
                ):
                    matched = bridge.status(selection, terminal_width())
                else:
                    matched = bridge.board(
                        selection,
                        terminal_width(),
                        every_claim=getattr(args, "all", False),
                        every_project=every_project,
                    )
            if matched and (
                selection.drifted or selection.pending or selection.over_budget
            ):
                return 1
        return 0
    except (
        BridgeError,
        KeyError,
        OSError,
        ValueError,
        sqlite3.Error,
        subprocess.TimeoutExpired,
    ) as exc:
        print(f"agent-parley: {_refusal_lines(exc)}", file=sys.stderr)
        if getattr(args, "json", False):
            print(_error_document(exc))
        return 1


def _error_message(exc: Exception) -> str:
    """Words one runtime failure as the line the CLI prints.

    A missing key reads as its quoted key alone. State files name themselves
    when they lack a required key, so one reaching the handler is either a
    record another process removed meanwhile or a fault in the code, and the
    message carries the innermost frame that raised it so a rerun that fails
    the same way can be traced.

    Args:
        exc: Failure the command handler caught.

    Returns:
        The message both the human line and the error document carry.
    """
    if isinstance(exc, KeyError):
        place = supervision.raised_at(exc)
        where = f" (at {place})" if place else ""
        return (
            f"No record {exc.args[0]!r}{where}; if another command changed "
            "the project meanwhile, rerun it, otherwise report this line."
        )
    return str(exc)


def _refusal_lines(exc: Exception) -> str:
    """Words one runtime failure as the human refusal on standard error.

    The message comes first, and a failure that names its resolving command
    ends on a ``next:`` line carrying it, in the shape `refusal` gives every
    refusal. The ``--json`` error document keeps the message alone, so its
    fields are unchanged.

    Args:
        exc: Failure the command handler caught.

    Returns:
        The message, followed by a ``next:`` line when the failure has one.
    """
    if isinstance(exc, KeyError):
        return _error_message(exc)
    return refusal(exc)


def _error_document(exc: Exception) -> str:
    """Frames one runtime failure as a single-line JSON error document.

    The document carries the shared snapshot envelope with ``kind`` set to
    ``error``, a stable ``type`` for the exception class and the same
    ``message`` the human line prints. A missing key also carries ``where``,
    the innermost frame that raised it. It fits on one line so it stays a
    separate record after any partial output a command already wrote.

    Args:
        exc: Failure the command handler caught.

    Returns:
        The error document as compact JSON text, without a trailing newline.
    """
    if isinstance(exc, BridgeError):
        kind = "bridge"
    elif isinstance(exc, subprocess.TimeoutExpired):
        kind = "timeout"
    elif isinstance(exc, OSError):
        kind = "os"
    elif isinstance(exc, KeyError):
        kind = "missing"
    elif isinstance(exc, sqlite3.Error):
        kind = "store"
    else:
        kind = "value"
    error = {"type": kind, "message": _error_message(exc)}
    if isinstance(exc, KeyError):
        error["where"] = supervision.raised_at(exc)
    return json.dumps(
        views.document("error", {"error": error}), ensure_ascii=False
    )


if __name__ == "__main__":
    raise SystemExit(main())
