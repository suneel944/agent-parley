"""Forecasts reservation collisions and merge conflicts between lanes.

A reservation names the paths a lane intends to edit. Git history names the
paths that habitually change together with them, and a peer may hold one of
those already. Reading that before the lane starts turns a merge conflict a
week later into a one-line notice today. Everything here is advisory: a
forecast never denies a reservation or a claim, and a history Git cannot read
inside its timeout is no forecast rather than a failed call.

Once lanes have committed, the merge forecast tests what their branches would
actually do when merged. Each pair of lanes whose changes touch a common path
is merged with ``git merge-tree --write-tree``, which needs no checkout and
moves no reference, index or worktree. The tree and blob objects it writes go
to a private object store under the project state directory, removed at the
end of the pass, so nothing is written into the target repository. A
predicted conflict names the pair, the files
and the conflicting hunks' line ranges, and each lane is told once per newly
predicted pair. ``plan`` orders the open pull requests and lanes with work
for landing so that the fewest of them conflict with the default branch as it
would stand. The coordinator never merges: it forecasts and recommends.
"""

import contextlib
import functools
import itertools
import json
import os
import sqlite3
import subprocess
import tempfile
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path

from agent_parley import roster
from agent_parley.state import BridgeError, write_json

WINDOW = 500
THRESHOLD = 3
TIMEOUT = 10
CACHE = "cochanges.json"
MAX_FORECAST = 16
MERGE_RECORD = "merge-forecast.json"
OBJECTS = "merge-forecast-objects"
MAX_MERGES = 64
MAX_FILES = 8
MAX_RANGES = 8
SCRATCH_IDENTITY = {
    "GIT_AUTHOR_NAME": "merge forecast",
    "GIT_AUTHOR_EMAIL": "forecast@localhost",
    "GIT_AUTHOR_DATE": "1000000000 +0000",
    "GIT_COMMITTER_NAME": "merge forecast",
    "GIT_COMMITTER_EMAIL": "forecast@localhost",
    "GIT_COMMITTER_DATE": "1000000000 +0000",
}


def _git(root: str, *args: str) -> str | None:
    """Runs one bounded Git read in the base checkout, or reports None."""
    try:
        result = subprocess.run(
            ["git", "-C", root, *args],
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return None if result.returncode else result.stdout


def history(root: str, directory: Path) -> list[list[str]]:
    """Reads the files each recent commit of the base checkout touched.

    The reading covers the last ``WINDOW`` commits reachable from the base
    checkout's head. It is cached as JSON in the project state directory,
    keyed by that head commit, so a lane's reservation costs one ``rev-parse``
    while the base stands still and one ``git log`` when it moves. The cache
    lives beside the manifest, never inside the repository.

    Args:
        root: Canonical project key, which is the base checkout's path.
        directory: Private project state directory that holds the cache.

    Returns:
        One list of repository-relative paths per commit, newest first.
        Empty when Git cannot answer inside its timeout, the checkout has no
        commit, or the cache cannot be written.
    """
    head = (_git(root, "rev-parse", "--verify", "HEAD") or "").strip()
    if not head:
        return []
    cache = directory / CACHE
    try:
        cached = json.loads(cache.read_text())
        if cached.get("base") == head and isinstance(
            cached.get("commits"), list
        ):
            return cached["commits"]
    except (OSError, ValueError, AttributeError):
        pass
    output = _git(
        root,
        "log",
        "--name-only",
        "--format=%x00",
        f"--max-count={WINDOW}",
        "HEAD",
    )
    if output is None:
        return []
    commits = [
        sorted({line for line in block.splitlines() if line})
        for block in output.split("\x00")
    ]
    commits = [files for files in commits if files]
    try:
        directory.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"base": head, "commits": commits}))
    except OSError:
        pass
    return commits


