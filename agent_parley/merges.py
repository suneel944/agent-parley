"""Gates, previews and ordering for integrating lane branches into the base."""

from __future__ import annotations

import json
import shlex
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

from agent_parley import lifecycle, policy, process, roster
from agent_parley.checkpoints import (
    activity,
    current_branch,
    lane_branch,
    participant_liveness,
)
from agent_parley.state import BridgeError, write_json
from agent_parley.worktrees import GIT_SECONDS, drift, git, has_branch

INTEGRATION_RECORD = "integration.json"
"""Project state file holding the one integration the base has not verified."""
REPAIR_ATTEMPTS = 3
"""Integration attempts one recorded recovery allows before it escalates."""
MAX_DIAGNOSTIC = 400
"""Characters of failure text a recovery record keeps."""
CONFLICT = "conflict"
GATE_FAILED = "gate failed"
INTERRUPTED = "interrupted"
INTEGRATION_KINDS = (CONFLICT, GATE_FAILED, INTERRUPTED)
VERIFY_RECOVERY = "agent-parley participant merge --verify-recovery"
"""Operator command that verifies the base as it stands and clears a record."""


def session_busy(name: str) -> str:
    """Builds the refusal used while a participant still holds a session.

    Args:
        name: Participant that owns the lane.

    Returns:
        The message reported when that participant is still working.
    """
    return f"{name} has a running session; stop that terminal first."


def attributed_commits(root: Path, base: str, branch: str) -> list[str]:
    """Lists the commits a lane would integrate that claim assistant authorship.

    A hook is a tool-level check and a session can reach Git another way, so
    integration reads the commits themselves. Subject, body and trailers are
    all examined, because a credit hides as easily in a trailer as in a
    sentence. This is the backstop and it has no skip flag, in the same way
    the verification gate has none.

    Args:
        root: Common repository root, which is always the base checkout.
        base: Commit the range starts after, exclusive.
        branch: Bridge branch the integration would carry.

    Returns:
        One refusal per offending commit, naming that commit and the rule it
        breaks, oldest first.

    Raises:
        BridgeError: If Git cannot read the commit range.
        subprocess.TimeoutExpired: If the read exceeds the command timeout.
    """
    log = git(
        root, "log", "--reverse", "--format=%H%x00%B%x01", f"{base}..{branch}"
    )
    refusals = []
    for entry in log.split("\x01"):
        commit, separator, message = entry.strip().partition("\x00")
        if not separator:
            continue
        rule = policy.matched_rule(message)
        if rule:
            refusals.append(policy.refusal(f"Commit {commit[:12]}", rule))
    return refusals


def merge_blockers(
    root: Path, lane: Path, name: str, branch: str, session: str = ""
) -> Iterator[str]:
    """Yields the conditions that refuse a lane merge, in the order met.

    Yielding lazily lets a merge stop at its first refusal while a preview
    collects every one of them, so both report a condition in the same
    words. The branch is examined first because nothing else can be
    inspected once it is gone, and iteration stops there. Every check reads;
    none writes.

    Args:
        root: Common repository root, which is always the base checkout.
        lane: Assigned bridge worktree belonging to the participant.
        name: Participant that owns the lane.
        branch: Bridge branch to merge into the base checkout.
        session: Session state when the participant still holds a running
            session, or an empty string when no session blocks the merge.

    Yields:
        One refusal message for each condition that is currently unmet.

    Raises:
        BridgeError: If Git cannot read either checkout.
        subprocess.TimeoutExpired: If a read exceeds the command timeout.
    """
    if not has_branch(root, branch):
        yield (
            f"Branch {branch} no longer exists. Recover it from the reflog, "
            f"or retire {name}, follow any kept-branch recovery instructions, "
            "then add it again."
        )
        return
    base = current_branch(root)
    if base == branch:
        yield (
            f"The base checkout at {root} is on {branch} itself. Switch it "
            "to the branch that should receive this work, then rerun."
        )
    if base == "<detached HEAD>":
        yield (
            f"The base checkout at {root} is on a detached HEAD. Switch it "
            "to the branch that should receive this work, then rerun."
        )
    git_dir = Path(
        git(root, "rev-parse", "--path-format=absolute", "--git-dir")
    )
    quoted = shlex.quote(str(root))
    if (git_dir / "MERGE_HEAD").exists():
        yield (
            f"The base checkout at {root} is already merging. Finish it with "
            f"`git -C {quoted} merge --continue`, or undo it with `git -C "
            f"{quoted} merge --abort`, then rerun."
        )
    if git(root, "status", "--porcelain"):
        yield (
            f"The base checkout at {root} has uncommitted changes. Commit or "
            "preserve them first; merge never discards work."
        )
    if lane.exists() and git(lane, "status", "--porcelain"):
        yield (
            f"{name} has uncommitted changes that {branch} does not carry. "
            "Commit them in the lane first; merge only ever merges commits."
        )
    yield from attributed_commits(root, "HEAD", branch)
    if session:
        yield session_busy(name)


