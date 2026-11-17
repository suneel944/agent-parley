"""Project policy rules that gate coordination steps.

A team's rules for its lanes, such as a large diff needing a review or a fix
needing a regression test, otherwise live in one operator's private hooks or
in prose a lane may skip. An optional policy file, `FILE_NAME` in the
project's private state directory, declares them as data so they are shared
with the project, shown by ``policy check`` and portable to a second
operator. The file is TOML read with the standard library, and the operator
writes it there with ``policy import PATH`` from a path of their choosing.

Rules are declarative on purpose: a cloned repository can never run code
through them, and a reviewer can read every rule. A rule only refuses a
coordination step the service already sees, `READY` (a ready report) or
`RESOLVE` (an operator completing a claim). It never grants a permission,
never approves a native CLI prompt and never adds a bypass flag. No file
means no rule, so behavior is unchanged.

Three kinds of rule exist. `REVIEW` needs a returned review round on the
claim once the lane's diff changes more than ``lines`` lines. `REVIEWER`
needs a returned round from the named ``lane`` once a changed path matches
one of the ``paths`` globs. `TEST` needs a changed file matching one of the
``tests`` globs once a commit subject on the lane's branch starts with
``title``. Globs use `fnmatch` rules, so ``*`` also crosses ``/``.

The file is checked strictly when it is loaded. A malformed file, an unknown
key, a missing key or a value of the wrong type is refused with its line and
key, never ignored, so a typo cannot silently disable a rule.
"""

from __future__ import annotations

import fnmatch
import os
import re
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING

from agent_parley import reviews, roster
from agent_parley.state import BridgeError
from agent_parley.worktrees import git

if TYPE_CHECKING:
    from agent_parley.cli import Bridge

FILE_NAME = "policy.toml"
READY = "ready"
RESOLVE = "resolve"
EVENTS = (READY, RESOLVE)
REVIEW = "review"
REVIEWER = "reviewer"
TEST = "test"
KEYS = {
    REVIEW: frozenset({"lines"}),
    REVIEWER: frozenset({"paths", "lane"}),
    TEST: frozenset({"title", "tests"}),
}
COMMON = frozenset({"name", "kind", "on"})
MAX_BYTES = 64 * 1024
MAX_RULES = 50
MAX_PATTERNS = 20
MAX_TEXT = 200
SHOWN_PATHS = 3
HEADER = re.compile(r"^\s*\[\[\s*rule\s*\]\]")
TABLE = re.compile(r"^\s*\[")
POSITION = re.compile(r"at line (\d+)")


def _line(text: str, key: str, block: int) -> int:
    """Finds the line a key is written on, for a refusal.

    Args:
        text: Policy file text.
        key: Key to find.
        block: One-based ``[[rule]]`` table the key belongs to, or zero for
            a top-level key.

    Returns:
        The one-based line of the key, else of the rule's header, else 1.
    """
    lines = text.splitlines()
    start = 0
    if block:
        found = [n for n, line in enumerate(lines) if HEADER.match(line)]
        start = found[block - 1] if len(found) >= block else 0
    written = re.compile(rf"^\s*\[*\s*[\"']?{re.escape(key)}[\"']?\s*(=|\]|\.)")
    for number in range(start, len(lines)):
        if block and number > start and TABLE.match(lines[number]):
            break
        if written.match(lines[number]):
            return number + 1
    return start + 1


def _patterns(value: object) -> tuple[str, ...] | None:
    """Reads a nonempty list of glob patterns.

    Args:
        value: Value written in the file.

    Returns:
        The patterns, or None when the value is not a valid list.
    """
    if (
        not isinstance(value, list)
        or not 0 < len(value) <= MAX_PATTERNS
        or not all(
            isinstance(item, str) and 0 < len(item.strip()) <= MAX_TEXT
            for item in value
        )
    ):
        return None
    return tuple(item.strip() for item in value)


def _text(value: object) -> str | None:
    """Reads a nonempty bounded string.

    Args:
        value: Value written in the file.

    Returns:
        The stripped text, or None when the value is not valid.
    """
    if isinstance(value, str) and 0 < len(value.strip()) <= MAX_TEXT:
        return value.strip()
    return None


def _value(key: str, value: object) -> object:
    """Validates one rule value.

    Args:
        key: Rule key.
        value: Value written in the file.

    Returns:
        The validated value, or None when it is not valid.
    """
    if key == "on":
        if (
            isinstance(value, list)
            and value
            and all(item in EVENTS for item in value)
        ):
            return tuple(dict.fromkeys(value))
        return None
    if key == "lines":
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value if value >= 0 else None
    if key in ("paths", "tests"):
        return _patterns(value)
    return _text(value)


