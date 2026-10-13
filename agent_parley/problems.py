"""Derives every condition an operator should act on from one status reading.

The view reads. It reuses the status reading, the supervision thresholds and
the store classification; it records nothing, wakes nobody and moves no
ownership. Each row names one lane and one cause, how many items share that
cause, how long the oldest of them has held, and either a command the
operator can paste or the actor already handling it, so an operator returning
to twenty lanes reads what needs them now instead of comparing timestamps
across `status`, `top`, `issue list` and `doctor`.

A remedy is derived from the lane's recorded state, never from the condition
alone. A lane whose own client holds an unanswered prompt, whose session
process is gone, or whose calls are paused cannot read mail, so it is never
offered `say`. A lane the coordination service is still waking carries what
that loop has already attempted rather than a command that would duplicate
it, and an active lane is never told to complete or stop a session it is
working in. A worktree holding uncommitted work names the worktree and the
files it holds, because retiring the lane would drop its claims to clean one
directory.
"""

import contextlib
import datetime
import json
import shlex
import sqlite3
import time
from pathlib import Path

from agent_parley import (
    approvals,
    budgets,
    dialogs,
    forge,
    issues,
    lanes,
    merges,
    plan,
    reclaim,
    records,
    recovery,
    roster,
    store,
    supervision,
    tables,
)
from agent_parley.state import BridgeError
from agent_parley.status import (
    FORGE_ISSUES,
    FORGE_RETRY,
    FORGE_STALE,
    fold_line,
    folded,
)

STORE = "store"
SERVICE = "service"
SUPERVISING = "supervision failing"
STALLED = "stalled"
INACTIVE = "inactive"
OVERDUE = "overdue claim"
OVER_CAP = "claims over cap"
OFFER = "unanswered offer"
REQUEST = "unanswered request"
UNRESOLVED = "unresolved completion"
DIVERGING = "not converging"
ACK = "awaiting acknowledgement"
BOUNCE = "bounced share"
RETIRED = "shares to a retired lane"
RETIRED_WINDOW = 86400
STALE_AFTER = 86400
STALE_HEADING = "Older than a day:"
DRIFT = "branch drift"
DIRTY = "dirty worktree"
BUDGET = "over budget"
WAKE = "wake attention"
APPROVAL = "waiting on approval"
HELD = "held by a native dialog"
READY = "ready to retire"
ORPHANED = "orphaned claims"
INTEGRATE = "ready to integrate"
UNOPENED = "ready without a pull request"
HOLDING = "holding a refused key"
FOREIGN = "second session"
REFUSED = "recovery refused"
INTEGRATION = "integration unverified"
ROOT = "root missing"
ROOT_RETURNED = "root returned"
ESCALATED = "escalated plan revision"
PROPOSED = "plan revisions pending"
RUN_BUDGET = "run budget exhausted"
RUN_UNMETERED = "run budget unmetered"
CHECKS = "checks stalled"
CROSSING = "crossing ready"
CHECKS_FAILED = "checks failed"
CHECKS_REFUSED = "checks refused"
CI_ROUNDS_EXHAUSTED = "ci rounds exhausted"
CHILD_SESSION = "session outliving its claim"
CHILD_RECENT = 60
FORGE_REFUSED = "forge refused"

BY_OPERATOR = "operator"
BY_SERVICE = "service"

DIALOG = "busy:input"
RETRY = "busy:repeat"
BUSY_APPROVAL = "busy:approval"
ATTENTION = "manual attention required"
PAUSED = "paused"

WAKE_DETAILS = {
    DIALOG: "wake refused because operator input is pending",
    RETRY: (
        "wake refused because the previous accepted wake produced no checkpoint"
    ),
    BUSY_APPROVAL: (
        "wake refused because the client is waiting for a native approval"
    ),
    ATTENTION: "wake requires operator attention",
    supervision.SESSION_HELD: (
        "resume refused because a running launcher holds the session lock "
        "and its wake socket did not answer"
    ),
    supervision.WAKE_UNAVAILABLE: (
        "wake not delivered because "
        + supervision.UNDELIVERED_WAKES[supervision.WAKE_UNAVAILABLE]
    ),
    supervision.OPT_IN_MISSING: (
        "setup gap: resume withheld because no bridge tool approval is "
        "recorded, so the resumed session would stop at a prompt nobody sees"
    ),
}


def _row(
    condition: str,
    detail: str,
    command: str,
    seconds: int | None = None,
    participant: str = "",
    project: str = "",
    actor: str = BY_OPERATOR,
    count: int = 1,
) -> dict:
    """Shapes one problem row with every field a report prints.

    Args:
        condition: The cause this row reports.
        detail: What was observed, including the count when items are grouped.
        command: A command the actor can run, or a sentence naming what the
            actor does when no command expresses it.
        seconds: Age of the oldest item behind the row, or None when the span
            is unknown.
        participant: Lane the row belongs to, empty for estate conditions.
        project: Canonical project key.
        actor: Who acts, `BY_OPERATOR` or `BY_SERVICE`.
        count: How many items of this cause the row stands for.

    Returns:
        One row with every field the text and JSON reports print.
    """
    return {
        "project": project,
        "participant": participant,
        "condition": condition,
        "detail": detail,
        "seconds": seconds,
        "count": count,
        "actor": actor,
        "command": command,
    }


def _blocked(record: dict) -> str:
    """Names what stops the lane reading its mail, or an empty string.

    Args:
        record: One participant record from the status reading.

    Returns:
        The session state or recorded wake refusal that keeps delivered mail
        unread, and an empty string when nothing recorded keeps it unread. A
        message sent to a blocked lane is stored and never reaches a turn, so
        no row offers `say` while one of these holds. A recorded dialog
        counts only while the session process that showed it may be alive.
    """
    result = str((record.get("wake") or {}).get("result", ""))
    if record["availability"]["state"] == supervision.STOPPED:
        return supervision.STOPPED
    if result in (DIALOG, ATTENTION):
        return result
    held = _dialog(record)
    if held.get("name") == dialogs.PERMISSION or held.get("escalated"):
        return DIALOG
    if record.get("paused"):
        return PAUSED
    return ""


def _dialog(record: dict) -> dict:
    """Reads the native dialog a lane's session still shows, if any.

    A dialog belongs to the session that showed it. The record is not
    cleared when that session ends, so a dialog read while the session
    process is known to be gone describes a prompt no terminal shows.

    Args:
        record: One participant record from the status reading.

    Returns:
        The recorded dialog, or an empty mapping when none is recorded or
        the lane's session process is known to be gone.
    """
    if record["availability"].get("process_alive") is False:
        return {}
    return record.get("dialog") or {}


def _inferred(record: dict) -> str:
    """Words how a lane's recorded state was inferred, for a row's detail.

    Args:
        record: One participant record from the status reading.

    Returns:
        A clause naming the watcher or liveness inference and the hook the
        lane's client lacks, starting with a separator, or an empty string
        when a hook confirmed the state.
    """
    note = lanes.inference(record.get("provenance"))
    return f" ({note})" if note else ""


def _answer(name: str, repo: str, record: dict, text: str) -> str:
    """Says where the operator answers a prompt the lane's client holds.

    A lane the service resumed has no terminal: its launcher reads nothing
    and writes to the lane's wake log, so no operator can answer the prompt
    where it is. That lane is ended and started again in the operator's own
    terminal, where the same prompt can be answered.

    Args:
        name: Participant that owns the lane.
        repo: Rendered `--repo` argument naming the project.
        record: One participant record from the status reading.
        text: Remedy for a lane whose launcher has a terminal.

    Returns:
        The given remedy, or the stop and restart commands naming the wake
        log when the lane's launcher has no terminal.
    """
    log = record.get("wake_log") or ""
    if not log:
        return text
    return (
        f"{name} was resumed without a terminal, so no one can answer its "
        f"prompt; its output is in {log}. Run `agent-parley participant "
        f"stop {name} {repo}`, then `agent-parley participant restart "
        f"{name} {repo}` in your terminal"
    )


def _attempted(name: str, record: dict) -> str:
    """States what the service's wake loop has already done for the lane.

    Args:
        name: Participant that owns the lane.
        record: One participant record from the status reading.

    Returns:
        One sentence naming the attempts the service has recorded and when it
        tries again. An active lane is reported as waited for rather than
        woken, because the loop asks for a turn only once a lane has been
        quiet past the inactive threshold.
    """
    if record["availability"]["state"] == supervision.ACTIVE:
        return (
            f"the coordination service wakes {name} once it goes idle; the "
            "lane is active and needs no operator now"
        )
    attempts = int((record.get("wake") or {}).get("attempts") or 0)
    if not attempts:
        return (
            f"the coordination service wakes {name} on its next poll; no "
            "operator action yet"
        )
    plural = "" if attempts == 1 else "s"
    return (
        f"the coordination service has woken {name} {attempts} time{plural} "
        "and wakes it again on its next poll; no operator action yet"
    )