def merge_preview(
    root: Path, lane: Path, name: str, branch: str, session: str
) -> str:
    """Reports what a lane merge would bring in and what would refuse it.

    The preview only reads: it records no merge commit, moves no branch,
    leaves the index and working tree of both checkouts alone, and never
    takes the participant's session lock, so previewing a lane while its
    agent still works cannot make that session fail. It attempts no trial
    merge either, so a preview that names no refusal says the merge is not
    currently refused, never that it would apply without conflicts.

    The file summary keeps the leading space Git indents every one of its
    rows with, which reading stripped command output would otherwise take
    from the first row alone and misalign the columns.

    Args:
        root: Common repository root, which is always the base checkout.
        lane: Assigned bridge worktree belonging to the participant.
        name: Participant that owns the lane.
        branch: Bridge branch the merge would integrate.
        session: Session state when the participant holds a running session,
            or an empty string when no session blocks the merge.

    Returns:
        An account of the commits the merge would carry, the files they
        change, and every condition that would refuse the merge right now.

    Raises:
        BridgeError: If Git cannot read the base checkout.
        subprocess.TimeoutExpired: If a read exceeds the command timeout.
    """
    header = f"Preview only: nothing merged, and {root} is unchanged."
    refused = "The merge would be refused right now:"
    if not has_branch(root, branch):
        missing = next(merge_blockers(root, lane, name, branch, session), "")
        return (
            f"{header}\n{refused}\n- {missing}\n"
            "Nothing further can be previewed while the branch is gone."
        )
    base = current_branch(root)
    pending = git(root, "log", "--oneline", f"HEAD..{branch}")
    report = [header]
    if pending:
        report += [
            f"Merging {branch} into {base} would bring in "
            f"{len(pending.splitlines())} commits:",
            pending,
            "Those commits change these files, relative to the merge base:",
            " " + git(root, "diff", "--stat", f"HEAD...{branch}"),
        ]
    else:
        report.append(f"{base} already contains every commit on {branch}.")
    blockers = []
    actual = lane_branch(lane)
    if actual != branch:
        blockers.append(drift(name, {"branch": branch}, actual))
    blockers += merge_blockers(root, lane, name, branch, session)
    if blockers:
        report.append(refused)
        report += [f"- {blocker}" for blocker in blockers]
        report.append(
            f"Clear those, then run `agent-parley participant merge {name}`."
        )
    elif pending:
        report.append(
            f"Nothing refuses this merge; it would land on {base}. The "
            "preview merges nothing, so it cannot predict conflicts."
        )
    return "\n".join(report)


