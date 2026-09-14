"""Records the operator decision that integration commands can require.

A lane reports itself ready, and nothing in that report says a human read it.
A repository can require a recorded operator decision before either command
that carries a lane's work out of its worktree: ``participant merge`` and
``participant pr``. The decision is appended to the lane's durable report log
in coordination state, outside every target repository, and it is written only
by ``agent-parley approve`` and ``agent-parley reject``, which refuse to run
from a lane worktree. No served coordination tool, hook or lane command
records one, so no command a lane can reach approves that lane's own work.

That is the product's own command-line boundary, not an operating-system one.
A program running as the same user can write the same state directly; run the
operator and the lanes as different users, or in different containers, when
that distinction has to hold. The gate is also not a verification of the code.
It records that a named local account decided, and the repository's own Git
and GitHub checks, including the configured verification command, still run
unchanged.

An approval is bound to one state of one lane: the identifier of the ready
report it answers, the lane branch's commit, the branch and base it targets,
the repository root, and a digest of the verification command, pull-request
policy and approval requirement in force. Anything in that binding changing
invalidates the approval, so new commits, a further report, a retargeted base
or a changed gate each require a new decision even when the lane never reports
again. The binding is rechecked immediately before the merge or the push,
while the lane's session exclusion is held.

Reading fails closed. A report log that exists but cannot be read, or that
holds a damaged record, refuses integration rather than treating the missing
decision as absent consent.
"""

from __future__ import annotations

import getpass
import hashlib
import json
from pathlib import Path

from agent_parley import metrics
from agent_parley.roster import APPROVAL_STEPS
from agent_parley.state import BridgeError

STEPS = APPROVAL_STEPS
APPROVED = "approved"
REJECTED = "rejected"
AWAITING = "awaiting approval"
UNREPORTED = "unreported"
COMMANDS = {"merge": "participant merge", "pr": "participant pr"}
RENEWED = (
    "New commits, a further report, a retargeted base or a changed "
    "verification or pull-request policy each require a new approval."
)


def operator() -> str:
    """Names the local account recording a decision.

    Returns:
        The account name the operating system reports for this process, or a
        plain placeholder when no name can be read.
    """
    try:
        return getpass.getuser()
    except (KeyError, OSError):
        return "unknown"


def policy_digest(manifest: dict) -> str:
    """Digests the project settings a decision is answerable to.

    Args:
        manifest: Project manifest holding the lane.

    Returns:
        A short digest of the verification command, the pull-request policy
        and the approval requirement, so changing any of them invalidates a
        decision that was taken under the previous settings.
    """
    material = json.dumps(
        {
            "verify": list(manifest.get("verify") or []),
            "pull_request": dict(manifest.get("pull_request") or {}),
            "approval": list(manifest.get("approval") or []),
        },
        sort_keys=True,
    )
    return hashlib.sha256(material.encode()).hexdigest()[:16]


def binding(manifest: dict, name: str, head: str, report: str) -> dict:
    """Describes the exact lane state one decision answers.

    Args:
        manifest: Project manifest holding the lane.
        name: Participant that owns the lane.
        head: Commit the lane's branch points at.
        report: Identifier of the ready report under review.

    Returns:
        The bound identity of the work: report, commit, branch, base,
        repository root and policy digest.
    """
    participant = manifest["participants"][name]
    return {
        "report": report,
        "head": head,
        "branch": participant["branch"],
        "base": manifest["base"],
        "root": manifest["root"],
        "policy": policy_digest(manifest),
    }


def _unreadable(name: str, path: Path) -> BridgeError:
    """Builds the refusal used when a decision cannot be read at all."""
    return BridgeError(
        f"The report and approval log for {name} at {path} cannot be read, "
        "so no operator approval can be confirmed. Integration is refused "
        "until the log is readable again; repair or restore it, then record "
        "the decision anew."
    )


def _log(directory: Path, name: str) -> list[dict]:
    """Reads one lane's durable report and decision log strictly.

    Args:
        directory: Private state directory for the common repository.
        name: Participant that owns the lane.

    Returns:
        Every record in the log, oldest first, or an empty list when the lane
        has recorded nothing yet.

    Raises:
        BridgeError: If the log exists but cannot be read, or holds a record
            that cannot be parsed. A damaged log refuses integration instead
            of being read past, because a skipped record could be the one
            decision that matters.
    """
    path = metrics.report_path(directory, name)
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        raise _unreadable(name, path) from exc
    records = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError as exc:
            raise _unreadable(name, path) from exc
        if not isinstance(record, dict):
            raise _unreadable(name, path)
        records.append(record)
    return records


