"""Launches native agents with isolated worktrees and in-house coordination."""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable, Sequence
from collections.abc import Iterator as Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from typing import NamedTuple as NamedTuple

from agent_parley import BridgeError as BridgeError
from agent_parley.claims import Claims
from agent_parley.copilot import COPILOT_EVENTS as COPILOT_EVENTS
from agent_parley.copilot import bridge_hook as bridge_hook
from agent_parley.copilot import configure_copilot as configure_copilot
from agent_parley.copilot import owned_hook as owned_hook
from agent_parley.copilot import release_copilot as release_copilot
from agent_parley.integration import Integration
from agent_parley.integration import attributed_commits as attributed_commits
from agent_parley.integration import group_lanes as group_lanes
from agent_parley.integration import group_refusal as group_refusal
from agent_parley.integration import lane_dependencies as lane_dependencies
from agent_parley.integration import lane_refusals as lane_refusals
from agent_parley.integration import lane_session as lane_session
from agent_parley.integration import merge_blockers as merge_blockers
from agent_parley.integration import merge_branch as merge_branch
from agent_parley.integration import merge_preview as merge_preview
from agent_parley.integration import (
    outside_prerequisites as outside_prerequisites,
)
from agent_parley.integration import ready_lanes as ready_lanes
from agent_parley.integration import reported_ready as reported_ready
from agent_parley.integration import unattempted as unattempted
from agent_parley.launch import LAUNCH_LOCK_SECONDS as LAUNCH_LOCK_SECONDS
from agent_parley.launch import Launch
from agent_parley.lazy import DeferredCallable as _DeferredCallable
from agent_parley.lazy import deferred as deferred
from agent_parley.lazy import deferred_module as deferred_module
from agent_parley.mail import Mail
from agent_parley.mail import operator_key as operator_key
from agent_parley.mail import repeat_plan as repeat_plan
from agent_parley.reports import Reports
from agent_parley.reports import report_comment as report_comment
from agent_parley.settings import Settings
from agent_parley.status import Selection as Selection
from agent_parley.status import Status
from agent_parley.status import inbound_status as inbound_status
from agent_parley.status import lane_detail as lane_detail
from agent_parley.status import narrow as narrow
from agent_parley.status import pending_offers as pending_offers
from agent_parley.status import reported_lanes as reported_lanes
from agent_parley.status import review_fields as review_fields
from agent_parley.status import reviewed_line as reviewed_line
from agent_parley.status import shown_record as shown_record
from agent_parley.status import supervision_failure as supervision_failure
from agent_parley.status import supervision_liveness as supervision_liveness
from agent_parley.status import wake_schedule as wake_schedule
from agent_parley.worktrees import GIT_SECONDS as GIT_SECONDS
from agent_parley.worktrees import INIT_OUTPUT_LINES as INIT_OUTPUT_LINES
from agent_parley.worktrees import VERIFY_TIMEOUT as VERIFY_TIMEOUT
from agent_parley.worktrees import Worktrees as Worktrees
from agent_parley.worktrees import drift as drift
from agent_parley.worktrees import exact_claim as exact_claim
from agent_parley.worktrees import git as git
from agent_parley.worktrees import has_branch as has_branch
from agent_parley.worktrees import held_claim as held_claim
from agent_parley.worktrees import initialize_lane as initialize_lane
from agent_parley.worktrees import preserve_pending as preserve_pending
from agent_parley.worktrees import session_busy as session_busy
from agent_parley.worktrees import verify_base as verify_base

if TYPE_CHECKING:
    import argparse
    import contextlib
    import datetime
    import json
    import secrets
    import shlex
    import shutil
    import socket
    import string
    import subprocess
    import textwrap

    from agent_parley import (
        archive,
        budgets,
        checkpoints,
        completion,
        evidence,
        forge,
        history,
        issues,
        metrics,
        notify,
        plan,
        problems,
        process,
        protocol,
        reclaim,
        recommend,
        roster,
        state,
        store,
        supervision,
        terminal,
        views,
    )
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
    from agent_parley.issues import attempt as change_attempt
    from agent_parley.issues import (
        change,
        deadline_state,
        describe,
        handoff_fields,
        offer_state,
        orphan_age,
        parse_issue,
        snapshot,
    )
    from agent_parley.state import lock, write_json, write_text

DEFERRED_MODULES = (
    "amp",
    "approvals",
    "archive",
    "attachments",
    "budgets",
    "checkpoints",
    "completion",
    "delivery",
    "dialogs",
    "evidence",
    "forecast",
    "forge",
    "gemini",
    "history",
    "inbound",
    "issues",
    "lanes",
    "lifecycle",
    "metrics",
    "notify",
    "opencode",
    "plan",
    "policy",
    "problems",
    "process",
    "protocol",
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
)
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
    "string",
    "subprocess",
    "textwrap",
    "uuid",
)
DEFERRED_ALIASES = {"watch": "stream"}

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
RETRY_HELP = (
    "Idempotency key. Retry a failed command with the key it first used and "
    "the repeat returns the first result without applying the change again. "
    "The same key with different arguments is refused."
)


MAX_HEALTH_BYTES = 65536
START_SECONDS = 25.0


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
    if seconds <= 0:
        raise ValueError(f"{text!r} is not a window; use 45m, 6h or 7d.")
    return seconds


