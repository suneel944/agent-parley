"""Versioned work-order plans recorded as advisory dependency edges.

An operator enters work order one edge at a time, and a project with a dozen
issues and three parallel tracks needs a dozen commands and keeps no record of
the shape that was intended. A plan is that record: one plain TOML file the
operator writes, applied to the ledger as the same advisory dependencies
`issue block` records.

A plan decides nothing. Applying one records edges and nothing else: it never
claims an issue, never assigns a lane and never gates a transition. The edges
it writes stay advisory, exactly as a hand-recorded edge is, so a plan can be
wrong without stopping anybody.
"""

import hashlib
import json
import time
import tomllib
from pathlib import Path

from agent_parley import roster
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


def _ordered(dependencies: dict[str, list[str]]) -> None:
    """Refuses a plan whose dependencies cannot all be satisfied.

    A cycle describes an order no lane can work in, so it is refused when the
    file is read rather than written into the ledger as edges an operator then
    has to unpick by hand.

    Args:
        dependencies: Blockers recorded against each issue.

    Raises:
        BridgeError: If the dependencies contain a cycle.
    """
    pending = {issue: set(blockers) for issue, blockers in dependencies.items()}
    while pending:
        ready = {
            issue
            for issue, blockers in pending.items()
            if not blockers & set(pending)
        }
        if not ready:
            raise BridgeError(
                "Plan dependencies form a cycle: "
                + ", ".join(f"#{issue}" for issue in sorted(pending, key=int))
            )
        pending = {
            issue: blockers
            for issue, blockers in pending.items()
            if issue not in ready
        }


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
    _ordered(dependencies)
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

    Applying adds edges. It never removes one, never claims an issue and never
    assigns a lane, so an operator who narrows a plan drops the edge with
    `issue unblock` and sees it as unlisted until then. An issue named only as
    a blocker gains no record of its own; an issue that waits on something
    gains an unowned record carrying its dependencies, exactly as
    `issue block` would leave it.

    Args:
        directory: Private state directory for the common repository.
        path: Plan file to apply.
        actor: Identity recorded with the version, the operator by default.

    Returns:
        The recorded version and the edges this apply added.

    Raises:
        BridgeError: If the plan file is unusable or the ledger cannot be
            locked.
    """
    document = read(path)
    with lock(directory / "issues.lock", timeout=1):
        state = snapshot(directory)
        added = []
        for issue, blockers in sorted(document["dependencies"].items()):
            record = state["issues"].setdefault(
                issue,
                {"owner": None, "offer": None, "blocked_by": [], "history": []},
            )
            waiting = record.get("blocked_by", [])
            for blocker in blockers:
                if blocker not in waiting:
                    waiting.append(blocker)
                    added.append((issue, blocker))
            if len(waiting) > MAX_BLOCKERS:
                raise BridgeError(
                    f"Issue #{issue} would wait on more than {MAX_BLOCKERS} "
                    "issues; drop one with issue unblock."
                )
            record["blocked_by"] = sorted(waiting, key=int)
        if added:
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


def describe(directory: Path) -> dict:
    """Returns the applied plan beside the current state of every issue.

    Returns:
        The latest version's name, digest, operator and time, one entry per
        planned issue carrying its owner and the issues it waits on, the
        groups the plan named, and every recorded edge the plan does not name,
        which is an edge entered by hand after the apply.
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
        groups and any edge recorded by hand after the apply.
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
    for name, members in sorted(document["groups"].items()):
        lines.append(
            f"  group {name}: " + ", ".join(f"#{member}" for member in members)
        )
    for issue, blocker in document["unplanned"]:
        lines.append(f"  recorded by hand: #{issue} waits on #{blocker}")
    return "\n".join(lines)
