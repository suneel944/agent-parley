"""Claims, hands off, recommends and resolves issues, and applies work plans."""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING

from agent_parley import BridgeError
from agent_parley.integration import reported_ready
from agent_parley.lazy import DeferredCallable, deferred, deferred_module
from agent_parley.mail import operator_key
from agent_parley.worktrees import Worktrees, git

if TYPE_CHECKING:
    import contextlib
    import subprocess
    import uuid

    from agent_parley import (
        attachments,
        forecast,
        forge,
        issues,
        lifecycle,
        plan,
        recommend,
        roster,
        store,
        supervision,
    )
    from agent_parley.issues import change, parse_issue, snapshot
else:
    contextlib = deferred_module("contextlib")
    subprocess = deferred_module("subprocess")
    uuid = deferred_module("uuid")
    attachments = deferred("attachments")
    forecast = deferred("forecast")
    forge = deferred("forge")
    issues = deferred("issues")
    lifecycle = deferred("lifecycle")
    plan = deferred("plan")
    recommend = deferred("recommend")
    roster = deferred("roster")
    store = deferred("store")
    supervision = deferred("supervision")
    change = DeferredCallable(issues, "change")
    parse_issue = DeferredCallable(issues, "parse_issue")
    snapshot = DeferredCallable(issues, "snapshot")


