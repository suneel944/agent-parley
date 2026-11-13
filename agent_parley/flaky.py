"""Flags a test suspected flaky once lanes see it flip on unchanged code.

A failing test stops a lane whether its change broke the test or the test
fails on its own now and then, and nothing told the two apart: each lane
that met a flaky test spent its own turns on it. This module keeps the
occurrences lanes record and names a test suspected flaky only on that
recorded evidence.

An occurrence is one test failing or passing on one Git tree in one lane,
from one of two sources. A lane's report evidence is read from its durable
report log (`report`), and a lane's pull request is read from the forge
(`forge`): the failed-step logs the poll already quotes to the lane on red
checks, and green checks. Test identifiers are pytest node ids,
`path.py::name`, read from `FAILED <id>`, `<id> FAILED`, the same two forms
with `ERROR`, and `PASSED <id>` or `<id> PASSED` for a pass; terminal colour
codes are removed first and nothing else is parsed. Checks that finished
green pass every test already tracked on that tree. Report evidence passes
only the tests it names as passed, because a report cannot show whether it
ran the whole suite or one focused file. A report counts only when its
worktree matched its commit: a ready report always does, since `report`
refuses one that differs, and any other report counts when it recorded
`clean`. Evidence spilled to an attachment is read from its retained
preview only.

A test is suspected flaky when it failed and passed on the same tree, in
either order, or when it failed in two lanes whose diffs change neither the
test file nor a module it imports. A lane's diff is its commit against the
point it forked from the base checkout's head; a Python module matches an
import when its dotted path equals the imported name or ends with it, and a
diff Git cannot read counts as changing the test. A failure whose diff
changes the test or its imports never counts towards two lanes, so a real
regression is not marked.

On a new suspicion every lane that is not retired is told once, by keyed
mail naming the test and up to `MAX_SHOWN` occurrences, and one `test:`
issue is suggested through `followups.suggest` for the operator to accept;
the lane files nothing, and no test is skipped or disabled. More
occurrences of a suspected test send nothing more. A later failure on a
tree outside the suspicion, whose diff changes the test or its imports,
clears the suspicion and drops the occurrences before it, so the test is
suspected again only on new evidence and under a new key.
"""

from __future__ import annotations

import ast
import contextlib
import hashlib
import json
import re
import sqlite3
import subprocess
import time
from pathlib import Path

from agent_parley import roster, worktrees
from agent_parley.state import BridgeError, lock, write_json

PUBLICATION = "flaky.json"
LOCK = "flaky.lock"
FAIL = "fail"
PASS = "pass"
REPORT = "report"
FORGE = "forge"
SAME_TREE = "failed and passed on the same tree"
TWO_LANES = "failed in two lanes whose diffs change neither it nor its imports"
MAX_TESTS = 256
MAX_EVENTS = 32
MAX_IDS = 256
MAX_SHOWN = 6
MAX_TEST = 300
MAX_TITLE_TEST = 160
MAX_PER_TEXT = 50
SLACK = 60.0

_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_NODE = r"(?:[\w.-]+/)*[\w.-]+\.py::\S+"
_FAILED = re.compile(
    rf"(?<![\w-])(?:FAILED|ERROR)\s+({_NODE})"
    rf"|({_NODE})\s+(?:FAILED|ERROR)(?![\w-])"
)
_PASSED = re.compile(
    rf"(?<![\w-])PASSED\s+({_NODE})|({_NODE})\s+PASSED(?![\w-])"
)


def _named(pattern: re.Pattern[str], text: str) -> list[str]:
    """Lists the distinct node ids one pattern finds, in order, bounded."""
    names: list[str] = []
    for match in pattern.finditer(text):
        name = (match.group(1) or match.group(2)).rstrip(",;:")[:MAX_TEST]
        if name not in names:
            names.append(name)
    return names[:MAX_PER_TEXT]


def tests_in(text: str) -> tuple[list[str], list[str]]:
    """Reads the failing and passing test identifiers evidence names.

    Args:
        text: Report evidence or a quoted failed-step log.

    Returns:
        The failing and the passing pytest node ids, each in order of first
        appearance and at most `MAX_PER_TEXT` long.
    """
    plain = _ESCAPE.sub("", text)
    return _named(_FAILED, plain), _named(_PASSED, plain)


def _git(root: Path, *args: str) -> str | None:
    """Runs Git in the base checkout, or returns None when it fails."""
    try:
        return worktrees.git(root, *args)
    except (BridgeError, OSError, subprocess.TimeoutExpired):
        return None


