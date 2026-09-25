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
            The shared readings, the transaction they came from, and any store
            failure that must still be reported against each lane's mail.
        """
        import sqlite3

        from agent_parley.cli import snapshot, store, supervision

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

        from agent_parley.cli import (
            activity,
            budgets,
            checkpoints,
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
        from agent_parley.cli import (
            Selection,
            describe,
            json,
            lane_detail,
            lanes,
            narrow,
            pending_offers,
            protocol,
            reclaim,
            reported_lanes,
            roster,
            snapshot,
            store,
            supervision_failure,
            supervision_liveness,
            tables,
        )

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