def _remedy(
    name: str, repo: str, record: dict, waking: bool
) -> tuple[str, str]:
    """Names who can give the lane its next turn and how.

    A lane between turns and a lane whose launcher exited need opposite
    commands. Resuming a lane whose launcher is still running collides with
    the session lock that launcher holds, so only a stopped lane is resumed,
    a lane whose last wake found that lock held is sent to its own client
    even when its record reads stopped, a lane whose resume was withheld for
    a missing bridge tool approval is sent to that opt-in or to the
    operator's own terminal, and a lane that cannot read mail is never
    handed a delivery command.

    Args:
        name: Participant that owns the lane.
        repo: Rendered `--repo` argument naming the project.
        record: One participant record from the status reading.
        waking: Whether the wake loop is enabled for this lane.

    Returns:
        The remedy text and the actor it belongs to. The service owns the row
        while its wake loop is enabled and still has attempts left for this
        backlog; otherwise the operator owns it, with the command that fits
        the lane's recorded state.
    """
    blocked = _blocked(record)
    wake = record.get("wake") or {}
    if wake.get("result") == supervision.SESSION_HELD:
        return (
            f"take the turn waiting in {name}'s own client; its launcher "
            "still holds the session lock, so a resume would be refused",
            BY_OPERATOR,
        )
    if wake.get("result") == supervision.WAKE_UNAVAILABLE:
        return (
            f"take the turn waiting in {name}'s own client, or end it and "
            f"run agent-parley run {name} --resume {repo} so wakes reach it",
            BY_OPERATOR,
        )
    if wake.get("result") == supervision.OPT_IN_MISSING:
        return (
            f"{supervision.OPT_IN_COMMAND} --participant {name} {repo}, or "
            f"agent-parley run {name} --resume {repo} in your own terminal",
            BY_OPERATOR,
        )
    if blocked == supervision.STOPPED:
        return f"agent-parley run {name} --resume {repo}", BY_OPERATOR
    held = _dialog(record)
    if (
        blocked == DIALOG
        and wake.get("result") == DIALOG
        and held.get("name") != dialogs.PERMISSION
        and not held.get("escalated")
    ):
        return (
            _answer(
                name,
                repo,
                record,
                f"submit or clear the unsent text in {name}'s own terminal "
                "(Enter, or Ctrl-U to clear the line); it reads no mail "
                "until that line is empty",
            ),
            BY_OPERATOR,
        )
    if blocked == DIALOG:
        return (
            _answer(
                name,
                repo,
                record,
                f"answer the prompt open in {name}'s own client; it reads no "
                "mail until that prompt is cleared",
            ),
            BY_OPERATOR,
        )
    if blocked == ATTENTION:
        return (
            f"take the turn waiting in {name}'s own client; the service "
            "stopped asking after its refusals",
            BY_OPERATOR,
        )
    if blocked == PAUSED:
        return f"agent-parley participant resume {name} {repo}", BY_OPERATOR
    attempts = int((record.get("wake") or {}).get("attempts") or 0)
    if waking and attempts < supervision.WORK_WAKE_ATTEMPTS:
        return _attempted(name, record), BY_SERVICE
    return f'agent-parley say {name} "<text>" {repo}', BY_OPERATOR


def _recorded(value: str | None, now: float) -> float:
    """Reads an instant the status reading carries as Unix seconds.

    Args:
        value: RFC 3339 instant from the reading, or None when none was
            recorded.
        now: Fallback used when nothing readable was recorded.

    Returns:
        The instant in Unix seconds, and the fallback when the reading carries
        no readable instant, so an unrecorded time reads as new rather than as
        an age measured from the Unix epoch.
    """
    try:
        return datetime.datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return now


def _age(value: object, now: float) -> int:
    """Measures whole seconds since an instant recorded as Unix seconds.

    Args:
        value: Unix seconds a state file recorded, or anything unreadable.
        now: Unix time the age is measured against.

    Returns:
        The non-negative age, or zero when no readable instant is recorded.
    """
    if not isinstance(value, int | float) or isinstance(value, bool):
        return 0
    return max(0, int(now - value))


def _listed(paths: list[str]) -> str:
    """Names the first few changed paths and counts the rest."""
    shown = ", ".join(paths[:3])
    return shown if len(paths) <= 3 else f"{shown} and {len(paths) - 3} more"


def _claim_rows(record: dict, name: str, repo: str, root: str) -> list[dict]:
    """Groups one lane's overdue claims into a single row.

    Args:
        record: One participant record from the status reading.
        name: Participant that owns the lane.
        repo: Rendered `--repo` argument naming the project.
        root: Canonical project key.

    Returns:
        One row naming the oldest overdue claim and counting the rest, or no
        row when the lane holds none. Ownership never moves on a deadline, so
        the release stays the operator's.
    """
    overdue = sorted(
        (claim for claim in record["claims"] if claim["overdue"]),
        key=lambda claim: -claim["overdue_seconds"],
    )
    if not overdue:
        return []
    oldest = overdue[0]
    numbers = ", ".join(f"#{claim['issue']}" for claim in overdue)
    detail = (
        f"issue #{oldest['issue']} is past its deadline"
        if len(overdue) == 1
        else f"{len(overdue)} claims are past their deadline: {numbers}"
    )
    return [
        _row(
            OVERDUE,
            detail,
            f"agent-parley issue release {oldest['issue']} {repo}",
            oldest["overdue_seconds"],
            name,
            root,
            BY_OPERATOR,
            len(overdue),
        )
    ]


def _cap_rows(
    record: dict, name: str, repo: str, root: str, cap: int
) -> list[dict]:
    """Reports a lane holding more claims than the project's claim cap.

    Every path that moves ownership to a lane refuses it past the cap, but a
    ledger written before a path was capped, or a cap lowered after claims
    were taken, can still hold more. Nothing moves them automatically, so the
    excess is named for the operator to release or offer. A delivered claim
    does not count, as it does not where the cap is enforced.

    Args:
        record: One participant record from the status reading.
        name: Participant that owns the lane.
        repo: Rendered `--repo` argument naming the project.
        root: Canonical project key.
        cap: The project's `max_claims_per_lane`.

    Returns:
        One row counting the claims past the cap and naming the newest-numbered
        of them as the one to release, or no row within the cap.
    """
    held = [
        claim["issue"]
        for claim in record["claims"]
        if not claim.get("delivered")
    ]
    if len(held) <= cap:
        return []
    excess = len(held) - cap
    return [
        _row(
            OVER_CAP,
            f"holds {len(held)} claims, {excess} past max_claims_per_lane "
            f"{cap}",
            f"agent-parley issue release {max(held)} {repo}",
            None,
            name,
            root,
            BY_OPERATOR,
            excess,
        )
    ]


def _awaiting_integration(record: dict) -> tuple[dict, int] | None:
    """Finds a ready report whose open pull request waits for integration.

    Args:
        record: One participant record from the status reading.

    Returns:
        The claim carrying the open pull request, preferring the reported
        issue, beside the seconds the ready report has waited, or None when
        the lane's latest report is not a ready one still unintegrated or
        none of its claims has an open pull request in the cached reading.
        The cache holds only open pull requests, so a merged or closed one
        ends the wait here.
    """
    wait = next(
        (
            item
            for item in record.get("waiting") or []
            if item.get("kind") == "report_integration"
            and not item.get("complete")
        ),
        None,
    )
    if wait is None:
        return None
    opened = sorted(
        (claim for claim in record["claims"] if claim.get("pull_request")),
        key=lambda claim: claim["issue"] != record.get("report_issue"),
    )
    if not opened:
        return None
    return opened[0], int(wait.get("seconds") or 0)


def _integrate_rows(
    record: dict, name: str, repo: str, root: str
) -> list[dict]:
    """Reports a ready report whose open pull request waits for integration.

    A lane that reported ready and opened a pull request may stop before
    anyone integrates it. Nothing merges on its own here; the row names the
    pull request and the integration step its approval policy allows,
    once the wait passes the same ceiling a declared wait uses.

    Args:
        record: One participant record from the status reading.
        name: Participant that owns the lane.
        repo: Rendered `--repo` argument naming the project.
        root: Canonical project key.

    Returns:
        One row naming the issue, the pull request and the command, or no
        row while no such report waits or it is younger than
        `supervision.DEFAULT_WAIT_CEILING`.
    """
    found = _awaiting_integration(record)
    if found is None:
        return []
    claim, seconds = found
    if seconds <= supervision.DEFAULT_WAIT_CEILING:
        return []
    pull = claim["pull_request"]
    shown = f"pull request #{pull['number']}"
    if pull.get("url"):
        shown += f" ({pull['url']})"
    approval = record.get("approval")
    if approval and approval.get("state") != approvals.APPROVED:
        command = f"agent-parley approve {name} {repo}"
    else:
        command = (
            f"review and merge {shown}, or agent-parley participant merge "
            f"{name} {repo}"
        )
    return [
        _row(
            INTEGRATE,
            f"issue #{claim['issue']} reported ready with open {shown} "
            "awaiting integration",
            command,
            seconds,
            name,
            root,
        )
    ]