def cochanges(
    commits: list[list[str]],
    paths: list[str],
    overlap: Callable[[str, str], bool],
    threshold: int = THRESHOLD,
) -> dict[str, int]:
    """Counts the files that changed in the same commit as a requested path.

    Args:
        commits: Files per commit, as ``history`` reports them.
        paths: Reservation keys or repository-relative paths being taken.
        overlap: Rule that says whether a committed path matches a key; the
            store's reservation overlap rule, so a glob key matches the files
            under it exactly as a competing reservation would.
        threshold: Fewest shared commits that make a file worth reporting.

    Returns:
        Mapping of co-changed file to how many of the recent commits changed
        it together with one of the requested paths. A file that itself
        matches a requested path is left out, because the reservation already
        covers it.
    """
    keys = [key for key in paths if ":" not in key.partition("/")[0]]
    if not keys or threshold < 1:
        return {}
    counts: Counter[str] = Counter()
    for files in commits:
        matched = {
            file for file in files if any(overlap(file, key) for key in keys)
        }
        if matched:
            counts.update(file for file in files if file not in matched)
    return {file: n for file, n in counts.items() if n >= threshold}


def collisions(
    counts: dict[str, int],
    held: dict[str, list[str]],
    overlap: Callable[[str, str], bool],
) -> list[dict]:
    """Names the co-changed files a peer currently reserves.

    Args:
        counts: Co-changed files and their counts, as ``cochanges`` reports.
        held: Active reservation keys per peer identity, the caller's own
            identity already excluded.
        overlap: The store's reservation overlap rule.

    Returns:
        One record per file and peer, carrying ``path``, ``peer`` and
        ``count``, most frequent first and then by path and peer, clipped to
        ``MAX_FORECAST`` records.
    """
    found: list[dict] = [
        {"path": path, "peer": peer, "count": count}
        for path, count in counts.items()
        for peer, patterns in held.items()
        if any(overlap(path, pattern) for pattern in patterns)
    ]
    found.sort(
        key=lambda entry: (-entry["count"], entry["path"], entry["peer"])
    )
    return found[:MAX_FORECAST]


def _run(
    root: str, *args: str, env: dict[str, str] | None = None
) -> tuple[int, str] | None:
    """Runs one bounded Git command and reports its status and output.

    Args:
        root: Checkout the command runs in.
        *args: Git arguments following the checkout selection.
        env: Variables added to the inherited environment, such as the
            private object store ``scratch`` names or the fixed identity
            ``commit-tree`` needs.

    Returns:
        The exit status and standard output, undecodable bytes replaced, or
        None when Git could not be run or exceeded its timeout.
    """
    try:
        result = subprocess.run(
            ["git", "-C", root, *args],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=TIMEOUT,
            check=False,
            env={**os.environ, **env} if env else None,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.returncode, result.stdout


def _ranges(text: str) -> list[list[int]]:
    """Reads the first and last line of each conflict marker block."""
    ranges: list[list[int]] = []
    start = 0
    for number, line in enumerate(text.splitlines(), start=1):
        if line.startswith("<<<<<<<"):
            start = number
        elif line.startswith(">>>>>>>") and start:
            ranges.append([start, number])
            start = 0
    return ranges[:MAX_RANGES]


def scratch(
    root: str, directory: Path, stack: contextlib.ExitStack
) -> dict[str, str] | None:
    """Opens a private object store for the objects a pass writes.

    The store is a temporary directory under ``OBJECTS`` in the project
    state directory, removed when ``stack`` closes. Git reads the target
    repository's objects through it as an alternate and writes every new
    object into it, so a merge test or scratch commit leaves the target
    repository's object store untouched.

    Args:
        root: Base checkout whose object store holds the commits.
        directory: Private project state directory.
        stack: Owner of the temporary directory's lifetime.

    Returns:
        The environment that points Git at the private store, or None when
        Git cannot name the repository's object store or the directory
        cannot be made.
    """
    common = _git(root, "rev-parse", "--git-common-dir")
    if common is None:
        return None
    parent = directory / OBJECTS
    try:
        parent.mkdir(parents=True, exist_ok=True)
        private = stack.enter_context(
            tempfile.TemporaryDirectory(dir=parent, ignore_cleanup_errors=True)
        )
    except OSError:
        return None
    return {
        "GIT_OBJECT_DIRECTORY": private,
        "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(
            Path(root, common.rstrip("\n")).resolve() / "objects"
        ),
    }


