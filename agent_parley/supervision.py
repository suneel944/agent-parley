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
    lifecycle,
    notify,
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
    "start_deadline": 30,
    "prompts": True,
    "wake": True,
}

ACTIVE = "active"
IDLE = "idle"
STOPPED = "stopped"
STARTING = "starting; awaiting native hook"
NOT_STARTED = "not started; no native hook"
WORK_WAKE_ATTEMPTS = 3
UNKNOWN = "unknown"
DIALOG_WAKES = frozenset({"busy:input", "manual attention required"})
WORK_EVENTS = frozenset({"PreToolUse", "PostToolUse", "Stop"})
WAKE_READY = frozenset({IDLE, STOPPED})
WAKE_ATTENTION = "manual attention required"

_LAUNCHERS: list[subprocess.Popen[bytes]] = []
_LAUNCHERS_LOCK = threading.Lock()


def track_launcher(child: subprocess.Popen[bytes]) -> None:
    """Records a launcher a wake attempt started so it can be reaped.

    The service is the parent of every launcher it starts, so an exited
    launcher stays a zombie until its status is collected.

    Args:
        child: The launcher process to reap on a later sweep.
    """
    with _LAUNCHERS_LOCK:
        _LAUNCHERS.append(child)


def reap_launchers() -> int:
    """Collects the launchers that exited since the previous sweep.

    The sweep never blocks: each launcher is polled once and a running
    launcher is kept for the next sweep.

    Returns:
        The number of tracked launchers still running.
    """
    with _LAUNCHERS_LOCK:
        _LAUNCHERS[:] = [child for child in _LAUNCHERS if child.poll() is None]
        return len(_LAUNCHERS)


def settings(value: dict) -> dict:
    """Validates supervision settings stored outside the repository."""
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        raise BridgeError("Invalid supervision settings.")
    result = {**DEFAULTS, **value}
    for field in (
        "interval",
        "inactive_after",
        "stalled_after",
        "start_deadline",
    ):
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

    A lane between turns and a lane whose launcher exited are different
    situations with different remedies, so they are never given the same word.
    Collapsing them told peers and operators that a healthy lane was gone.

    Args:
        directory: Private project state directory.
        name: Participant name.
        inactive_after: Checkpoint age after which a live lane reads as idle.

    Returns:
        State, process liveness, last native activity time and observed age.
        The state is `ACTIVE` while the recorded session process is alive and
        its latest native checkpoint is no older than the threshold, `IDLE`
        once that checkpoint has aged past the threshold while the process is
        still alive, and `STOPPED` when the recorded session process is gone.
        A session with no trustworthy process identity is `UNKNOWN`, and its
        `process_alive` value is `None`; it is never inferred dead from age.
        A lane that has recorded no native activity yet reports `last_active`
        and `age_seconds` as `None` rather than an age measured from the Unix
        epoch, and reads as `ACTIVE` while its process is alive, because a
        lane that has never checked in has not been quiet for any span a
        threshold can be compared against.
    """
    path = directory / f"{name}-activity.json"
    value = json.loads(path.read_text()) if path.exists() else {}
    pid = value.get("session_pid")
    ticks = value.get("session_ticks")
    identified = (
        type(pid) is int and pid > 1 and isinstance(ticks, str) and bool(ticks)
    )
    alive = process.alive(pid, ticks) if identified else None
    recorded = value.get("updated")
    age = None if recorded is None else max(0.0, time.time() - recorded)
    if value.get("activity") == "stopped":
        state = STOPPED
    elif alive is False:
        state = STOPPED
    elif alive is None:
        state = UNKNOWN if value else STOPPED
    elif age is None or age <= inactive_after:
        state = ACTIVE
    else:
        state = IDLE
    return {
        "state": state,
        "process_alive": alive,
        "last_active": recorded,
        "age_seconds": None if age is None else int(age),
    }


def _mark_unstarted(directory: Path, name: str, deadline: float) -> bool:
    """Records that one launch produced no native hook inside its deadline.

    The launcher's own activity write is the evidence: it clears the live
    session field and publishes `STARTING` before it starts a client, so a lane
    still carrying that label owns a client that has reported nothing. Only the
    label and the observation are written. The recorded launch time, the live
    session field and the resumable session are left exactly as the launcher
    and any earlier session left them, and no native process is signalled.

    Args:
        directory: Private project state directory.
        name: Participant that owns the lane.
        deadline: Seconds a launch may take before it reads as not started.

    Returns:
        Whether this call published the mark.
    """
    from agent_parley import checkpoints

    path = directory / f"{name}-activity.json"
    started = checkpoints.activity(directory, name).get("session_started")
    if (
        not isinstance(started, (int, float))
        or isinstance(started, bool)
        or time.time() - float(started) < deadline
    ):
        return False
    with lock(directory / f"{name}-checkpoint.lock", timeout=1):
        state = checkpoints.activity(directory, name)
        if state.get("activity") != STARTING or state.get(
            "session_started"
        ) != float(started):
            return False
        state["activity"] = NOT_STARTED
        state["not_started"] = {
            "at": time.time(),
            "deadline": float(deadline),
            "waited": int(time.time() - float(started)),
        }
        write_json(path, state)
        return True


def launches(directory: Path, manifest: dict, config: dict) -> list[str]:
    """Marks the launches that never reported a native hook event.

    A client can sit on a native trust, authentication or update dialog that
    fires no hook, and the launch label alone would then describe that lane as
    starting for as long as the dialog stands. The deadline turns that silence
    into an observation with a time on it. The mark says only what was
    observed: no native hook inside `start_deadline` seconds of the launch. It
    ends nothing, moves no claim and answers no dialog, and the next native
    hook event republishes the lane's real activity, which is what clears it.

    Contention is not a failure here. A lane whose checkpoint lock is held is
    being written by its own hook, which is the outcome this pass is watching
    for, so it is left to the next poll.

    Args:
        directory: Private project state directory.
        manifest: Current participant manifest.
        config: Resolved supervision settings.

    Returns:
        The lanes this pass marked, in manifest order.
    """
    marked = []
    for name in sorted(manifest.get("participants", {})):
        with contextlib.suppress(BridgeError, OSError):
            if _mark_unstarted(directory, name, config["start_deadline"]):
                marked.append(name)
    return marked


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


def _read(root: str, *arguments: str) -> str | None:
    """Runs one read-only Git command against a checkout.

    Args:
        root: Checkout the command runs in.
        *arguments: Git arguments following the checkout selection.

    Returns:
        Standard output without surrounding whitespace, or None when Git
        refused the command, could not be run, or exceeded its timeout. An
        answer Git cannot give is no opinion rather than an empty one.
    """
    try:
        result = subprocess.run(
            ["git", "-C", root, *arguments],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode:
        return None
    return result.stdout.strip()


def _changed(root: str, start: str, end: str) -> list[str]:
    """Lists the repository-relative paths that differ between two commits.

    Args:
        root: Checkout that holds both commits.
        start: Commit the comparison starts from.
        end: Commit the comparison ends at.

    Returns:
        The changed paths, empty when nothing differs and when Git could not
        answer.
    """
    listing = _read(root, "diff", "--name-only", start, end)
    return [path for path in (listing or "").splitlines() if path]


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


def base_advances(home: Path, manifest: dict) -> dict[str, list[str]]:
    """Names the paths a lane holds that the base branch changed under it.

    A merge from another lane, or a push, moves the base branch under every
    lane that already forked from it, and none of them learns anything until
    its own merge conflicts. The head of the base checkout, which is the
    branch every lane merges back into, is read once per project and
    compared with the point each lane branched from; a lane whose fork point
    is still that head is current, and the store is not read at all when no
    lane is behind.

    Where the base did advance, the paths it changed since the fork point are
    matched against the lane's active reservations, using the same overlap
    rule a competing reservation is judged by, and against the paths the lane
    itself changed, both those committed on its branch and those still
    uncommitted in its worktree.

    The reading is advisory. Nothing rebases, pauses or reverts, and the lane
    decides what a moved base means for its work.

    Args:
        home: Private bridge state root.
        manifest: Project manifest naming the base checkout and the roster.

    Returns:
        Mapping of participant name to the sorted paths the base changed that
        the lane also holds. Empty when the base has not advanced, when the
        advance touches nothing a lane holds, or when Git could not be read.
    """
    root = manifest["root"]
    head = _read(root, "rev-parse", "HEAD")
    if not head:
        return {}
    forks = {}
    for name, participant in manifest["participants"].items():
        fork = _read(root, "merge-base", head, participant["branch"])
        if fork and fork != head:
            forks[name] = fork
    if not forks:
        return {}
    try:
        held = store.active_reservations(home, root)
    except (BridgeError, OSError, sqlite3.Error):
        held = {}
    advances: dict[str, list[str]] = {}
    for name, fork in forks.items():
        participant = manifest["participants"][name]
        branch = participant["branch"]
        changed = _changed(root, fork, head)
        if not changed:
            continue
        patterns = held.get(participant["display"], [])
        mine = set(_changed(root, fork, branch))
        mine.update(dirty_paths(participant["lane"]) or [])
        matched = sorted(
            {
                path
                for path in changed
                if path in mine
                or any(store.overlapping(path, pattern) for pattern in patterns)
            }
        )
        if matched:
            advances[name] = matched
    return advances


def base_advance_marker(paths: list[str]) -> str:
    """Describes a base branch advance in one line, naming the paths."""
    if not paths:
        return ""
    plural = "" if len(paths) == 1 else "s"
    return (
        f"base advanced over held path{plural} {', '.join(paths)} since this "
        "lane forked; nothing was rebased"
    )


FIT_CHECKS = ("session", "capacity", "worktree", "mail")
CAPACITY_STATES = frozenset({"available", "exhausted", "retryable"})
UNKNOWN_CAPACITY = {
    "state": "unknown",
    "observed_at": None,
    "reset_at": None,
    "source": "",
    "session_id": "",
    "observation_id": "",
    "participant": "",
    "progress": None,
    "progressed": False,
    "request_tokens": None,
}
UNKNOWN_FIT: dict = {
    "fit": None,
    "checks": dict.fromkeys(FIT_CHECKS),
    "failed": [],
    "reason": "",
    "capacity": dict(UNKNOWN_CAPACITY),
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
        A launch that passed its start deadline without a native hook fails
        here, which is what keeps it out of offers and out of share targets.
        A lane whose launcher published a native dialog fails the check as
        well: its client is alive and reading nothing but a keypress. A lane
        waiting on a native approval names the tool the prompt asked about.
    """
    from agent_parley import checkpoints, dialogs

    state = checkpoints.activity(directory, name)
    if not state:
        return None, ""
    if state.get("activity") == NOT_STARTED:
        waited = (state.get("not_started") or {}).get("deadline", 0)
        return False, f"it never started within {int(waited)}s of its launch"
    if not process.alive(state.get("session_pid"), state.get("session_ticks")):
        return False, "its session process is not running"
    if state.get("activity") == "stopped":
        return False, "its session ended"
    if str(state.get("activity", "")).startswith(dialogs.APPROVAL):
        tool = str((state.get("dialog") or {}).get("tool", ""))
        named = f" of {tool}" if tool else ""
        return False, f"it is waiting for a native approval{named}"
    if isinstance(state.get("dialog"), dict):
        label = str(state["dialog"].get("label", "a native dialog"))
        return False, f"its client is held by {label}"
    return True, ""


