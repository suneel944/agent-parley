"""Accounts whether an owned issue converges on a verified result.

Wake accounting asks whether a lane moved its work: a commit, a report, a
claim transition or a reservation resets its attempt count. None of those
says the work got closer to an accepted result, so a lane can keep changing
a failing issue for as long as it keeps moving. This module keeps a second,
separate account per issue that only verification evidence can move.

The account is bound to the issue, its claim generation and the commit each
piece of evidence names. It reads the lane's durable report log: a report's
`--evidence` text and a peer's review verdict on that report. Evidence text
is classified deterministically as a pass, a failure or no verification
evidence. A failure keeps only a digest of its normalized first failing
line, so neither raw logs nor command output are stored.

Only a passing result is verified improvement. It records a new milestone
and clears the account. A failure is counted once per distinct commit and
signature, so a report restated at the same commit counts once and a commit
that changes nothing about the failure still counts. A new signature starts
a new repeat run without clearing the failures already counted, so
alternating between two failures never looks like progress. A report that
carries no classifiable verification is counted as unverified and is never
evidence of failure or of success.

Blocked work and work reported ready wait on something outside the lane, an
external check, another issue or verification and integration. While it
waits the account is frozen: nothing is decided and the time does not count
against the milestone.

Past the configured thresholds the owner is asked once to change approach
and, if failures continue, told once more with the explicit handoff commands
while the operator is notified through any configured transport. Both
responses are mail with a stable key and
never move ownership, answer an approval or stop a lane. After the second
response nothing further is sent until a verified improvement or a new claim
generation, and no generation receives more than `MAX_RESPONSES`.
"""

import contextlib
import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path

from agent_parley import issues, lifecycle
from agent_parley.state import BridgeError, lock, write_json

PUBLICATION = "convergence.json"
PASS = "pass"
FAIL = "fail"
NONE = "none"
APPROACH = "approach"
ESCALATED = "escalated"
MISSING = "no verification evidence"
PASSING = "passing"
FAILING = "failing"
MAX_ENTRIES = 256
MAX_SEEN = 16
MAX_IDS = 64
MAX_RESPONSES = 6
MAX_LINE = 200
SLACK = 60.0
VERDICTS = {"pass": PASS, "fail": FAIL}

_ZERO = re.compile(
    r"\b(?:0|no)\s+(?:failed|failures?|errors?|issues?)\b"
    r"|\b(?:failed|failures?|errors?)\s*[:=]?\s*0\b",
    re.IGNORECASE,
)
_FAILED = re.compile(
    r"\b(?:[1-9]\d*\s+(?:failed|failures?|errors?)"
    r"|(?:failed|failures?|errors?)\s*[:=]\s*[1-9]\d*"
    r"|traceback|assertionerror|not ok|exit (?:status|code) [1-9]\d*)\b"
)
_FAILED_TOKEN = re.compile(r"\bFAILED\b")
_PASSED = re.compile(
    r"\b(?:passed|passes|pass|green|succeeded|success|ok"
    r"|exit (?:status|code) 0)\b"
)
_PATH = re.compile(r"(?:[\w.~-]*/)+([\w.-]+)")
_HEX = re.compile(r"\b(?:0x[0-9a-f]+|[0-9a-f]{7,64})\b")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_SPACE = re.compile(r"\s+")