def merge(
    root: str, ours: str, theirs: str, env: dict[str, str]
) -> dict | None:
    """Tests one merge without a checkout and names what would conflict.

    ``git merge-tree --write-tree`` finds the merge base itself and writes
    the result as unreachable objects into the private store ``env`` names;
    no reference, index, worktree or repository object changes. Each
    conflicted file's line ranges are read from the conflict markers in the
    written result, so they number the lines of the merged file a person
    resolving the conflict would see. A conflict without markers, such as a
    binary file or a modify/delete, has no ranges.

    Args:
        root: Checkout whose object store holds both commits.
        ours: Commit or branch merged into.
        theirs: Commit or branch merged in.
        env: Private object store environment, as ``scratch`` returns it.

    Returns:
        ``clean``, whether the merge succeeds without conflict; ``tree``,
        the written result's tree; and ``files``, one record per conflicted
        path carrying ``path`` and ``ranges``, at most ``MAX_FILES`` of
        them. None when Git could not answer, including a Git older than
        2.38 that has no ``--write-tree``.
    """
    result = _run(
        root,
        "merge-tree",
        "--write-tree",
        "-z",
        "--name-only",
        "--no-messages",
        ours,
        theirs,
        env=env,
    )
    if result is None or result[0] not in (0, 1):
        return None
    tree, *listed = result[1].split("\0")
    paths: list[str] = []
    for path in listed:
        if not path:
            break
        if path not in paths:
            paths.append(path)
    files = []
    for path in paths[:MAX_FILES]:
        shown = _run(root, "cat-file", "blob", f"{tree}:{path}", env=env)
        ranges = _ranges(shown[1]) if shown and not shown[0] else []
        files.append({"path": path, "ranges": ranges})
    return {"clean": result[0] == 0, "tree": tree.strip(), "files": files}


def _branch_heads(root: str) -> dict[str, str] | None:
    """Reads local and ``origin`` branch heads in one Git call, or None."""
    result = _run(
        root,
        "for-each-ref",
        "--format=%(refname)%00%(objectname)",
        "refs/heads/",
        "refs/remotes/origin/",
    )
    if result is None or result[0]:
        return None
    heads: dict[str, str] = {}
    for line in result[1].splitlines():
        name, _, head = line.partition("\0")
        if name.startswith("refs/heads/"):
            heads[name.removeprefix("refs/heads/")] = head
        elif name.startswith("refs/remotes/origin/"):
            heads.setdefault(name.removeprefix("refs/remotes/origin/"), head)
    return heads


def _change(root: str, base: str, head: str, cached: dict) -> dict | None:
    """Reads where a branch forked from the base and the paths it changed.

    Args:
        root: Base checkout.
        base: Head commit of the base checkout.
        head: Head commit of the branch.
        cached: The previous reading for this branch, reused unchanged while
            neither head has moved.

    Returns:
        ``head``, ``base``, ``fork`` and the sorted ``paths`` the branch
        changed since it forked; no paths when it holds nothing beyond the
        base. None when Git could not answer, so a failed read is never
        cached as a branch without changes.
    """
    if cached.get("head") == head and cached.get("base") == base:
        return cached
    fork = _git(root, "merge-base", base, head)
    if fork is None:
        return None
    fork = fork.strip()
    listing = (
        _git(root, "diff", "--no-renames", "--name-only", "-z", fork, head)
        if fork != head
        else ""
    )
    if listing is None:
        return None
    return {
        "head": head,
        "base": base,
        "fork": fork,
        "paths": sorted(path for path in listing.split("\0") if path),
    }


