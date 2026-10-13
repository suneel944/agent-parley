"""Applies the recommended default to a reversible decision left unanswered.

Hands-free work stalls whenever the operator is away: a question nobody
answers holds its lane indefinitely, even when the choice is reversible and
has an obvious answer. `KINDS` is the one table of decision kinds. Each kind
declares whether it is ``reversible`` or ``irreversible``, the option the
service recommends, and the command that undoes that option.

A reversible kind waits `TIMEOUT_SECONDS` for an answer. When it expires
unanswered, `settle` returns the recommended option as ``applied by
timeout`` with the evidence it used, `record` appends that to the lane's
report log and `announce` sends one line naming what was done and the
command that undoes it. A reversible kind whose evidence is already
conclusive, such as an issue closed by a merged pull request from the
current claim generation, is settled without asking at all and recorded as
``applied on evidence``.

Each supervision poll runs `sweep` over the project's open decision records.
`EVENTS` names the table kind each recorded notification kind settles as; a
record whose kind it does not name is left open. A settled record is answered
with its own recommended option by `ANSWERED_BY` and handed to its lane the
way any other answer is.

An irreversible kind never times out: a merge to the default branch, a
release or tag, discarding uncommitted work, deleting a branch, every
native permission prompt and every tool call a native permission layer
denied always wait for an answer. A kind this table does
not know is treated as irreversible, so a misspelled or newer kind fails
closed.

A project can record, with `agent-parley timeout set`, that a kind is
always asked or that its timeout is longer than the default. The policy is
kept in the private manifest under ``timeouts``. It can only make a kind
more cautious: a timeout shorter than the default is refused, and no policy
gives an irreversible kind a timeout.
"""

from __future__ import annotations

import shlex
import time
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from agent_parley import decisions, metrics, notify, roster
from agent_parley.state import BridgeError

if TYPE_CHECKING:
    from agent_parley.cli import Bridge

REVERSIBLE = "reversible"
IRREVERSIBLE = "irreversible"
TIMEOUT_SECONDS = 30 * 60.0
MAX_TIMEOUT_SECONDS = 7 * 24 * 3600.0
POLICY = "timeouts"
ASK = "ask"
SECONDS = "seconds"
APPLIED = "applied by timeout"
CONCLUDED = "applied on evidence"
RECORD = "default"
WAIT = "wait for the operator"
ANSWERED_BY = "timeout"


class Kind(NamedTuple):
    """One decision kind's class, recommended option and undo command.

    Attributes:
        reversibility: `REVERSIBLE` or `IRREVERSIBLE`.
        recommended: Option applied when a reversible decision settles
            without an answer.
        undo: Command template that reverses the recommended option, filled
            from the decision's fields; empty for an irreversible kind.
        summary: What the decision is about, for the table and the notice.
    """

    reversibility: str
    recommended: str
    undo: str
    summary: str


KINDS: dict[str, Kind] = {
    "orphan_claim": Kind(
        REVERSIBLE,
        "reassign",
        "agent-parley issue assign {issue} {lane}",
        "reassign a dead lane's orphaned claim",
    ),
    "closed_claim": Kind(
        REVERSIBLE,
        "resolve",
        "agent-parley issue assign {issue} {lane}",
        "end a claim whose issue a merged pull request closed",
    ),
    "idle_key": Kind(
        REVERSIBLE,
        "release",
        "agent-parley say {lane} {key} --subject 'Reserve this key again'",
        "release a key its idle holder never uses",
    ),
    "failed_ci": Kind(
        REVERSIBLE,
        "rerun once",
        "gh run cancel {run}",
        "re-run a failed CI job once",
    ),
    "merge_default": Kind(
        IRREVERSIBLE, WAIT, "", "merge to the default branch"
    ),
    "release_tag": Kind(IRREVERSIBLE, WAIT, "", "publish a release or tag"),
    "discard_work": Kind(IRREVERSIBLE, WAIT, "", "discard uncommitted work"),
    "delete_branch": Kind(IRREVERSIBLE, WAIT, "", "delete a branch"),
    "native_permission": Kind(
        IRREVERSIBLE, WAIT, "", "answer a native permission prompt"
    ),
}

EVENTS: dict[str, str] = {
    notify.Event.ORPHAN_DECISION: "orphan_claim",
    notify.Event.PERMISSION_PROMPT: "native_permission",
    notify.Event.PERMISSION_DENIED: "native_permission",
    notify.Event.NATIVE_DIALOG: "native_permission",
}


def kind_of(name: str) -> Kind:
    """Looks a decision kind up, failing closed on an unknown name.

    Args:
        name: Decision kind name.

    Returns:
        The kind's table entry, or an irreversible entry that always waits
        when the table does not know the name.
    """
    return KINDS.get(name) or Kind(IRREVERSIBLE, WAIT, "", name)


