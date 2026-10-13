"""Blueprints: ordered deterministic and agent steps one claim runs through.

A blueprint is a named, ordered list of nodes an operator records in the
project manifest, so configuring one commits nothing to the target
repository. A run walks one claim through a blueprint. The harness runs the
deterministic nodes itself, with no model call, so a required step always
happens and in the recorded order: a ``run`` node executes an argument list
in the lane's worktree without a shell, a ``push`` node pushes the lane's
branch, a ``pull-request`` node opens or updates the lane's pull request
through the repository's ``pull_request.self_service`` policy, and a
``report`` node ends the run as done or blocked. An ``agent`` node writes its
prompt into the lane's inbox, with the last failing step's output attached,
and ends when the lane next files a report: ready follows the success edge
and blocked the failure edge. A ``wait-ci`` node reads the pull request
record the supervision poll keeps: checks green on the branch's head follow
the success edge, red ones the failure edge with the failed-step logs
attached, and a head past the ``ci_rounds`` limit ends the run blocked.

The supervision poll moves every run on through `supervise`, so an agent
node passes once its lane reports and a ``wait-ci`` node once its verdict is
known, without the operator running `blueprint advance`. Nothing waits by
sleeping: a node with no outcome yet leaves the run waiting for the next
poll. A run starts from `blueprint run`, or from `run NAME --issue N` with
``--blueprint``, or with the default blueprint the operator mapped to one
of the issue's labels.

Each node names the node that follows on success and on failure. A node with
no ``next`` falls through to the following node, and the last one ends the
run as done. A node with no ``on_failure`` retries itself. Every failure is
counted per node, and a node that fails more than its ``retries`` allow ends
the run blocked for the operator.

Only an operator shell in the base checkout records, starts or advances a
blueprint, as with `init set`, so a lane cannot plant commands later lanes
run. A ``run`` node inherits the operator's environment, which is what a
launched lane inherits minus its coordination token, plus
``AGENT_PARLEY_BASE``. No node merges: `participant merge` and its
verification gate stay the only way into the base, and a blueprint cannot
remove that gate. Progress is recorded per claim in the project's coordination
directory, and a claim with no run recorded behaves exactly as before.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from agent_parley import forge, issues, roster, supervision, unattended
from agent_parley.state import BridgeError, LockBusy, lock, write_json
from agent_parley.worktrees import VERIFY_TIMEOUT, git

if TYPE_CHECKING:
    from agent_parley.cli import Bridge

KINDS = ("run", "agent", "push", "pull-request", "wait-ci", "report")
"""Node kinds a blueprint may hold; none of them merges."""

FIELDS: dict[str, set[str]] = {
    "run": {"command", "timeout"},
    "push": {"timeout"},
    "pull-request": set(),
    "wait-ci": set(),
    "agent": {"prompt"},
    "report": {"outcome", "message"},
}
"""Fields each kind accepts beyond ``name``, ``kind`` and the edges."""

EDGES = {"name", "kind", "next", "on_failure", "retries"}
PUSH = ["git", "push", "--set-upstream", "origin", "HEAD"]
MAX_NODES = 50
MAX_RETRIES = 20
MAX_TEXT = 4000
MAX_VISITS = 200
"""Node visits one run may make, which ends a cycle of passing nodes."""

TAIL_LINES = 40
TAIL_CHARS = 2000
MAX_LOGS = 3
"""Failed checks whose step logs a red ``wait-ci`` verdict quotes."""

RUNS = "blueprint-runs.json"
RUNNING = "running"
WAITING = "waiting"
DONE = "done"
BLOCKED = "blocked"


def parse(text: str) -> list[dict]:
    """Validates a blueprint file into its ordered nodes.

    Args:
        text: JSON object whose ``nodes`` member lists the nodes in order.

    Returns:
        The nodes with defaults filled in: ``retries`` is 0, a ``run`` or
        ``push`` node's ``timeout`` is the verification limit, and a
        ``report`` node's ``outcome`` is done.

    Raises:
        BridgeError: If the text is not a valid blueprint.
    """
    try:
        document = json.loads(text)
    except ValueError as exc:
        raise BridgeError(f"Blueprint is not valid JSON: {exc}.") from None
    raw = document.get("nodes") if isinstance(document, dict) else None
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_NODES:
        raise BridgeError(
            "A blueprint is a JSON object whose `nodes` list holds 1 to "
            f"{MAX_NODES} nodes."
        )
    nodes = [_node(entry) for entry in raw]
    names = [node["name"] for node in nodes]
    if len(set(names)) != len(names):
        raise BridgeError("Blueprint node names must be unique.")
    for node in nodes:
        for edge in ("next", "on_failure"):
            if edge in node and node[edge] not in names:
                raise BridgeError(
                    f"Blueprint node {node['name']!r} names unknown "
                    f"{edge} node {node[edge]!r}."
                )
    return nodes


def _node(entry: object) -> dict:
    """Validates one blueprint node.

    Args:
        entry: One member of the blueprint's ``nodes`` list.

    Returns:
        The node with its defaults filled in.

    Raises:
        BridgeError: If the node is malformed.
    """
    if not isinstance(entry, dict):
        raise BridgeError("Every blueprint node must be a JSON object.")
    name = entry.get("name")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 64:
        raise BridgeError("Every blueprint node needs a name of 1-64 chars.")
    kind = entry.get("kind")
    if kind not in KINDS:
        raise BridgeError(
            f"Blueprint node {name!r} kind must be one of: "
            + ", ".join(KINDS)
            + "."
        )
    unknown = set(entry) - EDGES - FIELDS[kind]
    if unknown:
        raise BridgeError(
            f"Blueprint node {name!r} has unknown fields: "
            + ", ".join(sorted(unknown))
            + "."
        )
    node = dict(entry)
    retries = node.setdefault("retries", 0)
    if type(retries) is not int or not 0 <= retries <= MAX_RETRIES:
        raise BridgeError(
            f"Blueprint node {name!r} retries must be 0-{MAX_RETRIES}."
        )
    for edge in ("next", "on_failure"):
        if edge in node and not isinstance(node[edge], str):
            raise BridgeError(
                f"Blueprint node {name!r} {edge} must name a node."
            )
    if kind == "run":
        command = node.get("command")
        if isinstance(command, str):
            node["command"] = roster.verify_command(
                command, f"Blueprint node {name!r} command"
            )
        elif (
            not isinstance(command, list)
            or not command
            or any(not isinstance(token, str) for token in command)
        ):
            raise BridgeError(
                f"Blueprint node {name!r} needs a command: a string or a "
                "list of argument strings."
            )
        if not node["command"]:
            raise BridgeError(f"Blueprint node {name!r} needs a command.")
    if kind in ("run", "push"):
        limit = node.setdefault("timeout", VERIFY_TIMEOUT)
        if type(limit) not in (int, float) or not 0 < limit <= VERIFY_TIMEOUT:
            raise BridgeError(
                f"Blueprint node {name!r} timeout must be 1-{VERIFY_TIMEOUT} "
                "seconds."
            )
    if kind == "agent":
        prompt = node.get("prompt")
        if not isinstance(prompt, str) or not 1 <= len(prompt) <= MAX_TEXT:
            raise BridgeError(
                f"Blueprint node {name!r} needs a prompt of 1-{MAX_TEXT} "
                "characters."
            )
    if kind == "report":
        if node.setdefault("outcome", DONE) not in (DONE, BLOCKED):
            raise BridgeError(
                f"Blueprint node {name!r} outcome must be done or blocked."
            )
        message = node.setdefault("message", "")
        if not isinstance(message, str) or len(message) > MAX_TEXT:
            raise BridgeError(
                f"Blueprint node {name!r} message must be text of at most "
                f"{MAX_TEXT} characters."
            )
    return node


def define(bridge: Bridge, repo: Path, name: str, path: Path | None) -> str:
    """Records, replaces or removes one named blueprint.

    Args:
        bridge: Coordination runtime owning the project state.
        repo: Any checkout of the target repository, outside every lane.
        name: Blueprint name.
        path: Blueprint file to record, or None to remove the blueprint.

    Returns:
        An account of the change.

    Raises:
        BridgeError: If the command runs from a lane, the file cannot be
            read, or the blueprint is invalid or unknown.
    """
    root, directory = bridge.project(repo, create=False)
    data = roster.read(directory)
    unattended.operator_only(repo, root, data, "A blueprint is set")
    if not 1 <= len(name) <= 64 or not name.replace("-", "").isalnum():
        raise BridgeError(
            "Blueprint names use 1-64 letters, digits and hyphens."
        )
    nodes = None
    if path is not None:
        try:
            text = path.read_text()
        except OSError as exc:
            raise BridgeError(f"Cannot read blueprint {path}: {exc}.") from None
        nodes = parse(text)
    with lock(directory / "setup.lock"):
        data = roster.read(directory)
        stored = dict(data.get("blueprints") or {})
        labels = sorted(
            label
            for label, target in (data.get("blueprint_defaults") or {}).items()
            if target == name
        )
        if nodes is None and labels:
            raise BridgeError(
                f"Blueprint {name} is the default for label "
                + ", ".join(labels)
                + "; clear that first with agent-parley blueprint default "
                "LABEL."
            )
        if nodes is None:
            if stored.pop(name, None) is None:
                raise BridgeError(
                    f"No blueprint named {name!r}; run agent-parley "
                    "blueprint list."
                )
        else:
            stored[name] = nodes
        data["blueprints"] = stored
        write_json(directory / "project.json", data)
    if nodes is None:
        return f"Removed blueprint {name}."
    return f"Blueprint {name} records {len(nodes)} nodes: " + " -> ".join(
        node["name"] for node in nodes
    )


def describe(bridge: Bridge, repo: Path, name: str | None = None) -> str:
    """Lists the recorded blueprints, or shows one node by node.

    Args:
        bridge: Coordination runtime owning the project state.
        repo: Any checkout of the target repository.
        name: Blueprint to show, or None to list every blueprint.

    Returns:
        The listing or the blueprint's nodes with their edges.

    Raises:
        BridgeError: If the project or the named blueprint does not exist.
    """
    root, directory = bridge.project(repo, create=False)
    data = roster.read(directory)
    stored = data.get("blueprints") or {}
    if name is None:
        if not stored:
            return (
                f"{root} has no blueprints; a claim runs as the lane decides."
            )
        return "\n".join(
            [
                *(
                    f"{key}: " + " -> ".join(node["name"] for node in nodes)
                    for key, nodes in sorted(stored.items())
                ),
                *(
                    f"label {label} defaults to {target}"
                    for label, target in sorted(
                        (data.get("blueprint_defaults") or {}).items()
                    )
                ),
            ]
        )
    if name not in stored:
        raise BridgeError(
            f"No blueprint named {name!r}; run agent-parley blueprint list."
        )
    nodes = stored[name]
    lines = [f"Blueprint {name}:"]
    for index, node in enumerate(nodes):
        following = node.get("next") or (
            nodes[index + 1]["name"] if index + 1 < len(nodes) else DONE
        )
        if node["kind"] == "report":
            detail = node["outcome"]
        elif node["kind"] == "run":
            detail = shlex.join(node["command"])
        elif node["kind"] == "push":
            detail = shlex.join(PUSH)
        elif node["kind"] == "pull-request":
            detail = "pull_request.self_service"
        elif node["kind"] == "wait-ci":
            detail = "required checks"
        else:
            detail = node["prompt"].splitlines()[0][:60]
        edges = (
            ""
            if node["kind"] == "report"
            else f" ok->{following} fail->"
            f"{node.get('on_failure', node['name'])}"
            f" retries={node['retries']}"
        )
        lines.append(f"  {node['name']} [{node['kind']}] {detail}{edges}")
    return "\n".join(lines)


def default(
    bridge: Bridge, repo: Path, label: str, blueprint: str | None
) -> str:
    """Maps an issue label to the blueprint a launch on it runs by default.

    `run NAME --issue N` without ``--blueprint`` starts the blueprint mapped
    to one of the issue's labels. The mapping lives in the project manifest
    beside the blueprints, so only an operator shell changes it.

    Args:
        bridge: Coordination runtime owning the project state.
        repo: Any checkout of the target repository, outside every lane.
        label: Issue label, matched exactly.
        blueprint: Recorded blueprint the label selects, or None to clear
            the label's mapping.

    Returns:
        An account of the change.

    Raises:
        BridgeError: If the command runs from a lane, the label is empty or
            too long, the blueprint is unknown, or a cleared label had no
            mapping.
    """
    root, directory = bridge.project(repo, create=False)
    data = roster.read(directory)
    unattended.operator_only(repo, root, data, "A default blueprint is set")
    label = label.strip()
    if not 1 <= len(label) <= 64:
        raise BridgeError("A label names 1-64 characters.")
    with lock(directory / "setup.lock"):
        data = roster.read(directory)
        defaults = dict(data.get("blueprint_defaults") or {})
        if blueprint is None:
            if defaults.pop(label, None) is None:
                raise BridgeError(f"Label {label!r} selects no blueprint.")
        elif blueprint not in (data.get("blueprints") or {}):
            raise BridgeError(
                f"No blueprint named {blueprint!r}; run agent-parley "
                "blueprint list."
            )
        else:
            defaults[label] = blueprint
        data["blueprint_defaults"] = defaults
        write_json(directory / "project.json", data)
    if blueprint is None:
        return f"Label {label} no longer selects a blueprint."
    return (
        f"run --issue on an issue labelled {label} starts blueprint "
        f"{blueprint} unless --blueprint names another."
    )


def chosen(bridge: Bridge, repo: Path, number: str, blueprint: str) -> str:
    """Names the blueprint a launch on one issue starts, if any.

    An explicit name wins. Otherwise the issue's labels are read from the
    forge, and only when the operator mapped at least one label, so a
    project without defaults launches exactly as before.

    Args:
        bridge: Coordination runtime owning the project state.
        repo: Checkout the launch names.
        number: Bare issue number the launch claims.
        blueprint: Blueprint the launch named, or an empty string.

    Returns:
        The blueprint to start, or an empty string for none.

    Raises:
        BridgeError: If a blueprint applies and the launch runs from a lane,
            the blueprint is unknown, or the issue's labels select more than
            one blueprint or cannot be read while label defaults exist.
    """
    root, directory = bridge.project(repo, create=False)
    data = roster.read(directory)
    defaults = data.get("blueprint_defaults") or {}
    if not blueprint and defaults:
        labels = forge.issue_labels(root, number)
        if labels is None:
            raise BridgeError(
                f"The labels of issue #{number} could not be read, so its "
                "default blueprint is unknown; name one with --blueprint or "
                "retry."
            )
        matched = sorted(
            {defaults[label] for label in labels if label in defaults}
        )
        if len(matched) > 1:
            raise BridgeError(
                f"Issue #{number} carries labels selecting blueprints "
                + ", ".join(matched)
                + "; name one with --blueprint."
            )
        blueprint = matched[0] if matched else ""
    if not blueprint:
        return ""
    unattended.operator_only(repo, root, data, "A blueprint run is started")
    if blueprint not in (data.get("blueprints") or {}):
        raise BridgeError(
            f"No blueprint named {blueprint!r}; run agent-parley "
            "blueprint list."
        )
    return blueprint


def for_lane(directory: Path, lane: str) -> dict[str, dict]:
    """Reads the recorded blueprint runs one lane's claims went through.

    Args:
        directory: Project coordination directory.
        lane: Participant whose runs are read.

    Returns:
        The lane's runs keyed by issue number; empty for a lane that never
        ran a blueprint, so its views print exactly as before.
    """
    return {
        number: run
        for number, run in _runs(directory).items()
        if isinstance(run, dict) and run.get("lane") == lane
    }


def _runs(directory: Path) -> dict:
    """Reads every recorded blueprint run, keyed by issue number."""
    try:
        runs = json.loads((directory / RUNS).read_text())
    except (OSError, ValueError):
        return {}
    return runs if isinstance(runs, dict) else {}


def _save(directory: Path, issue: str, run: dict) -> None:
    """Records one issue's run beside every other recorded run."""
    run["updated"] = time.time()
    with lock(directory / f"{RUNS}.lock", timeout=5):
        runs = _runs(directory)
        runs[issue] = run
        write_json(directory / RUNS, runs)


