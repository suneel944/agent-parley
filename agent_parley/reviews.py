"""Review rounds a builder lane requests without giving up its claim.

A builder that wants a second pair of eyes had only a handoff, which moves
ownership, or mail to a peer, which records nothing. Mail sent to `ADDRESS`
with the branch as its subject and the paths as the remaining recipients
records a round here and offers it to an idle lane, preferring one on a
different provider than the builder. Different-provider is a ranking input,
not a requirement. The reviewer answers with mail to `ADDRESS` and the round
identifier, and its one findings report, capped at `REPORT_BYTES` like a
child report, is mailed back to the builder. The builder never reads the
reviewer's transcript, and the claim never leaves the builder: a round
writes nothing to the issue ledger.

A claim gets at most `MAX_ROUNDS` rounds, and only one open at a time. The
cap is fixed rather than configurable, because returning findings to the
same builder only pays while rounds converge; past it the builder opens the
pull request or asks the operator.

Rounds live in `RECORD_NAME` beside the issue ledger, under `LOCK_NAME`, and
hold no credential. Request and findings text is peer content.
"""

import json
import time
from pathlib import Path

from agent_parley.children import REPORT_BYTES
from agent_parley.state import BridgeError, lock, write_json

ADDRESS = "@review"
TOOL = "review_round"
RECORD_NAME = "reviews.json"
LOCK_NAME = "reviews.lock"
MAX_ROUNDS = 3
MAX_BRANCH = 100
MAX_PATHS = 15
MAX_PATH = 300
OPEN = "open"
RETURNED = "returned"
LOCK_SECONDS = 5.0
BULLETS = ("- ", "* ", "+ ")


def _read(directory: Path) -> dict:
    """Reads the round record, treating a missing or damaged file as empty.

    Args:
        directory: Private project state directory.

    Returns:
        The next identifier counter and the rounds keyed by identifier.
    """
    try:
        value = json.loads((directory / RECORD_NAME).read_text())
    except (OSError, ValueError):
        value = {}
    if not isinstance(value, dict) or not isinstance(value.get("rounds"), dict):
        value = {"next": 1, "rounds": {}}
    if not isinstance(value.get("next"), int):
        value["next"] = len(value["rounds"]) + 1
    return value


def _paths(value: object) -> list[str]:
    """Validates the repository paths a round names.

    Args:
        value: Recipients after the address.

    Returns:
        The paths as strings.

    Raises:
        BridgeError: If there are too many paths or one is not a short
            non-empty string.
    """
    if not isinstance(value, list | tuple) or len(value) > MAX_PATHS:
        raise BridgeError(f"A review round names at most {MAX_PATHS} paths.")
    listed = []
    for path in value:
        if not isinstance(path, str) or not path.strip():
            raise BridgeError("Each review path must be a non-empty string.")
        if len(path) > MAX_PATH:
            raise BridgeError(f"A review path is longer than {MAX_PATH}.")
        listed.append(path.strip())
    return listed


def count(findings: str) -> int:
    """Counts the findings in a report, one per bulleted or numbered line.

    Args:
        findings: Report text the reviewer returned.

    Returns:
        The number of lines that start a list item.
    """
    total = 0
    for line in findings.splitlines():
        stripped = line.lstrip()
        head, _, _ = stripped.partition(". ")
        if stripped.startswith(BULLETS) or (head.isdigit() and head):
            total += 1
    return total


def cap(text: str) -> str:
    """Cuts a report to `REPORT_BYTES` on a character boundary.

    Args:
        text: Report text.

    Returns:
        The text, cut where its UTF-8 encoding would pass the cap.
    """
    return text.encode()[:REPORT_BYTES].decode(errors="ignore")


def idle_lanes(directory: Path, manifest: dict) -> list[str]:
    """Lists running lanes that hold no claim and have no offer waiting.

    This is the reading ``followups.idle_lane`` takes its first entry from,
    kept whole so a round can rank every idle lane by provider.

    Args:
        directory: Private project state directory.
        manifest: Project manifest naming the participants.

    Returns:
        Display names of such lanes, in participant name order.
    """
    from agent_parley import checkpoints, issues, supervision

    ledger = issues.snapshot(directory)
    busy = set(issues.holders(ledger))
    busy |= {
        str((record.get("offer") or {}).get("to") or "")
        for record in ledger.get("issues", {}).values()
    }
    idle = []
    for name, entry in sorted(manifest.get("participants", {}).items()):
        display = str(entry.get("display") or name)
        if name in busy or display in busy:
            continue
        derived = supervision.lane_state(checkpoints.activity(directory, name))
        if derived.get("process_alive"):
            idle.append(display)
    return idle


