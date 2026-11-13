"""Lets a lane spawn child lanes on its own branch and receive one report.

A lane that wants parallel subtasks calls ``send_message`` addressed first to
the reserved recipient ``@child``, followed by the repository paths the child
may reserve, with the task as the body. No participant name can begin with
``@``, so the address never collides with a lane, and spawning needs no tool
of its own in the served list; ``spawn_child`` remains the name the spawn is
observed and refused under. The child is
an ordinary participant: it is recorded in the project manifest beside every
other lane, under the parent's provider and credential profile, on a worktree
of its own whose branch is cut from the parent's branch. It launches through
this package's own ``run``, the same detached launcher the service uses to
resume a lane, so it keeps native authentication, native permissions, the
project's ``init`` command, claims, budgets and hooks, and no bypass flag is
ever passed.

Three facts on the child's roster entry carry the relation: ``parent`` names
the lane that spawned it, ``paths`` limits which keys its reservation tools
grant, and ``result`` records the one report it returned. A child's first
ready or blocked report is relayed to the parent as one capped message and
recorded there; later reports stay in the child's own history. The parent
never reads the child's transcript, only that message, and merges the
child's branch into its own with Git.

Children are bounded per parent and per project, a child cannot spawn
children of its own, and the project's participant cap still applies. A
child that returned its report no longer counts against either limit.

A child's work ends in one of two ways, and both retire it in the roster so
it is never woken, offered work or counted again. Its result report retires
it as the report is relayed. A child that files none is given
`DEADLINE_SECONDS` from its spawn, recorded on its entry as ``deadline``;
past that time `expire`, run on every supervision poll, tells the parent
and records the outcome ``expired``. Retiring a child here only records the
time: its worktree and branch stay for the parent to merge, and a session
still running is left to end by itself.
"""

from __future__ import annotations

import fnmatch
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from agent_parley import roster, worktrees
from agent_parley.state import BridgeError, lock, write_json

TOOL = "spawn_child"
ADDRESS = "@child"
MAX_PER_PARENT = 3
MAX_PER_PROJECT = 8
DEADLINE_SECONDS = 4 * 3600.0
EXPIRED = "expired"
MAX_PATHS = 16
MAX_PATH_CHARACTERS = 200
TASK_BYTES = 4000
REPORT_BYTES = 2000
FIELD_BYTES = 500
LOCK_SECONDS = 10.0
STEM_CHARACTERS = 32
INHERITED = (
    "approve_bridge_tools",
    "auto_mode",
    "dialogs",
    "answer_questions",
    "budget",
)
UNREGISTERED = (
    "This repository has no coordination project yet; launch a participant "
    "once with agent-parley run so the project registers."
)


def _clip(text: str, limit: int) -> str:
    """Cuts text to a byte budget on a character boundary.

    Args:
        text: Text to bound.
        limit: Largest encoded size in bytes.

    Returns:
        The text unchanged when it fits, otherwise its longest prefix that
        fits once a trailing ellipsis is added.
    """
    if len(text.encode()) <= limit:
        return text
    return text.encode()[: limit - 3].decode(errors="ignore") + "..."


def _task(value: object) -> str:
    """Validates the task a child is started on.

    Args:
        value: ``task`` argument the caller sent.

    Returns:
        The task, stripped.

    Raises:
        BridgeError: If the task is not a nonempty string within
            `TASK_BYTES`.
    """
    if not isinstance(value, str) or not value.strip():
        raise BridgeError("spawn_child needs a nonempty task.")
    if len(value.encode()) > TASK_BYTES:
        raise BridgeError(
            f"spawn_child task exceeds its {TASK_BYTES}-byte budget; put the "
            "detail in a file the child reads."
        )
    return value.strip()


