"""Merges lane branches into the base checkout and records decisions on them."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from agent_parley import BridgeError
from agent_parley.lazy import DeferredCallable, deferred, deferred_module
from agent_parley.worktrees import (
    GIT_SECONDS,
    Worktrees,
    drift,
    exact_claim,
    git,
    has_branch,
    session_busy,
    verify_base,
)

if TYPE_CHECKING:
    import shlex
    import subprocess

    from agent_parley import (
        approvals,
        checkpoints,
        issues,
        lifecycle,
        metrics,
        plan,
        policy,
        process,
        roster,
        state,
    )
    from agent_parley.checkpoints import (
        activity,
        current_branch,
        lane_branch,
        participant_liveness,
    )
    from agent_parley.issues import snapshot
    from agent_parley.state import lock
else:
    shlex = deferred_module("shlex")
    subprocess = deferred_module("subprocess")
    approvals = deferred("approvals")
    checkpoints = deferred("checkpoints")
    issues = deferred("issues")
    lifecycle = deferred("lifecycle")
    metrics = deferred("metrics")
    plan = deferred("plan")
    policy = deferred("policy")
    process = deferred("process")
    roster = deferred("roster")
    state = deferred("state")
    activity = DeferredCallable(checkpoints, "activity")
    current_branch = DeferredCallable(checkpoints, "current_branch")
    lane_branch = DeferredCallable(checkpoints, "lane_branch")
    participant_liveness = DeferredCallable(checkpoints, "participant_liveness")
    snapshot = DeferredCallable(issues, "snapshot")
    lock = DeferredCallable(state, "lock")


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


class Integration(Worktrees):
    """Merges lanes, singly or in dependency order, and records decisions."""

    def merge(self, repo: Path, name: str) -> str:
        """Merges one participant's bridge branch into the base checkout.

        Args:
            repo: Any checkout of the target repository.
            name: Participant whose bridge branch is merged.

        Returns:
            An account of what was merged.

        Raises:
            BridgeError: If the lane drifted, if the participant holds a
                running session, if the project requires an operator approval
                the lane's current ready report does not have, if the
                repository's verification command fails, or if the merge
                cannot complete unattended.
            subprocess.TimeoutExpired: If verification exceeds its timeout.
        """
        root, directory = self.project(repo, create=False)
        roster.read(directory)
        with lock(directory / "setup.lock"):
            data = self._project(root, directory, verify={name})
            participant = data["participants"].get(name)
            if participant is None:
                raise BridgeError(
                    f"{name} is not a participant in this project; "
                    "run agent-parley participant list."
                )
            return self._integrate_lane(root, directory, data, name)

    def _integrate_lane(
        self, root: Path, directory: Path, data: dict, name: str
    ) -> str:
        """Runs the gate and merges one lane while its session is excluded.

        Every integration path goes through this step, so a lane merged in a
        group or in a bulk run is merged on exactly the terms the single-lane
        command merges it on.

        Args:
            root: Common repository root, which is always the base checkout.
            directory: Private state directory for the common repository.
            data: Project manifest holding the roster and the gate command.
            name: Participant whose bridge branch is merged.

        Returns:
            An account of what was merged.

        Raises:
            BridgeError: If the project requires an operator approval the
                lane's current ready report does not have, if the gate fails,
                or if the merge cannot complete unattended.
        """
        participant = data["participants"][name]
        with lock(directory / f"{name}.session.lock", session_busy(name)):
            self._require_approval(directory, data, name, "merge")
            claim = exact_claim(directory, name)
            source_commit = ""
            if claim["issue"] is not None:
                record = snapshot(directory)["issues"][str(claim["issue"])]
                execution = lifecycle.state(record)
                if execution["state"] != lifecycle.READY:
                    raise BridgeError(
                        f"Issue #{claim['issue']} is not reported ready."
                    )
                source_commit = (
                    execution.get("source_commit") or execution["commit"]
                )
            if data["verify"]:
                base_commit = git(root, "rev-parse", "HEAD")
                verify_base(root, data["verify"])
                if git(root, "rev-parse", "HEAD") != base_commit or git(
                    root, "status", "--porcelain"
                ):
                    raise BridgeError(
                        "Pre-merge verification changed the base checkout; "
                        "nothing was merged or recorded complete."
                    )
            lane = Path(participant["lane"])
            if (
                source_commit
                and git(lane, "rev-parse", "HEAD") != source_commit
            ):
                raise BridgeError(
                    f"{name} committed since issue #{claim['issue']} was "
                    "reported ready; record a new report before merging."
                )
            merged = merge_branch(
                root,
                lane,
                name,
                participant["branch"],
                source_commit,
            )
            integrated = git(root, "rev-parse", "HEAD")
            if data["verify"]:
                verify_base(root, data["verify"], integrated=True)
                if git(root, "rev-parse", "HEAD") != integrated:
                    raise BridgeError(
                        "The base commit changed while verification ran; "
                        "the integration stands but is not recorded complete."
                    )
                if git(root, "status", "--porcelain"):
                    raise BridgeError(
                        "Verification changed repository content; "
                        "the integration stands but is not recorded complete."
                    )
            if claim["issue"] is not None and claim["claim_id"]:
                lifecycle.complete(
                    directory,
                    str(claim["issue"]),
                    claim["claim_id"],
                    integrated,
                    data["verify"],
                    source_commit,
                )
            metrics.record_report(
                directory,
                name,
                {
                    "kind": "integration",
                    "action": "merge",
                    **claim,
                },
            )
            return merged

    def preview_merge(self, repo: Path, name: str) -> str:
        """Reports what merging a participant's lane would do, changing nothing.

        The preview deliberately never takes the participant's session lock,
        because previewing a lane while its agent still works is the ordinary
        case and taking that lock would make a concurrent launch fail. A
        running session is read from the recorded session process instead, the
        same way liveness reporting reads it. Only the shared setup lock is
        held, and only to read the project manifest.

        Args:
            repo: Any checkout of the target repository.
            name: Participant whose bridge branch the preview examines.

        Returns:
            An account of the commits the merge would carry, the files they
            change, and every condition that would refuse the merge right now.

        Raises:
            BridgeError: If the repository has no project, if the participant
                is unknown, or if a checkout cannot be read.
        """
        root, directory = self.project(repo)
        with lock(directory / "setup.lock"):
            data = roster.read(directory)
            participant = data["participants"].get(name)
            if participant is None:
                raise BridgeError(
                    f"{name} is not a participant in this project; "
                    "run agent-parley participant list."
                )
            session = lane_session(directory, name)
        return merge_preview(
            root,
            Path(participant["lane"]),
            name,
            participant["branch"],
            session,
        )

    def _integration_candidates(
        self,
        directory: Path,
        data: dict,
        state: dict,
        group: str,
        lanes: Sequence[str],
    ) -> tuple[str, dict[str, list[str]]]:
        """Names the lanes one bulk merge considers and what it reports under.

        Args:
            directory: Private state directory for the common repository.
            data: Project manifest holding the roster.
            state: Published issue ledger.
            group: Group of the applied plan; every ready lane when empty.
            lanes: Lanes a selector matched; unrestricted when empty.

        Returns:
            The subject the run reports under and the candidate lanes mapped
            to the issues each one holds.

        Raises:
            BridgeError: If a named group holds a member no participant owns.
        """
        if group:
            return f"Group {group}", group_lanes(
                data, state, group, plan.members(directory, group)
            )
        ready = ready_lanes(directory, data, state)
        if not lanes:
            return "Ready lanes", ready
        chosen = set(lanes)
        return "Selected ready lanes", {
            name: issues for name, issues in ready.items() if name in chosen
        }

    def integration_plan(
        self, repo: Path, group: str = "", lanes: Sequence[str] = ()
    ) -> dict:
        """Orders the lanes a bulk merge would attempt and names its waits.

        The order is the one the run itself uses, read from the same advisory
        dependency edges, so the plan an operator confirms is the run that
        follows. Prerequisites outside the selected set are named with the
        ledger's account of them, because narrowing a selection never lifts a
        recorded dependency.

        Args:
            repo: Any checkout of the target repository.
            group: Group of the applied plan; every ready lane when empty.
            lanes: Lanes a selector matched; unrestricted when empty.

        Returns:
            The subject the run reports under, the candidate lanes in
            dependency order, and one line per prerequisite outside the set.

        Raises:
            BridgeError: If the repository has no project, a group member is
                unheld, or the candidates form a dependency cycle.
        """
        _, directory = self.project(repo, create=False)
        data = roster.read(directory)
        state = snapshot(directory)
        subject, candidates = self._integration_candidates(
            directory, data, state, group, lanes
        )
        return {
            "subject": subject,
            "sequence": plan.order(
                lane_dependencies(state, candidates), "Lane dependencies"
            ),
            "outside": outside_prerequisites(state, candidates),
        }

    def integrate(
        self,
        repo: Path,
        group: str = "",
        preview: bool = False,
        lanes: Sequence[str] = (),
    ) -> str:
        """Integrates several lanes in the order their dependencies imply.

        Candidates are every lane whose latest report is ready, the subset of
        those a lane selector matched, or the lanes holding the members of one
        group of the applied plan. A selector narrows the ready lanes and
        never admits a lane on easier terms. They are ordered
        from the advisory dependency edges the ledger already records, so a
        lane whose issue waits on another is merged after the lane holding
        that issue. A cycle among the candidates is refused and named; it is
        never quietly ordered.

        Every candidate is preflighted with the same conditions
        `participant merge --preview` reports, and each merge then runs
        through the single-lane path, so no lane is integrated on easier terms
        than it would be alone. A group is admitted whole or not at all: one
        refused member leaves the group unmerged. Execution is still ordered
        rather than atomic, so a merge or gate failure part way through stops
        the run and leaves the earlier merge commits in place; the report then
        names what was integrated, what refused and what was not attempted.
        Nothing is ever reset or reverted.

        Args:
            repo: Any checkout of the target repository.
            group: Group of the applied plan to integrate; every ready lane
                when empty.
            preview: Whether to report the plan and every candidate's preview
                without merging anything.
            lanes: Lanes a selector matched, narrowing the ready lanes an
                ungrouped run considers; unrestricted when empty.

        Returns:
            The ordered plan when previewing, otherwise an account of every
            lane that was integrated.

        Raises:
            BridgeError: If the candidates cannot be ordered, if a group is
                refused, or if the run stops on a refusal or a failure, whose
                report names everything already integrated.
        """
        root, directory = self.project(repo, create=False)
        with lock(directory / "setup.lock"):
            data = roster.read(directory)
            state = snapshot(directory)
            subject, candidates = self._integration_candidates(
                directory, data, state, group, lanes
            )
            if not candidates:
                return f"{subject}: no lane to integrate, so nothing merged."
            waits = lane_dependencies(state, candidates)
            sequence = plan.order(waits, "Lane dependencies")
            refusals = {
                name: lane_refusals(
                    root, directory, data["participants"][name], name
                )
                for name in sequence
            }
            if preview:
                return self._integration_preview(
                    root, directory, data, subject, sequence
                )
            if group and any(refusals.values()):
                raise BridgeError(group_refusal(group, sequence, refusals))
            return self._integrate_sequence(
                root, directory, data, subject, sequence, waits, refusals
            )

    def _integration_preview(
        self,
        root: Path,
        directory: Path,
        data: dict,
        subject: str,
        sequence: list[str],
    ) -> str:
        """Reports the ordered plan and every candidate's own preview."""
        report = [
            f"{subject}: {len(sequence)} lanes in dependency order: "
            + ", ".join(sequence)
            + ".",
            "Preview only: nothing is merged and no lane is verified.",
        ]
        for name in sequence:
            participant = data["participants"][name]
            report.append(f"\n{name}:")
            report.append(
                merge_preview(
                    root,
                    Path(participant["lane"]),
                    name,
                    participant["branch"],
                    lane_session(directory, name),
                )
            )
        return "\n".join(report)

    def _integrate_sequence(
        self,
        root: Path,
        directory: Path,
        data: dict,
        subject: str,
        sequence: list[str],
        waits: dict[str, list[str]],
        refusals: dict[str, list[str]],
    ) -> str:
        """Merges an ordered run and reports how far it got."""
        report = [
            f"{subject}: {len(sequence)} lanes in dependency order: "
            + ", ".join(sequence)
            + "."
        ]
        merged: list[str] = []
        stopped = ""
        for name in sequence:
            if stopped:
                report.append(f"- {name}: {unattempted(name, waits, stopped)}")
                continue
            if refusals[name]:
                stopped = name
                report.append(f"- {name}: refused. {refusals[name][0]}")
                continue
            try:
                outcome = self._integrate_lane(root, directory, data, name)
            except BridgeError as failure:
                stopped = name
                report.append(f"- {name}: stopped. {failure}")
                continue
            merged.append(name)
            report.append(f"- {name}: {outcome}")
        report.append(
            f"Integrated {len(merged)} of {len(sequence)} lanes: "
            + (", ".join(merged) or "none")
            + "."
        )
        if stopped:
            raise BridgeError("\n".join(report))
        return "\n".join(report)

    def _decide(
        self, repo: Path, name: str, decision: str, reason: str = ""
    ) -> str:
        """Records one operator decision about a lane's ready report.

        The command runs from the base checkout only. Running it inside an
        assigned worktree is refused, so the lane's own command line cannot
        approve the lane's own work. That is this product's command-line
        boundary and not an operating-system one: a program running as the
        same user can write coordination state directly, so separate the
        operator from the lanes at the operating-system level when that
        distinction has to hold.

        Args:
            repo: Any checkout of the target repository, outside every lane.
            name: Participant whose ready report is decided.
            decision: Recorded outcome, approved or rejected.
            reason: Required explanation for a rejection, delivered to the
                lane as operator mail.

        Returns:
            An account of the decision, what it is bound to, and what
            invalidates it.

        Raises:
            BridgeError: If the command runs inside a lane, if the
                participant is unknown, if the lane has no current ready
                report, if a rejection carries no reason, or if the decision
                cannot be recorded.
        """
        root, directory = self.project(repo, create=False)
        data = roster.read(directory)
        if name not in data["participants"]:
            raise BridgeError(
                f"{name} is not a participant in this project; "
                "run agent-parley participant list."
            )
        if decision == approvals.REJECTED and not reason.strip():
            raise BridgeError(
                "A rejection requires a reason; the lane is told what to "
                "change."
            )
        here = Path(git(repo, "rev-parse", "--show-toplevel")).resolve()
        for lane in data["participants"].values():
            if here == Path(lane["lane"]).resolve():
                raise BridgeError(
                    "Approvals are recorded from the base checkout at "
                    f"{root}, never from an assigned worktree, so a lane "
                    "does not decide its own work."
                )
        reviewed = self._reviewed(directory, data, name)
        if reviewed["state"] == approvals.UNREPORTED:
            raise BridgeError(
                f"{name} has no current ready report to decide. Wait for "
                "the lane to report ready, then record the decision."
            )
        bound = reviewed["binding"]
        approvals.remember(
            directory,
            name,
            {
                "kind": "approval",
                "decision": decision,
                "operator": approvals.operator(),
                "reason": reason,
                "binding": bound,
            },
        )
        if decision == approvals.REJECTED:
            try:
                self.say(
                    repo,
                    name,
                    f"The operator rejected report {bound['report']}: {reason}",
                    subject="Report rejected",
                )
                delivery = "and told the lane why"
            except (BridgeError, OSError) as exc:
                delivery = f"but the lane could not be told: {exc}"
            return (
                f"Rejected {name}'s report {bound['report']} at "
                f"{bound['head'][:12]}, {delivery}. The lane keeps working; "
                "`participant merge` and `participant pr` stay refused "
                "until a new decision is recorded."
            )
        return (
            f"Approved {name}'s report {bound['report']} at "
            f"{bound['head'][:12]} on {bound['branch']} for {bound['base']}. "
            "This records a human decision, not a verification of the code. "
            f"{approvals.RENEWED}"
        )

    def approve(self, repo: Path, name: str) -> str:
        """Records that the operator approved a lane's ready report.

        Args:
            repo: Any checkout of the target repository, outside every lane.
            name: Participant whose ready report is approved.

        Returns:
            An account of the approval and what invalidates it.

        Raises:
            BridgeError: If the decision cannot be recorded for this lane.
        """
        return self._decide(repo, name, approvals.APPROVED)

    def reject(self, repo: Path, name: str, reason: str) -> str:
        """Records that the operator rejected a lane's ready report.

        Args:
            repo: Any checkout of the target repository, outside every lane.
            name: Participant whose ready report is rejected.
            reason: Explanation delivered to the lane as operator mail.

        Returns:
            An account of the rejection.

        Raises:
            BridgeError: If the decision cannot be recorded for this lane.
        """
        return self._decide(repo, name, approvals.REJECTED, reason)
