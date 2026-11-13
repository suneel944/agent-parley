"""Flags two live lanes converging on one branch name or one path set.

Lanes that run the same model on the same context make the same choices: in
one published run 18 of 30 agents on one repository picked one branch name.
Two lanes that share a branch name, reserve mostly the same paths, or plan
the same new file are duplicate work in flight, and nothing said so until
their pull requests collided.

Each live lane is compared with every other live lane of its project, and
each pair is flagged at most once, by the first of these it shares: a branch
name, a new file named in both task plans, or a reserved path set whose
overlap passes `MIN_SHARED_PATHS`. The flag is a reading. Reservations stay
advisory and nothing is refused, renamed or released; each lane of the pair
is told once and decides whether to rename or re-plan. The flag clears when
the shared item does.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path

from agent_parley import lanes, roster
from agent_parley.state import BridgeError, write_json

MIN_SHARED_PATHS = 3
"""Fewest exactly shared reservation keys that flag a path-set overlap.

One or two shared paths is ordinary contention, such as a changelog or a
registry module, and the reservation conflict already names it. Three or
more shared keys, which must also be at least half of the smaller set, means
the two lanes scoped the same change. Fixed in code so every project reads
the same flag; a knob would let one project hide a duplicate another sees.
"""
PUBLICATION = "collisions.json"
MAX_ITEM = 120
PLAN_PATH = re.compile(
    r"(?<![\w./-])(?:[\w.-]+/)+[\w-][\w.-]*\.[A-Za-z]{1,8}(?![\w/-])"
)
"""A repository path such as ``agent_parley/collisions.py`` in plan text.

