"""Versioned work-order plans recorded as advisory dependency edges.

An operator enters work order one edge at a time, and a project with a dozen
issues and three parallel tracks needs a dozen commands and keeps no record of
the shape that was intended. A plan is that record: one plain TOML file the
operator writes, applied to the ledger as the same advisory dependencies
`issue block` records.

A plan authorizes its listed issues for automatic dispatch and records their
advisory dependency edges. It never claims an issue or assigns a lane. The
edges stay advisory, exactly as a hand-recorded edge is.
"""

import hashlib
import json
import time
import tomllib
from pathlib import Path

from agent_parley import lifecycle, roster
from agent_parley.issues import MAX_BLOCKERS, parse_issue, snapshot
from agent_parley.state import BridgeError, lock, write_json

PLAN = "plan.json"
MAX_ISSUES = 200
MAX_GROUPS = 32
MAX_GROUP_MEMBERS = 32
MAX_VERSIONS = 20
MAX_NAME = 80


def _named(value: object, label: str) -> str:
    """Returns a bounded plain name, or reports why the value is unusable."""
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_NAME:
        raise BridgeError(f"{label} must be text of 1..{MAX_NAME} characters.")
    return value


def _issues(value: object, label: str, limit: int) -> list[str]:
    """Returns the bounded issue numbers one plan field lists."""
    if not isinstance(value, list) or len(value) > limit:
        raise BridgeError(f"{label} must list at most {limit} issue numbers.")
    numbers = [parse_issue(str(member), label) for member in value]
    if len(set(numbers)) != len(numbers):
        raise BridgeError(f"{label} names the same issue twice.")
    return numbers


def _sortable(name: str) -> tuple[int, int, str]:
    """Orders numeric names by value and every other name by its text."""
    return (0, int(name), "") if name.isdigit() else (1, 0, name)


def order(
    dependencies: dict[str, list[str]],
    label: str = "Dependencies",
    mark: str = "",
) -> list[str]:
    """Returns every named item after the items it waits on.

    A cycle describes an order nothing can proceed in, so it is refused and
    named rather than resolved by dropping an edge or by falling back to the
    order the names happened to arrive in. Ordering is used both when a plan
    file is read and when ready lanes are integrated, so one refusal covers
    both.

    Args:
        dependencies: Names mapped to the names each one waits on. A name
            that appears only as a dependency constrains the order but is not
            itself returned.
        label: Subject a cycle refusal names.
        mark: Prefix a cycle refusal puts before each name, so issue numbers
            keep the `#` they carry everywhere else and lane names keep none.

    Returns:
        Every key of the mapping, each one after every key it waits on. Names
        freed at the same step are returned in numeric order when they are
        numbers and in alphabetical order otherwise, so one graph always
        yields one order.

    Raises:
        BridgeError: If the dependencies contain a cycle.
    """
    pending = {name: set(waits) for name, waits in dependencies.items()}
    sequence: list[str] = []
    while pending:
        free = sorted(
            (
                name
                for name, waits in pending.items()
                if not waits & set(pending)
            ),
            key=_sortable,
        )
        if not free:
            raise BridgeError(
                f"{label} form a cycle: "
                + ", ".join(
                    f"{mark}{name}" for name in sorted(pending, key=_sortable)
                )
            )
        sequence += free
        pending = {
            name: waits
            for name, waits in pending.items()
            if name not in set(free)
        }
    return sequence


