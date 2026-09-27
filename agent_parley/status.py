"""Reads launcher, store and service fitness, problems, and lane status.

Method bodies resolve the module-level names they use through `cli` when
they run, so a name bound or replaced there, including a test's patch, is
the one a moved method reads. This module never imports `cli` at import
time.
"""

from __future__ import annotations

import contextlib
import time
from pathlib import Path
from typing import TYPE_CHECKING

from agent_parley import BridgeError
from agent_parley.core import BridgeCore

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator, Sequence

    from agent_parley.cli import Selection

DORMANT_SECONDS = 86400.0
ENDED_STATES = ("stopped", "dead", "reclaimed")
ATTENTION_LINES = 8
FORGE_ISSUES = "forge-issues.json"
FORGE_TTL = 300.0
FORGE_RETRY = 60.0
FORGE_TIMEOUT = 5
FORGE_LIMIT = 1000


def reading(
    issues: dict | None, limit: int, age: float, fresh: bool, reason: str
) -> dict:
    """Shapes one answer of `StatusMixin.forge_issues`.

    Args:
        issues: Issue number to its title, or None when never read.
        limit: Most issues the reading asked the forge for.
        age: Seconds since the reading was taken.
        fresh: Whether the reading is within `FORGE_TTL`.
        reason: Why the reading is not fresh, empty when it is.

    Returns:
        The issues, whether they are every open issue, their whole-second
        age or None when never read, freshness and the reason.
    """
    return {
        "issues": issues,
        "complete": issues is not None and len(issues) < limit,
        "age_seconds": int(age) if issues is not None else None,
        "fresh": fresh,
        "reason": reason,
    }


def forge_note(known: dict) -> str:
    """States in one line why the forge's issue state is not current.

    Args:
        known: Answer of `StatusMixin.forge_issues`.

    Returns:
        Empty for a fresh, complete reading. Otherwise the line naming a
        stale reading's age, an unavailable one's cause, or a list too long
        to read whole, and whether closed issues are hidden.
    """
    from agent_parley.tables import age

    if known["issues"] is None:
        return (
            f"Forge: open issues unavailable ({known['reason']}); claims on "
            "closed issues are not hidden"
        )
    if not known["complete"]:
        return (
            f"Forge: over {FORGE_LIMIT} open issues; claims on closed issues "
            "are not hidden"
        )
    if not known["fresh"]:
        return (
            f"Forge: open issues as read {age(known['age_seconds'])} ago; "
            f"refresh failed ({known['reason']})"
        )
    return ""


def lane_state(record: dict) -> str:
    """Names the state one lane is in for the compact status view.

    Args:
        record: One lane record from the status reading.

    Returns:
        ``retired`` or ``paused`` when the operator set either, else the
        lane's recorded state, such as ``working`` or ``stopped``, else the
        availability supervision observed for a lane with no record yet.
    """
    if record.get("retired_at"):
        return "retired"
    if record.get("paused"):
        return "paused"
    if condition := record.get("condition"):
        return str(condition["state"])
    return str(record["availability"]["state"])


def stopped_seconds(record: dict) -> float | None:
    """Reports how long a lane has been stopped, or None while it is not.

    Args:
        record: One lane record from the status reading.

    Returns:
        Seconds since a retired, stopped, dead or reclaimed lane entered that
        state, or since a lane without a live process was last active. None
        for a live lane or one with no recorded activity.
    """
    if record.get("retired_age_seconds") is not None:
        return float(record["retired_age_seconds"])
    condition = record.get("condition")
    if condition:
        if condition["state"] in ENDED_STATES:
            return float(condition["seconds"])
        return None
    availability = record["availability"]
    if availability["process_alive"] or availability["age_seconds"] is None:
        return None
    return float(availability["age_seconds"])


def dormant(root: str, participants: list[dict]) -> bool:
    """Reports whether a project is gone or every lane stopped long ago.

    Args:
        root: Recorded repository root.
        participants: Every lane record of the project.

    Returns:
        True when the root is no longer a directory, or when the project has
        lanes and each has been stopped for at least `DORMANT_SECONDS`.
    """
    if not Path(root).is_dir():
        return True
    spans = [stopped_seconds(record) for record in participants]
    return bool(spans) and all(
        span is not None and span >= DORMANT_SECONDS for span in spans
    )