def published_capacity(directory: Path, name: str) -> dict:
    """Reads one lane's last durable provider-capacity observation."""
    try:
        value = json.loads((directory / f"{name}-capacity.json").read_text())
    except (OSError, ValueError):
        return dict(UNKNOWN_CAPACITY)
    if not isinstance(value, dict) or value.get("state") not in CAPACITY_STATES:
        return dict(UNKNOWN_CAPACITY)
    return {**UNKNOWN_CAPACITY, **value}


def record_capacity(directory: Path, name: str, observation: dict) -> dict:
    """Persists a newer validated capacity or bounded-probe outcome.

    Args:
        directory: Private project state directory.
        name: Participant whose provider produced the observation.
        observation: Validated state, evidence source, session identity and
            observation identity. A probe caller must record its own source
            and outcome rather than relying on elapsed time.

    Returns:
        The durable observation after rejecting older evidence.

    Raises:
        BridgeError: If the observation does not carry a usable state, time,
            source and evidence identity.
    """
    state = observation.get("state")
    observed_at = observation.get("observed_at")
    if (
        state not in CAPACITY_STATES
        or not isinstance(observed_at, (int, float))
        or isinstance(observed_at, bool)
        or not observation.get("source")
        or not observation.get("observation_id")
    ):
        raise BridgeError("Invalid provider capacity observation.")
    value = {**UNKNOWN_CAPACITY, **observation, "participant": name}
    path = directory / f"{name}-capacity.json"
    with lock(directory / f"{name}-capacity.lock"):
        current = published_capacity(directory, name)
        current_at = current.get("observed_at")
        if current_at is None or float(observed_at) >= float(current_at):
            write_json(path, value)
            return value
        return current


def _native_recovery(current: dict, observation: dict) -> bool:
    """Checks that a native success advances beyond exhausted evidence."""
    if (
        current["state"] not in {"exhausted", "retryable"}
        or observation["state"] != "available"
    ):
        return True
    marker = observation.get("progress")
    previous = current.get("progress")
    if observation["source"] == "claude-session-record":
        return bool(marker and marker != previous)
    if observation["source"] != "codex-session-record":
        return False
    if observation.get("session_id") != current.get("session_id"):
        request_tokens = observation.get("request_tokens")
        return (
            isinstance(request_tokens, int)
            and not isinstance(request_tokens, bool)
            and request_tokens > 0
            and observation["observed_at"] > current["observed_at"]
        )
    return (
        isinstance(marker, int)
        and isinstance(previous, int)
        and marker > previous
    ) or bool(observation.get("progressed"))


def _observe_capacity(
    home: Path, directory: Path, name: str, participant: dict
) -> dict:
    """Persists one lane's newest native capacity evidence."""
    current = published_capacity(directory, name)
    observation = records.capacity_observation(home, participant)
    if (
        observation is not None
        and observation["state"] in {"exhausted", "retryable"}
        and observation.get("progress") is None
        and observation.get("session_id") == current.get("session_id")
    ):
        observation["progress"] = current.get("progress")
    if observation is not None and _native_recovery(current, observation):
        current = record_capacity(directory, name, observation)
    reset_at = current.get("reset_at")
    if (
        current["state"] == "exhausted"
        and isinstance(reset_at, (int, float))
        and not isinstance(reset_at, bool)
        and reset_at <= time.time()
    ):
        current = record_capacity(
            directory,
            name,
            {
                **current,
                "state": "available",
                "observed_at": float(reset_at),
                "reset_at": None,
                "source": "provider-reset",
                "observation_id": f"reset:{current['observation_id']}",
            },
        )
    return current


def _account_members(home: Path, manifest: dict, name: str) -> list[str]:
    """Names lanes sharing one explicitly identified provider account."""
    participant = manifest["participants"][name]
    credential = participant.get("credential")
    if not credential:
        return [name]
    try:
        adapter = roster.provider(home, participant["provider"])["adapter"]
    except (BridgeError, KeyError):
        return [name]
    members = []
    for other, candidate in manifest["participants"].items():
        if candidate.get("credential") != credential:
            continue
        try:
            other_adapter = roster.provider(home, candidate["provider"])[
                "adapter"
            ]
        except (BridgeError, KeyError):
            continue
        if other_adapter == adapter:
            members.append(other)
    return members or [name]


def capacity(home: Path, directory: Path, manifest: dict, name: str) -> dict:
    """Reports durable lane capacity, shared only for a known account."""
    observations = [
        _observe_capacity(
            home, directory, member, manifest["participants"][member]
        )
        for member in _account_members(home, manifest, name)
    ]
    known = [item for item in observations if item["state"] != "unknown"]
    if not known:
        return dict(UNKNOWN_CAPACITY)
    ranks = {"available": 0, "retryable": 1, "exhausted": 2}
    return max(
        known,
        key=lambda item: (
            float(item.get("observed_at") or 0),
            ranks[item["state"]],
        ),
    )