def signature(line: str) -> str:
    """Digests one failure line after removing what varies between runs.

    Paths keep only their last component, hexadecimal identifiers and
    numbers are replaced by placeholders, and whitespace is collapsed, so a
    line number, a duration or a temporary directory that changes between
    runs does not change the signature.

    Args:
        line: One line of verification output.

    Returns:
        A 16-character digest; the line itself is never kept.
    """
    text = _PATH.sub(r"\1", line.lower())
    text = _HEX.sub("<hex>", text)
    text = _NUMBER.sub("<n>", text)
    text = _SPACE.sub(" ", text).strip()[:MAX_LINE]
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def classify(text: str) -> tuple[str, str]:
    """Classifies verification text as a pass, a failure or neither.

    Counts of zero (`0 failed`, `errors: 0`, `no errors`) are removed first.
    A failure is only a non-zero failure or error count (`2 failed`,
    `errors: 1`), pytest's upper-case `FAILED` token, a traceback, an
    `AssertionError`, a TAP `not ok` or a non-zero exit status, so prose
    such as `fixed error handling` or `ran without errors` is never one.
    Any such marker makes the text a failure, whose signature is taken from
    the first line carrying one. Otherwise a pass word makes it a pass. Text
    with neither is no verification evidence.

    Args:
        text: Evidence text a lane or a reviewer recorded.

    Returns:
        The outcome, one of `PASS`, `FAIL` or `NONE`, and the failure
        signature, empty unless the outcome is `FAIL`.
    """
    lines = [_ZERO.sub(" ", line) for line in text.splitlines()]
    for line in lines:
        if _FAILED_TOKEN.search(line) or _FAILED.search(line.lower()):
            return FAIL, signature(line)
    if any(_PASSED.search(line.lower()) for line in lines):
        return PASS, ""
    return NONE, ""


def evidence(record: dict, reports: dict[str, dict]) -> dict | None:
    """Reads one verification item from a lane's report log record.

    A report is classified from its own evidence alone, whatever its state:
    ready evidence without a pass word is no verification evidence. A peer
    verdict is bound to the report it judges, so it inherits that report's
    issue, claim generation, commit and time; the latter lets `fold` ignore
    a pass on a report older than a failure already recorded.

    Args:
        record: One record from the lane's durable report log.
        reports: Report records of the same log keyed by identifier.

    Returns:
        The item with its issue, claim generation, commit, time, the time of
        the report it judges, identifier, outcome and signature, or None for
        a record that is neither a report nor a verdict on a retained report.
    """
    kind = record.get("kind")
    text = str(record.get("evidence") or "")
    if kind == "report":
        subject: dict | None = record
        found, mark = classify(text)
    elif kind == "review":
        subject = reports.get(str(record.get("report_id")))
        found = VERDICTS.get(str(record.get("verdict")), NONE)
        mark = ""
        if found == FAIL:
            mark = classify(text)[1] or signature(text.strip()[:MAX_LINE])
    else:
        return None
    if subject is None:
        return None
    at = float(record.get("at", 0) or 0)
    return {
        "issue": str(subject.get("issue")),
        "claim_id": str(subject.get("claim_id") or ""),
        "commit": str(subject.get("commit") or ""),
        "at": at,
        "subject_at": float(subject.get("at", at) or 0),
        "id": str(record.get("id", "")),
        "outcome": found,
        "signature": mark,
    }


def fresh(claim_id: str, owner: str, since: float, cursor: float = 0.0) -> dict:
    """Starts the account of one claim generation.

    Args:
        claim_id: Ownership generation the account belongs to.
        owner: Lane holding that generation.
        since: When the generation began, its first milestone.
        cursor: Time from which report records are read, less `SLACK`;
            the supervisor passes the current time, so a new or upgraded
            account starts empty rather than replaying retained reports.

    Returns:
        An account with no evidence yet.
    """
    return {
        "claim_id": claim_id,
        "owner": owner,
        "since": since,
        "cursor": cursor,
        "ids": [],
        "milestone": {"at": since, "commit": "", "source": "claim"},
        "last": None,
        "failed_at": 0.0,
        "unverified": 0,
        "failures": 0,
        "repeats": 0,
        "signature": "",
        "seen": [],
        "wait": None,
        "waited": 0.0,
        "stage": "",
        "stage_at": None,
        "stage_failures": 0,
        "stage_waited": 0.0,
        "responses": 0,
    }