def start(
    bridge: Bridge, repo: Path, name: str, blueprint: str, issue: str
) -> str:
    """Starts a lane's claim on a blueprint and runs it as far as it can go.

    The blueprint's nodes are copied into the run, so a later `blueprint
    set` changes the next run and never the one in flight.

    Args:
        bridge: Coordination runtime owning the project state.
        repo: Any checkout of the target repository, outside every lane.
        name: Participant whose claim runs the blueprint.
        blueprint: Recorded blueprint name.
        issue: Issue the participant claims, optionally prefixed with ``#``.

    Returns:
        An account of where the run stopped.

    Raises:
        BridgeError: If the command runs from a lane, the participant does
            not hold the issue, the blueprint is unknown, or the
            participant's own run on the issue is still in progress. A run
            an earlier owner left in progress is replaced.
    """
    root, directory = bridge.project(repo, create=False)
    data = roster.read(directory)
    unattended.operator_only(repo, root, data, "A blueprint run is started")
    number = issue.strip().removeprefix("#")
    if name not in data["participants"]:
        raise BridgeError(
            f"{name} is not a participant in this project; run "
            "agent-parley participant list."
        )
    nodes = (data.get("blueprints") or {}).get(blueprint)
    if nodes is None:
        raise BridgeError(
            f"No blueprint named {blueprint!r}; run agent-parley "
            "blueprint list."
        )
    record = issues.snapshot(directory)["issues"].get(number) or {}
    if record.get("owner") != name:
        raise BridgeError(
            f"{name} does not hold issue #{number}; a blueprint runs on the "
            "lane's own claim."
        )
    with lock(directory / f"blueprint-{number}.lock", _busy(number)):
        current = _runs(directory).get(number)
        if (
            current
            and current["status"] in (RUNNING, WAITING)
            and current["lane"] == name
        ):
            raise BridgeError(
                f"Issue #{number} is already at blueprint node "
                f"{current['node']!r}; run agent-parley blueprint advance "
                f"--issue {number}."
            )
        run = {
            "blueprint": blueprint,
            "lane": name,
            "nodes": nodes,
            "node": nodes[0]["name"],
            "passed": [],
            "failures": {},
            "visits": 0,
            "status": RUNNING,
            "since": 0.0,
            "output": "",
            "message": "",
            "started": time.time(),
        }
        _save(directory, number, run)
        return _walk(bridge, repo, root, directory, data, number, run)