def providers(manifest: dict) -> dict[str, str]:
    """Maps each lane's display name to the provider driving it.

    Args:
        manifest: Project manifest naming the participants.

    Returns:
        Display name to provider name.
    """
    return {
        str(entry.get("display") or name): str(entry.get("provider", ""))
        for name, entry in manifest.get("participants", {}).items()
    }


def _ranked(
    candidates: list[str], providers: dict[str, str], builder: str
) -> str:
    """Picks a reviewer, preferring a provider other than the builder's.

    Args:
        candidates: Idle lanes, in name order.
        providers: Provider of each lane.
        builder: Lane that requested the round.

    Returns:
        The first idle lane on a different provider, else the first idle
        lane, else an empty string.
    """
    own = providers.get(builder, "")
    others = [name for name in candidates if providers.get(name, "") != own]
    return (others or candidates or [""])[0]


def request(
    directory: Path,
    *,
    builder: str,
    issue: str,
    claim: str,
    branch: object,
    paths: object,
    candidates: list[str],
    providers: dict[str, str],
    key: str = "",
    now: float = 0.0,
) -> dict:
    """Records one review round for the builder's claim.

    A request repeating the idempotency key of the claim's open round gets
    that round back, so a retried send does not queue a second round.

    Args:
        directory: Private project state directory.
        builder: Lane that holds the claim and asks for the review.
        issue: Issue the claim is on.
        claim: Claim identifier the round belongs to.
        branch: Branch to review, from the mail subject.
        paths: Paths to review, from the recipients after the address.
        candidates: Idle lanes that could review, in name order.
        providers: Provider of each lane, for the ranking.
        key: Idempotency key of the request mail.
        now: Unix time; the clock when zero.

    Returns:
        The round, with ``repeated`` true when it was already recorded.

    Raises:
        BridgeError: If the branch or paths are invalid, the claim already
            has an open round or `MAX_ROUNDS` rounds, or no lane can review.
    """
    if not isinstance(branch, str) or not branch.strip():
        raise BridgeError("Put the branch to review in the subject.")
    if len(branch) > MAX_BRANCH:
        raise BridgeError(f"The branch is longer than {MAX_BRANCH}.")
    listed = _paths(paths)
    with lock(directory / LOCK_NAME, timeout=LOCK_SECONDS):
        record = _read(directory)
        held = [
            entry
            for entry in record["rounds"].values()
            if entry.get("claim_id") == claim
        ]
        waiting = [entry for entry in held if entry.get("state") == OPEN]
        if waiting and key and waiting[0].get("key") == key:
            return {**waiting[0], "repeated": True}
        if waiting:
            raise BridgeError(
                f"Review round {waiting[0]['id']} for #{issue} is still open "
                f"with {waiting[0]['reviewer']}; wait for its findings."
            )
        if len(held) >= MAX_ROUNDS:
            raise BridgeError(
                f"#{issue} already had {MAX_ROUNDS} review rounds, the cap "
                "per claim; open the pull request or ask the operator."
            )
        busy = {
            entry.get("reviewer")
            for entry in record["rounds"].values()
            if entry.get("state") == OPEN
        }
        reviewer = _ranked(
            [
                name
                for name in candidates
                if name != builder and name not in busy
            ],
            providers,
            builder,
        )
        if not reviewer:
            raise BridgeError(
                "No idle lane can review now; nothing was recorded. Ask "
                "again later or open the pull request."
            )
        ident = f"r{record['next']}"
        record["next"] += 1
        entry = {
            "id": ident,
            "issue": issue,
            "claim_id": claim,
            "round": len(held) + 1,
            "builder": builder,
            "reviewer": reviewer,
            "builder_provider": providers.get(builder, ""),
            "reviewer_provider": providers.get(reviewer, ""),
            "branch": branch.strip(),
            "paths": listed,
            "key": key,
            "state": OPEN,
            "requested": now or time.time(),
            "returned": 0.0,
            "findings": 0,
        }
        record["rounds"][ident] = entry
        write_json(directory / RECORD_NAME, record)
    return {**entry, "repeated": False}


