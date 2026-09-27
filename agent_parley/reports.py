"""Records lane reports and reviews, and reads, exports and archives history.

`ReportsMixin` adds ready/partial/blocked report recording, peer review of a
report, hook event export, state archive management and ownership history
reads to `cli.Bridge`.

Method bodies resolve the module-level names they use through `cli` when they
run, so a name bound or replaced there, including a test's patch, is the one a
moved method reads. This module never imports `cli` at import time.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

from agent_parley import BridgeError
from agent_parley.core import BridgeCore

if TYPE_CHECKING:
    import argparse


class ReportsMixin(BridgeCore):
    """Lane reports, peer review, hook event export, archive and history."""

    def report(
        self,
        repo: Path,
        outcome: str,
        summary: str,
        remaining: str,
        evidence: str,
        key: str = "",
        issue: str = "",
        resume_on: str = "",
        backlog: int | None = None,
    ) -> str:
        """Records an explicitly reported outcome independently of activity.

        A lane that newly reaches the ready state also posts its account to
        the exact issue reported, so a reviewer reading the forge sees the
        same summary and evidence the lane recorded. The comment is best
        effort and is posted once per arrival at the state.

        A ready report on a claim the supervisor already observed complete,
        whose pull request ended inside the current ownership generation, is
        the holder's turn to finish it. The report returns the completion
        the holder still owes, so it acts in the same turn instead of waiting
        for the reminder to reach it on a later wake.

        Args:
            repo: Assigned agent worktree.
            outcome: Partial, blocked, or ready-for-review state.
            summary: Nonempty account of the result.
            remaining: Required unfinished work for partial or blocked reports.
            evidence: Required verification evidence for ready reports.
            key: Idempotency key. A retried report carrying the key it first
                used records no second attempt and posts no second comment.
            issue: Exact owned issue, inferred only for a sole claim.
            resume_on: Existing authorized issue whose completion resumes a
                blocked report.
            backlog: Work units still remaining on this claim, in whatever the
                claim itself counts. Recording the count is what lets the
                supervisor offer a split once this lane goes idle on it. None
                leaves any recorded count as it stands.

        Returns:
            The completion the holder owes on an observed-complete claim for
            a ready report, otherwise an empty string.

        Raises:
            BridgeError: If the lane or required report fields are invalid, or
                if the key already names a report with other content.
        """
        from agent_parley.cli import (
            attachments,
            change_attempt,
            exact_claim,
            forge,
            git,
            issues,
            json,
            lifecycle,
            lock,
            metrics,
            report_comment,
            retries,
            roster,
            supervision,
            uuid,
            write_json,
        )

        if not summary.strip():
            raise BridgeError("Reports require a nonempty --summary.")
        _, directory = self.project(repo)
        data = roster.read(directory)
        lane = Path(git(repo, "rev-parse", "--show-toplevel")).resolve()
        agent = roster.resolve(data, lane)
        claim = exact_claim(directory, agent, issue)
        commit = git(lane, "rev-parse", "HEAD")
        if outcome in ("partial", "blocked") and not remaining.strip():
            raise BridgeError("Partial/blocked reports require --remaining.")
        if outcome == "ready" and not evidence.strip():
            raise BridgeError("Ready-for-review reports require --evidence.")
        if resume_on and outcome != "blocked":
            raise BridgeError("--resume-on is valid only for blocked reports.")
        for field, value in (("summary", summary), ("remaining", remaining)):
            if len(value.encode()) > metrics.MAX_REPORT_BYTES:
                raise BridgeError(
                    f"Report --{field} exceeds its "
                    f"{metrics.MAX_REPORT_BYTES}-byte budget; put the detail "
                    "in --evidence, which is attached when it is longer."
                )
        identifier = uuid.uuid4().hex[:16]
        evidence, attached = attachments.spill(
            directory,
            "report",
            identifier,
            evidence,
            metrics.MAX_REPORT_BYTES,
            agent,
            [],
        )
        scope = retries.scope(agent, "report", retries.validate(key))
        fingerprint = retries.digest(
            "report",
            {
                "state": outcome,
                "summary": summary,
                "remaining": remaining,
                "evidence": evidence,
                "issue": claim["issue"],
                "claim_id": claim["claim_id"],
                "resume_on": resume_on,
                "backlog": backlog,
            },
        )
        path = directory / f"{agent}-activity.json"
        with lock(directory / f"{agent}-report.lock", timeout=1):
            with lock(directory / f"{agent}-checkpoint.lock", timeout=1):
                state = json.loads(path.read_text()) if path.exists() else {}
                if key and (recorded := state.get("retries", {}).get(scope)):
                    retries.replayed(recorded, "report", key, fingerprint)
                    return ""
            lifecycle.record_report(
                directory,
                agent,
                outcome,
                commit,
                remaining,
                str(claim["issue"]) if claim["issue"] is not None else "",
                claim["claim_id"] or "",
                resume_on,
                backlog,
            )
            with lock(directory / f"{agent}-checkpoint.lock", timeout=1):
                state = json.loads(path.read_text()) if path.exists() else {}
                arrived = outcome == "ready" and state.get("outcome") != "ready"
                state.update(
                    outcome=outcome,
                    summary=summary,
                    remaining=remaining,
                    evidence=evidence,
                    reported_at=time.time(),
                )
                if key:
                    retries.remember(
                        state,
                        scope,
                        fingerprint,
                        retries.SERVED,
                        {"state": outcome},
                    )
                write_json(path, state)
        metrics.record_report(
            directory,
            agent,
            {
                "id": identifier,
                "kind": "report",
                "state": outcome,
                "issue": claim["issue"],
                "claim_id": claim["claim_id"],
                "summary": summary,
                "remaining": remaining,
                "evidence": evidence,
                "attachment": attached or None,
            },
        )
        owned = [str(claim["issue"])] if claim["issue"] is not None else []
        if outcome == "blocked":
            change_attempt(directory, agent, owned)
        if arrived:
            body = report_comment(summary, evidence)
            forge.select(repo, data)
            if claim["issue"] is not None:
                forge.comment(repo, str(claim["issue"]), body)
        if outcome != "ready" or claim["issue"] is None:
            return ""
        number = str(claim["issue"])
        record = issues.snapshot(directory)["issues"].get(number) or {}
        prompt = record.get("handoff_prompt") or {}
        if (
            prompt.get("trigger") != supervision.ENDED
            or prompt.get("responded_at")
            or prompt.get("holder") != agent
            or record.get("owner") != agent
        ):
            return ""
        waiting = ", ".join(prompt.get("waiting") or []) or "project peers"
        return (
            f"Issue #{number} is observed complete: its pull request ended "
            "during this claim. Complete it now: send the completion message "
            f"to {waiting} with the commit, verification and remaining work. "
            f"The operator then ends the claim with `issue resolve {number}`."
        )

    def show_report(self, repo: Path, identifier: str, full: bool) -> dict:
        """Reads one of this lane's durable report records.

        Args:
            repo: Assigned agent worktree.
            identifier: Report record identifier.
            full: Whether to read the attached evidence whole.

        Returns:
            The record, with the whole attachment under ``attachment_body``
            when asked for and present.

        Raises:
            BridgeError: If the lane is unknown or no record carries the
                identifier.
        """
        from agent_parley.cli import attachments, git, metrics, roster

        _, directory = self.project(repo)
        data = roster.read(directory)
        lane = Path(git(repo, "rev-parse", "--show-toplevel")).resolve()
        agent = roster.resolve(data, lane)
        for record in reversed(metrics.report_records(directory, agent)):
            if record.get("id") == identifier:
                break
        else:
            raise BridgeError(
                f"No report {identifier} is recorded for {agent}."
            )
        if full and record.get("attachment"):
            record["attachment_body"] = attachments.body(
                directory, str(record["attachment"]), agent
            )
        if record.get("kind") == "report":
            record["review"] = metrics.latest_review(
                directory, agent, identifier
            )
        return record

    def review_report(
        self, repo: Path, identifier: str, verdict: str, evidence: str
    ) -> dict:
        """Records this lane's verdict on a peer's report.

        The worktree the command runs in selects the reviewing lane, exactly
        as it selects the lane a report is written for, so the author of a
        report cannot record a verdict on it. The verdict is that peer's own
        claim about work it did not do: it is neither an operator approval nor
        independent verification, and it moves no ownership.

        Args:
            repo: Assigned agent worktree of the reviewing lane.
            identifier: Report record the verdict judges.
            verdict: Reviewed outcome, pass or fail.
            evidence: Nonempty account of what the reviewer checked.

        Returns:
            The recorded verdict.

        Raises:
            BridgeError: If the lane is unknown, no participant recorded the
                report, the reviewing lane wrote it, or the verdict or its
                evidence is invalid.
        """
        from agent_parley.cli import git, metrics, roster

        _, directory = self.project(repo)
        data = roster.read(directory)
        lane = Path(git(repo, "rev-parse", "--show-toplevel")).resolve()
        agent = roster.resolve(data, lane)
        return metrics.record_review(
            directory,
            list(data["participants"]),
            agent,
            identifier,
            verdict,
            evidence,
        )

    def export_events(
        self,
        repo: Path,
        participants: tuple[str, ...] = (),
        window: float = 0.0,
        output: Path | None = None,
    ) -> str:
        """Writes retained hook event records as JSON Lines.

        Each line carries the participant that produced the record, so an
        export of several lanes stays attributable. Records are grouped by
        participant and remain oldest first within one, which is the order
        the log retains them in.

        Each line also names its kind in ``record``: ``event`` for a hook
        decision, ``idle_interval`` for a measured stretch of observed
        coordination inactivity, and ``wait`` for how long a message, an
        acknowledgement, a handoff offer or a ready report waited. The
        intervals and waits leave with the records so they can be kept and
        compared across sessions, providers and accounts once retention has
        discarded the events they were derived from.

        Args:
            repo: Any checkout of the target repository.
            participants: Participants to export; every participant when
                empty.
            window: Seconds of history to export; everything retained when
                zero.
            output: Destination file, or None to write to standard output.

        Returns:
            An account of what was exported.

        Raises:
            BridgeError: If a named participant is not in this project.
            OSError: If the destination cannot be written.
        """
        from agent_parley.cli import json, metrics, read_events, roster

        _, directory = self.project(repo)
        data = roster.read(directory)
        names = sorted(data["participants"])
        unknown = sorted(set(participants) - set(names))
        if unknown:
            raise BridgeError(
                f"Not a participant in this project: {', '.join(unknown)}; "
                "run agent-parley participant list."
            )
        selected = [
            name for name in names if not participants or name in participants
        ]
        since = time.time() - window if window else 0.0
        lines = []
        for name in selected:
            lines += [
                json.dumps({"participant": name, "record": "event", **entry})
                for entry in read_events(directory, name, since)
            ]
            idle = metrics.idle_intervals(directory, name, since)
            lines += [
                json.dumps(
                    {
                        "participant": name,
                        "record": "idle_interval",
                        "complete": idle["complete"],
                        **interval,
                    }
                )
                for interval in idle["intervals"]
            ]
            lines += [
                json.dumps({"participant": name, "record": "wait", **wait})
                for wait in metrics.waits(
                    self.home, directory, data, name, since
                )
            ]
        text = "".join(f"{line}\n" for line in lines)
        if output is None:
            sys.stdout.write(text)
        else:
            output.write_text(text, encoding="utf-8")
        covered = (
            f"the last {int(window)}s" if window else "everything retained"
        )
        destination = "standard output" if output is None else str(output)
        return (
            f"Exported {len(lines)} records from {len(selected)} participants "
            f"covering {covered} to {destination}."
        )

    def state_archive(self, args: argparse.Namespace) -> str:
        """Exports, inspects or imports the state archive the operator named.

        An export names its archive and what it covers; an import names the
        projects restored and every participant whose lane path does not
        exist on this machine. Those lanes are not recreated: the operator
        recreates the worktree, and `run` then registers the participant
        again, because the archive carries no credential.

        Args:
            args: Parsed `state` command line.

        Returns:
            An account of what was written, read or restored.

        Raises:
            BridgeError: If the archive or the state directory refuses the
                operation.
        """
        from agent_parley.cli import archive

        if args.action == "show":
            return archive.describe(archive.read_manifest(args.archive))
        if args.action == "export":
            directory = None
            if args.project is not None:
                _, directory = self.project(
                    args.project.resolve(), create=False
                )
            manifest = archive.export(self.home, args.output, directory)
            names = ", ".join(entry["root"] for entry in manifest["projects"])
            return (
                f"Exported {len(manifest['projects'])} projects "
                f"({names or 'none'}) at schema {manifest['schema']} to "
                f"{args.output}; credentials excluded."
            )
        root = str(args.project.resolve()) if args.project else None
        result = archive.import_archive(
            self.home, args.archive, root, args.merge
        )
        lines = [
            f"Imported {len(result['projects'])} projects into {self.home}; "
            "every participant registers again on its next run."
        ]
        for entry in result["missing_lanes"]:
            lines.append(
                f"Lane missing for {entry['name']} in {entry['project']}: "
                f"{entry['lane'] or 'no path recorded'}; recreate the "
                "worktree before launching it."
            )
        return "\n".join(lines)

    def history(
        self,
        repo: Path,
        subject: str = "",
        value: str = "",
        *,
        kinds: tuple[str, ...] = (),
        participant: str = "",
        provider: str = "",
        issue: str = "",
        window: float = 0.0,
    ) -> dict:
        """Reads ownership history for one issue, lane or claim.

        The store is opened read-only, no lock is taken and no record is
        rewritten, so a history query is safe beside running lanes. Retention
        follows the substrate each record lives in: the issue ledger and the
        report log keep their records until the project is removed, while mail
        and reservations keep theirs for as long as the store does.

        Args:
            repo: Any checkout of the target repository.
            subject: Issue, participant or claim.
            value: The issue number, participant name or claim identifier.
            kinds: Record kinds to report; every kind when empty.
            participant: Lane filter applied to every listing.
            provider: Provider filter applied to every listing.
            issue: Issue filter applied to every listing.
            window: Seconds of history to report; everything when zero.

        Returns:
            The matching records, with the ownership generations of an issue
            listing.

        Raises:
            BridgeError: If the subject is unknown or the project has none.
        """
        from agent_parley.cli import history, parse_issue, roster

        _, directory = self.project(repo, create=False)
        data = roster.read(directory)
        since = time.time() - window if window else 0.0
        claim = ""
        held: list[dict] = []
        if subject == "issue":
            issue = parse_issue(value)
            held = history.holdings(directory, issue)
        elif subject == "participant":
            if value not in data["participants"]:
                raise BridgeError(
                    f"{value} is not a participant in this project; "
                    "run agent-parley participant list."
                )
            participant = value
        elif subject == "claim":
            claim = value
        return {
            "subject": subject or "project",
            "value": value,
            "holdings": held,
            "records": history.records(
                self.home,
                directory,
                data,
                kinds=kinds,
                participant=participant,
                provider=provider,
                issue=issue,
                claim=claim,
                since=since,
            ),
        }