def _test(
    root: str,
    entries: dict[str, dict],
    cache: dict,
    failed: list[str],
    objects: Callable[[], dict[str, str] | None],
) -> tuple[dict[str, list[dict]], list[str], set[tuple[str, str]]]:
    """Merges each pair of entries whose changed paths share a path.

    A pair is keyed by its two heads in label order. Its result is reused
    from ``cache`` while neither head moved, and a pair Git could not merge
    is not retried while neither head moved. New merges spend at most
    ``MAX_MERGES`` Git calls per pass, counting each ``merge-tree`` and each
    conflicted file's ``cat-file``; a merge runs only while its worst case
    of one plus ``MAX_FILES`` calls fits. The first merge Git cannot answer
    ends the pass's merging, since a Git older than 2.38 fails every one,
    and every pair it leaves untested is recorded as failed with it.

    Args:
        root: Base checkout.
        entries: Changes by label, each with ``head`` and ``paths``.
        cache: Conflicted files per head pair from an earlier pass.
        failed: Head pairs an earlier pass could not merge.
        objects: Supplies the private object store environment on first
            use, or None when there is none.

    Returns:
        The conflicted files per head pair tested or reused, empty for a
        clean merge; the head pairs that could not be merged; and the label
        pairs left untested, by the bound or by a failure.
    """
    merges: dict[str, list[dict]] = {}
    failures: list[str] = []
    untested: set[tuple[str, str]] = set()
    spent = 0
    stopped = False
    labels = [label for label in sorted(entries) if entries[label]["paths"]]
    for first, second in itertools.combinations(labels, 2):
        one, two = entries[first], entries[second]
        if not set(one["paths"]) & set(two["paths"]):
            continue
        key = f"{one['head']}..{two['head']}"
        files = cache.get(key)
        if files is None and key not in failed and not stopped:
            env = None if spent + 1 + MAX_FILES > MAX_MERGES else objects()
            if env is None:
                untested.add((first, second))
                continue
            tested = merge(root, one["head"], two["head"], env)
            spent += 1 + (len(tested["files"]) if tested else 0)
            stopped = tested is None
            if tested is not None:
                files = [] if tested["clean"] else tested["files"]
        if files is None:
            failures.append(key)
            untested.add((first, second))
            continue
        merges[key] = files
    return merges, failures, untested


def reading(
    root: str, manifest: dict, previous: dict, directory: Path
) -> dict | None:
    """Reads every working lane's branch change and tests overlapping pairs.

    The base is the head of the base checkout, which is the branch every
    lane merges back into. A retired lane is never read. A lane's fork point
    and changed paths are reused from ``previous`` while neither its head
    nor the base moved, and a pair's merge result is reused while neither
    head moved, so a cycle in which nothing moved costs two Git calls. A
    lane whose change Git cannot read keeps its previous reading for this
    pass, or is left out when it has none; the failure is not cached. Only
    a pair whose committed changes share a path is merged, since two lanes
    that touch disjoint paths cannot conflict line by line, within the
    ``MAX_MERGES`` call bound ``_test`` applies. A pair left untested keeps
    its previous predicted conflict, so its lanes are not told again. A
    pair Git could not merge is retried once either head or the base moves.
    Uncommitted worktree changes are not part of a branch and are not
    forecast.

    Args:
        root: Canonical project key, which is the base checkout's path.
        manifest: Project manifest naming the roster and each lane's branch.
        previous: The last published reading, or an empty mapping.
        directory: Private project state directory that holds the private
            object store for this pass.

    Returns:
        ``base``, the base head; ``lanes``, each lane's branch change by
        name; ``merges``, the conflicted files per tested head pair;
        ``failed``, the head pairs Git could not merge; and ``pairs``, one
        record per predicted conflict carrying ``lanes``, ``branches``,
        ``heads`` and ``files``, in lane-name order. None when Git cannot
        read the base or the branches.
    """
    with contextlib.ExitStack() as stack:
        objects = functools.cache(lambda: scratch(root, directory, stack))
        return _reading(root, manifest, previous, objects)