def read(path: Path) -> dict:
    """Reads and validates one plan file without touching the ledger.

    The file is TOML, which the standard library parses. `[plan]` carries an
    optional name, `[dependencies]` maps each issue to the issues it waits on,
    and `[groups]` names sets whose members may proceed together.

    Args:
        path: Plan file written by the operator.

    Returns:
        The plan's name, its dependencies, its groups, and the digest of the
        exact bytes read, which is what a recorded version is identified by.

    Raises:
        BridgeError: If the file is missing, is not valid TOML, exceeds a
            documented bound, names a malformed issue, or contains a cycle.
    """
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise BridgeError(f"Plan file cannot be read: {exc}") from exc
    try:
        document = tomllib.loads(content.decode())
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise BridgeError(f"Plan file is not valid TOML: {exc}") from exc
    if set(document) - {"plan", "dependencies", "groups"}:
        raise BridgeError(
            "A plan holds only [plan], [dependencies] and [groups]."
        )
    heading = document.get("plan") or {}
    if not isinstance(heading, dict):
        raise BridgeError("[plan] must be a table.")
    listed = document.get("dependencies") or {}
    grouped = document.get("groups") or {}
    if not isinstance(listed, dict) or not isinstance(grouped, dict):
        raise BridgeError("[dependencies] and [groups] must be tables.")
    if len(listed) > MAX_ISSUES:
        raise BridgeError(f"A plan holds at most {MAX_ISSUES} issues.")
    if len(grouped) > MAX_GROUPS:
        raise BridgeError(f"A plan holds at most {MAX_GROUPS} groups.")
    dependencies = {
        parse_issue(str(issue)): _issues(
            blockers, f"dependencies.{issue}", MAX_BLOCKERS
        )
        for issue, blockers in listed.items()
    }
    for issue, blockers in dependencies.items():
        if issue in blockers:
            raise BridgeError(f"Issue #{issue} cannot wait on itself.")
    order(dependencies, "Plan dependencies", "#")
    return {
        "name": _named(heading.get("name", path.stem), "plan.name"),
        "dependencies": dependencies,
        "groups": {
            _named(name, "group name"): _issues(
                members, f"groups.{name}", MAX_GROUP_MEMBERS
            )
            for name, members in grouped.items()
        },
        "digest": hashlib.sha256(content).hexdigest(),
    }


def recorded(directory: Path) -> dict:
    """Returns the applied plan versions, or an empty history."""
    path = directory / PLAN
    return (
        json.loads(path.read_text())
        if path.exists()
        else {"revision": 0, "versions": []}
    )


def edges(state: dict) -> set[tuple[str, str]]:
    """Returns every dependency edge the ledger currently records."""
    return {
        (issue, blocker)
        for issue, record in state["issues"].items()
        for blocker in record.get("blocked_by", [])
    }


def planned(document: dict) -> set[tuple[str, str]]:
    """Returns every dependency edge one plan document describes."""
    return {
        (issue, blocker)
        for issue, blockers in document["dependencies"].items()
        for blocker in blockers
    }


def groups(directory: Path) -> dict[str, list[str]]:
    """Returns the groups the most recently applied plan named."""
    history = recorded(directory)
    version = history["versions"][-1] if history["versions"] else {}
    return version.get("groups", {})


def members(directory: Path, name: str) -> list[str]:
    """Returns the issues one group of the applied plan names.

    Args:
        directory: Private state directory for the common repository.
        name: Group named by the applied plan.

    Returns:
        The group's issue numbers, in the order the plan file listed them.

    Raises:
        BridgeError: If no plan is applied, if the plan names no such group,
            or if the group is empty.
    """
    named = groups(directory)
    if not named:
        raise BridgeError(
            "No applied plan names any group; apply one with "
            "`agent-parley plan apply FILE`."
        )
    if name not in named:
        raise BridgeError(
            f"The applied plan names no group {name}. It names: "
            + ", ".join(sorted(named))
        )
    if not named[name]:
        raise BridgeError(f"Group {name} names no issues.")
    return named[name]


def ready_groups(
    named: dict[str, list[str]], state: dict, reported: set[str]
) -> list[str]:
    """Names the groups whose every member is held by a lane reported ready.

    A ready group is the operator's signal that a set is integrable as a set.
    It reports what the lanes themselves reported and nothing more: a reported
    state is a lane's own account, never review or independent verification.

    Args:
        named: Groups the applied plan names, mapped to their issues.
        state: Published issue ledger.
        reported: Participants whose latest report is the ready state.

    Returns:
        The group names whose members are all claimed and all held by a
        participant in the reported set, in alphabetical order.
    """
    return sorted(
        name
        for name, issues in named.items()
        if issues
        and all(
            state["issues"].get(issue, {}).get("owner") in reported
            for issue in issues
        )
    )