def _unopened_rows(
    record: dict, name: str, repo: str, root: str, directory: Path | None
) -> list[dict]:
    """Reports ready claims that no open pull request carries.

    A lane is told its ready claims wait on CI and review, but a claim
    whose pull requests all merged or closed while the issue stayed open
    waits on nothing, so it would stay claimed for good. The cached forge
    reading holds only open pull requests, so a missing one is the signal.
    A project with no forge reading proves nothing, and a pending approval
    is a real wait, so neither raises a row.

    Args:
        record: One participant record from the status reading.
        name: Participant that owns the lane.
        repo: Rendered `--repo` argument naming the project.
        root: Canonical project key.
        directory: Private project state directory holding the cached
            pull request reading, or None when there is none to consult.

    Returns:
        One row naming the longest-ready such claim and counting the rest,
        or no row while each ready claim has an open pull request, the
        project has no forge reading, an approval is pending, or none has
        been ready longer than `supervision.DEFAULT_WAIT_CEILING`.
    """
    if (
        directory is None
        or not (directory / supervision.PULL_REQUEST_RECORD).is_file()
    ):
        return []
    approval = record.get("approval")
    if approval and approval.get("state") != approvals.APPROVED:
        return []
    stale = sorted(
        (
            claim
            for claim in record["claims"]
            if not claim.get("pull_request")
            and (claim.get("ready_seconds") or 0)
            > supervision.DEFAULT_WAIT_CEILING
        ),
        key=lambda claim: -claim["ready_seconds"],
    )
    if not stale:
        return []
    oldest = stale[0]
    numbers = ", ".join(f"#{claim['issue']}" for claim in stale)
    detail = (
        f"issue #{oldest['issue']} is ready with no open pull request"
        if len(stale) == 1
        else f"{len(stale)} ready claims have no open pull request: {numbers}"
    )
    return [
        _row(
            UNOPENED,
            detail,
            f"open its pull request, close #{oldest['issue']} if the work "
            "merged, or agent-parley issue release "
            f"{oldest['issue']} {repo}",
            oldest["ready_seconds"],
            name,
            root,
            BY_OPERATOR,
            len(stale),
        )
    ]


def _retire_rows(
    record: dict, name: str, repo: str, root: str, ceiling: float
) -> list[dict]:
    """Reports once that a lane holding only orphaned claims may retire.

    A lane whose session died keeps its claims, and the supervisor marks
    them orphaned so a peer can take them. A marker that has stood past the
    ceiling means nobody took them and the lane did not return, so the
    lane is ready to retire. A lane whose session process is alive is never
    offered retirement, whatever its markers say, because `participant
    retire` refuses a lane with a running session. The row only names the
    command: the sweep never retires a lane on its own. Supervision returns
    the work of a lane it proved dead, but leaves the lane itself for the
    operator to retire or resume.

    Args:
        record: One participant record from the status reading.
        name: Participant that owns the lane.
        repo: Rendered `--repo` argument naming the project.
        root: Canonical project key.
        ceiling: Seconds an orphan marker stands before the row appears.

    Returns:
        One row naming the orphaned claims and the age of the oldest
        marker, or no row while the lane holds any claim not orphaned,
        every marker is younger than the ceiling, or a ready report with an
        open pull request waits for integration, since retiring would drop
        the claim that pull request closes.
    """
    claims = record["claims"]
    if record["availability"].get("process_alive") is True:
        return []
    if _awaiting_integration(record):
        return []
    if not claims or not all(claim.get("orphaned") for claim in claims):
        return []
    oldest = max(
        int(claim.get("orphan_recorded_seconds") or 0) for claim in claims
    )
    if oldest <= ceiling:
        return []
    numbers = ", ".join(f"#{claim['issue']}" for claim in claims)
    return [
        _row(
            READY,
            f"orphaned claims {numbers} stood unclaimed past the ceiling",
            f"agent-parley participant retire {name} {repo}",
            oldest,
            name,
            root,
            BY_OPERATOR,
            len(claims),
        )
    ]


def _orphan_rows(record: dict, name: str, root: str, peer: bool) -> list[dict]:
    """Reports a lane's orphaned claims with who can move them.

    The supervisor's orphan decision is also sent through notification
    transports, which may be off. This row is derived from the ledger
    alone, so the remedy reaches the operator either way. With no live
    lane besides the holder, it names the operator's command rather than
    a peer that does not exist.

    Args:
        record: One participant record from the status reading.
        name: Participant that owns the lane.
        root: Canonical project key.
        peer: Whether a live lane besides this one exists to take them.

    Returns:
        One row naming the orphaned claims and the age of the oldest marker,
        or no row while the lane's session process is alive or it holds no
        orphaned claim.
    """
    if record["availability"].get("process_alive") is True:
        return []
    orphaned = [claim for claim in record["claims"] if claim.get("orphaned")]
    if not orphaned:
        return []
    numbers = ", ".join(f"#{claim['issue']}" for claim in orphaned)
    issue = str(orphaned[0]["issue"]) if len(orphaned) == 1 else "NUMBER"
    reason = orphaned[0].get("orphan_reason") or "no reason recorded"
    return [
        _row(
            ORPHANED,
            f"claims {numbers} orphaned ({reason})",
            supervision.orphan_remedy(issue, root, peer),
            max(
                int(claim.get("orphan_recorded_seconds") or 0)
                for claim in orphaned
            ),
            name,
            root,
            BY_OPERATOR,
            len(orphaned),
        )
    ]


def _unresolved_rows(
    record: dict, name: str, repo: str, root: str, now: float
) -> list[dict]:
    """Groups one lane's unresolved completions into a single row.

    Args:
        record: One participant record from the status reading.
        name: Participant that owns the lane.
        repo: Rendered `--repo` argument naming the project.
        root: Canonical project key.
        now: Unix time the observation ages are measured against.

    Returns:
        One row naming the oldest observed-complete claim its holder never
        released and counting the rest, or no row when the lane holds none.
        Ownership never moves on an observation, so the resolution stays the
        operator's.
    """
    unresolved = sorted(
        (claim for claim in record["claims"] if claim.get("unresolved")),
        key=lambda claim: float(claim.get("observed_at") or now),
    )
    if not unresolved:
        return []
    oldest = unresolved[0]
    numbers = ", ".join(f"#{claim['issue']}" for claim in unresolved)
    detail = (
        f"issue #{oldest['issue']}: {oldest.get('reason', '')}"
        if len(unresolved) == 1
        else f"{len(unresolved)} claims are complete but never released: "
        f"{numbers}"
    )
    observed = float(oldest.get("observed_at") or now)
    return [
        _row(
            UNRESOLVED,
            detail,
            f"agent-parley issue resolve {oldest['issue']} {repo}",
            max(0, int(now - observed)),
            name,
            root,
            BY_OPERATOR,
            len(unresolved),
        )
    ]


def _child_rows(
    home: Path,
    manifest: dict,
    children: list[Path],
    record: dict,
    name: str,
    repo: str,
    root: str,
    now: float,
) -> list[dict]:
    """Reports a session still active in a worktree beyond the lane's claim.

    A lane can run a project's own tooling from its shell, and that tooling
    can start further native sessions in a worktree the lane made for a
    pull request or a sub-task. Agent Parley never sees those sessions
    start and never stops them; this only tells the lane, once its claim
    has ended, that one is still writing to its session record. The
    worktrees come from one `reclaim.children_by_lane` reading per project,
    so a project with hundreds of registrations is listed once, not once
    per lane.

    Args:
        home: Private bridge state root.
        manifest: Project manifest holding this participant.
        children: Worktrees Git registers and attributes to this lane.
        record: One participant record from the status reading.
        name: Participant that owns the lane.
        repo: Rendered `--repo` argument naming the project.
        root: Canonical project key.
        now: Unix time the observation ages are measured against.

    Returns:
        One row naming how many session records are still active and how
        long since the most recent, or no row while the lane holds a claim,
        no worktree beyond its own is attributed to it, or none of them
        show activity within `CHILD_RECENT` seconds.
    """
    if record["claims"] or not children:
        return []
    participant = manifest["participants"][name]
    count, latest = records.child_activity(home, participant, children)
    if not count or latest is None or now - latest > CHILD_RECENT:
        return []
    return [
        _row(
            CHILD_SESSION,
            f"{count} native session(s) in a worktree this lane made are "
            "still active after its claim ended",
            f"agent-parley status {name} {repo}; agent-parley never "
            "stops a session",
            max(0, int(now - latest)),
            name,
            root,
            BY_OPERATOR,
            count,
        )
    ]


