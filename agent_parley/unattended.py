"""Unattended integration authorized by an explicit project policy.

`participant merge` is an operator command. A project can also record, in its
private manifest, a standing authorization to integrate the ready work of a
bounded list of issues into one named target branch without an operator at
the keyboard. The authorization is `integration.unattended`: the ``target``
branch the base checkout must have checked out and the ``issues`` whose ready
work it covers. It is absent unless the operator writes it with
`agent-parley unattended set`, which refuses to run from an assigned worktree,
and no served coordination tool reads or writes it, so a lane cannot grant
itself the authority or widen it.

Authority never relaxes the terms of a merge. Every eligible lane is merged by
the same `_integrate_lane` step `participant merge` uses, under the project
merge lock and the lane's session lock, with the configured verification gate,
any required operator approval and the source-commit binding all applied.

The gate runs before the merge lock is taken. `_candidate` merges the ready
source commit into the target commit the evaluation read, in a scratch
worktree, and runs the gate there, so several lanes' gates run at once and a
lane waits on the merge lock only for the merges ahead of it. Under the lock
the attempt checks that the target has not moved and merges; the merge lands
without a second gate only when it reproduces the verified tree exactly. A
target that moved is evaluated and gated afresh, up to `ROUNDS` times, after
which the attempt is refused and the next one starts over.
What the policy adds is a stricter precondition: the claimed issue is listed,
the claim generation is current, the work is reported ready at a commit the
lane still sits on, the base checkout is on the target branch, every
dependency is verified complete, a verification command is configured, and no
peer's advisory reservation covers a path the work changed.

Every attempt leaves a durable decision record in the lane's report log with
bounded evidence: the claim generation, the source commit, the target branch
and commit, the verification command and the outcome. A refusal names the
condition that failed and what to do about it. Each attempt evaluates those
inputs afresh, so a changed commit, a moved base or a new claim generation is
judged on its own and never inherits an earlier decision. A replay after a
recorded integration merges and completes nothing.

A merge or gate failure goes through the merge step's durable recovery record,
which holds every other lane and moves the claim to repair; `failed` then
records the unattended decision beside it. Unattended integration never
repairs: while the base carries an unverified integration, every attempt is
refused and repair stays with the lane owner or the operator.

The supervision service attempts integration on its own only when the
operator also records ``auto`` with `agent-parley unattended set --auto`.
`supervise` then calls `integrate` for each listed, ready claim, once per
issue, claim generation, source commit and target commit, so an unchanged
refusal is recorded once rather than on every poll. A lane whose session is
still running is left until the session ends, since every merge refuses a
running session. Each new decision is mailed to the lane, and a failure the
service recorded is raised as a problem for the operator.
"""

from __future__ import annotations

import contextlib
import shlex
import shutil
import sqlite3
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from agent_parley import lifecycle, metrics, roster
from agent_parley.state import BridgeError

if TYPE_CHECKING:
    from agent_parley.cli import Bridge

POLICY = "integration.unattended"
"""Manifest path of the authorization, named in every decision record."""

KIND = "unattended"
"""Report-log record kind of an unattended integration decision."""

INTEGRATED = "integrated"
REFUSED = "refused"
FAILED = "failed"
MAX_REASON = 2000
"""Characters of a refusal or failure explanation kept in a record."""

LANE_TOKEN = "AGENT_PARLEY_TOKEN"
"""Environment variable a launched lane and its children carry."""

FORGE = "deferred to push"
"""Where forge checks and reviews are enforced; the local merge never
pushes, so they apply when the operator pushes the target branch."""

SERVICE = "service"
"""Evidence ``actor`` of a decision the supervision service made itself."""

MERGE_WAIT = 60.0
"""Seconds an attempt whose candidate passed waits for `merge.lock`."""

SETUP_WAIT = 10.0
"""Seconds an attempt waits for the setup lock to read the manifest, so
attempts the service starts together do not refuse one another."""

ROUNDS = 3
"""Candidate gates one attempt runs while the target keeps moving."""

CANDIDATE = "agent-parley candidate gate"
"""Lock reason of a scratch worktree, which keeps the reclaim sweep off it."""