def _rule(text: str, source: str, block: int, entry: dict) -> dict:
    """Validates one ``[[rule]]`` table.

    Args:
        text: Policy file text, for line numbers.
        source: File named in a refusal.
        block: One-based position of the table.
        entry: The parsed table.

    Returns:
        The rule with ``name``, ``kind``, ``on`` and its kind's keys.

    Raises:
        BridgeError: If a key is unknown, missing or of the wrong value.
    """
    kind = entry.get("kind")
    if kind not in KEYS:
        raise _refused(
            text, source, block, "kind", "must be one of " + ", ".join(KEYS)
        )
    allowed = COMMON | KEYS[str(kind)]
    for key in entry:
        if key not in allowed:
            raise _refused(
                text,
                source,
                block,
                key,
                f"is unknown for a {kind} rule, which takes "
                + ", ".join(sorted(allowed)),
            )
    rule: dict = {"kind": kind, "on": (READY,)}
    for key in sorted(allowed - {"kind"}):
        if key not in entry:
            if key == "on":
                continue
            raise _refused(text, source, block, key, "is missing")
        value = _value(key, entry[key])
        if value is None:
            raise _refused(text, source, block, key, _expected(key))
        rule[key] = value
    return rule


def _refused(
    text: str, source: str, block: int, key: str, problem: str
) -> BridgeError:
    """Builds the refusal of one rule key, naming its line.

    Args:
        text: Policy file text, for line numbers.
        source: File named in the refusal.
        block: One-based position of the rule table.
        key: Rule key at fault.
        problem: What is wrong with it.

    Returns:
        The error to raise.
    """
    return BridgeError(
        f"{source} line {_line(text, key, block)}: rule {block} key "
        f"{key!r} {problem}; the policy is refused."
    )


def _expected(key: str) -> str:
    """Describes the value a key takes, for a refusal.

    Args:
        key: Rule key.

    Returns:
        What the key must hold.
    """
    if key == "on":
        return "must be a nonempty list of " + ", ".join(EVENTS)
    if key == "lines":
        return "must be a whole number of at least 0"
    if key in ("paths", "tests"):
        return (
            f"must be a list of 1 to {MAX_PATTERNS} nonempty glob strings "
            f"of at most {MAX_TEXT} characters"
        )
    return f"must be a nonempty string of at most {MAX_TEXT} characters"


def parse(content: bytes, source: str) -> list[dict]:
    """Parses and validates a policy file strictly.

    Args:
        content: File bytes.
        source: File named in a refusal.

    Returns:
        The rules in file order.

    Raises:
        BridgeError: If the file is too large, not UTF-8, not valid TOML,
            holds anything but ``[[rule]]`` tables, or any rule is invalid.
            The refusal names the line and key.
    """
    if len(content) > MAX_BYTES:
        raise BridgeError(f"{source} is larger than {MAX_BYTES} bytes.")
    try:
        text = content.decode()
    except UnicodeDecodeError:
        raise BridgeError(f"{source} is not UTF-8 text.") from None
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        place = POSITION.search(str(error))
        line = place.group(1) if place else len(text.splitlines()) or 1
        raise BridgeError(
            f"{source} line {line}: not valid TOML: {error}; the policy is "
            "refused."
        ) from None
    for key in document:
        if key != "rule":
            raise BridgeError(
                f"{source} line {_line(text, key, 0)}: key {key!r} is "
                "unknown; a policy holds only [[rule]] tables, so it is "
                "refused."
            )
    listed = document.get("rule", [])
    if not isinstance(listed, list) or not all(
        isinstance(entry, dict) for entry in listed
    ):
        raise BridgeError(
            f"{source} line {_line(text, 'rule', 0)}: key 'rule' must be "
            "[[rule]] tables; the policy is refused."
        )
    if len(listed) > MAX_RULES:
        raise BridgeError(f"{source} holds more than {MAX_RULES} rules.")
    rules: list[dict] = []
    for block, entry in enumerate(listed, start=1):
        rule = _rule(text, source, block, entry)
        if any(rule["name"] == other["name"] for other in rules):
            raise BridgeError(
                f"{source} line {_line(text, 'name', block)}: rule {block} "
                f"key 'name' repeats {rule['name']!r}; the policy is refused."
            )
        rules.append(rule)
    return rules