def _changed(root: Path, commit: str) -> list[str] | None:
    """Lists the paths a commit changed since its fork from the base head."""
    fork = _git(root, "merge-base", "HEAD", commit)
    if fork is None:
        return None
    found = _git(root, "diff", "--name-only", "--no-renames", fork, commit)
    return None if found is None else found.splitlines()


def imports(source: str, path: str) -> set[str]:
    """Names the modules a Python file imports, relative ones resolved.

    Args:
        source: The file's text.
        path: The file's repository path, which anchors relative imports.

    Returns:
        Dotted module names, each `from` import also adding the imported
        name under its module; empty when the text does not parse.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return set()
    package = path.split("/")[:-1]
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level - 1 > len(package):
                continue
            kept = len(package) - node.level + 1
            base = package[:kept] if node.level else []
            parts = [*base, *(node.module or "").split(".")]
            module = ".".join(part for part in parts if part)
            if module:
                names.add(module)
            names.update(
                f"{module}.{alias.name}" if module else alias.name
                for alias in node.names
            )
    return names


def touches(changed: list[str], test: str, imported: set[str]) -> bool:
    """Reports whether a diff changes a test file or a module it imports.

    Args:
        changed: Repository paths the diff changes.
        test: The test's node id.
        imported: Modules the test file imports, from `imports`.

    Returns:
        True when the diff changes the test file itself, a ``conftest.py``
        in its directory or a parent, whose fixtures the test can use, or
        a Python file whose dotted module path equals an imported name or
        ends with one.
    """
    path = test.split("::", 1)[0]
    folder = path.rpartition("/")[0]
    for item in changed:
        if item == path:
            return True
        parent, _, leaf = item.rpartition("/")
        if leaf == "conftest.py" and (
            not parent or folder == parent or folder.startswith(f"{parent}/")
        ):
            return True
        if not item.endswith(".py"):
            continue
        module = item.removesuffix(".py").removesuffix("/__init__")
        dotted = module.replace("/", ".")
        if any(
            dotted == name or dotted.endswith(f".{name}") for name in imported
        ):
            return True
    return False


def _empty() -> dict:
    """Returns the record of a project with nothing recorded yet."""
    return {"version": 1, "tests": {}, "reports": {}}


def read(directory: Path) -> dict:
    """Reads the project's recorded occurrences and suspicions.

    Args:
        directory: Private project state directory.

    Returns:
        The record, with `tests` keyed by node id and `reports` holding
        each lane's report-log cursor; empty when missing or damaged.
    """
    try:
        found = json.loads((directory / PUBLICATION).read_text())
    except (OSError, ValueError):
        return _empty()
    if not isinstance(found, dict) or not isinstance(found.get("tests"), dict):
        return _empty()
    if not isinstance(found.get("reports"), dict):
        found["reports"] = {}
    found["tests"] = {
        name: entry for name, entry in found["tests"].items() if _sound(entry)
    }
    return found


def _sound(entry: object) -> bool:
    """Reports whether one test's stored record has every field read.

    A damaged entry is dropped rather than raised from, so one bad record
    cannot stop the poll that records every lane's pull request.
    """
    if not isinstance(entry, dict) or not isinstance(entry.get("events"), list):
        return False
    fields = ("lane", "outcome", "tree", "source")
    if not all(
        isinstance(item, dict)
        and isinstance(item.get("at"), int | float)
        and isinstance(item.get("touches"), bool)
        and all(isinstance(item.get(field), str) for field in fields)
        for item in entry["events"]
    ):
        return False
    held = entry.get("suspected")
    return held is None or (
        isinstance(held, dict)
        and isinstance(held.get("reason"), str)
        and isinstance(held.get("evidence"), list)
        and isinstance(held.get("told"), list)
        and isinstance(held.get("trees"), list)
        and isinstance(held.get("followup"), str)
        and isinstance(held.get("since"), int | float)
    )


def _latest(entry: dict) -> float:
    """Names when one test last gained an occurrence."""
    return max((float(item["at"]) for item in entry["events"]), default=0.0)


def record(
    directory: Path,
    root: Path,
    lane: str,
    commit: str,
    text: str,
    source: str,
    green: bool = False,
    now: float = 0.0,
) -> int:
    """Records the occurrences one run of a lane's tests shows.

    A failure keeps whether the lane's diff changes the test or a module it
    imports. A pass is kept only for a test already tracked or failing in
    the same text, so passing output never grows the record. An occurrence
    already recorded for the same lane, outcome, tree and source is not
    recorded again.

    Args:
        directory: Private project state directory.
        root: Base checkout, whose object store holds every lane's commits.
        lane: Participant whose run it was.
        commit: Commit the run tested.
        text: Evidence or failed-step log text.
        source: `REPORT` or `FORGE`.
        green: Whether the forge reported every check green.
        now: Unix time of the run; the clock when zero.

    Returns:
        How many new occurrences were recorded.
    """
    stamp = now or time.time()
    failed, passed = tests_in(text)
    whole = green and not failed
    tracked = set(read(directory)["tests"])
    passing = [name for name in passed if name in tracked or name in failed]
    if whole:
        passing.extend(sorted(tracked - set(passing) - set(failed)))
    if not failed and not passing:
        return 0
    tree = _git(root, "rev-parse", f"{commit}^{{tree}}")
    if not tree:
        return 0
    changed = _changed(root, commit) if failed else []
    sources: dict[str, set[str]] = {}
    events: list[tuple[str, dict]] = []
    for name in failed:
        path = name.split("::", 1)[0]
        if path not in sources:
            shown = _git(root, "show", f"{commit}:{path}")
            sources[path] = imports(shown or "", path)
        events.append(
            (
                name,
                {
                    "lane": lane,
                    "outcome": FAIL,
                    "tree": tree,
                    "commit": commit,
                    "source": source,
                    "at": stamp,
                    "touches": changed is None
                    or touches(changed, name, sources[path]),
                },
            )
        )
    events.extend(
        (
            name,
            {
                "lane": lane,
                "outcome": PASS,
                "tree": tree,
                "commit": commit,
                "source": source,
                "at": stamp,
                "touches": False,
            },
        )
        for name in passing
    )
    added = 0
    with lock(directory / LOCK, timeout=1):
        whole_record = read(directory)
        tests = whole_record["tests"]
        for name, event in events:
            entry = tests.setdefault(name, {"events": [], "suspected": None})
            if any(
                all(
                    known.get(field) == event[field]
                    for field in ("lane", "outcome", "tree", "source")
                )
                for known in entry["events"]
            ):
                continue
            entry["events"] = [*entry["events"], event][-MAX_EVENTS:]
            added += 1
        if added:
            if len(tests) > MAX_TESTS:
                whole_record["tests"] = dict(
                    sorted(tests.items(), key=lambda item: _latest(item[1]))[
                        -MAX_TESTS:
                    ]
                )
            write_json(directory / PUBLICATION, whole_record)
    return added


def suspicion(events: list[dict]) -> tuple[str, list[dict]]:
    """Decides whether recorded occurrences make a test suspected flaky.

    Args:
        events: One test's occurrences.

    Returns:
        The reason, `SAME_TREE` or `TWO_LANES`, with the occurrences behind
        it, or an empty reason and list when neither holds.
    """
    failures = [item for item in events if item["outcome"] == FAIL]
    passed = {item["tree"] for item in events if item["outcome"] == PASS}
    flipped = next(
        (item["tree"] for item in failures if item["tree"] in passed), ""
    )
    if flipped:
        return SAME_TREE, [item for item in events if item["tree"] == flipped]
    untouched = [item for item in failures if not item["touches"]]
    if len({item["lane"] for item in untouched}) >= 2:
        return TWO_LANES, untouched
    return "", []


def assess(entry: dict, now: float) -> dict:
    """Clears or raises one test's suspicion from its occurrences.

    Args:
        entry: One test's record.
        now: Unix time a new suspicion is stamped with.

    Returns:
        The updated record; the input is not changed.
    """
    current = dict(entry)
    held = current.get("suspected")
    if held:
        trees = set(held["trees"])
        clearing = next(
            (
                item
                for item in current["events"]
                if item["outcome"] == FAIL
                and item["touches"]
                and float(item["at"]) > float(held["since"])
                and item["tree"] not in trees
            ),
            None,
        )
        if clearing is None:
            return current
        current["events"] = [
            item
            for item in current["events"]
            if float(item["at"]) >= float(clearing["at"])
        ]
        current["suspected"] = None
        current["cleared"] = clearing["at"]
    reason, evidence = suspicion(current["events"])
    if reason:
        current["suspected"] = {
            "since": now,
            "reason": reason,
            "trees": sorted({item["tree"] for item in evidence}),
            "evidence": evidence[-MAX_SHOWN:],
            "told": [],
            "followup": "",
        }
    return current


def _occurrence(item: dict) -> str:
    """Renders one occurrence as one evidence line."""
    verb = "failed" if item["outcome"] == FAIL else "passed"
    return (
        f"- {item['lane']}: {verb} at commit {item['commit'][:12]} "
        f"(tree {item['tree'][:12]}) from its {item['source']}"
    )


def message(test: str, held: dict) -> tuple[str, str]:
    """Composes the bounded update each lane receives once per suspicion.

    Args:
        test: The suspected test's node id.
        held: The suspicion `assess` recorded.

    Returns:
        The subject and body, naming the test, the reason, up to
        `MAX_SHOWN` occurrences and the suggested follow-up.
    """
    lines = "\n".join(_occurrence(item) for item in held["evidence"])
    pending = (
        f"Suggestion {held['followup']} for a `test:` issue waits for the "
        "operator in `agent-parley followup list`; do not file it yourself."
        if held["followup"]
        else "No `test:` suggestion could be recorded yet; name the test in "
        "your report instead of filing an issue."
    )
    return (
        f"Suspected flaky test {test[:MAX_TITLE_TEST]}",
        f"Test {test} is suspected flaky: it {held['reason']}.\n"
        f"Evidence:\n{lines}\n"
        "This is a label with evidence: no test was skipped or disabled. "
        f"{pending} A later "
        "failure on a tree whose diff changes the test or its imports "
        "clears the suspicion.",
    )


def _suggest(directory: Path, test: str, held: dict) -> str:
    """Records the suspicion's `test:` follow-up, or returns '' on refusal."""
    from agent_parley import followups

    path = test.split("::", 1)[0]
    lines = "\n".join(_occurrence(item) for item in held["evidence"])
    try:
        made = followups.suggest(
            directory,
            str(held["evidence"][0]["lane"]),
            f"test: investigate suspected flaky {test[:MAX_TITLE_TEST]}",
            f"Suspected flaky: it {held['reason']}. Occurrences:\n{lines}",
            [path],
        )
    except BridgeError:
        return ""
    return str(made.get("id") or "")