def policy(manifest: dict) -> dict | None:
    """Reads the project's unattended integration authorization strictly.

    Args:
        manifest: Project manifest using the participant roster layout.

    Returns:
        The validated ``target`` and ``issues``, or None when the project
        records no authorization and integration stays operator-only.

    Raises:
        BridgeError: If the recorded policy is invalid; an invalid policy
            is a refusal, never an authorization.
    """
    return roster.integration_policy(manifest.get("integration", {})).get(
        "unattended"
    )


def operator_only(repo: Path, root: Path, manifest: dict, action: str) -> None:
    """Refuses an operator command run from a lane or a lane's shell.

    A launched lane carries its coordination credential in `LANE_TOKEN`, and
    every process it starts inherits it, so a lane that changes directory to
    the base checkout is still refused. Like `approve`, this is the product's
    command-line boundary, not an operating-system one. The unattended
    policy and the merge approval and verification gates all use it, so no
    lane can widen its own integration authority or clear a gate its merge
    has to pass.

    Args:
        repo: Checkout the command names.
        root: Common repository root, which is the base checkout.
        manifest: Project manifest holding every lane.
        action: What the refused command does, for the explanation.

    Raises:
        BridgeError: If the command runs in a lane, names one as its
            checkout, or runs with a lane's coordination credential.
    """
    from agent_parley.cli import git

    here = Path(git(repo, "rev-parse", "--show-toplevel")).resolve()
    lanes = {
        Path(lane["lane"]).resolve()
        for lane in manifest["participants"].values()
    }
    if here in lanes or roster.from_lane(manifest):
        raise BridgeError(
            f"{action} from an operator shell in the base checkout at "
            f"{root}, never from an assigned worktree or a process holding "
            f"a lane's {LANE_TOKEN}, so a lane cannot grant or widen its "
            "own integration authority."
        )


def describe(bridge: Bridge, repo: Path) -> str:
    """Reports the project's unattended integration authorization.

    Args:
        bridge: Coordination runtime owning the project state.
        repo: Any checkout of the target repository.

    Returns:
        An account of the recorded policy, or of its absence.

    Raises:
        BridgeError: If the repository has no project or the recorded
            policy is invalid.
    """
    root, directory = bridge.project(repo, create=False)
    recorded = policy(roster.read(directory))
    if recorded is None:
        return (
            f"{root} records no unattended integration policy; "
            "`participant merge` stays operator-only."
        )
    return (
        f"{root} authorizes unattended integration into "
        f"{recorded['target']} for issues "
        + ", ".join(f"#{number}" for number in recorded["issues"])
        + ", on the terms of `participant merge`."
        + (
            " The service attempts it on its own for ready lanes."
            if recorded.get("auto")
            else " It runs only when an operator types "
            "`agent-parley unattended run`."
        )
    )


def configure(
    bridge: Bridge,
    repo: Path,
    target: str | None,
    issues: list[str],
    auto: bool = False,
) -> str:
    """Records or removes the unattended integration authorization.

    Only an operator in the base checkout may run this. The whole policy is
    replaced on every call, so widening it is always an explicit operator
    act, and an empty issue list removes it.

    Args:
        bridge: Coordination runtime owning the project state.
        repo: Any checkout of the target repository, outside every lane.
        target: Branch the base checkout must have checked out; required
            unless the policy is being removed.
        issues: Issue numbers, optionally prefixed with ``#``, whose ready
            work may be integrated; empty to remove the policy.
        auto: Whether the supervision service attempts the integration on
            its own instead of waiting for `agent-parley unattended run`.

    Returns:
        An account of the recorded policy.

    Raises:
        BridgeError: If the command runs inside a lane, the repository has
            no project, or the policy is invalid.
    """
    from agent_parley.cli import lock, write_json

    root, directory = bridge.project(repo, create=False)
    operator_only(
        repo, root, roster.read(directory), "Unattended integration is set"
    )
    numbers = [str(number).strip().removeprefix("#") for number in issues]
    if not numbers:
        recorded: dict = {}
    elif target is None:
        raise BridgeError(
            "Name the branch unattended integration merges into with --target."
        )
    else:
        recorded = roster.integration_policy(
            {
                "unattended": {
                    "target": target,
                    "issues": numbers,
                    "auto": auto,
                }
            }
        )
    with lock(directory / "setup.lock"):
        data = roster.read(directory)
        data["integration"] = recorded
        write_json(directory / "project.json", data)
    return describe(bridge, repo)


