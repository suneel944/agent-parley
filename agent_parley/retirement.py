"""Withdrawal of one lane from a project at that lane's own request.

A lane that has finished, or that a peer has asked to stand down, used to have
nowhere to go: going idle reads as a stall and is woken again, and the only
retirement was an operator command. Retirement is therefore a transition the
lane itself can make, and it is ordered so that nothing it held is stranded if
a later step fails.

Work leaves first. Every issue the lane still owns is released back to the
pool, and every handoff offered to it is declined so the peer that offered it
owns it again rather than waiting on a lane that is gone. Work is released
rather than offered back to its sender, because an offer leaves ownership on
the offering lane until the recipient answers, and a retired lane can answer
nothing; the sender is told by mail instead, and the issue is immediately
claimable by any lane.

The worktree leaves next, and only when Git reports it clean. A lane with
uncommitted changes keeps its worktree and reports the paths, because
retirement never discards work. A checkout Git cannot inspect is treated the
same way: no opinion is not a clean reading.

The manifest mark is last, and it is what makes the retirement durable. The
participant stays in the roster carrying the time it retired, so status can
report it and the supervisor can skip it, and the operator re-admits it with
the same `participant add` command that created it.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from agent_parley import issues, roster
from agent_parley.state import BridgeError, lock, write_json

GIT_SECONDS = 30
KEPT = "kept"
PRUNED = "pruned"
GONE = "absent"


def _git(cwd: str, *arguments: str) -> tuple[bool, str]:
    """Runs one bounded Git command for a retirement step.

    Args:
        cwd: Checkout the command runs in.
        *arguments: Git arguments following the checkout selection.

    Returns:
        Whether Git succeeded, and its standard output exactly as Git wrote
        it: a porcelain status encodes its state in the first two columns of
        every line, so trimming the output would shift the path of any entry
        whose first column is blank. A command Git refused, could not run, or
        did not finish inside the timeout reports failure with no output
        rather than an answer that was never given.
    """
    try:
        result = subprocess.run(
            ["git", "-C", cwd, *arguments],
            capture_output=True,
            text=True,
            timeout=GIT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, ""
    return result.returncode == 0, result.stdout


def _prune(root: str, lane: Path) -> dict:
    """Removes a retiring lane's worktree when Git reports it clean.

    Args:
        root: Base checkout the worktree is linked to.
        lane: Assigned bridge worktree of the retiring lane.

    Returns:
        The state of the worktree as `PRUNED`, `KEPT` or `GONE`, and the
        repository-relative paths that kept it, empty when none did. A
        worktree Git could not inspect or could not remove is kept, so a
        retirement never destroys an uninspected checkout.
    """
    if not lane.exists():
        _git(root, "worktree", "prune")
        return {"worktree": GONE, "dirty": []}
    readable, listing = _git(str(lane), "status", "--porcelain", "-uall")
    if not readable:
        return {"worktree": KEPT, "dirty": []}
    dirty = []
    for line in listing.splitlines():
        if len(line) < 4:
            continue
        entry = line[3:]
        if line[0] in "RC" and " -> " in entry:
            entry = entry.split(" -> ", 1)[1]
        dirty.append(entry.rstrip("/"))
    if dirty:
        return {"worktree": KEPT, "dirty": sorted(dirty)}
    removed, _ = _git(root, "worktree", "remove", str(lane))
    _git(root, "worktree", "prune")
    return {"worktree": PRUNED if removed else KEPT, "dirty": []}


def _return_work(directory: Path, manifest: dict, name: str) -> dict:
    """Returns every piece of work a retiring lane holds or was offered.

    Args:
        directory: Private state directory for the common repository.
        manifest: Current project manifest.
        name: Retiring participant.

    Returns:
        The issue numbers released back to the pool, the numbers of handoffs
        declined back to their offering lanes, and the lanes that had handed
        this one work, each with the numbers they sent, so they can be told
        where that work went.
    """
    ledger = issues.snapshot(directory)
    participants = set(manifest["participants"])
    released: list[str] = []
    declined: list[str] = []
    senders: dict[str, list[str]] = {}
    for number in issues.holders(ledger).get(name, []):
        record = ledger["issues"][number]
        sender = str((record.get("handoff") or {}).get("from") or "")
        issues.change(
            directory, name, "release", number, participants=participants
        )
        released.append(number)
        if sender and sender != name and sender in participants:
            senders.setdefault(sender, []).append(number)
    for number in sorted(ledger.get("issues", {}), key=int):
        offer = ledger["issues"][number].get("offer") or {}
        if offer.get("to") != name or not offer.get("id"):
            continue
        issues.change(
            directory,
            name,
            "decline",
            number,
            participants=participants,
            offer_id=str(offer["id"]),
        )
        declined.append(number)
    return {"released": released, "declined": declined, "senders": senders}


def mark(directory: Path, name: str, at: float) -> None:
    """Records the time a participant retired, in the project manifest.

    Args:
        directory: Private state directory for the common repository.
        name: Participant that retired.
        at: Epoch seconds the retirement was recorded at.

    Raises:
        BridgeError: If the manifest no longer holds that participant.
    """
    with lock(directory / "setup.lock"):
        manifest = roster.read(directory)
        participant = manifest["participants"].get(name)
        if participant is None:
            raise BridgeError(f"{name} is not a participant in this project.")
        participant["retired"] = float(at)
        write_json(directory / "project.json", manifest)


def withdraw(directory: Path, name: str) -> dict:
    """Retires one lane, returning its work before it loses its lane.

    Args:
        directory: Private state directory for the common repository.
        name: Participant retiring itself.

    Returns:
        What the retirement did: the issues released and the handoffs
        declined, the lanes that had handed this one work, the state of the
        worktree with any paths that kept it, and the time recorded. A lane
        already retired reports its recorded time and changes nothing.

    Raises:
        BridgeError: If the manifest does not hold that participant.
    """
    manifest = roster.read(directory)
    participant = manifest["participants"].get(name)
    if participant is None:
        raise BridgeError(f"{name} is not a participant in this project.")
    if roster.retired(participant):
        return {
            "participant": name,
            "retired_at": float(participant["retired"]),
            "released": [],
            "declined": [],
            "senders": {},
            "worktree": KEPT,
            "dirty": [],
        }
    work = _return_work(directory, manifest, name)
    worktree = _prune(manifest["root"], Path(participant["lane"]))
    at = time.time()
    mark(directory, name, at)
    (directory / f"{name}-identity.json").unlink(missing_ok=True)
    return {"participant": name, "retired_at": at, **work, **worktree}
