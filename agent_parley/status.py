"""Reads launcher, store and service fitness, problems, and lane status.

Method bodies resolve the module-level names they use through `cli` when
they run, so a name bound or replaced there, including a test's patch, is
the one a moved method reads. This module never imports `cli` at import
time.
"""

from __future__ import annotations

import contextlib
import shlex
import time
from pathlib import Path
from typing import TYPE_CHECKING

from agent_parley import BridgeError, protocol
from agent_parley.core import BridgeCore

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator, Sequence

    from agent_parley.cli import Selection

DORMANT_SECONDS = 86400.0
ENDED_STATES = ("stopped", "dead", "reclaimed")
OPEN_REPORTS = ("blocked", "partial")
ATTENTION_LINES = 8
FORGE_ISSUES = "forge-issues.json"
FORGE_TTL = 300.0
FORGE_STALE = 2 * FORGE_TTL
FORGE_RETRY = 60.0
FORGE_LIMIT = 1000
FORGE_PAGE = 100
FORGE_TIMEOUT = 3 + 2 * -(-FORGE_LIMIT // FORGE_PAGE)
FORGE_FULL_SECONDS = 3600.0
FORGE_OVERLAP = 120.0


def reading(
    issues: dict | None, limit: int, age: float, fresh: bool, reason: str
) -> dict:
    """Shapes one answer of `StatusMixin.forge_issues`.

    Args:
        issues: Issue number to its title, or None when never read.
        limit: Most issues the reading asked the forge for.
        age: Seconds since the reading was taken.
        fresh: Whether the reading is within `FORGE_STALE`.
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


def landed_line(integration: dict) -> str:
    """States in one line which issues landed in the integration base.

    Args:
        integration: The ``integration`` reading of one status project.

    Returns:
        Empty when nothing landed there. Otherwise the base, each landed
        issue with its pull request, and that the issues wait only for the
        crossing pull request.
    """
    if not integration.get("landed"):
        return ""
    listed = ", ".join(
        f"#{item['issue']} (PR #{item['pull_request']})"
        if item["pull_request"]
        else f"#{item['issue']}"
        for item in integration["landed"]
    )
    return (
        f"Landed in {integration['base']}: {listed}; each waits only for "
        "the crossing pull request to the default branch."
    )


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


def service_state(server: dict) -> str:
    """Names the coordination service's state in the shared wording.

    `status`, `problems`, `top` and `doctor` print the same words for the
    same fact, so a stopped service reads ``not running`` everywhere.

    Args:
        server: The ``server`` record of `status_snapshot`.

    Returns:
        ``running`` for a ready service, `protocol.STOPPED` when nothing
        answered, and ``not ready`` for a service that answered but cannot
        serve, such as one running stale code or over an unusable store.
    """
    if server["ready"]:
        return "running"
    if server.get("state") in ("", "not ready"):
        return protocol.STOPPED
    return "not ready"


def folded(record: dict, after: float) -> bool:
    """Tells whether a dead lane has left nothing the default views need.

    A lane is folded out of the default `status` and `problems` views once
    its session is dead, it owns no claim, no process of it answers, and its
    newest report and activity are both older than the retention interval.
    Nothing in such a lane can change until an operator acts on it, so it
    is counted on one line instead of listed; `--all` lists it again.

    Args:
        record: One lane record from the status reading.
        after: Retention interval in seconds, the project's `fold_after`.

    Returns:
        True when the lane qualifies; False for any lane still holding a
        claim, still running, active inside the interval, or whose report
        and activity ages are both unknown.
    """
    availability = record.get("availability") or {}
    if (
        record.get("lane_state") != "dead"
        or record.get("claims")
        or availability.get("process_alive")
    ):
        return False
    ages = [
        value
        for value in (
            record.get("report_age_seconds"),
            availability.get("age_seconds"),
        )
        if value is not None
    ]
    return bool(ages) and min(ages) > after


def fold_line(count: int, after: float, noun: str) -> str:
    """Words the one line that stands for the rows a default view hides.

    Args:
        count: How many rows were hidden.
        after: Retention interval in seconds that hid them.
        noun: What one hidden row is, such as ``dead lane``; an ``s`` is
            added for any count but one.

    Returns:
        The summary line naming the count, the interval and `--all`.
    """
    from agent_parley import tables

    plural = "" if count == 1 else "s"
    return (
        f"{count} {noun}{plural} older than {tables.age(after)} hidden; "
        "--all shows them"
    )


def task_line(record: dict, fallback: str) -> str:
    """Describes the work one lane is on for its status task line.

    The lane's last report is its own account of its work, but a report
    about an issue the lane has since released describes finished work, not
    current work. Such a report keeps its text, labelled with its issue and
    age, and open claims the lane has not reported on since claiming are
    named ahead of it, so a closed issue's summary never reads as the lane's
    current task. A blocked or partial report on a held issue that mail
    citing the issue has followed is labelled with its age and that mail's
    count, because the mail may say its remaining work is done.

    Args:
        record: One lane record from the status reading, carrying its
            claims, report summary, report issue, report age and the count
            of newer mail on a held open report.
        fallback: Registered or mailed task shown when no report exists.

    Returns:
        The task line, at most 240 characters.
    """
    from agent_parley.tables import age

    summary = record["summary"]
    issue = record["report_issue"]
    if summary and record["report_held"]:
        newer = record["report_newer_mail"]
        if not newer:
            return summary[:240]
        reported = age(record["report_age_seconds"] or 0)
        plural = "" if newer == 1 else "s"
        return (
            f"#{issue} (reported {reported} ago, {newer} newer "
            f"message{plural} on it): {summary}"
        )[:240]
    silent = ", ".join(
        f"#{claim['issue']}"
        for claim in record["claims"]
        if not claim["ended"] and not claim["reported_since_claim"]
    )
    if summary and issue is not None:
        reported = age(record["report_age_seconds"] or 0)
        text = f"#{issue} (not held, reported {reported} ago): {summary}"
    else:
        text = summary or fallback
    note = f"no report on held {silent}" if silent else ""
    return "; ".join(part for part in (note, text) if part)[:240]


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
        The number, URL, check verdict, merge state, seconds since its head
        was first seen pending and whether that head was reported stalled,
        of the newest pull request that names the issue as one it closes,
        else of the one whose head is the lane's branch, or None when the
        cache holds neither.
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
        "pending_seconds": since(float(found.get("pending_since") or 0)),
        "stalled": bool(found.get("stalled_at")),
    }


def attention(
    claim: dict, owner: str, root: str = "", peer: bool = True
) -> list[str]:
    """States what an open claim needs and the command that resolves it.

    Args:
        claim: One open claim from a lane record of the status snapshot.
        owner: Participant holding the claim.
        root: Canonical project key the operator's commands name.
        peer: Whether a live lane besides the owner exists to take an
            orphaned claim; without one the operator's command is named.

    Returns:
        One line for a claim whose owner the supervisor marked orphaned and
        one for a claim past its deadline, each naming the single command
        that resolves it; nothing for a claim that needs no action.
    """
    from agent_parley import supervision
    from agent_parley.tables import age

    number = claim["issue"]
    lines = []
    if claim.get("orphaned"):
        lines.append(
            f"#{number} orphaned from {owner} "
            f"({claim.get('orphan_reason') or 'no reason'}); "
            + supervision.orphan_remedy(str(number), root, peer)
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


def moved_after(claim: dict, known: dict) -> bool:
    """Reports whether a claim progressed after the forge reading was taken.

    A number missing from the cached open-issue reading is evidence that
    the issue closed only when the reading is newer than the claim. A lane
    that opens an issue and claims it inside the poll interval is otherwise
    told to resolve work it picked up minutes ago, as
    `supervision.end_closed_issues` already guards for unowned issues.

    Args:
        claim: One claim record from the status reading.
        known: Answer of `StatusMixin.forge_issues`.

    Returns:
        True when the claim's last progress is more recent than the reading.
    """
    touched = claim.get("last_event_seconds")
    age = known.get("age_seconds")
    return touched is not None and age is not None and touched < age


def reported_since(record: dict) -> bool:
    """Reports whether a claim's holder reported on it since claiming it.

    Args:
        record: Published ledger record for one issue.

    Returns:
        True when the claim's recorded report progress is at or after the
        start of its current ownership generation. Progress carried over
        from an earlier generation does not count.
    """
    from agent_parley import issues, lifecycle

    progress = lifecycle.state(record).get("progress")
    if not isinstance(progress, dict):
        return False
    return float(progress.get("at", 0) or 0) >= issues.claimed_since(record)


def registered_root(home: Path, path: Path) -> str:
    """Names the registered project a directory belongs to, reading only.

    The directory matches a project when it sits inside the project's root
    or inside one of its lane worktrees. Only the manifests under ``home``
    are read, so the answer never runs git and never creates state. It
    imports what it reads directly rather than through `cli`, so the bare
    start screen can call it without loading the command surface.

    Args:
        home: Private state directory.
        path: Directory to place, usually the working directory.

    Returns:
        The recorded root of the first matching project, or an empty string
        when the directory belongs to none.
    """
    import json

    from agent_parley import roster

    here = path.resolve()
    for manifest in sorted((home / "projects").glob("*/project.json")):
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
            healthy system while every lane is denied. A registered project
            whose root checkout no longer exists is not consistent either,
            and the `projects` component names each such root. A damaged
            decisions record a writer kept aside is not consistent until
            the operator recovers and removes it, and the `decisions`
            component names each copy. The report
            also names
            the kernel release, the WSL generation or ``none``, and whether
            ``pidfd_open`` is available, so a platform gap is read here
            before a lane is started. The ``notify`` component says whether
            outbound and inbound notification are active; notification that
            is off is consistent, and one configured but unable to send is
            not. The `approvals` component names each lane
            `supervision.opt_in_missing` reports as a setup gap; it is the
            operator's choice, so it leaves the set consistent. The
            `dialogs` component names each lane `dialogs.drift` reports,
            a client newer than its recorded dialog matchers or a dialog
            none of them read, as a matcher drift that also leaves the set
            consistent, because the lane still escalates what it cannot read.
        """
        from agent_parley import decisions, plugins
        from agent_parley.cli import (
            inbound_status,
            json,
            notification_line,
            notify,
            process,
            protocol,
            store,
            supervision,
        )

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
        bundled = protocol.manifests(protocol.plugin_root())
        for client in protocol.CLIENTS:
            rows = []
            for label, directory in plugins.config_dirs(self.home, client):
                manifest = plugins.installed_manifest(client, directory)
                if manifest is not None:
                    suffix = f" ({label})" if label else ""
                    rows.append(
                        {
                            "component": f"{client} plugin{suffix}",
                            **plugins.assessed(manifest),
                        }
                    )
            if not rows:
                rows.append(
                    {
                        "component": f"{client} plugin",
                        **plugins.assessed(bundled[client]),
                        "version": "bundled",
                    }
                )
            components.extend(rows)
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
        manifests = [
            json.loads(path.read_text())
            for path in (self.home / "projects").glob("*/project.json")
        ]
        served = self.health()
        serving = served.get("status") or protocol.STOPPED
        stale = serving == protocol.STALE
        stopped = serving == protocol.STOPPED and any(
            manifest.get("participants") for manifest in manifests
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
        gone = sorted(
            str(manifest["root"])
            for manifest in manifests
            if manifest.get("root") and not Path(manifest["root"]).exists()
        )
        components.append(
            {
                "component": "projects",
                "version": "",
                "protocol": protocol.PROTOCOL,
                "state": protocol.ROOT_GONE if gone else protocol.OK,
                "remedy": (
                    protocol.RESTORE_ROOT
                    + "; ".join(
                        protocol.FORGET_ROOT.format(root=shlex.quote(root))
                        for root in gone
                    )
                    if gone
                    else ""
                ),
                "compatible": not gone,
            }
        )
        kept = decisions.damaged(self.home)
        components.append(
            {
                "component": "decisions",
                "version": "",
                "protocol": protocol.PROTOCOL,
                "state": protocol.DAMAGED if kept else protocol.OK,
                "remedy": (
                    protocol.RECOVER_DECISIONS + ", ".join(kept) if kept else ""
                ),
                "compatible": not kept,
            }
        )
        outbound = notify.reported(notify.environment(self.home))
        received = inbound_status(self.home)
        components.append(
            {
                "component": "notify",
                "version": ", ".join(outbound["transports"]),
                "protocol": protocol.PROTOCOL,
                "state": notification_line(outbound, received).removeprefix(
                    "Notify: "
                ),
                "remedy": outbound["fault"] or received["fault"],
                "compatible": not (
                    (outbound["enabled"] and outbound["fault"])
                    or received["fault"]
                ),
            }
        )
        unapproved = sorted(
            f"{manifest['root']} {name}"
            for manifest in manifests
            if manifest.get("root")
            for name in manifest.get("participants") or {}
            if supervision.opt_in_missing(self.home, manifest, name)
        )
        components.append(
            {
                "component": "approvals",
                "version": "",
                "protocol": protocol.PROTOCOL,
                "state": protocol.SETUP_GAP if unapproved else protocol.OK,
                "remedy": (
                    supervision.OPT_IN_REMEDY + ": " + ", ".join(unapproved)
                    if unapproved
                    else ""
                ),
                "compatible": True,
            }
        )
        from agent_parley import dialogs

        drifted = [
            line
            for path in sorted((self.home / "projects").glob("*/project.json"))
            for line in dialogs.drift(path.parent, json.loads(path.read_text()))
        ]
        components.append(
            {
                "component": "dialogs",
                "version": "",
                "protocol": protocol.PROTOCOL,
                "state": protocol.DRIFT if drifted else protocol.OK,
                "remedy": "; ".join(drifted),
                "compatible": True,
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
            "pull_request_record": cached,
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
            never the operator's last prompt, built by `task_line`, with
            `report_issue` naming the issue that report was about and
            `report_held` whether the lane still holds it and
            `report_newer_mail` how many messages citing a held issue
            followed a blocked or partial report on it; each claim
            carries whether it was reported on since claiming, and its
            recorded title, whether it ended on the forge, the seconds since
            it last progressed and its cached pull request. `self_wait` names
            an unexpired self-declared wait and its next-check time, which the
            session text also names in place of a bare idle label.
        """
        import sqlite3

        from agent_parley import lifecycle
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
        told = supervision.base_advance_told(state, list(advanced))
        observed = supervision.presence(
            directory, agent, configuration["inactive_after"]
        )
        branch = lane_branch(Path(participant["lane"]))
        reported_at = state.get("reported_at")
        latest = metrics.latest_report(directory, agent) or {}
        report_issue = (
            int(latest["issue"])
            if latest.get("issue") not in (None, "")
            and latest.get("summary") == state.get("summary")
            else None
        )
        open_issue = (
            report_issue
            if state.get("outcome") in OPEN_REPORTS
            and (ledger["issues"].get(str(report_issue)) or {}).get("owner")
            == agent
            else None
        )
        stalled = supervision.stall(
            self.home,
            directory,
            data,
            agent,
            configuration["stalled_after"],
        )
        idle = metrics.idle_intervals(directory, agent)
        declared_wait = supervision.published_wait(directory, agent)
        active_wait = (
            declared_wait
            if declared_wait and float(declared_wait["until"]) > time.time()
            else None
        )
        try:
            with store.reading(self.home, frame["db"]) as db:
                condition = lanes.read(db, data["root"], agent)
                accounts = lanes.read_accounts(db, data["root"])
                wake = lanes.read_wake(db, data["root"], agent)
                newer = (
                    store.issue_mail(
                        db, data["root"], name, open_issue, reported_at
                    )
                    if open_issue is not None and reported_at
                    else 0
                )
        except (sqlite3.Error, BridgeError, OSError, ValueError):
            condition, accounts, wake, newer = None, {}, {}, 0
        provenance = lanes.provenance(
            condition, supervision.hook_gaps(self.home, participant)
        )
        if condition:
            observed = supervision.recorded_presence(condition, observed)
            observed["evidence"] = condition["evidence"]
            age = observed["age_seconds"]
            inferred = lanes.inference(provenance)
            cause = supervision.provider_error(directory, agent) or (
                f"waiting on {active_wait['reason']}, next check "
                f"{views.timestamp(active_wait['until'])}"
                if active_wait
                else ""
            )
            liveness = (
                lanes.describe(
                    {**condition, "cause": cause}
                    if cause and condition["state"] == lanes.IDLE
                    else condition
                )
                + (f"; event {age}s ago" if age is not None else "")
                + (f"; {inferred}" if inferred else "")
            )
        else:
            liveness = participant_liveness(
                directory, agent, configuration["inactive_after"]
            )
        budget = budgets.report(
            self.home, directory, data, agent, frame["usage"]
        )
        from agent_parley import blueprints

        runs = blueprints.for_lane(directory, agent)
        record = {
            "participant": agent,
            "identity": name,
            "provider": participant["provider"],
            "credential": participant["credential"],
            "session": liveness,
            "condition": lanes.view(condition),
            "provenance": provenance,
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
            "wake_log": (
                str(directory / f"{agent}-wake.log")
                if state.get("attached") is False
                else ""
            ),
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
            "injected_per_hour": checkpoints.hourly_rate(state),
            "claims": [
                {
                    "issue": int(number),
                    "title": record.get("title") or "",
                    "ended": issues.ended(record),
                    "last_event_seconds": since(issues.last_progress(record)),
                    "ready_seconds": (
                        since(lifecycle.state(record).get("updated_at") or 0)
                        if lifecycle.state(record)["state"] == lifecycle.READY
                        else None
                    ),
                    "pull_request": pull_request(
                        frame.get("pull_requests") or [],
                        number,
                        participant["branch"],
                    ),
                    "delivered": issues.delivered(record),
                    "reported_since_claim": reported_since(record),
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
                    **(
                        {"blueprint": blueprints.line(number, runs[number])}
                        if number in runs
                        else {}
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
            "self_wait": {
                "reason": active_wait["reason"] if active_wait else "",
                "until": (
                    views.timestamp(active_wait["until"])
                    if active_wait
                    else None
                ),
            },
            "operator_edits": list(edited),
            "base_advance_paths": list(advanced),
            "base_advance": supervision.base_advance_marker(
                list(advanced), told
            ),
            "base_advance_told_at": views.timestamp(told),
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
            "report_issue": report_issue,
            "report_newer_mail": newer,
        }
        record["report_held"] = report_issue is not None and any(
            claim["issue"] == report_issue for claim in record["claims"]
        )
        record["current_task"] = task_line(record, str(state.get("task") or ""))
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
        record["current_task"] = task_line(
            record, str(mail["reported_task"] or state.get("task") or "")
        )
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
            for and the configuration fault that stops them, the outbound
            transports and the fault that stops them, and one record
            per registered project holding its issue ledger and its lanes. A
            service that reports itself stale is not ready, and the state
            names why. A project the supervisor retired because its root is
            gone is left out; one whose root is missing or whose lanes all
            stopped over `DORMANT_SECONDS` ago reads as dormant.
        """
        from agent_parley import pool
        from agent_parley.cli import (
            inbound_status,
            issues,
            json,
            metrics,
            notify,
            plan,
            reported_ready,
            roster,
            store,
            supervision,
            tables,
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
                    for agent in sorted(
                        data["participants"], key=tables.natural_key
                    )
                ]
                projects.append(
                    {
                        "root": data["root"],
                        "directory": str(path.parent),
                        "root_missing": not Path(data["root"]).is_dir(),
                        "dormant": dormant(data["root"], participants),
                        "reclaim": supervision.reclaim_summary(path.parent),
                        "spares": pool.reading(path.parent, data),
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
                        "fold_after": context["configuration"]["fold_after"],
                        "pull_request_outcomes": (
                            metrics.pull_request_outcomes(
                                context.get("pull_request_record"),
                                context["ledger"],
                                time.time() - metrics.OUTCOME_WINDOW,
                            )
                        ),
                        **self._integration(path.parent, data, context),
                    }
                )
        projects.sort(key=lambda project: project["root"])
        return {
            "server": {"ready": healthy, "state": state},
            "state_directory": str(self.home),
            "inbound": inbound_status(self.home),
            "outbound": notify.reported(notify.environment(self.home)),
            "projects": projects,
        }

    def _integration(self, directory: Path, data: dict, context: dict) -> dict:
        """Reads the work landed in the project's integration base.

        Args:
            directory: Private state directory of the project.
            data: Normalized project manifest.
            context: Project reading from `_project_context`.

        Returns:
            Empty when no integration base is recorded. Otherwise
            ``integration`` holding the base, the issues landed there that
            the forge has not closed, the issues still claimed, whether
            the forge's open issues were read whole as ``catalog``, and
            when the newest claim ended as ``settled_at``.
        """
        from agent_parley.cli import issues

        base = data.get("integration_base") or ""
        if not base:
            return {}
        known = self.forge_issues(directory, data["root"])
        ledger = context["ledger"]
        return {
            "integration": {
                "base": base,
                "landed": issues.landed(
                    ledger, base, known["issues"] if known["complete"] else None
                ),
                "catalog": bool(known["complete"]),
                "settled_at": issues.settled_at(ledger),
                "held": sorted(
                    (
                        int(number)
                        for number, record in ledger["issues"].items()
                        if record.get("owner")
                    ),
                ),
            }
        }

    def _health(self, report: dict) -> None:
        """Prints the service, code, store, notify and state directory lines.

        Args:
            report: Reading produced by `status_snapshot`.
        """
        from agent_parley.cli import notification_line, store

        print(f"Service: {service_state(report['server'])}")
        if report["server"].get("state") == protocol.STALE:
            print(f"Code: {protocol.STALE}; {protocol.RELAUNCH}")
        schema = store.schema_state(store.schema_version(self.home))
        if repair := store.remedy(schema):
            if schema == store.SCHEMA_DAMAGED:
                schema = f"{schema} ({store.damage(self.home)})"
            print(f"Store: {schema}; {repair}")
        if "outbound" in report:
            print(notification_line(report["outbound"], report["inbound"]))
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
        return registered_root(self.home, path)

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
        for their rows, and so are the lanes `folded` past the project's
        `fold_after`. Dormant projects are left out unless `every_project`
        asks for them, or the selection names that project.

        Args:
            selection: Filters the operator asked for; its project filter
                scopes the view to one project.
            width: Columns the tables may use, or None for whole lines.
            every_claim: Also list claims whose work ended on the forge and
                lanes folded out as long dead.
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
        """Reads the forge's open issues from the supervisor's cache.

        The open-work view hides claims on closed issues and shows titles,
        and only the forge knows either. `status` stays read-only, so it
        never calls the forge: the service's poll keeps `FORGE_ISSUES`
        current through `supervision.refresh_forge_issues`, and this reads
        that file alone. A reading older than `FORGE_STALE` seconds is
        reported stale, with a recorded failed read named as the cause.

        Args:
            directory: Private state directory of the project.
            root: Recorded repository root that selects the forge project.

        Returns:
            ``issues`` as issue number to its title, or None when no reading
            was ever cached; ``complete`` when that reading held every open
            issue; ``age_seconds`` of the reading; ``fresh`` when it is
            within `FORGE_STALE`; and ``reason`` naming why it is not.
        """
        from agent_parley.cli import forge, json

        try:
            cached = json.loads((directory / FORGE_ISSUES).read_text())
        except (OSError, ValueError):
            cached = {}
        if not isinstance(cached, dict):
            cached = {}
        now = time.time()
        stored = cached.get("issues")
        issues = stored if isinstance(stored, dict) else None
        read_at = float(cached.get("read_at") or 0)
        limit = int(cached.get("limit") or FORGE_LIMIT)
        if issues is not None and now - read_at < FORGE_STALE:
            return reading(issues, limit, now - read_at, True, "")
        try:
            manifest = json.loads((directory / "project.json").read_text())
        except (OSError, ValueError):
            manifest = {}
        if forge.select(Path(root), manifest) != "github":
            return reading(issues, limit, now - read_at, False, "no GitHub")
        failed = float(cached.get("failed_at") or 0) > read_at
        failure = cached.get("failure")
        kind = failure.get("kind") if isinstance(failure, dict) else None
        reason = (
            "forge unreachable"
            + (f": {kind}" if kind and kind != "error" else "")
            if failed
            else "service not refreshing"
        )
        return reading(issues, limit, now - read_at, False, reason)

    def _board_project(
        self, project: dict, width: int | None, every_claim: bool
    ) -> None:
        """Prints one project's open work, lanes and claims needing action.

        Args:
            project: One project record from the status reading.
            width: Columns the tables may use, or None for whole lines.
            every_claim: Also list claims whose work ended on the forge and
                lanes folded out as long dead.
        """
        from agent_parley import metrics, supervision
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
        runs: list[str] = []
        ended: list[int] = []
        gone = 0
        after = project.get("fold_after") or supervision.DEFAULTS["fold_after"]
        states = {
            record["participant"]: record["lane_state"]
            for record in project["participants"]
        }
        for record in project["participants"]:
            if not every_claim and folded(record, after):
                gone += 1
                continue
            owner = record["participant"]
            peer = supervision.live_peer(states, owner)
            live = 0
            for claim in record["claims"]:
                entry = (opened or {}).get(str(claim["issue"])) or {}
                closed = claim["ended"] or (
                    opened is not None
                    and str(claim["issue"]) not in opened
                    and not moved_after(claim, known)
                )
                if closed:
                    ended.append(claim["issue"])
                else:
                    live += 1
                    notes.extend(attention(claim, owner, project["root"], peer))
                    if claim.get("blueprint"):
                        runs.append(f"Blueprint {claim['blueprint']}")
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
        if gone:
            print(fold_line(gone, after, "dead lane"))
        for text in runs:
            print(text)
        if outcome := metrics.outcome_line(
            project.get("pull_request_outcomes") or {},
            tables.age(metrics.OUTCOME_WINDOW),
        ):
            print(outcome)
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
        from agent_parley import pool
        from agent_parley.cli import (
            Selection,
            describe,
            json,
            lane_detail,
            lanes,
            metrics,
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
        readings = [
            (path, roster.normalize(json.loads(path.read_text())))
            for path in (self.home / "projects").glob("*/project.json")
        ]
        readings.sort(key=lambda item: item[1]["root"])
        for path, data in readings:
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
            if line := landed_line(project.get("integration") or {}):
                print(line)
            if outcome := metrics.outcome_line(
                project.get("pull_request_outcomes") or {},
                tables.age(metrics.OUTCOME_WINDOW),
            ):
                print(outcome)
            if measured := reclaim.summary_line(project.get("reclaim") or {}):
                print(measured)
            if spares := pool.summary_line(project.get("spares") or {}):
                print(spares)
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
            for record in reported:
                for claim in record["claims"]:
                    if claim.get("blueprint"):
                        print(f"Blueprint {claim['blueprint']}")
        return matched