def _reading(
    root: str,
    manifest: dict,
    previous: dict,
    objects: Callable[[], dict[str, str] | None],
) -> dict | None:
    """Takes the reading ``reading`` describes with a given object store."""
    base = (_git(root, "rev-parse", "--verify", "HEAD") or "").strip()
    heads = _branch_heads(root) if base else None
    if heads is None:
        return None
    known = previous.get("lanes") or {}
    found: dict[str, dict] = {}
    for name, participant in sorted(manifest["participants"].items()):
        branch = participant.get("branch") or ""
        head = heads.get(branch)
        if roster.retired(participant) or not head:
            continue
        cached = known.get(name) or {}
        change = _change(root, base, head, cached)
        if change is None and cached.get("branch") == branch:
            change = cached
        if change is not None:
            found[name] = {**change, "branch": branch}
    merges, failed, untested = _test(
        root,
        found,
        previous.get("merges") or {},
        previous.get("failed") or [] if previous.get("base") == base else [],
        objects,
    )
    earlier = {
        tuple(pair.get("lanes") or ()): pair
        for pair in previous.get("pairs") or []
    }
    pairs = []
    for first, second in itertools.combinations(found, 2):
        one, two = found[first], found[second]
        files = merges.get(f"{one['head']}..{two['head']}")
        if (first, second) in untested and (first, second) in earlier:
            pairs.append(earlier[(first, second)])
        elif files and (first, second) not in untested:
            pairs.append(
                {
                    "lanes": [first, second],
                    "branches": [one["branch"], two["branch"]],
                    "heads": [one["head"], two["head"]],
                    "files": files,
                }
            )
    return {
        "base": base,
        "lanes": found,
        "merges": merges,
        "failed": failed,
        "pairs": pairs,
    }


def read(directory: Path) -> dict:
    """Reads the last published merge forecast, or an empty mapping."""
    try:
        published = json.loads((directory / MERGE_RECORD).read_text())
    except (OSError, ValueError):
        return {}
    return published if isinstance(published, dict) else {}


def files_text(files: list[dict]) -> str:
    """Names conflicted files with their line ranges, in one phrase."""
    return ", ".join(
        entry["path"]
        + (
            " lines "
            + ",".join(f"{start}-{end}" for start, end in entry["ranges"])
            if entry["ranges"]
            else ""
        )
        for entry in files
    )


def involving(published: dict, name: str) -> list[dict]:
    """Lists the predicted conflicts one lane is part of.

    Args:
        published: Merge forecast as ``read`` returns it.
        name: Participant whose conflicts are listed.

    Returns:
        One record per predicted conflict carrying the ``peer`` lane, the
        peer's ``branch`` and the conflicted ``files``.
    """
    found = []
    for pair in published.get("pairs") or []:
        lanes = pair.get("lanes") or []
        if name in lanes and len(lanes) == 2:
            index = 1 - lanes.index(name)
            found.append(
                {
                    "peer": lanes[index],
                    "branch": pair["branches"][index],
                    "files": pair["files"],
                }
            )
    return found


def marker(conflicts: list[dict]) -> str:
    """Describes a lane's predicted merge conflicts in one line."""
    if not conflicts:
        return ""
    return (
        "; ".join(
            f"merge with {entry['peer']} predicted to conflict in "
            + files_text(entry["files"])
            for entry in conflicts
        )
        + "; nothing was merged"
    )


def message(pair: dict, name: str) -> tuple[str, str]:
    """Composes the one notice a lane gets for a newly predicted conflict.

    Args:
        pair: Predicted conflict as ``reading`` reports it.
        name: Lane being told, one of the pair.

    Returns:
        The subject and body.
    """
    index = pair["lanes"].index(name)
    peer = pair["lanes"][1 - index]
    return (
        f"Merge conflict predicted with {peer}",
        f"Merging your branch {pair['branches'][index]} with {peer}'s "
        f"branch {pair['branches'][1 - index]} is predicted to conflict in "
        f"{files_text(pair['files'])}. The forecast merged the commits both "
        "branches hold now with git merge-tree; nothing was merged, rebased "
        f"or paused. Agree with {peer} which lands first, or run "
        "`agent-parley merge plan` for a recommended landing order. No "
        "further notice follows while this conflict stays predicted.",
    )