def merge_branch(
    root: Path,
    lane: Path,
    name: str,
    branch: str,
    source_commit: str = "",
) -> str:
    """Merges one lane's bridge branch into the base checkout.

    The merge runs in the base checkout, never inside another lane, and
    always records a merge commit so the integration stays auditable. It
    reads the lane only to refuse merging a branch that does not yet carry
    the lane's work. It never resets, cleans, stashes or force-switches, and
    a conflict is left in the working tree for the operator to resolve.

    The merge itself is bounded by the same timeout every other Git call
    here carries, so a merge hook or a prompt that never returns stops the
    merge instead of pinning the command that asked for it.

    Args:
        root: Common repository root, which is always the base checkout.
        lane: Assigned bridge worktree belonging to the participant.
        name: Participant that owns the lane.
        branch: Bridge branch to merge into the base checkout.
        source_commit: Immutable reported commit to merge instead of the
            moving branch name.

    Returns:
        An account of what was merged.

    Raises:
        BridgeError: If either checkout cannot be merged from, if the merge
            stopped on conflicts that only the operator can resolve, or if
            the merge ran past its timeout and was stopped.
        subprocess.TimeoutExpired: If a preliminary read exceeds its timeout.
    """
    blocker = next(merge_blockers(root, lane, name, branch), "")
    if blocker:
        raise BridgeError(blocker)
    base = current_branch(root)
    git_dir = Path(
        git(root, "rev-parse", "--path-format=absolute", "--git-dir")
    )
    quoted = shlex.quote(str(root))
    target = source_commit or branch
    pending = git(root, "log", "--oneline", f"HEAD..{target}")
    if not pending:
        return f"{base} already contains every commit on {branch}."
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "merge",
                "--no-ff",
                "-m",
                f"Merge lane branch {branch}",
                target,
            ],
            capture_output=True,
            text=True,
            timeout=GIT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise BridgeError(
            f"Merging {branch} into {base} was still running after "
            f"{GIT_SECONDS} seconds and was stopped, so the command did not "
            f"wait for it. A merge hook or a prompt in {root} is the usual "
            f"cause. Check `git -C {quoted} status`, finish or abort whatever "
            f"the merge left, then run `agent-parley participant merge "
            f"{name}` again."
        ) from None
    if result.returncode:
        if not (git_dir / "MERGE_HEAD").exists():
            raise BridgeError(
                result.stderr.strip()
                or result.stdout.strip()
                or f"Merging {branch} into {base} failed."
            )
        conflicted = git(root, "diff", "--name-only", "--diff-filter=U")
        raise BridgeError(
            f"Merging {branch} into {base} stopped on conflicts and the "
            f"merge is now in progress in {root}:\n{conflicted}\n"
            f"Resolve those paths and run `git -C {quoted} merge --continue`, "
            f"or run `git -C {quoted} merge --abort` to leave {base} exactly "
            "as it was. Agent Parley never resolves a conflict for you."
        )
    merged = len(pending.splitlines())
    return (
        f"Merged {branch} into {base} as a merge commit, carrying {merged} "
        f"commits from {name}. The lane and its branch are unchanged; retire "
        f"{name} separately when the lane is no longer needed."
    )


def lane_dependencies(
    state: dict, candidates: dict[str, list[str]]
) -> dict[str, list[str]]:
    """Maps each candidate lane to the candidate lanes it waits on.

    The edges are the advisory dependencies the ledger already records. An
    edge that leaves the candidate set constrains nothing here, because the
    lane holding the other end is not being integrated in this run.

    Args:
        state: Published issue ledger.
        candidates: Participants mapped to the issues each one holds.

    Returns:
        One entry per candidate, naming the other candidates whose issues its
        own issues wait on.
    """
    holder = {
        issue: name for name, issues in candidates.items() for issue in issues
    }
    return {
        name: sorted(
            {
                holder[blocker]
                for issue in issues
                for blocker in state["issues"]
                .get(issue, {})
                .get("blocked_by", [])
                if holder.get(blocker, name) != name
            }
        )
        for name, issues in candidates.items()
    }


def lane_session(directory: Path, name: str) -> str:
    """Names the running session that blocks a lane merge, if any.

    The recorded session process is read rather than the session lock taken,
    so reading a lane while its agent still works cannot make that session
    fail.

    Args:
        directory: Private state directory for the common repository.
        name: Participant that owns the lane.

    Returns:
        The lane's liveness when a session is running, an empty string
        otherwise.
    """
    state = activity(directory, name)
    running = process.alive(
        state.get("session_pid"), state.get("session_ticks")
    )
    return participant_liveness(directory, name) if running else ""