class Claims(Worktrees):
    """Applies issue transitions and reads the ledger for one project."""

    def authorize_recovery(
        self,
        repo: Path,
        number: str,
        reason: str,
    ) -> dict:
        """Records operator approval for one live claim recovery.

        Args:
            repo: Project base checkout, never a participant lane.
            number: Repository issue number whose claim may be stopped.
            reason: Operator rationale stored with the approval.

        Returns:
            Approval bound to the current owner, claim and native session.

        Raises:
            BridgeError: If invoked from a lane or no exact live claim exists.
        """
        _, directory = self.project(repo)
        data = roster.read(directory)
        if repo.resolve() != Path(data["root"]).resolve():
            raise BridgeError(
                "Live recovery approval must be recorded from the project "
                "base checkout."
            )
        from agent_parley import recovery

        return recovery.authorize(directory, data, parse_issue(number), reason)

    def issue(
        self,
        repo: Path,
        action: str,
        number: str = "",
        *,
        to: str | None = None,
        summary: str = "",
        offer_id: str | None = None,
        on: str | None = None,
        within: float | None = None,
        key: str = "",
        when_released: str = "",
        remaining: list[str] | None = None,
        take_orphaned: bool = False,
    ) -> dict:
        """Reads the issue ledger or applies a transition as the selected lane.

        An unblock run from the project base checkout is the operator's: it
        drops the edge whether or not any lane owns the waiting issue, because
        a released issue has no owner who could drop it.

        A claim additionally attempts a read-only forge lookup for the issue
        title. That lookup is optional context: an unavailable forge resolves
        to no title and never blocks or fails the claim.

        A completed claim or release is then mirrored onto the host forge as
        an assignment, so the issue reads as worked outside Agent Parley. The
        ledger is written first and the mirror never reverses it: a forge that
        is missing, offline or unwilling leaves the transition in force.

        A claim with a reachable forge also reads the paths the issue's earlier
        pull requests touched and forecasts, from the base checkout's recent
        co-change history, which peer-reserved files are likely to collide.
        The forecast is returned as ``forecast`` beside the record, advisory
        only, and omitted when nothing is likely or no forge is configured.

        An offer records the work state beside its summary: the lane's head
        commit, the reservation keys it holds, the remaining work it states,
        and the diff against the project base when that diff fits the
        attachment cap. An acceptance then moves those reservations from the
        offering lane to the accepting one, so the advisory declaration on
        each key names the lane that now owns the work.

        Args:
            repo: Repository for listing, or assigned worktree for mutations.
            action: List, claim, release, offer, accept, decline, cancel,
                block, or unblock.
            number: Repository issue number for a mutation.
            to: Handoff recipient.
            summary: Handoff context supplied by the owner.
            offer_id: Exact current offer ID for acceptance or decline.
            on: Issue this one waits on, for a block or unblock.
            within: Seconds this claim or offer is expected to take, recorded
                as a deadline. None takes the project default.
            key: Idempotency key. A retried command carrying the key it first
                used returns the first result and transfers nothing further.
            when_released: Issue whose explicit release or completion an offer
                waits on. The offer is recorded rather than applied, and the
                supervision poll applies it once that release is recorded.
            remaining: Work the offering lane states as still to do, one item
                per entry, recorded beside the summary.
            take_orphaned: Whether this claim takes an issue the supervisor
                marked orphaned, recording the previous owner and the reason.

        Returns:
            The whole ledger for list, or the resulting issue record. An
            acceptance additionally reports the reservation keys that moved,
            and a take reports the keys the orphaned owner's reservations
            moved to the new ownership generation.

        Raises:
            BridgeError: If lane, ownership, or transition checks fail.
        """
        _, directory = self.project(repo)
        data = roster.read(directory)
        if action == "list":
            return snapshot(directory)
        lane = Path(git(repo, "rev-parse", "--show-toplevel")).resolve()
        if action == "unblock" and lane == Path(data["root"]).resolve():
            agent = roster.OPERATOR
        else:
            agent = roster.resolve(data, lane)
        if action == "offer" and when_released:
            recipient = to or ""
            if recipient not in data["participants"] or recipient == agent:
                raise BridgeError(
                    "Choose another participant in this project; "
                    "run agent-parley participant list."
                )
            if not 1 <= len(summary.strip()) <= 2000:
                raise BridgeError(
                    "Handoff summary must contain 1-2000 characters."
                )
            return store.schedule(
                self.home,
                data["root"],
                {
                    "kind": "offer",
                    "recipient": recipient,
                    "actor": agent,
                    "body_md": summary.strip(),
                    "issue": parse_issue(number),
                    "condition": f"released:{parse_issue(when_released)}",
                },
            )
        forge.select(repo, data)
        title = (
            forge.issue_title(repo, parse_issue(number))
            if action == "claim"
            else None
        )
        carried = (
            self._carry(repo, directory, data, agent, to or "", remaining)
            if action == "offer"
            else {}
        )
        takeover = {}
        if action == "claim" and take_orphaned:
            from agent_parley import recovery

            current = (
                snapshot(directory)["issues"].get(parse_issue(number)) or {}
            )
            if current.get("orphan"):
                takeover = recovery.prepare_takeover(
                    directory, data, agent, parse_issue(number)
                )
                recovery.preflight(directory, repo, takeover["checkpoint"])
        try:
            record = change(
                directory,
                agent,
                action,
                number,
                participants=set(data["participants"]),
                key=key,
                to=to,
                summary=summary,
                offer_id=offer_id,
                on=on,
                title=title,
                within=within,
                defaults=data["deadlines"],
                carried=carried,
                take_orphaned=take_orphaned,
                takeover=takeover,
            )
        except BridgeError:
            attachments.remove(directory, carried.get("diff", ""))
            raise
        if action == "accept":
            return self._inherit(data, agent, record)
        if action == "claim":
            from agent_parley import recovery

            if recovery.current_take(record):
                record = recovery.restore(directory, repo, record)
            record = self._free_orphaned(data, record)
            forge.assign(repo, parse_issue(number))
            likely = self._claim_forecast(
                repo, directory, data, agent, parse_issue(number)
            )
            if likely:
                record = {**record, "forecast": likely}
        elif action == "release":
            forge.unassign(repo, parse_issue(number))
        return record

    def _carry(
        self,
        repo: Path,
        directory: Path,
        data: dict,
        agent: str,
        recipient: str,
        remaining: list[str] | None,
    ) -> dict:
        """Reads the work state an offer transfers beside its summary.

        Every field is read best effort. A lane without a readable Git head,
        without a reachable store, or whose diff exceeds the attachment cap
        offers exactly what it can state, because a handoff that refuses to be
        recorded is worse than one that carries less.

        Args:
            repo: Assigned worktree the offer is made from.
            directory: Private project state directory.
            data: Project manifest.
            agent: Offering participant.
            recipient: Participant the offer names.
            remaining: Work the offering lane states as still to do.

        Returns:
            The commit, reservation keys, remaining work and, where one was
            attached, the diff reference and its byte count.
        """
        import sqlite3

        carried: dict = {"remaining": list(remaining or [])}
        with contextlib.suppress(BridgeError, subprocess.TimeoutExpired):
            carried["commit"] = git(repo, "rev-parse", "HEAD")
        own = data["participants"][agent]["display"]
        try:
            held = store.active_reservations(self.home, data["root"])
        except (BridgeError, OSError, sqlite3.Error):
            held = {}
        carried["reservations"] = held.get(own, [])
        text = ""
        with contextlib.suppress(BridgeError, subprocess.TimeoutExpired):
            text = git(repo, "diff", data["base"], "HEAD")
        if not text or len(text.encode()) > attachments.MAX_ATTACHMENT_BYTES:
            return carried
        with contextlib.suppress(BridgeError, OSError):
            carried["diff"] = attachments.keep(
                directory,
                "offer",
                uuid.uuid4().hex,
                text,
                agent,
                [recipient],
            )
            carried["diff_bytes"] = len(text.encode())
        return carried

    def _inherit(self, data: dict, agent: str, record: dict) -> dict:
        """Moves an accepted handoff's reservations to the accepting lane.

        Reservations are advisory declarations of intent, never enforced file
        system locks. The release and the grant share one store transaction,
        so a peer reading the keys sees them held by the offering lane or by
        the accepting one, never by both and never by neither.

        A store that cannot answer leaves every key with the offering lane,
        which is the state a declined handoff leaves, and reports why beside
        the record rather than reversing a committed transfer of ownership.

        Args:
            data: Project manifest.
            agent: Participant that accepted the handoff.
            record: Persisted record the acceptance produced.

        Returns:
            The record with the reservation keys that moved, and the reason
            none did where the store refused.
        """
        import sqlite3

        inherited = record.get("handoff") or {}
        keys = list(inherited.get("reservations") or [])
        offerer = inherited.get("from")
        if not keys or offerer not in data["participants"]:
            return record
        try:
            moved = store.transfer_reservations(
                self.home,
                data["root"],
                data["participants"][offerer]["display"],
                data["participants"][agent]["display"],
                keys,
                record.get("claim_id", ""),
            )
        except (BridgeError, OSError, sqlite3.Error) as exc:
            return {
                **record,
                "reservations_moved": [],
                "reservations_error": str(exc),
            }
        return {**record, "reservations_moved": moved}

    def _free_orphaned(self, data: dict, record: dict) -> dict:
        """Moves reservations of the recovered ownership generation.

        Reservations are advisory declarations of intent, never enforced file
        system locks. Only reservations correlated with this claim move to the
        recovering lane. Reservations for unrelated claims stay with their
        owner.

        A store that cannot answer leaves every key where it was and reports
        why beside the record, because a committed take is not reversed by a
        failure to tidy the declarations it left behind.

        Args:
            data: Project manifest.
            record: Persisted record the claim produced.

        Returns:
            The record unchanged when nothing was taken, and otherwise the
            record carrying the moved keys, or the reason none were.
        """
        import sqlite3

        from agent_parley import recovery

        taken = recovery.current_take(record)
        previous = taken.get("from")
        if previous not in data["participants"]:
            return record
        checkpoint = taken.get("checkpoint") or {}
        source_claim = str(checkpoint.get("claim_id") or "")
        new_owner = str(record.get("owner") or "")
        if new_owner not in data["participants"]:
            return record
        try:
            moved = store.transfer_claim_reservations(
                self.home,
                data["root"],
                data["participants"][previous]["display"],
                data["participants"][new_owner]["display"],
                source_claim,
                str(record.get("claim_id") or ""),
            )
        except (BridgeError, OSError, sqlite3.Error) as exc:
            return {
                **record,
                "reservations_moved": [],
                "reservations_error": str(exc),
            }
        return {**record, "reservations_moved": moved}

    def _claim_forecast(
        self, repo: Path, directory: Path, data: dict, agent: str, number: str
    ) -> list[dict]:
        """Forecasts collisions for a claim from its earlier pull requests.

        The paths the issue's earlier pull requests touched stand in for the
        reservation the lane has not filed yet. Without a configured forge
        there is nothing to read and the claim is reported unchanged.

        Args:
            repo: Assigned worktree that selects the forge project.
            directory: Private project state directory holding the cache.
            data: Project manifest.
            agent: Claiming participant.
            number: Bare repository issue number.

        Returns:
            Forecast records naming path, peer and count, or an empty list.
        """
        import sqlite3

        touched = forge.issue_pull_request_paths(repo, number)
        if not touched:
            return []
        commits = forecast.history(data["root"], directory)
        counts = forecast.cochanges(commits, touched, store.overlapping)
        if not counts:
            return []
        own = data["participants"][agent]["display"]
        try:
            held = store.active_reservations(self.home, data["root"])
        except (BridgeError, OSError, sqlite3.Error):
            return []
        held.pop(own, None)
        return forecast.collisions(counts, held, store.overlapping)

    def issue_next(
        self, repo: Path, limit: int = recommend.MAX_SHORTLIST
    ) -> dict:
        """Ranks the unclaimed issues this lane could take next, claiming none.

        One reading answers what `issue list`, `plan show` and `status` are
        read together for: which recorded issues are free, unblocked, part of
        a plan group already under way, clear of the paths peers reserve, and
        declared for this lane's provider. It is advice and nothing else. No
        ledger entry is written, no offer is made and no reservation is taken,
        so the lane still claims the issue it chooses through `issue claim`
        and still races any peer that chose the same one.

        Args:
            repo: Assigned worktree, which names the lane doing the reading.
            limit: Most candidates to return.

        Returns:
            The lane, its provider, whether the forge answered with any paths,
            and the ranked candidates with the reasons for their order.

        Raises:
            BridgeError: If the worktree belongs to no registered lane.
        """
        import sqlite3

        _, directory = self.project(repo)
        data = roster.read(directory)
        lane = Path(git(repo, "rev-parse", "--show-toplevel")).resolve()
        agent = roster.resolve(data, lane)
        participant = data["participants"][agent]
        forge.select(repo, data)
        try:
            held = store.active_reservations(self.home, data["root"])
        except (BridgeError, OSError, sqlite3.Error):
            held = {}
        held.pop(participant["display"], None)
        return {
            "participant": agent,
            **recommend.shortlist(
                directory,
                data["root"],
                repo,
                participant["provider"],
                held,
                store.overlapping,
                limit,
            ),
        }

    def issue_match(
        self, repo: Path, goal: str, limit: int = recommend.MAX_SHORTLIST
    ) -> dict:
        """Lists the open issues a stated goal already describes.

        A lane that opens a second issue for tracked work splits one task
        across two numbers, and the split is invisible from inside a single
        worktree. This reads the forge's open issues, the ledger's ownership
        and the reservations peers hold, and reports what the goal's own
        words already match. It writes nothing and claims nothing.

        Args:
            repo: Assigned worktree, which names the lane doing the reading.
            goal: What the lane intends to do, in the operator's words.
            limit: Most matches to return.

        Returns:
            The goal, the words it matched on, the peer reservations those
            words run into, and the matching open issues.

        Raises:
            BridgeError: If the worktree belongs to no registered lane.
        """
        import sqlite3

        _, directory = self.project(repo)
        data = roster.read(directory)
        lane = Path(git(repo, "rev-parse", "--show-toplevel")).resolve()
        agent = roster.resolve(data, lane)
        forge.select(repo, data)
        try:
            held = store.active_reservations(self.home, data["root"])
        except (BridgeError, OSError, sqlite3.Error):
            held = {}
        held.pop(data["participants"][agent]["display"], None)
        return {
            "participant": agent,
            **recommend.match(
                goal,
                forge.open_issues(repo),
                snapshot(directory),
                held,
                limit,
            ),
        }

    def issue_assign(
        self,
        repo: Path,
        number: str,
        name: str = "",
        *,
        reason: str = "",
        withdraw: bool = False,
    ) -> dict:
        """Offers one issue to a lane, or withdraws that offer again.

        The operator directs work by offering it, never by taking it. An
        unheld issue is offered to the named lane directly, and that lane
        answers the offer exactly as it answers a peer's. A held issue stays
        with its owner: the operator's wish is recorded as a request that
        owner answers, and only that answer creates the offer to the named
        lane. No command line moves ownership that a lane has not accepted.

        Args:
            repo: Any checkout of the target repository.
            number: Repository issue number being offered.
            name: Lane the issue is offered to.
            reason: Why the operator is moving the work. It travels with the
                offer and is kept on the record.
            withdraw: Whether to withdraw an operator offer no lane accepted.

        Returns:
            Which of the two paths was recorded, the issue, the identifier the
            answering lane quotes, the recipient, the current owner, and,
            where a request was recorded, whether its notice reached the
            owner's inbox and why it did not.

        Raises:
            BridgeError: If the repository has no project, the named lane is
                not a participant or already owns the issue, or no operator
                offer is pending to withdraw.
        """
        _, directory = self.project(repo)
        data = roster.read(directory)
        issue = parse_issue(number)
        record = change(
            directory,
            roster.OPERATOR,
            "unassign" if withdraw else "assign",
            number,
            participants=set(data["participants"]),
            to=name,
            summary=reason,
            defaults=data["deadlines"],
        )
        result = {
            "issue": int(issue),
            "recorded": "withdrawal",
            "owner": record["owner"],
            "to": name,
            "offer_id": "",
            "delivered": True,
            "detail": "",
        }
        if withdraw:
            return result
        if pending := record.get("request"):
            result.update(
                recorded="request",
                to=pending["to"],
                offer_id=pending["id"],
                **self._request_notice(data, record["owner"], issue, pending),
            )
            return result
        offer = record["offer"]
        result.update(recorded="offer", to=offer["to"], offer_id=offer["id"])
        return result

    def issue_resolve(
        self,
        repo: Path,
        number: str,
        *,
        reason: str = "",
        release: bool = False,
    ) -> dict:
        """Ends a claim whose holder never filed the completion it landed.

        A claim can otherwise only be ended by the lane that holds it, so work
        that is merged on the forge stays open in the ledger forever once that
        lane stops answering, and every capacity and load decision downstream
        reads the stale row. This is the operator's way out, and it is bounded
        on both sides. The forge is read here, now: the issue's own closing
        pull request speaks first, whichever branch it came from, and the
        lane branch's newest pull request only when the forge cannot say. The
        issue must have closed, or that pull request opened, inside the
        current ownership generation,
        so an unverified claim is never ended this way. The supervisor must
        already have escalated the claim as an unresolved completion, so a
        holder that is answering is never resolved out from under it. The
        transition is recorded as the operator's own, never as the lane's.

        Peers gain nothing here. The escalation goes to the operator, and no
        lane is given any power over another lane's claim.

        Args:
            repo: Any checkout of the target repository.
            number: Repository issue number being resolved.
            reason: Operator rationale kept beside the forge evidence.
            release: Whether to return the work to the queue instead of
                recording it complete. It is required for a pull request that
                was closed without merging, because nothing was integrated.

        Returns:
            The issue, the outcome recorded, the lane the claim was held by,
            the forge evidence that justified it and the resulting execution
            state.

        Raises:
            BridgeError: If the issue is unheld, no merged or closed pull
                request names the current claim, the evidence is closed and
                unmerged without `release`, or the claim carries no
                unresolved-completion escalation.
        """
        _, directory = self.project(repo, create=False)
        data = roster.read(directory)
        issue = parse_issue(number)
        record = snapshot(directory)["issues"].get(issue) or {}
        holder = record.get("owner")
        if not holder:
            raise BridgeError(f"Issue #{issue} has no owner.")
        participant = data["participants"].get(holder) or {}
        forge.select(repo, data)
        closing = forge.issue_completion(Path(data["root"]), issue)
        evidence: dict | None
        if closing and closing["state"] in ("MERGED", "CLOSED"):
            evidence = {
                "branch": closing["branch"] or f"issue #{issue}",
                "state": closing["state"],
                "created_at": closing["closed_at"],
                "commit": closing["commit"],
                "pull_request": closing["pull_request"],
                "url": closing["url"],
            }
        else:
            evidence = forge.branch_evidence(
                Path(data["root"]), str(participant.get("branch", ""))
            )
        if not evidence or evidence["state"] not in ("MERGED", "CLOSED"):
            raise BridgeError(
                f"No merged or closed pull request was observed for {holder}, "
                f"so issue #{issue} has no evidence to resolve it on."
            )
        if evidence["created_at"] < supervision.claimed_since(record):
            raise BridgeError(
                f"The newest pull request on {evidence['branch']} predates "
                f"the current claim on issue #{issue}, so it does not name "
                "this work."
            )
        if evidence["state"] != "MERGED" and not release:
            raise BridgeError(
                f"The pull request on {evidence['branch']} was closed without "
                "merging, so nothing was integrated; add --release to return "
                "the work to the queue."
            )
        resolved = lifecycle.resolve(
            directory,
            issue,
            evidence={**evidence, "observed_at": time.time()},
            outcome="release" if release else "complete",
            actor=roster.OPERATOR,
            reason=reason,
        )
        return {
            "issue": int(issue),
            "outcome": resolved["resolution"]["outcome"],
            "holder": holder,
            "owner": resolved["owner"],
            "state": lifecycle.state(resolved)["state"],
            "evidence": resolved["resolution"]["evidence"],
        }

    def _request_notice(
        self, data: dict, owner: str, issue: str, request: dict
    ) -> dict:
        """Delivers the operator's handoff request to the issue's owner.

        The ledger already carries the request when this runs, so a mailbox
        that cannot be written leaves the request in force and is reported
        rather than reversing it. The owner reads the offer identifier it
        must quote to authorize the handoff, and nothing here accepts,
        declines or transfers anything on that owner's behalf.

        Args:
            data: Project manifest for the repository.
            owner: Lane that holds the issue.
            issue: Repository issue number the request names.
            request: Recorded request, carrying its identifier and reason.

        Returns:
            Whether the notice reached the owner's inbox, and the failure
            text when it did not.
        """
        participant = data["participants"].get(owner) or {}
        subject = f"Operator request: hand issue #{issue} to {request['to']}"
        body = "\n".join(
            [
                f"The operator asks you to hand issue #{issue} to "
                f"{request['to']}.",
                f"Reason: {request['reason'] or 'none given'}",
                f"Authorize it with: agent-parley issue accept {issue} "
                f"--offer-id {request['id']}",
                f"Refuse it with: agent-parley issue decline {issue} "
                f"--offer-id {request['id']}",
                "You keep the issue until you authorize the handoff, and "
                f"{request['to']} owns it only after accepting the offer "
                "your authorization creates.",
            ]
        )
        identity = participant.get("display", owner)
        try:
            store.speak(
                self.home,
                data["root"],
                identity,
                subject,
                body,
                operator_key(identity, subject, body),
            )
        except BridgeError as exc:
            return {"delivered": False, "detail": str(exc)}
        return {"delivered": True, "detail": ""}

    def issue_reading(self, repo: Path, number: str) -> dict:
        """Reads one issue's ownership, reservations and recorded history.

        The reading opens no lock and writes nothing, so it is safe beside
        running lanes. Reservations are advisory declarations by the lane
        that owns the issue, not filesystem locks.

        Args:
            repo: Any checkout of the target repository.
            number: Issue number, with or without its leading hash.

        Returns:
            The project root, the issue number, the published ledger and the
            issue's record in it, the reservation keys its owner holds, and
            the history reading for the same issue.

        Raises:
            BridgeError: If the number is unusable or the project has none.
        """
        root, directory = self.project(repo, create=False)
        data = roster.read(directory)
        identifier = parse_issue(number)
        ledger = snapshot(directory)
        record = ledger["issues"].get(identifier) or {}
        owner = record.get("owner") or ""
        held = store.active_reservations(self.home, data["root"])
        identity = (
            data["participants"][owner]["display"]
            if owner in data["participants"]
            else ""
        )
        return {
            "root": str(root),
            "issue": identifier,
            "ledger": ledger,
            "record": record or None,
            "owner": owner,
            "reservations": held.get(identity, []),
            "history": self.history(repo, "issue", identifier),
        }

    def work_plan(
        self, repo: Path, action: str, path: Path | None = None
    ) -> dict:
        """Applies, compares or reports the repository's work-order plan.

        A plan records advisory dependencies and nothing else. Applying one
        claims no issue, assigns no lane and gates no transition, so a plan
        that turns out to be wrong never blocks anybody.

        Args:
            repo: Any checkout of the target repository.
            action: Apply, diff, or show.
            path: Plan file for apply and diff.

        Returns:
            The recorded version for apply, the comparison for diff, or the
            applied plan beside current ownership for show.

        Raises:
            BridgeError: If the plan file is unusable or the ledger cannot be
                locked.
        """
        _, directory = self.project(repo)
        if action == "show":
            return plan.describe(directory, reported_ready(directory))
        if path is None:
            raise BridgeError("Name the plan file to apply or compare.")
        if action == "apply":
            return plan.apply(directory, path)
        return plan.diff(directory, path)