def _diverging_rows(
    record: dict, name: str, repo: str, root: str
) -> list[dict]:
    """Reports each claim whose convergence account escalated.

    Args:
        record: One participant record from the status reading.
        name: Participant that owns the lane.
        repo: Rendered `--repo` argument naming the project.
        root: Canonical project key.

    Returns:
        One row per claim whose repeated verified failures outlasted the
        request to change approach. The holder keeps the claim; any handoff
        or reassignment is the operator's or the holder's explicit act.
    """
    rows = []
    for claim in record["claims"]:
        shown = claim.get("convergence") or {}
        if shown.get("stage") != "escalated":
            continue
        rows.append(
            _row(
                DIVERGING,
                f"issue #{claim['issue']}: {shown['failures']} failing "
                f"verification results, signature {shown['signature']} "
                f"x{shown['repeats']}, no verified improvement for "
                f"{shown['since_milestone_seconds']}s",
                f"agent-parley issue show {claim['issue']} {repo}",
                int(shown["since_milestone_seconds"]),
                name,
                root,
            )
        )
    return rows


def _ack_rows(
    record: dict,
    name: str,
    repo: str,
    root: str,
    ack_after: float,
    waking: bool,
) -> list[dict]:
    """Groups the messages one lane has left unacknowledged into one row.

    Args:
        record: One participant record from the status reading.
        name: Participant that owns the lane.
        repo: Rendered `--repo` argument naming the project.
        root: Canonical project key.
        ack_after: Seconds after which an unacknowledged message counts.
        waking: Whether the wake loop is enabled for this lane.

    Returns:
        One row naming the oldest unacknowledged message and counting the
        rest, or no row when none is old enough. A parked lane owes one cause,
        not one cause per message it never read.
    """
    pending = sorted(
        (
            item
            for item in (record.get("mail") or {}).get("outstanding_ack", [])
            if item["age_seconds"] >= ack_after
        ),
        key=lambda item: -item["age_seconds"],
    )
    if not pending:
        return []
    oldest = pending[0]
    detail = (
        f"message {oldest['message_id']} from {oldest['sender']} awaits "
        "acknowledgement"
        if len(pending) == 1
        else f"{len(pending)} messages await acknowledgement, the oldest "
        f"{oldest['message_id']} from {oldest['sender']}"
    )
    command, actor = _remedy(name, repo, record, waking)
    return [
        _row(
            ACK,
            detail,
            command,
            oldest["age_seconds"],
            name,
            root,
            actor,
            len(pending),
        )
    ]


def _lane_rows(
    record: dict,
    participant: dict,
    root: str,
    config: dict,
    ack_after: float,
    now: float,
    directory: Path | None = None,
    peer: bool = True,
) -> list[dict]:
    """Derives the rows one lane record carries, one per cause.

    Args:
        record: One participant record from the status reading.
        participant: The lane's manifest entry, naming its worktree.
        root: Canonical project key.
        config: Resolved supervision settings for the project.
        ack_after: Seconds after which an unacknowledged message is a row.
        now: Unix time the observation ages are measured against.
        directory: Private project state directory holding the lane's
            key-hold deadline, or None to read no deadline.
        peer: Whether a live lane besides this one exists to take its
            orphaned claims.

    Returns:
        Zero or more rows, one per cause the record shows, each carrying how
        many items share that cause and the age of the oldest. A lane that has
        recorded no native activity carries no age on the rows that report one,
        because the span it has been quiet for is unknown rather than long.
        A lane owing several acknowledgements carries one row naming the
        oldest, so a broadcast costs one row per lane rather than one per
        message it created. A native approval prompt is reported once it has
        stood unanswered past the same bound an unacknowledged message uses,
        because a prompt the operator is about to answer needs no row. A
        native dialog the launcher escalated is reported at once by name,
        with the options it offers, because nothing will answer it but the
        operator. A prompt or dialog is reported only while the session
        process that showed it may be alive; once that process is gone no
        terminal shows the prompt, and the lane carries only the stopped
        remedy. A row about the lane's state says when the dialog watcher
        or the liveness sample inferred that state and names the hook the
        lane's client lacks, because no hook confirmed it. A quiet lane
        that refused a peer a key it still holds is reported with the lanes
        it refused and how long it has been quiet, because the refused lane
        saw the refusal and nobody else did. Once the release deadline the
        service gave that lane passes, the row belongs to the operator as a
        decision. A
        second client sending hooks under the lane's identity is named with
        its process while it lasts, because its events are ignored. A quiet
        lane is inactive only while it owes work, meaning a claim it has not
        delivered, or while something recorded keeps it from its next turn.
        A lane that delivered everything it holds is at rest, and waking it
        spends a turn on nothing. Uncommitted work is reported only once
        the session process is gone, because a running session owns its
        worktree and a quiet reading does not make its edits abandoned. A
        lane that retired reports only the
        worktree it kept,
        because its quiet is the state the operator asked for and every
        other remedy here would wake a lane that has given its work back.
    """
    name = record["participant"]
    repo = f"--repo {shlex.quote(str(root))}"
    rows: list[dict] = []
    if roster.retired(participant):
        if supervision.dirty_paths(participant["lane"]):
            rows.append(
                _row(
                    DIRTY,
                    "uncommitted work kept when this lane retired",
                    f"agent-parley participant add {name} {repo}",
                    record.get("retired_age_seconds"),
                    name,
                    root,
                )
            )
        return rows
    idle = record["idle"]
    availability = record["availability"]
    quiet = availability["state"] != supervision.ACTIVE
    waking = bool(config["wake"] and participant.get("wake", True))
    wake = record.get("wake") or {}
    if wake.get("result", "") in WAKE_DETAILS:
        command, actor = _remedy(name, repo, record, waking)
        detail = WAKE_DETAILS[wake["result"]]
        if wake["result"] in supervision.UNDELIVERED_WAKES and wake.get("at"):
            detail = (
                f"wake attempted at {wake['at']}, not delivered: "
                f"{supervision.UNDELIVERED_WAKES[wake['result']]}"
            )
        rows.append(
            _row(
                WAKE,
                detail,
                command,
                wake.get("age_seconds"),
                name,
                root,
                actor,
            )
        )
    held = _dialog(record)
    since = held.get("since")
    if held.get("name") == dialogs.PERMISSION and isinstance(
        since, (int, float)
    ):
        waited = max(0, int(now - float(since)))
        if waited >= ack_after:
            tool = str(held.get("tool", "")) or "a tool"
            rows.append(
                _row(
                    APPROVAL,
                    f"the client is waiting for approval of {tool}"
                    + _inferred(record),
                    _answer(
                        name,
                        repo,
                        record,
                        f"answer the prompt in {name}'s terminal",
                    ),
                    waited,
                    name,
                    root,
                )
            )
    elif held.get("escalated"):
        shown = str(held.get("label", "")) or "a native prompt"
        offered = [str(item) for item in held.get("options") or []]
        if offered:
            shown = f"{shown} ({'; '.join(offered)})"
        at = held.get("at")
        rows.append(
            _row(
                HELD,
                f"the client is held by {shown}" + _inferred(record),
                _answer(
                    name,
                    repo,
                    record,
                    f"answer the prompt in {name}'s terminal",
                ),
                (
                    max(0, int(now - float(at)))
                    if isinstance(at, (int, float))
                    else None
                ),
                name,
                root,
            )
        )
    if foreign := record.get("foreign_session"):
        rows.append(
            _row(
                FOREIGN,
                f"session {foreign['session_id'] or 'unnamed'} (pid "
                f"{foreign['pid']}) sends hooks as this lane and is ignored",
                f"stop that process, or run it outside {name}'s worktree",
                foreign.get("age_seconds"),
                name,
                root,
            )
        )
    if idle["stalled"]:
        command, actor = _remedy(name, repo, record, waking)
        rows.append(
            _row(
                STALLED,
                supervision.stall_marker(idle) + _inferred(record),
                command,
                int(idle["age_seconds"]),
                name,
                root,
                actor,
            )
        )
    elif (
        quiet
        and availability["process_alive"]
        and (
            _blocked(record)
            or any(not claim.get("delivered") for claim in record["claims"])
        )
    ):
        command, actor = _remedy(name, repo, record, waking)
        rows.append(
            _row(
                INACTIVE,
                "alive but no native activity past the inactive threshold"
                + _inferred(record),
                command,
                availability["age_seconds"],
                name,
                root,
                actor,
            )
        )
    rows.extend(_claim_rows(record, name, repo, root))
    rows.extend(_unopened_rows(record, name, repo, root, directory))
    rows.extend(
        _cap_rows(record, name, repo, root, config["max_claims_per_lane"])
    )
    if _awaiting_integration(record):
        rows.extend(_integrate_rows(record, name, repo, root))
    else:
        retire = _retire_rows(
            record, name, repo, root, config["orphan_retire_after"]
        )
        rows.extend(retire or _orphan_rows(record, name, root, peer))
    rows.extend(_unresolved_rows(record, name, repo, root, now))
    rows.extend(_diverging_rows(record, name, repo, root))
    rows.extend(_ack_rows(record, name, repo, root, ack_after, waking))
    refused = (record.get("mail") or {}).get("refused") or []
    if quiet and refused:
        command, actor = _remedy(name, repo, record, waking)
        detail = f"idle while holding a key refused to {_listed(refused)}"
        deadline = (
            supervision.key_hold(directory, name).get("deadline")
            if directory
            else None
        )
        if isinstance(deadline, (int, float)):
            late = int(now - deadline)
            if late >= 0:
                detail += f"; its release deadline passed {late}s ago"
                command, actor = (
                    f'decide: agent-parley say {name} "release the key '
                    f'refused to {", ".join(refused)}" {repo}, or let the '
                    "refused lanes wait",
                    BY_OPERATOR,
                )
            else:
                detail += f"; asked to release it within {-late}s"
        rows.append(
            _row(
                HOLDING,
                detail,
                command,
                availability["age_seconds"],
                name,
                root,
                actor,
                len(refused),
            )
        )
    if record["drift"]:
        rows.append(
            _row(
                DRIFT,
                f"on {record['branch']} instead of {record['assigned_branch']}",
                f"agent-parley participant restore {name} {repo}",
                availability["age_seconds"],
                name,
                root,
            )
        )
    elif (
        quiet
        and availability["process_alive"] is not True
        and (changed := supervision.dirty_paths(participant["lane"]))
    ):
        lane = participant["lane"]
        rows.append(
            _row(
                DIRTY,
                f"uncommitted work in {lane} and no recent activity: "
                f"{_listed(changed)}",
                f"commit or stash the work in {lane}; retiring {name} would "
                "drop the claims it holds to clean one directory",
                availability["age_seconds"],
                name,
                root,
                BY_OPERATOR,
                len(changed),
            )
        )
    if (record.get("budget") or {}).get("over"):
        rows.append(
            _row(
                BUDGET,
                record["budget"]["marker"],
                f"agent-parley participant budget {name} {repo}",
                None,
                name,
                root,
            )
        )
    return rows