def reported_ready(directory: Path) -> set[str]:
    """Names every participant whose latest report is the ready state.

    A reported state is a lane's own account of its work. It is neither
    review nor independent verification, and this reads it without changing
    it. A plan can be applied and shown before any participant exists, so a
    repository with no manifest yet reports nobody rather than refusing.

    Args:
        directory: Private state directory for the common repository.

    Returns:
        The participants that currently report ready.
    """
    if not (directory / "project.json").exists():
        return set()
    return {
        name
        for name in roster.read(directory)["participants"]
        if activity(directory, name).get("outcome") == "ready"
    }


def ready_lanes(
    directory: Path, data: dict, state: dict
) -> dict[str, list[str]]:
    """Maps every lane whose latest report is ready to the issues it holds.

    Args:
        directory: Private state directory for the common repository.
        data: Project manifest holding the roster.
        state: Published issue ledger.

    Returns:
        One entry per participant whose latest report is the ready state,
        carrying the issues that participant currently holds.
    """
    return {
        name: sorted(
            (
                number
                for number, record in state["issues"].items()
                if record["owner"] == name
            ),
            key=int,
        )
        for name in sorted(data["participants"])
        if activity(directory, name).get("outcome") == "ready"
    }


def group_lanes(
    data: dict, state: dict, name: str, listed: list[str]
) -> dict[str, list[str]]:
    """Maps each lane holding a member of one group to the members it holds.

    Args:
        data: Project manifest holding the roster.
        state: Published issue ledger.
        name: Group named by the applied plan.
        listed: Issues the group names.

    Returns:
        One entry per participant holding at least one member.

    Raises:
        BridgeError: If a member is unclaimed, or is held by somebody who is
            not a participant in this project.
    """
    lanes: dict[str, list[str]] = {}
    for issue in listed:
        owner = state["issues"].get(issue, {}).get("owner")
        if not owner:
            raise BridgeError(
                f"Group {name} cannot be integrated: #{issue} is unclaimed. "
                "Every member is integrated from the lane that holds it."
            )
        if owner not in data["participants"]:
            raise BridgeError(
                f"Group {name} cannot be integrated: #{issue} is held by "
                f"{owner}, which is not a participant in this project."
            )
        lanes.setdefault(owner, []).append(issue)
    return lanes


def lane_refusals(
    root: Path, directory: Path, participant: dict, name: str
) -> list[str]:
    """Collects every condition that refuses one lane's merge right now.

    The conditions are exactly the ones `participant merge --preview` lists,
    read the same way and in the same words, so a bulk preflight can never
    admit a lane the single-lane command would refuse. Every check reads; the
    participant's session lock is never taken.

    Args:
        root: Common repository root, which is always the base checkout.
        directory: Private state directory for the common repository.
        participant: Roster record holding the lane and its assigned branch.
        name: Participant that owns the lane.

    Returns:
        One refusal message per unmet condition, empty when nothing refuses
        the merge at this moment.
    """
    session = lane_session(directory, name)
    lane = Path(participant["lane"])
    refusals = []
    actual = lane_branch(lane)
    if actual != participant["branch"]:
        refusals.append(drift(name, participant, actual))
    refusals += merge_blockers(root, lane, name, participant["branch"], session)
    try:
        held = integration_record(directory)
    except BridgeError as unreadable:
        refusals.append(str(unreadable))
    else:
        if (
            held is not None
            and integration_owner(directory, held) != name
            and integration_holds(root, held)
        ):
            refusals.append(integration_hold(root, directory, held))
    return refusals