def withdraw(directory: Path, ident: str) -> None:
    """Drops an open round whose offer mail could not be sent.

    Args:
        directory: Private project state directory.
        ident: Round identifier.
    """
    with lock(directory / LOCK_NAME, timeout=LOCK_SECONDS):
        record = _read(directory)
        entry = record["rounds"].get(ident)
        if entry and entry.get("state") == OPEN:
            del record["rounds"][ident]
            write_json(directory / RECORD_NAME, record)


def pending(directory: Path, ident: object, reviewer: str) -> dict:
    """Returns the open round a reviewer may answer.

    Args:
        directory: Private project state directory.
        ident: Round identifier from the recipients.
        reviewer: Lane sending the findings.

    Returns:
        The open round.

    Raises:
        BridgeError: If the round is unknown, already answered or offered
            to another lane.
    """
    entry = _read(directory)["rounds"].get(str(ident))
    if entry is None:
        raise BridgeError(f"No review round {ident} is recorded.")
    if entry.get("reviewer") != reviewer:
        raise BridgeError(
            f"Review round {ident} was offered to {entry.get('reviewer')}."
        )
    if entry.get("state") != OPEN:
        raise BridgeError(f"Review round {ident} already returned findings.")
    return entry


def returned(
    directory: Path, ident: str, reviewer: str, findings: str, now: float = 0.0
) -> dict:
    """Records the findings a reviewer returned on its open round.

    Args:
        directory: Private project state directory.
        ident: Round identifier.
        reviewer: Lane that returned the findings.
        findings: Capped report text, delivered to the builder.
        now: Unix time; the clock when zero.

    Returns:
        The answered round.

    Raises:
        BridgeError: If the round cannot be answered by this lane.
    """
    with lock(directory / LOCK_NAME, timeout=LOCK_SECONDS):
        pending(directory, ident, reviewer)
        record = _read(directory)
        entry = record["rounds"][ident]
        entry["state"] = RETURNED
        entry["returned"] = now or time.time()
        entry["findings"] = count(findings)
        write_json(directory / RECORD_NAME, record)
    return entry


def rounds(directory: Path, *, issue: str = "", lane: str = "") -> list[dict]:
    """Lists recorded rounds, oldest first.

    Args:
        directory: Private project state directory.
        issue: Keep only rounds on this issue, when given.
        lane: Keep only rounds this lane built or reviewed, when given.

    Returns:
        The matching rounds.
    """
    rows = sorted(
        _read(directory)["rounds"].values(),
        key=lambda entry: int(str(entry.get("id", "r0"))[1:] or 0),
    )
    return [
        entry
        for entry in rows
        if (not issue or entry.get("issue") == issue)
        and (not lane or lane in (entry.get("builder"), entry.get("reviewer")))
    ]


def lines(rows: list[dict]) -> list[str]:
    """Formats rounds for ``issue show`` and ``participant show``.

    Args:
        rows: Rounds `rounds` returned.

    Returns:
        One line per round, or nothing when there are none.
    """
    shown = []
    for entry in rows:
        result = (
            f"{entry['findings']} findings"
            if entry.get("state") == RETURNED
            else "open"
        )
        shown.append(
            f"Review {entry['id']}: #{entry['issue']} round "
            f"{entry['round']}/{MAX_ROUNDS}, {entry['builder']} reviewed by "
            f"{entry['reviewer']} on {entry['branch']}; {result}"
        )
    return shown


def offer_text(entry: dict, note: str) -> tuple[str, str]:
    """Builds the mail that offers a round to its reviewer.

    Args:
        entry: Recorded round.
        note: Builder's request text.

    Returns:
        The subject and body.
    """
    subject = (
        f"Review round {entry['id']} ({entry['round']} of {MAX_ROUNDS}) "
        f"for #{entry['issue']}: {entry['branch']}"
    )
    listed = "".join(f"\n- `{path}`" for path in entry["paths"])
    body = (
        f"{entry['builder']} asks for a review of `{entry['branch']}`; the "
        "claim stays with the builder.\n\n"
        + (f"{note.strip()}\n\n" if note.strip() else "")
        + (f"Paths:{listed}\n\n" if listed else "")
        + "Return one findings report, at most "
        f"{REPORT_BYTES} bytes, one finding per bullet line, with "
        f'send_message to ["{ADDRESS}", "{entry["id"]}"].'
    )
    return subject, body


def findings_subject(entry: dict, total: int) -> str:
    """Names the findings mail sent back to the builder.

    Args:
        entry: Round being answered.
        total: Finding count.

    Returns:
        The subject line.
    """
    return (
        f"Review round {entry['id']} findings for #{entry['issue']}: "
        f"{total} from {entry['reviewer']}"
    )