def fold(entry: dict, item: dict) -> dict:
    """Applies one verification item to an account.

    A pass whose judged report is older than the latest recorded failure is
    stale: it is marked applied but neither resets the account nor counts.

    Args:
        entry: Current account of the item's claim generation.
        item: Verification item from `evidence`.

    Returns:
        The updated account; the input is not changed.
    """
    current = dict(entry)
    current["cursor"] = max(float(entry["cursor"]), item["at"])
    current["ids"] = [*entry["ids"], item["id"]][-MAX_IDS:]
    if item["outcome"] == NONE:
        current["unverified"] = int(entry["unverified"]) + 1
        return current
    failed_at = float(entry.get("failed_at") or 0.0)
    if item["outcome"] == PASS and (
        float(item.get("subject_at", item["at"])) < failed_at
    ):
        return current
    if item["outcome"] == FAIL:
        current["failed_at"] = max(failed_at, item["at"])
    current["unverified"] = 0
    current["last"] = {
        "outcome": item["outcome"],
        "commit": item["commit"],
        "at": item["at"],
        "id": item["id"],
    }
    if item["outcome"] == PASS:
        wait = entry.get("wait")
        current.update(
            milestone={
                "at": item["at"],
                "commit": item["commit"],
                "source": item["id"],
            },
            failures=0,
            repeats=0,
            signature="",
            seen=[],
            waited=0.0,
            wait=(
                {**wait, "since": max(float(wait["since"]), item["at"])}
                if wait
                else None
            ),
            stage="",
            stage_at=None,
            stage_failures=0,
            stage_waited=0.0,
        )
        return current
    key = f"{item['commit']}:{item['signature']}"
    if key in entry["seen"]:
        return current
    current["seen"] = [*entry["seen"], key][-MAX_SEEN:]
    current["failures"] = int(entry["failures"]) + 1
    current["repeats"] = (
        int(entry["repeats"]) + 1
        if item["signature"] == entry["signature"]
        else 1
    )
    current["signature"] = item["signature"]
    return current


def waiting(record: dict) -> str:
    """Names what an issue waits on outside its lane, if anything.

    Args:
        record: Published ledger record for the issue.

    Returns:
        `issue #N` or `external check` for blocked work, `verification` for
        work reported ready, otherwise an empty string.
    """
    execution = lifecycle.state(record)
    if execution["state"] == lifecycle.READY:
        return "verification"
    if execution["state"] != lifecycle.BLOCKED:
        return ""
    condition = execution.get("resume_when") or {}
    if isinstance(condition, dict) and condition.get("kind") == "issue":
        return f"issue #{condition.get('issue')}"
    return "external check"


def settle_wait(entry: dict, kind: str, now: float) -> dict:
    """Opens, keeps or closes the account's wait on an outside condition.

    Args:
        entry: Current account.
        kind: What the issue waits on now, empty when it waits on nothing.
        now: Current Unix time.

    Returns:
        The account with its wait and accumulated waiting time updated.
    """
    current = dict(entry)
    wait = entry.get("wait")
    if kind and not wait:
        current["wait"] = {"kind": kind, "since": now}
    elif kind and wait:
        current["wait"] = {**wait, "kind": kind}
    elif wait:
        current["waited"] = float(entry["waited"]) + max(
            0.0, now - float(wait["since"])
        )
        current["wait"] = None
    return current


def age(entry: dict, now: float) -> float:
    """Seconds since the last verified milestone, less time spent waiting.

    Args:
        entry: Account to measure.
        now: Current Unix time.

    Returns:
        Working seconds without verified improvement, never negative.
    """
    waited = float(entry["waited"])
    if wait := entry.get("wait"):
        waited += max(0.0, now - float(wait["since"]))
    return max(0.0, now - float(entry["milestone"]["at"]) - waited)


def decide(entry: dict, now: float, repeats: int, after: float) -> str:
    """Decides the next bounded response an account calls for.

    Nothing is decided while the issue waits outside its lane, while no
    failure is recorded, or once the generation spent `MAX_RESPONSES`. The
    first response is due when one signature repeats `repeats` times, when
    `2 * repeats` failures accumulate, or when a failure stands `after`
    working seconds past the milestone. The second is due once `repeats`
    more failures, or `after` more working seconds, follow the first.

    Args:
        entry: Current account.
        now: Current Unix time.
        repeats: Configured repeated-failure threshold.
        after: Configured seconds without verified improvement.

    Returns:
        `APPROACH`, `ESCALATED` or an empty string when nothing is due.
    """
    if (
        entry.get("wait")
        or int(entry["failures"]) < 1
        or int(entry["responses"]) >= MAX_RESPONSES
    ):
        return ""
    if not entry["stage"]:
        if (
            int(entry["repeats"]) >= repeats
            or int(entry["failures"]) >= 2 * repeats
            or age(entry, now) >= after
        ):
            return APPROACH
        return ""
    if entry["stage"] != APPROACH:
        return ""
    elapsed = (
        now
        - float(entry["stage_at"] or now)
        - (float(entry["waited"]) - float(entry["stage_waited"]))
    )
    if (
        int(entry["failures"]) - int(entry["stage_failures"]) >= repeats
        or elapsed >= after
    ):
        return ESCALATED
    return ""