def advance(bridge: Bridge, repo: Path, issue: str) -> str:
    """Moves a claim's blueprint run on as far as it can go.

    Args:
        bridge: Coordination runtime owning the project state.
        repo: Any checkout of the target repository, outside every lane.
        issue: Issue whose run moves on, optionally prefixed with ``#``.

    Returns:
        An account of where the run stopped.

    Raises:
        BridgeError: If the command runs from a lane, or the issue has no
            run in progress.
    """
    root, directory = bridge.project(repo, create=False)
    data = roster.read(directory)
    unattended.operator_only(repo, root, data, "A blueprint run is advanced")
    number = issue.strip().removeprefix("#")
    with lock(directory / f"blueprint-{number}.lock", _busy(number)):
        run = _runs(directory).get(number)
        if not run or run["status"] not in (RUNNING, WAITING):
            raise BridgeError(
                f"Issue #{number} has no blueprint run in progress; start "
                "one with agent-parley blueprint run."
            )
        return _walk(bridge, repo, root, directory, data, number, run)


def supervise(
    home: Path, directory: Path, manifest: dict
) -> list[threading.Thread]:
    """Moves every blueprint run in progress on from the supervision poll.

    A run waiting on an agent node moves only once its lane has reported
    since the prompt, and every other run in progress moves each poll. Each
    move runs in its own daemon thread under the run's lock, so a
    deterministic node that takes minutes never holds up the poll, and a
    run another command is moving is left for the next poll. A run whose
    lane left the roster is left as it was, and a run whose lane no longer
    holds the issue open moves once, to end blocked.

    Args:
        home: Private bridge state root.
        directory: Project coordination directory.
        manifest: Current participant manifest.

    Returns:
        The threads started, so a caller may wait for them.
    """
    from agent_parley.cli import Bridge

    ledger = issues.snapshot(directory)["issues"]
    started = []
    for number, run in _runs(directory).items():
        if (
            not isinstance(run, dict)
            or run.get("status") not in (RUNNING, WAITING)
            or run.get("lane") not in manifest["participants"]
            or (
                _held(ledger.get(number) or {}, run)
                and not _due(directory, run)
            )
        ):
            continue
        thread = threading.Thread(
            target=_moved,
            args=(Bridge(home), directory, number),
            name=f"agent-parley-blueprint-{number}",
            daemon=True,
        )
        thread.start()
        started.append(thread)
    return started