def pull_request(readings: list[dict], issue: str, branch: str) -> dict | None:
    """Finds the cached open pull request that carries one claim.

    Args:
        readings: Open pull requests supervision last cached for the project.
        issue: Claimed issue number.
        branch: Branch assigned to the lane that owns the claim.

    Returns:
        The number, URL, check verdict and merge state of the newest pull
        request that names the issue as one it closes, else of the one whose
        head is the lane's branch, or None when the cache holds neither.
    """
    ordered = sorted(
        readings, key=lambda item: int(item.get("number") or 0), reverse=True
    )
    found = next(
        (item for item in ordered if issue in (item.get("issues") or [])),
        None,
    ) or next(
        (item for item in ordered if branch and item.get("branch") == branch),
        None,
    )
    if found is None:
        return None
    return {
        "number": int(found.get("number") or 0),
        "url": str(found.get("url") or ""),
        "checks": str(found.get("checks") or ""),
        "mergeable": str(found.get("mergeable") or ""),
    }


def attention(claim: dict, owner: str) -> list[str]:
    """States what an open claim needs and the command that resolves it.

    Args:
        claim: One open claim from a lane record of the status snapshot.
        owner: Participant holding the claim.

    Returns:
        One line for a claim whose owner the supervisor marked orphaned and
        one for a claim past its deadline, each naming the single command
        that resolves it; nothing for a claim that needs no action.
    """
    from agent_parley.tables import age

    number = claim["issue"]
    lines = []
    if claim.get("orphaned"):
        lines.append(
            f"#{number} orphaned from {owner} "
            f"({claim.get('orphan_reason') or 'no reason'}); a peer lane runs "
            f"agent-parley issue claim {number} --take-orphaned"
        )
    if claim.get("overdue"):
        lines.append(
            f"#{number} overdue {age(claim['overdue_seconds'])} with {owner}; "
            f"agent-parley issue assign {number} LANE --reason TEXT"
        )
    return lines


def since(instant: float) -> int | None:
    """Reports whole seconds since a Unix time, or None when it is unset."""
    return max(int(time.time() - instant), 0) if instant else None