def _record(
    directory: Path, name: str, outcome: str, evidence: dict, reason: str = ""
) -> dict:
    """Appends one durable unattended integration decision.

    Args:
        directory: Private state directory for the common repository.
        name: Participant whose lane the decision concerns.
        outcome: `INTEGRATED`, `REFUSED` or `FAILED`.
        evidence: Inputs the decision was bound to.
        reason: Explanation of a refusal or failure.

    Returns:
        The appended record.
    """
    return metrics.record_report(
        directory,
        name,
        {
            "kind": KIND,
            "action": f"unattended {outcome}",
            "outcome": outcome,
            "policy": POLICY,
            "issue": evidence.get("issue"),
            "claim_id": evidence.get("claim_id"),
            "evidence": dict(evidence),
            "reason": reason[:MAX_REASON],
        },
    )


def refuse(
    directory: Path, name: str, evidence: dict, reason: str
) -> BridgeError:
    """Records why a lane is not eligible and builds the refusal to raise.

    Nothing was merged when this is called.

    Args:
        directory: Private state directory for the common repository.
        name: Participant whose lane was evaluated.
        evidence: Inputs read before the unmet condition was found.
        reason: The unmet condition and what to do about it.

    Returns:
        The refusal, naming the policy and the condition.
    """
    _record(directory, name, REFUSED, evidence, reason)
    return BridgeError(
        f"Unattended integration of {name} refused; nothing was merged. "
        f"{reason}"
    )


def failed(
    directory: Path, name: str, evidence: dict, reason: str
) -> BridgeError:
    """Records a merge or gate failure on an authorized unattended attempt.

    Every failure after eligibility was established goes through here: a
    busy session, a missing approval, a failing verification gate, a merge
    conflict or post-merge verification that leaves the integration
    standing but not complete. The merge step has already written the
    failed-integration recovery record for a conflict or a failed gate, so
    this adds the unattended decision and never a second recovery path.

    Args:
        directory: Private state directory for the common repository.
        name: Participant whose lane was being integrated.
        evidence: Inputs the attempt was authorized on.
        reason: The failure as the merge step reported it.

    Returns:
        The refusal to raise, naming the failure and the recorded evidence.
    """
    _record(directory, name, FAILED, evidence, reason)
    return BridgeError(
        f"Unattended integration of {name} failed and was recorded for "
        f"recovery: {reason}"
    )


def _replayed(directory: Path, name: str, key: tuple) -> dict | None:
    """Finds an earlier recorded integration this attempt would repeat.

    Only an exact match counts: the same issue, claim generation and source
    commit, all known, and the ledger recording that generation complete.
    An attempt that read no claim or no source commit never matches, so it
    falls through to a recorded refusal instead of reporting a success.

    Args:
        directory: Private state directory for the common repository.
        name: Participant whose lane is evaluated.
        key: Issue, claim generation and source commit this attempt read.

    Returns:
        The earlier integrated decision, or None.
    """
    from agent_parley.cli import snapshot

    if any(not part for part in key):
        return None
    issues = snapshot(directory)["issues"]
    for record in reversed(metrics.report_records(directory, name)):
        if record.get("kind") != KIND or record.get("outcome") != INTEGRATED:
            continue
        evidence = record.get("evidence") or {}
        bound = (
            evidence.get("issue"),
            evidence.get("claim_id"),
            evidence.get("source_commit"),
        )
        if bound != key:
            continue
        completed = lifecycle.state(issues.get(str(key[0]), {}))
        if (
            completed["state"] == lifecycle.COMPLETE
            and completed["claim_id"] == key[1]
        ):
            return record
    return None