def _due(directory: Path, run: dict) -> bool:
    """Reports whether a run in progress has anything new to act on."""
    nodes = {node["name"]: node for node in run["nodes"]}
    if run["status"] != WAITING or nodes[run["node"]]["kind"] != "agent":
        return True
    return _reported(directory, run) is not None


def _held(record: dict, run: dict) -> bool:
    """Reports whether the run's lane still holds its issue open."""
    return record.get("owner") == run["lane"] and not issues.ended(record)


def _moved(bridge: Bridge, directory: Path, number: str) -> None:
    """Moves one run on, leaving a busy one for the next poll.

    Any other failure is recorded as the project's supervision error, where
    `status` names it, and the run is retried on the next poll from the
    last node it passed.
    """
    try:
        data = roster.read(directory)
        root = Path(data["root"])
        with lock(directory / f"blueprint-{number}.lock", _busy(number)):
            run = _runs(directory).get(number)
            if run and run["status"] in (RUNNING, WAITING):
                _walk(bridge, root, root, directory, data, number, run)
    except LockBusy:
        return
    except Exception as exc:
        issues.note_supervision_error(
            directory, supervision.failure(f"blueprint #{number}", exc)
        )


def progress(bridge: Bridge, repo: Path, issue: str | None = None) -> str:
    """Reports the recorded blueprint runs: current node and nodes passed.

    Args:
        bridge: Coordination runtime owning the project state.
        repo: Any checkout of the target repository.
        issue: Issue to report, or None for every recorded run.

    Returns:
        One line per run.

    Raises:
        BridgeError: If the project does not exist.
    """
    _, directory = bridge.project(repo, create=False)
    runs = _runs(directory)
    if issue is not None:
        number = issue.strip().removeprefix("#")
        runs = {number: runs[number]} if number in runs else {}
    if not runs:
        return "No blueprint runs are recorded."
    return "\n".join(line(number, run) for number, run in sorted(runs.items()))


