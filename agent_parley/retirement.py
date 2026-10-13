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
claimable by any lane. Ready work is the exception: it must stay claimed until
verified integration completes, so a lane holding any is refused before a
single issue is released, rather than left half retired.

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

from agent_parley import issues, lifecycle, roster
from agent_parley.state import BridgeError, lock, write_json

GIT_SECONDS = 30
LOCK_SECONDS = 1.0
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


def ignored_files(lane: str) -> list[str] | None:
    """Lists the files inside a worktree that Git tracks nowhere else.

    Args:
        lane: Worktree the listing is read from.

    Returns:
        Sorted worktree-relative paths Git ignores, empty when there are
        none, or None when Git could not inspect the worktree. These files
        are never staged and `git status` never reports them, so removing
        the worktree would delete them with no other copy anywhere.
    """
    readable, listing = _git(
        lane,
        "ls-files",
        "--others",
        "--ignored",
        "--exclude-standard",
        "--directory",
    )
    if not readable:
        return None
    return sorted(line.rstrip("/") for line in listing.splitlines() if line)


def _prune(root: str, lane: Path) -> dict:
    """Removes a retiring lane's worktree when Git reports it clean.

    Args:
        root: Base checkout the worktree is linked to.
        lane: Assigned bridge worktree of the retiring lane.

    Returns:
        The state of the worktree as `PRUNED`, `KEPT` or `GONE`, and the
        repository-relative paths that kept it, empty when none did.
        Ignored files keep it too: `git status` never lists them, and
        `git worktree remove` deletes them, so a lane's `.env` or local
        build output would otherwise vanish without a word. A worktree Git
        could not inspect or could not remove is kept, so a retirement
        never destroys an uninspected checkout.
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
    ignored = ignored_files(str(lane))
    if ignored is None:
        return {"worktree": KEPT, "dirty": []}
    if ignored:
        return {"worktree": KEPT, "dirty": ignored}
    removed, _ = _git(root, "worktree", "remove", str(lane))
    _git(root, "worktree", "prune")
    return {"worktree": PRUNED if removed else KEPT, "dirty": []}


def _awaits(record: dict) -> bool:
    """Reports whether a claim is ready work still awaiting integration.

    Ready work whose issue closed on the forge inside the claim's generation
    has nothing left to integrate, so it may be released like any other
    claim, as `lifecycle.released` allows.

    Args:
        record: Published ledger record for one issue.

    Returns:
        Whether the claim is ready and its issue has not closed.
    """
    return lifecycle.state(record)[
        "state"
    ] == lifecycle.READY and not issues.closed(record)


def return_work(directory: Path, manifest: dict, name: str) -> dict:
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

    Raises:
        BridgeError: If the lane holds ready work, which must stay claimed
            until verified integration completes, or a claim blocked on an
            operator action, which `lifecycle.operator_blocked` keeps with
            the lane that recorded the block. Every held issue is checked
            before any is released, so a refusal leaves the lane's claims as
            they were rather than half returned.
    """
    ledger = issues.snapshot(directory)
    held = issues.holders(ledger).get(name, [])
    kept = [
        number
        for number in held
        if _awaits(ledger["issues"][number])
        or lifecycle.operator_blocked(ledger["issues"][number])
    ]
    if kept:
        listed = ", ".join(f"#{number}" for number in kept)
        raise BridgeError(
            f"{name} holds ready or operator-blocked work ({listed}) that "
            "must stay claimed; land ready work with agent-parley "
            f"participant merge {name}, hand it on with agent-parley issue "
            "offer, or release blocked work on purpose with agent-parley "
            "issue release, before retiring."
        )
    participants = set(manifest["participants"])
    released: list[str] = []
    declined: list[str] = []
    senders: dict[str, list[str]] = {}
    for number in held:
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

    The whole withdrawal holds the lane's checkpoint lock, the lock a launch
    takes before it records a session start. A restart or launch racing the
    retirement therefore either finishes recording its start first, or waits
    and then finds the participant retired and refuses, rather than starting
    a session in a worktree removed a moment later. The lane's session lock
    is not the one taken, because the retiring lane calls this from inside
    its own session, whose launcher holds that lock until the session ends.

    Returns:
        What the retirement did: the issues released and the handoffs
        declined, the lanes that had handed this one work, the state of the
        worktree with any paths that kept it, and the time recorded. A lane
        already retired reports its recorded time and changes nothing.

    Raises:
        BridgeError: If the manifest does not hold that participant.
        LockBusy: If another lane command holds the lane's checkpoint lock
            past `LOCK_SECONDS`; nothing was released, removed or recorded.
    """
    with lock(
        directory / f"{name}-checkpoint.lock",
        f"{name} is busy with another lane command; nothing was retired, "
        "so retry the retirement.",
        timeout=LOCK_SECONDS,
    ):
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
        work = return_work(directory, manifest, name)
        worktree = _prune(manifest["root"], Path(participant["lane"]))
        at = time.time()
        mark(directory, name, at)
        (directory / f"{name}-identity.json").unlink(missing_ok=True)
        return {"participant": name, "retired_at": at, **work, **worktree}


def abandon(directory: Path, name: str, cap: int | None = None) -> dict:
    """Returns the work a lane supervision proved dead still holds.

    A dead lane cannot retire itself and never answers, so everything that
    waits on it waits for nothing. Supervision calls this once the lane is
    proved dead, and the lane's own transitions are applied on its behalf,
    as a retirement would apply them, but the lane is not retired: its
    worktree, branch, credential and roster entry stay, so an operator can
    still resume it, and a resumed lane finds its claims in the pool. A lane
    whose headless session supervision ended at a prompt nobody answered
    returns its work the same way.

    Offers made to the lane are declined back to their senders. Each claim
    it holds is released to the pool, and a claim a live peer asked to take
    over is then claimed for that peer. A claim with a pending offer is kept,
    because the recipient's acceptance moves it without the dead lane, and
    ready work whose issue has not closed is kept, because it must stay
    claimed until verified integration completes. Requests the lane made are
    withdrawn, and every completion reminder addressed to it is stamped
    answered by the supervisor, so no peer is shown a reminder the lane will
    never answer. Nothing in the worktree is touched.

    Args:
        directory: Private state directory for the common repository.
        name: Participant proved dead.
        cap: Most claims one lane may hold, applied to a requesting peer.

    Returns:
        The issues released, the offers declined, the claims kept with the
        lane, and the issues handed to the peer that requested them.

    Raises:
        LockBusy: If another lane command holds the lane's checkpoint lock
            past `LOCK_SECONDS`; nothing was changed.
    """
    report: dict = {"released": [], "declined": [], "kept": [], "given": {}}
    with lock(directory / f"{name}-checkpoint.lock", timeout=LOCK_SECONDS):
        manifest = roster.read(directory)
        participant = manifest["participants"].get(name)
        if participant is None or roster.retired(participant):
            return report
        participants = set(manifest["participants"])
        ledger = issues.snapshot(directory)
        for number in sorted(ledger["issues"], key=int):
            offer = ledger["issues"][number].get("offer") or {}
            if offer.get("to") != name or not offer.get("id"):
                continue
            try:
                issues.change(
                    directory,
                    name,
                    "decline",
                    number,
                    participants=participants,
                    offer_id=str(offer["id"]),
                )
            except BridgeError:
                continue
            report["declined"].append(number)
        ledger = issues.snapshot(directory)
        for number in issues.holders(ledger).get(name, []):
            record = ledger["issues"][number]
            if record.get("offer") or _awaits(record):
                report["kept"].append(number)
                continue
            request = record.get("request") or {}
            peer = (
                str(request.get("to") or "")
                if issues.offer_source(request) == issues.PEER
                else ""
            )
            try:
                issues.change(
                    directory,
                    name,
                    "release",
                    number,
                    participants=participants,
                )
            except BridgeError:
                report["kept"].append(number)
                continue
            report["released"].append(number)
            if peer in participants - {name} and not issues.closed(record):
                try:
                    issues.change(
                        directory,
                        peer,
                        "claim",
                        number,
                        participants=participants,
                        cap=cap,
                    )
                except BridgeError:
                    continue
                report["given"][number] = peer
        _silence(directory, name)
    return report


def _silence(directory: Path, name: str) -> None:
    """Withdraws a dead lane's requests and answers its reminders.

    Args:
        directory: Private state directory for the common repository.
        name: Participant proved dead.
    """
    now = time.time()
    with lock(directory / "issues.lock", timeout=LOCK_SECONDS):
        ledger = issues.snapshot(directory)
        changed = False
        for record in ledger["issues"].values():
            request = record.get("request") or {}
            if request.get("to") == name:
                record["request"] = None
                lifecycle.append_history(
                    record,
                    {
                        "action": "withdraw",
                        "actor": "supervisor",
                        "at": now,
                        "owner": record.get("owner"),
                        "offer": record.get("offer"),
                        "request": None,
                        "offer_id": request.get("id"),
                        "claim_id": record.get("claim_id"),
                    },
                )
                changed = True
            prompt = record.get("handoff_prompt") or {}
            if prompt.get("holder") == name and not prompt.get("responded_at"):
                prompt.update(responded_at=now, answered_by="supervisor")
                changed = True
        if changed:
            ledger["revision"] += 1
            write_json(directory / "issues.json", ledger)