def _capacity_check(observation: dict) -> tuple[bool | None, str]:
    """Converts a durable capacity state into a fit check.

    Elapsed supervision time never changes the state. Only a later successful
    request, a reliable provider reset, or a recorded bounded probe can make
    an exhausted lane available again.

    Args:
        observation: Persisted provider capacity observation.

    Returns:
        The check result and, when it failed, the recorded reason.
    """
    state = observation["state"]
    if state == "unknown":
        return None, ""
    if state == "available":
        return True, ""
    reset_at = observation.get("reset_at")
    reset = f" until {int(reset_at)}" if reset_at is not None else ""
    if state == "exhausted":
        return False, f"its provider capacity is exhausted{reset}"
    return False, "its provider reported a retryable transient failure"


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
        after: Seconds after which an unanswered item blocks an offer.

    Returns:
        Whether the lane is fit, each check's result, the names of the failed
        checks and one line naming the first failure.
    """
    participant = manifest["participants"][name]
    observed_capacity = capacity(home, directory, manifest, name)
    results = {
        "session": _session_check(directory, name),
        "capacity": _capacity_check(observed_capacity),
        "worktree": _worktree_check(participant),
        "mail": _mail_check(home, manifest, name, after),
    }
    failed = [check for check in FIT_CHECKS if results[check][0] is False]
    return {
        "fit": not failed,
        "checks": {check: results[check][0] for check in FIT_CHECKS},
        "failed": failed,
        "capacity": observed_capacity,
        "reason": (
            f"unfit ({failed[0]}): {name} {results[failed[0]][1]}"
            if failed
            else ""
        ),
    }


def stranded_claims(
    manifest: dict, ledger: dict, results: dict[str, dict]
) -> list[dict]:
    """Lists exhausted owners' claims and peers eligible for recovery.

    The candidates are observations only. They neither grant a peer ownership
    nor establish that a live owner stopped editing. Recovery must record its
    own transition and fence the prior claim generation before any transfer.

    Args:
        manifest: Project manifest holding every participant.
        ledger: Current issue ledger.
        results: Fit result per participant, including durable capacity.

    Returns:
        One candidate per unfinished owned issue. A candidate with no eligible
        peers remains in the result as a durable wait obligation.
    """
    owned = issues.holders(ledger)
    eligible = [
        name
        for name in sorted(manifest["participants"])
        if results.get(name, {}).get("fit") and not owned.get(name)
    ]
    candidates = []
    for owner, numbers in sorted(owned.items()):
        observed = results.get(owner, {}).get("capacity", UNKNOWN_CAPACITY)
        if observed.get("state") != "exhausted":
            continue
        peers = [name for name in eligible if name != owner]
        reset_at = observed.get("reset_at")
        reason = f"{owner} provider capacity is exhausted"
        if reset_at is not None:
            reason += f" until {int(reset_at)}"
        next_action = (
            "request a recorded recovery transition"
            if peers
            else "wait for provider recovery or an eligible peer"
        )
        for number in numbers:
            candidates.append(
                {
                    "issue": number,
                    "owner": owner,
                    "eligible_peers": peers,
                    "reason": reason,
                    "next_action": next_action,
                    "reset_at": reset_at,
                    "source": observed.get("source", ""),
                    "session_id": observed.get("session_id", ""),
                    "observation_id": observed.get("observation_id", ""),
                }
            )
    return candidates


def record_stranded_claims(directory: Path, candidates: list[dict]) -> None:
    """Atomically replaces the durable exhausted-claim candidates.

    Args:
        directory: Private project state directory.
        candidates: Complete current candidate snapshot from
            :func:`stranded_claims`. An empty list clears stale candidates.

    Raises:
        BridgeError: If a candidate lacks the evidence recovery must verify.
    """
    required = {
        "issue",
        "owner",
        "eligible_peers",
        "reason",
        "next_action",
        "reset_at",
        "source",
        "session_id",
        "observation_id",
    }
    for candidate in candidates:
        if (
            not isinstance(candidate, dict)
            or set(candidate) != required
            or not isinstance(candidate.get("issue"), str)
            or not candidate.get("owner")
            or not isinstance(candidate.get("eligible_peers"), list)
            or not candidate.get("source")
            or not candidate.get("session_id")
            or not candidate.get("observation_id")
        ):
            raise BridgeError("Invalid stranded capacity candidate.")
    with lock(directory / "capacity-candidates.lock"):
        write_json(
            directory / "capacity-candidates.json",
            {"version": 1, "candidates": candidates},
        )


def published_stranded_claim(directory: Path, issue: str) -> dict | None:
    """Reads the authoritative exhausted-capacity candidate for one issue.

    Args:
        directory: Private project state directory.
        issue: Issue number to find in the current candidate snapshot.

    Returns:
        The exact persisted candidate, or None when the snapshot is absent,
        malformed, stale-cleared, or does not include the issue.
    """
    try:
        document = json.loads(
            (directory / "capacity-candidates.json").read_text()
        )
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict) or document.get("version") != 1:
        return None
    candidates = document.get("candidates")
    if not isinstance(candidates, list):
        return None
    return next(
        (
            candidate
            for candidate in candidates
            if isinstance(candidate, dict) and candidate.get("issue") == issue
        ),
        None,
    )


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
    unknown = {
        **UNKNOWN_FIT,
        "checks": dict(UNKNOWN_FIT["checks"]),
        "capacity": dict(UNKNOWN_CAPACITY),
    }
    try:
        record = json.loads((directory / f"{name}-work.json").read_text())
    except (OSError, ValueError):
        return unknown
    return {**unknown, **record} if isinstance(record, dict) else unknown


def _pull_text(available: list[str], busy: list[str]) -> str:
    """Describes the work an idle lane could take from the ledger."""
    parts = ["Work offer. You hold no claim and no fit check found a blocker."]
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


def _split_text(issue: str, count: int, recipients: list[str]) -> str:
    """Describes the backlog an idle holder can split with a fit peer."""
    return (
        f"Split offer. You hold #{issue} with {count} units of remaining work "
        "recorded and no coordination event past the stall interval, so the "
        f"claim is held while nothing moves. {', '.join(recipients)} read as "
        "able to take part of it now. Split the backlog and send one part "
        "with send_message and ack_required, or hand the whole claim over "
        "with agent-parley issue offer. You decide what moves, and nothing "
        "moves until a recipient answers."
    )


def _continue_text(ledger: dict, numbers: list[str]) -> str:
    """Describes already-owned work that remains authorized to continue."""
    actions = ", ".join(
        f"#{number} ({lifecycle.describe_action(ledger['issues'][number])})"
        for number in numbers[:5]
    )
    return (
        f"Continue authorized work already assigned to you: {actions}. "
        "Resume the current claim generation; delivery does not mark progress."
    )


def _work_bindings(ledger: dict, numbers: list[str]) -> list[dict]:
    """Captures the issue state that makes a selected offer actionable."""
    selected = []
    for number in numbers:
        record = ledger["issues"].get(number, {})
        selected.append(
            {
                "issue": number,
                "owner": record.get("owner"),
                "claim_id": record.get("claim_id"),
                "blocked_by": record.get("blocked_by", []),
                "offer": (record.get("offer") or {}).get("id"),
                "execution": record.get("execution"),
            }
        )
    return selected


def _work_progress(ledger: dict, numbers: list[str]) -> str:
    """Fingerprints issue-scoped changes that count as offer progress.

    Args:
        ledger: Current issue ledger.
        numbers: Issues selected for this work offer.

    Returns:
        Stable digest of ownership, dependencies, handoffs and execution state.
    """
    encoded = json.dumps(
        _work_bindings(ledger, numbers),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode()).hexdigest()[:16]


def _work_dispatch(previous: dict, offer: dict) -> dict:
    """Keeps one dispatch obligation until work changes or disappears.

    Args:
        previous: Last work publication for this lane.
        offer: Newly derived actionable offer.

    Returns:
        Existing dispatch state for unchanged work, or a fresh generation.
    """
    prior_offer = previous.get("offer") or {}
    prior = previous.get("dispatch") or {}
    if (
        prior_offer.get("id") == offer["id"]
        and prior_offer.get("progress") == offer["progress"]
        and isinstance(prior, dict)
        and prior
    ):
        return prior
    return {
        "generation": offer["id"],
        "progress": offer["progress"],
        "state": "pending",
        "attempts": 0,
        "last_result": "",
        "updated_at": time.time(),
    }


def share_recipients(
    home: Path,
    directory: Path,
    manifest: dict,
    results: dict[str, dict],
    stretches: dict[str, int],
    owned: dict[str, list[str]],
    ledger: dict,
    config: dict,
) -> list[str]:
    """Lists the lanes that could act on a share of somebody else's work.

    A lane qualifies only when both readings the runtime already has agree: the
    fit check every work offer uses, and the share conditions a returned share
    is judged by. It must also hold no claim of its own and have been idle past
    the stall interval, which is the same threshold that makes a lane a
    rebalance target.

    Args:
        home: Private bridge state root.
        directory: Private project state directory.
        manifest: Current participant manifest.
        results: Current fit result for every participant.
        stretches: Current idle duration for every participant.
        owned: Issue numbers grouped by owner.
        ledger: Current issue ledger.
        config: Resolved supervision settings.

    Returns:
        Qualifying participants in name order. The reading is derived on every
        sweep, so a lane that parks on a dialog or takes a claim of its own
        stops qualifying without anything being cleared.
    """
    return [
        name
        for name in sorted(manifest["participants"])
        if results.get(name, {}).get("fit")
        and not owned.get(name)
        and stretches.get(name, 0) >= config["stalled_after"]
        and not share_blocker(
            home,
            directory,
            manifest,
            name,
            presence(directory, name, config["inactive_after"]),
            ledger,
        )
    ]


def _work_offer(
    name: str,
    results: dict[str, dict],
    stretches: dict[str, int],
    owned: dict[str, list[str]],
    available: list[str],
    ledger: dict,
    after: float,
    recipients: list[str],
) -> dict | None:
    """Derives the current offer for one lane from live eligibility inputs.

    A lane that holds a claim carrying a countable backlog and has recorded no
    coordination event for the stall interval is offered a split of that
    backlog, because a held claim with remaining work and an idle holder moves
    nothing until someone says so. The split is offered only while a recipient
    could act on it and while this lane's own capacity check has not failed:
    the holder still has to take the turn that sends the share, and an
    exhausted owner belongs to recovery instead. A lane holding more than one
    claim is told to shed a whole claim first, which needs no split.

    Args:
        name: Participant receiving the offer.
        results: Current fit result for every participant.
        stretches: Current idle duration for every participant.
        owned: Issue numbers grouped by owner.
        available: Current unclaimed and unblocked issue numbers.
        ledger: Current issue ledger.
        after: Minimum idle duration for a rebalance target.
        recipients: Participants that read as able to act on a share now.

    Returns:
        The actionable offer, or None when no work is currently eligible.
    """
    held = owned.get(name, [])
    continuation = lifecycle.actionable(ledger, name)
    busy_work = {
        holder: lifecycle.actionable(ledger, holder) for holder in sorted(owned)
    }
    busy = sorted(
        holder for holder, claims in busy_work.items() if len(claims) > 1
    )
    idle = [
        peer
        for peer in sorted(results)
        if results[peer]["fit"]
        and not owned.get(peer)
        and stretches[peer] >= after
    ]
    offer = None
    capacity = results[name]["checks"].get("capacity")
    if len(continuation) > 1 and (
        peers := [peer for peer in idle if peer != name]
    ):
        selected = continuation[:5]
        offer = {
            "kind": "rebalance",
            "issues": selected,
            "text": _rebalance_text(held, peers),
            "progress": _work_progress(ledger, selected),
        }
    elif (
        capacity is not False
        and stretches[name] >= after
        and (able := [peer for peer in recipients if peer != name])
        and (
            loaded := next(
                (
                    number
                    for number in continuation
                    if lifecycle.backlog(ledger["issues"].get(number) or {})
                ),
                "",
            )
        )
    ):
        offer = {
            "kind": "split",
            "issues": [loaded],
            "text": _split_text(
                loaded,
                lifecycle.backlog(ledger["issues"][loaded]),
                able,
            ),
            "progress": _work_progress(ledger, [loaded]),
        }
    elif continuation and capacity is not False:
        selected = continuation[:5]
        offer = {
            "kind": "continue",
            "issues": selected,
            "text": _continue_text(ledger, selected),
            "progress": _work_progress(ledger, selected),
        }
    elif results[name]["fit"] and not held and (available or busy):
        selected = available[:5]
        for holder in busy:
            selected.extend(
                number for number in busy_work[holder] if number not in selected
            )
            selected = selected[:5]
        offer = {
            "kind": "pull",
            "issues": selected,
            "text": _pull_text(available, busy),
            "progress": _work_progress(ledger, selected),
        }
    if offer:
        offer["id"] = hashlib.sha256(
            f"{offer['kind']}\x00{offer['text']}".encode()
        ).hexdigest()[:16]
    return offer


def work(home: Path, directory: Path, manifest: dict, config: dict) -> None:
    """Publishes each lane's fit result and any advisory work offer.

    A lane holding no claim is offered the unclaimed work and told which peers
    hold more than one claim. A lane holding more than one claim is told which
    fit peers have been idle past the stall interval. A lane holding one claim
    that carries a countable backlog, and that has itself been idle past that
    interval, is offered a split of that backlog with a peer that reads as able
    to act on a share. Offers are advisory and never claim work. Separately
    approved recovery may stop an exhausted owner and preserve its work before
    a peer explicitly takes its claim.

    A recipient must pass both readings the runtime already has: the fit check
    every offer uses, and the share conditions a returned share is judged by,
    so no lane is offered a split with a peer that could not answer it.

    An offer carries a digest of its own content as its identifier, so a lane
    whose situation has not changed sees the same offer rather than a new one
    on every poll.

    Args:
        home: Private bridge state root.
        directory: Private project state directory.
        manifest: Current participant manifest.
        config: Resolved supervision settings.
    """
    from agent_parley import recovery

    after = config["stalled_after"]
    ledger = issues.snapshot(directory)
    owned = issues.holders(ledger)
    available = lifecycle.actionable(ledger)
    results = {
        name: fit(home, directory, manifest, name, after)
        for name in manifest["participants"]
    }
    record_stranded_claims(
        directory, stranded_claims(manifest, ledger, results)
    )
    recovered = recovery.quiesce_authorized(directory, manifest)
    if recovered:
        ledger = issues.snapshot(directory)
        for number, record in ledger["issues"].items():
            marker = record.get("orphan")
            if marker in recovered:
                _announce_orphan(
                    home,
                    manifest,
                    str(record["owner"]),
                    [number],
                    list(marker.get("reservations") or []),
                )
        owned = issues.holders(ledger)
        available = lifecycle.actionable(ledger)
        results = {
            name: fit(home, directory, manifest, name, after)
            for name in manifest["participants"]
        }
        record_stranded_claims(
            directory, stranded_claims(manifest, ledger, results)
        )
    stretches = {
        name: idle_seconds(directory, name)
        for name in sorted(manifest["participants"])
    }
    recipients = share_recipients(
        home, directory, manifest, results, stretches, owned, ledger, config
    )
    for name in manifest["participants"]:
        result = results[name]
        offer = _work_offer(
            name,
            results,
            stretches,
            owned,
            available,
            ledger,
            after,
            recipients,
        )
        with lock(directory / f"{name}-work.lock", timeout=1):
            previous = published_work(directory, name)
            published = {**result, "offer": offer}
            if offer:
                published["dispatch"] = _work_dispatch(previous, offer)
            path = directory / f"{name}-work.json"
            if published != previous:
                write_json(path, published)
    idle = [
        name
        for name in sorted(manifest["participants"])
        if results[name]["fit"]
        and not owned.get(name)
        and stretches[name] >= after
    ]
    announce_idle(directory, manifest, idle, stretches, after)


def announce_idle(
    directory: Path,
    manifest: dict,
    idle: list[str],
    stretches: dict[str, int],
    after: float,
) -> list[str]:
    """Notifies the owner about each lane idle past the grace period.

    The idle stretch has no native event of its own, so the sweep that
    already measured it is what reports it. The notifier keys the report on
    the lane's last recorded activity, so a lane idle across many sweeps is
    reported once and reported again only after it next checks in. A
    misconfigured transport is recorded as a supervision error rather than
    ending the sweep.

    Args:
        directory: Private project state directory.
        manifest: Current participant manifest.
        idle: Lanes measured as fit, unclaimed and idle past the interval.
        stretches: Measured idle seconds per lane.
        after: Grace period the lanes were measured against.

    Returns:
        The lanes a notification was started for.
    """
    if not idle or not notify.enabled():
        return []
    started = []
    try:
        for name in idle:
            sent = notify.deliver(
                directory,
                name,
                notify.Event.LANE_IDLE,
                {
                    "repo": manifest["root"],
                    "provider": str(
                        manifest["participants"][name].get("provider", "")
                    ),
                    "since": str(
                        presence(directory, name, after)["last_active"]
                    ),
                    "detail": (
                        f"idle {stretches[name]}s with no claim, past the "
                        f"{int(after)}s grace period"
                    ),
                },
            )
            if sent:
                started.append(name)
    except (BridgeError, OSError) as exc:
        issues.note_supervision_error(directory, f"Notification: {exc}")
    return started


def configuration(home: Path, manifest: dict) -> dict:
    """Resolves project settings while honoring the global wake opt-out.

    A project's supervision block also carries lane-facing choices the
    supervisor has no threshold for, such as the answers recorded for native
    dialogs and the native approval opt-in. The roster validates those where it
    reads the manifest, so only the supervisor's own fields are resolved here
    rather than refusing a manifest that records one of them.
    """
    path = home / "supervision.json"
    global_config = settings(
        json.loads(path.read_text()) if path.exists() else {}
    )
    project = {
        field: value
        for field, value in (manifest.get("supervision") or {}).items()
        if field in DEFAULTS
    }
    config = settings({**global_config, **project})
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


def dialog_waiting(directory: Path, name: str) -> bool:
    """Reports whether the lane's own state reads as a dialog on its screen.

    The lane's last wake outcome is the only local reading that distinguishes
    a session waiting for the operator from one working, so it is the
    predicate every caller uses. A screen watcher that records a dialog
    through the same lane state is read here without further change.

    Args:
        directory: Private project state directory.
        name: Participant that owns the lane.

    Returns:
        Whether the lane last read as waiting for operator input. A lane with
        no recorded wake outcome reads as not waiting, because an absent
        record is no evidence of a dialog.
    """
    try:
        wake = json.loads((directory / f"{name}-wake.json").read_text())
    except (OSError, ValueError):
        return False
    return isinstance(wake, dict) and wake.get("result") in DIALOG_WAKES


def unacknowledged_reason(directory: Path, name: str, observed: dict) -> str:
    """States in one clause why a lane did not acknowledge in time.

    Only what the runtime can read locally is reported: the recorded session
    process, the last wake outcome, the lane's durable provider capacity and
    the measured idle stretch. Nothing here is inferred from the silence
    itself, so a lane that reads as available is reported as exactly that.

    Args:
        directory: Private project state directory.
        name: Participant that owes the acknowledgement.
        observed: That lane's presence reading.

    Returns:
        One clause naming the condition, worded to follow the lane's name.
    """
    if observed.get("state") == STOPPED:
        return "has no running session"
    if dialog_waiting(directory, name):
        return "has a native dialog waiting for the operator"
    state = published_capacity(directory, name)["state"]
    if state == "exhausted":
        return "has exhausted its provider capacity"
    if state == "retryable":
        return "hit a retryable provider failure"
    if observed.get("state") == UNKNOWN:
        return "has no trustworthy session identity"
    if observed.get("state") == IDLE:
        return f"has been idle for {int(observed.get('age_seconds') or 0)}s"
    return "was live and did not answer"


def acknowledgement_deadlines(
    home: Path, directory: Path, manifest: dict, observations: dict
) -> None:
    """Returns each missed acknowledgement deadline to the lane that sent it.

    An acknowledgement nobody answers is otherwise permanent: it stays on the
    problem list of a lane that may never read mail again and its sender is
    never told. Past the deadline the sender receives one message naming every
    recipient that did not acknowledge and what the runtime could read about
    why, and the expectation is retired so the row clears. The notice is
    deduplicated by the message it reports, so a sweep that repeats writes
    nothing further.

    A sender that holds no inbox, such as the supervising operator, is not
    mailed; the expectation is still retired, because a permanent row is the
    condition this removes.

    Args:
        home: Private bridge state root.
        directory: Private project state directory.
        manifest: Current participant manifest.
        observations: Presence reading per participant from this sweep.
    """
    named = {
        entry["display"]: name
        for name, entry in manifest["participants"].items()
    }
    with contextlib.suppress(BridgeError, OSError, sqlite3.Error):
        for breach in store.overdue_acknowledgements(home, manifest["root"]):
            silent = []
            for display in breach["recipients"]:
                observed = observations.get(named.get(display, display))
                reason = (
                    unacknowledged_reason(directory, named[display], observed)
                    if observed
                    else "is not a participant in this project"
                )
                silent.append(f"- {display} {reason}")
            body = (
                f"Message {breach['message_id']} ({breach['subject']}) "
                "required an acknowledgement and its deadline passed "
                f"{breach['overdue_seconds']}s ago. These recipients did not "
                "acknowledge it:\n"
                + "\n".join(silent)
                + "\nThe expectation is retired. Send it again, ask the "
                "operator to acknowledge it, or continue without it."
            )
            with contextlib.suppress(BridgeError):
                store.speak(
                    home,
                    manifest["root"],
                    breach["sender"],
                    "Acknowledgement deadline passed: message "
                    f"{breach['message_id']}",
                    body,
                    f"ack-deadline-{breach['message_id']}",
                )
            store.retire_acknowledgement(
                home, manifest["root"], breach["message_id"]
            )


def share_blocker(
    home: Path,
    directory: Path,
    manifest: dict,
    name: str,
    observed: dict,
    ledger: dict,
) -> str:
    """States in one clause why a lane cannot act on a share right now.

    Only conditions the runtime can read locally count: the recorded session
    process, the lane's own dialog state, its durable provider capacity and
    the dependencies of the claims it already holds. Silence is not one of
    them, so a live lane that has simply not answered yet is never reported
    as unable to act; that case belongs to the acknowledgement deadline.

    Args:
        home: Private bridge state root.
        directory: Private project state directory.
        manifest: Project manifest holding this participant.
        name: Participant the share was addressed to.
        observed: That lane's presence reading.
        ledger: Current issue ledger.

    Returns:
        One clause naming the condition, worded to follow the lane's name, or
        an empty string when nothing local says the lane cannot act.
    """
    if observed.get("state") == STOPPED:
        return "has no live session process"
    if dialog_waiting(directory, name):
        return "has a native dialog waiting for the operator"
    if capacity(home, directory, manifest, name)["state"] == "exhausted":
        return "has exhausted its provider capacity"
    blocked = sorted(
        (
            number
            for number, record in ledger.get("issues", {}).items()
            if record.get("owner") == name
            and record.get("blocked_by")
            and not lifecycle.dependencies_complete(ledger, record)
        ),
        key=int,
    )
    if blocked:
        held = ledger["issues"][blocked[0]]
        waiting = ", ".join(f"#{number}" for number in held["blocked_by"])
        return f"holds #{blocked[0]}, itself blocked by {waiting}"
    return ""


def bounced_shares(
    home: Path, directory: Path, manifest: dict, observations: dict
) -> list[dict]:
    """Lists the shares whose recipients cannot act on them.

    A share here is an acknowledgement request still inside its deadline: the
    sender is waiting for an answer that decides whether the work moves. A
    recipient that reads as unable to act will not produce that answer, so
    the share is reported bounced while the sender can still keep the work
    and offer it elsewhere.

    The reading is derived, never stored: a recipient that becomes fit, or
    acknowledges, stops appearing without anything being cleared. Nothing
    here withdraws a share, moves ownership or deletes mail.

    Args:
        home: Private bridge state root.
        directory: Private project state directory.
        manifest: Current participant manifest.
        observations: Presence reading per participant.

    Only a share a lane sent is reported. The supervising operator is at a
    terminal and already reads the request as awaiting acknowledgement, so
    returning it would add a second row for a condition that is on screen.

    Returns:
        One entry per share, oldest first, naming the message, its subject,
        its sender as an identity and as a participant, how long it has
        waited, and one blocked entry per recipient that cannot act, carrying
        that recipient and the reason. A store that cannot be read yields what
        was read before it failed.
    """
    named = {
        entry["display"]: name
        for name, entry in manifest["participants"].items()
    }
    found: list[dict] = []
    ledger = issues.snapshot(directory)
    with contextlib.suppress(BridgeError, OSError, sqlite3.Error):
        for share in store.pending_acknowledgements(home, manifest["root"]):
            if share["sender"] not in named:
                continue
            blocked = []
            for display in share["recipients"]:
                lane = named.get(display)
                observed = observations.get(lane) if lane else None
                if lane is None or observed is None:
                    continue
                reason = share_blocker(
                    home, directory, manifest, lane, observed, ledger
                )
                if reason:
                    blocked.append(
                        {
                            "lane": lane,
                            "recipient": display,
                            "reason": reason,
                        }
                    )
            if blocked:
                found.append(
                    {
                        "message_id": share["message_id"],
                        "subject": share["subject"],
                        "sender": share["sender"],
                        "sender_lane": named[share["sender"]],
                        "waiting_seconds": share["waiting_seconds"],
                        "blocked": blocked,
                    }
                )
    return found


def share_bounces(
    home: Path, directory: Path, manifest: dict, observations: dict
) -> None:
    """Returns a share no recipient can act on to the lane that sent it.

    Delivery into a mailbox is not receipt. A share addressed to a lane with
    no live session, a dialog on its screen, exhausted provider capacity or a
    blocked claim of its own is answered by nobody, and until now the sender
    learned that only when the acknowledgement deadline passed, or never. The
    sweep returns it as soon as the condition is readable, naming each
    recipient and its reason, so the sender keeps the work and can offer it to
    a lane that reads as fit.

    The notice is deduplicated by the share it reports, so a repeating sweep
    writes nothing further, and it is ordinary mail: the sender's own backlog
    carries it, which is what stops that lane waiting quietly on an answer
    that is not coming. The acknowledgement expectation is left in force, so a
    recipient that recovers can still answer and the deadline path stays the
    one place an expectation is retired.

    A share the supervising operator sent is not returned: that operator is at
    a terminal and reads the request as awaiting acknowledgement already.

    Args:
        home: Private bridge state root.
        directory: Private project state directory.
        manifest: Current participant manifest.
        observations: Presence reading per participant from this sweep.
    """
    for share in bounced_shares(home, directory, manifest, observations):
        listed = "\n".join(
            f"- {entry['recipient']} {entry['reason']}"
            for entry in share["blocked"]
        )
        body = (
            f"Message {share['message_id']} ({share['subject']}) asked for an "
            "acknowledgement that these recipients cannot give:\n"
            f"{listed}\n"
            "The share is returned to you. You keep the work: offer it to a "
            "lane that reads as fit with agent-parley participant status, or "
            "hold it yourself. Nothing was withdrawn, no ownership moved, and "
            "the acknowledgement still stands until its deadline."
        )
        with contextlib.suppress(BridgeError, OSError, sqlite3.Error):
            store.speak(
                home,
                manifest["root"],
                share["sender"],
                f"Share returned: message {share['message_id']}",
                body,
                f"share-bounce-{share['message_id']}",
            )


def orphan_reason(name: str, observed: dict) -> str:
    """States why a lane's claims read as orphaned, in one clause."""
    return (
        f"{name} has no running session process and has been silent for "
        f"{int(observed['age_seconds'] or 0)}s"
    )


