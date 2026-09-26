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

from pathlib import Path
from typing import TYPE_CHECKING

from agent_parley import BridgeError
from agent_parley.mail import MailMixin

if TYPE_CHECKING:
    from collections.abc import Sequence


class IntegrationMixin(MailMixin):
    """Lane merges, bulk integration order, and operator decisions."""

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
        from agent_parley.cli import lock, roster

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
        from agent_parley.cli import (
            exact_claim,
            git,
            lifecycle,
            lock,
            merge_branch,
            metrics,
            session_busy,
            snapshot,
            verify_base,
        )

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
        from agent_parley.cli import lane_session, lock, merge_preview, roster

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
        from agent_parley.cli import group_lanes, plan, ready_lanes

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
    ) -> str:
        """Merges an ordered run and reports how far it got."""
        from agent_parley.cli import unattempted

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
        from agent_parley.cli import approvals, git, roster

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