def read(directory: Path) -> dict[str, dict]:
    """Reads every recorded account, tolerating a missing or damaged file.

    Args:
        directory: Private project state directory.

    Returns:
        Accounts keyed by issue number.
    """
    accounts = _load(directory).get("issues")
    return accounts if isinstance(accounts, dict) else {}


def _load(directory: Path) -> dict:
    """Reads the whole account file, or an empty mapping when unreadable."""
    try:
        state = json.loads((directory / PUBLICATION).read_text())
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def _stamp(directory: Path, owner: str) -> list[int]:
    """Names one lane's report log version by modification time and size."""
    from agent_parley import metrics

    try:
        found = metrics.report_path(directory, owner).stat()
    except OSError:
        return [0, 0]
    return [found.st_mtime_ns, found.st_size]


def _order(item: tuple[str, dict]) -> tuple[int, str]:
    """Orders ledger items numerically, with non-numeric keys last."""
    number = item[0]
    return (int(number) if number.isdigit() else 1 << 62, number)


def observe(
    directory: Path,
    manifest: dict,
    repeats: int,
    after: float,
    now: float | None = None,
    respond: bool = True,
) -> list[dict]:
    """Updates every owned issue's account and returns the responses due.

    Accounts exist only for issues a participant owns in their current claim
    generation; any other account is dropped, so the file stays bounded by
    the claims in force. A generation change starts a fresh account whose
    cursor is the current time, so an upgraded service replays no retained
    report, and evidence naming another generation or issue is ignored.
    Records already applied are recognised by identifier, so a restart
    replays nothing. Each lane's report log is read and parsed at most once
    per pass, and not at all while its modification time and size match
    the previous pass.

    Args:
        directory: Private project state directory.
        manifest: Current participant manifest.
        repeats: Configured repeated-failure threshold.
        after: Configured seconds without verified improvement.
        now: Current Unix time, read from the clock when omitted.
        respond: Whether to decide responses; when False the accounts are
            still recorded but no stage is taken or returned.

    Returns:
        One mapping per response due, naming the issue, owner, claim
        generation, response stage and the account that decided it. The
        stage is recorded before this returns, so it is never due twice.
    """
    from agent_parley import metrics

    moment = time.time() if now is None else now
    ledger = issues.snapshot(directory)["issues"]
    logs: dict[str, tuple[list[dict], dict[str, dict]]] = {}
    actions: list[dict] = []
    with lock(directory / "convergence.lock", timeout=1):
        whole = _load(directory)
        recorded = whole.get("issues")
        state = recorded if isinstance(recorded, dict) else {}
        seen = whole.get("logs")
        before = seen if isinstance(seen, dict) else {}
        stamps: dict[str, list[int]] = {}
        kept: dict[str, dict] = {}
        for number, record in sorted(ledger.items(), key=_order):
            owner = record.get("owner")
            claim = record.get("claim_id")
            if owner not in manifest["participants"] or not claim:
                continue
            entry = state.get(number)
            if (
                not isinstance(entry, dict)
                or entry.get("claim_id") != claim
                or entry.get("owner") != owner
            ):
                entry = fresh(
                    str(claim),
                    str(owner),
                    issues.claimed_since(record) or moment,
                    moment,
                )
            if owner not in logs:
                stamps[owner] = _stamp(directory, owner)
                records = (
                    []
                    if before.get(owner) == stamps[owner]
                    else metrics.report_records(directory, owner)
                )
                logs[owner] = (
                    records,
                    {
                        str(item.get("id")): item
                        for item in records
                        if item.get("kind") == "report"
                    },
                )
            records, reports = logs[owner]
            floor = float(entry["cursor"]) - SLACK
            items = [
                item
                for item in (
                    evidence(raw, reports)
                    for raw in records
                    if float(raw.get("at", 0) or 0) > floor
                )
                if item is not None
                and item["issue"] == number
                and item["claim_id"] == claim
                and item["id"] not in entry["ids"]
            ]
            for item in sorted(items, key=lambda item: item["at"]):
                entry = fold(entry, item)
            entry = settle_wait(entry, waiting(record), moment)
            stage = decide(entry, moment, repeats, after) if respond else ""
            if stage:
                entry = {
                    **entry,
                    "stage": stage,
                    "stage_at": moment,
                    "stage_failures": entry["failures"],
                    "stage_waited": entry["waited"],
                    "responses": int(entry["responses"]) + 1,
                }
                actions.append(
                    {
                        "issue": number,
                        "owner": owner,
                        "claim_id": claim,
                        "stage": stage,
                        "entry": entry,
                    }
                )
            kept[number] = entry
        if len(kept) > MAX_ENTRIES:
            kept = dict(sorted(kept.items(), key=_order)[-MAX_ENTRIES:])
        if kept != state or stamps != before:
            write_json(
                directory / PUBLICATION,
                {"version": 1, "issues": kept, "logs": stamps},
            )
    return actions