def orphan_marker(numbers: list[str], keys: list[str]) -> str:
    """Describes one lane's orphaned claims in one line, naming its keys."""
    if not numbers:
        return ""
    listed = ", ".join(f"#{number}" for number in numbers)
    held = f"; holds {', '.join(keys)}" if keys else ""
    return (
        f"orphaned claims {listed}{held}; still owned until a peer runs "
        "issue claim --take-orphaned"
    )


def _dead(observed: dict, after: float) -> bool:
    """Reports whether a lane is gone rather than merely quiet.

    Args:
        observed: Presence reading for the lane.
        after: Seconds of silence the project counts as a stall.

    Returns:
        Whether the recorded session process is gone and the lane has been
        silent for longer than that threshold. A live lane is never dead
        however long it has been idle, and a lane that recorded no activity
        at all has no age to measure, so it is left alone.
    """
    return (
        observed["process_alive"] is False
        and observed["age_seconds"] is not None
        and observed["age_seconds"] >= after
    )


def orphans(home: Path, directory: Path, manifest: dict, config: dict) -> None:
    """Marks a dead lane's claims as orphaned and tells every other lane once.

    A crashed lane keeps its issues, and peers that wait on them cannot tell a
    working owner from one that will never answer. The marker states that
    observation where the ledger is read, and one notice per dead lane names
    the orphaned issues and the reservations it still holds.

    Nothing moves here. The issue keeps its owner, the reservations keep their
    holder, and only an explicit ``issue claim --take-orphaned`` by a peer
    transfers either. A lane that is merely idle is never marked, and a lane
    that returns keeps nothing but the right to claim its work again.

    Args:
        home: Private bridge state root.
        directory: Private project state directory.
        manifest: Current participant manifest.
        config: Resolved supervision settings.
    """
    after = config["stalled_after"]
    dead = {
        name: observed
        for name in manifest["participants"]
        if _dead(
            observed := presence(directory, name, config["inactive_after"]),
            after,
        )
    }
    if not dead:
        return
    from agent_parley import recovery

    recoverable = set()
    for name in dead:
        try:
            recovery.capture(directory, manifest, name)
            recoverable.add(name)
        except (BridgeError, OSError, ValueError):
            pass
    try:
        reservations = store.active_reservations(home, manifest["root"])
    except (BridgeError, OSError, sqlite3.Error):
        reservations = {}
    notices = []
    with lock(directory / "issues.lock", timeout=1):
        ledger = issues.snapshot(directory)
        changed = False
        for name, observed in dead.items():
            if name not in recoverable:
                continue
            keys = reservations.get(
                manifest["participants"][name]["display"], []
            )
            reason = orphan_reason(name, observed)
            marked = []
            fresh = False
            for number, record in ledger["issues"].items():
                if record.get("owner") != name:
                    continue
                current = record.get("orphan") or {}
                if (
                    current.get("owner") == name
                    and current.get("claim_id") == record.get("claim_id")
                    and current.get("authorization")
                    and current.get("checkpoint")
                ):
                    marked.append(number)
                    continue
                identifier = (
                    f"{name}:{record.get('claim_id') or number}:"
                    f"{int(observed['last_active'] or 0)}"
                )
                marked.append(number)
                if record.get("orphan", {}).get("id") == identifier:
                    continue
                record["orphan"] = {
                    "id": identifier,
                    "owner": name,
                    "reason": reason,
                    "reservations": list(keys),
                    "created": time.time(),
                }
                changed = True
                fresh = True
            if fresh:
                notices.append((name, sorted(marked, key=int), list(keys)))
        if changed:
            ledger["revision"] += 1
            write_json(directory / "issues.json", ledger)
    for name, marked, keys in notices:
        _announce_orphan(home, manifest, name, marked, keys)