def _offer_rows(project: dict, now: float) -> list[dict]:
    """Groups the handoff offers nobody has answered, one row per recipient.

    Args:
        project: One project block from the status reading.
        now: Unix time the offer ages are measured against.

    Returns:
        One row per lane holding unanswered offers, naming the oldest offer
        and counting the rest, so a lane ignoring five offers reads as one
        condition rather than five.
    """
    repo = f"--repo {shlex.quote(str(project['root']))}"
    waiting: dict[str, list[tuple[int, int, dict]]] = {}
    for record in project["issues"]:
        offer = record["offer"]
        if not offer:
            continue
        created = _recorded(offer.get("created_at"), now)
        waiting.setdefault(offer["to"], []).append(
            (max(0, int(now - created)), record["issue"], offer)
        )
    rows = []
    for recipient, items in waiting.items():
        items.sort(key=lambda item: -item[0])
        age, number, offer = items[0]
        source = issues.offer_source(offer)
        command = (
            f"agent-parley issue assign {number} --unassign {repo}"
            if source == issues.OPERATOR
            else f"agent-parley issue cancel {number} {repo}"
        )
        detail = (
            f"issue #{number} offered to {recipient} by {source}"
            if len(items) == 1
            else f"{len(items)} offers await {recipient}, the oldest issue "
            f"#{number} by {source}"
        )
        rows.append(
            _row(
                OFFER,
                detail,
                command,
                age,
                recipient,
                project["root"],
                BY_OPERATOR,
                len(items),
            )
        )
    return rows


def _request_rows(project: dict, now: float) -> list[dict]:
    """Groups the unanswered peer takeover requests, one row per holder.

    An operator's own request carries no row here, the same way an
    operator's own offer withdrawal is left to the operator, because it
    still waits on that operator to withdraw or restate it. A peer request
    is different: the peer asking has no path to force a decision, so the
    row names the holder that owes one.

    Args:
        project: One project block from the status reading.
        now: Unix time the request ages are measured against.

    Returns:
        One row per lane holding unanswered peer takeover requests, naming
        the oldest request and counting the rest, exactly as unanswered
        handoff offers are grouped by the lane that must decide.
    """
    repo = f"--repo {shlex.quote(str(project['root']))}"
    waiting: dict[str, list[tuple[int, int, dict]]] = {}
    for record in project["issues"]:
        pending = record["request"]
        if not pending or pending.get("source") != issues.PEER:
            continue
        created = _recorded(pending.get("created_at"), now)
        waiting.setdefault(record["owner"], []).append(
            (max(0, int(now - created)), record["issue"], pending)
        )
    rows = []
    for holder, items in waiting.items():
        items.sort(key=lambda item: -item[0])
        age, number, pending = items[0]
        peer = pending["to"]
        detail = (
            f"issue #{number} takeover asked by {peer}, unanswered by {holder}"
            if len(items) == 1
            else f"{len(items)} takeover requests wait on {holder}, the "
            f"oldest issue #{number} asked by {peer}"
        )
        rows.append(
            _row(
                REQUEST,
                detail,
                f"agent-parley issue assign {number} --unassign {repo}",
                age,
                holder,
                project["root"],
                BY_OPERATOR,
                len(items),
            )
        )
    return rows


def _bounce_rows(
    home: Path, directory: Path, data: dict, project: dict, config: dict
) -> list[dict]:
    """Derives one row per share whose recipients cannot act on it.

    The row sits on the sender's lane, because that is the lane still holding
    work it believed it had shared. The command names what the first blocked
    recipient needs, since clearing that condition is what lets the share be
    answered at all. An operator's own request carries no row here; it is
    already reported as awaiting acknowledgement.

    Args:
        home: Private bridge state root.
        directory: Private project state directory.
        data: Project manifest holding every participant.
        project: One project's status reading.
        config: Resolved supervision settings for the project.

    Returns:
        Zero or more rows, oldest share first.
    """
    records = {
        record["participant"]: record for record in project["participants"]
    }
    availability = {
        name: record.get("availability") or {}
        for name, record in records.items()
    }
    repo = f"--repo {shlex.quote(str(project['root']))}"
    rows = []
    for share in supervision.bounced_shares(
        home, directory, data, availability
    ):
        blocked = share["blocked"]
        listed = ", ".join(
            f"{entry['recipient']} {entry['reason']}" for entry in blocked[:3]
        )
        first = blocked[0]["lane"]
        record = records.get(first) or {
            "availability": availability.get(first)
            or {"state": supervision.UNKNOWN}
        }
        waking = bool(
            config["wake"]
            and (data["participants"].get(first) or {}).get("wake", True)
        )
        command, actor = _remedy(first, repo, record, waking)
        rows.append(
            _row(
                BOUNCE,
                f"share {share['message_id']} returned unanswerable: {listed}",
                command,
                share["waiting_seconds"],
                share["sender_lane"] or share["sender"],
                project["root"],
                actor,
            )
        )
    return rows


def _retired_rows(home: Path, project: dict) -> list[dict]:
    """Derives one row per retired lane whose shares retirement superseded.

    Retirement supersedes every delivery the lane still owed, so those
    shares never bounce; this row says once where they went instead of
    letting them vanish. It is informational: the shares cannot be answered
    and nothing moves them back. It lasts only while those shares are inside
    their acknowledgement deadline, the same bound a bounced share has, and a
    share recorded without a deadline counts for `RETIRED_WINDOW` seconds
    after retirement, one day, long enough for an operator returning the
    next session to read it.

    Args:
        home: Private bridge state root.
        project: One project's status reading.

    Returns:
        Zero or more rows, one per retired lane, carrying the share count.
    """
    groups: list[dict] = []
    with contextlib.suppress(BridgeError, OSError, sqlite3.Error):
        groups = store.superseded_shares(home, project["root"], RETIRED_WINDOW)
    rows = []
    for group in groups:
        reason = group["reason"]
        if not reason.endswith(" retired"):
            continue
        name = reason.removesuffix(" retired")
        count = group["count"]
        noun = "share" if count == 1 else "shares"
        rows.append(
            _row(
                RETIRED,
                f"{count} {noun} to {name} superseded: {reason}",
                "nothing to run; the row clears when those shares' "
                "acknowledgement deadlines pass",
                group["waiting_seconds"],
                name,
                project["root"],
                BY_OPERATOR,
                count,
            )
        )
    return rows


