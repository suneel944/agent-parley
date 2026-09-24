"""Reads, narrows and prints lane status, health checks and problems."""

from __future__ import annotations

import os
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from agent_parley import BridgeError
from agent_parley.integration import reported_ready
from agent_parley.lazy import DeferredCallable, deferred, deferred_module
from agent_parley.worktrees import Worktrees, drift

if TYPE_CHECKING:
    import contextlib
    import json
    import sqlite3

    from agent_parley import (
        approvals,
        budgets,
        checkpoints,
        inbound,
        issues,
        lanes,
        metrics,
        plan,
        problems,
        process,
        protocol,
        reclaim,
        roster,
        store,
        supervision,
        tables,
        views,
    )
    from agent_parley.checkpoints import (
        activity,
        lane_branch,
        mailbox,
        participant_liveness,
    )
    from agent_parley.issues import (
        deadline_state,
        describe,
        handoff_fields,
        offer_state,
        orphan_age,
        snapshot,
    )
else:
    contextlib = deferred_module("contextlib")
    json = deferred_module("json")
    approvals = deferred("approvals")
    budgets = deferred("budgets")
    checkpoints = deferred("checkpoints")
    inbound = deferred("inbound")
    issues = deferred("issues")
    lanes = deferred("lanes")
    metrics = deferred("metrics")
    plan = deferred("plan")
    problems = deferred("problems")
    process = deferred("process")
    protocol = deferred("protocol")
    reclaim = deferred("reclaim")
    roster = deferred("roster")
    store = deferred("store")
    supervision = deferred("supervision")
    tables = deferred("tables")
    views = deferred("views")
    activity = DeferredCallable(checkpoints, "activity")
    lane_branch = DeferredCallable(checkpoints, "lane_branch")
    mailbox = DeferredCallable(checkpoints, "mailbox")
    participant_liveness = DeferredCallable(checkpoints, "participant_liveness")
    deadline_state = DeferredCallable(issues, "deadline_state")
    describe = DeferredCallable(issues, "describe")
    handoff_fields = DeferredCallable(issues, "handoff_fields")
    offer_state = DeferredCallable(issues, "offer_state")
    orphan_age = DeferredCallable(issues, "orphan_age")
    snapshot = DeferredCallable(issues, "snapshot")


class Selection(NamedTuple):
    """Narrows a status reading to the lanes an operator asked about.

    Every field is a filter, and filters combine: a lane is reported only
    when it satisfies all of them. The same selection narrows the table and
    the machine-readable document, so a script and an operator never disagree
    about which lanes matched.

    Attributes:
        participant: Single participant to report in full detail.
        project: Repository root to report, as a path or as recorded.
        providers: Providers to report; every provider when empty.
        outcome: Reported outcome to report, such as ``ready``.
        drifted: Report only lanes away from their assigned branch.
        pending: Report only lanes holding unread mail, unanswered
            acknowledgements, an offer, or a reservation past its declared
            time to live.
        idle: Report only live lanes that served no coordination call inside
            the window.
        since: Seconds of coordination inactivity `idle` requires; the
            project's configured interval when zero.
        over_budget: Report only lanes over any of their advisory token,
            call or hour limits.
        issue: Issue number a lane must hold or be offered.
    """

    participant: str = ""
    project: str = ""
    providers: tuple[str, ...] = ()
    outcome: str = ""
    drifted: bool = False
    pending: bool = False
    idle: bool = False
    since: float = 0.0
    over_budget: bool = False
    issue: int = 0

    def filtered(self) -> bool:
        """Reports whether the operator narrowed the reading at all."""
        return bool(
            self.participant
            or self.project
            or self.providers
            or self.outcome
            or self.drifted
            or self.pending
            or self.idle
            or self.over_budget
            or self.issue
        )

    def describe(self) -> str:
        """Names the filters that were applied, for an empty result."""
        applied = []
        if self.participant:
            applied.append(f"participant {self.participant}")
        if self.project:
            applied.append(f"--project {self.project}")
        applied.extend(f"--provider {name}" for name in self.providers)
        if self.outcome:
            applied.append(f"--outcome {self.outcome}")
        if self.drifted:
            applied.append("--drifted")
        if self.pending:
            applied.append("--pending")
        if self.idle:
            applied.append("--idle")
        if self.over_budget:
            applied.append("--over-budget")
        if self.issue:
            applied.append(f"--issue {self.issue}")
        return " ".join(applied)

    def holds_project(self, root: str) -> bool:
        """Reports whether one project root satisfies the project filter."""
        if not self.project:
            return True
        return root in (
            self.project,
            str(Path(self.project).expanduser().resolve()),
        )

    def holds(self, record: dict, offers: tuple[int, ...]) -> bool:
        """Reports whether one lane satisfies every applied filter.

        Args:
            record: One lane record from the status reading.
            offers: Issue numbers offered to this lane and still pending.

        Returns:
            Whether the lane is reported. A mailbox that could not be read
            answers no pending work rather than inventing a count.
        """
        mail = record["mail"] or {}
        held = [claim["issue"] for claim in record["claims"]]
        waiting = bool(
            mail.get("unread")
            or mail.get("pending_ack")
            or mail.get("stale_reservations")
            or offers
        )
        return (
            (not self.participant or record["participant"] == self.participant)
            and (not self.providers or record["provider"] in self.providers)
            and (not self.outcome or record["outcome"] == self.outcome)
            and (not self.drifted or record["drift"])
            and (not self.pending or waiting)
            and (not self.idle or self._inactive(record))
            and (
                not self.over_budget
                or (record.get("budget") or {}).get("over", False)
            )
            and (not self.issue or self.issue in held or self.issue in offers)
        )

    def _inactive(self, record: dict) -> bool:
        """Reports whether a live lane served no call inside the window."""
        if not record["availability"]["process_alive"]:
            return False
        if self.since:
            return record["idle"]["served_age_seconds"] >= self.since
        return record["idle"]["stalled"]