def diff(directory: Path, path: Path) -> dict:
    """Reports the edges a plan file would add, and those it does not name.

    Args:
        directory: Private state directory for the common repository.
        path: Plan file to compare against the ledger.

    Returns:
        The plan's name and digest, the edges applying it would add, and the
        recorded edges the file does not name, which applying never removes.

    Raises:
        BridgeError: If the plan file is unusable.
    """
    document = read(path)
    current = edges(snapshot(directory))
    intended = planned(document)
    return {
        "plan": document["name"],
        "digest": document["digest"],
        "add": sorted(intended - current),
        "unlisted": sorted(current - intended),
    }


def apply(directory: Path, path: Path, actor: str = roster.OPERATOR) -> dict:
    """Records a plan's dependencies and the version that recorded them.

    Applying authorizes every issue the plan names and adds edges. An edge to
    an issue already complete is skipped, because nothing would ever clear
    it, and an edge that closes a cycle with the edges already recorded,
    from this plan or any other, refuses the whole apply. It never
    removes one, claims an issue or assigns a lane, so an operator who narrows
    a plan drops the edge with `issue unblock` and sees it as unlisted until
    then. Blockers gain queued records so dispatch can finish them before
    their successors.

    Args:
        directory: Private state directory for the common repository.
        path: Plan file to apply.
        actor: Identity recorded with the version, the operator by default.

    Returns:
        The recorded version and the edges this apply added.

    Raises:
        BridgeError: If the plan file is unusable, an edge would close a
            dependency cycle, or the ledger cannot be locked.
    """
    document = read(path)
    with lock(directory / "issues.lock", timeout=1):
        state = snapshot(directory)
        added = []
        approved = set(document["dependencies"])
        approved.update(
            blocker
            for blockers in document["dependencies"].values()
            for blocker in blockers
        )
        approved.update(
            member
            for members in document["groups"].values()
            for member in members
        )
        for issue in sorted(approved, key=int):
            record = state["issues"].setdefault(
                issue,
                {
                    "owner": None,
                    "offer": None,
                    "request": None,
                    "blocked_by": [],
                    "history": [],
                    "deadline": None,
                    "attempts": 0,
                    "budget": None,
                    "execution": lifecycle.initial(),
                },
            )
            lifecycle.authorize(record)
        for issue, blockers in sorted(document["dependencies"].items()):
            record = state["issues"][issue]
            waiting = record.get("blocked_by", [])
            for blocker in blockers:
                recorded_blocker = state["issues"][blocker]
                if blocker in waiting or (
                    lifecycle.state(recorded_blocker)["state"]
                    == lifecycle.COMPLETE
                ):
                    continue
                if lifecycle.reaches(state["issues"], blocker, issue):
                    raise BridgeError(
                        f"Issue #{issue} waiting on #{blocker} would form a "
                        "dependency cycle with the recorded ledger."
                    )
                waiting.append(blocker)
                record["blocked_by"] = waiting
                added.append((issue, blocker))
            if len(waiting) > MAX_BLOCKERS:
                raise BridgeError(
                    f"Issue #{issue} would wait on more than {MAX_BLOCKERS} "
                    "issues; drop one with issue unblock."
                )
            record["blocked_by"] = sorted(waiting, key=int)
        if approved:
            state["revision"] += 1
            write_json(directory / "issues.json", state)
    version = {
        "at": time.time(),
        "by": actor,
        "name": document["name"],
        "digest": document["digest"],
        "dependencies": document["dependencies"],
        "groups": document["groups"],
        "added": sorted(added),
    }
    with lock(directory / "plan.lock", timeout=1):
        history = recorded(directory)
        history["versions"] = [*history["versions"], version][-MAX_VERSIONS:]
        history["revision"] += 1
        write_json(directory / PLAN, history)
    return version