def load(directory: Path) -> list[dict]:
    """Reads the project's policy, or no rules when it has none.

    Args:
        directory: Private project state directory.

    Returns:
        The validated rules, empty when no policy file exists.

    Raises:
        BridgeError: If the file exists but cannot be read or is invalid.
    """
    path = directory / FILE_NAME
    try:
        content = path.read_bytes()
    except FileNotFoundError:
        return []
    except OSError as error:
        raise BridgeError(f"{path} cannot be read: {error}") from None
    return parse(content, str(path))


def facts(
    directory: Path,
    root: Path,
    lane: Path,
    *,
    builder: str,
    issue: str,
    claim: str,
) -> dict:
    """Reads the evidence rules are judged on for one lane's claim.

    The diff runs from the merge base of the base checkout's HEAD to the
    lane's worktree, the way `context.lane_diffstat` counts it. Commit
    subjects are those on the lane's branch since it forked.

    Args:
        directory: Private project state directory.
        root: Base checkout.
        lane: The lane's worktree.
        builder: Lane name holding the claim.
        issue: Claimed issue number.
        claim: Claim identifier, or empty to accept any round on the issue.

    Returns:
        ``files`` changed, total changed ``lines``, commit ``subjects`` and
        the ``reviewers`` of every returned review round on the claim.

    Raises:
        BridgeError: If Git cannot read the lane or the base, so the rules
            cannot be judged.
    """
    try:
        base = git(root, "rev-parse", "HEAD")
        listing = git(
            lane, "diff", "--numstat", "--no-renames", "--merge-base", base
        )
        subjects = git(lane, "log", "--format=%s", f"{base}..HEAD")
    except (BridgeError, OSError) as error:
        raise BridgeError(
            f"Project policy cannot read the work of {builder} at {lane}: "
            f"{error}"
        ) from None
    files = []
    lines = 0
    for row in listing.splitlines():
        added, _, rest = row.partition("\t")
        deleted, _, path = rest.partition("\t")
        if not path:
            continue
        files.append(path)
        lines += sum(int(n) for n in (added, deleted) if n.isdigit())
    reviewers = [
        str(entry.get("reviewer"))
        for entry in reviews.rounds(directory, issue=issue, lane=builder)
        if entry.get("builder") == builder
        and entry.get("state") == reviews.RETURNED
        and (not claim or entry.get("claim_id") == claim)
    ]
    return {
        "files": files,
        "lines": lines,
        "subjects": subjects.splitlines(),
        "reviewers": reviewers,
    }


def _matching(paths: list[str], patterns: tuple[str, ...]) -> list[str]:
    """Lists the paths any pattern matches.

    Args:
        paths: Changed paths.
        patterns: Glob patterns.

    Returns:
        The matching paths, in order.
    """
    return [
        path
        for path in paths
        if any(fnmatch.fnmatchcase(path, glob) for glob in patterns)
    ]


def _missing(rule: dict, seen: dict) -> str:
    """Names the evidence one rule lacks.

    Args:
        rule: Validated rule.
        seen: Evidence `facts` read.

    Returns:
        What is missing, or an empty string when the rule passes.
    """
    if rule["kind"] == REVIEW:
        if seen["lines"] <= rule["lines"] or seen["reviewers"]:
            return ""
        return (
            f"{seen['lines']} changed lines exceed {rule['lines']} and no "
            "review round on this claim has returned findings"
        )
    if rule["kind"] == REVIEWER:
        matched = _matching(seen["files"], rule["paths"])
        if not matched or rule["lane"] in seen["reviewers"]:
            return ""
        shown = ", ".join(matched[:SHOWN_PATHS])
        more = len(matched) - SHOWN_PATHS
        return (
            f"{shown}{f' and {more} more' if more > 0 else ''} match "
            f"{', '.join(rule['paths'])} but {rule['lane']} returned no "
            "review round on this claim"
        )
    titled = [
        subject
        for subject in seen["subjects"]
        if subject.startswith(rule["title"])
    ]
    if not titled or _matching(seen["files"], rule["tests"]):
        return ""
    return (
        f"commit {titled[0]!r} starts with {rule['title']!r} but no changed "
        f"file matches {', '.join(rule['tests'])}"
    )


def evaluate(rules: list[dict], seen: dict) -> list[dict]:
    """Judges every rule against one claim's evidence.

    Args:
        rules: Validated rules.
        seen: Evidence `facts` read.

    Returns:
        One ``{"name", "kind", "on", "passed", "missing"}`` per rule.
    """
    results = []
    for rule in rules:
        missing = _missing(rule, seen)
        results.append(
            {
                "name": rule["name"],
                "kind": rule["kind"],
                "on": list(rule["on"]),
                "passed": not missing,
                "missing": missing,
            }
        )
    return results