def policy(manifest: dict) -> dict[str, dict]:
    """Reads the project's decision timeout policy strictly.

    Args:
        manifest: Project manifest using the participant roster layout.

    Returns:
        Per-kind entries, each either ``{"ask": True}`` or ``{"seconds":
        N}`` with N at least `TIMEOUT_SECONDS`.

    Raises:
        BridgeError: If the recorded policy names an unknown kind, gives an
            irreversible kind a timeout, or shortens a timeout. An invalid
            policy is refused, never read as permission to act sooner.
    """
    recorded = manifest.get(POLICY) or {}
    if not isinstance(recorded, dict):
        raise BridgeError("The recorded decision timeout policy is invalid.")
    for name, entry in recorded.items():
        _valid(name, entry)
    return recorded


def _valid(name: str, entry: object) -> None:
    """Refuses one policy entry that would make a kind less cautious."""
    if name not in KINDS:
        raise BridgeError(
            f"{name} is not a decision kind; known kinds are "
            + ", ".join(sorted(KINDS))
            + "."
        )
    if entry == {ASK: True}:
        return
    seconds = entry.get(SECONDS) if isinstance(entry, dict) else None
    if (
        not isinstance(entry, dict)
        or set(entry) != {SECONDS}
        or isinstance(seconds, bool)
        or not isinstance(seconds, (int, float))
    ):
        raise BridgeError(f"The timeout policy for {name} is invalid.")
    if KINDS[name].reversibility == IRREVERSIBLE:
        raise BridgeError(
            f"{name} is irreversible and always waits for an answer; no "
            "policy can give it a timeout."
        )
    if not TIMEOUT_SECONDS <= seconds <= MAX_TIMEOUT_SECONDS:
        raise BridgeError(
            f"A policy can only raise the timeout for {name}: give between "
            f"{int(TIMEOUT_SECONDS)} and {int(MAX_TIMEOUT_SECONDS)} seconds."
        )


def timeout(manifest: dict, name: str) -> float | None:
    """Returns how long a decision waits before its default is applied.

    Args:
        manifest: Project manifest using the participant roster layout.
        name: Decision kind name.

    Returns:
        Seconds the decision waits for an answer, or None when it always
        waits: the kind is irreversible or unknown, or the project policy
        says to always ask.

    Raises:
        BridgeError: If the recorded policy is invalid.
    """
    if kind_of(name).reversibility != REVERSIBLE:
        return None
    entry = policy(manifest).get(name, {})
    if entry.get(ASK):
        return None
    return float(entry.get(SECONDS, TIMEOUT_SECONDS))


def undo(name: str, fields: dict) -> str:
    """Fills the command that reverses a kind's recommended option.

    Args:
        name: Decision kind name.
        fields: Values the undo template names, such as ``issue`` and
            ``lane``.

    Returns:
        The command to paste, or an empty string for a kind that has none.

    Raises:
        BridgeError: If a field the template names is missing, so a default
            is never applied without a way back.
    """
    template = kind_of(name).undo
    if not template:
        return ""
    quoted = {key: shlex.quote(str(value)) for key, value in fields.items()}
    try:
        return template.format(**quoted)
    except KeyError as exc:
        raise BridgeError(
            f"A {name} decision needs {exc.args[0]} to name its undo command."
        ) from exc


def settle(manifest: dict, question: dict, now: float) -> dict | None:
    """Decides whether an open question is settled without an answer.

    Args:
        manifest: Project manifest using the participant roster layout.
        question: The open decision: ``kind``, ``asked_at``, the ``fields``
            its undo command names, the ``evidence`` it rests on, and
            ``conclusive`` when that evidence already decides it.
        now: Current time in seconds since the epoch.

    Returns:
        The applied decision, with its kind, option, outcome, evidence and
        undo command, or None while the question must keep waiting.

    Raises:
        BridgeError: If the policy is invalid or a field the undo command
            needs is missing.
    """
    name = str(question.get("kind", ""))
    waits = timeout(manifest, name)
    if waits is None:
        return None
    if question.get("conclusive"):
        outcome = CONCLUDED
    elif now - float(question.get("asked_at", now)) >= waits:
        outcome = APPLIED
    else:
        return None
    fields = dict(question.get("fields") or {})
    return {
        "decision": name,
        "option": KINDS[name].recommended,
        "outcome": outcome,
        "evidence": list(question.get("evidence") or []),
        "fields": fields,
        "undo": undo(name, fields),
    }


def line(applied: dict) -> str:
    """Words the one-line notice for an applied default.

    Args:
        applied: Decision returned by `settle`.

    Returns:
        What was done, why, and the command that undoes it.
    """
    kind = kind_of(applied["decision"])
    return (
        f"{applied['outcome']}: {kind.summary} ({applied['option']}); "
        f"undo with `{applied['undo']}`"
    )


def record(directory: Path, name: str, applied: dict) -> dict:
    """Appends an applied default to the lane's durable report log.

    Args:
        directory: Private state directory for the common repository.
        name: Participant whose lane the decision concerns.
        applied: Decision returned by `settle`.

    Returns:
        The appended record.
    """
    return metrics.record_report(directory, name, {"kind": RECORD, **applied})