def view(entry: dict | None, now: float | None = None) -> dict:
    """Reports one account without its internal bookkeeping.

    Args:
        entry: Account of the current claim generation, or None when the
            supervisor has not recorded one.
        now: Current Unix time, read from the clock when omitted.

    Returns:
        The evidence state (`passing`, `failing` or `no verification
        evidence`), the counts and signature behind it, the milestone and its
        age, any outside wait and the response stage.
    """
    moment = time.time() if now is None else now
    if not entry:
        return {"evidence": MISSING, "recorded": False}
    last = entry.get("last") or {}
    wait = entry.get("wait") or {}
    return {
        "recorded": True,
        "claim_id": entry["claim_id"],
        "evidence": (
            MISSING
            if not last
            else PASSING
            if last["outcome"] == PASS
            else FAILING
        ),
        "last_commit": last.get("commit", ""),
        "unverified": int(entry["unverified"]),
        "failures": int(entry["failures"]),
        "repeats": int(entry["repeats"]),
        "signature": entry["signature"],
        "milestone_at": entry["milestone"]["at"],
        "milestone_commit": entry["milestone"]["commit"],
        "since_milestone_seconds": int(age(entry, moment)),
        "waiting": wait.get("kind", ""),
        "stage": entry["stage"],
        "responses": int(entry["responses"]),
    }


def current(accounts: dict[str, dict], number: str, claim_id: object) -> dict:
    """Views one issue's account for its current claim generation.

    Args:
        accounts: Accounts from `read`.
        number: Issue number.
        claim_id: Current claim generation; an account of any other
            generation is stale and reported as absent.

    Returns:
        The `view` of the account.
    """
    entry = accounts.get(number)
    if not isinstance(entry, dict) or entry.get("claim_id") != claim_id:
        entry = None
    return view(entry)


def reading(directory: Path, number: str, claim_id: object) -> dict:
    """Reads one issue's account for its current claim generation.

    Args:
        directory: Private project state directory.
        number: Issue number.
        claim_id: Current claim generation.

    Returns:
        The `view` of the account, as `current` gives it.
    """
    return current(read(directory), number, claim_id)


def describe(shown: dict) -> str:
    """Renders an account view as one line for a terminal.

    Args:
        shown: Output of `view`.

    Returns:
        A line naming the evidence state and what decided it.
    """
    text = f"Convergence: {shown['evidence']}"
    if not shown.get("recorded"):
        return text
    if shown["failures"]:
        text += (
            f"; {shown['failures']} failing results since the milestone "
            f"{shown['since_milestone_seconds']}s ago, signature "
            f"{shown['signature']} x{shown['repeats']}"
        )
    if shown["unverified"]:
        text += f"; {shown['unverified']} reports without verification"
    if shown["waiting"]:
        text += f"; waiting on {shown['waiting']}"
    if shown["stage"]:
        text += f"; response {shown['stage']}"
    return text