def gate(
    directory: Path,
    root: Path,
    lane: Path,
    *,
    event: str,
    builder: str,
    issue: str,
    claim: str,
) -> None:
    """Refuses a coordination step that a project rule fails.

    Nothing is read beyond the policy file when no rule applies to the
    event, so a project without a policy pays nothing.

    Args:
        directory: Private project state directory.
        root: Base checkout.
        lane: The lane's worktree.
        event: `READY` or `RESOLVE`.
        builder: Lane name holding the claim.
        issue: Claimed issue number.
        claim: Claim identifier, or empty.

    Raises:
        BridgeError: If the policy is invalid, its evidence cannot be read,
            or a rule fails; the refusal names each failed rule and the
            evidence it lacks.
    """
    applying = [rule for rule in load(directory) if event in rule["on"]]
    if not applying:
        return
    seen = facts(
        directory, root, lane, builder=builder, issue=issue, claim=claim
    )
    failed = [row for row in evaluate(applying, seen) if not row["passed"]]
    if failed:
        raise BridgeError(
            f"Project policy refuses {event} on #{issue}: "
            + "; ".join(
                f"rule {row['name']} failed: {row['missing']}" for row in failed
            )
            + f". The rules are in {directory / FILE_NAME}."
        )


def install(bridge: Bridge, repo: Path, source: Path) -> str:
    """Validates a policy file and records it as the project's policy.

    Only an operator in the base checkout may run this, so a lane cannot
    loosen the rules that gate its own steps through this command.

    Args:
        bridge: Coordination runtime owning the project state.
        repo: Any checkout of the target repository, outside every lane.
        source: Policy file the operator chose.

    Returns:
        An account of the recorded rules.

    Raises:
        BridgeError: If the command runs in a lane, the project is missing,
            or the file cannot be read or is invalid; nothing is recorded.
    """
    from agent_parley.unattended import operator_only

    root, directory = bridge.project(repo, create=False)
    operator_only(repo, root, roster.read(directory), "A policy is imported")
    try:
        content = source.read_bytes()
    except OSError as error:
        raise BridgeError(f"{source} cannot be read: {error}") from None
    rules = parse(content, str(source))
    target = directory / FILE_NAME
    staged = target.with_name(f".{FILE_NAME}.{os.getpid()}")
    staged.write_bytes(content)
    os.replace(staged, target)
    return f"Recorded {len(rules)} policy rule(s) in {target}."


def check(bridge: Bridge, repo: Path) -> str:
    """Judges every rule against every held claim, changing nothing.

    Args:
        bridge: Coordination runtime owning the project state.
        repo: Any checkout of the target repository.

    Returns:
        Every rule with pass or fail for each claim a lane holds, or the
        rules alone when no lane holds a claim.

    Raises:
        BridgeError: If the project is missing or its policy is invalid.
    """
    from agent_parley import issues

    _, directory = bridge.project(repo, create=False)
    path = directory / FILE_NAME
    rules = load(directory)
    if not rules:
        return f"No project policy at {path}; no step is gated."
    data = roster.read(directory)
    shown = [f"Project policy {path}: {len(rules)} rule(s)."]
    held = sorted(
        (int(number) if number.isdigit() else 0, number, record)
        for number, record in issues.snapshot(directory)["issues"].items()
        if record.get("owner") in data["participants"]
    )
    for _, number, record in held:
        owner = str(record["owner"])
        shown.append(f"#{number} held by {owner}:")
        try:
            seen = facts(
                directory,
                Path(data["root"]),
                Path(data["participants"][owner]["lane"]),
                builder=owner,
                issue=number,
                claim=str(record.get("claim_id") or ""),
            )
        except BridgeError as error:
            shown.append(f"  not judged: {error}")
            continue
        for row in evaluate(rules, seen):
            on = ", ".join(row["on"])
            shown.append(
                f"  {'pass' if row['passed'] else 'FAIL'} {row['name']} "
                f"({row['kind']}, on {on})"
                + (f": {row['missing']}" if row["missing"] else "")
            )
    if not held:
        shown.append("No lane holds a claim, so no rule was judged:")
        shown.extend(
            f"  {rule['name']} ({rule['kind']}, on {', '.join(rule['on'])})"
            for rule in rules
        )
    return "\n".join(shown)