def supervise(
    home: Path, directory: Path, manifest: dict, config: dict
) -> None:
    """Runs one merge forecast pass and tells each newly conflicting pair.

    The reading is published to ``MERGE_RECORD`` in the project state
    directory, with the wall seconds it took, for ``status`` and ``top``. A
    pair is new when the previous publication did not predict it; it keeps
    the time it was first predicted, and each of its lanes is told once as
    operator mail keyed by that time, so a later pass, a retried pass or a
    moved head never repeats the notice. A pair that stops conflicting and
    later conflicts again is new again. The publication is written before
    any mail is sent, so a pass whose record cannot be written tells
    nobody. With prompts disabled the forecast is published and nobody is
    told. Git that cannot be read leaves the previous publication in place.

    Args:
        home: Private bridge state root.
        directory: Private project state directory.
        manifest: Current participant manifest.
        config: Resolved supervision settings.
    """
    from agent_parley import store

    started = time.monotonic()
    previous = read(directory)
    taken = reading(manifest["root"], manifest, previous, directory)
    if taken is None:
        return
    since = {
        tuple(pair.get("lanes") or ()): pair.get("since")
        for pair in previous.get("pairs") or []
    }
    now = time.time()
    fresh = []
    for pair in taken["pairs"]:
        first = since.get(tuple(pair["lanes"]))
        pair["since"] = first or now
        if not first:
            fresh.append(pair)
    taken["at"] = now
    taken["seconds"] = round(time.monotonic() - started, 3)
    try:
        write_json(directory / MERGE_RECORD, taken)
    except OSError:
        return
    if not config.get("prompts", True):
        return
    for pair in fresh:
        for name in pair["lanes"]:
            subject, body = message(pair, name)
            key = (
                f"merge-forecast:{':'.join(pair['lanes'])}:"
                f"{pair['since']!r}:{name}"
            )
            with contextlib.suppress(BridgeError, OSError, sqlite3.Error):
                store.speak(
                    home,
                    manifest["root"],
                    manifest["participants"][name]["display"],
                    subject,
                    body,
                    key,
                )


def _land(
    root: str, landed: str, head: str, tree: str, env: dict[str, str]
) -> str:
    """Writes the unreachable scratch commit a simulated landing leaves.

    The commit has both the landed base and the candidate as parents, so
    the next candidate's merge base is found from real ancestry. It is
    written with a fixed identity and date into the private object store
    ``env`` names and is reachable from no reference.

    Returns:
        The scratch commit, or an empty string when Git refused.
    """
    result = _run(
        root,
        "commit-tree",
        "--no-gpg-sign",
        "-p",
        landed,
        "-p",
        head,
        "-m",
        "merge forecast",
        tree,
        env={**env, **SCRATCH_IDENTITY},
    )
    return result[1].strip() if result and not result[0] else ""


def _candidates(
    root: str, taken: dict, ready: set[str], pulls: list[dict]
) -> tuple[dict[str, dict], list[dict]]:
    """Collects what could land: lanes with work and open pull requests.

    Returns:
        The candidates by label, each with ``lane``, ``branch``, ``head``,
        ``paths``, ``pull_request`` and ``ready``, and the open pull
        requests whose head branch is not available locally.
    """
    candidates = {
        name: {
            "lane": name,
            "branch": lane["branch"],
            "head": lane["head"],
            "paths": lane["paths"],
            "pull_request": None,
            "ready": name in ready,
        }
        for name, lane in taken["lanes"].items()
        if lane["paths"]
    }
    by_branch = {entry["branch"]: entry for entry in candidates.values()}
    heads = _branch_heads(root) or {}
    skipped = []
    for pull in pulls:
        branch = str(pull.get("branch") or "")
        number = pull.get("number")
        if branch in by_branch:
            by_branch[branch]["pull_request"] = number
            continue
        head = heads.get(branch)
        change = _change(root, taken["base"], head, {}) if head else None
        if not change or not change["paths"]:
            skipped.append(
                {
                    "pull_request": number,
                    "branch": branch,
                    "reason": "head branch not available locally"
                    if not head
                    else "Git could not read its change"
                    if change is None
                    else "nothing beyond the default branch",
                }
            )
            continue
        candidates[f"#{number}"] = {
            "lane": None,
            "branch": branch,
            "head": head,
            "paths": change["paths"],
            "pull_request": number,
            "ready": False,
        }
    return candidates, skipped