def _paths(value: object) -> list[str]:
    """Validates the repository paths a child may reserve.

    Args:
        value: ``paths`` argument the caller sent.

    Returns:
        The distinct paths in the order given, trailing slashes removed.

    Raises:
        BridgeError: If the list is empty or too long, or a path is absolute,
            climbs out of the repository with ``..``, names a resource with
            a scheme, or is not a short string.
    """
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_PATHS:
        raise BridgeError(
            f"spawn_child needs 1 to {MAX_PATHS} repository-relative paths."
        )
    accepted: list[str] = []
    for item in value:
        path = item.rstrip("/") if isinstance(item, str) else ""
        if (
            not path
            or len(path) > MAX_PATH_CHARACTERS
            or path.startswith("/")
            or "\\" in path
            or "\0" in path
            or ":" in path
            or ".." in path.split("/")
        ):
            raise BridgeError(
                "spawn_child paths must be repository-relative paths or "
                f"globs of at most {MAX_PATH_CHARACTERS} characters, without "
                "'..', a leading '/' or a resource scheme."
            )
        if path not in accepted:
            accepted.append(path)
    return accepted


def _lane_of(manifest: dict, display: str) -> str:
    """Names the participant an authenticated identity belongs to.

    Args:
        manifest: Project manifest.
        display: Identity the served call authenticated as.

    Returns:
        The participant name.

    Raises:
        BridgeError: If no participant presents that identity.
    """
    for name, entry in manifest["participants"].items():
        if entry.get("display") == display:
            return name
    raise BridgeError(f"{display} is not a participant in this project.")


def due(entry: dict) -> float:
    """Reads the time by which a child must have returned its result.

    Args:
        entry: A child's roster entry.

    Returns:
        The recorded ``deadline`` in epoch seconds. An entry written before
        deadlines were recorded is given `DEADLINE_SECONDS` from its
        ``spawned`` time, so an older child expires as a new one does.
    """
    recorded = entry.get("deadline")
    if isinstance(recorded, (int, float)) and not isinstance(recorded, bool):
        return float(recorded)
    return float(entry.get("spawned") or 0.0) + DEADLINE_SECONDS


def live(manifest: dict, now: float | None = None) -> list[str]:
    """Lists the children that still count against the limits.

    Args:
        manifest: Project manifest.
        now: Epoch seconds the deadlines are judged at; the current time
            when omitted.

    Returns:
        Child participants, in name order, that neither retired nor returned
        their result report and whose deadline has not passed.
    """
    moment = time.time() if now is None else now
    return sorted(
        name
        for name, entry in manifest["participants"].items()
        if entry.get("parent")
        and not roster.retired(entry)
        and not entry.get("result")
        and due(entry) > moment
    )


def results(manifest: dict, name: str) -> set[str]:
    """Names the messages that carried a lane's child outcomes to it.

    Args:
        manifest: Project manifest.
        name: Parent participant.

    Returns:
        Identifiers, as text, of the result and expiry messages recorded on
        the entries of that lane's children.
    """
    return {
        str(entry["result"]["message"])
        for entry in manifest["participants"].values()
        if entry.get("parent") == name
        and (entry.get("result") or {}).get("message") is not None
    }


def of(manifest: dict, name: str) -> list[dict]:
    """Lists one lane's children for a status or web view to render.

    The reading is pure: it takes the manifest a view already holds and
    reads nothing else, so a refreshing view pays nothing extra for it.

    Args:
        manifest: Project manifest.
        name: Participant whose children are listed.

    Returns:
        One mapping per child in name order, with its ``name``, ``branch``,
        ``lane``, reservable ``paths``, ``spawned`` time and ``state``:
        ``running`` until it reports, ``retired`` once it retires without
        a report, otherwise the outcome of its relayed report, which is
        also kept whole under ``result``.
    """
    found = []
    for child, entry in sorted(manifest["participants"].items()):
        if entry.get("parent") != name:
            continue
        result = entry.get("result") or {}
        if result:
            state = str(result.get("outcome") or "reported")
        elif roster.retired(entry):
            state = "retired"
        else:
            state = "running"
        found.append(
            {
                "name": child,
                "branch": entry.get("branch", ""),
                "lane": entry.get("lane", ""),
                "paths": list(entry.get("paths") or []),
                "spawned": entry.get("spawned"),
                "state": state,
                "result": dict(result),
            }
        )
    return found