def _busy(number: str) -> str:
    """Explains a run another operator command is already moving."""
    return (
        f"Another command is moving the blueprint run on issue #{number}; "
        "retry when it returns."
    )


def line(number: str, run: dict) -> str:
    """Formats one run as its issue, lane, status, node and nodes passed.

    Args:
        number: Issue the run belongs to.
        run: The run's recorded state.

    Returns:
        The line `blueprint progress`, `status`, `top` and `watch` print.
    """
    passed = ", ".join(run["passed"]) or "none"
    line = (
        f"#{number} {run['lane']} {run['blueprint']}: {run['status']} at "
        f"{run['node']}; passed {passed}"
    )
    return f"{line}. {run['message']}" if run["message"] else line


def _walk(
    bridge: Bridge,
    repo: Path,
    root: Path,
    directory: Path,
    data: dict,
    number: str,
    run: dict,
) -> str:
    """Runs nodes until the run waits on its lane or ends.

    Args:
        bridge: Coordination runtime owning the project state.
        repo: Checkout the operator command names.
        root: Base checkout of the repository.
        directory: Project coordination directory.
        data: Project manifest.
        number: Issue the run belongs to.
        run: The run's recorded state, updated and saved as nodes pass.

    Returns:
        An account of where the run stopped.
    """
    nodes = {node["name"]: node for node in run["nodes"]}
    order = [node["name"] for node in run["nodes"]]
    lane = Path(data["participants"][run["lane"]]["lane"])
    if not _held(issues.snapshot(directory)["issues"].get(number) or {}, run):
        run["status"] = BLOCKED
        run["message"] = (
            f"{run['lane']} no longer holds issue #{number} open, so the run "
            "stopped."
        )
    while run["status"] in (RUNNING, WAITING):
        node = nodes[run["node"]]
        if node["kind"] == "report":
            run["passed"].append(node["name"])
            run["status"] = node["outcome"]
            run["message"] = node["message"]
            break
        if run["visits"] >= MAX_VISITS:
            run["status"] = BLOCKED
            run["message"] = (
                f"The run visited {MAX_VISITS} nodes without ending; check "
                "the blueprint's edges."
            )
            break
        if node["kind"] in ("agent", "wait-ci"):
            passed = (
                _agent(bridge, repo, directory, number, run, node)
                if node["kind"] == "agent"
                else _wait_ci(root, directory, data, run)
            )
            if passed is None:
                break
        elif node["kind"] == "pull-request":
            passed = _pull_request(bridge, root, data, run)
        else:
            command = PUSH if node["kind"] == "push" else node["command"]
            passed = _execute(command, lane, root, node["timeout"], run)
        run["visits"] += 1
        if passed:
            run["passed"].append(node["name"])
            index = order.index(node["name"])
            following = node.get("next") or (
                order[index + 1] if index + 1 < len(order) else None
            )
            if following is None:
                run["status"] = DONE
                break
            run["node"] = following
            _save(directory, number, run)
            continue
        failures = run["failures"].get(node["name"], 0) + 1
        run["failures"][node["name"]] = failures
        if failures > node["retries"]:
            run["status"] = BLOCKED
            run["message"] = (
                f"Node {node['name']} failed {failures} times, past its "
                f"retry limit of {node['retries']}."
            )
            break
        run["node"] = node.get("on_failure", node["name"])
    _save(directory, number, run)
    return line(number, run)


