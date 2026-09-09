"""Local, repository-scoped issue ownership and explicit handoffs."""

import json
import re
import time
import uuid
from pathlib import Path

from agent_parley.state import BridgeError, lock, write_json


def snapshot(directory: Path) -> dict:
    """Returns the published issue ledger, or an empty ledger."""
    path = directory / "issues.json"
    return (
        json.loads(path.read_text())
        if path.exists()
        else {"revision": 0, "issues": {}}
    )


def change(
    directory: Path,
    agent: str,
    action: str,
    issue: str,
    *,
    participants: set[str],
    to: str | None = None,
    summary: str = "",
    offer_id: str | None = None,
) -> dict:
    """Applies one issue transition while holding the repository lock.

    Args:
        directory: Private state directory for the common repository.
        agent: Acting lane, resolved by the CLI from its worktree.
        action: Claim, release, offer, accept, decline, or cancel.
        issue: Positive repository issue number, optionally prefixed with #.
        participants: Every participant registered for this project.
        to: Recipient participant for an offer.
        summary: Peer-provided handoff context.
        offer_id: Exact current offer required for acceptance or decline.

    Returns:
        The persisted issue record, including transition history.

    Raises:
        BridgeError: If validation, ownership, offer, or lock checks fail.
    """
    if not re.fullmatch(r"#?[1-9][0-9]{0,17}", issue):
        raise BridgeError(
            "Issue must be a positive number of up to 18 digits, "
            "e.g. 432 or #432."
        )
    issue = issue.lstrip("#")
    with lock(directory / "issues.lock"):
        state = snapshot(directory)
        record = state["issues"].get(issue)
        if action == "claim":
            if record and record["owner"]:
                if record["owner"] == agent:
                    return record
                raise BridgeError(
                    f"Issue #{issue} is owned by {record['owner']}."
                )
            record = {
                "owner": agent,
                "offer": None,
                "history": (record or {}).get("history", []),
            }
        else:
            if not record or not record["owner"]:
                raise BridgeError(f"Issue #{issue} has no owner.")
            if action in ("accept", "decline"):
                offer = record["offer"]
                if not offer or offer["id"] != offer_id:
                    raise BridgeError(
                        "Handoff changed or was cancelled; inspect issue list."
                    )
                if offer["to"] != agent:
                    raise BridgeError(
                        "Only the named recipient can answer this handoff."
                    )
                if action == "accept":
                    record["owner"] = agent
                record["offer"] = None
            else:
                if record["owner"] != agent:
                    raise BridgeError(
                        f"Only {record['owner']} can change issue #{issue}."
                    )
                if action == "offer":
                    recipient = to
                    summary = summary.strip()
                    if recipient not in participants or recipient == agent:
                        raise BridgeError(
                            "Choose another participant in this project; "
                            "run agent-parley participant list."
                        )
                    if not summary or len(summary) > 2000:
                        raise BridgeError(
                            "Handoff summary must contain 1–2000 characters."
                        )
                    if record["offer"]:
                        raise BridgeError(
                            "A handoff is pending; "
                            "cancel it before replacing it."
                        )
                    record["offer"] = {
                        "id": uuid.uuid4().hex,
                        "to": recipient,
                        "summary": summary,
                        "created": time.time(),
                    }
                elif action == "cancel":
                    if not record["offer"]:
                        raise BridgeError("No handoff is pending.")
                    record["offer"] = None
                elif action == "release":
                    record.update(owner=None, offer=None)
                else:
                    raise BridgeError("Unknown issue action.")
        record["history"].append(
            {
                "action": action,
                "actor": agent,
                "at": time.time(),
                "owner": record["owner"],
                "offer": record["offer"],
                "offer_id": offer_id,
            }
        )
        state["issues"][issue] = record
        state["revision"] += 1
        write_json(directory / "issues.json", state)
        return record


def describe(state: dict, liveness: dict[str, str] | None = None) -> str:
    """Formats active ownership and pending offers without changing state.

    Args:
        state: Published issue ledger.
        liveness: Optional session state per participant, reported beside the
            owner. Liveness is information for an operator; silence, idleness,
            and a stopped session never transfer ownership.

    Returns:
        One line for each owned issue, or a notice that none are claimed.
    """
    lines = []
    for number, record in sorted(
        state["issues"].items(), key=lambda item: int(item[0])
    ):
        if not record["owner"]:
            continue
        line = f"#{number}: {record['owner']}"
        if liveness and record["owner"] in liveness:
            line += f" ({liveness[record['owner']]})"
        if offer := record["offer"]:
            age = max(0, int(time.time() - offer["created"]))
            line += (
                f"; handoff to {offer['to']} pending {age}s; "
                f"offer {offer['id']}"
            )
            line += "\n  Peer-provided summary: " + json.dumps(offer["summary"])
        lines.append(line)
    return "\n".join(lines) or "No issues claimed."
