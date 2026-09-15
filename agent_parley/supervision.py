"""Observes lane availability and reminds holders about waiting peers."""

import contextlib
import hashlib
import json
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

from agent_parley import (
    forge,
    issues,
    process,
    records,
    roster,
    store,
    terminal,
)
from agent_parley.state import BridgeError, lock, write_json

DEFAULTS = {
    "interval": 30,
    "inactive_after": 300,
    "stalled_after": 600,
    "prompts": True,
    "wake": True,
}


def settings(value: dict) -> dict:
    """Validates supervision settings stored outside the repository."""
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        raise BridgeError("Invalid supervision settings.")
    result = {**DEFAULTS, **value}
    for field in ("interval", "inactive_after", "stalled_after"):
        if (
            type(result[field]) not in (int, float)
            or not 1 <= result[field] <= 86400
        ):
            raise BridgeError(f"{field} must be between 1 and 86400 seconds.")
    for field in ("prompts", "wake"):
        if type(result[field]) is not bool:
            raise BridgeError(f"{field} must be a boolean.")
    return result


def presence(directory: Path, name: str, inactive_after: float = 300) -> dict:
    """Derives availability from the recorded process and native checkpoints.

    A live but quiet session is distinguishable from a dead launcher. No
    heartbeat from the participant is required, and no ownership is inferred.

    Args:
        directory: Private project state directory.
        name: Participant name.
        inactive_after: Checkpoint age after which a live lane is unreachable.

    Returns:
        State, process liveness, last native activity time and observed age.
    """
    path = directory / f"{name}-activity.json"
    value = json.loads(path.read_text()) if path.exists() else {}
    alive = process.alive(value.get("session_pid"), value.get("session_ticks"))
    age = max(0, time.time() - value.get("updated", 0))
    state = "active" if alive and age <= inactive_after else "unreachable"
    return {
        "state": state,
        "process_alive": alive,
        "last_active": value.get("updated"),
        "age_seconds": int(age),
    }


def stall(
    home: Path, directory: Path, manifest: dict, name: str, after: float
) -> dict:
    """Names a live lane that owes an answer and is serving no calls.

    A lane whose turn ended while an unread or unacknowledged message sat in
    its inbox looks healthy in every column: the process is alive, the branch
    is right, and the mail counter is a number rather than a wait. Naming that
    stall needs no new mechanism, because the runtime already records when the
    message arrived and when a call was last served for the lane.

    The report is read-only. It revokes nothing, releases nothing, moves no
    ownership and wakes nobody; it states what an operator or a waiting peer
    would otherwise have to work out from timestamps by hand.

    Args:
        home: Private bridge state root.
        directory: Private state directory for the common repository.
        manifest: Project manifest holding this participant.
        name: Participant that owns the lane.
        after: Seconds of silence after which a waiting item reads as a stall.

    Returns:
        Whether the lane is stalled, the oldest waiting item and its age, and
        the age of the last served call. A lane whose recorded session process
        is not alive is never reported as stalled: it is stopped, which the
        session state already says.
    """
    from agent_parley import checkpoints

    idle = {
        "stalled": False,
        "kind": None,
        "message_id": None,
        "sender": "",
        "age_seconds": 0,
        "served_age_seconds": None,
    }
    state = checkpoints.activity(directory, name)
    if not process.alive(state.get("session_pid"), state.get("session_ticks")):
        return idle
    try:
        report = store.waiting(
            home, manifest["root"], manifest["participants"][name]["display"]
        )
    except (BridgeError, OSError, sqlite3.Error):
        return idle
    served = report["served_age_seconds"]
    idle.update(report)
    idle["stalled"] = bool(
        report["kind"]
        and report["age_seconds"] >= after
        and (served is None or served >= after)
    )
    return idle


def stall_marker(idle: dict) -> str:
    """Describes a stall in one line, naming the oldest waiting item."""
    if not idle["stalled"]:
        return ""
    item = (
        f"message {idle['message_id']} from {idle['sender']}"
        if idle["kind"] == "unread"
        else f"acknowledgement of message {idle['message_id']} "
        f"for {idle['sender']}"
    )
    return f"idle; {item} waiting {int(idle['age_seconds'])}s"