def _announce_orphan(
    home: Path, manifest: dict, name: str, numbers: list[str], keys: list[str]
) -> None:
    """Tells every other lane once that one lane's claims read as orphaned.

    Args:
        home: Private bridge state root.
        manifest: Current participant manifest.
        name: Participant whose claims were marked.
        numbers: Issue numbers marked orphaned, in ledger order.
        keys: Reservation keys that lane still holds.
    """
    listed = ", ".join(f"#{number}" for number in numbers)
    held = ", ".join(keys) or "none"
    body = (
        f"{name} reads as orphaned: no running session process. Orphaned "
        f"claims: {listed}. Reservations it still holds: {held}. Nothing has "
        "moved: take one with agent-parley issue claim NUMBER "
        "--take-orphaned, which records you as the owner and releases those "
        "reservations. Leaving it alone keeps it with " + name + "."
    )
    for peer, participant in manifest["participants"].items():
        if peer == name:
            continue
        digest = hashlib.sha256(
            f"{name}\x00{listed}\x00{held}\x00{peer}".encode()
        ).hexdigest()[:32]
        with contextlib.suppress(BridgeError, OSError, sqlite3.Error):
            store.speak(
                home,
                manifest["root"],
                participant["display"],
                f"Orphaned claims held by {name}",
                body,
                f"orphan:{digest}",
            )