def group_refusal(
    group: str, sequence: list[str], refusals: dict[str, list[str]]
) -> str:
    """Reports why a whole group was refused before anything was merged.

    Args:
        group: Group named by the applied plan.
        sequence: Members' lanes in dependency order.
        refusals: Conditions currently refusing each lane.

    Returns:
        Every refusing condition of every member, and a statement that the
        preflight admits a group whole or not at all.
    """
    lines = [
        f"Group {group} is refused as a whole, so nothing was merged and "
        "the base checkout is unchanged."
    ]
    for name in sequence:
        for refusal in refusals[name]:
            lines.append(f"- {name}: {refusal}")
    lines.append(
        "A group preflight admits every member or none. Clear these, then "
        "rerun; a refused member is never followed by a member that waits "
        "on it."
    )
    return "\n".join(lines)


def unattempted(name: str, waits: dict[str, list[str]], stopped: str) -> str:
    """Reports why one lane was left alone after an ordered run stopped."""
    return (
        f"not attempted; it waits on {stopped}."
        if stopped in waits.get(name, [])
        else f"not attempted; the run stopped at {stopped}."
    )


def outside_prerequisites(
    state: dict, candidates: dict[str, list[str]]
) -> list[str]:
    """Names the prerequisites of a selection that lie outside it.

    A selection narrows what a run attempts; it never lifts a recorded
    dependency. Every issue a selected lane holds is read for the issues it
    waits on, and each one that no selected lane holds is named here. The
    ledger records no completion, so a prerequisite nobody holds is reported
    as released rather than as finished work.

    Args:
        state: Published issue ledger.
        candidates: Selected participants mapped to the issues each holds.

    Returns:
        One line per prerequisite outside the selection, ordered by issue.
    """
    held = {issue for issues in candidates.values() for issue in issues}
    waited = {
        blocker
        for issues in candidates.values()
        for number in issues
        for blocker in state["issues"].get(number, {}).get("blocked_by", [])
        if blocker not in held
    }
    lines = []
    for issue in sorted(waited, key=int):
        owner = state["issues"].get(issue, {}).get("owner", "")
        satisfied = (
            f"held by {owner}, so it is not satisfied here"
            if owner
            else "released, so no lane still holds it"
        )
        lines.append(
            f"#{issue} is a prerequisite outside this selection, {satisfied}."
        )
    return lines


def diagnostic(text: str) -> str:
    """Bounds failure text to what a recovery record keeps.

    Args:
        text: Failure text as raised.

    Returns:
        The last `MAX_DIAGNOSTIC` characters, which name the outcome.
    """
    text = text.strip()
    if len(text) <= MAX_DIAGNOSTIC:
        return text
    return "..." + text[-MAX_DIAGNOSTIC:]


def integration_record(directory: Path) -> dict | None:
    """Reads the one integration the project's base has not verified.

    An integration merges before its post-merge gate runs, so from the
    moment a merge starts until that gate passes, the base may carry a
    result nobody verified. That span is written to `INTEGRATION_RECORD`
    before the merge and removed only when the exact resulting commit
    passes, so a crash, a restart or a failed gate leaves it standing.

    Args:
        directory: Private state directory for the common repository.

    Returns:
        The recorded integration, or None when the base carries none.

    Raises:
        BridgeError: If the record exists but cannot be read, which holds
            integration exactly as a readable record would.
    """
    path = directory / INTEGRATION_RECORD
    try:
        recorded = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise BridgeError(
            f"The integration recovery record at {path} cannot be read "
            f"({exc}), so no lane is integrated until it can. Inspect the "
            "file; it is written only by `participant merge`."
        ) from None
    if (
        not isinstance(recorded, dict)
        or recorded.get("kind") not in INTEGRATION_KINDS
    ):
        raise BridgeError(
            f"The integration recovery record at {path} is not a recovery "
            "record, so no lane is integrated until it is. Inspect the file; "
            "it is written only by `participant merge`."
        )
    return recorded


