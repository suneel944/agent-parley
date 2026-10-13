"""Keeps spare lane worktrees prepared so a new lane skips the init wait.

A new lane pays for its worktree and for the project's ``init`` command
before its native CLI starts, and ``init`` is usually most of that wait. A
project that records ``pool N`` keeps N spare worktrees ready instead: each
is a worktree on its own branch, cut from the project base, with ``init``
already run in it. ``run`` takes a ready spare when one exists, renames its
branch to the lane branch it would have created, and starts the CLI there.

A spare is disposable and never repaired. One cut from an older base, or
prepared by a different ``init`` command, is stale: it is never handed out,
and the next fill or ``gc`` sweep removes its worktree and its branch. A
spare holds no work by construction, because nothing runs in it until a lane
takes it, and a taken spare leaves the record at that moment.

The record lives in the project state directory, outside the target
repository, and is guarded by its own lock, which is held only while the
record is read or written, never while a worktree is created or ``init``
runs. A spare carries no participant, no credential profile and no
registration; those attach when ``run`` takes it.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from agent_parley.state import BridgeError, lock, write_json

RECORD = "spares.json"
LOCK = "pool.lock"
LOG = "pool.log"
LOCK_SECONDS = 10
READY = "ready"
PREPARING = "preparing"
STALE = "stale"


def spares(directory: Path) -> list[dict]:
    """Reads the spare worktrees a project recorded.

    Args:
        directory: Private project state directory.

    Returns:
        Every recorded spare, ready or still being prepared.
    """
    import json

    path = directory / RECORD
    if not path.exists():
        return []
    try:
        recorded = json.loads(path.read_text()).get("spares", [])
    except (OSError, ValueError, AttributeError):
        return []
    return [spare for spare in recorded if isinstance(spare, dict)]


def _write(directory: Path, recorded: list[dict]) -> None:
    """Replaces the spare record."""
    write_json(directory / RECORD, {"spares": recorded})


def _git(cwd: Path, *arguments: str) -> str | None:
    """Runs Git and returns stripped output, or None when it fails."""
    from agent_parley.worktrees import git

    try:
        return git(cwd, *arguments)
    except (BridgeError, OSError, subprocess.TimeoutExpired):
        return None


def current(spare: dict, manifest: dict) -> bool:
    """Reports whether a spare was prepared for the project as it is now.

    Args:
        spare: One recorded spare.
        manifest: Project manifest holding the base and ``init`` command.

    Returns:
        Whether the spare was cut from the current base and prepared by the
        current ``init`` command.
    """
    return spare.get("base") == manifest["base"] and spare.get(
        "initialize"
    ) == list(manifest.get("initialize") or [])


def _preparing(spare: dict) -> bool:
    """Reports whether a live fill is still preparing one spare."""
    from agent_parley import process

    return spare.get("state") == PREPARING and process.alive(
        spare.get("pid"), spare.get("ticks")
    )


def state(spare: dict, manifest: dict) -> str:
    """Names one spare's condition for a reading.

    Args:
        spare: One recorded spare.
        manifest: Project manifest holding the base and ``init`` command.

    Returns:
        `PREPARING` while a live fill prepares it, `READY` when it can be
        handed out, and `STALE` otherwise.
    """
    if _preparing(spare):
        return PREPARING
    if (
        spare.get("state") == READY
        and current(spare, manifest)
        and Path(spare["path"]).is_dir()
    ):
        return READY
    return STALE


def discard(root: Path, spare: dict) -> bool:
    """Removes one spare's worktree and its branch.

    A spare holds no work, so its worktree is removed with force: the files
    ``init`` wrote are all it carries. Its branch is deleted only once the
    worktree is gone, so a branch never outlives a refusal unexplained.

    Args:
        root: Base checkout the spare is registered with.
        spare: The spare, already dropped from the record.

    Returns:
        Whether both the worktree and the branch are gone.
    """
    path = Path(spare["path"])
    if path.exists() and (
        _git(root, "worktree", "remove", "--force", str(path)) is None
    ):
        return False
    _git(root, "worktree", "prune")
    branch = str(spare.get("branch") or "")
    if branch and _git(root, "rev-parse", "--verify", "--quiet", branch):
        return _git(root, "branch", "-D", branch) is not None
    return True


def _slot(directory: Path, key: str, prefix: str, taken: set[str]) -> dict:
    """Names the next free spare worktree path and branch.

    A spare directory carries a dot, which a participant name never does,
    so a spare can never shadow a lane or a lane's state file.
    """
    ordinal = 1
    while True:
        path = directory / f"spare.{ordinal}"
        branch = f"{prefix}/{key}/spare-{ordinal}"
        if str(path) not in taken and branch not in taken and not path.exists():
            return {"path": str(path), "branch": branch}
        ordinal += 1


def fill(root: Path, directory: Path, manifest: dict) -> list[dict]:
    """Brings the pool to its configured size from the current base.

    Stale spares and spares beyond the configured size are removed first.
    Each missing spare is reserved in the record, then created and prepared
    without the record's lock held, and marked ready only when ``init``
    succeeded. A spare whose creation or ``init`` failed is removed again.

    Args:
        root: Base checkout of the project.
        directory: Private project state directory.
        manifest: Project manifest holding the pool size, base, branch
            prefix and ``init`` command.

    Returns:
        The spares recorded once the fill finished.
    """
    from agent_parley import process
    from agent_parley.worktrees import initialize_lane

    wanted = int(manifest.get("pool") or 0)
    with lock(directory / LOCK, timeout=LOCK_SECONDS):
        kept: list[dict] = []
        dropped: list[dict] = []
        for spare in spares(directory):
            condition = state(spare, manifest)
            if condition == PREPARING or (
                condition == READY and len(kept) < wanted
            ):
                kept.append(spare)
            else:
                dropped.append(spare)
        refs = _git(root, "for-each-ref", "--format=%(refname:short)")
        taken = set((refs or "").splitlines())
        for spare in kept:
            taken.update((spare["path"], spare["branch"]))
        reserved = []
        for _ in range(wanted - len(kept)):
            slot = _slot(
                directory,
                directory.name,
                str(manifest.get("branch_prefix") or "parley"),
                taken,
            )
            taken.update(slot.values())
            reserved.append(
                {
                    **slot,
                    "base": manifest["base"],
                    "initialize": list(manifest.get("initialize") or []),
                    "state": PREPARING,
                    "pid": os.getpid(),
                    "ticks": process.start_ticks(os.getpid()),
                    "created": time.time(),
                }
            )
        _write(directory, kept + reserved)
    for spare in dropped:
        discard(root, spare)
    for spare in reserved:
        started = time.monotonic()
        prepared = (
            _git(
                root,
                "worktree",
                "add",
                "-b",
                spare["branch"],
                spare["path"],
                spare["base"],
            )
            is not None
        )
        if prepared and spare["initialize"]:
            try:
                initialize_lane(Path(spare["path"]), spare["initialize"], root)
            except (BridgeError, subprocess.TimeoutExpired) as error:
                print(error, file=sys.stderr)
                prepared = False
        with lock(directory / LOCK, timeout=LOCK_SECONDS):
            recorded = spares(directory)
            for entry in recorded:
                if entry["path"] == spare["path"]:
                    entry.update(
                        state=READY if prepared else STALE,
                        pid=None,
                        ticks=None,
                        init_seconds=round(time.monotonic() - started, 3),
                    )
            _write(
                directory,
                [
                    entry
                    for entry in recorded
                    if prepared or entry["path"] != spare["path"]
                ],
            )
        if not prepared:
            discard(root, spare)
    return spares(directory)


def take(root: Path, directory: Path, manifest: dict) -> dict | None:
    """Hands out one ready spare cut from the current base, if any.

    The spare leaves the record before it is returned, so no second launch
    can take it. A spare is handed out only when it is ready, was prepared
    for the current base and ``init`` command, and its worktree still sits
    on its own branch at exactly that base. A ready spare whose worktree
    moved off that branch or base is removed here; any other unusable spare
    is left for the next fill or sweep to remove.

    Args:
        root: Base checkout of the project.
        directory: Private project state directory.
        manifest: Project manifest holding the base and ``init`` command.

    Returns:
        The spare taken, or None when the pool holds no usable one.
    """
    if not (directory / RECORD).exists():
        return None
    moved: list[dict] = []
    chosen = None
    with lock(directory / LOCK, timeout=LOCK_SECONDS):
        recorded = spares(directory)
        for spare in recorded:
            if state(spare, manifest) != READY:
                continue
            path = Path(spare["path"])
            if (
                _git(path, "rev-parse", "HEAD") != manifest["base"]
                or _git(path, "symbolic-ref", "--short", "HEAD")
                != spare["branch"]
            ):
                moved.append(spare)
                continue
            chosen = spare
            break
        if chosen is not None or moved:
            _write(
                directory,
                [
                    entry
                    for entry in recorded
                    if entry is not chosen and entry not in moved
                ],
            )
    for spare in moved:
        discard(root, spare)
    return chosen


def replenish(home: Path, root: Path, directory: Path) -> None:
    """Starts a detached fill so the pool regains the spare just taken.

    The fill runs in its own session with no lane credential in its
    environment, and appends its output to the project's pool log, so the
    launch that took the spare never waits for the next one.

    Args:
        home: Private bridge state root.
        root: Base checkout of the project.
        directory: Private project state directory.
    """
    environment = {
        name: value
        for name, value in os.environ.items()
        if name != "AGENT_PARLEY_TOKEN"
    }
    with (directory / LOG).open("ab") as log:
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "agent_parley.cli",
                "--home",
                str(home),
                "pool",
                "fill",
                "--repo",
                str(root),
            ],
            cwd=directory,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )


def reading(directory: Path, manifest: dict) -> dict:
    """Reports the pool's configured size and each spare's condition.

    Args:
        directory: Private project state directory.
        manifest: Project manifest holding the pool size and base.

    Returns:
        The configured size and one row per recorded spare naming its
        worktree, branch, base and condition.
    """
    return {
        "size": int(manifest.get("pool") or 0),
        "spares": [
            {
                "worktree": spare.get("path", ""),
                "branch": spare.get("branch", ""),
                "base": spare.get("base", ""),
                "state": state(spare, manifest),
            }
            for spare in spares(directory)
        ],
    }


def summary_line(reading: dict) -> str:
    """Renders the pool's reading as one status line.

    Args:
        reading: The pool reading `reading` produced.

    Returns:
        One line counting ready, preparing and stale spares against the
        configured size, or an empty string when the project keeps no pool
        and records no spare.
    """
    rows = reading.get("spares") or []
    if not reading.get("size") and not rows:
        return ""
    counts = {
        condition: sum(row["state"] == condition for row in rows)
        for condition in (READY, PREPARING, STALE)
    }
    return (
        f"Spare worktrees: {counts[READY]} ready of {reading.get('size', 0)}"
        f", {counts[PREPARING]} preparing, {counts[STALE]} stale"
    )


def sweep(
    root: Path, directory: Path, manifest: dict, *, apply: bool = False
) -> list[dict]:
    """Assesses, and optionally removes, stale and surplus spares for ``gc``.

    Args:
        root: Base checkout of the project.
        directory: Private project state directory.
        manifest: Project manifest holding the pool size and base.
        apply: Whether stale spares and spares beyond the pool size are
            removed, as the sweep that removes dead lanes does.

    Returns:
        One row per recorded spare, shaped like a reclaim row: a usable
        spare is kept, any other is reclaimable or, when applied, removed.
    """
    if not (directory / RECORD).exists():
        return []
    wanted = int(manifest.get("pool") or 0)
    with lock(directory / LOCK, timeout=LOCK_SECONDS):
        kept: list[dict] = []
        dropped: list[dict] = []
        for spare in spares(directory):
            condition = state(spare, manifest)
            if condition == PREPARING or (
                condition == READY
                and sum(state(entry, manifest) == READY for entry in kept)
                < wanted
            ):
                kept.append(spare)
            else:
                dropped.append(spare)
        if apply:
            _write(directory, kept)
    rows = [
        {
            "worktree": spare["path"],
            "branch": spare.get("branch", ""),
            "participant": "",
            "reclaim": False,
            "reason": "spare",
            "paths": [],
        }
        for spare in kept
    ]
    for spare in dropped:
        rows.append(
            {
                "worktree": spare["path"],
                "branch": spare.get("branch", ""),
                "participant": "",
                "reclaim": True,
                "reason": "stale spare",
                "paths": [],
                **({"removed": discard(root, spare)} if apply else {}),
            }
        )
    return rows