def _evaluate(
    bridge: Bridge,
    root: Path,
    directory: Path,
    data: dict,
    name: str,
    issue: str = "",
) -> tuple[dict, str]:
    """Reads the inputs of one decision and names the first unmet condition.

    An input that cannot be read, such as a missing ledger record or a Git
    query that fails or times out, is itself an unmet condition, so the
    attempt still leaves a recorded refusal.

    Args:
        bridge: Coordination runtime owning the project state.
        root: Common repository root, which is the base checkout.
        directory: Private state directory for the common repository.
        data: Project manifest read under the setup lock.
        name: Participant whose lane is evaluated.
        issue: Claimed issue to integrate, or empty to take the lane's only
            ready claim.

    Returns:
        The evidence read so far, and the unmet condition, which is empty
        when the lane is eligible.
    """
    evidence: dict = {"policy": POLICY, "participant": name, "forge": FORGE}
    try:
        unmet = _conditions(
            bridge, root, directory, data, name, evidence, issue
        )
    except (KeyError, BridgeError, OSError, subprocess.TimeoutExpired) as exc:
        unmet = (
            "An input of the decision could not be read, so eligibility "
            f"cannot be established: {exc!r}. Repair it and run again."
        )
    return evidence, unmet


def _conditions(
    bridge: Bridge,
    root: Path,
    directory: Path,
    data: dict,
    name: str,
    evidence: dict,
    issue: str = "",
) -> str:
    """Checks each eligibility condition in order, filling in the evidence.

    Args:
        bridge: Coordination runtime owning the project state.
        root: Common repository root, which is the base checkout.
        directory: Private state directory for the common repository.
        data: Project manifest read under the setup lock.
        name: Participant whose lane is evaluated.
        evidence: Mutable evidence, extended as each input is read.
        issue: Claimed issue to integrate, or empty to take the lane's only
            ready claim.

    Returns:
        The first unmet condition, or an empty string when eligible.

    Raises:
        KeyError: If the ledger lacks the claimed issue's record.
        BridgeError: If Git cannot answer a query.
        subprocess.TimeoutExpired: If a Git query exceeds its timeout.
    """
    from agent_parley.cli import (
        current_branch,
        git,
        merge_claim,
        merges,
        reserved_overlaps,
        snapshot,
        store,
    )

    try:
        recorded = policy(data)
    except BridgeError as exc:
        return f"{exc} Correct it with `agent-parley unattended set`."
    if recorded is None:
        return (
            "This project records no unattended integration policy, so "
            "integration stays operator-only: run `participant merge`, or "
            "authorize issues with `agent-parley unattended set`."
        )
    evidence["target"] = recorded["target"]
    try:
        claim = merge_claim(directory, name, issue)
    except BridgeError as exc:
        return str(exc)
    if claim["issue"] is None:
        return f"{name} holds no claimed issue to integrate."
    issue = str(claim["issue"])
    evidence.update(issue=issue, claim_id=claim["claim_id"])
    if issue not in recorded["issues"]:
        return (
            f"Issue #{issue} is not listed in {POLICY}; an operator adds it "
            "with `agent-parley unattended set` or runs `participant merge`."
        )
    ledger = snapshot(directory)
    record = ledger["issues"][issue]
    execution = lifecycle.state(record)
    if execution["claim_id"] != claim["claim_id"]:
        return f"Issue #{issue} changed claim generation while it was read."
    if execution["state"] != lifecycle.READY:
        return (
            f"Issue #{issue} is {execution['state']}, not ready; the lane "
            "records a ready report first."
        )
    source = str(execution.get("source_commit") or execution["commit"] or "")
    evidence["source_commit"] = source
    if not lifecycle.COMMIT.fullmatch(source):
        return f"Issue #{issue} names no ready source commit."
    lane = Path(data["participants"][name]["lane"])
    if git(lane, "rev-parse", "HEAD") != source:
        return (
            f"{name} committed since issue #{issue} was reported ready at "
            f"{source[:12]}; record a new ready report."
        )
    branch = current_branch(root)
    if branch != recorded["target"]:
        return (
            f"The base checkout has {branch} checked out, not the policy "
            f"target {recorded['target']}."
        )
    target_commit = git(root, "rev-parse", "HEAD")
    evidence["target_commit"] = target_commit
    standing = merges.integration_record(directory)
    if standing and merges.integration_holds(root, standing):
        return (
            "Unattended integration never repairs an unverified base. "
            + merges.integration_hold(root, directory, standing)
        )
    waits = merges.dependency_refusal(ledger, issue)
    if waits:
        return waits
    if not data["verify"]:
        return (
            "This project configures no verification command, so no green "
            "gate can authorize unattended integration. Set one with "
            "`agent-parley verify set`."
        )
    evidence["verify"] = shlex.join(data["verify"])
    changed = [
        line
        for line in git(
            root, "diff", "--name-only", f"{target_commit}...{source}"
        ).splitlines()
        if line
    ]
    evidence["changed_paths"] = len(changed)
    try:
        held = store.active_reservations(bridge.home, data["root"])
    except (BridgeError, OSError, sqlite3.Error) as exc:
        return (
            "The advisory reservations could not be read, so no overlap "
            f"with a peer can be ruled out: {exc}"
        )
    held.pop(data["participants"][name]["display"], None)
    if overlaps := reserved_overlaps(
        changed, held, fold=roster.case_insensitive(data)
    ):
        return (
            "A peer reservation covers what the work changed: "
            + "; ".join(overlaps[:5])
            + ". Hand the work over or wait for the release."
        )
    return ""