def _drift(recorded: dict, current: dict) -> str:
    """Names what changed since a decision was recorded."""
    if recorded.get("report") != current["report"]:
        return "it answered an earlier report"
    if recorded.get("head") != current["head"]:
        return "the lane has committed since it was recorded"
    if recorded.get("branch") != current["branch"] or recorded.get(
        "root"
    ) != current.get("root"):
        return "the lane now targets a different branch"
    if recorded.get("base") != current["base"]:
        return "the project base changed"
    return "the verification command or pull-request policy changed"


def review(directory: Path, manifest: dict, name: str, head: str) -> dict:
    """Reads the operator decision standing against a lane's current work.

    Args:
        directory: Private state directory for the common repository.
        manifest: Project manifest holding the lane.
        name: Participant that owns the lane.
        head: Commit the lane's branch points at.

    Returns:
        The decision state, the report it concerns, the binding the work has
        now, the recorded decision if one exists, and a detail naming what
        invalidated an earlier decision.

    Raises:
        BridgeError: If the lane's log cannot be read, so that an unreadable
            record refuses rather than permits.
    """
    records = _log(directory, name)
    report = ""
    decision: dict | None = None
    for record in records:
        if record.get("kind") == "report":
            report = (
                str(record.get("id", ""))
                if record.get("state") == "ready"
                else ""
            )
        elif record.get("kind") == "approval":
            decision = record
    current = binding(manifest, name, head, report)
    reviewed = {
        "state": UNREPORTED,
        "report": report,
        "binding": current,
        "decision": decision,
        "detail": "",
    }
    if not report:
        return reviewed
    recorded = (decision or {}).get("binding")
    if decision is None or not isinstance(recorded, dict):
        return {**reviewed, "state": AWAITING}
    if recorded != current:
        drift = _drift(recorded, current)
        return {**reviewed, "state": AWAITING, "detail": drift}
    state = REJECTED if decision.get("decision") == REJECTED else APPROVED
    return {**reviewed, "state": state}


def refusal(name: str, step: str, reviewed: dict) -> str:
    """Explains why one step is refused, naming the report and the command.

    Args:
        name: Participant that owns the lane.
        step: Step being attempted, ``merge`` or ``pr``.
        reviewed: Decision state from :func:`review`.

    Returns:
        The refusal to raise, or an empty string when the step may proceed.
    """
    command = COMMANDS[step]
    grant = f"agent-parley approve {name}"
    if reviewed["state"] == APPROVED:
        return ""
    if reviewed["state"] == UNREPORTED:
        return (
            f"{command} requires a recorded operator approval of {name}'s "
            "ready report, and no current ready report exists. Have the lane "
            'run `agent-parley report --state ready --summary "..." '
            f'--evidence "..."`, then run `{grant}`.'
        )
    report = reviewed["report"]
    if reviewed["state"] == REJECTED:
        reason = (reviewed["decision"] or {}).get("reason", "")
        return (
            f"The operator rejected {name}'s report {report}"
            + (f": {reason}" if reason else ".")
            + f" {command} stays refused until a new decision is recorded "
            f"with `{grant}`."
        )
    detail = reviewed["detail"]
    lapsed = (
        f", and the last decision no longer applies because {detail}"
        if detail
        else ""
    )
    return (
        f"{command} requires a recorded operator approval of {name}'s report "
        f"{report}{lapsed}. Review the report, then run `{grant}` or "
        f'`agent-parley reject {name} "reason"`. {RENEWED}'
    )


def remember(directory: Path, name: str, entry: dict) -> dict:
    """Appends one operator decision and confirms it was persisted.

    Args:
        directory: Private state directory for the common repository.
        name: Participant that owns the lane.
        entry: Decision fields, without an identifier or timestamp.

    Returns:
        The recorded decision, read back from the log.

    Raises:
        BridgeError: If the decision cannot be read back, so an approval is
            never reported as recorded when the log did not take it.
    """
    written = metrics.record_report(directory, name, entry)
    for record in _log(directory, name):
        if record.get("id") == written["id"]:
            return record
    raise _unreadable(name, metrics.report_path(directory, name))