def pending_offers(project: dict, agent: str) -> tuple[int, ...]:
    """Reports the issues offered to one participant and still unanswered.

    Args:
        project: One project record from the status reading.
        agent: Participant the offers are addressed to.

    Returns:
        Issue numbers in ledger order. An offer is a request: it moves no
        ownership until the participant accepts it.
    """
    return tuple(
        record["issue"]
        for record in project["issues"]
        if record["offer"] and record["offer"]["to"] == agent
    )


def narrow(report: dict, selection: Selection) -> dict:
    """Applies a selection to a status reading without changing its shape.

    Args:
        report: Reading produced by `Bridge.status_snapshot`.
        selection: Filters the operator asked for.

    Returns:
        The same document with each project holding only the lanes that
        matched, and without the projects the project filter excluded. Every
        other field is carried through, so the machine-readable contract is
        the unfiltered one with fewer rows.
    """
    projects = []
    for project in report["projects"]:
        if not selection.holds_project(project["root"]):
            continue
        projects.append(
            {
                **project,
                "participants": [
                    record
                    for record in project["participants"]
                    if selection.holds(
                        record, pending_offers(project, record["participant"])
                    )
                ],
            }
        )
    return {**report, "projects": projects}


def reviewed_line(review: dict) -> str:
    """States one peer verdict as the claim it is.

    Args:
        review: Verdict record a peer wrote against a lane's report.

    Returns:
        One line naming the verdict, the peer that recorded it and the report
        it judges, and saying that a verdict is that peer's own claim rather
        than independent verification.
    """
    return (
        f"Peer review: {review.get('verdict', '')} by "
        f"{review.get('reviewer', '')} on report "
        f"{review.get('report_id', '')}; a verdict is the reviewing lane's "
        "own claim about work it did not do, not independent verification."
    )


def supervision_failure(error: dict) -> str:
    """States that the project's supervision poll is failing, and since when.

    Args:
        error: Failure record read beside the project's issue ledger.

    Returns:
        One line naming how long polls have been failing and the last failure,
        so a supervisor that fails on every tick never reads as healthy.
    """
    since = max(int(time.time() - error["since"]), 0)
    return f"Supervision: failing for {since}s; last: {error['detail']}"


def supervision_liveness(polled: dict) -> str:
    """States when supervision last polled the project and how long it took.

    A supervision thread that died leaves this reading ageing, so the line
    distinguishes a supervisor that stopped from one that is merely quiet.

    Args:
        polled: Poll record written beside the project's issue ledger.

    Returns:
        One line naming the age and wall time of the last poll, its slowest
        stage, and the age of the last poll in which no step failed.
    """
    now = time.time()
    line = (
        f"Supervision: last poll {max(int(now - polled['at']), 0)}s ago "
        f"in {float(polled.get('seconds', 0)):.2f}s"
    )
    stages = polled.get("stages")
    if isinstance(stages, dict) and stages:
        slowest = max(stages, key=lambda label: float(stages[label]))
        line += f", slowest {slowest} {float(stages[slowest]):.2f}s"
    clean = polled.get("clean_at")
    if clean is None:
        return line + "; no clean poll recorded"
    if clean != polled["at"]:
        return line + f"; last clean {max(int(now - clean), 0)}s ago"
    return line


def wake_schedule(wake: dict) -> str:
    """States when a parked lane is asked again, or why it is not.

    Args:
        wake: Wake reading from a participant record.

    Returns:
        One line naming the next attempt and the cause that postponed it, or
        the exhausted budget with the last cause. A record written before the
        schedule existed reports that it is still to be re-decided rather than
        inventing a time.
    """
    cause = wake.get("blocked") or wake.get("result") or "unknown"
    seconds = wake.get("next_seconds")
    if wake.get("exhausted"):
        exhausted = f"Wake budget exhausted; last cause: {cause}"
        if seconds is None:
            return exhausted
        return f"{exhausted}; backing off, next wake in {seconds}s"
    if seconds is None:
        return "Next wake: due on the next re-evaluation"
    blocked = f"; blocked: {wake['blocked']}" if wake.get("blocked") else ""
    return f"Next wake in {seconds}s at {wake['next_at']}{blocked}"