def _candidate(root: Path, directory: Path, data: dict, evidence: dict) -> str:
    """Runs the verification gate on the merge an attempt would make.

    The candidate is the ready source commit merged into the target commit
    the evaluation read. It is built in a scratch worktree inside the
    private state directory, never in the base checkout, so no lock is held
    while the gate runs. The worktree is locked with `CANDIDATE` so the
    reclaim sweep leaves it alone, and it is removed whatever the outcome.
    It holds tracked content only: files the base checkout ignores, such as
    a virtual environment, are absent while the gate runs.

    Args:
        root: Common repository root, which is the base checkout.
        directory: Private state directory for the common repository.
        data: Project manifest holding the gate command.
        evidence: Eligible evidence naming the participant, the target
            commit and the source commit.

    Returns:
        The tree of the verified candidate, which the merge under
        `merge.lock` has to reproduce exactly to land without another gate.

    Raises:
        BridgeError: If the candidate cannot be built, does not merge
            cleanly, fails the gate, or the gate changes its content.
        subprocess.TimeoutExpired: If a Git step exceeds its timeout.
    """
    from agent_parley.cli import git, verify_base

    target = evidence["target_commit"]
    source = evidence["source_commit"]
    scratch = Path(
        tempfile.mkdtemp(
            prefix=f".candidate-{evidence['participant']}-", dir=directory
        )
    )
    try:
        git(
            root,
            "worktree",
            "add",
            "--lock",
            "--reason",
            CANDIDATE,
            "--detach",
            str(scratch),
            target,
        )
        try:
            git(scratch, "merge", "--no-ff", "--no-commit", source)
        except BridgeError as exc:
            raise BridgeError(
                f"The ready commit {source[:12]} does not merge cleanly onto "
                f"{target[:12]}; nothing was merged. {exc}"
            ) from None
        tree = git(scratch, "write-tree")
        verify_base(scratch, data["verify"])
        git(scratch, "add", "--all")
        if git(scratch, "write-tree") != tree:
            raise BridgeError(
                "Verification changed the candidate's content, so it is not "
                "verified; nothing was merged."
            )
        return tree
    finally:
        with contextlib.suppress(BridgeError, subprocess.TimeoutExpired):
            git(root, "worktree", "remove", "--force", "--force", str(scratch))
        shutil.rmtree(scratch, ignore_errors=True)
        with contextlib.suppress(BridgeError, subprocess.TimeoutExpired):
            git(root, "worktree", "prune")