class StatusMixin(BridgeCore):
    """Health check, problem list and per-lane status reporting."""

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
        from agent_parley.cli import json, process, protocol, store

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
        from agent_parley.cli import problems

        return problems.derive(self.home, self.status_snapshot(), ack_after)

    def liveness(self, repo: Path) -> dict[str, str]:
        """Reports every participant's session state for one repository.

        Args:
            repo: Any checkout of the target repository.

        Returns:
            Mapping of participant name to session state and checkpoint age.
        """
        from agent_parley.cli import participant_liveness, roster

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

        from agent_parley.cli import store

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

        from agent_parley.cli import lanes, store

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
            The shared readings, the open pull requests supervision last
            cached, the transaction they came from, and any store failure
            that must still be reported against each lane's mail.
        """
        import sqlite3

        from agent_parley.cli import json, snapshot, store, supervision

        try:
            cached = json.loads(
                (directory / supervision.PULL_REQUEST_RECORD).read_text()
            )
        except (OSError, ValueError):
            cached = {}
        pulls = cached.get("pull_requests") if isinstance(cached, dict) else {}
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
            "pull_requests": [
                reading
                for reading in (
                    pulls.values() if isinstance(pulls, dict) else []
                )
                if isinstance(reading, dict)
            ],
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
            `current_task` is the lane's own last report or registered task,
            never the operator's last prompt, and each claim carries its
            recorded title, whether it ended on the forge, the seconds since
            it last progressed and its cached pull request.
        """
        import sqlite3

        from agent_parley.cli import (
            activity,
            budgets,
            checkpoints,
            convergence,
            deadline_state,
            handoff_fields,
            issues,
            lane_branch,
            lanes,
            mailbox,
            metrics,
            offer_state,
            orphan_age,
            participant_liveness,
            review_fields,
            roster,
            store,
            supervision,
            views,
        )

        frame = (
            context
            if context is not None
            else self._project_context(directory, data)
        )
        ledger = frame["ledger"]
        accounts = convergence.read(directory)
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
                    "title": record.get("title") or "",
                    "ended": issues.ended(record),
                    "last_event_seconds": since(issues.last_progress(record)),
                    "pull_request": pull_request(
                        frame.get("pull_requests") or [],
                        number,
                        participant["branch"],
                    ),
                    "delivered": issues.delivered(record),
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
                    "convergence": convergence.current(
                        accounts, number, record.get("claim_id")
                    ),
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
                "silent_seconds": stalled["silent_seconds"],
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
            "current_task": str(
                state.get("summary") or state.get("task") or ""
            )[:240],
        }
        record["lane_state"] = lane_state(record)
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
        record["current_task"] = str(
            state.get("summary")
            or mail["reported_task"]
            or state.get("task")
            or ""
        )[:240]
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
            gone is left out; one whose root is missing or whose lanes all
            stopped over `DORMANT_SECONDS` ago reads as dormant.
        """
        from agent_parley.cli import (
            inbound_status,
            issues,
            json,
            plan,
            reported_ready,
            roster,
            store,
            supervision,
            views,
        )

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
                participants = [
                    self._lane_status(
                        path.parent,
                        data,
                        agent,
                        edits.get(agent, []),
                        advances.get(agent, []),
                        context=context,
                    )
                    for agent in sorted(data["participants"])
                ]
                projects.append(
                    {
                        "root": data["root"],
                        "directory": str(path.parent),
                        "root_missing": not Path(data["root"]).is_dir(),
                        "dormant": dormant(data["root"], participants),
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
                        "participants": participants,
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

    def _health(self, report: dict) -> None:
        """Prints the server, code, store, inbound and state directory lines.

        Args:
            report: Reading produced by `status_snapshot`.
        """
        from agent_parley.cli import protocol, store

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

    def project_at(self, path: Path) -> str:
        """Names the registered project a directory belongs to.

        The directory matches a project when it sits inside the project's
        root or inside one of its lane worktrees. Only recorded paths are
        compared, so the answer never runs git.

        Args:
            path: Directory to place, usually the working directory.

        Returns:
            The recorded root of the first matching project, or an empty
            string when the directory belongs to none.
        """
        from agent_parley.cli import json, roster

        here = path.resolve()
        for manifest in sorted((self.home / "projects").glob("*/project.json")):
            data = roster.normalize(json.loads(manifest.read_text()))
            places = [
                data["root"],
                *(item["lane"] for item in data["participants"].values()),
            ]
            if any(
                here.is_relative_to(Path(place).resolve())
                for place in places
                if place
            ):
                return data["root"]
        return ""

    def board(
        self,
        selection: Selection | None = None,
        width: int | None = None,
        *,
        every_claim: bool = False,
        every_project: bool = False,
    ) -> int:
        """Prints who is working on which open issue, and whether it moves.

        Each project prints one table of open work, one line per lane naming
        its state, its live claims and the task its own last report or
        registration names, and a short list of orphaned or overdue claims
        with the command that resolves each. Claims whose work already ended
        on the forge are summarized on one line unless `every_claim` asks
        for their rows. Dormant projects are left out unless `every_project`
        asks for them, or the selection names that project.

        Args:
            selection: Filters the operator asked for; its project filter
                scopes the view to one project.
            width: Columns the tables may use, or None for whole lines.
            every_claim: Also list claims whose work ended on the forge.
            every_project: Also list dormant projects, after the others.

        Returns:
            The number of lanes reported.
        """
        from agent_parley.cli import Selection, narrow, reported_lanes

        selection = selection or Selection()
        reading = self.status_snapshot()
        report = narrow(reading, selection)
        self._health(report)
        shown = sorted(
            (
                project
                for project in report["projects"]
                if every_project or selection.project or not project["dormant"]
            ),
            key=lambda project: project["dormant"],
        )
        for project in shown:
            self._board_project(project, width, every_claim)
        if selection.project and (
            others := len(reading["projects"]) - len(report["projects"])
        ):
            print(
                f"\n{others} other project(s) not shown; --all-projects "
                "lists them."
            )
        if hidden := len(report["projects"]) - len(shown):
            print(
                f"\n{hidden} dormant project(s) hidden, root missing or every "
                "lane stopped over 24h; --all-projects lists them last."
            )
        return reported_lanes({"projects": shown})

    def forge_issues(self, directory: Path, root: str) -> dict:
        """Reads the forge's open issues through a short-lived cache.

        The open-work view hides claims on closed issues and shows titles,
        and only the forge knows either. One bounded `forge` call reads every
        open issue and is kept in `FORGE_ISSUES` for `FORGE_TTL` seconds. A
        failed call is retried no sooner than `FORGE_RETRY` seconds later, so
        a forge that is down costs one timeout per retry window rather than
        one per reading, and the last good cache answers meanwhile.

        Args:
            directory: Private state directory of the project.
            root: Recorded repository root that selects the forge project.

        Returns:
            ``issues`` as issue number to its title, or None when no reading
            was ever cached; ``complete`` when that reading held every open
            issue; ``age_seconds`` of the reading; ``fresh`` when it is
            within `FORGE_TTL`; and ``reason`` naming why it is not.
        """
        from agent_parley.cli import forge, json
        from agent_parley.state import write_json

        path = directory / FORGE_ISSUES
        try:
            cached = json.loads(path.read_text())
        except (OSError, ValueError):
            cached = {}
        if not isinstance(cached, dict):
            cached = {}
        now = time.time()
        stored = cached.get("issues")
        issues = stored if isinstance(stored, dict) else None
        read_at = float(cached.get("read_at") or 0)
        limit = int(cached.get("limit") or FORGE_LIMIT)
        reason = "forge unreachable"
        if issues is not None and now - read_at < FORGE_TTL:
            return reading(issues, limit, now - read_at, True, "")
        try:
            manifest = json.loads((directory / "project.json").read_text())
        except (OSError, ValueError):
            manifest = {}
        if forge.select(Path(root), manifest) != "github":
            return reading(issues, limit, now - read_at, False, "no GitHub")
        if now - float(cached.get("failed_at") or 0) >= FORGE_RETRY:
            catalog = forge.open_issue_catalog(
                Path(root), FORGE_LIMIT, FORGE_TIMEOUT
            )
            if catalog is not None:
                issues = {
                    number: {"title": str(item.get("title") or "")}
                    for number, item in catalog.items()
                }
                with contextlib.suppress(OSError):
                    write_json(
                        path,
                        {
                            "read_at": now,
                            "limit": FORGE_LIMIT,
                            "issues": issues,
                        },
                    )
                return reading(issues, FORGE_LIMIT, 0.0, True, "")
            with contextlib.suppress(OSError):
                write_json(path, {**cached, "failed_at": now})
        return reading(issues, limit, now - read_at, False, reason)

    def _board_project(
        self, project: dict, width: int | None, every_claim: bool
    ) -> None:
        """Prints one project's open work, lanes and claims needing action.

        Args:
            project: One project record from the status reading.
            width: Columns the tables may use, or None for whole lines.
            every_claim: Also list claims whose work ended on the forge.
        """
        from agent_parley.cli import supervision_failure, tables

        dormancy = " (dormant)" if project["dormant"] else ""
        print(f"\nProject: {project['root']}{dormancy}")
        if failing := project.get("supervision_error"):
            print(supervision_failure(failing))
        known = self.forge_issues(Path(project["directory"]), project["root"])
        if note := forge_note(known):
            print(note)
        opened = known["issues"] if known["complete"] else None
        work: list[tuple[int, tuple[str, ...]]] = []
        lanes: list[tuple[str, ...]] = []
        notes: list[str] = []
        ended: list[int] = []
        for record in project["participants"]:
            owner = record["participant"]
            live = 0
            for claim in record["claims"]:
                entry = (opened or {}).get(str(claim["issue"])) or {}
                closed = claim["ended"] or (
                    opened is not None and str(claim["issue"]) not in opened
                )
                if closed:
                    ended.append(claim["issue"])
                else:
                    live += 1
                    notes.extend(attention(claim, owner))
                if every_claim or not closed:
                    shown = {
                        **claim,
                        "ended": closed,
                        "title": claim["title"] or entry.get("title", ""),
                    }
                    work.append(
                        (
                            claim["issue"],
                            tables.work_row(shown, owner, record["lane_state"]),
                        )
                    )
            lanes.append(
                (
                    owner,
                    record["lane_state"],
                    str(live),
                    record.get("current_task") or "-",
                )
            )
        rows = [row for _, row in sorted(work, key=lambda item: item[0])]
        for text in tables.work_table(rows, width) or ["No open claims."]:
            print(text)
        for text in tables.lane_table(lanes, width):
            print(text)
        if ended:
            listed = " ".join(f"#{number}" for number in sorted(ended)[:10])
            more = f" and {len(ended) - 10} more" if len(ended) > 10 else ""
            hidden = "" if every_claim else " hidden, --all lists them;"
            print(
                f"Closed or ended on the forge, still owned: {listed}{more};"
                f"{hidden} end each with agent-parley issue resolve N"
            )
        if notes:
            print("Needs action:")
            for text in notes[:ATTENTION_LINES]:
                print(f"  {text}")
            if len(notes) > ATTENTION_LINES:
                print(
                    f"  and {len(notes) - ATTENTION_LINES} more; "
                    "agent-parley status LANE shows each"
                )

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
        from agent_parley.cli import (
            Selection,
            describe,
            json,
            lane_detail,
            lanes,
            narrow,
            pending_offers,
            reclaim,
            reported_lanes,
            roster,
            snapshot,
            supervision_failure,
            supervision_liveness,
            tables,
        )

        selection = selection or Selection()
        report = narrow(self.status_snapshot(), selection)
        kept = {project["root"]: project for project in report["projects"]}
        self._health(report)
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