def review_fields(review: dict | None) -> dict | None:
    """Reports one peer verdict in the shape every snapshot carries it.

    Args:
        review: Verdict record a peer wrote, or None when none is recorded.

    Returns:
        The verdict, its reviewer, the report it judges, its evidence and its
        age, with ``independent_verification`` false so a machine reader
        carries the same limit the printed views state. None when no peer
        recorded a verdict.
    """
    if not review:
        return None
    at = float(review.get("at", 0) or 0)
    return {
        "review_id": review.get("id", ""),
        "report_id": review.get("report_id", ""),
        "reviewer": review.get("reviewer", ""),
        "verdict": review.get("verdict", ""),
        "evidence": review.get("evidence", ""),
        "recorded_at": views.timestamp(at or None),
        "age_seconds": int(time.time() - at) if at else None,
        "independent_verification": False,
    }


def shown_record(record: dict) -> str:
    """Formats one shown message or report for a terminal.

    The record's fields print one per line, then the stored body or
    evidence, then the whole attachment when it was read.

    Args:
        record: Message or report record, optionally carrying
            ``attachment_body``.

    Returns:
        The text to print.
    """
    body = record.get("body_md", record.get("evidence", ""))
    attached = record.get("attachment_body")
    review = record.get("review")
    fields = [
        f"{name}: {value}"
        for name, value in record.items()
        if name not in ("body_md", "evidence", "attachment_body", "review")
    ]
    text = "\n".join(fields) + "\n\n" + str(body)
    if review:
        text += f"\n\n{reviewed_line(review)}\n{review.get('evidence', '')}"
    if attached is not None:
        text += f"\n\n--- attachment {record.get('attachment')} ---\n{attached}"
    return text


def reported_lanes(report: dict) -> int:
    """Counts the lanes a narrowed status reading still holds."""
    return sum(len(project["participants"]) for project in report["projects"])


