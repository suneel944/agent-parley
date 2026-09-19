"""Captures and restores durable work for an abandoned issue claim."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

from agent_parley import process
from agent_parley.state import BridgeError, lock, write_json

GIT_SECONDS = 30
MAX_STEP_BYTES = 400
RECOVERY_FOLDER = "recovery"


def _git(
    lane: Path,
    *args: str,
    env: dict[str, str] | None = None,
    input_data: bytes | None = None,
) -> bytes:
    """Runs one bounded Git operation in a lane.

    Args:
        lane: Assigned worktree used for the operation.
        *args: Git arguments, passed without shell expansion.
        env: Optional environment additions.
        input_data: Optional bytes supplied on standard input.

    Returns:
        Standard output bytes.

    Raises:
        BridgeError: If Git exits unsuccessfully.
    """
    environment = dict(os.environ)
    environment.update(env or {})
    try:
        result = subprocess.run(
            ["git", "-C", str(lane), *args],
            input=input_data,
            capture_output=True,
            timeout=GIT_SECONDS,
            check=False,
            env=environment,
        )
    except subprocess.TimeoutExpired as exc:
        raise BridgeError(
            f"Recovery Git operation timed out after {exc.timeout}s."
        ) from None
    if result.returncode:
        detail = result.stderr.decode(errors="replace").strip()
        raise BridgeError(detail or "Recovery Git operation failed.")
    return result.stdout


def _text(lane: Path, *args: str, env: dict[str, str] | None = None) -> str:
    """Runs Git and returns stripped text output."""
    return _git(lane, *args, env=env).decode(errors="replace").strip()


def _folder(directory: Path) -> Path:
    """Returns the private folder holding recovery records and bundles."""
    folder = directory / RECOVERY_FOLDER
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    folder.chmod(0o700)
    return folder


def _digest(path: Path) -> str:
    """Returns a streaming SHA-256 digest for a recovery artifact."""
    digest = hashlib.sha256()
    with path.open("rb") as artifact:
        while chunk := artifact.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _identifier(issue: str, claim_id: str) -> str:
    """Returns a path-safe identifier for one ownership generation."""
    if not re.fullmatch(r"[1-9][0-9]{0,17}", issue):
        raise BridgeError("Recovery issue must be a positive issue number.")
    if not re.fullmatch(r"[0-9a-f]{16}", claim_id):
        raise BridgeError("Recovery claim generation is invalid.")
    return f"issue-{issue}-{claim_id}"


def _commit(
    lane: Path,
    tree: str,
    parent: str,
    message: str,
) -> str:
    """Creates an unreachable checkpoint commit without changing the lane."""
    identity = {
        "GIT_AUTHOR_NAME": "Agent Parley",
        "GIT_AUTHOR_EMAIL": "recovery@localhost",
        "GIT_AUTHOR_DATE": "@0 +0000",
        "GIT_COMMITTER_NAME": "Agent Parley",
        "GIT_COMMITTER_EMAIL": "recovery@localhost",
        "GIT_COMMITTER_DATE": "@0 +0000",
    }
    return (
        _git(
            lane,
            "commit-tree",
            tree,
            "-p",
            parent,
            "-m",
            message,
            env=identity,
        )
        .decode()
        .strip()
    )


def _snapshot_commits(lane: Path, temporary_index: Path) -> tuple[str, str]:
    """Builds commits for the lane index and complete working content."""
    head = _text(lane, "rev-parse", "HEAD")
    index_tree = _text(lane, "write-tree")
    index_commit = _commit(lane, index_tree, head, "Recovery index")
    environment = {"GIT_INDEX_FILE": str(temporary_index)}
    _git(lane, "read-tree", index_tree, env=environment)
    _git(lane, "add", "-A", env=environment)
    worktree = _text(lane, "write-tree", env=environment)
    worktree_commit = _commit(lane, worktree, index_commit, "Recovery worktree")
    return index_commit, worktree_commit


def _content_trees(lane: Path) -> tuple[str, str]:
    """Returns trees representing the current index and visible worktree."""
    index_tree = _text(lane, "write-tree")
    descriptor, index_name = tempfile.mkstemp()
    os.close(descriptor)
    temporary_index = Path(index_name)
    temporary_index.unlink()
    environment = {"GIT_INDEX_FILE": str(temporary_index)}
    try:
        _git(lane, "read-tree", index_tree, env=environment)
        _git(lane, "add", "-A", env=environment)
        worktree_tree = _text(lane, "write-tree", env=environment)
    finally:
        temporary_index.unlink(missing_ok=True)
    return index_tree, worktree_tree


def _tree(lane: Path, revision: str) -> str:
    """Returns the tree object for one verified revision."""
    return _text(lane, "rev-parse", f"{revision}^{{tree}}")


def _paths(output: bytes) -> set[str]:
    """Decodes a NUL-delimited Git path list without losing byte values."""
    return {os.fsdecode(value) for value in output.split(b"\0") if value}


def _refuse_untracked_collisions(
    lane: Path,
    destination_head: str,
    worktree_commit: str,
) -> None:
    """Refuses ignored destination content touched by recovered paths."""
    tracked = _paths(_git(lane, "ls-files", "-z"))
    affected = _paths(
        _git(
            lane,
            "diff",
            "--name-only",
            "-z",
            destination_head,
            worktree_commit,
        )
    )
    root = lane.resolve()
    for name in affected:
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise BridgeError("Recovery checkpoint contains an unsafe path.")
        target = root / relative
        if name not in tracked and (target.exists() or target.is_symlink()):
            raise BridgeError(
                f"Recovery destination has untracked content at {name}; "
                "preserve it before resuming this checkpoint."
            )
        parent = target.parent
        while parent != root:
            if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
                raise BridgeError(
                    f"Recovery destination has an unsafe parent for {name}."
                )
            parent = parent.parent


def _require_dead(activity: dict, issue: str, owner: str) -> None:
    """Requires one recorded process generation to be known and stopped."""
    session_id = activity.get("session_id")
    pid = activity.get("session_pid")
    ticks = activity.get("session_ticks")
    if (
        not isinstance(session_id, str)
        or not session_id
        or not isinstance(pid, int)
        or not isinstance(ticks, str)
        or not ticks
    ):
        raise BridgeError(
            f"Issue #{issue} owner {owner} has no complete session identity; "
            "confirm that generation before takeover."
        )
    if process.alive(pid, ticks):
        raise BridgeError(
            f"Issue #{issue} owner {owner} has a live session; stop and "
            "confirm that generation before takeover."
        )


def _publish_bundle(
    lane: Path,
    destination: Path,
    reference: str,
    commit: str,
) -> tuple[int, str]:
    """Publishes one fsynced bundle and removes its temporary Git ref."""
    temporary_ref = f"refs/agent-parley-recovery/{reference}"
    _git(lane, "update-ref", temporary_ref, commit)
    descriptor, temporary_name = tempfile.mkstemp(dir=destination.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        _git(
            lane,
            "bundle",
            "create",
            str(temporary),
            temporary_ref,
        )
        size = temporary.stat().st_size
        digest = _digest(temporary)
        os.replace(temporary, destination)
        folder = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(folder)
        finally:
            os.close(folder)
    finally:
        temporary.unlink(missing_ok=True)
        _git(lane, "update-ref", "-d", temporary_ref)
    return size, digest


def _step(payload: dict) -> tuple[str, dict]:
    """Returns bounded last-step and gate evidence from a native event."""
    event = str(payload.get("hook_event_name") or "")
    tool = str(payload.get("tool_name") or "")
    step = ": ".join(item for item in (event, tool) if item)
    step = step.encode()[:MAX_STEP_BYTES].decode(errors="ignore")
    command = str((payload.get("tool_input") or {}).get("cmd") or "")
    gate: dict = {}
    if re.search(r"\b(pytest|mypy|ruff)\b|\bmake\s+check\b", command):
        response = payload.get("tool_response") or {}
        gate = {
            "command": command.encode()[:MAX_STEP_BYTES].decode(
                errors="ignore"
            ),
            "exit_code": (
                response.get("exit_code")
                if isinstance(response, dict)
                else None
            ),
            "observed_at": time.time(),
        }
    return step, gate


def capture(
    directory: Path,
    manifest: dict,
    agent: str,
    payload: dict | None = None,
) -> list[dict]:
    """Captures every claim owned by one lane into private durable bundles.

    The lane index becomes one checkpoint commit. Its working tree, including
    non-ignored untracked files, becomes a child commit. A bundle outside the
    target repository keeps both commits reachable across Git maintenance.
    Source index and worktree content remain unchanged.

    Args:
        directory: Private project state directory.
        manifest: Current participant manifest.
        agent: Participant whose owned claims are captured.
        payload: Optional native lifecycle event supplying step evidence.

    Returns:
        Checkpoint records published for the lane's current claims.

    Raises:
        BridgeError: If the lane or Git state cannot be captured.
    """
    from agent_parley import issues

    participant = manifest["participants"].get(agent)
    if not participant:
        raise BridgeError(f"{agent} is not a participant in this project.")
    lane = Path(participant["lane"])
    ledger = issues.snapshot(directory)
    owned = [
        (number, record)
        for number, record in ledger["issues"].items()
        if record.get("owner") == agent and record.get("claim_id")
    ]
    if not owned:
        return []
    folder = _folder(directory)
    descriptor, index_name = tempfile.mkstemp(dir=folder)
    os.close(descriptor)
    temporary_index = Path(index_name)
    temporary_index.unlink()
    try:
        index_commit, worktree_commit = _snapshot_commits(lane, temporary_index)
    finally:
        temporary_index.unlink(missing_ok=True)
    head = _text(lane, "rev-parse", "HEAD")
    branch = _text(lane, "branch", "--show-current")
    step, gate = _step(payload or {})
    published = []
    for number, record in owned:
        claim_id = str(record["claim_id"])
        identifier = _identifier(number, claim_id)
        record_path = folder / f"{identifier}.json"
        try:
            previous = json.loads(record_path.read_text())
        except (OSError, ValueError):
            previous = {}
        bundle = folder / f"{identifier}.bundle"
        size, digest = _publish_bundle(
            lane, bundle, identifier, worktree_commit
        )
        handoff = record.get("handoff") or {}
        offer = record.get("offer") or {}
        same_content = previous.get("worktree_commit") == worktree_commit
        checkpoint = {
            "id": identifier,
            "issue": number,
            "claim_id": claim_id,
            "owner": agent,
            "source_worktree": str(lane.resolve()),
            "branch": branch,
            "head": head,
            "index_commit": index_commit,
            "worktree_commit": worktree_commit,
            "captured_at": time.time(),
            "last_verified_step": (
                step
                if payload is not None
                else str(previous.get("last_verified_step") or "")
            ),
            "gate": (
                gate
                if gate or not same_content
                else dict(previous.get("gate") or {})
            ),
            "remaining": list(
                handoff.get("remaining") or offer.get("remaining") or []
            ),
            "blockers": list(record.get("blocked_by") or []),
            "artifact": {
                "kind": "git-bundle",
                "reference": bundle.name,
                "bytes": size,
                "sha256": digest,
            },
        }
        write_json(record_path, checkpoint)
        published.append(checkpoint)
    return published


def checkpoint(directory: Path, issue: str, claim_id: str) -> dict:
    """Reads the checkpoint for one exact ownership generation."""
    identifier = _identifier(issue, claim_id)
    try:
        value = json.loads(
            (_folder(directory) / f"{identifier}.json").read_text()
        )
    except (OSError, ValueError):
        raise BridgeError(
            f"Issue #{issue} has no durable checkpoint for claim {claim_id}."
        ) from None
    if value.get("id") != identifier:
        raise BridgeError(f"Issue #{issue} recovery checkpoint is invalid.")
    return value


def authorize(
    directory: Path,
    manifest: dict,
    issue: str,
    reason: str,
) -> dict:
    """Records operator approval to stop one exact live claim generation.

    Args:
        directory: Private project state directory.
        manifest: Current participant manifest.
        issue: Bare issue number selected by the operator.
        reason: Operator rationale for recovering the live claim.

    Returns:
        Persisted approval bound to the current claim and session.

    Raises:
        BridgeError: If the claim or exact live session cannot be identified.
    """
    from agent_parley import issues

    issue = str(issue)
    record = issues.snapshot(directory)["issues"].get(issue) or {}
    owner = str(record.get("owner") or "")
    claim_id = str(record.get("claim_id") or "")
    if not owner or not claim_id or owner not in manifest["participants"]:
        raise BridgeError(f"Issue #{issue} has no current recoverable claim.")
    reason = reason.strip()
    if not 1 <= len(reason) <= 2000:
        raise BridgeError("Recovery reason must contain 1-2000 characters.")
    activity_path = directory / f"{owner}-activity.json"
    with lock(directory / f"{owner}-checkpoint.lock", timeout=1):
        try:
            activity = json.loads(activity_path.read_text())
        except (OSError, ValueError):
            raise BridgeError(
                "Claim owner has no live session identity."
            ) from None
        session_id = str(activity.get("session_id") or "")
        pid = activity.get("session_pid")
        ticks = activity.get("session_ticks")
        if not session_id or not process.alive(pid, ticks):
            raise BridgeError("Claim owner has no matching live session.")
        approval = {
            "id": f"authorization:{claim_id}:{session_id}",
            "issue": issue,
            "claim_id": claim_id,
            "owner": owner,
            "session_id": session_id,
            "session_pid": pid,
            "session_ticks": ticks,
            "reason": reason,
            "authorized_by": "operator",
            "authorized_at": time.time(),
        }
        write_json(
            _folder(directory)
            / f"{_identifier(issue, claim_id)}-approval.json",
            approval,
        )
    return approval


def approval(directory: Path, issue: str, claim_id: str) -> dict | None:
    """Returns an unused operator approval for one exact claim generation."""
    path = _folder(directory) / f"{_identifier(issue, claim_id)}-approval.json"
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if value.get("claim_id") != claim_id or value.get("used_at"):
        return None
    return value


def _consume_approval(
    directory: Path,
    issue: str,
    claim_id: str,
    authorization_id: str,
) -> None:
    """Durably consumes the exact approval behind a completed transition."""
    path = _folder(directory) / f"{_identifier(issue, claim_id)}-approval.json"
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        raise BridgeError(
            "Completed live recovery has no matching operator approval."
        ) from None
    if value.get("id") != authorization_id:
        raise BridgeError(
            "Completed live recovery names a different operator approval."
        )
    if not value.get("used_at"):
        value["used_at"] = time.time()
        write_json(path, value)


def _capacity_candidate(directory: Path, issue: str) -> dict:
    """Returns the authoritative published capacity candidate for an issue."""
    transitions = []
    for path in _folder(directory).glob(f"issue-{issue}-*-quiesce.json"):
        try:
            transition = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if transition.get("phase") != "complete" and isinstance(
            transition.get("candidate"), dict
        ):
            transitions.append(transition["candidate"])
    if len(transitions) == 1:
        return transitions[0]
    if len(transitions) > 1:
        raise BridgeError("Live recovery has conflicting durable transitions.")
    try:
        document = json.loads(
            (directory / "capacity-candidates.json").read_text()
        )
    except (OSError, ValueError):
        raise BridgeError(
            "Live recovery has no published capacity observation."
        ) from None
    if document.get("version") != 1:
        raise BridgeError("Published capacity observations are invalid.")
    matches = [
        value
        for value in document.get("candidates") or []
        if isinstance(value, dict) and str(value.get("issue") or "") == issue
    ]
    if len(matches) != 1:
        raise BridgeError(
            "Live recovery requires one current capacity observation."
        )
    return matches[0]


def prepare_takeover(
    directory: Path,
    manifest: dict,
    agent: str,
    issue: str,
) -> dict:
    """Loads a dead owner's exact claim generation for takeover.

    Args:
        directory: Private project state directory.
        manifest: Current participant manifest.
        agent: Participant requesting the takeover.
        issue: Bare repository issue number.

    Returns:
        Expected claim and orphan identities plus its durable checkpoint.

    Raises:
        BridgeError: If ownership changed, no checkpoint exists, or the old
            session process is alive.
    """
    from agent_parley import issues

    record = issues.snapshot(directory)["issues"].get(issue) or {}
    orphan = record.get("orphan") or {}
    owner = str(record.get("owner") or "")
    if not owner or orphan.get("owner") != owner:
        raise BridgeError(
            f"Issue #{issue} does not have a current orphaned owner."
        )
    if owner == agent:
        raise BridgeError(f"Issue #{issue} is already owned by {agent}.")
    if owner not in manifest["participants"]:
        raise BridgeError(f"Issue #{issue} names an unknown owner {owner}.")
    claim_id = str(record.get("claim_id") or "")
    saved = checkpoint(directory, issue, claim_id)
    with lock(directory / f"{owner}-checkpoint.lock", timeout=1):
        activity_path = directory / f"{owner}-activity.json"
        try:
            activity = json.loads(activity_path.read_text())
        except (OSError, ValueError):
            activity = {}
        _require_dead(activity, issue, owner)
    return {
        "claim_id": claim_id,
        "orphan_id": orphan.get("id", ""),
        "checkpoint": saved,
    }


def commit_takeover(
    directory: Path,
    agent: str,
    evidence: dict,
    issue: str,
    record: dict,
    orphan: dict,
) -> dict:
    """Fences a revalidated dead generation inside issue serialization.

    Args:
        directory: Private project state directory.
        agent: Participant receiving the ownership generation.
        evidence: Takeover evidence prepared by the requesting lane.
        issue: Bare issue number being transferred.
        record: Current issue record under the issue ledger lock.
        orphan: Current orphan marker under the same lock.

    Returns:
        Prepared fence identity. The issue ledger history makes the fence
        effective when it publishes the matching takeover.

    Raises:
        BridgeError: If process identity returned or evidence changed.
    """
    owner = str(record.get("owner") or "")
    if (
        not agent
        or evidence.get("claim_id") != record.get("claim_id")
        or evidence.get("orphan_id") != orphan.get("id")
    ):
        raise BridgeError(f"Issue #{issue} recovery evidence is invalid.")
    activity_path = directory / f"{owner}-activity.json"
    with lock(directory / f"{owner}-checkpoint.lock", timeout=1):
        try:
            activity = json.loads(activity_path.read_text())
        except (OSError, ValueError):
            activity = {}
        _require_dead(activity, issue, owner)
        fence = {
            "id": hashlib.sha256(
                (
                    f"{issue}\0{record['claim_id']}\0"
                    f"{orphan.get('id', '')}\0{agent}"
                ).encode()
            ).hexdigest(),
            "issue": issue,
            "claim_id": record["claim_id"],
            "session_id": activity.get("session_id", ""),
            "orphan_id": orphan.get("id", ""),
            "taken_by": agent,
            "created": time.time(),
        }
        activity["ownership_fence"] = fence
        write_json(activity_path, activity)
    return fence


def quiesce_exhausted(
    directory: Path,
    manifest: dict,
    issue: str,
) -> dict:
    """Stops one explicitly selected exhausted generation before recovery.

    A capacity observation only identifies work needing attention. Calling
    this transition supplies the explicit selection. It verifies that the
    observation still names the current owner, claim and live native session,
    terminates that exact process identity, captures its work, then publishes
    an orphan marker. Silence or elapsed time cannot enter this path.

    Args:
        directory: Private project state directory.
        manifest: Current participant manifest.
        issue: Bare issue number selected for recovery.

    Returns:
        Published orphan marker for the stopped ownership generation.

    Raises:
        BridgeError: If evidence is incomplete or stale, process identity is
            not live, or the exact process cannot be stopped.
    """
    from agent_parley import issues

    issue = str(issue)
    candidate = _capacity_candidate(directory, issue)
    owner = str(candidate.get("owner") or "")
    if (
        not owner
        or str(candidate.get("issue") or "") != issue
        or not candidate.get("observation_id")
        or not candidate.get("session_id")
        or not candidate.get("source")
    ):
        raise BridgeError(
            "Live recovery requires a complete capacity observation."
        )
    if owner not in manifest["participants"]:
        raise BridgeError("Capacity observation names an unknown owner.")
    activity_path = directory / f"{owner}-activity.json"
    with lock(directory / "issues.lock", timeout=1):
        ledger = issues.snapshot(directory)
        current = ledger["issues"].get(issue) or {}
        claim_id = str(current.get("claim_id") or "")
        if current.get("owner") != owner or not claim_id:
            raise BridgeError("Capacity observation no longer owns this issue.")
        authorization = approval(directory, issue, claim_id)
        if (
            not authorization
            or authorization.get("owner") != owner
            or authorization.get("session_id") != candidate.get("session_id")
        ):
            raise BridgeError(
                "Live recovery requires operator approval for this claim "
                "session."
            )
        transition_path = (
            _folder(directory) / f"{_identifier(issue, claim_id)}-quiesce.json"
        )
        expected_transition = {
            "issue": issue,
            "claim_id": claim_id,
            "owner": owner,
            "session_id": candidate["session_id"],
            "session_pid": authorization["session_pid"],
            "session_ticks": authorization["session_ticks"],
            "authorization_id": authorization["id"],
            "observation_id": candidate["observation_id"],
        }
        if transition_path.exists():
            try:
                transition = json.loads(transition_path.read_text())
            except (OSError, ValueError):
                raise BridgeError(
                    "Durable live recovery transition is invalid."
                ) from None
            if any(
                transition.get(key) != value
                for key, value in expected_transition.items()
            ):
                raise BridgeError(
                    "Durable live recovery transition names stale evidence."
                )
        else:
            transition = {
                **expected_transition,
                "phase": "authorized",
                "candidate": dict(candidate),
                "created": time.time(),
            }
            write_json(transition_path, transition)
        with lock(directory / f"{owner}-checkpoint.lock", timeout=1):
            try:
                activity = json.loads(activity_path.read_text())
            except (OSError, ValueError):
                raise BridgeError(
                    "Capacity owner has no session identity."
                ) from None
            if activity.get("session_id") != candidate.get("session_id"):
                raise BridgeError("Capacity observation names a stale session.")
            if manifest["participants"][owner].get("paused", False):
                raise BridgeError(
                    "Capacity owner is paused; resume it before live recovery."
                )
            if activity.get("activity") in ("waiting for approval", "idle"):
                raise BridgeError(
                    "Capacity owner is waiting for operator input; resolve "
                    "that native wait before live recovery."
                )
            pid = activity.get("session_pid")
            ticks = activity.get("session_ticks")
            if pid != authorization.get(
                "session_pid"
            ) or ticks != authorization.get("session_ticks"):
                raise BridgeError(
                    "Operator approval names a stale process generation."
                )
            alive = process.alive(pid, ticks)
            if not alive and transition.get("phase") not in (
                "authorized",
                "stopped",
                "captured",
            ):
                raise BridgeError("Capacity recovery process evidence changed.")
            if alive:
                process.ServerProcess(int(pid), str(ticks)).stop()
            if process.alive(pid, ticks):
                raise BridgeError("Capacity owner process did not stop.")
            transition["phase"] = "stopped"
            transition["stopped_at"] = time.time()
            write_json(transition_path, transition)
            activity["activity"] = "stopped for recovery"
            activity["quiesced"] = {
                "issue": issue,
                "claim_id": claim_id,
                "observation_id": str(candidate["observation_id"]),
                "stopped_at": time.time(),
            }
            write_json(activity_path, activity)
            saved = next(
                value
                for value in capture(directory, manifest, owner)
                if value["issue"] == issue
            )
            transition["phase"] = "captured"
            transition["checkpoint"] = saved["id"]
            write_json(transition_path, transition)
        marker: dict = {
            "id": (
                f"capacity:{claim_id}:{str(candidate['observation_id'])[:64]}"
            ),
            "owner": owner,
            "claim_id": claim_id,
            "reason": str(candidate.get("reason") or "capacity exhausted")[
                :2000
            ],
            "reservations": [],
            "created": time.time(),
            "observation_id": str(candidate["observation_id"]),
            "session_id": str(candidate["session_id"]),
            "reset_at": candidate.get("reset_at"),
            "source": str(candidate["source"]),
            "checkpoint": saved["id"],
            "authorization": {
                "id": authorization["id"],
                "actor": authorization["authorized_by"],
                "reason": authorization["reason"],
                "approved_at": authorization["authorized_at"],
            },
        }
        current["orphan"] = marker
        ledger["revision"] += 1
        write_json(directory / "issues.json", ledger)
        transition["phase"] = "complete"
        transition["completed_at"] = time.time()
        write_json(transition_path, transition)
        _consume_approval(
            directory,
            issue,
            claim_id,
            str(authorization["id"]),
        )
    return marker


def quiesce_authorized(directory: Path, manifest: dict) -> list[dict]:
    """Quiesces published exhausted claims with exact operator approval.

    Args:
        directory: Private project state directory.
        manifest: Current participant manifest.

    Returns:
        Orphan markers published for authorized live recovery transitions.
    """
    try:
        document = json.loads(
            (directory / "capacity-candidates.json").read_text()
        )
    except (OSError, ValueError):
        document = {"version": 1, "candidates": []}
    if document.get("version") != 1:
        return []
    from agent_parley import issues

    markers = []
    candidates = list(document.get("candidates") or [])
    completed = []
    for path in _folder(directory).glob("issue-*-quiesce.json"):
        try:
            transition = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        candidate = transition.get("candidate")
        if transition.get("phase") == "complete":
            completed.append(transition)
        elif isinstance(candidate, dict):
            candidates.append(candidate)
    seen = set()
    for transition in completed:
        issue = str(transition.get("issue") or "")
        record = issues.snapshot(directory)["issues"].get(issue) or {}
        marker = record.get("orphan") or {}
        candidate = transition.get("candidate") or {}
        identity = (issue, str(candidate.get("observation_id") or ""))
        if (
            record.get("owner") == transition.get("owner")
            and record.get("claim_id") == transition.get("claim_id")
            and marker.get("authorization", {}).get("id")
            == transition.get("authorization_id")
            and marker.get("checkpoint") == transition.get("checkpoint")
        ):
            _consume_approval(
                directory,
                issue,
                str(transition.get("claim_id") or ""),
                str(transition.get("authorization_id") or ""),
            )
            markers.append(marker)
            seen.add(identity)
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        issue = str(candidate.get("issue") or "")
        identity = (issue, str(candidate.get("observation_id") or ""))
        if identity in seen:
            continue
        seen.add(identity)
        if not re.fullmatch(r"[1-9][0-9]{0,17}", issue):
            continue
        record = issues.snapshot(directory)["issues"].get(issue) or {}
        claim_id = str(record.get("claim_id") or "")
        allowed = approval(directory, issue, claim_id) if claim_id else None
        if (
            allowed
            and allowed.get("owner") == candidate.get("owner")
            and allowed.get("session_id") == candidate.get("session_id")
        ):
            markers.append(quiesce_exhausted(directory, manifest, issue))
    return markers


def stale_session(directory: Path, agent: str, payload: dict) -> dict | None:
    """Returns a native refusal for a session fenced by a takeover."""
    try:
        activity = json.loads(
            (directory / f"{agent}-activity.json").read_text()
        )
    except (OSError, ValueError):
        return None
    fence = activity.get("ownership_fence") or {}
    session = str(payload.get("session_id") or "")
    if not fence or session != str(fence.get("session_id") or ""):
        return None
    from agent_parley import issues

    record = issues.snapshot(directory)["issues"].get(
        str(fence.get("issue") or "")
    ) or {"history": []}
    published = any(
        entry.get("action") == "take"
        and (entry.get("taken") or {}).get("fence") == fence.get("id")
        for entry in record.get("history") or []
    )
    if not published:
        return None
    detail = (
        f"Claim {fence.get('claim_id')} for issue #{fence.get('issue')} was "
        f"transferred to {fence.get('taken_by')}; this session generation "
        "cannot resume edits. Start a new session and claim new work."
    )
    event = str(payload.get("hook_event_name") or "")
    if event == "PreToolUse":
        return {
            "hookSpecificOutput": {
                "hookEventName": event,
                "permissionDecision": "deny",
                "permissionDecisionReason": detail,
            }
        }
    if event == "Stop":
        return {"decision": "block", "reason": detail}
    return {
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": detail,
        }
    }


def _artifact(directory: Path, saved: dict) -> Path:
    """Validates and returns a checkpoint bundle path."""
    artifact = saved.get("artifact") or {}
    identifier = _identifier(
        str(saved.get("issue") or ""), str(saved.get("claim_id") or "")
    )
    if saved.get("id") != identifier:
        raise BridgeError("Recovery checkpoint identity is invalid.")
    name = str(artifact.get("reference") or "")
    if name != f"{saved.get('id')}.bundle":
        raise BridgeError("Recovery artifact reference is invalid.")
    path = _folder(directory) / name
    try:
        size = path.stat().st_size
        digest = _digest(path)
    except OSError:
        raise BridgeError("Recovery artifact is unavailable.") from None
    if size != artifact.get("bytes") or digest != artifact.get("sha256"):
        raise BridgeError("Recovery artifact failed its integrity check.")
    return path


def preflight(directory: Path, lane: Path, saved: dict) -> None:
    """Verifies a checkpoint can enter a clean compatible destination.

    Args:
        directory: Private project state directory.
        lane: Recipient's assigned worktree.
        saved: Durable checkpoint selected for takeover.

    Raises:
        BridgeError: If content could be overwritten or history is unsafe.
    """
    if _git(lane, "status", "--porcelain", "-z"):
        raise BridgeError(
            "Recovery destination has dirty or untracked work; preserve it "
            "before resuming this checkpoint."
        )
    bundle = _artifact(directory, saved)
    _git(lane, "bundle", "verify", str(bundle))
    _git(
        lane,
        "fetch",
        "--no-write-fetch-head",
        str(bundle),
        str(saved["worktree_commit"]),
    )
    destination_head = _text(lane, "rev-parse", "HEAD")
    common = _text(lane, "merge-base", destination_head, str(saved["head"]))
    if common != destination_head:
        raise BridgeError(
            "Recovery destination has commits outside the captured history; "
            "choose a clean lane based on the captured claim."
        )
    _refuse_untracked_collisions(
        lane, destination_head, str(saved["worktree_commit"])
    )


def restore(directory: Path, lane: Path, record: dict) -> dict:
    """Restores one taken checkpoint into a clean recipient worktree.

    Args:
        directory: Private project state directory.
        lane: Recipient's assigned worktree.
        record: Persisted taken-claim record.

    Returns:
        Record with restored checkpoint and content evidence.

    Raises:
        BridgeError: If the artifact is invalid or destination has work that
            could be overwritten.
    """
    taken = record.get("taken") or {}
    saved = taken.get("checkpoint") or {}
    if not saved:
        return record
    recipient = str(record.get("owner") or "")
    recipient_claim = str(record.get("claim_id") or "")
    if not recipient or not re.fullmatch(r"[0-9a-f]{16}", recipient_claim):
        raise BridgeError("Recovery recipient generation is invalid.")
    _artifact(directory, saved)
    resolved_lane = str(lane.resolve())
    lane_id = hashlib.sha256(resolved_lane.encode()).hexdigest()[:16]
    receipt = _folder(directory) / (
        f"{saved['id']}-to-{recipient_claim}-{lane_id}.json"
    )
    expected = {
        "checkpoint": saved["id"],
        "recipient": recipient,
        "recipient_claim_id": recipient_claim,
        "recipient_worktree": resolved_lane,
    }
    if receipt.exists():
        try:
            restored = json.loads(receipt.read_text())
        except (OSError, ValueError):
            raise BridgeError("Recovery restore record is invalid.") from None
        if any(restored.get(key) != value for key, value in expected.items()):
            raise BridgeError(
                "Recovery restore record names another recipient."
            )
    else:
        preflight(directory, lane, saved)
        restored = {
            **expected,
            "phase": "prepared",
            "initial_head": _text(lane, "rev-parse", "HEAD"),
            "source_worktree": saved["source_worktree"],
            "head": saved["head"],
            "index_commit": saved["index_commit"],
            "worktree_commit": saved["worktree_commit"],
            "remaining": list(saved.get("remaining") or []),
            "blockers": list(saved.get("blockers") or []),
            "artifact": dict(saved["artifact"]),
        }
        write_json(receipt, restored)
    if restored.get("phase") == "complete":
        return {**record, "recovery": restored}
    head_tree = _tree(lane, str(saved["head"]))
    index_tree = _tree(lane, str(saved["index_commit"]))
    worktree_tree = _tree(lane, str(saved["worktree_commit"]))
    if restored.get("phase") == "prepared":
        current_head = _text(lane, "rev-parse", "HEAD")
        current_index, current_worktree = _content_trees(lane)
        initial_head = str(restored["initial_head"])
        initial_tree = _tree(lane, initial_head)
        if current_head == initial_head and (
            current_index,
            current_worktree,
        ) == (initial_tree, initial_tree):
            if current_head != saved["head"]:
                _git(lane, "merge", "--ff-only", str(saved["head"]))
        elif current_head != saved["head"] or (
            current_index,
            current_worktree,
        ) != (head_tree, head_tree):
            raise BridgeError(
                "Recovery destination changed during the saved HEAD phase."
            )
        restored["phase"] = "head"
        write_json(receipt, restored)
    if restored.get("phase") == "head":
        if _text(lane, "rev-parse", "HEAD") != saved["head"]:
            raise BridgeError(
                "Recovery destination HEAD changed during resume."
            )
        current = _content_trees(lane)
        if current == (head_tree, head_tree):
            index_patch = _git(
                lane,
                "diff",
                "--binary",
                str(saved["head"]),
                str(saved["index_commit"]),
            )
            if index_patch:
                _git(
                    lane,
                    "apply",
                    "--index",
                    "--binary",
                    "-",
                    input_data=index_patch,
                )
        elif current != (index_tree, index_tree):
            raise BridgeError(
                "Recovery destination changed during the saved index phase."
            )
        restored["phase"] = "index"
        write_json(receipt, restored)
    if restored.get("phase") == "index":
        current_index, current_worktree = _content_trees(lane)
        if current_index != index_tree:
            raise BridgeError("Recovery index changed during worktree resume.")
        if current_worktree == index_tree:
            worktree_patch = _git(
                lane,
                "diff",
                "--binary",
                str(saved["index_commit"]),
                str(saved["worktree_commit"]),
            )
            if worktree_patch:
                _git(
                    lane,
                    "apply",
                    "--binary",
                    "-",
                    input_data=worktree_patch,
                )
        elif current_worktree != worktree_tree:
            raise BridgeError(
                "Recovery worktree changed during the saved worktree phase."
            )
        restored["phase"] = "complete"
        restored["restored_at"] = time.time()
        write_json(receipt, restored)
    restored = {
        **restored,
        "checkpoint": saved["id"],
    }
    return {**record, "recovery": restored}