def plan(
    root: str,
    manifest: dict,
    ready: set[str],
    pulls: list[dict],
    directory: Path,
) -> dict:
    """Recommends a landing order that minimises predicted conflicts.

    Every lane with committed work beyond the default branch and every open
    pull request whose head is available locally is a candidate. Pairwise
    conflicts are tested first, reusing the published merge forecast's
    lane readings and merge results and spending at most ``MAX_MERGES`` Git
    calls on new pairwise merges; a pair over that bound counts as not
    conflicting. Landing is then simulated against the
    default branch as it stands: at each position the candidates still
    waiting are tried in order of how many other waiting candidates they
    conflict with, fewest first, an open pull request before a lane without
    one, a lane reporting ready before one that is not, then by name, and
    the first that merges cleanly onto the simulated branch takes the
    position. Its result becomes the simulated branch through an
    unreachable scratch commit. Candidates that merge cleanly onto nothing
    left are listed last, each with the files it would conflict in.
    Deferring the candidate that conflicts with the most others is what
    keeps the count of predicted conflicts low: landing it first would
    make every one of them conflict.

    The plan claims nothing, merges nothing, moves no reference, index or
    worktree, and writes its objects only into a private object store that
    is removed when it returns.

    Args:
        root: Canonical project key, which is the base checkout's path.
        manifest: Project manifest naming the roster and branches.
        ready: Participants whose latest report is ready.
        pulls: Open pull requests as supervision last cached them, each
            with ``number`` and head ``branch``.
        directory: Private project state directory holding the published
            merge forecast and the private object store.

    Returns:
        ``branch`` and ``base``, the default branch and its head; ``order``,
        one record per candidate in landing order carrying ``position``,
        ``lane``, ``pull_request``, ``branch``, ``ready``, ``clean``,
        ``files``, ``conflicts_with`` and ``reason``; ``conflicts``, how
        many candidates are predicted to conflict; and ``skipped``, the
        pull requests that could not be tested.

    Raises:
        BridgeError: If Git cannot read the base checkout or its branches,
            cannot open the private object store, or cannot test a merge.
    """
    with contextlib.ExitStack() as stack:
        objects = functools.cache(lambda: scratch(root, directory, stack))
        return _plan(root, manifest, ready, pulls, read(directory), objects)


def _plan(
    root: str,
    manifest: dict,
    ready: set[str],
    pulls: list[dict],
    published: dict,
    objects: Callable[[], dict[str, str] | None],
) -> dict:
    """Makes the plan ``plan`` describes with a given object store."""
    taken = _reading(root, manifest, published, objects)
    if taken is None:
        raise BridgeError(
            f"Git could not read the branches of {root}; no plan was made."
        )
    branch = (
        _git(root, "symbolic-ref", "--short", "-q", "HEAD") or "HEAD"
    ).strip()
    candidates, skipped = _candidates(root, taken, ready, pulls)
    merges, _, _ = _test(
        root, candidates, taken["merges"], taken["failed"], objects
    )
    collides: dict[str, set[str]] = {label: set() for label in candidates}
    for first, second in itertools.combinations(sorted(candidates), 2):
        one, two = candidates[first], candidates[second]
        if merges.get(f"{one['head']}..{two['head']}"):
            collides[first].add(second)
            collides[second].add(first)
    waiting = sorted(candidates)
    landed = taken["base"]
    above: list[str] = []
    order: list[dict] = []
    while waiting:
        pending = set(waiting)
        waiting.sort(
            key=lambda label: (
                len(collides[label] & pending),
                candidates[label]["pull_request"] is None,
                not candidates[label]["ready"],
                label,
            )
        )
        failed: dict[str, list[dict]] = {}
        chosen = ""
        for label in waiting:
            env = objects()
            if env is None:
                raise BridgeError(
                    "Git could not name the object store of "
                    f"{root}, or the private object store could not be "
                    "made; no plan was made."
                )
            head = candidates[label]["head"]
            tested = merge(root, landed, head, env)
            if tested is None:
                raise BridgeError(
                    f"Git could not test merging {candidates[label]['branch']}"
                    " with git merge-tree --write-tree, which needs Git 2.38 "
                    "or later; no plan was made."
                )
            if tested["clean"]:
                written = _land(root, landed, head, tested["tree"], env)
                if not written:
                    raise BridgeError(
                        "Git could not write the scratch commit that "
                        "simulates landing; no plan was made."
                    )
                landed, chosen = written, label
                break
            failed[label] = tested["files"]
        if not chosen:
            for label in waiting:
                order.append(
                    _entry(
                        candidates[label],
                        len(order) + 1,
                        False,
                        failed.get(label, []),
                        sorted(collides[label]),
                        _blocked(
                            branch, failed.get(label, []), collides[label]
                        ),
                    )
                )
            break
        waiting.remove(chosen)
        later = sorted(collides[chosen] & set(waiting))
        order.append(
            _entry(
                candidates[chosen],
                len(order) + 1,
                True,
                [],
                sorted(collides[chosen]),
                _landed(branch, above, later),
            )
        )
        above.append(chosen)
    return {
        "branch": branch,
        "base": taken["base"],
        "order": order,
        "conflicts": sum(1 for entry in order if not entry["clean"]),
        "skipped": skipped,
    }


