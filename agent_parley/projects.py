"""Recent projects and named workspaces recorded in the state root.

Every registered project already has a manifest under the state root, so the
list of projects needs no second registry. This module adds two small records
beside them: `RECENT` holds when each project root was last used by `run` or
`setup`, and `WORKSPACES` holds named groups of project roots. Both live in
the private state root and nothing is written into a target repository.

A workspace only groups roots. Each project keeps its own manifest, claims,
reservations and integration target, and starting a workspace's lanes uses the
same detached resume the service's wake uses, one lane at a time, on the
local host.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Sequence
from pathlib import Path

from agent_parley import BridgeError
from agent_parley.state import lock, write_json

RECENT = "recent-projects.json"
"""State-root record of each project root's last use, in epoch seconds."""

WORKSPACES = "workspaces.json"
"""State-root record of each workspace name and its ordered project roots."""

LOCK = "projects.lock"
"""Lock serializing writes to `RECENT` and `WORKSPACES`."""

WORKSPACE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
"""Accepted workspace names: a letter or digit, then up to 63 more."""


def _read(path: Path, key: str) -> dict:
    """Reads one mapping from a state-root record.

    Args:
        path: Record to read.
        key: Top-level key holding the mapping.

    Returns:
        The mapping, or an empty one when the record does not exist yet.

    Raises:
        BridgeError: If the record exists but is not a readable mapping.
    """
    if not path.exists():
        return {}
    try:
        document = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise BridgeError(f"{path} is unreadable: {error}") from error
    value = document.get(key) if isinstance(document, dict) else None
    if not isinstance(value, dict):
        raise BridgeError(f"{path} holds no {key} mapping.")
    return value


def registered(home: Path) -> dict[str, dict]:
    """Reads every registered project manifest under the state root.

    Args:
        home: Private bridge state root.

    Returns:
        Mapping of recorded project root to its normalized manifest, with
        unreadable manifests left out.
    """
    from agent_parley import roster

    found: dict[str, dict] = {}
    for path in sorted((home / "projects").glob("*/project.json")):
        try:
            manifest = roster.normalize(json.loads(path.read_text()))
        except (OSError, ValueError, KeyError, BridgeError):
            continue
        root = manifest.get("root")
        if isinstance(root, str) and root:
            found[root] = {**manifest, "directory": str(path.parent)}
    return found


def touch(home: Path, root: str, now: float | None = None) -> None:
    """Records that a project root was just used.

    Args:
        home: Private bridge state root.
        root: Recorded root of the project.
        now: Time of the use; the current time when omitted.
    """
    with lock(home / LOCK, timeout=5):
        recent = _read(home / RECENT, "projects")
        recent[root] = time.time() if now is None else now
        write_json(home / RECENT, {"projects": recent})


def listed(home: Path) -> list[dict]:
    """Lists the registered projects, most recently used first.

    A project never recorded as used counts from its manifest's change
    time, so projects registered before this record existed still sort.

    Args:
        home: Private bridge state root.

    Returns:
        One entry per project with its ``name`` (the root's last path
        component), ``root``, ``last_used`` epoch seconds, ``lanes`` names
        and the ``workspaces`` that hold it.
    """
    recent = _read(home / RECENT, "projects")
    groups = workspaces(home)
    entries = []
    for root, manifest in registered(home).items():
        used = recent.get(root)
        if not isinstance(used, int | float):
            used = (Path(manifest["directory"]) / "project.json").stat()
            used = used.st_mtime
        entries.append(
            {
                "name": Path(root).name,
                "root": root,
                "last_used": float(used),
                "lanes": sorted(manifest.get("participants", {})),
                "workspaces": sorted(
                    name for name, roots in groups.items() if root in roots
                ),
            }
        )
    return sorted(entries, key=lambda entry: -entry["last_used"])


def describe(entries: Sequence[dict]) -> str:
    """States the projects `listed` returned, one line each.

    Args:
        entries: Projects as `listed` returns them.

    Returns:
        One line per project naming it, its last use, its lanes and its
        workspaces, then its root; or how to register one when none is.
    """
    if not entries:
        return "No registered projects; `agent-parley setup` adds one."
    lines = []
    for entry in entries:
        used = time.strftime(
            "%Y-%m-%d %H:%M", time.localtime(entry["last_used"])
        )
        grouped = ", ".join(entry["workspaces"]) or "none"
        lines.append(
            f"{entry['name']}  used {used}  "
            f"lanes {len(entry['lanes'])}  workspaces {grouped}\n"
            f"  {entry['root']}"
        )
    return "\n".join(lines)


