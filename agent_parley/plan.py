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
from agent_parley.state import BridgeError, Transient, lock, write_json

PLAN = "plan.json"
MAX_ISSUES = 200
MAX_GROUPS = 32
MAX_GROUP_MEMBERS = 32
MAX_VERSIONS = 20
MAX_NAME = 80
MAX_CHANGES = 10
MAX_AUTOMATIC = 100
MAX_PENDING = 20
MAX_PROPOSALS = 50
MAX_REASON = 2000
MAX_EVIDENCE = 5
MAX_EVIDENCE_TEXT = 500
MAX_FLIPS = 2
MAX_APPLIED_IDS = 100
PENDING = "pending"
ESCALATED = "escalated"
ACCEPTED = "accepted"
REJECTED = "rejected"
STALE = "stale"
OPEN = (PENDING, ESCALATED)


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
    `[groups]` names sets whose members may proceed together, and the
    optional `[revisions]` table is the envelope `envelope` validates.

    Args:
        path: Plan file written by the operator.

    Returns:
        The plan's name, its dependencies, its groups, its revision envelope,
        and the digest of the exact bytes read, which is what a recorded
        version is identified by.

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
    if set(document) - {"plan", "dependencies", "groups", "revisions"}:
        raise BridgeError(
            "A plan holds only [plan], [dependencies], [groups] and "
            "[revisions]."
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
        "envelope": envelope(document.get("revisions")),
        "digest": hashlib.sha256(content).hexdigest(),
    }