def record_integration(directory: Path, entry: dict) -> dict:
    """Publishes one observation of an unverified integration.

    Every observation carries the attempt it belongs to. One from an
    earlier attempt than the record holds is dropped, so a late or repeated
    observation never rewinds a newer one, and recording the same
    observation twice leaves the same record.

    Args:
        directory: Private state directory for the common repository.
        entry: Complete record for one attempt.

    Returns:
        The record that stands after the observation.

    Raises:
        BridgeError: If the standing record cannot be read.
        OSError: If the record cannot be written.
    """
    current = integration_record(directory)
    if current is not None and current["attempt"] > entry["attempt"]:
        return current
    entry = {**entry, "updated_at": time.time()}
    write_json(directory / INTEGRATION_RECORD, entry)
    return entry


def withdraw_integration(
    directory: Path, entry: dict, previous: dict | None
) -> None:
    """Puts back the record an attempt replaced when it merged nothing.

    Args:
        directory: Private state directory for the common repository.
        entry: Record the refused attempt wrote.
        previous: Record that stood before it, or None when none did.

    Raises:
        BridgeError: If the standing record cannot be read.
        OSError: If the record cannot be written.
    """
    current = integration_record(directory)
    if current is None or current["attempt"] != entry["attempt"]:
        return
    if previous is None:
        (directory / INTEGRATION_RECORD).unlink(missing_ok=True)
    else:
        write_json(directory / INTEGRATION_RECORD, previous)


def integration_owner(directory: Path, record: dict) -> str:
    """Names the one lane that may repair a recorded integration.

    Repair follows ownership rather than the lane that merged: when the
    integration carried an issue, whichever lane holds that issue now, by
    claim, handoff or approved recovery, repairs it, and nobody does while
    the issue is unheld or already complete. An integration that carried no
    issue stays with the lane that merged it while that lane is an active
    participant. When no lane may repair it, only the operator's
    `VERIFY_RECOVERY` releases the base.

    Args:
        directory: Private state directory for the common repository.
        record: Recorded unverified integration.

    Returns:
        The lane that may retry the integration, or empty when none may.
    """
    if not record.get("issue"):
        try:
            lanes = roster.read(directory)["participants"]
        except (BridgeError, OSError):
            return ""
        lane = lanes.get(str(record["lane"]))
        if lane is None or roster.retired(lane):
            return ""
        return str(record["lane"])
    try:
        ledger = json.loads((directory / "issues.json").read_text())
    except (OSError, ValueError):
        return ""
    held = ledger.get("issues", {}).get(str(record["issue"])) or {}
    if lifecycle.state(held)["state"] == lifecycle.COMPLETE:
        return ""
    return str(held.get("owner") or "")


def merging(root: Path) -> bool:
    """Reports whether the base checkout holds a merge in progress."""
    git_dir = Path(
        git(root, "rev-parse", "--path-format=absolute", "--git-dir")
    )
    return (git_dir / "MERGE_HEAD").exists()


def integration_holds(root: Path, record: dict) -> bool:
    """Reports whether a recorded integration still leaves the base unverified.

    A conflict changes no commit. Once its merge is aborted and the base is
    back at the recorded pre-merge commit, the base is the one verified
    before the attempt, so the record no longer holds other lanes; the
    conflicting claim keeps its repair state in the issue ledger.

    Args:
        root: Common repository root, which is always the base checkout.
        record: Recorded unverified integration.

    Returns:
        Whether integrating onto the base must wait for the record.

    Raises:
        BridgeError: If Git cannot read the base checkout.
    """
    return (
        record["kind"] != CONFLICT
        or merging(root)
        or git(root, "rev-parse", "HEAD") != record["base"]
    )


def unmerged(root: Path) -> bool:
    """Reports whether the base checkout holds paths a merge left unmerged.

    A merge that stopped on a conflict leaves unmerged paths. One killed
    before it finished can leave `MERGE_HEAD` without any, which is an
    interruption rather than a conflict for a lane to resolve.

    Args:
        root: Common repository root, which is always the base checkout.

    Returns:
        Whether any path is still unmerged.

    Raises:
        BridgeError: If Git cannot read the base checkout.
    """
    return bool(git(root, "diff", "--name-only", "--diff-filter=U"))