def message(action: dict, notified: bool = False) -> tuple[str, str]:
    """Composes the owner's mail for one response.

    Args:
        action: Response from `observe`.
        notified: Whether an escalation's outbound operator notification
            was sent; the escalation says so only when it was.

    Returns:
        The subject and body. Neither carries evidence text, only counts,
        the failure signature digest and commits.
    """
    number = action["issue"]
    entry = action["entry"]
    milestone = entry["milestone"]
    anchor = (
        f"commit {milestone['commit'][:12]}"
        if milestone["commit"]
        else "the claim start"
    )
    account = (
        f"{entry['failures']} failing verification results on distinct "
        f"commits since the last verified milestone ({anchor}); failure "
        f"signature {entry['signature']} repeated {entry['repeats']} times. "
        "Commits, reports and reservations alone do not count as "
        "improvement; a passing gate does."
    )
    record = (
        f"Record each gate result with `agent-parley report --state partial "
        f"--issue {number} --evidence '<gate summary line>'`."
    )
    if action["stage"] == APPROACH:
        return (
            f"Issue #{number} is not converging",
            f"Issue #{number} is not converging on a verified result. "
            f"{account} Change approach: re-read the acceptance criteria, "
            "find the root cause of the repeated failure, or narrow the "
            f"change. {record} You keep ownership; nothing has moved.",
        )
    subject = f"Issue #{number} still not converging"
    operator = (
        "The operator has been notified."
        if notified
        else "No outbound notification was sent; the operator sees the "
        "issue under `not converging` in `agent-parley problems`."
    )
    return (
        f"{subject}; operator notified" if notified else subject,
        f"Issue #{number} is still not converging after the request to change "
        f"approach. {account} {operator} If another "
        "lane should take it, offer it explicitly with `agent-parley issue "
        f"offer {number} --to NAME --summary TEXT`; the operator can reassign "
        f"it with `agent-parley issue assign {number} NAME`. Nothing moves on "
        "its own, and no further convergence notice follows until a verified "
        "improvement or a new claim.",
    )


def supervise(
    home: Path, directory: Path, manifest: dict, config: dict
) -> None:
    """Runs one convergence pass and sends the responses it decided.

    Each response is sent once, as operator mail with a key naming the
    issue, generation, milestone and stage, so a retried pass cannot send it
    twice. An escalation first notifies the operator through the configured
    outbound transports, and its mail says whether that happened. A store or
    transport failure loses that one send and is never retried by a later
    pass. The accounts are recorded on every pass; with prompts disabled no
    response is decided or sent.

    Args:
        home: Private bridge state root.
        directory: Private project state directory.
        manifest: Current participant manifest.
        config: Resolved supervision settings.
    """
    from agent_parley import notify, store

    actions = observe(
        directory,
        manifest,
        int(config["convergence_repeats"]),
        float(config["convergence_after"]),
        respond=bool(config.get("prompts", True)),
    )
    for action in actions:
        participant = manifest["participants"][action["owner"]]
        milestone = int(action["entry"]["milestone"]["at"])
        notified = False
        if action["stage"] == ESCALATED:
            try:
                notified = bool(
                    notify.deliver(
                        directory,
                        action["owner"],
                        notify.Event.NON_CONVERGENCE,
                        {
                            "repo": manifest["root"],
                            "issue": action["issue"],
                            "claim": action["claim_id"],
                            "milestone": milestone,
                            "detail": (
                                f"{action['entry']['failures']} failing "
                                "results, signature "
                                f"{action['entry']['signature']}"
                            ),
                        },
                    )
                )
            except BridgeError as exc:
                issues.note_supervision_error(directory, f"Notification: {exc}")
        subject, body = message(action, notified)
        key = (
            f"convergence:{action['issue']}:{action['claim_id']}:"
            f"{milestone}:{action['stage']}"
        )
        with contextlib.suppress(BridgeError, OSError, sqlite3.Error):
            store.speak(
                home,
                manifest["root"],
                participant["display"],
                subject,
                body,
                key,
            )