def envelope(value: object) -> dict | None:
    """Validates the operator's `[revisions]` table, the automatic envelope.

    A lane's revision proposal is applied without the operator only when
    every change stays inside this envelope. A plan without the table has
    no envelope, so every lane proposal waits for the operator.

    Args:
        value: The parsed `[revisions]` table, or None when the plan has
            none.

    Returns:
        The issues whose edges lanes may revise unattended, the most edges
        one automatic revision may change, and the most automatic revisions
        one applied plan allows, or None when the plan defines no envelope.

    Raises:
        BridgeError: If the table holds an unknown key or a value outside
            its documented bound.
    """
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - {
        "scope",
        "max_changes",
        "max_revisions",
    }:
        raise BridgeError(
            "[revisions] holds only scope, max_changes and max_revisions."
        )
    bounds = {"max_changes": MAX_CHANGES, "max_revisions": MAX_AUTOMATIC}
    limits: dict[str, int] = {}
    for field, bound in bounds.items():
        limit = value.get(field, 1)
        if type(limit) is not int or not 1 <= limit <= bound:
            raise BridgeError(
                f"revisions.{field} must be a whole number from 1 to {bound}."
            )
        limits[field] = limit
    return {
        "scope": sorted(
            _issues(value.get("scope", []), "revisions.scope", MAX_ISSUES),
            key=int,
        ),
        **limits,
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


def _blank() -> dict:
    """Returns the queued, unowned record a plan gives an unrecorded issue."""
    return {
        "owner": None,
        "offer": None,
        "request": None,
        "blocked_by": [],
        "history": [],
        "deadline": None,
        "attempts": 0,
        "budget": None,
        "execution": lifecycle.initial(),
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
    their successors. Removing or changing an edge under a plan is the
    separate revision operation `propose` and `decide` implement; an apply
    starts a new plan version, which leaves every open proposal stale and
    resets the automatic revision count and the contradiction record.

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
    with (
        lock(directory / "plan.lock", timeout=1),
        lock(directory / "issues.lock", timeout=1),
    ):
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
            record = state["issues"].setdefault(issue, _blank())
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
            "envelope": document["envelope"],
            "added": sorted(added),
        }
        history = recorded(directory)
        history["versions"] = [*history["versions"], version][-MAX_VERSIONS:]
        history["revision"] += 1
        history.update(automatic=0, flips={})
        _expire(history)
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


def _edge(value: str, label: str) -> tuple[str, str]:
    """Parses one `ISSUE:BLOCKER` edge a revision names."""
    issue, separator, blocker = value.partition(":")
    if not separator:
        raise BridgeError(
            f"{label} {value!r} must read ISSUE:BLOCKER, e.g. 42:17."
        )
    waiting, awaited = parse_issue(issue, label), parse_issue(blocker, label)
    if waiting == awaited:
        raise BridgeError(f"Issue #{waiting} cannot wait on itself.")
    return waiting, awaited


def _changes(add: list[str], remove: list[str]) -> list[dict]:
    """Returns the bounded, ordered edge changes one proposal requests."""
    changes = [
        {"op": op, "issue": issue, "blocker": blocker}
        for op, values in (("add", add), ("remove", remove))
        for issue, blocker in (_edge(value, f"--{op}") for value in values)
    ]
    if not 1 <= len(changes) <= MAX_CHANGES:
        raise BridgeError(f"A revision changes 1 to {MAX_CHANGES} edges.")
    if len({(item["issue"], item["blocker"]) for item in changes}) != len(
        changes
    ):
        raise BridgeError("A revision names the same edge twice.")
    return sorted(
        changes,
        key=lambda item: (int(item["issue"]), int(item["blocker"])),
    )


def _grounds(reason: str, evidence: list[str]) -> tuple[str, list[str]]:
    """Returns the bounded rationale and evidence one proposal carries."""
    reason = reason.strip()
    if not 1 <= len(reason) <= MAX_REASON:
        raise BridgeError(f"A revision reason holds 1 to {MAX_REASON} chars.")
    items = [item.strip() for item in evidence]
    if len(items) > MAX_EVIDENCE or any(
        not 1 <= len(item) <= MAX_EVIDENCE_TEXT for item in items
    ):
        raise BridgeError(
            f"A revision carries at most {MAX_EVIDENCE} evidence items of "
            f"1 to {MAX_EVIDENCE_TEXT} characters each."
        )
    return reason, items


def _revised(state: dict, changes: list[dict]) -> dict[str, list[str]]:
    """Returns the whole dependency graph a revision would leave.

    Args:
        state: Published issue ledger.
        changes: Edge changes of one proposal.

    Returns:
        Every recorded issue mapped to the issues it would wait on.

    Raises:
        BridgeError: If a removed edge is not recorded, an added edge is
            already recorded or waits on complete work, an issue would wait
            on more than the ledger allows, or the resulting graph, recorded
            edges included, contains a cycle.
    """
    ledger = state["issues"]
    graph = {
        number: list(record.get("blocked_by", []))
        for number, record in ledger.items()
    }
    for change in changes:
        issue, blocker = change["issue"], change["blocker"]
        waits = graph.setdefault(issue, [])
        if change["op"] == "remove":
            if blocker not in waits:
                raise BridgeError(
                    f"Issue #{issue} does not wait on #{blocker}."
                )
            waits.remove(blocker)
            continue
        if blocker in waits:
            raise BridgeError(f"Issue #{issue} already waits on #{blocker}.")
        finished = lifecycle.state(ledger.get(blocker, {}))["state"]
        if finished == lifecycle.COMPLETE:
            raise BridgeError(
                f"Issue #{blocker} is already complete; an edge on it would "
                "never clear."
            )
        waits.append(blocker)
        if len(waits) > MAX_BLOCKERS:
            raise BridgeError(
                f"Issue #{issue} would wait on more than {MAX_BLOCKERS} issues."
            )
    order(graph, "Revised dependencies", "#")
    return graph


def _effect(state: dict, graph: dict[str, list[str]]) -> dict:
    """Names the authorized work a revision would hold back or release."""
    ledger = state["issues"]

    def clear(waits: list[str]) -> bool:
        """Reports whether every issue in the list is verified complete."""
        return all(
            lifecycle.state(ledger.get(number, {}))["state"]
            == lifecycle.COMPLETE
            for number in waits
        )

    held: list[str] = []
    freed: list[str] = []
    for number, record in ledger.items():
        execution = lifecycle.state(record)
        if (
            not execution["authorized"]
            or execution["state"] == lifecycle.COMPLETE
        ):
            continue
        before = clear(record.get("blocked_by", []))
        after = clear(graph.get(number, []))
        if before and not after:
            held.append(number)
        elif after and not before:
            freed.append(number)
    return {"waits": sorted(held, key=int), "frees": sorted(freed, key=int)}


def _held(
    history: dict, state: dict, changes: list[dict], by: str
) -> list[str]:
    """Names every reason a lane's proposal falls outside the envelope.

    Args:
        history: Recorded plan versions and revision counters.
        state: Published issue ledger.
        changes: Edge changes of the proposal.
        by: Participant that filed the proposal.

    Returns:
        Each reason once, in the order found. An empty list means the
        proposal may be applied without the operator.
    """
    bounds = history["versions"][-1].get("envelope")
    if not bounds:
        return ["the applied plan defines no [revisions] envelope"]
    reasons = []
    if len(changes) > bounds["max_changes"]:
        reasons.append(
            f"it changes {len(changes)} edges; the envelope allows "
            f"{bounds['max_changes']} per revision"
        )
    if history.get("automatic", 0) >= bounds["max_revisions"]:
        reasons.append(
            f"the plan's {bounds['max_revisions']} automatic revisions are "
            "spent"
        )
    scope = set(bounds["scope"])
    ledger = state["issues"]
    for change in changes:
        issue, blocker = change["issue"], change["blocker"]
        reasons += [
            f"#{number} is outside the revision scope"
            for number in (issue, blocker)
            if number not in scope
        ]
        owner = ledger.get(issue, {}).get("owner")
        if owner and owner != by:
            reasons.append(f"#{issue} is held by {owner}")
        if change["op"] == "add":
            reasons += [
                f"#{number} is not authorized yet"
                for number in (issue, blocker)
                if number not in ledger
                or not lifecycle.state(ledger[number])["authorized"]
            ]
        elif (
            lifecycle.state(ledger.get(blocker, {}))["state"] == lifecycle.READY
        ):
            reasons.append(f"#{blocker} awaits verification")
    return list(dict.fromkeys(reasons))


def _contradicted(history: dict, changes: list[dict]) -> list[str]:
    """Names the edges this plan version has already revised too often."""
    flips = history.get("flips", {})
    found = []
    for change in changes:
        count = flips.get(f"{change['issue']}:{change['blocker']}", 0)
        if count >= MAX_FLIPS:
            found.append(
                f"#{change['issue']} waits on #{change['blocker']} was "
                f"already revised {count} times under this plan version"
            )
    return found


def _settle(
    directory: Path, history: dict, proposal: dict, operator: bool
) -> None:
    """Applies one proposal atomically, or records why it is held.

    The caller holds the plan lock. The ledger is written once, under its own
    lock, with the proposal's identifier among the revisions it records, so
    a proposal whose ledger write landed before its plan record is recorded
    as accepted on replay rather than applied twice.

    Args:
        directory: Private state directory for the common repository.
        history: Recorded plan versions, updated in place on acceptance.
        proposal: Proposal to settle, updated in place.
        operator: Whether the operator is acting, which applies the proposal
            whatever the envelope says and authorizes the issues it adds.

    Raises:
        BridgeError: If the resulting graph is invalid or the ledger cannot
            be locked. Nothing is written in that case.
    """
    changes = proposal["changes"]
    with lock(directory / "issues.lock", timeout=1):
        state = snapshot(directory)
        applied = state.get("plan_revisions", [])
        if proposal["id"] not in applied:
            graph = _revised(state, changes)
            proposal["effect"] = _effect(state, graph)
            if not operator:
                contradicted = _contradicted(history, changes)
                held = [
                    *contradicted,
                    *_held(history, state, changes, proposal["by"]),
                ]
                if held:
                    proposal.update(
                        status=ESCALATED if contradicted else PENDING,
                        held=held,
                    )
                    return
            for change in changes:
                state["issues"].setdefault(change["issue"], _blank())
                if change["op"] == "add":
                    record = state["issues"].setdefault(
                        change["blocker"], _blank()
                    )
                    if operator:
                        lifecycle.authorize(record)
                state["issues"][change["issue"]]["blocked_by"] = sorted(
                    graph[change["issue"]], key=int
                )
            state["plan_revisions"] = [*applied, proposal["id"]][
                -MAX_APPLIED_IDS:
            ]
            state["revision"] += 1
            write_json(directory / "issues.json", state)
    _accept(history, proposal, operator)


def _accept(history: dict, proposal: dict, operator: bool) -> None:
    """Records an applied proposal as a new plan version."""
    previous = history["versions"][-1]
    dependencies = {
        issue: list(blockers)
        for issue, blockers in previous["dependencies"].items()
    }
    for change in proposal["changes"]:
        waits = dependencies.setdefault(change["issue"], [])
        if change["op"] == "add" and change["blocker"] not in waits:
            waits.append(change["blocker"])
        elif change["blocker"] in waits:
            waits.remove(change["blocker"])
    dependencies = {
        issue: sorted(waits, key=int)
        for issue, waits in dependencies.items()
        if waits or issue in previous["dependencies"]
    }
    now = time.time()
    history["versions"] = [
        *history["versions"],
        {
            "at": now,
            "by": proposal["by"],
            "name": previous["name"],
            "digest": hashlib.sha256(
                json.dumps(dependencies, sort_keys=True).encode()
            ).hexdigest(),
            "dependencies": dependencies,
            "groups": previous["groups"],
            "envelope": previous.get("envelope"),
            "added": [
                (item["issue"], item["blocker"])
                for item in proposal["changes"]
                if item["op"] == "add"
            ],
            "removed": [
                (item["issue"], item["blocker"])
                for item in proposal["changes"]
                if item["op"] == "remove"
            ],
            "proposal": proposal["id"],
        },
    ][-MAX_VERSIONS:]
    history["revision"] += 1
    history["accepted"] = [*history.get("accepted", []), proposal["id"]][
        -MAX_APPLIED_IDS:
    ]
    flips = history.setdefault("flips", {})
    for item in proposal["changes"]:
        key = f"{item['issue']}:{item['blocker']}"
        flips[key] = flips.get(key, 0) + 1
    if not operator:
        history["automatic"] = history.get("automatic", 0) + 1
    proposal.update(
        status=ACCEPTED,
        held=[],
        decided_by=roster.OPERATOR if operator else "envelope",
        decided_at=now,
        version=history["revision"],
    )
    _expire(history)


def _retain(history: dict) -> None:
    """Drops the oldest settled proposals past the retention bound."""
    proposals = history["proposals"]
    settled = [
        key for key, item in proposals.items() if item["status"] not in OPEN
    ]
    for key in settled[: max(len(proposals) - MAX_PROPOSALS, 0)]:
        del proposals[key]


def _expire(history: dict) -> None:
    """Marks every open proposal written against an older version stale.

    Approval refuses such a proposal anyway, so leaving it open would only
    hold one of the `MAX_PENDING` places and keep it in the problems view
    until the operator decided something that can no longer apply.
    """
    now = time.time()
    for item in history.get("proposals", {}).values():
        if item["status"] in OPEN and item["base"] != history["revision"]:
            item.update(
                status=STALE,
                decided_at=now,
                note=(
                    f"the plan moved from version {item['base']} to "
                    f"{history['revision']}; propose again against it"
                ),
            )


def _landed(directory: Path, identity: str) -> bool:
    """Reports whether the ledger already holds one proposal's changes."""
    return identity in snapshot(directory).get("plan_revisions", [])


def propose(
    directory: Path,
    by: str,
    base: int,
    add: list[str],
    remove: list[str],
    reason: str,
    evidence: list[str],
) -> dict:
    """Files a revision of the applied plan's dependency edges.

    A revision is separate from `apply`: it may add a discovered
    prerequisite or remove an obsolete edge, and it names the plan version
    it was written against. The operator's own proposal is applied at once.
    A lane's proposal is applied at once only when every change sits inside
    the envelope the applied plan's `[revisions]` table defines; otherwise
    it is kept, changing nothing, until the operator approves or rejects it.
    An edge this plan version has already revised `MAX_FLIPS` times is
    escalated rather than applied, so contradictory revisions end with the
    operator instead of looping. Either way a revision only changes edges:
    it never claims, releases, completes or verifies work.

    Args:
        directory: Private state directory for the common repository.
        by: Participant filing the proposal, or the operator.
        base: Plan version the proposal was written against.
        add: `ISSUE:BLOCKER` edges to record.
        remove: `ISSUE:BLOCKER` edges to drop.
        reason: Why the plan should change.
        evidence: Bounded supporting observations.

    Returns:
        The proposal record, marked `replayed` when an identical proposal
        from the same participant against the same version already exists.

    Raises:
        BridgeError: If no plan is applied, the base version is stale and
            the ledger does not already hold the proposal, a change or its
            evidence is malformed or out of bounds, the
            resulting graph is invalid, or too many proposals are open.
    """
    changes = _changes(add, remove)
    reason, evidence = _grounds(reason, evidence)
    identity = hashlib.sha256(
        json.dumps(
            {"base": base, "by": by, "changes": changes}, sort_keys=True
        ).encode()
    ).hexdigest()[:16]
    with lock(directory / "plan.lock", timeout=1):
        history = recorded(directory)
        proposals = history.setdefault("proposals", {})
        if identity in proposals:
            return {**proposals[identity], "replayed": True}
        if identity in history.get("accepted", []):
            return {"id": identity, "status": ACCEPTED, "replayed": True}
        if not history["versions"]:
            raise BridgeError(
                "No plan is applied; apply one with "
                "`agent-parley plan apply FILE` before revising it."
            )
        landed = _landed(directory, identity)
        if base != history["revision"] and not landed:
            raise BridgeError(
                f"The proposal names plan version {base}, but the plan is at "
                f"version {history['revision']}; read `agent-parley plan "
                "proposals` and propose against the current version."
            )
        proposal = {
            "id": identity,
            "base": base,
            "by": by,
            "at": time.time(),
            "reason": reason,
            "evidence": evidence,
            "changes": changes,
            "status": PENDING,
            "held": [],
            "effect": {"waits": [], "frees": []},
            "decided_by": None,
            "decided_at": None,
            "note": "",
            "version": None,
        }
        if landed:
            _accept(history, proposal, by == roster.OPERATOR)
        else:
            _settle(directory, history, proposal, by == roster.OPERATOR)
        if proposal["status"] in OPEN and (
            sum(item["status"] in OPEN for item in proposals.values())
            >= MAX_PENDING
        ):
            raise BridgeError(
                f"{MAX_PENDING} revision proposals already await the "
                "operator; nothing was recorded."
            )
        proposals[identity] = proposal
        _retain(history)
        write_json(directory / PLAN, history)
    return {**proposal, "replayed": False}


def decide(directory: Path, identity: str, approve: bool, note: str) -> dict:
    """Approves or rejects one open proposal as the operator.

    Approval validates the whole resulting graph again and applies the
    proposal atomically, authorizing any prerequisite it adds; it is refused
    as stale when the plan has moved past the proposal's base version, and
    rejected with the reason when the graph is no longer valid. An approval
    whose ledger write landed before a crash is recorded as accepted
    whatever the plan version is now, and a busy ledger lock leaves the
    proposal open for a retry instead of rejecting it. Deciding a proposal
    the same way twice changes nothing.

    Args:
        directory: Private state directory for the common repository.
        identity: Proposal identifier.
        approve: Whether to approve rather than reject.
        note: Operator's bounded explanation, kept with the decision.

    Returns:
        The proposal record, marked `replayed` for a repeated decision.

    Raises:
        BridgeError: If no such proposal exists, it was already decided the
            other way, or the note is too long.
        Transient: If the ledger lock stays busy; nothing is recorded.
    """
    note = note.strip()
    if len(note) > MAX_REASON:
        raise BridgeError(f"A decision note holds at most {MAX_REASON} chars.")
    wanted = ACCEPTED if approve else REJECTED
    with lock(directory / "plan.lock", timeout=1):
        history = recorded(directory)
        proposal = history.get("proposals", {}).get(identity)
        if proposal is None:
            raise BridgeError(
                f"No revision proposal {identity}; list them with "
                "`agent-parley plan proposals`."
            )
        if proposal["status"] == wanted:
            return {**proposal, "replayed": True}
        landed = approve and _landed(directory, identity)
        if proposal["status"] not in OPEN and not landed:
            raise BridgeError(
                f"Proposal {identity} is already {proposal['status']}."
            )
        settled = {
            "decided_by": roster.OPERATOR,
            "decided_at": time.time(),
            "note": note,
        }
        if not approve:
            proposal.update(status=REJECTED, **settled)
        elif landed:
            _accept(history, proposal, True)
            proposal["note"] = note
        elif proposal["base"] != history["revision"]:
            proposal.update(
                settled,
                status=STALE,
                note=(
                    f"the plan moved from version {proposal['base']} to "
                    f"{history['revision']}; propose again against it"
                ),
            )
        else:
            try:
                _settle(directory, history, proposal, True)
                proposal["note"] = note
            except Transient:
                raise
            except BridgeError as exc:
                proposal.update(settled, status=REJECTED, note=str(exc))
        write_json(directory / PLAN, history)
    return {**proposal, "replayed": False}


def revisions(directory: Path) -> dict:
    """Returns the current plan version, its envelope and every proposal.

    Returns:
        The version a new proposal must name as its base, the envelope, the
        automatic revisions already spent under it, and the retained
        proposals newest first, each marked `current` when its base is the
        current version.
    """
    history = recorded(directory)
    version = history["versions"][-1] if history["versions"] else {}
    return {
        "revision": history["revision"],
        "envelope": version.get("envelope"),
        "automatic": history.get("automatic", 0),
        "proposals": [
            {**item, "current": item["base"] == history["revision"]}
            for item in reversed(list(history.get("proposals", {}).values()))
        ],
    }


def render_proposal(proposal: dict) -> str:
    """Formats one proposal, its changes and what it still waits on.

    Returns:
        A heading line with the identifier, status and proposer, one line per
        edge change, the reason, anything holding it, and the exact command
        that approves an open proposal.
    """
    lines = [
        f"{proposal['id']} {proposal['status']} (by {proposal['by']}, "
        f"base version {proposal['base']})"
    ]
    for change in proposal["changes"]:
        lines.append(
            f"  {change['op']}: #{change['issue']} waits on "
            f"#{change['blocker']}"
        )
    lines.append(f"  reason: {proposal['reason']}")
    lines += [f"  held: {reason}" for reason in proposal["held"]]
    if proposal["note"]:
        lines.append(f"  note: {proposal['note']}")
    if proposal["status"] in OPEN:
        lines.append(f"  approve: agent-parley plan approve {proposal['id']}")
    return "\n".join(lines)


def render_revisions(document: dict) -> str:
    """Formats the plan version, its envelope and every retained proposal.

    Returns:
        The version a proposal must name, the envelope or its absence, and
        each proposal as `render_proposal` formats it.
    """
    bounds = document["envelope"]
    lines = [f"Plan version {document['revision']}."]
    if bounds:
        lines.append(
            "Envelope: "
            + (", ".join(f"#{n}" for n in bounds["scope"]) or "no issues")
            + f"; up to {bounds['max_changes']} edges per revision; "
            f"{document['automatic']} of {bounds['max_revisions']} automatic "
            "revisions used."
        )
    else:
        lines.append("No envelope: every lane proposal waits for the operator.")
    lines += [render_proposal(item) for item in document["proposals"]]
    return "\n".join(lines)


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