def _reports(directory: Path, root: Path, lanes: list[str]) -> None:
    """Records the occurrences in each lane's new clean report records."""
    from agent_parley import metrics

    cursors = read(directory)["reports"]
    for lane in lanes:
        kept = cursors.get(lane)
        cursor: dict = kept if isinstance(kept, dict) else {}
        floor = float(cursor.get("at") or 0.0) - SLACK
        known = list(cursor.get("ids") or [])
        newest = float(cursor.get("at") or 0.0)
        for item in metrics.report_records(directory, lane, max(floor, 0.0)):
            ident = str(item.get("id") or "")
            if not ident or ident in known:
                continue
            known.append(ident)
            at = float(item.get("at") or 0.0)
            newest = max(newest, at)
            commit = str(item.get("commit") or "")
            if (
                item.get("kind") != "report"
                or not commit
                or not (item.get("state") == "ready" or item.get("clean"))
            ):
                continue
            record(
                directory,
                root,
                lane,
                commit,
                str(item.get("evidence") or ""),
                REPORT,
                now=at,
            )
        cursors[lane] = {"at": newest, "ids": known[-MAX_IDS:]}
    with lock(directory / LOCK, timeout=1):
        whole = read(directory)
        if whole["reports"] != cursors:
            whole["reports"] = cursors
            write_json(directory / PUBLICATION, whole)