def _refused_rows(directory: Path, root: str, now: float) -> list[dict]:
    """Derives one row per issue whose approved live recovery was refused.

    The service retries an approved recovery on every poll and records why
    the last attempt could not proceed. The row lasts while the refused
    claim is still the issue's current claim, so a release, a handoff or a
    completed recovery clears it.

    Args:
        directory: Private project state directory.
        root: Canonical project key.
        now: Unix time the refusal ages are measured against.

    Returns:
        Zero or more rows, one per refused issue, on the owning lane.
    """
    rows = []
    with contextlib.suppress(OSError, ValueError):
        ledger = issues.snapshot(directory)["issues"]
        for number, record in ledger.items():
            refused = recovery.refusal(directory, number)
            if not refused or not record.get("claim_id"):
                continue
            if refused.get("claim_id") != record["claim_id"]:
                continue
            rows.append(
                _row(
                    REFUSED,
                    f"issue #{number}: approved recovery refused: "
                    f"{refused.get('reason', '')}",
                    "clear what the reason names in the owner's lane; the "
                    "service retries the recovery on every poll",
                    _age(refused.get("refused_at"), now),
                    str(record.get("owner") or ""),
                    root,
                )
            )
    return rows


def _integration_rows(directory: Path, root: str, now: float) -> list[dict]:
    """Reports the integration the project's base has not verified.

    The record is one per project, so this is the single escalation an
    unverified base raises, on the lane that may repair it and with the
    command that moves it on. A conflict whose merge was aborted leaves the
    base as verified before the attempt, so it raises no row here; its claim
    carries the repair state instead.

    Args:
        directory: Private project state directory.
        root: Canonical project key, which is the base checkout's path.
        now: Unix time the record's age is measured against.

    Returns:
        One row while the base carries an unverified integration, or none.
    """
    try:
        held = merges.integration_record(directory)
    except BridgeError as unreadable:
        return [
            _row(
                INTEGRATION,
                str(unreadable),
                "inspect the record the detail names",
                project=root,
            )
        ]
    if held is None:
        return []
    with contextlib.suppress(BridgeError, OSError):
        if not merges.integration_holds(Path(root), held):
            return []
    owner = merges.integration_owner(directory, held)
    result = held["result"][:12] or "an unfinished merge"
    return [
        _row(
            INTEGRATION,
            f"{held['kind']} at {result}, attempt {held['attempt']} of "
            f"{held['limit']}: {held['detail'] or 'no gate result recorded'}",
            merges.integration_remedy(Path(root), held, owner),
            _age(held.get("recorded_at"), now),
            owner or str(held["lane"]),
            root,
        )
    ]


def _root_rows(directory: Path, root: str, now: float) -> list[dict]:
    """Reports a project whose root checkout is gone, naming the lanes kept.

    The supervisor retires every lane once the root has been missing
    `root_gone_after`, except a lane whose session process is still alive or
    whose activity record cannot be read. Those lanes keep the project out
    of retirement until their sessions end, so the row names them. A root
    whose parent cannot be listed is reported unreachable and retires
    nothing. With no missing root, a root that returned after lanes retired
    is reported by `_returned_rows`.

    Args:
        directory: Private project state directory.
        root: Canonical project key.
        now: Unix time the absence is measured against.

    Returns:
        One row while the supervisor records the root as missing, or none.
    """
    try:
        recorded = json.loads(
            (directory / supervision.ROOT_PUBLICATION).read_text()
        )
    except (OSError, ValueError):
        return _returned_rows(directory, root, now)
    if not isinstance(recorded, dict) or recorded.get("retired"):
        return []
    live = [str(name) for name in recorded.get("live") or []]
    detail = (
        f"project root is gone; live lanes kept from retirement: "
        f"{', '.join(live)}"
        if live
        else "project root is unreachable: its parent directory cannot be "
        "listed, so no lane retires"
        if recorded.get("unreachable")
        else "project root is gone; lanes retire once it has been gone "
        "root_gone_after"
    )
    return [
        _row(
            ROOT,
            detail,
            "restore the root checkout, or end the named sessions so the "
            "next poll retires them",
            _age(recorded.get("since"), now),
            project=root,
            count=max(len(live), 1),
        )
    ]


def _returned_rows(directory: Path, root: str, now: float) -> list[dict]:
    """Reports lanes retired while a project root was gone that came back.

    The supervisor keeps `supervision.ROOT_RETURNED` once a root reappears
    after lanes retired for its absence, and removes it when every lane it
    names is re-admitted.

    Args:
        directory: Private project state directory.
        root: Canonical project key.
        now: Unix time the record's age is measured against.

    Returns:
        One row naming the retired lanes and their re-admit commands, or none.
    """
    try:
        recorded = json.loads(
            (directory / supervision.ROOT_RETURNED).read_text()
        )
    except (OSError, ValueError):
        return []
    if not isinstance(recorded, dict):
        return []
    names = [
        str(lane["participant"])
        for lane in recorded.get("lanes") or []
        if isinstance(lane, dict) and lane.get("participant")
    ]
    if not names:
        return []
    repo = f"--repo {shlex.quote(root)}"
    return [
        _row(
            ROOT_RETURNED,
            "project root returned after these lanes retired for its "
            f"absence: {', '.join(names)}",
            "; ".join(
                f"agent-parley participant add {name} {repo}" for name in names
            ),
            _age(recorded.get("returned_at"), now),
            project=root,
            count=len(names),
        )
    ]


def _forge_rows(directory: Path, root: str, now: float) -> list[dict]:
    """Reports a forge that refuses the supervisor's catalog read.

    The supervisor records the class of a failed catalog read beside
    ``failed_at`` in `FORGE_ISSUES`. A refused credential or a project the
    signed-in account cannot see stops every pull request and completion
    wake, so it is named with the `gh auth` remedy; a rate limit names the
    next retry instead.

    Args:
        directory: Private project state directory.
        root: Canonical project key.
        now: Unix time the failure's age is measured against.

    Returns:
        One row while the newest catalog read failed for one of those
        reasons less than `FORGE_STALE` seconds ago, or none.
    """
    try:
        cached = json.loads((directory / FORGE_ISSUES).read_text())
        failed_at = float(cached.get("failed_at") or 0)
        read_at = float(cached.get("read_at") or 0)
        kind = str(cached["failure"]["kind"])
        detail = str(cached["failure"].get("detail") or "")
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return []
    if failed_at <= read_at or now - failed_at >= FORGE_STALE:
        return []
    if kind in ("auth", "missing"):
        project = forge.slug(Path(root)) or "<owner>/<name>"
        owner = project.split("/")[0]
        command = f"gh auth status, then gh auth switch --user {owner}"
        cause = "refused the signed-in account"
    elif kind == "rate":
        retry = time.strftime(
            "%H:%M:%S", time.localtime(failed_at + FORGE_RETRY)
        )
        command = f"none; the supervisor retries at {retry}"
        cause = "rate limited the supervisor"
    else:
        return []
    return [
        _row(
            FORGE_REFUSED,
            f"forge {cause} ({kind}): {detail}".rstrip(": "),
            command,
            _age(failed_at, now),
            project=root,
        )
    ]


def _plan_rows(directory: Path, root: str, now: float) -> list[dict]:
    """Reports the plan revisions that wait on the operator.

    A lane's revision that repeats a contradiction is escalated rather than
    applied, and one outside the envelope is kept pending; neither changes
    the ledger, so nothing else would bring it to the operator. Each
    escalated proposal is its own row on the proposing lane, carrying the
    commands that settle it, and the pending proposals share one row that
    names the listing. Both are bounded by the proposals the plan retains.

    Args:
        directory: Private project state directory.
        root: Canonical project key.
        now: Unix time the proposal ages are measured against.

    Returns:
        One row per escalated proposal and at most one for the pending ones.
    """
    try:
        filed = plan.recorded(directory).get("proposals", {})
    except (OSError, ValueError):
        return []
    retained = list(filed.values())[-plan.MAX_PROPOSALS :]
    where = f"--repo {shlex.quote(str(root))}"
    rows = [
        _row(
            ESCALATED,
            f"plan proposal {item['id']} is escalated: "
            + "; ".join(item.get("held", [])),
            f"agent-parley plan approve {item['id']} {where} or "
            f"agent-parley plan reject {item['id']} --reason TEXT {where}",
            _age(item.get("at"), now),
            str(item.get("by") or ""),
            root,
        )
        for item in retained
        if item.get("status") == plan.ESCALATED
    ]
    pending = [item for item in retained if item.get("status") == plan.PENDING]
    if pending:
        rows.append(
            _row(
                PROPOSED,
                f"{len(pending)} plan proposals outside the revision "
                "envelope await the operator",
                f"agent-parley plan proposals {where}",
                max(_age(item.get("at"), now) for item in pending),
                project=root,
                count=len(pending),
            )
        )
    return rows