def _execute(
    command: list[str], lane: Path, root: Path, timeout: float, run: dict
) -> bool:
    """Runs one deterministic node in the lane's worktree without a shell.

    Args:
        command: Argument tokens to run.
        lane: Worktree the command runs in.
        root: Base checkout, offered as ``AGENT_PARLEY_BASE``.
        timeout: Seconds the command may take.
        run: Run state whose ``output`` receives the bounded output tail.

    Returns:
        Whether the command exited zero within its timeout.
    """
    environment = {
        key: value
        for key, value in os.environ.items()
        if key != unattended.LANE_TOKEN
    }
    environment["AGENT_PARLEY_BASE"] = str(root)
    try:
        result = subprocess.run(
            command,
            cwd=lane,
            env=environment,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except OSError as exc:
        run["output"] = f"`{shlex.join(command)}` could not run: {exc}"
        return False
    except subprocess.TimeoutExpired:
        run["output"] = f"`{shlex.join(command)}` ran past {timeout:g} seconds."
        return False
    lines = (result.stdout + result.stderr).splitlines()[-TAIL_LINES:]
    run["output"] = (
        f"`{shlex.join(command)}` exited {result.returncode}:\n"
        + "\n".join(lines)[-TAIL_CHARS:]
    )
    return not result.returncode


def _agent(
    bridge: Bridge,
    repo: Path,
    directory: Path,
    number: str,
    run: dict,
    node: dict,
) -> bool | None:
    """Hands an agent node's prompt to the lane, or reads its outcome.

    The first visit writes the prompt into the lane's inbox, with the last
    failing node's output attached, and waits. A later visit reads the
    lane's latest report: ready since the prompt passes the node, blocked
    fails it, and anything else keeps waiting.

    Args:
        bridge: Coordination runtime owning the project state.
        repo: Checkout the operator command names.
        directory: Project coordination directory.
        number: Issue the run belongs to.
        run: The run's recorded state.
        node: The agent node.

    Returns:
        True when the lane reported ready, False when it reported blocked,
        and None while the run waits on the lane.
    """
    if run["status"] == WAITING:
        activity = _reported(directory, run)
        if activity is None:
            return None
        run["status"] = RUNNING
        run["output"] = str(activity.get("summary") or "")
        return activity["outcome"] == "ready"
    text = node["prompt"]
    if run["output"] and run["failures"]:
        text = f"{text}\n\nLast failing step output:\n{run['output']}"
    run["since"] = time.time()
    bridge.say(
        repo,
        run["lane"],
        text,
        subject=f"Blueprint {run['blueprint']} step {node['name']} "
        f"for #{number}",
        key=f"blueprint-{number}-{node['name']}-{run['visits']}",
    )
    run["status"] = WAITING
    return None


def _reported(directory: Path, run: dict) -> dict | None:
    """Reads the ready or blocked report a lane filed since its prompt.

    Args:
        directory: Project coordination directory.
        run: The run's recorded state, naming the lane and when the prompt
            was handed over.

    Returns:
        The lane's activity record, or None while no such report exists.
    """
    try:
        activity = json.loads(
            (directory / f"{run['lane']}-activity.json").read_text()
        )
    except (OSError, ValueError):
        return None
    if (
        isinstance(activity, dict)
        and float(activity.get("reported_at") or 0) > run["since"]
        and activity.get("outcome") in ("ready", "blocked")
    ):
        return activity
    return None


def _pull_request(bridge: Bridge, root: Path, data: dict, run: dict) -> bool:
    """Opens or updates the lane's pull request under the self-service policy.

    The node goes through `Bridge.self_service_pull_request`, so every
    condition of the repository's ``pull_request.self_service`` policy still
    applies: a ready report, a configured verification gate that runs before
    the push, the assigned branch and no overlapping peer reservation. Its
    Git and forge calls carry that path's own time limits.

    Args:
        bridge: Coordination runtime owning the project state.
        root: Base checkout of the repository.
        data: Project manifest.
        run: Run state whose ``output`` receives the bounded account.

    Returns:
        Whether the pull request was opened or updated.
    """
    if not (data.get("pull_request") or {}).get("self_service"):
        run["output"] = (
            "This repository's pull_request.self_service policy is off, so "
            "the harness opens no pull request for the lane."
        )
        return False
    try:
        account = bridge.self_service_pull_request(root, run["lane"])
    except (BridgeError, OSError, subprocess.SubprocessError) as exc:
        run["output"] = f"The pull request was not opened: {exc}"[:TAIL_CHARS]
        return False
    run["output"] = account[:TAIL_CHARS]
    return True


def _wait_ci(root: Path, directory: Path, data: dict, run: dict) -> bool | None:
    """Reads the verdict of the lane's pull request checks, if it is known.

    The supervision poll keeps one reading per open pull request, with its
    head commit, check verdict and CI round count. Only a reading of the
    lane's branch at the branch's current head commit counts, so a verdict
    on an earlier push never passes or fails the node. A red verdict is
    judged once per head commit and re-run, so a run that fails over to an
    agent node waits for a new push or a re-run before judging again. A red
    head supervision is re-running automatically keeps the node waiting, and
    a pull request that closes after the node saw it ends the run blocked.

    Args:
        root: Base checkout of the repository.
        directory: Project coordination directory.
        data: Project manifest.
        run: Run state; ``output`` receives the verdict and, on red, the
            failing checks with their failed-step logs.

    Returns:
        True on green checks, False on red ones, and None while the checks
        are pending, unread or being re-run, or once the run ends blocked
        because the pull request closed or used up its CI rounds.
    """
    branch = data["participants"][run["lane"]]["branch"]
    try:
        head = git(root, "rev-parse", "--verify", "--quiet", branch)
    except (BridgeError, subprocess.SubprocessError):
        head = ""
    try:
        record = json.loads(
            (directory / supervision.PULL_REQUEST_RECORD).read_text()
        )
    except (OSError, ValueError):
        record = {}
    readings = record.get("pull_requests") if isinstance(record, dict) else {}
    open_requests = [
        entry
        for entry in (readings or {}).values()
        if isinstance(entry, dict) and entry.get("branch") == branch
    ]
    reading = next(
        (entry for entry in open_requests if entry.get("sha") == head), None
    )
    run["status"] = WAITING
    if open_requests:
        run["pull_request"] = open_requests[0].get("number")
    elif isinstance(readings, dict) and run.get("pull_request"):
        run["status"] = BLOCKED
        run["message"] = (
            f"Pull request #{run['pull_request']} for {branch} is no longer "
            "open, so no check verdict will come; the operator decides."
        )
        return None
    if (
        not head
        or not reading
        or reading.get("checks") not in ("green", "red")
        or _rerunning(reading)
    ):
        return None
    if reading["checks"] == "green":
        run["status"] = RUNNING
        run["output"] = (
            f"Pull request #{reading['number']} checks are green at {head}."
        )
        return True
    judged = f"{head}:{reading.get('red_attempts') or 1}"
    if run.get("judged") == judged:
        return None
    run["judged"] = judged
    run["status"] = RUNNING
    run["output"] = _red(root, reading)
    if reading.get("exhausted"):
        run["status"] = BLOCKED
        run["message"] = (
            f"Pull request #{reading['number']} used {reading['exhausted']} "
            "CI rounds and its checks are still red; the operator decides "
            "whether to grant one more round or take over."
        )
        return None
    return False


def _rerunning(reading: dict) -> bool:
    """Reports whether supervision's automatic re-run decides a red head.

    Supervision re-runs a head that is red only through cancelled or
    timed-out jobs once per red attempt. Until that re-run reports, the red
    reading is not a verdict on the lane's change.
    """
    rerun = reading.get("rerun") or {}
    failed = reading.get("failed") or []
    return (
        reading.get("checks") == "red"
        and bool(rerun.get("accepted"))
        and rerun.get("attempt") == int(reading.get("red_attempts") or 1)
        and bool(failed)
        and all(
            check.get("conclusion") in supervision.RERUN_CONCLUSIONS
            for check in failed
        )
    )


def _red(root: Path, reading: dict) -> str:
    """Describes red checks with the tails of their failed-step logs.

    Args:
        root: Base checkout that selects the forge project.
        reading: The pull request reading the supervision poll kept.

    Returns:
        The failing check names and up to `MAX_LOGS` failed-step log tails,
        within `MAX_TEXT` characters. A cancelled, timed-out or never
        started check has no failing step and contributes only its name.
    """
    parts = [
        f"Pull request #{reading['number']} checks are red at "
        f"{reading['sha']}: " + ", ".join(reading.get("failing") or [])
    ]
    for check in (reading.get("failed") or [])[:MAX_LOGS]:
        if (
            check.get("conclusion") in supervision.RERUN_CONCLUSIONS
            or check.get("not_started")
            or not check.get("run")
            or not check.get("job")
        ):
            continue
        command, log = forge.failed_log(
            root, str(check["run"]), str(check["job"])
        )
        tail = "\n".join((log or "").splitlines()[-TAIL_LINES:])[-TAIL_CHARS:]
        parts.append(f"{check['name']} failed-step log (`{command}`):\n{tail}")
    return "\n".join(parts)[:MAX_TEXT]