def clear_integration(directory: Path, attempt: int, result: str) -> bool:
    """Removes the record once one exact attempt's result is verified.

    Only the attempt that is still recorded, at the result it recorded, can
    clear the record, so a verification that finished after a newer attempt
    failed never clears that newer failure.

    Args:
        directory: Private state directory for the common repository.
        attempt: Attempt whose result was verified.
        result: Exact commit that passed the gate, or empty when the attempt
            merged nothing.

    Returns:
        Whether the record was removed.

    Raises:
        BridgeError: If the standing record cannot be read.
    """
    current = integration_record(directory)
    if (
        current is None
        or current["attempt"] != attempt
        or current["result"] != result
    ):
        return False
    (directory / INTEGRATION_RECORD).unlink(missing_ok=True)
    return True


def integration_exhausted(record: dict) -> bool:
    """Reports whether a record has used every attempt it allows."""
    return int(record["attempt"]) >= int(record["limit"])


def integration_remedy(
    root: Path, record: dict, owner: str | None = None
) -> str:
    """Names the one runnable step that moves a recorded integration on.

    Args:
        root: Common repository root, which is always the base checkout.
        record: Recorded unverified integration.
        owner: Lane that may repair it, as `integration_owner` names it, or
            None to name the lane that merged it.

    Returns:
        The step and the command it runs.
    """
    lane = record["lane"] if owner is None else owner
    quoted = shlex.quote(str(root))
    verify = (
        f"Once the base at {root} is fixed or reset by hand, run "
        f"`{VERIFY_RECOVERY}` from the base checkout; it runs the recorded "
        "gate on the base as it stands and clears the record only if it "
        "passes and leaves the tree clean."
    )
    if not lane:
        if not record.get("issue"):
            return (
                f"{record['lane']} is no longer an active participant, so no "
                f"lane may repair this integration. {verify}"
            )
        return (
            f"Issue #{record['issue']} is unheld or already complete, so no "
            "lane may repair its integration. Have a lane claim it with "
            f"`agent-parley issue claim {record['issue']}` in that lane, "
            "report ready there with `agent-parley report --state ready "
            "--summary <change> --evidence <gate>`, then run `agent-parley "
            f"participant merge <that lane>`. Otherwise: {verify}"
        )
    retry = f"`agent-parley participant merge {lane}`"
    if integration_exhausted(record):
        return (
            f"All {record['attempt']} of {record['limit']} attempts are "
            f"used. Inspect `git -C {quoted} show --stat "
            f"{record['result'] or 'HEAD'}` and the gate output, fix the "
            "lane or the base, then run `agent-parley participant merge "
            f"{lane} --renew-recovery` from the base checkout. {verify}"
        )
    if record["kind"] == CONFLICT:
        return (
            f"Resolve it with `git -C {quoted} merge --continue`, or run "
            f"`git -C {quoted} merge --abort` and have {lane} merge the "
            f"base into its branch and report ready again; then run {retry}."
        )
    if record["kind"] == GATE_FAILED:
        return (
            f"{lane} repairs its branch and reports ready again, then run "
            f"{retry}; it verifies the exact repaired result."
        )
    if not record["result"]:
        return (
            f"If `git -C {quoted} status` shows a merge in progress, run "
            f"`git -C {quoted} merge --abort`; then run {retry}."
        )
    return (
        f"Run {retry}; it verifies the base again before anything is "
        "recorded complete."
    )


def integration_hold(root: Path, directory: Path, record: dict) -> str:
    """Refuses integrating another lane onto an unverified base.

    Args:
        root: Common repository root, which is always the base checkout.
        directory: Private state directory for the common repository.
        record: Recorded unverified integration.

    Returns:
        The refusal, naming the recorded integration and its remedy.
    """
    result = record["result"][:12] or "an unfinished merge"
    return (
        f"The base checkout at {root} carries {record['lane']}'s "
        f"integration at {result}, recorded as {record['kind']} on attempt "
        f"{record['attempt']} of {record['limit']}, so no other lane is "
        "integrated onto it until it is verified. "
        + integration_remedy(root, record, integration_owner(directory, record))
    )