def clock(text: str) -> float:
    """Converts a wall-clock time of day into the next instant it names.

    The time of day is read in the timezone of the machine the operator types
    on, and the resulting instant is recorded absolutely. A later timezone
    change, a daylight-saving transition or a service restart therefore moves
    nothing: the item keeps the instant it was recorded for.

    Args:
        text: A 24-hour time of day such as ``15:00``.

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
    now = datetime.datetime.now().astimezone()
    target = datetime.datetime.combine(now.date(), moment, now.tzinfo)
    if target <= now:
        target += datetime.timedelta(days=1)
    return target.timestamp()


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
    whatever the others leave.

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
        0 when every matched lane was done, when nothing matched, or when the
        operator declined; 1 when any lane refused or failed.
    """
    proposal = selection_plan(action, names, notes)
    if not names:
        print(proposal)
        return 0
    if not confirmed(proposal, assume_yes):
        print("Declined: nothing was done.")
        return 0
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
        BridgeError: If the selection is ambiguous or names nothing, or if
            the ordered run stops on a refusal or a failure.
    """
    narrowing = bool(
        args.provider
        or args.outcome
        or args.drifted
        or args.idle
        or getattr(args, "over_budget", False)
    )
    if selected(args) or args.group:
        if args.name:
            raise BridgeError(
                "`participant merge` integrates one named lane, every ready "
                "lane with --all, a selected set, or one group with --group "
                "NAME. Drop the participant name to integrate a set."
            )
        names: list[str] = []
        if narrowing:
            directory, data = lane_roster(bridge, repo)
            names = matching_lanes(bridge.home, directory, data, args)
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
            return "Declined: nothing was merged."
        return bridge.integrate(repo, group=args.group, lanes=names)
    if not args.name:
        raise BridgeError(
            "`participant merge` needs a participant name, --all, a "
            "selector or --group NAME."
        )
    return (
        bridge.preview_merge(repo, args.name)
        if preview
        else bridge.merge(repo, args.name)
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
    changed: list[str], held: dict[str, list[str]]
) -> list[str]:
    """Names the peer reservations a lane's own changed paths run into.

    Args:
        changed: Repository-relative paths the lane's branch changed.
        held: Active reservation keys per peer identity, with the lane's own
            identity already removed.

    Returns:
        One sentence per overlap, ordered by path and then by peer, naming
        the peer, the key it holds and the changed path that key covers.
    """
    return [
        f"{peer} reserves {pattern!r}, which covers {path!r}"
        for path in sorted(changed)
        for peer, patterns in sorted(held.items())
        for pattern in sorted(patterns)
        if store.overlapping(path, pattern)
    ]


class Bridge(Settings, Integration, Claims, Mail, Reports, Status, Launch):
    """Coordinates native agent worktrees using one private local state root.

    Each group of commands lives in a mixin of its own module; this class
    combines them and keeps the service lifecycle and the pull request path.

    Attributes:
        home: Resolved private state directory.
        config: Local HTTP port and bearer credential.
        url: Loopback HTTP origin of the mail server.
    """

    def __init__(self, home: Path) -> None:
        """Loads or initializes private configuration under home."""
        self.home = home.expanduser().resolve()
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.home.stat().st_mode & 0o077:
            raise BridgeError(
                f"State directory must be private: chmod 700 {self.home}"
            )
        path = self.home / "config.json"
        if not path.exists():
            with lock(self.home / "config.lock"):
                if not path.exists():
                    port = int(os.environ.get("AGENT_PARLEY_PORT", "8876"))
                    if not 1024 <= port <= 65535:
                        raise BridgeError(
                            "AGENT_PARLEY_PORT must be between 1024 and 65535."
                        )
                    write_json(
                        path,
                        {"port": port, "token": secrets.token_urlsafe(32)},
                    )
        self.config = json.loads(path.read_text())
        self.url = f"http://127.0.0.1:{self.config['port']}"

    def server_process(self) -> process.ServerProcess | None:
        """Returns the recorded server only if its process identity matches."""
        record = self.home / "server.json"
        if not record.exists():
            return None
        data = json.loads(record.read_text())
        return process.identify(data, self.home)

    def health(self) -> dict:
        """Reads the service's own account of itself, without a proxy.

        The service answers what code it started on and whether the checkout
        has moved past it, so a reading here reports drift the launcher
        cannot see from its own process.

        Returns:
            The readiness document, or an empty mapping when nothing answers
            on the configured port.
        """
        port = int(self.config["port"])
        authority = f"127.0.0.1:{port}"
        request = (
            "GET /health/readiness HTTP/1.0\r\n"
            f"Host: {authority}\r\n"
            f"Authorization: Bearer {self.config['token']}\r\n"
            "Connection: close\r\n\r\n"
        ).encode()
        response = bytearray()
        try:
            with socket.create_connection(
                ("127.0.0.1", port), timeout=1
            ) as connection:
                connection.sendall(request)
                while len(response) <= MAX_HEALTH_BYTES:
                    chunk = connection.recv(
                        min(8192, MAX_HEALTH_BYTES + 1 - len(response))
                    )
                    if not chunk:
                        break
                    response.extend(chunk)
        except OSError:
            return {}
        headers, separator, body = bytes(response).partition(b"\r\n\r\n")
        status = headers.split(b"\r\n", 1)[0].split()[1:2]
        if (
            len(response) > MAX_HEALTH_BYTES
            or not separator
            or status != [b"200"]
        ):
            return {}
        try:
            document = json.loads(body)
        except ValueError:
            return {}
        return document if isinstance(document, dict) else {}

    def ready(self) -> bool:
        """Checks authenticated readiness without routing through proxies."""
        return self.health().get("status") == "ready"

    def up(self) -> None:
        """Starts the mail server with bounded readiness checking.

        The published record names a service that answered. A record found
        naming a process that is gone, as a reboot or a drift exit leaves
        behind, is removed before the new service is started. A start that
        fails, on an occupied port or a process never ready within
        `START_SECONDS`, publishes a record with ``state: failed``, the time
        and the count of consecutive failures instead, and names no process.
        Every reader of the record therefore learns of a service only once it
        serves, and the hooks' relaunch still finds a record to retry from,
        with a backoff the failure count sets.

        A service already serving is left running and the store is migrated
        in place to this build's schema, so repairing a store an upgrade left
        behind interrupts no lane.

        Raises:
            BridgeError: If the port is occupied or startup fails.
        """
        with lock(self.home / "server.lock"):
            published = self.home / "server.json"
            failures = 0
            if published.exists():
                record = json.loads(published.read_text())
                if record.get("state") == "failed":
                    failures = int(record.get("failures", 0))
                legacy = "pid" in record and "start_ticks" not in record
                if legacy and process.running(record["pid"]):
                    raise BridgeError(
                        "A service from an older installation is running. "
                        "Stop it with the command that started it before "
                        "upgrading; existing sessions are preserved."
                    )
            running = self.server_process()
            if running:
                if not self.ready():
                    raise BridgeError(
                        "Server is running but unhealthy. "
                        f"Inspect {self.home}/server.log"
                    )
                store.initialize(self.home)
                return
            failed = {
                "state": "failed",
                "failed_at": time.time(),
                "failures": failures + 1,
            }
            published.unlink(missing_ok=True)
            with socket.socket() as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    probe.bind(("127.0.0.1", self.config["port"]))
                except OSError:
                    write_json(published, failed)
                    raise BridgeError(
                        f"Port {self.config['port']} "
                        "is occupied by another service."
                    ) from None
            with (self.home / "server.log").open("ab") as log:
                child = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "agent_parley.server",
                        "--home",
                        str(self.home),
                    ],
                    cwd=self.home,
                    env=os.environ.copy(),
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    start_new_session=True,
                )
            identity = {
                "pid": child.pid,
                "start_ticks": process.start_ticks(child.pid),
            }
            deadline = time.monotonic() + START_SECONDS
            while time.monotonic() < deadline:
                if child.poll() is not None:
                    break
                if self.ready():
                    write_json(published, identity)
                    return
                time.sleep(0.2)
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
            write_json(published, failed)
            raise BridgeError(
                "Coordination server failed to start. "
                f"Inspect {self.home}/server.log"
            )

    def down(self) -> None:
        """Stops the identified server while retaining all persistent state.

        A start holds the same lock until the new service answers, which is
        bounded at thirty seconds including the wind-down of a service that
        never became ready. Refusing the moment that lock is held reported
        contention for a stop that was only queued behind a start, so the
        stop waits for that span before it reports the lock busy.

        Raises:
            BridgeError: If locking fails or the server does not stop in time.
        """
        with lock(self.home / "server.lock", timeout=30):
            running = self.server_process()
            if running:
                running.stop()
            (self.home / "server.json").unlink(missing_ok=True)

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
            with lock(
                directory / f"{name}-integration.lock",
                f"{name} is already opening its own pull request.",
            ):
                return self._pull_request(
                    repo, name, self._authorize(directory, data, name)
                )
        with lock(directory / f"{name}.session.lock"):
            return self._pull_request(repo, name)

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
        if overlaps := reserved_overlaps(changed, held):
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


COMMAND_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "Coordination",
        (
            "issue",
            "mail",
            "say",
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
            "init",
            "branch",
            "forge",
            "deadlines",
            "budget",
            "resources",
            "provider",
            "credentials",
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
            "plan",
            "state",
            "gc",
            "version",
            "completion",
        ),
    ),
)


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
    blocks = []
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
    command.add_argument("--repo", type=Path, default=Path.cwd())
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


def spoken(
    bridge: Bridge, parser: argparse.ArgumentParser, args: argparse.Namespace
) -> int:
    """Delivers one operator message, to a named lane or to a selected set.

    Args:
        bridge: Launcher holding the private coordination state.
        parser: Root parser, used to report a usage error.
        args: Parsed `say` or `mail send` arguments.

    Returns:
        0 when the message was delivered, 1 when a selected lane refused it.
    """
    repo = args.repo.resolve()
    if selected(args):
        return spoken_lanes(bridge, repo, args)
    if not args.participant or not args.text:
        parser.error(
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
            "agent-parley participant list."
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
    reported = reading["history"]
    lines.append(history.describe(reported["records"], reported["holdings"]))
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
    return "\n".join(lines)


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
    )
    completing.add_argument("shell", choices=completion.SHELLS)
    candidates = commands.add_parser("__complete", help=argparse.SUPPRESS)
    candidates.add_argument("kind", choices=completion.KINDS)
    released = commands.add_parser(
        "version",
        help="Print the installed version and the state directory in use.",
    )
    released.add_argument("--json", action="store_true", help=JSON_HELP)
    starting = commands.add_parser(
        "up", help="Start the local coordination server in the background."
    )
    starting.add_argument("--json", action="store_true", help=JSON_HELP)
    stopping = commands.add_parser(
        "down",
        help="Stop the coordination server; retain all data and worktrees.",
    )
    stopping.add_argument("--json", action="store_true", help=JSON_HELP)
    health = commands.add_parser(
        "status", help="Show server health and registered workspaces."
    )
    health.add_argument("--json", action="store_true", help=JSON_HELP)
    add_status_filters(health)
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
            "state; `watch` follows one lane's events as a stream."
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
            "PARTICIPANT,STATE,IDLE. Every column is shown by default."
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
    follow = commands.add_parser(
        "watch",
        help=(
            "Follow one lane's coordination events as a stream; `top` draws "
            "the dashboard, and the agent's conversation is never shown."
        ),
    )
    follow.add_argument("participant", help="Participant name to follow.")
    follow.add_argument("--repo", type=Path, default=Path.cwd())
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
    for subject, argument in (
        ("issue", "number"),
        ("participant", "name"),
        ("claim", "claim_id"),
    ):
        listing = subjects.add_parser(subject)
        listing.add_argument(argument)
        listing.add_argument("--repo", type=Path, default=Path.cwd())
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
        listing.add_argument("--participant", default="")
        listing.add_argument("--provider", default="")
        listing.add_argument("--issue", default="")
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
        export.add_argument("--repo", type=Path, default=Path.cwd())
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
    showing.add_argument("archive", type=Path)
    showing.add_argument("--json", action="store_true", help=JSON_HELP)
    importing = archives.add_parser(
        "import", help="Restore an archive into the state directory."
    )
    importing.add_argument("archive", type=Path)
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
    setup = commands.add_parser(
        "setup",
        help="Register a repository for coordination from committed HEAD.",
    )
    setup.add_argument("repo", type=Path)
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
    run.add_argument("--repo", type=Path, default=Path.cwd())
    run.add_argument("--resume", action="store_true")
    run.add_argument(
        "--task", default="Check shared coordination state and await my task."
    )
    run.add_argument("--json", action="store_true", help=JSON_HELP)
    report = commands.add_parser(
        "report", help="Record a partial, blocked, or ready-for-review handoff."
    )
    report.add_argument("--repo", type=Path, default=Path.cwd())
    report.add_argument("--state", choices=("partial", "blocked", "ready"))
    report.add_argument("--summary", default="")
    records = report.add_subparsers(dest="action")
    showing_report = records.add_parser(
        "show", help="Print one recorded report of this lane."
    )
    showing_report.add_argument("report_id")
    showing_report.add_argument("--repo", type=Path, default=Path.cwd())
    showing_report.add_argument(
        "--full",
        action="store_true",
        help="Print the whole attached evidence after the record.",
    )
    showing_report.add_argument("--json", action="store_true", help=JSON_HELP)
    reviewing_report = records.add_parser(
        "review",
        help="Record this lane's verdict on another lane's report.",
    )
    reviewing_report.add_argument("report_id")
    reviewing_report.add_argument("--repo", type=Path, default=Path.cwd())
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
    report.add_argument("--remaining", default="")
    report.add_argument("--evidence", default="")
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
        "--idempotency-key", default="", metavar="KEY", help=RETRY_HELP
    )
    steer = commands.add_parser(
        "say", help="Send one lane a coordination message as the operator."
    )
    add_say_arguments(steer)
    issue = commands.add_parser(
        "issue", help="Claim issues and explicitly hand off ownership."
    )
    actions = issue.add_subparsers(dest="action", required=True)
    for action in (
        "list",
        "claim",
        "release",
        "offer",
        "accept",
        "decline",
        "cancel",
        "block",
        "unblock",
    ):
        command = actions.add_parser(action)
        command.add_argument("--repo", type=Path, default=Path.cwd())
        if action == "list":
            command.add_argument("--json", action="store_true", help=JSON_HELP)
        else:
            command.add_argument("number")
            command.add_argument(
                "--idempotency-key",
                default="",
                metavar="KEY",
                help=RETRY_HELP,
            )
        if action in ("claim", "offer", "accept"):
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
            command.add_argument("--to", required=True)
            command.add_argument("--summary", required=True)
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
        if action in ("accept", "decline"):
            command.add_argument("--offer-id", required=True)
        if action in ("block", "unblock"):
            command.add_argument("--on", required=True)
    recovering = actions.add_parser(
        "recover",
        help=(
            "Authorize stopping the current live owner when a matching "
            "capacity observation is later published."
        ),
    )
    recovering.add_argument("number")
    recovering.add_argument("--repo", type=Path, default=Path.cwd())
    recovering.add_argument(
        "--reason",
        required=True,
        help="Operator rationale persisted with this exact claim approval.",
    )
    resolving = actions.add_parser(
        "resolve",
        help=(
            "End a claim whose holder never filed the completion its pull "
            "request already landed, recording the forge evidence."
        ),
    )
    resolving.add_argument("number")
    resolving.add_argument("--repo", type=Path, default=Path.cwd())
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
    choosing = actions.add_parser(
        "next",
        help=(
            "Rank the unclaimed issues this lane could take next, with the "
            "reason for each; it claims nothing."
        ),
    )
    choosing.add_argument("--repo", type=Path, default=Path.cwd())
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
    matching.add_argument("--repo", type=Path, default=Path.cwd())
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
    assigning.add_argument("--repo", type=Path, default=Path.cwd())
    assigning.add_argument("number")
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
    inspecting.add_argument("number")
    inspecting.add_argument("--repo", type=Path, default=Path.cwd())
    inspecting.add_argument("--json", action="store_true", help=JSON_HELP)
    checking = commands.add_parser(
        "doctor",
        help="Report launcher, plugin and store versions and their fit.",
    )
    checking.add_argument("--json", action="store_true", help=JSON_HELP)
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
    triage = triaging.add_subparsers(dest="action")
    acking = triage.add_parser(
        "ack",
        help=(
            "Record your own acknowledgement of one message a lane has left "
            "unanswered, clearing that condition and nothing else."
        ),
    )
    acking.add_argument("message_id", type=int)
    acking.add_argument("--repo", type=Path, default=Path.cwd())
    acking.add_argument("--json", action="store_true", help=JSON_HELP)
    collecting = commands.add_parser(
        "gc",
        aliases=["reclaim"],
        help=(
            "Reclaim the lane worktrees and branches whose work has landed, "
            "keeping and reporting every lane that still holds any."
        ),
    )
    collecting.add_argument("--repo", type=Path, default=Path.cwd())
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
    collecting.add_argument(
        "--force",
        action="store_true",
        help=(
            "With --apply, also remove lane worktrees kept only for "
            "uncommitted changes, unpushed commits or a recent change, after "
            "writing a recovery checkpoint of each."
        ),
    )
    collecting.add_argument("--json", action="store_true", help=JSON_HELP)
    notifying = commands.add_parser(
        "notify",
        help=(
            "Verify the outbound notification transports configured in the "
            "environment; exit 1 when one of them refuses the message."
        ),
    )
    notices = notifying.add_subparsers(dest="action", required=True)
    probing = notices.add_parser(
        "test",
        help="Send one test message on each configured transport.",
    )
    probing.add_argument("--repo", type=Path, default=Path.cwd())
    probing.add_argument("--json", action="store_true", help=JSON_HELP)
    planning = commands.add_parser(
        "plan", help="Apply, compare or show the recorded work-order plan."
    )
    steps = planning.add_subparsers(dest="action", required=True)
    for action in ("apply", "diff", "show"):
        command = steps.add_parser(action)
        command.add_argument("--repo", type=Path, default=Path.cwd())
        if action != "show":
            command.add_argument(
                "path", type=Path, help="TOML plan file to read."
            )
        if action != "apply":
            command.add_argument("--json", action="store_true", help=JSON_HELP)
    mail = commands.add_parser(
        "mail",
        help="Read mail, or list and cancel pending operator items.",
    )
    letters = mail.add_subparsers(dest="action", required=True)
    reading = letters.add_parser("thread")
    reading.add_argument("thread_id")
    reading.add_argument("--repo", type=Path, default=Path.cwd())
    reading.add_argument("--after-id", type=int, default=0)
    add_reader_argument(reading)
    reading.add_argument("--json", action="store_true", help=JSON_HELP)
    showing_mail = letters.add_parser(
        "show", help="Print one message you sent or received."
    )
    showing_mail.add_argument("message_id", type=int)
    showing_mail.add_argument("--repo", type=Path, default=Path.cwd())
    showing_mail.add_argument(
        "--full",
        action="store_true",
        help="Print the whole attachment after the stored body.",
    )
    add_reader_argument(showing_mail)
    showing_mail.add_argument("--json", action="store_true", help=JSON_HELP)
    finding = letters.add_parser("search")
    finding.add_argument("query")
    finding.add_argument("--repo", type=Path, default=Path.cwd())
    finding.add_argument("--limit", type=int, default=store.MAX_SEARCH_HITS)
    add_reader_argument(finding)
    finding.add_argument("--json", action="store_true", help=JSON_HELP)
    inbox = letters.add_parser(
        "list",
        help=(
            "List this lane's own mail, newest first, without a search "
            "query to write."
        ),
    )
    inbox.add_argument("--repo", type=Path, default=Path.cwd())
    inbox.add_argument("--limit", type=int, default=store.MAX_SEARCH_HITS)
    add_reader_argument(inbox)
    inbox.add_argument("--json", action="store_true", help=JSON_HELP)
    sending = letters.add_parser(
        "send",
        help=(
            "Send one lane a coordination message as the operator; the same "
            "command as `say`."
        ),
    )
    add_say_arguments(sending)
    waiting = letters.add_parser(
        "pending", help="List operator items recorded but not delivered."
    )
    waiting.add_argument("--repo", type=Path, default=Path.cwd())
    waiting.add_argument("--json", action="store_true", help=JSON_HELP)
    dropping = letters.add_parser(
        "cancel", help="Remove one recorded operator item before delivery."
    )
    dropping.add_argument("item_id", type=int)
    dropping.add_argument("--repo", type=Path, default=Path.cwd())
    dropping.add_argument("--json", action="store_true", help=JSON_HELP)
    deciding = commands.add_parser(
        "decide", help="Record one decision every lane of the project reads."
    )
    deciding.add_argument("text", help="Decision text participants read.")
    deciding.add_argument("--repo", type=Path, default=Path.cwd())
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
        "decision", help="Read the decisions recorded for this project."
    )
    decision_actions = decision.add_subparsers(dest="action", required=True)
    decision_list = decision_actions.add_parser("list")
    decision_list.add_argument(
        "query",
        nargs="?",
        default="",
        help="Text to match; without it the newest decisions are listed.",
    )
    decision_list.add_argument("--repo", type=Path, default=Path.cwd())
    decision_list.add_argument(
        "--limit", type=int, default=store.MAX_SEARCH_HITS
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
    listing = roles.add_parser("list")
    listing.add_argument("--repo", type=Path, default=Path.cwd())
    listing.add_argument("--json", action="store_true", help=JSON_HELP)
    reporting_lane = roles.add_parser(
        "show",
        help=(
            "Print one lane's branch, worktree, provider, advisory budget, "
            "current claims and last coordination."
        ),
    )
    reporting_lane.add_argument("name")
    reporting_lane.add_argument("--repo", type=Path, default=Path.cwd())
    reporting_lane.add_argument("--json", action="store_true", help=JSON_HELP)
    joining = roles.add_parser("add")
    joining.add_argument("name")
    joining.add_argument("--provider")
    joining.add_argument("--credentials")
    joining.add_argument("--repo", type=Path, default=Path.cwd())
    for action in (
        "restore",
        "retire",
        "merge",
        "pr",
        "pause",
        "resume",
        "stop",
        "restart",
    ):
        command = roles.add_parser(action)
        if action in BULK_PARTICIPANT or action == "merge":
            command.add_argument("name", nargs="?", default="")
        else:
            command.add_argument("name")
        command.add_argument("--repo", type=Path, default=Path.cwd())
        if action == "merge":
            command.add_argument("--preview", action="store_true")
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
            command.add_argument("--task", default="")
    limiting = roles.add_parser(
        "budget",
        help=(
            "Show or set a lane's advisory token, call and hour limits; "
            "crossing one marks the lane and stops nothing."
        ),
    )
    limiting.add_argument("name")
    limiting.add_argument("--repo", type=Path, default=Path.cwd())
    add_budget_flags(limiting)
    granting = commands.add_parser(
        "approve",
        help="Record that you approve a lane's ready report for integration.",
    )
    granting.add_argument(
        "participant", help="Participant whose ready report you approve."
    )
    granting.add_argument("--repo", type=Path, default=Path.cwd())
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
    refusing.add_argument("--repo", type=Path, default=Path.cwd())
    refusing.add_argument("--json", action="store_true", help=JSON_HELP)
    requirement = commands.add_parser(
        "approval",
        help="Show or set the steps that require a recorded approval first.",
    )
    requirements = requirement.add_subparsers(dest="action", required=True)
    stating = requirements.add_parser("show")
    stating.add_argument("--repo", type=Path, default=Path.cwd())
    stating.add_argument("--json", action="store_true", help=JSON_HELP)
    requiring = requirements.add_parser("set")
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
    requiring.add_argument("--repo", type=Path, default=Path.cwd())
    gate = commands.add_parser(
        "verify",
        help="Show or set the command a repository requires before a merge.",
    )
    gates = gate.add_subparsers(dest="action", required=True)
    showing = gates.add_parser("show")
    showing.add_argument("--repo", type=Path, default=Path.cwd())
    showing.add_argument("--json", action="store_true", help=JSON_HELP)
    setting = gates.add_parser("set")
    setting.add_argument(
        "command_line",
        metavar="COMMAND",
        help=(
            "Command run in the base checkout before every merge; pass an "
            "empty string to remove the gate. No flag skips it."
        ),
    )
    setting.add_argument("--repo", type=Path, default=Path.cwd())
    preparation = commands.add_parser(
        "init",
        help="Show or set the command every new lane runs before it starts.",
    )
    preparations = preparation.add_subparsers(dest="action", required=True)
    reporting = preparations.add_parser("show")
    reporting.add_argument("--repo", type=Path, default=Path.cwd())
    reporting.add_argument("--json", action="store_true", help=JSON_HELP)
    recording = preparations.add_parser("set")
    recording.add_argument(
        "command_line",
        metavar="COMMAND",
        help=(
            "Command run in every new lane before its agent starts; pass an "
            "empty string to remove it. AGENT_PARLEY_BASE names the base "
            "checkout while it runs. No flag skips it."
        ),
    )
    recording.add_argument("--repo", type=Path, default=Path.cwd())
    naming = commands.add_parser(
        "branch",
        help="Show or set the prefix new lane branches are created under.",
    )
    namings = naming.add_subparsers(dest="action", required=True)
    naming_show = namings.add_parser("show")
    naming_show.add_argument("--repo", type=Path, default=Path.cwd())
    naming_show.add_argument("--json", action="store_true", help=JSON_HELP)
    naming_set = namings.add_parser("set")
    naming_set.add_argument(
        "prefix",
        metavar="PREFIX",
        help=(
            "Prefix for new lane branches, such as parley. Existing lanes "
            "keep the branch they were created with."
        ),
    )
    naming_set.add_argument("--repo", type=Path, default=Path.cwd())
    tracker = commands.add_parser(
        "forge",
        help="Show or set the issue tracker this project coordinates over.",
    )
    trackers = tracker.add_subparsers(dest="action", required=True)
    tracker_show = trackers.add_parser("show")
    tracker_show.add_argument("--repo", type=Path, default=Path.cwd())
    tracker_show.add_argument("--json", action="store_true", help=JSON_HELP)
    tracker_set = trackers.add_parser("set")
    tracker_set.add_argument(
        "name",
        metavar="NAME",
        choices=forge.FORGES,
        help=(
            "github speaks through gh, beads through bd when the repository "
            "carries a .beads/ ledger, and null keeps issue numbers bare."
        ),
    )
    tracker_set.add_argument("--repo", type=Path, default=Path.cwd())
    budgets = commands.add_parser(
        "deadlines",
        help="Show or set this project's deadline and attempt defaults.",
    )
    budget_actions = budgets.add_subparsers(dest="action", required=True)
    budget_show = budget_actions.add_parser("show")
    budget_show.add_argument("--repo", type=Path, default=Path.cwd())
    budget_show.add_argument("--json", action="store_true", help=JSON_HELP)
    budget_set = budget_actions.add_parser("set")
    budget_set.add_argument("--repo", type=Path, default=Path.cwd())
    for field, described in (
        ("claim", "a claim"),
        ("offer", "a handoff offer"),
        ("ack", "an acknowledgement"),
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
    ceiling_show = ceiling_actions.add_parser("show")
    ceiling_show.add_argument("--repo", type=Path, default=Path.cwd())
    ceiling_show.add_argument("--json", action="store_true", help=JSON_HELP)
    ceiling_set = ceiling_actions.add_parser("set")
    ceiling_set.add_argument("--repo", type=Path, default=Path.cwd())
    add_budget_flags(ceiling_set)
    shared = commands.add_parser(
        "resources",
        help="Show or declare the named resources lanes may reserve.",
    )
    declarations = shared.add_subparsers(dest="action", required=True)
    declared_show = declarations.add_parser("show")
    declared_show.add_argument("--repo", type=Path, default=Path.cwd())
    declared_show.add_argument("--json", action="store_true", help=JSON_HELP)
    declared_set = declarations.add_parser("set")
    declared_set.add_argument(
        "names",
        metavar="NAMES",
        help=(
            "Space-separated resource names such as 'port:5432 db:local'; "
            "pass an empty string to accept any well-formed name again."
        ),
    )
    declared_set.add_argument("--repo", type=Path, default=Path.cwd())
    provider = commands.add_parser(
        "provider", help="Inspect or define providers that drive a native CLI."
    )
    definitions = provider.add_subparsers(dest="action", required=True)
    definitions.add_parser("list").add_argument(
        "--json", action="store_true", help=JSON_HELP
    )
    definitions.add_parser("remove").add_argument("name")
    provider_show = definitions.add_parser(
        "show", help="Print one provider definition and its hook support."
    )
    provider_show.add_argument("name")
    provider_show.add_argument("--json", action="store_true", help=JSON_HELP)
    defining = definitions.add_parser("add")
    defining.add_argument("name")
    defining.add_argument("--adapter", choices=roster.ADAPTERS, required=True)
    defining.add_argument("--executable", required=True)
    defining.add_argument("--home-env", default="")
    defining.add_argument("--env", action="append", default=[])
    defining.add_argument("--require-env", action="append", default=[])
    capping = definitions.add_parser(
        "budget",
        help=(
            "Show or set the advisory limits every lane on this provider "
            "inherits unless its own budget says otherwise."
        ),
    )
    capping.add_argument("name")
    add_budget_flags(capping)
    accounts = commands.add_parser(
        "credentials", help="Inspect or define per-account profiles."
    )
    profiles = accounts.add_subparsers(dest="action", required=True)
    profiles.add_parser("list").add_argument(
        "--json", action="store_true", help=JSON_HELP
    )
    profiles.add_parser("remove").add_argument("name")
    credential_show = profiles.add_parser(
        "show",
        help=("Print one account profile with every recorded value redacted."),
    )
    credential_show.add_argument("name")
    credential_show.add_argument("--json", action="store_true", help=JSON_HELP)
    profile = profiles.add_parser("add")
    profile.add_argument("name")
    profile.add_argument("--config-home", default="")
    profile.add_argument("--env", action="append", default=[])
    profile.add_argument("--require-env", action="append", default=[])
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
            dest="command", metavar="COMMAND", help=argparse.SUPPRESS
        ),
        typed,
    )
    declare(parser, commands)
    return parser, commands


def _plain_status() -> int:
    """Runs the unfiltered status command without building its parser."""
    home = Path(
        os.environ.get("AGENT_PARLEY_HOME", "~/.local/state/agent-parley")
    )
    try:
        Bridge(home).status(Selection(), terminal_width())
    except (BridgeError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"agent-parley: {exc}", file=sys.stderr)
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
        parser.print_help()
        return 0
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
    try:
        bridge = Bridge(args.home)
        if args.command == "up":
            bridge.up()
            print(
                views.render(
                    "up",
                    {
                        "url": f"{bridge.url}/mcp/",
                        "state_directory": str(bridge.home),
                        "ready": bridge.ready(),
                    },
                )
                if args.json
                else f"Coordination server ready at {bridge.url}/mcp/"
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
                name for name in names if name not in dict(dashboard.COLUMNS)
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
            if args.json:
                print(
                    views.render(
                        "top",
                        views.frame(
                            dashboard.select(
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
                        ),
                    )
                )
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
                parser.error("history takes issue, participant or claim.")
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
                        reported["records"], reported["holdings"]
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
        elif args.command == "setup":
            registered = bridge.setup(args.repo.resolve())
            print(
                views.render("setup", registered)
                if args.json
                else json.dumps(registered, indent=2)
            )
        elif args.command == "run":
            status = bridge.launch(
                args.participant,
                args.repo.resolve(),
                args.task,
                args.provider,
                args.credentials,
                resume=args.resume,
            )
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
                parser.error("report needs --state and --summary.")
            bridge.report(
                args.repo.resolve(),
                args.state,
                args.summary,
                args.remaining,
                args.evidence,
                key=args.idempotency_key,
                issue=args.issue,
                resume_on=args.resume_on,
                backlog=args.backlog,
            )
            print(f"Recorded outcome: {args.state}")
        elif args.command == "say" or (
            args.command == "mail" and args.action == "send"
        ):
            return spoken(bridge, parser, args)
        elif args.command == "issue" and args.action == "show":
            detail = bridge.issue_reading(args.repo.resolve(), args.number)
            print(
                views.render(
                    "issue",
                    views.issue_detail(
                        detail["ledger"],
                        int(detail["issue"]),
                        detail["reservations"],
                        detail["history"],
                    ),
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
        elif args.command == "issue" and args.action == "resolve":
            ended = bridge.issue_resolve(
                args.repo.resolve(),
                args.number,
                reason=args.reason,
                release=args.release,
            )
            print(json.dumps(ended, indent=2))
        elif args.command == "issue" and args.action == "recover":
            approved = bridge.authorize_recovery(
                args.repo.resolve(), args.number, args.reason
            )
            print(json.dumps(approved, indent=2))
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
                views.render("problems", problems.rendered(found))
                if args.json
                else "\n".join(problems.lines(found))
            )
            return 1 if found else 0
        elif args.command in ("gc", "reclaim"):
            if args.force and not args.apply:
                parser.error("--force needs --apply.")
            swept = bridge.reclaim(args.repo.resolve(), apply=args.apply)
            made = bridge.reclaim_worktrees(
                args.repo.resolve(),
                apply=args.apply,
                sizes=not args.apply,
                force=args.force,
            )
            print(
                views.render("gc", {"lanes": swept, "worktrees": made})
                if args.json
                else "\n".join(reclaim.lines(swept + made))
                or "No lane to reclaim."
            )
        elif args.command == "notify":
            probed = notify.probe(args.repo.resolve().name)
            print(
                views.render("notify", probed)
                if getattr(args, "json", False)
                else notification_report(probed)
            )
            return 0 if all(item["ok"] for item in probed["results"]) else 1
        elif args.command == "plan":
            applied = bridge.work_plan(
                args.repo.resolve(), args.action, getattr(args, "path", None)
            )
            if args.action == "apply":
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
                else json.dumps(decisions, indent=2)
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
                bridge.add_participant(
                    repository, args.name, args.provider, args.credentials
                )
            elif args.action == "restore":
                print(bridge.restore(repository, args.name))
            elif args.action == "retire":
                print(bridge.retire(repository, args.name))
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
                        },
                    )
                )
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
                            "ack": args.ack,
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
            else:
                print(
                    bridge.budget(
                        repository,
                        "project",
                        "",
                        budget_changes(args) if args.action == "set" else None,
                    )
                )
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
            if getattr(args, "json", False):
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
                else json.dumps({args.name: inspected}, indent=2)
            )
        elif args.command == "credentials" and args.action == "show":
            profile_shown = views.credential_detail(
                args.name, roster.credential(bridge.home, args.name)
            )
            print(
                views.render("credentials_show", profile_shown)
                if args.json
                else json.dumps(profile_shown, indent=2)
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
                else json.dumps(defined, indent=2)
            )
        elif args.command == "credentials":
            if args.action == "add":
                roster.define_credential(
                    bridge.home,
                    args.name,
                    args.config_home,
                    args.env,
                    args.require_env,
                )
            elif args.action == "remove":
                roster.remove(bridge.home, "credentials", args.name)
            registered = roster.credentials(bridge.home)
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
                else json.dumps(registered, indent=2)
            )
        else:
            selection = selected_status(args)
            if args.json:
                narrowed = narrow(bridge.status_snapshot(), selection)
                print(views.render("status", narrowed))
                matched = reported_lanes(narrowed)
            else:
                matched = bridge.status(selection, terminal_width())
            if matched and (
                selection.drifted or selection.pending or selection.over_budget
            ):
                return 1
        return 0
    except (
        BridgeError,
        OSError,
        ValueError,
        subprocess.TimeoutExpired,
    ) as exc:
        print(f"agent-parley: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