def announce(applied: dict) -> list[dict]:
    """Sends the applied default's one-line notice, best effort.

    Args:
        applied: Decision returned by `settle`.

    Returns:
        One delivery result per configured transport; empty when
        notification is off.
    """
    if not notify.enabled():
        return []
    text = line(applied)
    subject = text[: notify.MAX_SUBJECT_BYTES]
    return notify.send(notify.settings(), subject, text)


def sweep(
    home: Path, directory: Path, manifest: dict, now: float = 0.0
) -> list[dict]:
    """Answers every open reversible decision whose timeout has passed.

    Only a record that `EVENTS` maps to a reversible kind, that is itself
    recorded reversible, and that recommends one of its own options is
    settled; every other record keeps waiting. A settled record is answered
    with that option by `ANSWERED_BY`, handed to its lane, appended to the
    lane's report log and announced. A record answered first by the
    operator, or missing a field its undo command needs, is left alone.

    Args:
        home: Private bridge state root.
        directory: Private project state directory.
        manifest: Project manifest using the participant roster layout.
        now: Unix time; the clock when zero.

    Returns:
        The decisions applied, as `settle` returned them.

    Raises:
        BridgeError: If the recorded policy is invalid, so no default is
            applied until it is fixed.
    """
    import contextlib
    import sqlite3

    from agent_parley import inbound

    stamp = now or time.time()
    policy(manifest)
    done = []
    for entry in decisions.list_open(directory, stamp):
        name = EVENTS.get(str(entry.get("kind", "")), "")
        option = str(entry.get("recommended", ""))
        if (
            not name
            or entry.get("reversibility") != decisions.REVERSIBLE
            or option not in decisions.options(entry)
        ):
            continue
        asked = float(entry.get("created", stamp))
        question = {
            "kind": name,
            "asked_at": asked,
            "fields": {
                key: str(entry[key])
                for key in ("issue", "lane")
                if entry.get(key)
            },
            "evidence": [
                f"decision {entry['id']} ({entry.get('question', '')}) "
                f"unanswered for {int(stamp - asked)} seconds"
            ],
        }
        try:
            applied = settle(manifest, question, stamp)
            if applied is None:
                continue
            answered = decisions.answer(
                directory, entry["id"], option, ANSWERED_BY, now=stamp
            )
        except BridgeError:
            continue
        with contextlib.suppress(
            OSError, ValueError, BridgeError, sqlite3.Error
        ):
            inbound.settle(home, directory, answered)
        record(directory, str(entry.get("lane", "")), applied)
        announce(applied)
        done.append(applied)
    return done


def describe(manifest: dict) -> str:
    """Tabulates every decision kind under the project's policy.

    Args:
        manifest: Project manifest using the participant roster layout.

    Returns:
        One line per kind: its class, recommended option and timeout.

    Raises:
        BridgeError: If the recorded policy is invalid.
    """
    rows = []
    for name, kind in KINDS.items():
        waits = timeout(manifest, name)
        after = "always asks" if waits is None else f"{int(waits)}s"
        rows.append(
            f"{name}: {kind.reversibility}, default {kind.recommended}, {after}"
        )
    return "\n".join(rows)


def show(bridge: Bridge, repo: Path) -> str:
    """Tabulates every decision kind under the recorded project policy.

    Args:
        bridge: Coordination runtime owning the project state.
        repo: Any checkout of the target repository.

    Returns:
        The table `describe` renders.

    Raises:
        BridgeError: If the repository has no project or the recorded
            policy is invalid.
    """
    _, directory = bridge.project(repo, create=False)
    return describe(roster.read(directory))


def configure(
    bridge: Bridge,
    repo: Path,
    name: str,
    ask: bool = False,
    seconds: float | None = None,
) -> str:
    """Records how long one decision kind waits, or that it always asks.

    Only an operator in the base checkout may run this. Without ``ask`` or
    ``seconds`` the kind returns to its default.

    Args:
        bridge: Coordination runtime owning the project state.
        repo: Any checkout of the target repository, outside every lane.
        name: Decision kind name.
        ask: Always ask, never apply the default.
        seconds: Longer timeout than `TIMEOUT_SECONDS`.

    Returns:
        The table under the recorded policy.

    Raises:
        BridgeError: If the command runs inside a lane, the repository has
            no project, or the entry would make the kind less cautious.
    """
    from agent_parley.cli import lock, write_json
    from agent_parley.unattended import operator_only

    root, directory = bridge.project(repo, create=False)
    operator_only(
        repo, root, roster.read(directory), "A decision timeout is set"
    )
    entry: dict | None = None
    if ask:
        entry = {ASK: True}
    elif seconds is not None:
        entry = {SECONDS: seconds}
    if entry is not None:
        _valid(name, entry)
    elif name not in KINDS:
        _valid(name, {ASK: True})
    with lock(directory / "setup.lock"):
        data = roster.read(directory)
        recorded = dict(policy(data))
        recorded.pop(name, None)
        if entry is not None:
            recorded[name] = entry
        data[POLICY] = recorded
        write_json(directory / "project.json", data)
    return describe(roster.read(directory))