def _entry(
    candidate: dict,
    position: int,
    clean: bool,
    files: list[dict],
    peers: list[str],
    reason: str,
) -> dict:
    """Builds one landing-order record from a candidate."""
    return {
        "position": position,
        "lane": candidate["lane"],
        "pull_request": candidate["pull_request"],
        "branch": candidate["branch"],
        "ready": candidate["ready"],
        "clean": clean,
        "files": files,
        "conflicts_with": peers,
        "reason": reason,
    }


def _landed(branch: str, above: list[str], later: list[str]) -> str:
    """Says why a candidate that merges cleanly takes its position."""
    onto = f"{branch} and {', '.join(above)}" if above else branch
    if not later:
        return (
            f"merges cleanly onto {onto} and conflicts with nothing still "
            "waiting"
        )
    return (
        f"merges cleanly onto {onto}; it conflicts with {', '.join(later)}, "
        "the fewest conflicts among the candidates that merge cleanly, so "
        "those rebase after it lands"
    )


def _blocked(branch: str, files: list[dict], peers: set[str]) -> str:
    """Says why a candidate is predicted to conflict wherever it lands."""
    where = f" in {files_text(files)}" if files else ""
    after = (
        f"; rebase it after {', '.join(sorted(peers))} land"
        if peers
        else f"; rebase it onto {branch}"
    )
    return (
        f"predicted to conflict with {branch} after the positions above"
        f"{where}{after}"
    )


def project_plan(directory: Path, ready: set[str], record: Path) -> dict:
    """Plans the landing order for one project from its recorded state.

    Args:
        directory: Private project state directory.
        ready: Participants whose latest report is ready.
        record: Supervision's cached open pull request record.

    Returns:
        The plan as ``plan`` reports it.
    """
    manifest = roster.read(directory)
    try:
        cached = json.loads(record.read_text())
    except (OSError, ValueError):
        cached = {}
    pulls = cached.get("pull_requests") if isinstance(cached, dict) else {}
    readings = [
        pull
        for pull in (pulls.values() if isinstance(pulls, dict) else [])
        if isinstance(pull, dict)
    ]
    return plan(manifest["root"], manifest, ready, readings, directory)


def render_plan(planned: dict) -> str:
    """Renders a landing plan as terminal text, one line per position."""
    lines = [
        f"Landing order onto {planned['branch']} at "
        f"{planned['base'][:12]}; this claims and merges nothing."
    ]
    if not planned["order"]:
        lines.append("No open pull request or lane has work to land.")
    for entry in planned["order"]:
        label = entry["lane"] or f"pull request #{entry['pull_request']}"
        if entry["lane"] and entry["pull_request"]:
            label += f" (pull request #{entry['pull_request']})"
        if entry["ready"]:
            label += ", ready"
        lines.append(
            f"{entry['position']}. {label} on {entry['branch']}: "
            f"{entry['reason']}"
        )
    for entry in planned["skipped"]:
        lines.append(
            f"Not planned: pull request #{entry['pull_request']} on "
            f"{entry['branch']}, {entry['reason']}."
        )
    lines.append(f"Predicted conflicts in this order: {planned['conflicts']}.")
    return "\n".join(lines)