def _run_rows(
    directory: Path, manifest: dict, root: str, now: float
) -> list[dict]:
    """Reports an exhausted run budget and every lane it cannot meter.

    Args:
        directory: Private project state directory.
        manifest: Project manifest.
        root: Canonical project key.
        now: Unix time the exhaustion's age is measured against.

    Returns:
        One row while the run budget is exhausted; otherwise one row per
        lane refused because its use cannot be metered, or none.
    """
    stopped = budgets.halted(directory, manifest)
    if not stopped:
        return [
            _row(
                RUN_UNMETERED,
                f"{name} cannot be metered: no readable token records while "
                "a token limit is enforced, or a live session not started by "
                "agent-parley run while an hours limit is; no wake, dispatch "
                "or retry starts for it",
                f"agent-parley budget enforce --tokens 0 --hours 0 --repo "
                f"{root} (or relaunch the lane with agent-parley run on a "
                "provider whose transcripts Parley reads)",
                participant=name,
                project=root,
            )
            for name in budgets.unmetered(directory, manifest)
        ]
    return [
        _row(
            RUN_BUDGET,
            f"{stopped['cause']}; no wake, dispatch, retry or launch starts",
            f"agent-parley budget resume --repo {shlex.quote(str(root))} "
            "(add --reset to start "
            "a new accounting period, or raise the limit with agent-parley "
            "budget enforce)",
            _age(stopped.get("at"), now),
            project=root,
        )
    ]


def _checks_rows(directory: Path, root: str, now: float) -> list[dict]:
    """Reports each open pull request whose head supervision found stalled.

    The lane was already told once; the row keeps the stall in front of the
    operator until the head finishes or changes, which clears the mark.

    Args:
        directory: Private project state directory.
        root: Canonical project key.
        now: Unix time the pending ages are measured against.

    Returns:
        One row per pull request whose pending head is marked stalled.
    """
    try:
        cached = json.loads(
            (directory / supervision.PULL_REQUEST_RECORD).read_text()
        )
        readings = list((cached.get("pull_requests") or {}).values())
    except (OSError, ValueError, AttributeError):
        return []
    return [
        _row(
            CHECKS,
            f"pull request #{reading.get('number')} checks pending: "
            + ", ".join(
                f"{check.get('name')} {check.get('state')}"
                for check in reading.get("pending") or []
            ),
            f"gh pr checks {reading.get('url')}, then re-run once with "
            "gh run rerun RUN --failed, cancelling first a run still in "
            "progress past its job timeout",
            _age(reading.get("pending_since"), now),
            str(reading.get("lane") or ""),
            root,
        )
        for reading in readings
        if isinstance(reading, dict)
        and reading.get("checks") == "pending"
        and reading.get("stalled_at")
    ]


def _crossing_rows(project: dict, now: float) -> list[dict]:
    """Asks the operator once to cross a fully landed integration base.

    When no claim is held and at least one issue landed in the integration
    base still waits on the forge, the milestone's work sits complete on a
    branch the default branch never received. Merging it there cannot be
    undone, so the row only asks: nothing opens or merges the crossing pull
    request for the operator. The row clears once a claim is held again or
    the forge closes the landed issues. Without a whole reading of the
    forge's open issues, a landed issue the crossing already closed cannot
    be told apart, so no row is raised. The row ages from the newest
    landing or the newest claim end, whichever is later, so the claim that
    last stopped holding the work is what opens the decision.

    Args:
        project: One project of the status reading.
        now: Unix time the landing ages are measured against.

    Returns:
        One row while the integration base is fully landed, or none.
    """
    integration = project.get("integration") or {}
    landed = integration.get("landed") or []
    if not landed or integration.get("held") or not integration.get("catalog"):
        return []
    base = str(integration["base"])
    return [
        _row(
            CROSSING,
            f"every claim landed in {base}: "
            + ", ".join(f"#{item['issue']}" for item in landed)
            + "; the default branch has not received them",
            f"gh pr create --head {shlex.quote(base)}, naming each landed "
            "issue as Closes #N in its body; review and merge it yourself, "
            "since that merge cannot be undone",
            _age(
                max(
                    float(integration.get("settled_at") or 0),
                    *(float(item.get("at") or 0) for item in landed),
                ),
                now,
            ),
            project=str(project["root"]),
            count=len(landed),
        )
    ]


def _checks_failed_rows(directory: Path, root: str, now: float) -> list[dict]:
    """Reports each open pull request whose head's checks ran and failed.

    A check the forge never started names an operator cause and is reported
    by `_checks_refused_rows` instead, grouped across every pull request it
    blocks rather than once per lane.

    Args:
        directory: Private project state directory.
        root: Canonical project key.
        now: Unix time the attempt's age is measured against.

    Returns:
        One row per open pull request whose checks are red and report at
        least one check the forge ran and failed, or whose head is pending
        on the one automatic re-run `supervision.pull_request_wakes`
        requested for it. A red head after that re-run, or after the forge
        refused it, names the exact re-run command as its remedy.
    """
    try:
        cached = json.loads(
            (directory / supervision.PULL_REQUEST_RECORD).read_text()
        )
        readings = list((cached.get("pull_requests") or {}).values())
    except (OSError, ValueError, AttributeError):
        return []
    rows = []
    for reading in readings:
        if not isinstance(reading, dict):
            continue
        rerun = reading.get("rerun") or {}
        if reading.get("checks") == "pending" and rerun.get("accepted"):
            rows.append(
                _row(
                    CHECKS_FAILED,
                    f"pull request #{reading.get('number')} re-run "
                    f"requested: {rerun.get('checks')}",
                    "wait for the re-run's conclusion; nothing to push",
                    _age(rerun.get("at"), now),
                    str(reading.get("lane") or ""),
                    root,
                )
            )
            continue
        if reading.get("checks") != "red":
            continue
        ran = [
            check
            for check in reading.get("failed") or []
            if not check.get("not_started")
        ]
        if not ran:
            continue
        failing = ", ".join(
            f"{check['name']} {check['conclusion']}" for check in ran
        )
        if reading.get("exhausted"):
            rows.append(
                _row(
                    CI_ROUNDS_EXHAUSTED,
                    f"pull request #{reading.get('number')} used "
                    f"{reading.get('exhausted')} CI rounds: {failing}",
                    "operator took over the branch"
                    if reading.get("taken_over")
                    else "answer the decision: grant one more round or "
                    "take over; the lane stops pushing",
                    _age(reading.get("red_since"), now),
                    str(reading.get("lane") or ""),
                    root,
                )
            )
            continue
        attempt = int(reading.get("red_attempts") or 1)
        waiting = bool(rerun.get("accepted")) and attempt <= int(
            rerun.get("attempt") or 1
        )
        remedy = (
            f"gh pr checks {reading.get('url')}, then fix and push or re-run"
        )
        if waiting:
            remedy = "wait for the re-run's conclusion; nothing to push"
        elif rerun:
            commands = "; ".join(rerun.get("commands") or [])
            remedy = f"run `{commands}` yourself, or fix and push"
        rows.append(
            _row(
                CHECKS_FAILED,
                f"pull request #{reading.get('number')} checks failed "
                f"(attempt {attempt}"
                + (", re-run requested" if waiting else "")
                + "): "
                + failing,
                remedy,
                _age(reading.get("red_since"), now),
                str(reading.get("lane") or ""),
                root,
            )
        )
    return rows


def _checks_refused_rows(directory: Path, root: str, now: float) -> list[dict]:
    """Reports each cause blocking pull requests the forge refused to start.

    Every open pull request whose head reports a required check the forge
    never started shares one row per check name and conclusion, naming every
    pull request it blocks, so the operator answers the cause once rather
    than once per lane. `supervision.pull_request_wakes` opens the matching
    decision; this row only reads the same cached reading.

    Args:
        directory: Private project state directory.
        root: Canonical project key.
        now: Unix time the oldest occurrence's age is measured against.

    Returns:
        One row per distinct check name and forge conclusion an open pull
        request's red head reports as never started.
    """
    try:
        cached = json.loads(
            (directory / supervision.PULL_REQUEST_RECORD).read_text()
        )
        readings = list((cached.get("pull_requests") or {}).values())
    except (OSError, ValueError, AttributeError):
        return []
    groups: dict[tuple[str, str], list[dict]] = {}
    for reading in readings:
        if not isinstance(reading, dict) or reading.get("checks") != "red":
            continue
        for check in reading.get("failed") or []:
            if check.get("not_started"):
                key = (str(check["name"]), str(check["conclusion"]))
                groups.setdefault(key, []).append(reading)
    rows = []
    for (name, conclusion), members in groups.items():
        numbers = sorted(int(item["number"]) for item in members)
        listed = ", ".join(f"#{number}" for number in numbers)
        rows.append(
            _row(
                CHECKS_REFUSED,
                f"{len(numbers)} pull requests blocked: required job "
                f"{name!r} not started, forge reports {conclusion}: "
                f"{listed}",
                "resolve at the forge (billing, spending limit or manual "
                "approval), then re-run once",
                max(_age(item.get("red_since"), now) for item in members),
                project=root,
                count=len(numbers),
            )
        )
    return rows