def integrate(
    bridge: Bridge,
    repo: Path,
    name: str,
    issue: str = "",
    *,
    actor: str = "",
) -> str:
    """Integrates one lane under the project's unattended policy.

    Eligibility is evaluated and the candidate gated by `_candidate` with no
    lock held; the shared setup lock covers only the manifest read. The
    project merge lock is then taken, waiting up to `MERGE_WAIT` seconds for
    a merge ahead of it, and the lane is merged by the step `participant
    merge` uses only if the target is still the commit the candidate was
    built on. A target that moved is evaluated and gated afresh, so no
    result is merged that its gate did not verify. Every outcome is
    recorded as a decision.

    Args:
        bridge: Coordination runtime owning the project state.
        repo: Any checkout of the target repository, outside every lane.
        name: Participant whose ready work is integrated.
        issue: Claimed issue to integrate, or empty to take the lane's only
            ready claim.
        actor: `SERVICE` when the supervision service makes the attempt,
            recorded in the decision's evidence; empty for an operator.

    Returns:
        An account of the merge, or of the earlier integration a replay
        found, in which case nothing is merged or completed again.

    Raises:
        BridgeError: If the command runs inside a lane, another merge holds
            the merge lock past `MERGE_WAIT`, the lane is not eligible, the
            target kept moving for `ROUNDS` gates, or the merge or its gate
            fails; every case but the first two is recorded durably first.
    """
    from agent_parley.cli import current_branch, git, lock
    from agent_parley.integration import MERGE_BUSY

    root, directory = bridge.project(repo, create=False)
    data = roster.read(directory)
    if name not in data["participants"]:
        raise BridgeError(
            f"{name} is not a participant in this project; "
            "run agent-parley participant list.",
            next_command="agent-parley participant list",
        )
    operator_only(repo, root, data, "Unattended integration runs")
    evidence: dict = {}
    for _ in range(ROUNDS):
        with lock(directory / "setup.lock", timeout=SETUP_WAIT):
            data = bridge._project(root, directory, verify={name})
        evidence, unmet = _evaluate(bridge, root, directory, data, name, issue)
        if actor:
            evidence["actor"] = actor
        key = (
            evidence.get("issue"),
            evidence.get("claim_id"),
            evidence.get("source_commit"),
        )
        if earlier := _replayed(directory, name, key):
            bound = earlier["evidence"]
            return (
                f"Issue #{bound.get('issue')} of {name} was already "
                f"integrated at {str(bound.get('integrated_commit'))[:12]} "
                "and recorded complete; nothing was merged again."
            )
        if unmet:
            raise refuse(directory, name, evidence, unmet)
        try:
            tree = _candidate(root, directory, data, evidence)
        except subprocess.TimeoutExpired as exc:
            raise failed(
                directory,
                name,
                evidence,
                f"A Git step of the candidate gate exceeded its "
                f"{exc.timeout}s timeout.",
            ) from exc
        except BridgeError as exc:
            raise failed(directory, name, evidence, str(exc)) from exc
        with lock(directory / "merge.lock", MERGE_BUSY, timeout=MERGE_WAIT):
            if (
                git(root, "rev-parse", "HEAD") != evidence["target_commit"]
                or current_branch(root) != evidence["target"]
            ):
                continue
            try:
                merged = bridge._integrate_lane(
                    root,
                    directory,
                    data,
                    name,
                    expected=(evidence["claim_id"], evidence["source_commit"]),
                    issue=evidence["issue"],
                    verified=(evidence["target_commit"], tree),
                )
            except subprocess.TimeoutExpired as exc:
                raise failed(
                    directory,
                    name,
                    evidence,
                    f"Verification exceeded its {exc.timeout}s timeout.",
                ) from exc
            except BridgeError as exc:
                raise failed(directory, name, evidence, str(exc)) from exc
            evidence["integrated_commit"] = git(root, "rev-parse", "HEAD")
            _record(directory, name, INTEGRATED, evidence)
        return (
            f"{merged}\nIntegrated unattended under {POLICY}: issue "
            f"#{evidence['issue']} at {evidence['source_commit'][:12]} into "
            f"{evidence['target']}."
        )
    raise refuse(
        directory,
        name,
        evidence,
        f"The target {evidence['target']} moved while each of {ROUNDS} "
        "candidate gates ran, so no verified result could be merged; the "
        "next attempt evaluates it afresh.",
    )


def latest(directory: Path, name: str) -> dict | None:
    """Reads the newest unattended decision recorded for one lane.

    Args:
        directory: Private state directory for the common repository.
        name: Participant whose report log is read.

    Returns:
        The newest decision record, or None when the lane has none.
    """
    for record in reversed(metrics.report_records(directory, name)):
        if record.get("kind") == KIND:
            return record
    return None