def lane_detail(record: dict, data: dict) -> None:
    """Prints one lane's full reading under its table row.

    Args:
        record: One lane record from the status reading.
        data: Project manifest holding the participant.
    """
    agent = record["participant"]
    account = record["credential"] or "default account"
    print(
        f"  {agent} ({record['identity']}): {record['session']}\n"
        f"    Provider: {record['provider']}; {account}"
    )
    print(
        f"    Availability: {record['availability']['state']}; "
        "session process alive: "
        f"{record['availability']['process_alive']}"
    )
    if record["drift"]:
        print(
            "    " + drift(agent, data["participants"][agent], record["branch"])
        )
    if record["idle"]["stalled"]:
        print(f"    {record['idle']['marker']}")
    if record["budget"]["marker"]:
        print(f"    {record['budget']['marker']}")
    print(
        "    Observed coordination inactivity: "
        f"{record['idle_seconds']}s"
        + ("" if record["idle_complete"] else " (incomplete)")
    )
    for wait in record["waiting"]:
        item = wait.get("message_id") or wait.get("issue") or ""
        print(
            f"    Waiting {wait['seconds']}s: {wait['kind']}"
            + (f" {item}" if item else "")
        )
    for claim in record["claims"]:
        if claim.get("orphaned"):
            held = claim.get("orphan_reservations") or []
            recorded = claim.get("orphan_recorded_seconds") or 0
            print(
                f"    Issue #{claim['issue']} was marked orphaned "
                f"{recorded}s ago: {claim['orphan_reason']}; still owned "
                f"until a peer runs issue claim {claim['issue']} "
                "--take-orphaned"
                + (f"; holds {', '.join(held)}" if held else "")
            )
        if claim.get("unresolved"):
            print(
                f"    Issue #{claim['issue']} has an unresolved completion: "
                f"{claim.get('reason', '')}; still owned until the operator "
                f"runs issue resolve {claim['issue']}"
            )
        if claim["overdue"]:
            print(
                f"    Issue #{claim['issue']} is overdue by "
                f"{claim['overdue_seconds']}s and still owned."
            )
        if claim["budget"]:
            print(
                f"    Issue #{claim['issue']} attempts "
                f"{claim['attempts']}/{claim['budget']}"
                + (
                    "; budget exceeded and still owned"
                    if claim["budget_exceeded"]
                    else ""
                )
            )
    print(f"    Reported outcome: {record['outcome']}")
    approval = record["approval"] or {}
    if approval.get("state", approvals.UNREPORTED) != approvals.UNREPORTED:
        detail = approval["detail"]
        print(
            f"    Approval: {approval['state']}"
            + (f"; {detail}" if detail else "")
        )
    print(
        f"    Context delivered: {record['injected_bytes']} "
        f"UTF-8 bytes in {record['injections']} notices"
    )
    if record["report_age_seconds"] is not None:
        print(f"    Report age: {record['report_age_seconds']}s")
    if record["summary"]:
        print(f"    Summary: {record['summary']}")
    if record["remaining"]:
        print(f"    Remaining: {record['remaining']}")
    if record["evidence"]:
        print(f"    Reported verification: {record['evidence']}")
    if review := record["review"]:
        print(f"    {reviewed_line(review)}")
        if review["evidence"]:
            print(f"    Peer review evidence: {review['evidence']}")
    if wake := record["wake"]:
        print(
            f"    Runtime wake: {wake['result']}; "
            f"attempt {wake['attempts']}"
            + (f"/{wake['budget']}" if wake.get("budget") else "")
            + f"; {wake['age_seconds']}s ago"
        )
        print(f"    {wake_schedule(wake)}")
    if foreign := record.get("foreign_session"):
        print(
            f"    Second session: {foreign['session_id'] or 'unnamed'} "
            f"(pid {foreign['pid']}) sends hooks as this lane; "
            "its events are ignored"
        )
    mail = record["mail"] or {}
    if "error" in mail:
        print(f"    Coordination unavailable: {mail['error']}")
        return
    stale = mail["stale_reservations"]
    age = mail.get("stale_reservation_age", 0)
    print(
        f"    Unread: {mail['unread']}"
        + (
            f" ({mail['superseded']} superseded)"
            if mail.get("superseded")
            else ""
        )
        + f"; pending acknowledgements: {mail['pending_ack']}; "
        + f"active reservations: {mail['reservations'] - stale}"
        + (
            f"; expired: {stale}, oldest {age}s past its deadline"
            if stale
            else ""
        )
    )
    if edited := record["operator_edits"]:
        print("    " + supervision.operator_edit_marker(edited))
    if advanced := record["base_advance_paths"]:
        print("    " + supervision.base_advance_marker(advanced))
    if mail["named_resources"]:
        print("    Named resources held: " + ", ".join(mail["named_resources"]))
    if topics := mail.get("unread_topics"):
        print(
            "    Unread by topic: "
            + ", ".join(f"{topic} {count}" for topic, count in topics.items())
        )
    if mail.get("queued_requests"):
        print(
            "    Reservation requests queued on its keys: "
            f"{mail['queued_requests']}"
            + (
                " (" + ", ".join(mail["queued_by"]) + ")"
                if mail.get("queued_by")
                else ""
            )
        )
    if mail.get("refused"):
        print("    Refused a key it holds: " + ", ".join(mail["refused"]))
    print(f"    Last coordination: {mail['last_coordination_at']}")
    for pending in mail["outstanding_ack"]:
        print(
            "    Awaiting acknowledgement: "
            f"message {pending['message_id']} "
            f"from {pending['sender']}; "
            f"{pending['age_seconds']}s"
        )
    print(f"    Latest prompt/task (reported): {mail['task']}")
    if mail["awaiting_delivery"]:
        print(
            "    Awaiting checkpoint delivery: "
            f"{mail['awaiting_delivery']} "
            "(batch capped at 3)"
        )


def inbound_status() -> dict:
    """Describes inbound status without loading its transport when disabled."""
    if not os.environ.get("AGENT_PARLEY_INBOUND", "").strip():
        return {"enabled": False, "fault": ""}
    return inbound.reported()