def resolve(home: Path, name: str) -> str:
    """Finds the one registered project a name or path selects.

    Args:
        home: Private bridge state root.
        name: Project name as `listed` prints it, or its root path.

    Returns:
        The recorded root of the selected project.

    Raises:
        BridgeError: If no project or more than one project matches.
    """
    roots = list(registered(home))
    if name in roots:
        return name
    path = str(Path(name).expanduser().resolve())
    if path in roots:
        return path
    matches = [root for root in roots if Path(root).name == name]
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise BridgeError(
            f"{len(matches)} projects are named {name}: "
            f"{', '.join(matches)}. Name one by its root.",
            next_command="agent-parley projects",
        )
    raise BridgeError(
        f"No registered project is named {name}.",
        next_command="agent-parley projects",
    )


def workspaces(home: Path) -> dict[str, list[str]]:
    """Reads every recorded workspace.

    Args:
        home: Private bridge state root.

    Returns:
        Mapping of workspace name to its project roots, in recorded order.
    """
    return {
        name: [str(root) for root in roots]
        for name, roots in _read(home / WORKSPACES, "workspaces").items()
        if isinstance(roots, list)
    }


def members(home: Path, name: str) -> list[str]:
    """Names the project roots one workspace groups.

    Args:
        home: Private bridge state root.
        name: Workspace name.

    Returns:
        The workspace's project roots, in recorded order.

    Raises:
        BridgeError: If no workspace has that name.
    """
    recorded = workspaces(home)
    if name not in recorded:
        raise BridgeError(
            f"No workspace is named {name}.",
            next_command="agent-parley workspace list",
        )
    return recorded[name]


def set_workspace(home: Path, name: str, projects: Sequence[str]) -> list[str]:
    """Records a workspace, replacing any workspace of the same name.

    Args:
        home: Private bridge state root.
        name: Workspace name matching `WORKSPACE_NAME`.
        projects: Project names or roots, each resolved through `resolve`.

    Returns:
        The recorded project roots, in the order given, without repeats.

    Raises:
        BridgeError: If the name is invalid, no project is given, or a
            project does not resolve to exactly one registered project.
    """
    if not WORKSPACE_NAME.fullmatch(name):
        raise BridgeError(
            f"Workspace name {name!r} is invalid; use letters, digits, "
            "'.', '_' or '-', starting with a letter or digit."
        )
    if not projects:
        raise BridgeError("A workspace needs at least one project.")
    roots = list(dict.fromkeys(resolve(home, project) for project in projects))
    with lock(home / LOCK, timeout=5):
        recorded = _read(home / WORKSPACES, "workspaces")
        recorded[name] = roots
        write_json(home / WORKSPACES, {"workspaces": recorded})
    return roots


def remove_workspace(home: Path, name: str) -> None:
    """Forgets one workspace; its projects and their state are untouched.

    Args:
        home: Private bridge state root.
        name: Workspace name.

    Raises:
        BridgeError: If no workspace has that name.
    """
    with lock(home / LOCK, timeout=5):
        recorded = _read(home / WORKSPACES, "workspaces")
        if name not in recorded:
            raise BridgeError(
                f"No workspace is named {name}.",
                next_command="agent-parley workspace list",
            )
        del recorded[name]
        write_json(home / WORKSPACES, {"workspaces": recorded})


def start_lanes(home: Path, root: str) -> dict[str, str]:
    """Resumes every stopped lane of one project in a detached launcher.

    The launcher is the service wake's own `run --resume`, so each lane
    keeps its native authentication, permissions and project state, and
    its output goes to the lane's wake log. A lane is left alone when it
    is paused or retired, its recorded session still runs, it never
    recorded a session to resume, or a resume would stop unseen on the
    bridge tools opt-in the service also requires; the outcome says which.

    Args:
        home: Private bridge state root.
        root: Recorded root of the project.

    Returns:
        Mapping of lane name to one line stating what was done.

    Raises:
        BridgeError: If the project is not registered.
    """
    from agent_parley import checkpoints, process, roster, supervision

    manifest = registered(home).get(root)
    if manifest is None:
        raise BridgeError(
            f"{root} is no longer a registered project.",
            next_command="agent-parley projects",
        )
    directory = Path(manifest["directory"])
    outcomes = {}
    for name, participant in sorted(manifest["participants"].items()):
        state = checkpoints.activity(directory, name)
        pid = state.get("session_pid")
        session = state.get("resumable_session", state.get("session_id"))
        if participant.get("paused") or roster.retired(participant):
            outcomes[name] = "skipped: paused or retired"
        elif type(pid) is int and process.alive(
            pid, str(state.get("session_ticks") or "")
        ):
            outcomes[name] = "already running"
        elif not session:
            outcomes[name] = (
                "skipped: no recorded session; start it with "
                f"`agent-parley run {name} --repo {root}`"
            )
        elif supervision.opt_in_missing(home, manifest, name):
            outcomes[name] = (
                "skipped: a detached resume would stop on the bridge tools "
                "prompt; record the opt-in with "
                f"`{supervision.OPT_IN_COMMAND}`"
            )
        else:
            child = supervision._resume(home, directory, manifest, name)
            outcomes[name] = (
                f"resuming (launcher {child.pid}, log "
                f"{directory / f'{name}-wake.log'})"
            )
    return outcomes
