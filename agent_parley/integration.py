"""Lane merges, bulk integration runs, and operator approve/reject decisions.

`IntegrationMixin` merges one participant's lane or several lanes in
dependency order, previews what a merge would do, and records the
operator's approval or rejection of a lane's ready report.

Method bodies resolve the module-level names they use through `cli` when they
run, so a name bound or replaced there, including a test's patch, is the one a
moved method reads. This module never imports `cli` at import time, because
`cli` imports it to define `Bridge`.
"""

from __future__ import annotations

import contextlib
import shlex
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

from agent_parley import BridgeError
from agent_parley.mail import MailMixin

if TYPE_CHECKING:
    from collections.abc import Sequence

MERGE_BUSY = "Another merge into the base checkout is running; retry later."
"""Refusal a merge reports while another merge holds `merge.lock`."""


class IntegrationMixin(MailMixin):
    """Lane merges, bulk integration order, and operator decisions."""

    def merge(self, repo: Path, name: str, renew: bool = False) -> str:
        """Merges one participant's bridge branch into the base checkout.

        Merges serialize on `merge.lock`. The shared setup lock is held only
        while the manifest is read, never across the verification gate, so
        launches, pauses, retirements and policy changes stay available while
        a gate runs. The lane's session lock then excludes its own launch, and
        the lane's recorded worktree and branch are checked again under it.

        Args:
            repo: Any checkout of the target repository.
            name: Participant whose bridge branch is merged.
            renew: Whether the operator grants the recorded unverified
                integration a fresh set of attempts. Accepted only from the
                base checkout, never from an assigned worktree.

        Returns:
            An account of what was merged.

        Raises:
            BridgeError: If the lane drifted, if the participant holds a
                running session, if the project requires an operator approval
                the lane's current ready report does not have, if the
                repository's verification command fails, if the base carries
                an unverified integration this lane may not repair or whose
                attempts are used, or if the merge cannot complete unattended.
            subprocess.TimeoutExpired: If verification exceeds its timeout.
        """
        from agent_parley.cli import lock, roster

        root, directory = self.project(repo, create=False)
        roster.read(directory)
        with lock(directory / "merge.lock", MERGE_BUSY):
            with lock(directory / "setup.lock"):
                data = self._project(root, directory, verify={name})
                participant = data["participants"].get(name)
                if participant is None:
                    raise BridgeError(
                        f"{name} is not a participant in this project; "
                        "run agent-parley participant list."
                    )
            if renew:
                self._from_base(repo, root, data, "Recovery attempts are")
            return self._integrate_lane(root, directory, data, name, renew)

    def _from_base(
        self, repo: Path, root: Path, data: dict, subject: str
    ) -> None:
        """Refuses an operator decision made inside an assigned worktree.

        This is the command-line boundary between the operator and the
        lanes, not an operating-system one: a program running as the same
        user can write coordination state directly.

        Args:
            repo: Checkout the command runs in.
            root: Common repository root, which is always the base checkout.
            data: Project manifest holding the roster.
            subject: What is decided, as the start of the refusal.

        Raises:
            BridgeError: If the command runs inside an assigned worktree.
        """
        from agent_parley.cli import git, roster

        here = Path(git(repo, "rev-parse", "--show-toplevel")).resolve()
        lanes = {
            Path(lane["lane"]).resolve()
            for lane in data["participants"].values()
        }
        if here in lanes or roster.caller_lane(data):
            raise BridgeError(
                f"{subject} recorded from the base checkout at {root}, never "
                "from an assigned worktree, so a lane does not decide its own "
                "work."
            )

    def verify_recovery(self, repo: Path) -> str:
        """Clears the recorded integration once the base as it stands passes.

        This is the operator's path when no lane may repair the record: its
        issue is unheld or complete, its lane retired, or its attempts are
        used. It merges nothing and resets nothing. It runs the recorded gate
        command on the base checkout's current HEAD and removes the record
        only if the gate passes, HEAD stays put and the tree stays clean.
        The issue ledger is left as it is.

        Args:
            repo: Checkout the command runs in; must be the base checkout.

        Returns:
            An account of the verified commit and the record it cleared.

        Raises:
            BridgeError: If run from an assigned worktree, if the base holds
                a merge in progress or uncommitted changes, if the gate
                fails, or if the gate moves HEAD or changes the tree. The
                record stands in every case.
            subprocess.TimeoutExpired: If verification exceeds its timeout.
        """
        from agent_parley.cli import git, lock, merges, roster, verify_base

        root, directory = self.project(repo, create=False)
        roster.read(directory)
        quoted = shlex.quote(str(root))
        with lock(directory / "merge.lock", MERGE_BUSY):
            with lock(directory / "setup.lock"):
                data = self._project(root, directory, verify=set())
            self._from_base(repo, root, data, "Recovery verification is")
            held = merges.integration_record(directory)
            if held is None:
                return (
                    f"The base checkout at {root} carries no unverified "
                    "integration, so nothing was verified."
                )
            stands = "The recovery record stands."
            if merges.merging(root):
                raise BridgeError(
                    f"The base checkout at {root} holds a merge in progress. "
                    f"Finish it with `git -C {quoted} merge --continue` or "
                    f"`git -C {quoted} merge --abort` first. {stands}"
                )
            if git(root, "status", "--porcelain"):
                raise BridgeError(
                    f"The base checkout at {root} has uncommitted changes. "
                    f"Commit or remove them yourself first. {stands}"
                )
            head = git(root, "rev-parse", "HEAD")
            if held["command"]:
                try:
                    verify_base(root, held["command"], integrated=True)
                except BridgeError as failure:
                    raise BridgeError(f"{failure}\n{stands}") from None
            if git(root, "rev-parse", "HEAD") != head or git(
                root, "status", "--porcelain"
            ):
                raise BridgeError(
                    "Verification changed the base checkout, so the base is "
                    f"not verified. {stands}"
                )
            merges.clear_integration(directory, held["attempt"], held["result"])
            return (
                f"Verified the base at {head[:12]} with the recorded gate and "
                f"cleared {held['lane']}'s {held['kind']} record from attempt "
                f"{held['attempt']} of {held['limit']}. Other lanes may "
                "integrate again; the issue ledger is unchanged."
            )

    def _held_integration(
        self,
        root: Path,
        directory: Path,
        name: str,
        claim: dict,
        renew: bool,
    ) -> dict | None:
        """Admits one lane to retry the integration the base has not verified.

        Args:
            root: Common repository root, which is always the base checkout.
            directory: Private state directory for the common repository.
            name: Participant whose bridge branch is merged.
            claim: Issue and claim generation the lane holds now.
            renew: Whether the operator grants a fresh set of attempts.

        Returns:
            The record the attempt continues, or None when the base carries
            no unverified integration.

        Raises:
            BridgeError: If the base carries an integration this lane may not
                repair, if its attempts are used and not renewed, or if a
                renewal names no recorded integration.
        """
        from agent_parley.cli import merges

        held = merges.integration_record(directory)
        if held is not None and not merges.integration_holds(root, held):
            merges.clear_integration(directory, held["attempt"], held["result"])
            held = None
        if held is None:
            if renew:
                raise BridgeError(
                    f"The base checkout at {root} carries no unverified "
                    "integration, so there are no attempts to renew."
                )
            return None
        owner = merges.integration_owner(directory, held)
        if owner != name or (
            held.get("issue") and str(claim["issue"]) != held["issue"]
        ):
            raise BridgeError(merges.integration_hold(root, directory, held))
        if renew:
            held = merges.record_integration(
                directory,
                {**held, "limit": held["attempt"] + merges.REPAIR_ATTEMPTS},
            )
        if merges.integration_exhausted(held):
            raise BridgeError(
                f"{name}'s integration at {held['result'][:12] or 'HEAD'} "
                f"is still unverified ({held['kind']}); nothing was merged. "
                + merges.integration_remedy(root, held, owner)
            )
        return held

    def _unverified(
        self,
        root: Path,
        directory: Path,
        entry: dict,
        kind: str,
        failure: Exception,
    ) -> BridgeError:
        """Records a failed attempt and returns the repair work to its owner.

        Moving the claim into repair is best effort: if the issue ledger is
        busy, the durable record still stands and the failure is still
        reported, and the claim stays ready for its owner to retry.

        Args:
            root: Common repository root, which is always the base checkout.
            directory: Private state directory for the common repository.
            entry: Record of the attempt that failed.
            kind: How it failed, one of `merges.INTEGRATION_KINDS`.
            failure: The failure as raised.

        Returns:
            The failure to raise, naming what was recorded and the remedy.
        """
        from agent_parley.cli import lifecycle, merges

        recorded = merges.record_integration(
            directory,
            {**entry, "kind": kind, "detail": merges.diagnostic(str(failure))},
        )
        remedy = merges.integration_remedy(
            root, recorded, merges.integration_owner(directory, recorded)
        )
        changed = False
        if (
            kind != merges.INTERRUPTED
            and recorded["issue"]
            and recorded["claim_id"]
        ):
            with contextlib.suppress(BridgeError, OSError):
                changed = lifecycle.integration_failed(
                    directory,
                    recorded["issue"],
                    recorded["claim_id"],
                    recorded["detail"],
                    recorded["result"],
                )
        if changed:
            with contextlib.suppress(BridgeError, OSError):
                self.say(
                    root,
                    recorded["lane"],
                    f"Integrating issue #{recorded['issue']} failed "
                    f"({kind}, attempt {recorded['attempt']} of "
                    f"{recorded['limit']}): {recorded['detail']} "
                    f"{remedy}",
                    subject="Integration needs repair",
                    key=(
                        f"integration-{recorded['issue']}-"
                        f"{recorded['attempt']}-{kind}"
                    ),
                )
        return BridgeError(
            f"{failure}\nRecorded as {kind} on attempt "
            f"{recorded['attempt']} of {recorded['limit']}; no other lane is "
            f"integrated onto this base until it is verified. {remedy}"
        )

    def _integrate_lane(
        self,
        root: Path,
        directory: Path,
        data: dict,
        name: str,
        renew: bool = False,
    ) -> str:
        """Runs the gate and merges one lane while its session is excluded.

        Every integration path goes through this step, so a lane merged in a
        group or in a bulk run is merged on exactly the terms the single-lane
        command merges it on.

        An attempt is recorded before the merge starts and cleared only when
        the exact resulting commit passes the post-merge gate, so a conflict,
        a failed gate or a crash leaves a durable account of an unverified
        base. While it stands no other lane is integrated, and a retry by the
        lane that may repair it skips the pre-merge gate, whose failure the
        record already names, and verifies the exact result instead.

        Args:
            root: Common repository root, which is always the base checkout.
            directory: Private state directory for the common repository.
            data: Project manifest holding the roster and the gate command.
            name: Participant whose bridge branch is merged.
            renew: Whether the operator grants a fresh set of attempts.

        Returns:
            An account of what was merged.

        Raises:
            BridgeError: If the project requires an operator approval the
                lane's current ready report does not have, if the gate fails,
                if the base carries an unverified integration this attempt may
                not continue, or if the merge cannot complete unattended.
        """
        from agent_parley.cli import (
            exact_claim,
            git,
            lifecycle,
            lock,
            merge_branch,
            merges,
            metrics,
            roster,
            session_busy,
            snapshot,
            verify_base,
        )

        participant = data["participants"][name]
        with lock(directory / f"{name}.session.lock", session_busy(name)):
            current = roster.read(directory)["participants"].get(name)
            if current is None or any(
                current[key] != participant[key] for key in ("lane", "branch")
            ):
                raise BridgeError(
                    f"{name}'s lane changed before the merge started; "
                    "nothing was merged. Retry the merge."
                )
            self._require_approval(directory, data, name, "merge")
            claim = exact_claim(directory, name)
            held = self._held_integration(root, directory, name, claim, renew)
            source_commit = ""
            if claim["issue"] is not None:
                record = snapshot(directory)["issues"][str(claim["issue"])]
                execution = lifecycle.state(record)
                if execution["state"] != lifecycle.READY and not (
                    held and execution["state"] == lifecycle.RECOVERY
                ):
                    raise BridgeError(
                        f"Issue #{claim['issue']} is not reported ready."
                    )
                source_commit = (
                    execution.get("source_commit") or execution["commit"]
                )
            if data["verify"] and held is None:
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
            blocker = next(
                merges.merge_blockers(root, lane, name, participant["branch"]),
                "",
            )
            if blocker:
                raise BridgeError(blocker)
            before = git(root, "rev-parse", "HEAD")
            entry = {
                "kind": merges.INTERRUPTED,
                "lane": name,
                "branch": participant["branch"],
                "issue": (
                    None if claim["issue"] is None else str(claim["issue"])
                ),
                "claim_id": claim["claim_id"],
                "source_commit": source_commit,
                "base": held["base"] if held else before,
                "result": "",
                "command": list(data["verify"]),
                "attempt": held["attempt"] + 1 if held else 1,
                "limit": held["limit"] if held else merges.REPAIR_ATTEMPTS,
                "detail": "",
                "recorded_at": held["recorded_at"] if held else time.time(),
            }
            merges.record_integration(directory, entry)
            try:
                merged = merge_branch(
                    root,
                    lane,
                    name,
                    participant["branch"],
                    source_commit,
                )
            except (BridgeError, subprocess.TimeoutExpired) as failure:
                if merges.merging(root):
                    kind = (
                        merges.CONFLICT
                        if merges.unmerged(root)
                        else merges.INTERRUPTED
                    )
                    raise self._unverified(
                        root, directory, entry, kind, failure
                    ) from None
                if git(root, "rev-parse", "HEAD") == before:
                    merges.withdraw_integration(directory, entry, held)
                    raise
                entry["result"] = git(root, "rev-parse", "HEAD")
                raise self._unverified(
                    root, directory, entry, merges.INTERRUPTED, failure
                ) from None
            integrated = git(root, "rev-parse", "HEAD")
            entry["result"] = integrated
            merges.record_integration(directory, entry)
            if data["verify"]:
                try:
                    verify_base(root, data["verify"], integrated=True)
                except BridgeError as failure:
                    raise self._unverified(
                        root, directory, entry, merges.GATE_FAILED, failure
                    ) from None
                if git(root, "rev-parse", "HEAD") != integrated:
                    raise self._unverified(
                        root,
                        directory,
                        entry,
                        merges.INTERRUPTED,
                        BridgeError(
                            "The base commit changed while verification "
                            "ran; the integration stands but is not "
                            "recorded complete."
                        ),
                    )
                if git(root, "status", "--porcelain"):
                    raise self._unverified(
                        root,
                        directory,
                        entry,
                        merges.GATE_FAILED,
                        BridgeError(
                            "Verification changed repository content; "
                            "the integration stands but is not recorded "
                            "complete."
                        ),
                    )
            merges.clear_integration(directory, entry["attempt"], integrated)
            if held:
                merged += (
                    f" Recovery verified {integrated[:12]} on attempt "
                    f"{entry['attempt']} of {entry['limit']}."
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
        from agent_parley.cli import (
            lane_session,
            lock,
            merge_preview,
            merges,
            roster,
        )

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
        report = merge_preview(
            root,
            Path(participant["lane"]),
            name,
            participant["branch"],
            session,
        )
        try:
            held = merges.integration_record(directory)
        except BridgeError as unreadable:
            return f"{report}\n{unreadable}"
        if held is None or not merges.integration_holds(root, held):
            return report
        if merges.integration_owner(directory, held) != name:
            return f"{report}\n{merges.integration_hold(root, directory, held)}"
        return (
            f"{report}\nThis merge would retry the recorded {held['kind']} "
            f"integration, attempt {held['attempt'] + 1} of {held['limit']}, "
            "and verify its exact result."
        )

    def _integration_candidates(
        self,
        directory: Path,
        data: dict,
        state: dict,
        group: str,
        lanes: Sequence[str] | None,
    ) -> tuple[str, dict[str, list[str]]]:
        """Names the lanes one bulk merge considers and what it reports under.

        Args:
            directory: Private state directory for the common repository.
            data: Project manifest holding the roster.
            state: Published issue ledger.
            group: Group of the applied plan; every ready lane when empty.
            lanes: Lanes a selector matched; unrestricted when None. An empty
                selection admits no lane.

        Returns:
            The subject the run reports under and the candidate lanes mapped
            to the issues each one holds.

        Raises:
            BridgeError: If a named group holds a member no participant owns.
        """
        from agent_parley.cli import group_lanes, plan, ready_lanes

        if group:
            return f"Group {group}", group_lanes(
                data, state, group, plan.members(directory, group)
            )
        ready = ready_lanes(directory, data, state)
        if lanes is None:
            return "Ready lanes", ready
        chosen = set(lanes)
        return "Selected ready lanes", {
            name: issues for name, issues in ready.items() if name in chosen
        }

    def integration_plan(
        self,
        repo: Path,
        group: str = "",
        lanes: Sequence[str] | None = None,
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
            lanes: Lanes a selector matched; unrestricted when None.

        Returns:
            The subject the run reports under, the candidate lanes in
            dependency order, and one line per prerequisite outside the set.

        Raises:
            BridgeError: If the repository has no project, a group member is
                unheld, or the candidates form a dependency cycle.
        """
        from agent_parley.cli import (
            lane_dependencies,
            outside_prerequisites,
            plan,
            roster,
            snapshot,
        )

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
        lanes: Sequence[str] | None = None,
        *,
        confirmed: Sequence[str] | None = None,
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
                ungrouped run considers; unrestricted when None. An empty
                selection integrates nothing.
            confirmed: Lanes of the plan the operator confirmed, so a lane
                that became a candidate afterwards is never merged under
                that confirmation; unrestricted when None.

        Returns:
            The ordered plan when previewing, otherwise an account of every
            lane that was integrated.

        Raises:
            BridgeError: If the candidates cannot be ordered, if a group is
                refused or gained a lane after its plan was confirmed, or if
                the run stops on a refusal or a failure, whose report names
                everything already integrated.
        """
        from agent_parley.cli import (
            group_refusal,
            lane_dependencies,
            lane_refusals,
            lock,
            plan,
            roster,
            snapshot,
        )

        root, directory = self.project(repo, create=False)
        with lock(directory / "merge.lock", MERGE_BUSY):
            with lock(directory / "setup.lock"):
                data = roster.read(directory)
                state = snapshot(directory)
                subject, candidates = self._integration_candidates(
                    directory, data, state, group, lanes
                )
                skipped: list[str] = []
                if confirmed is not None:
                    skipped = [
                        f"- {name} no longer ready, skipped."
                        for name in confirmed
                        if name not in candidates
                    ]
                    candidates = self._confirmed_candidates(
                        group, candidates, confirmed
                    )
                if not candidates:
                    return "\n".join(
                        [
                            f"{subject}: no lane to integrate, "
                            "so nothing merged.",
                            *skipped,
                        ]
                    )
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
                root,
                directory,
                data,
                subject,
                sequence,
                waits,
                refusals,
                skipped,
            )

    def _confirmed_candidates(
        self,
        group: str,
        candidates: dict[str, list[str]],
        confirmed: Sequence[str],
    ) -> dict[str, list[str]]:
        """Keeps only the candidates the operator's confirmed plan named.

        A ready lane that appeared after the confirmation is left out of an
        ungrouped run. A group is admitted whole, so a group that gained a
        lane since its plan was confirmed is refused instead of being merged
        in part.

        Args:
            group: Group of the applied plan; empty for an ungrouped run.
            candidates: Current candidate lanes mapped to the issues they hold.
            confirmed: Lanes of the plan the operator confirmed.

        Returns:
            The current candidates that the confirmed plan named.

        Raises:
            BridgeError: If a group gained a lane after its plan was confirmed.
        """
        kept = set(confirmed)
        added = sorted(name for name in candidates if name not in kept)
        if group and added:
            raise BridgeError(
                f"Group {group} gained {', '.join(added)} after its plan was "
                "confirmed; nothing was merged. Run the merge again to "
                "confirm the current plan."
            )
        return {
            name: issues for name, issues in candidates.items() if name in kept
        }

    def _integration_preview(
        self,
        root: Path,
        directory: Path,
        data: dict,
        subject: str,
        sequence: list[str],
    ) -> str:
        """Reports the ordered plan and every candidate's own preview."""
        from agent_parley.cli import lane_session, merge_preview

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
        skipped: Sequence[str] = (),
    ) -> str:
        """Merges an ordered run and reports how far it got.

        Args:
            root: Common repository root, which is always the base checkout.
            directory: Private state directory for the common repository.
            data: Project manifest holding the roster and the gate command.
            subject: What the run reports under.
            sequence: Candidate lanes in dependency order.
            waits: Each candidate mapped to the candidates it waits on.
            refusals: Each candidate mapped to its preflight refusals.
            skipped: Report lines naming confirmed lanes no longer ready.

        Returns:
            An account of every lane that was integrated.

        Raises:
            BridgeError: If the run stops on a refusal or a failure, whose
                report names everything already integrated.
        """
        from agent_parley.cli import unattempted

        report = [
            f"{subject}: {len(sequence)} lanes in dependency order: "
            + ", ".join(sequence)
            + ".",
            *skipped,
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
        from agent_parley.cli import approvals, roster

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
        self._from_base(repo, root, data, "Approvals are")
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
        from agent_parley.cli import approvals

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
        from agent_parley.cli import approvals

        return self._decide(repo, name, approvals.REJECTED, reason)