def outside(scope: list[str], keys: list[str]) -> list[str]:
    """Names the reservation keys that fall outside a child's paths.

    A key is inside when it equals a scope path, lies beneath one as a
    directory, or matches a scope glob. A named resource such as
    ``port:5432`` is inside only when listed exactly, which `spawn_child`
    never allows, so a child reserves files alone.

    Args:
        scope: Paths the child was spawned with.
        keys: Keys a reservation tool was asked for.

    Returns:
        The keys outside the scope, in the order asked.
    """
    return [
        key
        for key in keys
        if not any(
            key == path
            or key.startswith(path + "/")
            or fnmatch.fnmatchcase(key, path)
            for path in scope
        )
    ]


def confine(home: Path, actor: dict, keys: object) -> None:
    """Refuses a child's reservation that reaches beyond its paths.

    Args:
        home: Private bridge state root.
        actor: Authenticated project and lane.
        keys: ``paths`` argument of the reservation call.

    Raises:
        BridgeError: If the caller is a child and any key lies outside the
            paths it was spawned with. Malformed keys are left to the
            reservation tool's own validation.
    """
    root = str(actor.get("project", ""))
    directory = roster.locate(home, root) if root else None
    if directory is None or not isinstance(keys, list):
        return
    try:
        manifest = roster.read(directory)
    except (BridgeError, OSError, ValueError):
        return
    entry: dict[str, Any] = next(
        (
            participant
            for participant in manifest["participants"].values()
            if participant.get("display") == actor.get("name")
        ),
        {},
    )
    scope = entry.get("paths")
    if not entry.get("parent") or not scope:
        return
    wide = outside(list(scope), [key for key in keys if isinstance(key, str)])
    if wide:
        raise BridgeError(
            f"{actor.get('name')} is a child lane limited to "
            f"{', '.join(scope)}; outside that: {', '.join(wide[:5])}."
        )


def _admit(home: Path, directory: Path, manifest: dict, parent: str) -> None:
    """Refuses a spawn the limits or the parent's budget do not allow.

    Args:
        home: Private bridge state root.
        directory: Private project state directory.
        manifest: Project manifest read under the setup lock.
        parent: Participant asking for a child.

    Raises:
        BridgeError: Naming the limit: a child asking for a child, the
            per-parent or per-project child limit, the project participant
            cap, a crossed budget of the parent, or a run budget that admits
            no further turn.
    """
    from agent_parley import budgets

    participants = manifest["participants"]
    if participants[parent].get("parent"):
        raise BridgeError(
            f"spawn_child refused: {parent} is itself a child lane, and a "
            "child cannot spawn children."
        )
    running = live(manifest)
    mine = [name for name in running if participants[name]["parent"] == parent]
    if len(mine) >= MAX_PER_PARENT:
        raise BridgeError(
            f"spawn_child refused: {parent} already runs {len(mine)} child "
            f"lanes, the per-parent limit of {MAX_PER_PARENT}. A child stops "
            "counting once its result report arrives."
        )
    if len(running) >= MAX_PER_PROJECT:
        raise BridgeError(
            f"spawn_child refused: this project already runs {len(running)} "
            f"child lanes, the per-project limit of {MAX_PER_PROJECT}."
        )
    if len(participants) >= roster.MAX_PARTICIPANTS:
        raise BridgeError(
            "spawn_child refused: this project already has "
            f"{roster.MAX_PARTICIPANTS} participants, the project lane cap."
        )
    reading = budgets.standing(home, directory, manifest, parent)
    if reading["over"]:
        raise BridgeError(
            f"spawn_child refused: {parent} is over its budget "
            f"({', '.join(reading['crossed'])}), and a child runs under the "
            "same limits."
        )
    if reason := budgets.admit(directory, manifest, parent):
        raise BridgeError(f"spawn_child refused: {reason}.")