def claimed_since(record: dict) -> float:
    """Reports when the current ownership generation of an issue began.

    A lane branch is reused across claims, so a pull request that ended before
    the current claim started describes earlier work and must not report that
    claim as finished. The generation starts at the most recent claim or
    accepted handoff; a record whose history no longer names one is treated as
    having always been owned, which preserves the previous observation. A take
    of an orphaned claim starts a generation like any other claim.

    Args:
        record: Published ledger record for one issue.

    Returns:
        Unix time the current ownership generation began, or zero.
    """
    return max(
        (
            float(entry.get("at", 0) or 0)
            for entry in record.get("history", [])
            if entry.get("action") in {"claim", "accept", "take"}
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

    Launches are judged against their start deadline first, so a lane whose
    client never reported a native hook is published as not started before this
    same poll reads presence, publishes fitness and considers a wake.
    """
    manifest = roster.read(directory)
    config = configuration(home, manifest)
    launches(directory, manifest, config)
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
                    observed["process_alive"] is True,
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
        acknowledgement_deadlines(home, directory, manifest, observations)
        share_bounces(home, directory, manifest, observations)
        orphans(home, directory, manifest, config)
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


def _work_backlog(
    home: Path,
    directory: Path,
    manifest: dict,
    name: str,
    config: dict,
    record: dict,
) -> tuple[str, dict] | None:
    """Builds a wake key for one still-actionable published work offer.

    Args:
        home: Private bridge state root.
        directory: Private project state directory.
        manifest: Current participant manifest.
        name: Participant receiving the offer.
        config: Resolved supervision settings.
        record: Lane work publication.

    Returns:
        Backlog key and offer, or None when no actionable offer is published.
    """
    offer = record.get("offer") or {}
    dispatch = record.get("dispatch") or {}
    if not config["prompts"] or not offer.get("id") or not dispatch:
        return None
    ledger = issues.snapshot(directory)
    owned = issues.holders(ledger)
    results = {
        peer: fit(home, directory, manifest, peer, config["stalled_after"])
        for peer in manifest["participants"]
    }
    stretches = {
        peer: idle_seconds(directory, peer) for peer in manifest["participants"]
    }
    current = _work_offer(
        name,
        results,
        stretches,
        owned,
        lifecycle.actionable(ledger),
        ledger,
        config["stalled_after"],
        share_recipients(
            home,
            directory,
            manifest,
            results,
            stretches,
            owned,
            ledger,
            config,
        ),
    )
    if not current or (
        current.get("id"),
        current.get("progress"),
    ) != (offer.get("id"), offer.get("progress")):
        return None
    return (
        f"work:{current['id']}:{current.get('progress', current['id'])}",
        current,
    )


def _lane_activity(
    home: Path, directory: Path, manifest: dict, name: str
) -> dict:
    """Marks what the lane itself did, for comparison across wake attempts.

    A lane can work for far longer than one wake window without changing the
    state of the issue it was offered, so an offer's progress digest cannot
    separate a busy lane from a silent one. Every signal read here is work the
    lane performed: the newest tool or turn-end event it recorded, and the last
    message it sent. A report, a commit on the claim branch and a message are
    all tool calls, so the event log already carries them; the store reading is
    kept beside it because a lane whose hooks are not installed still leaves
    the messages it sent. Nothing here starts a process: the wake path must not
    make a lane's own activity depend on a Git call that can time out.

    Only `WORK_EVENTS` count. Session starts, prompt submissions, permission
    requests, wake records and notification failures are produced by the
    runtime, by a resumed launcher or by the very dialog an operator is being
    told about, so counting them would make a stuck lane look busy and an
    escalation unreachable.

    Each reading is best effort and contributes nothing when it cannot be
    taken. An event log held by maintenance and a store that cannot be opened
    must neither fail the wake nor invent activity the lane did not produce.

    Args:
        home: Private bridge state root.
        directory: Private project state directory.
        manifest: Current participant manifest.
        name: Participant that owns the lane.

    Returns:
        Comparable activity marker for the lane, whose values change only when
        the lane acts.
    """
    from agent_parley import checkpoints

    participant = manifest.get("participants", {}).get(name) or {}
    marker: dict = {"events": 0.0, "message": 0}
    entries: list[dict] = []
    with contextlib.suppress(BridgeError, OSError):
        entries = checkpoints.read_events(directory, name)
    marker["events"] = max(
        (
            float(entry["ts"])
            for entry in entries
            if str(entry.get("event", "")) in WORK_EVENTS
            and type(entry.get("ts")) in (int, float)
        ),
        default=0.0,
    )
    display = participant.get("display", "")
    if display:
        with contextlib.suppress(BridgeError, OSError, sqlite3.Error):
            with store.connect(home) as db:
                row = db.execute(
                    "SELECT MAX(m.id) AS sent FROM messages m "
                    "JOIN agents a ON a.id=m.sender_id "
                    "JOIN projects p ON p.id=a.project_id "
                    "WHERE p.human_key=? AND a.name=?",
                    (manifest.get("root", ""), display),
                ).fetchone()
            marker["message"] = int((row["sent"] if row else 0) or 0)
    return marker


def _work_escalation(offer: dict, attempts: int, last_result: str) -> str:
    """Names abandoned work, its last refusal and the operator remedy."""
    named = ", ".join(f"#{number}" for number in offer.get("issues", []))
    subject = f"work offer {offer['id']}"
    if named:
        subject += f" for {named}"
    return (
        f"manual attention required: {subject} recorded no lane activity "
        f"across {attempts} wake attempts; last result: "
        f"{last_result or 'unknown'}; next action: inspect the lane, resolve "
        "the refusal, then claim or hand off one named issue"
    )


def _write_work_dispatch(
    directory: Path,
    name: str,
    offer: dict,
    attempts: int,
    result: str,
    state: str,
) -> None:
    """Persists a dispatch outcome only for the current offer generation.

    Args:
        directory: Private project state directory.
        name: Participant owning the offer.
        offer: Offer observed before dispatch.
        attempts: Counted attempts for this generation and progress digest.
        result: Last launcher result or escalation reason.
        state: Pending outcome state.
    """
    path = directory / f"{name}-work.json"
    with lock(directory / f"{name}-work.lock", timeout=1):
        current = published_work(directory, name)
        published = current.get("offer") or {}
        if published.get("id") != offer.get("id") or published.get(
            "progress"
        ) != offer.get("progress"):
            return
        dispatch = dict(current.get("dispatch") or {})
        dispatch.update(
            state=state,
            attempts=attempts,
            last_result=result,
            updated_at=time.time(),
        )
        current["dispatch"] = dispatch
        write_json(path, current)


def _wake_flags(home: Path, directory: Path, name: str) -> dict:
    """Reads the persisted wake gates used to fence prompt admission."""
    with lock(directory / "setup.lock"):
        manifest = roster.read(directory)
        participants = manifest.get("participants", {})
        participant = participants.get(name) or {}
        config = configuration(home, manifest)
        return {
            "present": name in participants,
            "enabled": bool(config["wake"]),
            "participant_wake": bool(participant.get("wake", True)),
            "paused": bool(participant.get("paused", False)),
        }


def _select_work_prompt(
    home: Path, directory: Path, name: str, offer: dict | None
) -> bool:
    """Publishes a prompt fenced to current lane and issue state."""
    path = directory / f"{name}-wake-work.json"
    flags = _wake_flags(home, directory, name)
    if (
        not flags["present"]
        or not flags["enabled"]
        or not flags["participant_wake"]
        or flags["paused"]
    ):
        path.unlink(missing_ok=True)
        return False
    bindings = []
    if offer:
        with lock(directory / "issues.lock", timeout=1):
            ledger = issues.snapshot(directory)
            bindings = _work_bindings(ledger, offer.get("issues", []))
            if _work_progress(ledger, offer.get("issues", [])) != offer.get(
                "progress"
            ):
                path.unlink(missing_ok=True)
                return False
    if _wake_flags(home, directory, name) != flags:
        path.unlink(missing_ok=True)
        return False
    write_json(
        path,
        {
            "offer": offer,
            "bindings": bindings,
            "flags": flags,
            "selected_at": time.time(),
        },
    )
    return True


def _wake_due(
    at: float, attempts: int, ready_at: float, window: float
) -> float:
    """Times the next wake attempt after the recorded one.

    The delay doubles with each attempt already spent, so a lane that answered
    nothing is asked again less often instead of being asked on every poll, and
    a cause that names its own clearing time postpones the attempt until then.

    Args:
        at: Time of the last recorded attempt.
        attempts: Attempts already spent against the current backlog.
        ready_at: Earliest time the blocking cause can clear, 0 when unknown.
        window: Inactivity window the attempts are spaced by.

    Returns:
        Unix time from which the next attempt may be made.
    """
    delay = window * 2 ** max(attempts - 1, 0)
    return max(at + delay, ready_at)


def _wake_block(
    directory: Path, name: str, state: dict, observed: dict, record: dict
) -> tuple[str, float]:
    """Names what stops a wake attempt now and when it could clear.

    A wake is re-decided on every poll rather than once, so an attempt is spent
    only on a lane that could answer it. Capacity is read from the durable
    observation, never from elapsed time, and blocks only while the provider
    named a reset still ahead, because the attempt after that reset is itself
    the evidence that the lane is back. A lane whose screen state is not idle
    or stopped while its recorded process runs is working or parked on a native
    dialog it owns, and the dialog watcher publishes into the same activity
    state, so answering the dialog clears this cause with no change here. A
    lane with no running process and no session to resume is blocked only once
    its first attempt has already recorded that, so the refusal is reported
    before the cause starts sparing the budget.

    A launch marked not started is blocked before any of that. It has no session
    to resume and no client that could read an injected prompt, so it is a lane
    with no session however alive its launcher still is, and nothing a wake can
    do reaches the dialog that is holding it. The operator answering that dialog
    produces a native hook event, which republishes the activity and clears the
    cause here with no change of its own.

    Args:
        directory: Private project state directory.
        name: Participant that owns the lane.
        state: The lane's published activity state.
        observed: The lane's presence reading.
        record: The lane's last durable wake record.

    Returns:
        The blocking cause, empty when an attempt can be made, and the earliest
        time that cause can clear, 0 when only a later observation clears it.
    """
    observation = published_capacity(directory, name)
    allowed, reason = _capacity_check(observation)
    reset_at = observation.get("reset_at")
    if (
        allowed is False
        and isinstance(reset_at, (int, float))
        and not isinstance(reset_at, bool)
        and float(reset_at) > time.time()
    ):
        return reason, float(reset_at)
    activity = str(state.get("activity", "")) or UNKNOWN
    if activity == NOT_STARTED:
        waited = (state.get("not_started") or {}).get("deadline", 0)
        return f"it never started within {int(waited)}s of its launch", 0.0
    if activity not in WAKE_READY and observed["process_alive"]:
        return f"its screen state is {activity}", 0.0
    if (
        observed["process_alive"] is False
        and not state.get("session_id")
        and str(record.get("result", "")) == WAKE_ATTENTION
    ):
        return "its session process is not running", 0.0
    return "", 0.0


def _park_wake(path: Path, record: dict, cause: str, due: float | None) -> None:
    """Publishes the blocking cause and next attempt time on a wake record.

    Args:
        path: The lane's durable wake record.
        record: That record as read.
        cause: Why no attempt was made, empty when only spacing applies.
        due: Unix time of the next attempt, None once the budget is spent.
    """
    if record.get("blocked") == cause and record.get("next_at") == due:
        return
    record.update(blocked=cause, next_at=due)
    write_json(path, record)


def _defer_wake(
    directory: Path, name: str, cause: str, ready_at: float, window: float
) -> None:
    """Keeps a blocked lane's next attempt visible without spending one.

    Only a lane that already recorded an attempt is parked, because a lane the
    service never woke owes no schedule and must not gain a wake record from
    being observed.

    Args:
        directory: Private project state directory.
        name: Participant that owns the lane.
        cause: Why no attempt was made.
        ready_at: Earliest time the cause can clear, 0 when unknown.
        window: Inactivity window the attempts are spaced by.
    """
    path = directory / f"{name}-wake.json"
    if not path.exists():
        return
    with lock(directory / f"{name}-wake.lock"):
        record: dict = {}
        with contextlib.suppress(OSError, ValueError):
            record = json.loads(path.read_text())
        if not isinstance(record, dict) or not record.get("result"):
            return
        if record.get("exhausted_at"):
            _park_wake(path, record, cause, None)
            return
        _park_wake(
            path,
            record,
            cause,
            _wake_due(
                float(record.get("at", 0) or 0),
                int(record.get("attempts", 0) or 0),
                ready_at,
                window,
            ),
        )


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
    rather than extending the old one. A request the launcher refuses with a
    `busy` reason is spaced like any other but does not count against the
    bound, because the lane never received a turn to decline; it is asked
    again once it is idle. A non-idle activity label blocks a wake only while
    its recorded process remains alive. A session with no trustworthy process
    identity records a manual-attention refusal and is never presumed dead.

    The attempt bound counts silence, not elapsed wakes. A lane accepts an
    offer and then works for as long as the work takes, which can span several
    wake windows without moving the offer's progress digest, so the digest is
    no longer what the bound is judged on. Each attempt records the lane's own
    activity marker, and an attempt that finds the marker changed resets the
    count to zero and clears any escalation on the offer, whether or not this
    pass goes on to ask for a turn. Only a lane that recorded nothing across
    the whole bound is escalated, because only then is there something an
    operator has to do.

    A spent attempt is not the end of the series. Every poll re-decides the
    lane against what it can read locally: durable provider capacity, the
    published screen state and the recorded session process. A cause that is
    still in force parks the lane with that cause and the time its next attempt
    is due, and spends nothing, so the budget is not consumed while nothing
    could have answered. When the cause clears, the next attempt is due one
    doubling window after the last one, or at the provider reset the capacity
    observation named, whichever is later. Only a lane that has actually spent
    its whole budget is recorded as exhausted, and it keeps the last cause and
    result for the operator instead of a next time it will never have.

    The launcher still owns native authentication, trust and approval prompts.
    A resumed process uses a real terminal, not an unattended permission mode.
    Nothing reads, acknowledges, releases, accepts or transfers work for the
    lane; waking only asks the lane to take its own turn.
    """
    participant = manifest["participants"][name]
    if (
        not config["wake"]
        or not participant.get("wake", True)
        or participant.get("paused", False)
    ):
        return
    path = directory / f"{name}-activity.json"
    state = json.loads(path.read_text()) if path.exists() else {}
    wake_path = directory / f"{name}-wake.json"
    window = config["inactive_after"]
    parked: dict = {}
    with contextlib.suppress(OSError, ValueError):
        if wake_path.exists():
            published = json.loads(wake_path.read_text())
            parked = published if isinstance(published, dict) else {}
    blocked, ready_at = _wake_block(directory, name, state, observed, parked)
    if blocked:
        _defer_wake(directory, name, blocked, ready_at, window)
        return
    if observed["process_alive"] and (
        observed["age_seconds"] is None
        or observed["age_seconds"] < config["inactive_after"]
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
    with lock(directory / f"{name}-wake.lock"):
        work_item = _work_backlog(
            home,
            directory,
            manifest,
            name,
            config,
            published_work(directory, name),
        )
        if work_item:
            work_key, work_offer = work_item
            backlog.append(work_key)
        else:
            work_offer = None
        if not backlog:
            return
        record = json.loads(wake_path.read_text()) if wake_path.exists() else {}
        same_backlog = record.get("backlog") == backlog
        marker = _lane_activity(home, directory, manifest, name)
        worked = bool(record.get("activity")) and record["activity"] != marker
        attempts = (
            record.get("attempts", 0) if same_backlog and not worked else 0
        )
        throttle_at = record.get("at", 0) if same_backlog else 0
        if work_offer:
            dispatch = published_work(directory, name).get("dispatch") or {}
            if not worked:
                attempts = max(attempts, int(dispatch.get("attempts", 0)))
            if dispatch.get("attempts"):
                throttle_at = max(
                    throttle_at, float(dispatch.get("updated_at", 0))
                )
            if worked and dispatch:
                _write_work_dispatch(
                    directory, name, work_offer, 0, "", "pending"
                )
        if worked:
            record.pop("escalated_at", None)
            record.pop("exhausted_at", None)
            record.update(attempts=0, activity=marker, blocked="", next_at=None)
            write_json(wake_path, record)
        if attempts >= WORK_WAKE_ATTEMPTS:
            if not record.get("exhausted_at"):
                record.update(exhausted_at=time.time(), next_at=None)
                write_json(wake_path, record)
            if work_offer and dispatch.get("state") != "escalated":
                result = _work_escalation(
                    work_offer, attempts, str(record.get("result", ""))
                )
                record.update(result=result, escalated_at=time.time())
                write_json(wake_path, record)
                _write_work_dispatch(
                    directory,
                    name,
                    work_offer,
                    attempts,
                    result,
                    "escalated",
                )
            return
        due = _wake_due(float(throttle_at), int(attempts), ready_at, window)
        if time.time() < due:
            if record.get("result"):
                _park_wake(wake_path, record, "", due)
            return
        result = WAKE_ATTENTION
        selected = _select_work_prompt(home, directory, name, work_offer)
        if not selected:
            result = "busy:stale"
        elif observed["process_alive"]:
            result = terminal.request(directory, name)
        elif (
            observed["process_alive"] is False
            or state.get("activity") == "stopped"
        ) and state.get("session_id"):
            entry = roster.provider(home, participant["provider"])
            if entry["adapter"] in roster.ADAPTERS and not entry.get(
                "require_env"
            ):
                prompt = terminal.selected_prompt(directory, name, home)
                if prompt is None:
                    result = "busy:stale"
                else:
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
                                prompt,
                            ],
                            stdin=subprocess.DEVNULL,
                            stdout=output,
                            stderr=output,
                            start_new_session=True,
                        )
                    track_launcher(child)
                    result = f"resume requested (launcher {child.pid})"
        counted = attempts + (not result.startswith("busy"))
        now = time.time()
        spent = counted >= WORK_WAKE_ATTEMPTS
        write_json(
            wake_path,
            {
                "at": now,
                "backlog": backlog,
                "attempts": counted,
                "result": result,
                "activity": marker,
                "blocked": "",
                "next_at": (
                    None if spent else _wake_due(now, counted, 0.0, window)
                ),
                "exhausted_at": now if spent else None,
            },
        )
        if work_offer:
            _write_work_dispatch(
                directory,
                name,
                work_offer,
                counted,
                result,
                (
                    "deferred"
                    if result.startswith("busy")
                    else "awaiting_progress"
                ),
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
        reap_launchers()
        stopped.wait(1)