def supervise(home: Path, directory: Path, manifest: dict) -> None:
    """Runs one flaky-test pass and tells each lane of a new suspicion.

    Each lane's new report records are read first. Every tracked test is
    then assessed; a suspicion's follow-up is suggested until one is
    recorded, and each lane that is not retired is told once, under an
    idempotency key naming the test, the suspicion's time and the lane. A
    send that fails leaves only that lane untold, and the next pass
    retries it alone.

    Args:
        home: Private bridge state root.
        directory: Private project state directory.
        manifest: Current participant manifest.
    """
    from agent_parley import store

    root = Path(manifest["root"])
    participants = {
        name: participant
        for name, participant in manifest["participants"].items()
        if not roster.retired(participant)
    }
    _reports(directory, root, list(manifest["participants"]))
    now = time.time()
    with lock(directory / LOCK, timeout=1):
        whole = read(directory)
        tests = whole["tests"]
        for test, entry in sorted(tests.items()):
            current = assess(entry, now)
            held = current.get("suspected")
            if held and not held["followup"]:
                held["followup"] = _suggest(directory, test, held)
            if held:
                digest = hashlib.sha256(
                    f"{test}|{held['since']}".encode()
                ).hexdigest()[:16]
                subject, body = message(test, held)
                for lane, participant in participants.items():
                    if lane in held["told"]:
                        continue
                    with contextlib.suppress(
                        BridgeError, OSError, sqlite3.Error
                    ):
                        store.speak(
                            home,
                            manifest["root"],
                            participant.get("display", lane),
                            subject,
                            body,
                            f"flaky:{digest}:{lane}",
                        )
                        held["told"] = [*held["told"], lane]
            tests[test] = current
        write_json(directory / PUBLICATION, whole)