def _slot(manifest: dict, parent: str, refs: set[str], directory: Path) -> int:
    """Picks the first child number whose name, branch and path are free.

    Args:
        manifest: Project manifest read under the setup lock.
        parent: Participant spawning the child.
        refs: Local branch names the repository holds.
        directory: Private project state directory.

    Returns:
        The child number.

    Raises:
        BridgeError: If every number up to the participant cap is taken.
    """
    participants = manifest["participants"]
    displays = {entry.get("display") for entry in participants.values()}
    stem = parent[:STEM_CHARACTERS]
    branch = participants[parent]["branch"]
    for number in range(1, roster.MAX_PARTICIPANTS + 1):
        name = f"{stem}-c{number}"
        if (
            name not in participants
            and name not in displays
            and f"{branch}-c{number}" not in refs
            and not (directory / name).exists()
        ):
            return number
    raise BridgeError(f"spawn_child refused: no free child name for {parent}.")


def brief(parent: str, branch: str, paths: list[str], task: str) -> str:
    """Writes the task a child session starts on.

    Args:
        parent: Participant that spawned the child.
        branch: Branch the child commits to.
        paths: Paths the child may reserve.
        task: Task the parent asked for.

    Returns:
        The task text, naming the parent, the branch, the paths and the one
        report that ends the child's work.
    """
    return (
        f"You are a child lane of {parent}, on branch {branch}, cut from "
        f"{parent}'s branch. Reserve and change only: {', '.join(paths)}. "
        "Commit your work on your branch. When the task is done, file "
        "exactly one result report and stop: `agent-parley report --state "
        "ready --summary '<what changed>' --evidence '<checks run and their "
        "results>'`, or `--state blocked --remaining '<what is left>'`. "
        f"{parent} receives only that report and merges your branch.\n\n"
        f"Task from {parent}:\n{task}"
    )


def launch(
    home: Path, directory: Path, root: str, name: str, task: str
) -> subprocess.Popen[bytes]:
    """Starts the detached launcher that runs a child's native CLI.

    The launcher is this package's own ``run``, as for a service resume, so
    the child registers, configures its native client and starts it with
    the provider and credential profile recorded for it, and its output
    goes to the child's wake log. Standard input is closed, and the launcher
    still gives a child the pseudo-terminal, dialog watch and wake socket of
    a resumed lane, because it takes that path for any participant whose
    entry names a ``parent``.

    Args:
        home: Private bridge state root.
        directory: Private project state directory.
        root: Repository root the child belongs to.
        name: Child participant.
        task: Task the child session starts on.

    Returns:
        The tracked launcher process.
    """
    from agent_parley import supervision

    command = [
        sys.executable,
        "-m",
        "agent_parley.cli",
        "--home",
        str(home),
        "run",
        name,
        "--repo",
        root,
        "--task",
        task,
    ]
    with (directory / f"{name}-wake.log").open("ab") as output:
        child = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=output,
            start_new_session=True,
        )
    supervision.track_launcher(child)
    return child


def requested(tool: str, args: dict) -> dict | None:
    """Reads a spawn request out of a ``send_message`` call, if it is one.

    Only ``ADDRESS`` as the first recipient asks for a child; the same
    address anywhere else is left to ordinary recipient validation, which
    refuses it as an unregistered participant.

    Args:
        tool: Name of the coordination tool called.
        args: Arguments of that call.

    Returns:
        The ``task`` and ``paths`` to spawn with, or None when the call does
        not address a child.
    """
    to = args.get("to")
    if tool != "send_message" or not isinstance(to, list) or not to:
        return None
    if to[0] != ADDRESS:
        return None
    return {"task": args.get("body_md"), "paths": to[1:]}