A directory part and a letters-only extension are required, so prose such
as ``e.g``, a version such as ``0.23.0`` and a bare module name are not read
as a file the lane means to create.
"""
ENDED = frozenset({lanes.STOPPED, lanes.DEAD, lanes.RECLAIMED})
"""Lane states whose session is not running, so its lane is not compared."""


def usable_branch(branch: str) -> bool:
    """Reports whether a lane's branch reading names a real branch.

    Args:
        branch: The lane's branch, or a marker such as ``<detached HEAD>``
            or ``an unavailable worktree``.

    Returns:
        True for a branch name, False for an empty reading or a marker.
    """
    return bool(branch) and not branch.startswith(("<", "an unavailable"))


def plan_files(root: Path, plan: dict | None) -> set[str]:
    """Names the new files a lane's task plan mentions.

    Args:
        root: Base checkout of the project.
        plan: The lane's plan as `lanes.lane_task_plan` returns it.

    Returns:
        Paths named in unfinished plan items that do not exist in the base
        checkout, so only a file the lane means to create is compared. A
        name the file system cannot check, such as one too long, is left
        out.
    """
    found: set[str] = set()
    for item in (plan or {}).get("items", []):
        if item.get("status") == "completed":
            continue
        for path in PLAN_PATH.findall(str(item.get("text", ""))):
            try:
                if (root / path).exists():
                    continue
            except OSError:
                continue
            found.add(path)
    return found


def find(readings: dict[str, dict]) -> list[dict]:
    """Pairs live lanes that converge on one branch, file or path set.

    Args:
        readings: Lane name to ``branch``, ``paths``, the reservation keys
            it holds, ``files``, the new files its plan names, and
            optionally ``parent``, the lane that started it.

    Returns:
        One entry per converging pair, ordered by lane names, with
        ``lanes``, the two names in order, ``kind``, one of ``branch``,
        ``file`` or ``paths``, ``item``, the shared item, and ``count``,
        how many items the pair shares of that kind. A child lane and its
        parent are never paired: the child works inside the parent's scope
        by design.
    """
    names = sorted(readings)
    pairs = []
    for index, first in enumerate(names):
        for second in names[index + 1 :]:
            one, two = readings[first], readings[second]
            if second == one.get("parent") or first == two.get("parent"):
                continue
            shared_paths = set(one["paths"]) & set(two["paths"])
            smaller = min(len(set(one["paths"])), len(set(two["paths"])))
            files = sorted(set(one["files"]) & set(two["files"]))
            if usable_branch(one["branch"]) and one["branch"] == two["branch"]:
                kind, items = "branch", [one["branch"]]
            elif files:
                kind, items = "file", files
            elif (
                len(shared_paths) >= MIN_SHARED_PATHS
                and 2 * len(shared_paths) >= smaller
            ):
                kind, items = "paths", sorted(shared_paths)
            else:
                continue
            pairs.append(
                {
                    "lanes": [first, second],
                    "kind": kind,
                    "item": items[0][:MAX_ITEM],
                    "count": len(items),
                }
            )
    return pairs


def read(
    home: Path,
    directory: Path,
    manifest: dict,
    branches: dict[str, str] | None = None,
    db: sqlite3.Connection | None = None,
) -> list[dict]:
    """Reads the converging pairs among a project's live lanes.

    Args:
        home: Private bridge state root.
        directory: Private project state directory.
        manifest: Project manifest naming the base checkout and the roster.
        branches: Lane name to the branch a caller already read, or None to
            read each live lane's checkout.
        db: A status frame's open read transaction, or None to open one.

    Returns:
        The pairs `find` returns among lanes that are neither retired nor
        stopped, dead or reclaimed. A store that cannot be read compares
        branches and plans only, of every lane not retired.
    """
    from agent_parley import store
    from agent_parley.checkpoints import lane_branch

    held: dict[str, list[str]] = {}
    ended: set[str] = set()
    try:
        if db is not None or store.present(home):
            with store.reading(home, db) as reader:
                held = store.active_reservations(
                    home, manifest["root"], db=reader
                )
                for name in manifest["participants"]:
                    record = lanes.read(reader, manifest["root"], name)
                    if record and record["state"] in ENDED:
                        ended.add(name)
    except (BridgeError, OSError, sqlite3.Error):
        held, ended = {}, set()
    root = Path(manifest["root"])
    readings = {}
    for name, participant in manifest["participants"].items():
        if roster.retired(participant) or name in ended:
            continue
        branch = (branches or {}).get(name)
        if branch is None:
            branch = lane_branch(Path(participant["lane"]))
        readings[name] = {
            "branch": branch,
            "paths": held.get(participant.get("display", name), []),
            "files": plan_files(root, lanes.lane_task_plan(directory, name)),
            "parent": participant.get("parent"),
        }
    return find(readings)


def shared(pair: dict) -> str:
    """Words the item one converging pair shares.

    Args:
        pair: One entry of `find`.

    Returns:
        Text such as ``branch fix/login`` or ``4 reserved paths, such as
        app.py``.
    """
    if pair["kind"] == "branch":
        return f"branch {pair['item']}"
    noun = "new file" if pair["kind"] == "file" else "reserved path"
    if pair["count"] == 1:
        return f"{noun} {pair['item']}"
    return f"{pair['count']} {noun}s, such as {pair['item']}"


def line(pair: dict) -> str:
    """Words one converging pair as the line `status` prints.

    Args:
        pair: One entry of `find`.

    Returns:
        One line naming both lanes and the shared item.
    """
    first, second = pair["lanes"]
    return (
        f"Converging: {first} and {second} share {shared(pair)}; one of "
        "them should rename or re-plan. Nothing was refused."
    )


def peers(pairs: list[dict], lane: str) -> list[str]:
    """Names what each pair one lane belongs to shares, for its bucket.

    Args:
        pairs: Entries of `find` for the lane's project.
        lane: Lane name.

    Returns:
        Text such as ``codex on branch fix/login`` for each pair holding
        the lane, in pair order.
    """
    return [
        f"{other} on {shared(pair)}"
        for pair in pairs
        if lane in pair["lanes"]
        for other in pair["lanes"]
        if other != lane
    ]


def supervise(home: Path, directory: Path, manifest: dict) -> None:
    """Tells each lane of a newly converging pair once.

    The pairs flagged now are kept in the project's `PUBLICATION` under the
    two lane names and the kind they share, with the time each was first
    seen and the lanes already told. The shared item itself is not part of
    the key, so a reserved path set that shifts while it still overlaps is
    not told again. A pair that cleared is dropped, so the same pair
    converging again later is told again under a new first-seen time. Each
    message carries an idempotency key derived from the pair key and that
    first-seen time, so a retried pass cannot send it twice. A send that
    fails leaves only that lane untold, and the next pass retries it alone.

    Args:
        home: Private bridge state root.
        directory: Private project state directory.
        manifest: Current participant manifest.
    """
    from agent_parley import store

    pairs = read(home, directory, manifest)
    path = directory / PUBLICATION
    try:
        kept = json.loads(path.read_text())
    except (OSError, ValueError):
        kept = {}
    if not isinstance(kept, dict):
        kept = {}
    current = {}
    for pair in pairs:
        key = "|".join([*pair["lanes"], pair["kind"]])
        entry = kept.get(key)
        if not isinstance(entry, dict):
            entry = {"seen": time.time(), "told": []}
        told = [lane for lane in entry.get("told", []) if lane in pair["lanes"]]
        seen = entry.get("seen", time.time())
        digest = hashlib.sha256(f"{key}|{seen}".encode()).hexdigest()[:16]
        for lane in pair["lanes"]:
            if lane in told:
                continue
            other = next(name for name in pair["lanes"] if name != lane)
            participant = manifest["participants"][lane]
            try:
                store.speak(
                    home,
                    manifest["root"],
                    participant.get("display", lane),
                    f"Converging with {other}",
                    f"You and {other} share {shared(pair)} while working on "
                    "different claims, which reads as duplicate work in "
                    "flight. Rename your branch or re-plan if the overlap is "
                    "not intended, or agree an owner with the other lane. "
                    "Nothing was refused, renamed or released.",
                    f"converging:{digest}:{lane}",
                )
            except (BridgeError, OSError, sqlite3.Error):
                continue
            told.append(lane)
        current[key] = {"seen": seen, "told": told}
    if current != kept:
        with contextlib.suppress(OSError):
            write_json(path, current)