def describe(directory: Path, reported: set[str] | None = None) -> dict:
    """Returns the applied plan beside the current state of every issue.

    Args:
        directory: Private state directory for the common repository.
        reported: Participants whose latest report is the ready state, used
            to mark the groups that are integrable as a set. No group is
            marked when the caller supplies none.

    Returns:
        The latest version's name, digest, operator and time, one entry per
        planned issue carrying its owner and the issues it waits on, the
        groups the plan named, the groups whose members are all reported
        ready, and every recorded edge the plan does not name, which is an
        edge entered by hand after the apply.
    """
    history = recorded(directory)
    version = history["versions"][-1] if history["versions"] else {}
    state = snapshot(directory)
    dependencies = version.get("dependencies", {})
    blocking = {
        blocker for blockers in dependencies.values() for blocker in blockers
    }
    numbers = sorted(set(dependencies) | blocking, key=int)
    return {
        "plan": version.get("name", ""),
        "digest": version.get("digest", ""),
        "applied_by": version.get("by", ""),
        "applied_at": version.get("at"),
        "versions": len(history["versions"]),
        "issues": [
            {
                "issue": number,
                "owner": state["issues"].get(number, {}).get("owner"),
                "title": state["issues"].get(number, {}).get("title"),
                "waits_on": dependencies.get(number, []),
            }
            for number in numbers
        ],
        "groups": version.get("groups", {}),
        "ready_groups": ready_groups(
            version.get("groups", {}), state, set(reported or ())
        ),
        "unplanned": sorted(
            edges(state) - planned({"dependencies": dependencies})
        ),
    }


def render_diff(reported: dict) -> str:
    """Formats a comparison between a plan file and the recorded edges.

    Returns:
        One line per edge applying the file would add, one per recorded edge
        the file does not name, and a notice when the two already agree.
    """
    lines = [f"{reported['plan']} ({reported['digest'][:12]})"]
    for issue, blocker in reported["add"]:
        lines.append(f"  add: #{issue} waits on #{blocker}")
    for issue, blocker in reported["unlisted"]:
        lines.append(f"  unlisted: #{issue} waits on #{blocker}")
    if len(lines) == 1:
        lines.append("  The ledger already matches this plan.")
    return "\n".join(lines)


def render(document: dict) -> str:
    """Formats an applied plan as an indented tree for a terminal.

    Returns:
        One line per issue, indented under the issues it waits on, carrying
        the current owner and any recorded title, followed by the plan's
        groups, each marked when every member is reported ready, and any edge
        recorded by hand after the apply.
    """
    if not document["plan"]:
        return "No plan applied."
    waits = {entry["issue"]: entry["waits_on"] for entry in document["issues"]}
    owners = {entry["issue"]: entry for entry in document["issues"]}
    children: dict[str, list[str]] = {number: [] for number in waits}
    roots = []
    for number, blockers in waits.items():
        if blockers:
            for blocker in blockers:
                children.setdefault(blocker, []).append(number)
        else:
            roots.append(number)
    lines = [
        f"{document['plan']} ({document['digest'][:12]}, "
        f"applied by {document['applied_by']})"
    ]

    def branch(number: str, depth: int, seen: tuple[str, ...]) -> None:
        """Prints one issue and the issues that wait on it."""
        entry = owners.get(number, {})
        owner = entry.get("owner") or "unclaimed"
        line = f"{'  ' * (depth + 1)}#{number}: {owner}"
        if title := entry.get("title"):
            line += f" — {title}"
        if number in seen:
            lines.append(line + " (already shown)")
            return
        lines.append(line)
        for waiting in sorted(children.get(number, []), key=int):
            branch(waiting, depth + 1, (*seen, number))

    for number in sorted(roots, key=int):
        branch(number, 0, ())
    for name, listed in sorted(document["groups"].items()):
        line = f"  group {name}: " + ", ".join(
            f"#{member}" for member in listed
        )
        if name in document.get("ready_groups", []):
            line += " (every member reported ready)"
        lines.append(line)
    for issue, blocker in document["unplanned"]:
        lines.append(f"  recorded by hand: #{issue} waits on #{blocker}")
    return "\n".join(lines)