def spawn(home: Path, actor: dict, args: dict) -> dict:
    """Registers a child of the calling lane and launches it.

    Admission, the worktree, ``init`` and the roster entry all happen under
    the project setup lock, so two spawns racing for the last place cannot
    both pass. The launch starts after the lock is released, because the
    launcher takes that lock itself.

    Args:
        home: Private bridge state root.
        actor: Authenticated project and lane.
        args: ``task`` and ``paths`` from the tool call.

    Returns:
        The child's name, branch, worktree, reservable paths, provider,
        credential profile and the log its launcher writes.

    Raises:
        BridgeError: If an argument is invalid, a limit or budget refuses
            the spawn, the project or caller is unknown, Git cannot create
            the worktree, or ``init`` fails in it.
    """
    task = _task(args.get("task"))
    paths = _paths(args.get("paths"))
    root = str(actor.get("project", ""))
    directory = roster.locate(home, root) if root else None
    if directory is None:
        raise BridgeError(UNREGISTERED)
    with lock(directory / "setup.lock", timeout=LOCK_SECONDS):
        manifest = roster.read(directory)
        participants = manifest["participants"]
        parent = _lane_of(manifest, str(actor.get("name", "")))
        _admit(home, directory, manifest, parent)
        base = Path(manifest["root"])
        refs = set(
            worktrees.git(
                base, "for-each-ref", "--format=%(refname:short)", "refs/heads"
            ).splitlines()
        )
        number = _slot(manifest, parent, refs, directory)
        source = participants[parent]
        name = f"{parent[:STEM_CHARACTERS]}-c{number}"
        branch = f"{source['branch']}-c{number}"
        lane = directory / name
        started = time.monotonic()
        worktrees.git(
            base, "worktree", "add", "-b", branch, str(lane), source["branch"]
        )
        created = time.monotonic()
        if manifest.get("initialize"):
            worktrees.initialize_lane(lane, manifest["initialize"], base)
        spawned = time.time()
        participants[name] = {
            "provider": source["provider"],
            "display": name,
            "lane": str(lane),
            "branch": branch,
            "credential": source["credential"],
            "scheme": "lane",
            "launch": worktrees.preparation(
                created - started, time.monotonic() - created
            ),
            **{key: source[key] for key in INHERITED if key in source},
            "parent": parent,
            "paths": paths,
            "spawned": spawned,
            "deadline": spawned + DEADLINE_SECONDS,
        }
        write_json(directory / "project.json", manifest)
    launch(
        home,
        directory,
        manifest["root"],
        name,
        brief(parent, branch, paths, task),
    )
    return {
        "child": name,
        "branch": branch,
        "worktree": str(lane),
        "paths": paths,
        "provider": source["provider"],
        "credential": source["credential"],
        "log": str(directory / f"{name}-wake.log"),
    }


def message(name: str, branch: str, report: dict) -> str:
    """Writes the capped result message a parent receives.

    Args:
        name: Child participant.
        branch: Child branch.
        report: The child's recorded report: ``state``, ``commit``,
            ``summary``, ``evidence`` and ``remaining``.

    Returns:
        The message body, at most `REPORT_BYTES`, carrying only the report's
        fields and the merge step, never the child's transcript.
    """
    lines = [
        f"Child lane {name} reported {report.get('state')}.",
        f"Branch: {branch} at {str(report.get('commit') or '')[:12]}",
        "Summary: " + _clip(str(report.get("summary") or ""), FIELD_BYTES),
    ]
    if report.get("evidence"):
        lines.append("Checks: " + _clip(str(report["evidence"]), FIELD_BYTES))
    if report.get("remaining"):
        lines.append(
            "Remaining: " + _clip(str(report["remaining"]), FIELD_BYTES)
        )
    merge = f"Merge it into your branch: git merge --no-ff {branch}"
    room = REPORT_BYTES - len(merge.encode()) - 1
    return _clip("\n".join(lines), room) + "\n" + merge