class Status(Worktrees):
    """Reads every lane's state and prints the operator's status views."""

    def doctor(self) -> dict:
        """Reports the launcher, plugin, store and service fit.

        The command reads. It opens no lane, writes no configuration and
        repairs nothing, so it stays safe to run while lanes are working, and
        it reports no credential, token or path inside a credential profile.
        It does ask a running service what code it is answering from, which
        is a reading the launcher cannot take from its own process.

        Returns:
            The launcher's package version and wire protocol, the protocol each
            shipped plugin manifest declares, the store's schema version
            against the schema this build writes, the code a running service
            is answering from, and whether the whole set is consistent. A
            service that started before the sources moved is reported stale,
            because it answers from modules the checkout no longer holds. A
            service that is not running is no drift either, but on a machine
            that holds a registered lane it is an outage: every hook on that
            machine pays the in-process decision, so the state carries the
            command that starts a service and the set is not consistent. A
            machine with no lane registered has nothing to serve and stays
            consistent with no service running. Each component
            carries the state this build puts it in and the one command that
            state needs. A store behind this build is
            not consistent: every process running this code queries columns it
            does not have, so reporting it as compatible would describe a
            healthy system while every lane is denied. The report also names
            the kernel release, the WSL generation or ``none``, and whether
            ``pidfd_open`` is available, so a platform gap is read here
            before a lane is started.
        """
        components = [
            {
                "component": "launcher",
                "version": protocol.launcher_version(),
                "protocol": protocol.PROTOCOL,
                "state": protocol.OK,
                "remedy": "",
                "compatible": True,
            }
        ]
        for client, manifest in protocol.manifests(
            protocol.plugin_root()
        ).items():
            declared = protocol.installed(manifest)
            accepted = protocol.compatible(declared)
            components.append(
                {
                    "component": f"{client} plugin",
                    "version": "",
                    "protocol": declared,
                    "state": protocol.OK if accepted else protocol.MISMATCH,
                    "remedy": "" if accepted else protocol.UPDATE,
                    "compatible": accepted,
                }
            )
        schema = store.schema_version(self.home)
        state = store.schema_state(schema)
        components.append(
            {
                "component": "store",
                "version": f"schema {schema}",
                "protocol": protocol.PROTOCOL,
                "state": state,
                "remedy": store.remedy(state),
                "compatible": state in store.SCHEMA_USABLE,
            }
        )
        served = self.health()
        serving = served.get("status") or protocol.STOPPED
        stale = serving == protocol.STALE
        stopped = serving == protocol.STOPPED and any(
            json.loads(path.read_text()).get("participants")
            for path in (self.home / "projects").glob("*/project.json")
        )
        remedy = protocol.RELAUNCH if stale else ""
        components.append(
            {
                "component": "service",
                "version": str(served.get("version", "")),
                "protocol": protocol.PROTOCOL,
                "state": protocol.OK if serving == "ready" else serving,
                "remedy": protocol.START if stopped else remedy,
                "compatible": not stale and not stopped,
            }
        )
        return {
            "protocol": protocol.PROTOCOL,
            "supported": list(protocol.SUPPORTED),
            "schema": store.SCHEMA_VERSION,
            "platform": process.host_report(),
            "components": components,
            "consistent": all(
                component["compatible"] for component in components
            ),
        }

    def problems(self, ack_after: float = 0.0) -> list[dict]:
        """Lists every condition an operator should act on, oldest first.

        The rows are derived from the same status reading `status` and
        `top` print, so a lane reads the same on every surface. The command
        reads: it wakes nobody, releases nothing and moves no ownership.

        Args:
            ack_after: Seconds a message may await acknowledgement before it
                is listed; each project's stall interval when zero.

        Returns:
            One row per condition, naming the lane, the condition, how long
            it has held and the command that clears it.
        """
        return problems.derive(self.home, self.status_snapshot(), ack_after)

    def liveness(self, repo: Path) -> dict[str, str]:
        """Reports every participant's session state for one repository.

        Args:
            repo: Any checkout of the target repository.

        Returns:
            Mapping of participant name to session state and checkpoint age.
        """
        _, directory = self.project(repo)
        data = roster.read(directory)
        return {
            name: participant_liveness(directory, name)
            for name in data["participants"]
        }

    def _approval_state(
        self, directory: Path, data: dict, agent: str
    ) -> dict | None:
        """Reads how a lane stands against the approval its project requires.

        Args:
            directory: Private state directory for the common repository.
            data: Project manifest holding this participant.
            agent: Participant that owns the lane.

        Returns:
            The decision state beside the lane's ready report, or None when
            the project requires no approval. A state that cannot be read is
            reported as unreadable rather than as approved, matching the
            refusal the integration commands would raise.
        """
        if not data["approval"]:
            return None
        try:
            reviewed = self._reviewed(directory, data, agent)
        except (BridgeError, OSError) as exc:
            return {"state": "unreadable", "report": "", "detail": str(exc)}
        return {
            "state": reviewed["state"],
            "report": reviewed["report"],
            "detail": reviewed["detail"],
        }

    @contextlib.contextmanager
    def _project_reading(self) -> Iterator[sqlite3.Connection | None]:
        """Holds one read transaction for the questions of a single frame.

        The store runs in write-ahead logging mode, so a held read never
        delays a writer. A store that does not exist yet, or that refuses the
        transaction, yields nothing and leaves each reading to open its own
        connection and report its own failure as before.
        """
        import sqlite3

        if not (self.home / store.DATABASE).exists():
            yield None
            return
        try:
            transaction = store.connect(self.home)
        except sqlite3.Error:
            yield None
            return
        with transaction as db:
            yield db

    def _project_accounting(
        self, db: sqlite3.Connection | None, root: str
    ) -> dict | None:
        """Reads a project's idle lane-minutes and unaccountable claims.

        Args:
            db: Frame's shared read transaction, or None to open one.
            root: Canonical project key.

        Returns:
            The `lanes.summary` of every lane's totals merged, or None when
            no lane has been accounted or the store cannot be read.
        """
        import sqlite3

        try:
            with store.reading(self.home, db) as reader:
                accounts = lanes.read_accounts(reader, root)
        except (sqlite3.Error, BridgeError, OSError):
            return None
        return lanes.summary(lanes.combine(accounts)) if accounts else None

    def _project_context(
        self,
        directory: Path,
        data: dict,
        db: sqlite3.Connection | None = None,
    ) -> dict:
        """Takes the readings a status frame needs once for a whole project.

        The issue ledger, the supervision configuration, project usage and
        pending scheduled items describe the project rather than any one lane,
        so reading them per lane repeated the same file and the same query for
        every participant and could describe two different instants inside one
        frame.

        Args:
            directory: Private state directory for the common repository.
            data: Project manifest holding every participant.
            db: Open read transaction to answer store questions from.

        Returns:
            The shared readings, the transaction they came from, and any store
            failure that must still be reported against each lane's mail.
        """
        import sqlite3

        try:
            usage = store.usage(self.home, data["root"], db=db)
        except sqlite3.Error:
            usage = {}
        schedules: list[dict] = []
        failure: Exception | None = None
        try:
            schedules = store.schedules(self.home, data["root"], db=db)
        except (sqlite3.Error, BridgeError, OSError) as exc:
            failure = exc
        return {
            "ledger": snapshot(directory),
            "configuration": supervision.configuration(self.home, data),
            "usage": usage,
            "schedules": schedules,
            "schedules_error": failure,
            "db": db,
        }

    def _lane_status(
        self,
        directory: Path,
        data: dict,
        agent: str,
        edited: Sequence[str] = (),
        advanced: Sequence[str] = (),
        *,
        context: dict | None = None,
    ) -> dict:
        """Reads one lane's reported state, ownership context and mailbox.

        Args:
            directory: Private state directory for the common repository.
            data: Project manifest holding this participant.
            agent: Participant that owns the lane.
            edited: Reserved paths an operator changed in the base checkout,
                read once per project by the caller.
            advanced: Paths this lane holds that the base branch changed
                since the lane forked, read once per project by the caller.
            context: Project-wide readings the caller already took for this
                frame, holding the issue ledger, the supervision
                configuration, project usage, pending scheduled items and the
                open read transaction they were answered from. Absent, this
                lane takes each reading for itself.

        Returns:
            The lane's session, availability, branch, reported outcome and
            mailbox counts, together with any native dialog the lane records as
            holding its client. An unreadable mailbox is reported as an error
            beside the rest of the lane rather than failing the whole report.
            A lane with a state record has its session and availability read
            from that record alone, through `supervision.recorded_presence`,
            so the activity file cannot report a condition the record does
            not hold; only a lane with no record yet is read from the file.
        """
        import sqlite3

        frame = (
            context
            if context is not None
            else self._project_context(directory, data)
        )
        ledger = frame["ledger"]
        configuration = frame["configuration"]
        participant = data["participants"][agent]
        name = participant["display"]
        state = activity(directory, agent)
        observed = supervision.presence(
            directory, agent, configuration["inactive_after"]
        )
        branch = lane_branch(Path(participant["lane"]))
        reported_at = state.get("reported_at")
        stalled = supervision.stall(
            self.home,
            directory,
            data,
            agent,
            configuration["stalled_after"],
        )
        idle = metrics.idle_intervals(directory, agent)
        try:
            with store.reading(self.home, frame["db"]) as db:
                condition = lanes.read(db, data["root"], agent)
                accounts = lanes.read_accounts(db, data["root"])
                wake = lanes.read_wake(db, data["root"], agent)
        except (sqlite3.Error, BridgeError, OSError, ValueError):
            condition, accounts, wake = None, {}, {}
        if condition:
            observed = supervision.recorded_presence(condition, observed)
            observed["evidence"] = condition["evidence"]
            age = observed["age_seconds"]
            liveness = lanes.describe(condition) + (
                f"; event {age}s ago" if age is not None else ""
            )
        else:
            liveness = participant_liveness(
                directory, agent, configuration["inactive_after"]
            )
        budget = budgets.report(
            self.home, directory, data, agent, frame["usage"]
        )
        record = {
            "participant": agent,
            "identity": name,
            "provider": participant["provider"],
            "credential": participant["credential"],
            "session": liveness,
            "condition": lanes.view(condition),
            "accounting": (
                lanes.summary(accounts[agent]) if agent in accounts else None
            ),
            "availability": {
                "state": observed["state"],
                "activity": observed["activity"],
                "evidence": observed["evidence"],
                "stale": observed["stale"],
                "process_alive": observed["process_alive"],
                "last_active_at": views.timestamp(observed["last_active"]),
                "age_seconds": observed["age_seconds"],
            },
            "branch": branch,
            "assigned_branch": participant["branch"],
            "drift": branch != participant["branch"],
            "paused": participant.get("paused", False),
            "dialog": (
                state["dialog"] if isinstance(state.get("dialog"), dict) else {}
            ),
            "foreign_session": checkpoints.foreign_reading(state),
            "retired_at": views.timestamp(participant.get("retired")),
            "retired_age_seconds": (
                int(time.time() - float(participant["retired"]))
                if roster.retired(participant)
                else None
            ),
            "outcome": state.get("outcome", "unknown"),
            "approval": self._approval_state(directory, data, agent),
            "summary": state.get("summary", ""),
            "remaining": state.get("remaining", ""),
            "evidence": state.get("evidence", ""),
            "review": review_fields(metrics.latest_review(directory, agent)),
            "reported_at": views.timestamp(reported_at),
            "report_age_seconds": (
                int(time.time() - reported_at) if reported_at else None
            ),
            "injected_bytes": state.get("injected_bytes", 0),
            "injections": state.get("injections", 0),
            "claims": [
                {
                    "issue": int(number),
                    **deadline_state(record),
                    "deadline_at": views.timestamp(
                        deadline_state(record)["deadline"]
                    ),
                    "offer": offer_state(record.get("offer")),
                    "handoff": handoff_fields(
                        record.get("offer") or record.get("handoff")
                    ),
                    "orphaned": bool(record.get("orphan")),
                    "orphan_reason": (record.get("orphan") or {}).get(
                        "reason", ""
                    ),
                    "orphan_recorded_seconds": (
                        orphan_age(record["orphan"])
                        if record.get("orphan")
                        else None
                    ),
                    "orphan_reservations": list(
                        (record.get("orphan") or {}).get("reservations", [])
                    ),
                    **issues.unresolved_completion(record),
                }
                for number, record in sorted(
                    ledger["issues"].items(), key=lambda i: int(i[0])
                )
                if record.get("owner") == agent
            ],
            "idle": {
                "stalled": stalled["stalled"],
                "kind": stalled["kind"],
                "message_id": stalled["message_id"],
                "sender": stalled["sender"],
                "age_seconds": stalled["age_seconds"],
                "served_age_seconds": stalled["served_age_seconds"],
                "marker": supervision.stall_marker(stalled),
            },
            "operator_edits": list(edited),
            "base_advance_paths": list(advanced),
            "idle_seconds": idle["seconds"],
            "idle_complete": idle["complete"],
            "budget": {
                **budget,
                "marker": budgets.marker(budget),
            },
            "waiting": metrics.pending(
                metrics.waits(
                    self.home,
                    directory,
                    data,
                    agent,
                    db=frame["db"],
                    ledger=ledger,
                )
            ),
            "wake": None,
            "mail": None,
        }
        if wake:
            next_at = wake.get("next_at")
            record["wake"] = {
                "result": wake["result"],
                "attempts": wake["attempts"],
                "at": views.timestamp(wake["at"]),
                "age_seconds": int(time.time() - wake["at"]),
                "budget": supervision.WORK_WAKE_ATTEMPTS,
                "blocked": wake.get("blocked", ""),
                "exhausted": bool(wake.get("exhausted_at")),
                "next_at": (views.timestamp(next_at) if next_at else None),
                "next_seconds": (
                    max(int(next_at - time.time()), 0) if next_at else None
                ),
            }
        try:
            mail = mailbox(
                self.home, data["root"], name, state.get("cursor", 0)
            )
            if frame["schedules_error"]:
                raise frame["schedules_error"]
            scheduled = sum(
                item["recipient"] == agent for item in frame["schedules"]
            )
        except (sqlite3.Error, BridgeError, OSError) as exc:
            record["mail"] = {"error": str(exc)}
            return record
        record["mail"] = {
            "pending_operator_items": scheduled,
            "unread": mail["unread"],
            "superseded": mail.get("superseded", 0),
            "unread_topics": dict(mail.get("unread_topics") or {}),
            "pending_ack": mail["pending_ack"],
            "reservations": mail["reservations"],
            "stale_reservations": mail.get("stale_reservations", 0),
            "stale_reservation_age": mail.get("stale_reservation_age", 0),
            "named_resources": list(mail.get("named_resources", [])),
            "queued_requests": frame["usage"].get(name, {}).get("queued", 0),
            "queued_by": list(
                frame["usage"].get(name, {}).get("queued_by", [])
            ),
            "refused": list(frame["usage"].get(name, {}).get("refused", [])),
            "last_coordination_at": views.timestamp(mail["last_coordination"]),
            "outstanding_ack": [
                {
                    "message_id": pending["id"],
                    "sender": pending["sender"],
                    "age_seconds": pending["age_seconds"],
                }
                for pending in mail.get("outstanding_ack", [])
            ],
            "awaiting_delivery": len(mail["messages"]),
            "task": (
                state.get("last_prompt")
                or mail["reported_task"]
                or state.get("task", "")
            )[:240],
        }
        return record

    def status_snapshot(self) -> dict:
        """Reads server health and every registered lane without writing.

        The same reading answers the printed report and the machine-readable
        document, so a script and an operator never see two different states
        of the same coordination store.

        A live process answering its own readiness probe is not readiness when
        the store it serves cannot be read by the code around it. Readiness
        therefore also requires a usable store schema, so the report cannot
        claim health while every participant is refused against the same
        store.

        Returns:
            Server readiness and the state the service reports itself in, the
            private state directory, whether inbound status queries were asked
            for and the configuration fault that stops them, and one record
            per registered project holding its issue ledger and its lanes. A
            service that reports itself stale is not ready, and the state
            names why. A project the supervisor retired because its root is
            gone is left out.
        """
        usable = (
            store.schema_state(store.schema_version(self.home))
            in store.SCHEMA_USABLE
        )
        served = self.health()
        state = served.get("status") or "not ready"
        healthy = usable and bool(self.server_process()) and state == "ready"
        projects = []
        for path in sorted((self.home / "projects").glob("*/project.json")):
            data = roster.normalize(json.loads(path.read_text()))
            if supervision.root_retired(path.parent):
                continue
            edits, advances = supervision.readings(self.home, data)
            with self._project_reading() as db:
                context = self._project_context(path.parent, data, db)
                projects.append(
                    {
                        "root": data["root"],
                        "reclaim": supervision.reclaim_summary(path.parent),
                        "accounting": self._project_accounting(
                            db, data["root"]
                        ),
                        **views.ledger(context["ledger"]),
                        "ready_groups": plan.ready_groups(
                            plan.groups(path.parent),
                            context["ledger"],
                            reported_ready(path.parent),
                        ),
                        "participants": [
                            self._lane_status(
                                path.parent,
                                data,
                                agent,
                                edits.get(agent, []),
                                advances.get(agent, []),
                                context=context,
                            )
                            for agent in sorted(data["participants"])
                        ],
                        "supervision_error": issues.supervision_error(
                            path.parent
                        ),
                        "supervision_poll": supervision.last_poll(path.parent),
                    }
                )
        return {
            "server": {"ready": healthy, "state": state},
            "state_directory": str(self.home),
            "inbound": inbound_status(),
            "projects": projects,
        }

    def status(
        self, selection: Selection | None = None, width: int | None = None
    ) -> int:
        """Prints one table per project, or one lane in full detail.

        The table answers which lanes are ready, drifted or waiting at a
        glance. A named participant is reported as the full reading instead
        of a row, because a single lane is read rather than compared.

        Args:
            selection: Filters the operator asked for; every lane when None.
            width: Columns the tables may use, or None to print every column,
                which is what a redirected stream receives.

        Returns:
            The number of lanes reported, so a caller can gate on a filter
            having matched at least one lane.
        """
        selection = selection or Selection()
        report = narrow(self.status_snapshot(), selection)
        kept = {project["root"]: project for project in report["projects"]}
        ready = "ready" if report["server"]["ready"] else "not ready"
        print(f"Server: {ready}")
        if report["server"].get("state") == protocol.STALE:
            print(f"Code: {protocol.STALE}; {protocol.RELAUNCH}")
        schema = store.schema_state(store.schema_version(self.home))
        if repair := store.remedy(schema):
            print(f"Store: {schema}; {repair}")
        if refusal := (report.get("inbound") or {}).get("fault"):
            print(f"Inbound: {refusal}")
        print(f"State: {report['state_directory']}")
        matched = reported_lanes(report)
        if selection.filtered() and not matched:
            print(f"No participant matches {selection.describe()}.")
            return matched
        for path in sorted((self.home / "projects").glob("*/project.json")):
            data = roster.normalize(json.loads(path.read_text()))
            project = kept.get(data["root"])
            if project is None:
                continue
            reported = project["participants"]
            if selection.filtered() and not reported:
                continue
            print(f"\nProject: {data['root']}")
            if failing := project.get("supervision_error"):
                print(supervision_failure(failing))
            polled = project.get("supervision_poll") or {}
            if isinstance(polled.get("at"), (int, float)):
                print(supervision_liveness(polled))
            print(describe(snapshot(path.parent)))
            if measured := reclaim.summary_line(project.get("reclaim") or {}):
                print(measured)
            if accounted := project.get("accounting"):
                print(f"Lanes: {lanes.describe_account(accounted)}")
            if groups := project.get("ready_groups") or []:
                print(
                    "Every member reported ready in: "
                    + ", ".join(groups)
                    + ". Integrate one with `agent-parley participant merge "
                    "--group NAME`."
                )
            if selection.participant:
                for record in reported:
                    lane_detail(record, data)
                continue
            rows = [
                tables.status_row(
                    record, pending_offers(project, record["participant"])
                )
                for record in reported
            ]
            for row in tables.status_table(rows, width):
                print(row)
        return matched