def dirty_paths(root: str) -> list[str] | None:
    """Lists the paths Git reports as changed in the base checkout.

    Args:
        root: Canonical project key, which is the base checkout's path.

    Returns:
        Repository-relative paths in porcelain order, a rename reported by its
        new name, or None when Git could not answer inside its timeout. A
        checkout Git cannot inspect is no opinion rather than a clean one.
    """
    try:
        result = subprocess.run(
            ["git", "-C", root, "status", "--porcelain", "-uall"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode:
        return None
    paths = []
    for line in result.stdout.splitlines():
        if len(line) < 4:
            continue
        entry = line[3:]
        if line[0] in "RC" and " -> " in entry:
            entry = entry.split(" -> ", 1)[1]
        paths.append(entry.rstrip("/"))
    return paths


def operator_edits(home: Path, manifest: dict) -> dict[str, list[str]]:
    """Names the reserved paths an operator has changed in the base checkout.

    Reservations only ever saw other lanes. A person editing the base checkout
    is invisible to every lane until the merge conflicts, so this reading
    compares what Git reports as dirty there against every lane's active
    reservation patterns, using the same overlap rule a competing reservation
    is judged by. One Git call and one store query answer every lane.

    The reading is advisory. Nothing pauses, reverts or locks; a reservation
    stays what it always was, and the operator's work stays where it is.

    Args:
        home: Private bridge state root.
        manifest: Project manifest naming the base checkout and the roster.

    Returns:
        Mapping of participant name to the sorted dirty paths that overlap a
        reservation it holds. Empty when the checkout is clean, when nothing
        is reserved, or when Git or the store could not be read.
    """
    dirty = dirty_paths(manifest["root"])
    if not dirty:
        return {}
    try:
        held = store.active_reservations(home, manifest["root"])
    except (BridgeError, OSError, sqlite3.Error):
        return {}
    collisions: dict[str, list[str]] = {}
    for name, participant in manifest["participants"].items():
        patterns = held.get(participant["display"], [])
        matched = sorted(
            {
                path
                for path in dirty
                for pattern in patterns
                if store.overlapping(path, pattern)
            }
        )
        if matched:
            collisions[name] = matched
    return collisions


def operator_edit_marker(paths: list[str]) -> str:
    """Describes an operator collision in one line, naming the paths."""
    if not paths:
        return ""
    plural = "" if len(paths) == 1 else "s"
    return (
        f"operator edited reserved path{plural} {', '.join(paths)} in the "
        "base checkout; nothing was reverted"
    )


FIT_CHECKS = ("session", "capacity", "worktree", "mail")
UNKNOWN_FIT: dict = {
    "fit": None,
    "checks": dict.fromkeys(FIT_CHECKS),
    "failed": [],
    "reason": "",
    "offer": None,
}


def _session_check(directory: Path, name: str) -> tuple[bool | None, str]:
    """Reads whether the lane's recorded session process is running.

    Args:
        directory: Private project state directory.
        name: Participant that owns the lane.

    Returns:
        The check result and, when it failed, why. A lane that published no
        activity yet reports None, which is no opinion rather than a refusal.
    """
    from agent_parley import checkpoints

    state = checkpoints.activity(directory, name)
    if not state:
        return None, ""
    if not process.alive(state.get("session_pid"), state.get("session_ticks")):
        return False, "its session process is not running"
    if state.get("activity") == "stopped":
        return False, "its session ended"
    if state.get("activity") == "waiting for approval":
        return False, "it is waiting for a native approval"
    return True, ""


def _capacity_check(
    home: Path, participant: dict, after: float
) -> tuple[bool | None, str]:
    """Reads the lane's own client records for a recent usage refusal.

    The reader is provider specific and defaults to no opinion, so a provider
    whose client publishes no such record skips the check instead of blocking
    an offer. Nothing is asked of a vendor.

    Args:
        home: Private bridge state root.
        participant: Manifest entry naming the lane, provider and account.
        after: Seconds within which a recorded refusal still counts.

    Returns:
        The check result and, when it failed, how old the refusal is.
    """
    at = records.reported_refusal(home, participant)
    if at is None:
        return None, ""
    age = max(0.0, time.time() - at)
    if age < after:
        return False, f"its client refused a request {int(age)}s ago"
    return True, ""


def _worktree_check(participant: dict) -> tuple[bool | None, str]:
    """Reads whether the lane is on its branch or has nothing uncommitted.

    Args:
        participant: Manifest entry naming the lane and its assigned branch.

    Returns:
        The check result and, when it failed, the branch that holds the work.
        A worktree Git cannot inspect reports None.
    """
    from agent_parley import checkpoints

    lane = Path(participant["lane"])
    branch = checkpoints.lane_branch(lane)
    if branch == participant["branch"]:
        return True, ""
    try:
        result = subprocess.run(
            ["git", "-C", str(lane), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, ""
    if result.returncode:
        return None, ""
    if result.stdout.strip():
        return False, f"it holds uncommitted work on {branch}"
    return True, ""


def _mail_check(
    home: Path, manifest: dict, name: str, after: float
) -> tuple[bool | None, str]:
    """Reads whether the lane owes an old acknowledgement.

    Args:
        home: Private bridge state root.
        manifest: Project manifest holding this participant.
        name: Participant that owns the lane.
        after: Seconds after which an unanswered acknowledgement counts.

    Returns:
        The check result and, when it failed, the age of the oldest item. An
        unreadable mailbox reports None.
    """
    from agent_parley import checkpoints

    try:
        mail = checkpoints.mailbox(
            home, manifest["root"], manifest["participants"][name]["display"]
        )
    except (BridgeError, OSError, sqlite3.Error):
        return None, ""
    oldest = max(
        (int(item["age_seconds"]) for item in mail["outstanding_ack"]),
        default=0,
    )
    if oldest >= after:
        return False, f"it owes an acknowledgement {oldest}s old"
    return True, ""


def fit(
    home: Path, directory: Path, manifest: dict, name: str, after: float
) -> dict:
    """Reports whether a lane could take more work right now.

    Offering work to a lane that cannot take it stalls twice, so every offer
    is checked first against what the runtime can read locally: the recorded
    session process, the lane's own client records, its worktree, and the
    acknowledgements it owes. Each check defaults to no opinion, and only a
    check that actually failed makes a lane unfit.

    The result describes what was observed. It grants nothing, revokes
    nothing, and never moves ownership.

    Args:
        home: Private bridge state root.
        directory: Private project state directory.
        manifest: Project manifest holding this participant.
        name: Participant whose lane is being considered.
        after: Seconds of the interval a refusal or an unanswered item is
            still counted within.

    Returns:
        Whether the lane is fit, each check's result, the names of the failed
        checks and one line naming the first failure.
    """
    participant = manifest["participants"][name]
    results = {
        "session": _session_check(directory, name),
        "capacity": _capacity_check(home, participant, after),
        "worktree": _worktree_check(participant),
        "mail": _mail_check(home, manifest, name, after),
    }
    failed = [check for check in FIT_CHECKS if results[check][0] is False]
    return {
        "fit": not failed,
        "checks": {check: results[check][0] for check in FIT_CHECKS},
        "failed": failed,
        "reason": (
            f"unfit ({failed[0]}): {name} {results[failed[0]][1]}"
            if failed
            else ""
        ),
    }


def idle_seconds(directory: Path, name: str) -> int:
    """Reports how long the lane's still-open idle stretch has lasted.

    Args:
        directory: Private project state directory.
        name: Participant that owns the lane.

    Returns:
        Seconds of observed coordination inactivity since the lane's last turn
        ended, or zero when no stretch is open. This measures a quiet
        coordination channel, which is not a claim about what the native
        client is doing inside a turn.
    """
    from agent_parley import metrics

    return next(
        (
            int(interval["seconds"])
            for interval in reversed(
                metrics.idle_intervals(directory, name)["intervals"]
            )
            if interval.get("open")
        ),
        0,
    )


def published_work(directory: Path, name: str) -> dict:
    """Reads the fit result and work offer last published for a lane.

    Args:
        directory: Private project state directory.
        name: Participant that owns the lane.

    Returns:
        The published record, or an unknown result when the supervisor has
        published nothing for this lane yet.
    """
    unknown = {**UNKNOWN_FIT, "checks": dict(UNKNOWN_FIT["checks"])}
    try:
        record = json.loads((directory / f"{name}-work.json").read_text())
    except (OSError, ValueError):
        return unknown
    return {**unknown, **record} if isinstance(record, dict) else unknown


def _pull_text(available: list[str], busy: list[str]) -> str:
    """Describes the work an idle lane could take from the ledger."""
    parts = ["Work offer. You hold no claim and every fit check passed."]
    if available:
        listed = ", ".join(f"#{number}" for number in available[:5])
        parts.append(
            f"Unclaimed and unblocked, most unblocking first: {listed}."
        )
    if busy:
        parts.append(f"Holding more than one claim: {', '.join(busy)}.")
    parts.append(
        "Claim one yourself with agent-parley issue claim, or ask a holder "
        "for a handoff. Nothing is claimed for you."
    )
    return " ".join(parts)


def _rebalance_text(owned: list[str], idle: list[str]) -> str:
    """Describes the claims a busy lane could shed to an idle fit peer."""
    listed = ", ".join(f"#{number}" for number in owned[:5])
    return (
        f"Rebalance offer. {', '.join(idle)} read as fit and have been idle "
        f"past the stall interval while you hold {listed}. Shed one with "
        "agent-parley issue offer if it helps; you decide, and ownership "
        "moves only when the recipient accepts."
    )


def work(home: Path, directory: Path, manifest: dict, config: dict) -> None:
    """Publishes each lane's fit result and any advisory work offer.

    A lane holding no claim is offered the unclaimed work and told which peers
    hold more than one claim. A lane holding more than one claim is told which
    fit peers have been idle past the stall interval. Both are advisory: the
    ledger is not touched, nothing is claimed, and ``issue offer`` remains the
    only path that moves work.

    An offer carries a digest of its own content as its identifier, so a lane
    whose situation has not changed sees the same offer rather than a new one
    on every poll.

    Args:
        home: Private bridge state root.
        directory: Private project state directory.
        manifest: Current participant manifest.
        config: Resolved supervision settings.
    """
    after = config["stalled_after"]
    ledger = issues.snapshot(directory)
    owned = issues.holders(ledger)
    available = issues.unclaimed(ledger)
    busy = sorted(name for name, held in owned.items() if len(held) > 1)
    results = {
        name: fit(home, directory, manifest, name, after)
        for name in manifest["participants"]
    }
    idle = [
        name
        for name in sorted(manifest["participants"])
        if results[name]["fit"]
        and not owned.get(name)
        and idle_seconds(directory, name) >= after
    ]
    for name in manifest["participants"]:
        result = results[name]
        held = owned.get(name, [])
        offer = None
        if result["fit"] and not held and (available or busy):
            offer = {"kind": "pull", "text": _pull_text(available, busy)}
        elif len(held) > 1 and (peers := [e for e in idle if e != name]):
            offer = {"kind": "rebalance", "text": _rebalance_text(held, peers)}
        if offer:
            offer["id"] = hashlib.sha256(
                f"{offer['kind']}\x00{offer['text']}".encode()
            ).hexdigest()[:16]
        published = {**result, "offer": offer}
        path = directory / f"{name}-work.json"
        if published != published_work(directory, name):
            write_json(path, published)


def configuration(home: Path, manifest: dict) -> dict:
    """Resolves project settings while honoring the global wake opt-out."""
    path = home / "supervision.json"
    global_config = settings(
        json.loads(path.read_text()) if path.exists() else {}
    )
    config = settings({**global_config, **manifest.get("supervision", {})})
    config["wake"] = config["wake"] and global_config["wake"]
    config["prompts"] = config["prompts"] and global_config["prompts"]
    return config


def reminders(directory: Path, manifest: dict, closed: set[str]) -> None:
    """Records idempotent reminders without releasing or transferring claims.

    Args:
        directory: Private project state directory.
        manifest: Current participant manifest.
        closed: Issues whose lane pull request is observed merged or closed.
    """
    after_message_id = 0
    home = directory.parent.parent
    if (home / store.DATABASE).exists():
        with store.connect(home) as db:
            after_message_id = db.execute(
                "SELECT coalesce(max(id),0) FROM messages"
            ).fetchone()[0]
    with lock(directory / "issues.lock", timeout=1):
        ledger = issues.snapshot(directory)
        changed = False
        for number, record in ledger["issues"].items():
            waiting = sorted(
                {
                    other["owner"]
                    for other in ledger["issues"].values()
                    if number in other.get("blocked_by", [])
                    and other.get("owner")
                }
            )
            if not waiting and number not in closed:
                continue
            history = record.get("history", [])
            if not history:
                continue
            released = history and history[-1]["action"] == "release"
            holder = record.get("owner") or (
                history[-1]["actor"] if released else None
            )
            if holder not in manifest["participants"]:
                continue
            if not released and number not in closed:
                continue
            trigger = "claim released" if released else "pull request ended"
            identifier = f"{number}:{history[-1]['at']}:{trigger}"
            if record.get("handoff_prompt", {}).get("id") == identifier:
                continue
            recipients = ", ".join(waiting) or "project peers"
            record["handoff_prompt"] = {
                "id": identifier,
                "holder": holder,
                "waiting": waiting,
                "created": time.time(),
                "after_message_id": after_message_id,
                "trigger": trigger,
                "text": (
                    f"Issue #{number}: {trigger}. {holder}, send an explicit "
                    f"completion message to {recipients} "
                    "with the "
                    "commit, verification and remaining work. Ownership "
                    "does not move until an explicit handoff."
                ),
            }
            changed = True
        if changed:
            ledger["revision"] += 1
            write_json(directory / "issues.json", ledger)


def deadline_notices(directory: Path, manifest: dict) -> None:
    """Records one bounded notice for each claim that passed its budget.

    The notice is written once per breach, identified by the deadline or the
    attempt count that caused it, so a lane and its waiting peers are told
    once rather than on every read. Recording a notice moves nothing: the
    issue keeps its owner, its offer and its dependencies, and the notice
    itself says so.

    Args:
        directory: Private project state directory.
        manifest: Current participant manifest.
    """
    with lock(directory / "issues.lock", timeout=1):
        ledger = issues.snapshot(directory)
        changed = False
        for number, record in ledger["issues"].items():
            holder = record.get("owner")
            if holder not in manifest["participants"]:
                continue
            timing = issues.deadline_state(record)
            if not (timing["overdue"] or timing["budget_exceeded"]):
                continue
            identifier = (
                f"{number}:{timing['deadline']}:{timing['attempts']}:"
                f"{int(timing['budget_exceeded'])}"
            )
            if record.get("deadline_notice", {}).get("id") == identifier:
                continue
            waiting = sorted(
                {
                    other["owner"]
                    for other in ledger["issues"].values()
                    if number in other.get("blocked_by", [])
                    and other.get("owner")
                }
            )
            cause = (
                "is past its attempt budget"
                if timing["budget_exceeded"]
                else f"is overdue by {timing['overdue_seconds']}s"
            )
            record["deadline_notice"] = {
                "id": identifier,
                "holder": holder,
                "waiting": waiting,
                "created": time.time(),
                "text": (
                    f"Issue #{number} {cause}. {holder} still owns it: a "
                    "deadline reports, it never transfers. Ask for a handoff "
                    "or release it explicitly."
                ),
            }
            changed = True
        if changed:
            ledger["revision"] += 1
            write_json(directory / "issues.json", ledger)


def claimed_since(record: dict) -> float:
    """Reports when the current ownership generation of an issue began.

    A lane branch is reused across claims, so a pull request that ended before
    the current claim started describes earlier work and must not report that
    claim as finished. The generation starts at the most recent claim or
    accepted handoff; a record whose history no longer names one is treated as
    having always been owned, which preserves the previous observation.

    Args:
        record: Published ledger record for one issue.

    Returns:
        Unix time the current ownership generation began, or zero.
    """
    return max(
        (
            float(entry.get("at", 0) or 0)
            for entry in record.get("history", [])
            if entry.get("action") in {"claim", "accept"}
        ),
        default=0.0,
    )


def reported_since(directory: Path, name: str, since: float) -> bool:
    """Reports whether a lane filed a report after a recorded instant.

    Args:
        directory: Private project state directory.
        name: Participant whose reports are read.
        since: Unix time the recorded item was written.

    Returns:
        Whether the lane recorded a report after that instant.
    """
    path = directory / f"{name}-activity.json"
    if not path.exists():
        return False
    try:
        state = json.loads(path.read_text())
    except ValueError:
        return False
    return float(state.get("reported_at") or 0) > since


def eligible(item: dict, ledger: dict, now: float) -> bool:
    """Reports whether a recorded item's time and condition are both met.

    An item carrying both a not-before time and a condition waits for both. A
    condition is answered from recorded ledger transitions only, so nothing
    delivers on an inference about branch or pull request history.

    Args:
        item: Recorded item as the store reports it.
        ledger: Published issue records of the project.
        now: Unix time the poll is evaluating.

    Returns:
        Whether the item is deliverable now.
    """
    if item["not_before"] is not None and now < item["not_before"]:
        return False
    condition = item["condition"]
    if condition.startswith("released:"):
        return issues.released(ledger.get(condition.split(":", 1)[1]) or {})
    return True


def hand_off(home: Path, directory: Path, manifest: dict, item: dict) -> None:
    """Applies one recorded handoff offer and records it as delivered.

    An offer lives in the issue ledger rather than the mailbox, so a poll
    interrupted between the two substrates would otherwise retry an offer the
    ledger already carries. A refused transition whose result is nevertheless
    the recorded offer is therefore treated as delivered; any other refusal
    leaves the item waiting, and the operator can cancel it.

    Args:
        home: Private bridge state root.
        directory: Private project state directory.
        manifest: Current participant manifest.
        item: Recorded item as the store reports it.
    """
    try:
        issues.change(
            directory,
            item["actor"],
            "offer",
            item["issue"],
            participants=set(manifest["participants"]),
            to=item["recipient"],
            summary=item["body_md"],
            defaults=manifest["deadlines"],
        )
    except BridgeError:
        record = issues.snapshot(directory)["issues"].get(item["issue"]) or {}
        if (record.get("offer") or {}).get("to") != item["recipient"]:
            return
    store.complete_schedule(home, manifest["root"], item["id"])


def deliveries(home: Path, directory: Path, manifest: dict) -> None:
    """Delivers the recorded operator items whose trigger has arrived.

    Delivery happens here and nowhere else: a status view, a dashboard refresh
    or any other read-only path reports pending items and never releases one.
    A delayed message the lane has already answered with a report is dropped
    rather than delivered when it was recorded with that opt-out.

    Args:
        home: Private bridge state root.
        directory: Private project state directory.
        manifest: Current participant manifest.
    """
    now = time.time()
    ledger = issues.snapshot(directory)["issues"]
    for item in store.schedules(home, manifest["root"]):
        participant = manifest["participants"].get(item["recipient"])
        if participant is None:
            continue
        with contextlib.suppress(BridgeError, OSError, sqlite3.Error):
            if item["unless_reported"] and reported_since(
                directory, item["recipient"], item["created_ts"]
            ):
                store.cancel_schedule(home, manifest["root"], item["id"])
            elif eligible(item, ledger, now):
                if item["kind"] == "offer":
                    hand_off(home, directory, manifest, item)
                else:
                    store.deliver_schedule(
                        home,
                        manifest["root"],
                        item["id"],
                        participant["display"],
                    )


def poll(home: Path, directory: Path) -> None:
    """Refreshes presence, delivers due operator items and reminds holders.

    Recorded operator items are delivered here, before reminders and waking,
    so a message whose time or condition has just arrived is part of the
    backlog this same poll may wake the lane for.
    """
    manifest = roster.read(directory)
    config = configuration(home, manifest)
    observations = {
        name: presence(directory, name, config["inactive_after"])
        for name in manifest["participants"]
    }
    with store.connect(home, write=True, timeout=0) as db:
        for name, participant in manifest["participants"].items():
            observed = observations[name]
            db.execute(
                "INSERT INTO participant_presence(agent_id,state,process_alive,"
                "observed_ts,last_active) SELECT a.id,?,?,?,? FROM agents a "
                "JOIN projects p ON p.id=a.project_id WHERE p.human_key=? "
                "AND a.name=? ON CONFLICT(agent_id) DO UPDATE SET "
                "state=excluded.state,process_alive=excluded.process_alive,"
                "observed_ts=excluded.observed_ts,last_active=excluded.last_active",
                (
                    observed["state"],
                    observed["process_alive"],
                    time.time(),
                    observed["last_active"],
                    manifest["root"],
                    participant["display"],
                ),
            )
    deliveries(home, directory, manifest)
    if config["prompts"]:
        closed: set[str] = set()
        ledger = issues.snapshot(directory)
        for name, participant in manifest["participants"].items():
            claimed = {
                number: claimed_since(record)
                for number, record in ledger["issues"].items()
                if record.get("owner") == name
            }
            if not claimed:
                continue
            completion = forge.branch_completion(
                Path(manifest["root"]), participant["branch"]
            )
            if completion is None or completion[0] not in {"MERGED", "CLOSED"}:
                continue
            closed.update(
                number
                for number, since in claimed.items()
                if completion[1] >= since
            )
        reminders(directory, manifest, closed)
        deadline_notices(directory, manifest)
        with contextlib.suppress(OSError):
            (directory / issues.SUPERVISION_ERROR).unlink(missing_ok=True)
        observe_responses(home, directory, manifest)
        work(home, directory, manifest, config)
    if config["wake"]:
        for name, participant in manifest["participants"].items():
            if participant.get("wake", True):
                wake(
                    home, directory, manifest, name, observations[name], config
                )


def observe_responses(home: Path, directory: Path, manifest: dict) -> None:
    """Marks reminders responded to after explicit mail reaches every waiter.

    This observes delivery, not the semantic completeness of a handoff. It
    never acknowledges mail, releases an issue or transfers ownership.
    """
    with lock(directory / "issues.lock", timeout=1):
        ledger = issues.snapshot(directory)
        changed = False
        with store.connect(home) as db:
            for record in ledger["issues"].values():
                prompt = record.get("handoff_prompt")
                if not prompt or prompt.get("responded_at"):
                    continue
                holder = manifest["participants"].get(prompt["holder"])
                if not holder:
                    continue
                recipients = {
                    row["name"]
                    for row in db.execute(
                        "SELECT recipient.name FROM messages m "
                        "JOIN agents sender ON sender.id=m.sender_id "
                        "JOIN projects p ON p.id=m.project_id "
                        "JOIN message_recipients r ON r.message_id=m.id "
                        "JOIN agents recipient ON recipient.id=r.agent_id "
                        "WHERE p.human_key=? AND sender.name=? "
                        "AND m.id>?",
                        (
                            manifest["root"],
                            holder["display"],
                            prompt.get("after_message_id", 0),
                        ),
                    )
                }
                if recipients and all(
                    manifest["participants"].get(name, {}).get("display")
                    in recipients
                    for name in prompt["waiting"]
                ):
                    prompt["responded_at"] = time.time()
                    changed = True
        if changed:
            ledger["revision"] += 1
            write_json(directory / "issues.json", ledger)


def wake(
    home: Path,
    directory: Path,
    manifest: dict,
    name: str,
    observed: dict,
    config: dict,
) -> None:
    """Requests or resumes a native turn with bounded attempts per backlog.

    The backlog counts every reason this lane owes someone a turn: unread or
    unacknowledged mail, an unanswered completion reminder it holds, and a
    handoff offer naming it as recipient. An offer alone is enough, because a
    peer that offers an issue to an idle lane would otherwise wait for an
    unrelated trigger. An offer that was cancelled, declined or accepted is no
    longer recorded on its issue and so leaves the backlog, and a replacement
    offer carries a new identifier, which resets the bounded attempt count
    rather than extending the old one. A request the launcher refuses as busy
    is spaced like any other but does not count against the bound, because
    the lane never received a turn to decline; it is asked again once it is
    idle.

    The launcher still owns native authentication, trust and approval prompts.
    A resumed process uses a real terminal, not an unattended permission mode.
    Nothing reads, acknowledges, releases, accepts or transfers work for the
    lane; waking only asks the lane to take its own turn.
    """
    participant = manifest["participants"][name]
    path = directory / f"{name}-activity.json"
    state = json.loads(path.read_text()) if path.exists() else {}
    if state.get("activity") not in {"idle", "stopped"}:
        return
    if (
        observed["process_alive"]
        and observed["age_seconds"] < config["inactive_after"]
    ):
        return
    with store.connect(home) as db:
        pending = db.execute(
            "SELECT m.id FROM messages m JOIN message_recipients r "
            "ON r.message_id=m.id JOIN agents a ON a.id=r.agent_id "
            "JOIN projects p ON p.id=a.project_id WHERE p.human_key=? "
            "AND a.name=? AND (r.read_ts IS NULL OR "
            "(m.ack_required=1 AND r.ack_ts IS NULL)) ORDER BY m.id",
            (manifest["root"], participant["display"]),
        ).fetchall()
    backlog = [str(row["id"]) for row in pending]
    ledger = issues.snapshot(directory)["issues"].values()
    backlog.extend(
        record["handoff_prompt"]["id"]
        for record in ledger
        if record.get("handoff_prompt", {}).get("holder") == name
        and not record["handoff_prompt"].get("responded_at")
    )
    backlog.extend(
        record["offer"]["id"]
        for record in ledger
        if (record.get("offer") or {}).get("to") == name
    )
    if not backlog:
        return
    wake_path = directory / f"{name}-wake.json"
    with lock(directory / f"{name}-wake.lock"):
        record = json.loads(wake_path.read_text()) if wake_path.exists() else {}
        attempts = (
            record.get("attempts", 0) if record.get("backlog") == backlog else 0
        )
        if (
            attempts >= 3
            or time.time() - record.get("at", 0) < config["inactive_after"]
        ):
            return
        result = "manual attention required"
        if observed["process_alive"]:
            result = terminal.request(directory, name)
        elif state.get("session_id") and state.get("launcher_managed"):
            entry = roster.provider(home, participant["provider"])
            if entry["adapter"] in roster.ADAPTERS and not entry.get(
                "require_env"
            ):
                with (directory / f"{name}-wake.log").open("ab") as output:
                    child = subprocess.Popen(
                        [
                            sys.executable,
                            "-m",
                            "agent_parley.cli",
                            "--home",
                            str(home),
                            "run",
                            name,
                            "--repo",
                            manifest["root"],
                            "--resume",
                            "--task",
                            terminal.PROMPT,
                        ],
                        stdin=subprocess.DEVNULL,
                        stdout=output,
                        stderr=output,
                        start_new_session=True,
                    )
                result = f"resume requested (launcher {child.pid})"
        write_json(
            wake_path,
            {
                "at": time.time(),
                "backlog": backlog,
                "attempts": attempts + (result != "busy"),
                "result": result,
            },
        )
        from agent_parley import checkpoints

        checkpoints.record(
            directory,
            name,
            {"hook_event_name": "RuntimeWake"},
            checkpoints.Reason.WAKE_REQUESTED,
            None,
            result,
        )


def run(home: Path, stopped: threading.Event) -> None:
    """Runs bounded project polls until the local service stops.

    Per-project failures are isolated from coordination calls. Observations
    become visibly stale if polling fails; they never grant ownership.
    """
    deadlines: dict[Path, float] = {}
    while not stopped.is_set():
        for path in (home / "projects").glob("*/project.json"):
            if stopped.is_set():
                return
            if time.monotonic() < deadlines.get(path, 0):
                continue
            interval = DEFAULTS["interval"]
            with contextlib.suppress(
                OSError, ValueError, BridgeError, sqlite3.Error
            ):
                manifest = roster.read(path.parent)
                interval = configuration(home, manifest)["interval"]
                poll(home, path.parent)
            deadlines[path] = time.monotonic() + interval
        stopped.wait(1)