def deliver(home: Path, directory: Path, name: str, report: dict) -> bool:
    """Relays a child's first ready or blocked report to its parent.

    The message is sent as the child, with a key fixed per child, and the
    child's roster entry records it under ``result`` in the same setup
    lock, so the parent receives one result however often the child
    reports. The same write retires the child, so it stops being served,
    woken or offered work the moment its result is recorded. A child that
    is not registered with the store yet is refused by the send, nothing is
    recorded, and its next report tries again; `expire` ends a child whose
    result never arrives.

    Args:
        home: Private bridge state root.
        directory: Private project state directory.
        name: Participant that filed the report.
        report: The recorded report, as `message` reads it.

    Returns:
        True when the result was relayed now, False for a lane that is not
        a child or already returned its result.

    Raises:
        BridgeError: If the store refuses the message.
    """
    from agent_parley import store

    with lock(directory / "setup.lock", timeout=LOCK_SECONDS):
        manifest = roster.read(directory)
        entry = manifest["participants"].get(name) or {}
        parent = manifest["participants"].get(entry.get("parent") or "")
        if parent is None or entry.get("result"):
            return False
        sent = store.speak(
            home,
            manifest["root"],
            parent["display"],
            f"Child {name} result: {report.get('state')}",
            message(name, entry["branch"], report),
            f"child-result-{name}",
            sender=entry["display"],
        )
        at = time.time()
        entry["result"] = {
            "outcome": report.get("state"),
            "commit": report.get("commit"),
            "message": sent.get("id"),
            "at": at,
        }
        entry["retired"] = at
        write_json(directory / "project.json", manifest)
    return True


def expire(home: Path, directory: Path, now: float | None = None) -> list[str]:
    """Ends every child whose deadline passed without a result report.

    Each such child is recorded with the outcome `EXPIRED` and retired, and
    its parent is sent one operator message, keyed per child, naming the
    branch the child left. A parent the store cannot be reached for still
    has its child ended, with no message recorded, so a failed send never
    keeps the slot held. The manifest is first read without the setup lock,
    and the lock is taken only when that reading holds an overdue child, so
    a poll of a project with none never contends for it.

    Args:
        home: Private bridge state root.
        directory: Private project state directory.
        now: Epoch seconds the deadlines are judged at; the current time
            when omitted.

    Returns:
        The children ended now, in name order.
    """
    from agent_parley import store

    moment = time.time() if now is None else now
    ended: list[str] = []
    unlocked = roster.read(directory)
    if live(unlocked, 0.0) == live(unlocked, moment):
        return ended
    with lock(directory / "setup.lock", timeout=LOCK_SECONDS):
        manifest = roster.read(directory)
        participants = manifest["participants"]
        for name, entry in sorted(participants.items()):
            parent = participants.get(entry.get("parent") or "")
            if (
                parent is None
                or entry.get("result")
                or roster.retired(entry)
                or due(entry) > moment
            ):
                continue
            sent: dict = {}
            try:
                sent = store.speak(
                    home,
                    manifest["root"],
                    parent["display"],
                    f"Child {name} result: {EXPIRED}",
                    f"Child lane {name} returned no result report within "
                    "its deadline and was retired. It no longer counts "
                    "against the child limits. Its branch "
                    f"{entry.get('branch', '')} keeps whatever it committed.",
                    f"child-expired-{name}",
                )
            except BridgeError as exc:
                print(f"Child expiry not relayed: {exc}", file=sys.stderr)
            entry["result"] = {
                "outcome": EXPIRED,
                "commit": None,
                "message": sent.get("id"),
                "at": moment,
            }
            entry["retired"] = moment
            ended.append(name)
        if ended:
            write_json(directory / "project.json", manifest)
    return ended