def _grouped_gaps(found: list[dict], repo: str) -> list[dict]:
    """Folds one project's withheld-resume rows into a single row.

    The missing resume opt-in is one project setting, so five lanes that
    lack it are one decision for the operator, not five rows. A project
    with one such lane keeps its lane-scoped row.

    Args:
        found: Lane rows of one project.
        repo: Rendered `--repo` argument naming the project.

    Returns:
        The same rows, with every withheld-resume row replaced by one row
        that names the lanes and the project-wide opt-in command.
    """
    detail = WAKE_DETAILS[supervision.OPT_IN_MISSING]
    gaps = [
        row
        for row in found
        if row["condition"] == WAKE and row["detail"] == detail
    ]
    if len(gaps) < 2:
        return found
    names = ", ".join(row["participant"] for row in gaps)
    return [row for row in found if all(row is not gap for gap in gaps)] + [
        _row(
            WAKE,
            f"{detail}; lanes: {names}",
            f"{supervision.OPT_IN_COMMAND} {repo}, or resume each lane in "
            f"your own terminal with agent-parley run NAME --resume {repo}",
            max(row["seconds"] or 0 for row in gaps),
            "",
            gaps[0]["project"],
            BY_OPERATOR,
            len(gaps),
        )
    ]


def derive(
    home: Path, report: dict, ack_after: float = 0.0, now: float = 0.0
) -> list[dict]:
    """Lists every condition an operator should act on, oldest first.

    Args:
        home: Private bridge state root.
        report: The status reading the launcher produced.
        ack_after: Seconds after which a message awaiting acknowledgement is
            reported; the project's stall interval when zero.
        now: Unix time to compare against; the clock when zero.

    Returns:
        One row per lane per cause, ordered by how long the oldest item behind
        each has held, longest first, with every row older than a day moved
        after the rest so today's problems lead instead of rows a week old
        from lanes long gone. A store or service row carries no age and
        leads the list, because no other row can be acted on until the store is
        usable and the service is up. Each row names its actor: the rows the
        coordination service already handles report what its wake loop has
        attempted, and the rest carry what the operator can run. A row of a
        lane `folded` out as long dead that is itself older than the
        project's `fold_after` carries that interval as `folded`, so `lines`
        and `rendered` can count it instead of listing it. A row reports;
        nothing here revokes, releases or wakes.
    """
    stamp = now or time.time()
    schema = store.schema_state(store.schema_version(home))
    rows: list[dict] = []
    if repair := store.remedy(schema):
        rows.append(_row(STORE, f"schema is {schema}", repair))
    if not report["server"]["ready"]:
        rows.append(
            _row(SERVICE, "coordination server is not ready", "agent-parley up")
        )
    manifests = {
        data["root"]: (path.parent, data)
        for path in (home / "projects").glob("*/project.json")
        for data in [roster.normalize(json.loads(path.read_text()))]
    }
    aged: list[dict] = []
    for project in report["projects"]:
        directory, data = manifests[project["root"]]
        if failing := issues.supervision_error(directory):
            rows.append(
                _row(
                    SUPERVISING,
                    f"supervision poll failing; last: {failing['detail']}",
                    "read server.log in the state directory and fix the "
                    "stage it names; the next clean poll clears this row",
                    max(int(stamp - failing["since"]), 0),
                    project=project["root"],
                )
            )
        config = supervision.configuration(home, data)
        after = ack_after or config["stalled_after"]
        repo = f"--repo {shlex.quote(str(project['root']))}"
        start = len(aged)
        found: list[dict] = []
        states = {
            record["participant"]: record.get("lane_state")
            for record in project["participants"]
        }
        children = reclaim.children_by_lane(data)
        for record in project["participants"]:
            found.extend(
                _lane_rows(
                    record,
                    data["participants"][record["participant"]],
                    project["root"],
                    config,
                    after,
                    stamp,
                    directory,
                    supervision.live_peer(states, record["participant"]),
                )
            )
            aged.extend(
                _child_rows(
                    home,
                    data,
                    children.get(record["participant"], []),
                    record,
                    record["participant"],
                    repo,
                    project["root"],
                    stamp,
                )
            )
        aged.extend(_grouped_gaps(found, repo))
        aged.extend(_offer_rows(project, stamp))
        aged.extend(_request_rows(project, stamp))
        aged.extend(_bounce_rows(home, directory, data, project, config))
        aged.extend(_retired_rows(home, project))
        aged.extend(_refused_rows(directory, project["root"], stamp))
        aged.extend(_integration_rows(directory, project["root"], stamp))
        aged.extend(_root_rows(directory, project["root"], stamp))
        aged.extend(_forge_rows(directory, project["root"], stamp))
        aged.extend(_plan_rows(directory, project["root"], stamp))
        aged.extend(_run_rows(directory, data, project["root"], stamp))
        aged.extend(_checks_rows(directory, project["root"], stamp))
        aged.extend(_checks_failed_rows(directory, project["root"], stamp))
        aged.extend(_checks_refused_rows(directory, project["root"], stamp))
        aged.extend(_crossing_rows(project, stamp))
        _fold(aged[start:], project, config["fold_after"])
    aged.sort(
        key=lambda row: (_stale(row), -(row["seconds"] or 0)),
    )
    return rows + aged


def _stale(row: dict) -> bool:
    """Tells whether the oldest item behind a row has held past a day.

    Args:
        row: One row `derive` produced.

    Returns:
        True when the row's age exceeds `STALE_AFTER`; False for a younger
        row or one whose age is unknown.
    """
    return (row["seconds"] or 0) > STALE_AFTER


def _fold(rows: list[dict], project: dict, after: float) -> None:
    """Marks the old rows of one project's long-dead lanes as folded.

    Args:
        rows: The rows `derive` produced for the project, marked in place.
        project: The project record from the status reading.
        after: The project's retention interval, `fold_after`, in seconds.
    """
    gone = {
        record["participant"]
        for record in project["participants"]
        if folded(record, after)
    }
    for row in rows:
        if row["participant"] in gone and (row["seconds"] or 0) > after:
            row["folded"] = after


def shown(rows: list[dict], everything: bool = False) -> list[dict]:
    """Keeps the rows a default view lists.

    Args:
        rows: Rows `derive` produced.
        everything: Keep the folded rows too, as `--all` asks.

    Returns:
        Every row when `everything` is set, else the rows not folded.
    """
    if everything:
        return rows
    return [row for row in rows if not row.get("folded")]


def lines(rows: list[dict], everything: bool = False) -> list[str]:
    """Renders the rows as one line each, or one line saying there are none.

    Args:
        rows: Rows `derive` produced.
        everything: List the folded rows too instead of counting them.

    Returns:
        One line per row, with `STALE_HEADING` above the trailing rows
        older than a day, closed by a line counting what needs an operator
        against what the coordination service is handling whenever any row
        belongs to the service, so a screen of rows still says how much of it
        is someone's work. Folded rows are left out and counted on one last
        line naming `--all`, unless `everything` lists them.
    """
    hidden = [row["folded"] for row in rows if row.get("folded")]
    rows = shown(rows, everything)
    if everything or not hidden:
        closing = []
    else:
        closing = [fold_line(len(hidden), max(hidden), "dead-lane problem")]
    if not rows:
        return closing or [
            "No problems: every lane, claim and store reading is clear."
        ]
    width = max(len(row["condition"]) for row in rows)
    named = max(len(row["participant"] or "-") for row in rows)
    listed = [
        f"{tables.age(-1 if row['seconds'] is None else row['seconds']):>4}  "
        f"{(row['participant'] or '-').ljust(named)}  "
        f"{row['condition'].ljust(width)}  {row['detail']}; {row['command']}"
        for row in rows
    ]
    tail = len(rows)
    while tail and _stale(rows[tail - 1]):
        tail -= 1
    if tail < len(rows):
        listed.insert(tail, STALE_HEADING)
    handled = sum(row["actor"] == BY_SERVICE for row in rows)
    if handled:
        listed.append(
            f"{len(rows) - handled} need an operator; {handled} the "
            "coordination service is handling."
        )
    return listed + closing


def rendered(rows: list[dict], everything: bool = False) -> dict:
    """Shapes the rows for the machine-readable document.

    Args:
        rows: Rows `derive` produced.
        everything: Keep the folded rows in the document too.

    Returns:
        The rows `shown` keeps, their count, the split between the rows an
        operator owns and the rows the coordination service is already
        handling, and how many folded rows were left out.
    """
    kept = shown(rows, everything)
    handled = sum(row["actor"] == BY_SERVICE for row in kept)
    return {
        "problems": kept,
        "count": len(kept),
        "operator": len(kept) - handled,
        "service": handled,
        "folded": len(rows) - len(kept),
    }