def _decided(
    directory: Path, name: str, key: tuple, target_commit: str
) -> bool:
    """Reports whether an earlier decision already settled this attempt.

    The newest decision bound to the same issue, claim generation and
    source commit settles it, unless that decision read a target commit
    the base has since moved from, or it failed only because the lane's
    session was running, which ends without changing anything the key
    names.

    Args:
        directory: Private state directory for the common repository.
        name: Participant whose decisions are read.
        key: Issue, claim generation and source commit of the claim.
        target_commit: Commit the base checkout has checked out now.

    Returns:
        True when the service should not attempt the claim again.
    """
    from agent_parley.cli import merges

    for record in reversed(metrics.report_records(directory, name)):
        if record.get("kind") != KIND:
            continue
        evidence = record.get("evidence") or {}
        bound = (
            evidence.get("issue"),
            evidence.get("claim_id"),
            evidence.get("source_commit"),
        )
        if bound != key:
            continue
        read = evidence.get("target_commit")
        return not (read and read != target_commit) and record.get(
            "reason"
        ) != merges.session_busy(name)
    return False


def _attempt(
    bridge: Bridge, root: Path, directory: Path, name: str, issue: str
) -> None:
    """Makes one service attempt and tells the lane what was decided.

    Nothing is mailed when no decision was recorded, as when another merge
    holds the merge lock; the next poll tries again. A failure this thread
    did not expect is recorded beside the issue ledger, never lost.

    Args:
        bridge: Coordination runtime owning the project state.
        root: Common repository root, which is the base checkout.
        directory: Private state directory for the common repository.
        name: Participant whose ready claim is integrated.
        issue: Listed issue the claim covers.
    """
    from agent_parley import issues

    try:
        before = latest(directory, name)
        with contextlib.suppress(BridgeError):
            integrate(bridge, root, name, issue, actor=SERVICE)
        decision = latest(directory, name)
        if decision is None or decision == before:
            return
        outcome = decision["outcome"]
        text = (
            f"The service recorded unattended integration of issue #{issue} "
            f"as {outcome} under {POLICY}."
        )
        if decision.get("reason"):
            text += f" {decision['reason']}"
        bridge.say(
            root, name, text, subject=f"Unattended integration {outcome}"
        )
    except Exception as exc:
        issues.note_supervision_error(
            directory,
            f"unattended {name}: {type(exc).__name__}: {exc}",
        )


def supervise(
    home: Path, directory: Path, manifest: dict
) -> list[threading.Thread]:
    """Starts the service's attempts for the claims the policy covers.

    Nothing starts unless the recorded policy carries ``auto``. A claim is
    attempted when its issue is listed, its execution is ready, its lane is
    in the roster, not retired and running no session, no attempt for the
    lane is already running, and `_decided` finds no decision that settles
    it. Each attempt runs `integrate` on a daemon thread, so a slow
    verification gate never holds the poll.

    Args:
        home: Private bridge state root.
        directory: Private project state directory.
        manifest: Current participant manifest.

    Returns:
        The threads started, so a caller may wait for them.

    Raises:
        BridgeError: If the recorded policy is invalid.
    """
    from agent_parley.cli import Bridge, git, merges, snapshot

    recorded = policy(manifest)
    if not recorded or not recorded.get("auto"):
        return []
    root = Path(manifest["root"])
    running = {thread.name for thread in threading.enumerate()}
    target_commit = ""
    started = []
    for number, record in snapshot(directory)["issues"].items():
        name = record.get("owner")
        participant = manifest["participants"].get(name)
        execution = lifecycle.state(record)
        label = f"agent-parley-unattended-{directory.name}-{name}"
        if (
            participant is None
            or roster.retired(participant)
            or number not in recorded["issues"]
            or execution["state"] != lifecycle.READY
            or label in running
        ):
            continue
        source = str(
            execution.get("source_commit") or execution["commit"] or ""
        )
        target_commit = target_commit or git(root, "rev-parse", "HEAD")
        if _decided(
            directory,
            name,
            (number, execution["claim_id"], source),
            target_commit,
        ) or merges.lane_session(directory, name):
            continue
        thread = threading.Thread(
            target=_attempt,
            args=(Bridge(home), root, directory, name, number),
            name=label,
            daemon=True,
        )
        thread.start()
        running.add(label)
        started.append(thread)
    return started
