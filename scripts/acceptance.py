"""Unattended acceptance run for a live lane estate.

The integration suite drives synthetic clients, so it proves the protocol
and nothing about a day of real native clients. This module runs a whole
estate against a throwaway project for a fixed period with no operator
input, samples what the service can see while it runs, and decides the
run against the conditions an operator would otherwise have to check by
hand.

One command starts it::

    uv run --locked python -m scripts.acceptance run --hours 24

``rehearse`` seeds, registers and samples the same estate once without
starting a lane, so a run can be checked before it spends model quota.
``record`` turns a finished run's verdict into the live acceptance record
``docs/acceptance/X.Y.Z.json`` that a minor or major release requires.

The run writes a frame every interval to ``frames.jsonl`` under its
workspace, and a Markdown report next to it when the period ends. The
report is the artifact attached to a release; the frames are the evidence
behind it.

Nothing here answers a lane, acknowledges a message, releases a claim or
touches a native client. The run is only allowed to observe, because an
operator touch is what it is measuring the absence of.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import pty
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path

from agent_parley import metrics, problems, supervision

BACKLOG = 20
HOURS = 24.0
ROOT = Path("~/.local/state/parley-acceptance")
RECORDS = Path(__file__).resolve().parents[1] / "docs" / "acceptance"
IDENTITY = ("Acceptance run", "acceptance@localhost")
INTERVAL = 300.0
SETTLE = 20.0
ROWS = 40
COLUMNS = 120
TERMINALS: list[int] = []
LANES = (
    "claude:claude",
    "claude-1:claude",
    "claude-2:claude",
    "claude-3:claude:claude-p2",
    "claude-4:claude:claude-p2",
    "claude-5:claude",
    "codex:codex",
    "codex-1:codex",
)
TASK = """You are one lane of an unattended acceptance run. No operator
is watching, and nobody will answer a question you ask.

Coordinate through your `agent_parley` tools rather than a shell: they
are the surface this estate gives you, and the shell is not.

Work this loop until the backlog is empty:

1. List the issues and pick the lowest numbered task in `tasks/` that no
   lane owns.
2. Claim it.
3. Read `tasks/<number>.md` in your worktree and make exactly the change
   it asks for, with a test.
4. Run the project's verify command. When it passes, commit and report
   the issue ready with a summary of what you changed. When the task
   cannot be done as written, report it blocked with the reason and
   release the claim.
5. Take the next task.

Read your mail when the bridge tells you there is mail, and answer a peer
that asks you something. Never wait for the operator."""
CONDITION = """# Task {number}

Add a function `{name}` to `library.py` that {behaviour}, and a test for
it in `tests/test_library.py`.

The function takes one integer and returns one integer. Keep the module
importable with no dependencies outside the standard library.
"""
BEHAVIOURS = (
    "returns the input doubled",
    "returns the input's absolute value",
    "returns the input plus one",
    "returns the input's square",
    "returns the input negated",
)


def _run(
    command: list[str], cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    """Runs one command and returns its completed process.

    Args:
        command: Argument vector to run.
        cwd: Directory to run the command in, the caller's own when None.

    Returns:
        The completed process, with output captured as text.
    """
    return subprocess.run(
        command, capture_output=True, text=True, check=False, cwd=cwd
    )


def _document(cli: str, home: Path, args: list[str]) -> dict:
    """Reads one JSON document from the coordination CLI.

    Args:
        cli: Executable that speaks the coordination CLI.
        home: Private state directory the estate runs under.
        args: Command and flags after the home selection.

    Returns:
        The parsed document, or an ``error`` record when the command
        printed nothing that parses. A document is taken whatever the
        command's status was, because a view that reports conditions
        exits non-zero while still printing the conditions. A sampling
        failure is recorded rather than raised, because the run must
        survive one bad frame.
    """
    result = _run([cli, "--home", str(home), *args, "--json"])
    try:
        return json.loads(result.stdout)
    except ValueError:
        return {"error": result.stderr.strip() or result.stdout[:400].strip()}


def workspace(path: Path, issues: int) -> None:
    """Creates the throwaway project the estate coordinates over.

    Args:
        path: Directory the project is created in.
        issues: Number of backlog tasks to write.

    Raises:
        SystemExit: The directory already holds a repository, which would
            put the run on somebody's real work, or the seed commit failed.
            The identity is set on this repository alone, because a machine
            that keeps its identity per repository leaves the commit with
            none and the whole run then fails later with a registration
            error that names nothing.
    """
    if (path / ".git").exists():
        raise SystemExit(f"{path} already holds a repository")
    (path / "tasks").mkdir(parents=True, exist_ok=True)
    (path / "tests").mkdir(exist_ok=True)
    (path / "library.py").write_text('"""Acceptance run library."""\n')
    (path / "tests" / "test_library.py").write_text(
        '"""Tests for the acceptance run library."""\n'
    )
    (path / "README.md").write_text(
        "# Acceptance workspace\n\nA throwaway project for an unattended "
        "acceptance run.\n"
    )
    for number in range(1, issues + 1):
        (path / "tasks" / f"{number}.md").write_text(
            CONDITION.format(
                number=number,
                name=f"task_{number}",
                behaviour=BEHAVIOURS[(number - 1) % len(BEHAVIOURS)],
            )
        )
    for command in (
        ["git", "init", "-b", "main"],
        ["git", "config", "user.name", IDENTITY[0]],
        ["git", "config", "user.email", IDENTITY[1]],
        ["git", "add", "-A"],
        ["git", "commit", "-m", "chore: seed the acceptance backlog"],
    ):
        result = _run(command, cwd=path)
        if result.returncode:
            raise SystemExit(result.stderr.strip() or result.stdout.strip())


def register(cli: str, home: Path, repo: Path, lanes: list[str]) -> None:
    """Registers the project and admits every lane of the estate.

    Args:
        cli: Executable that speaks the coordination CLI.
        home: Private state directory the estate runs under.
        repo: Throwaway project the lanes coordinate over.
        lanes: Lane specifications as ``name:provider[:credentials]``.

    Raises:
        SystemExit: Registration or admission refused, which leaves the
            run with an estate smaller than the one it claims to prove.
    """
    base = [cli, "--home", str(home)]
    for command in (
        [*base, "setup", str(repo)],
        [*base, "forge", "set", "null", "--repo", str(repo)],
        [*base, "verify", "set", "python -m pytest -q", "--repo", str(repo)],
    ):
        result = _run(command)
        if result.returncode:
            raise SystemExit(result.stderr.strip() or result.stdout.strip())
    for lane in lanes:
        name, provider, credentials = _lane(lane)
        command = [
            *base,
            "participant",
            "add",
            name,
            "--provider",
            provider,
            "--repo",
            str(repo),
        ]
        if credentials:
            command.extend(["--credentials", credentials])
        result = _run(command)
        if result.returncode and "already" not in result.stderr:
            raise SystemExit(result.stderr.strip() or result.stdout.strip())
    supervise(home, repo)


def supervise(home: Path, repo: Path) -> None:
    """Records the one opt-in an unattended estate is given.

    Args:
        home: Private state directory the estate runs under.
        repo: Throwaway project the lanes coordinate over.

    A resumed session asks again for permission to use this bridge's own
    MCP tools, and no operator is there to answer. The opt-in scopes a
    native permission rule to this bridge's coordination tools and
    nothing else: no file tool, no shell, no other server. Every other
    permission the clients ask for is left exactly as the operator
    configured it, because what this run measures is a day without an
    operator, not a day without permissions.
    """
    manifest = _directory(home, repo) / "project.json"
    data = _read(manifest)
    supervision = dict(data.get("supervision") or {})
    supervision["approve_bridge_tools"] = True
    data["supervision"] = supervision
    manifest.write_text(json.dumps(data, indent=1) + "\n")


def trust(home: Path, repo: Path, lanes: list[str]) -> list[str]:
    """Records the operator's trust of the throwaway project's directories.

    Args:
        home: Private state directory the estate runs under.
        repo: Throwaway project the lanes coordinate over.
        lanes: Lane specifications as ``name:provider[:credentials]``.

    Returns:
        The directories recorded as trusted, so the report can say which
        ones the run was given.

    Both native clients open a fresh directory with a trust screen, and a
    launcher cannot name or answer that screen. Eight lanes on eight new
    worktrees therefore park at startup and never fire a hook. The
    decision itself stays the operator's: this writes only the
    directories of a project the run seeded, into each client's own trust
    record, and only when the operator asked for it on the command line.

    A lane launched on a credential profile reads a configuration
    directory of its own, so its trust belongs in that profile's record
    rather than the operator's: a lane on the default account and a lane
    on a second account are two clients with two trust records.
    """
    directory = _directory(home, repo)
    profiles = _read(home / "credentials.json").get("entries") or {}
    paths = [str(repo)]
    accounts: dict[tuple[str, str], list[str]] = {}
    for lane in lanes:
        name, provider, credentials = _lane(lane)
        paths.append(str(directory / name))
        account = str((profiles.get(credentials) or {}).get("home") or "")
        accounts.setdefault((provider, account), []).append(
            str(directory / name)
        )
    for (provider, account), lanes_of in accounts.items():
        configuration = Path(account) if account else None
        if provider == "claude":
            record = (configuration or Path.home()) / ".claude.json"
            data = _read(record)
            projects = data.setdefault("projects", {})
            for path in [str(repo), *lanes_of]:
                entry = projects.setdefault(path, {})
                entry["hasTrustDialogAccepted"] = True
            record.parent.mkdir(parents=True, exist_ok=True)
            _replace(record, json.dumps(data, indent=2) + "\n")
        if provider == "codex":
            record = (configuration or Path.home() / ".codex") / "config.toml"
            text = record.read_text() if record.exists() else ""
            added = ""
            for path in [str(repo), *lanes_of]:
                if f'[projects."{path}"]' not in text:
                    added += f'\n[projects."{path}"]\ntrust_level = "trusted"\n'
            if added:
                record.parent.mkdir(parents=True, exist_ok=True)
                _replace(record, text + added)
    return paths


def mark(home: Path, repo: Path) -> None:
    """Records where the shared service log stood when the run began.

    Args:
        home: Private state directory the estate runs under.
        repo: Throwaway project the lanes coordinate over.

    One service writes one log for every project on the machine, so the
    verdict reads only what was appended after this instant.
    """
    log = home / "server.log"
    directory = repo / "acceptance"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "start.json").write_text(
        json.dumps(
            {
                "at": time.time(),
                "log": log.stat().st_size if log.exists() else 0,
            },
            indent=1,
        )
        + "\n"
    )


def _replace(path: Path, text: str) -> None:
    """Writes a file the native clients also write, without a half file.

    Args:
        path: File to replace.
        text: Whole new contents.
    """
    scratch = path.with_name(path.name + ".acceptance")
    scratch.write_text(text)
    os.replace(scratch, path)


def _lane(spec: str) -> tuple[str, str, str]:
    """Splits one lane specification into its parts.

    Args:
        spec: Lane as ``name:provider[:credentials]``.

    Returns:
        The lane's name, its provider, and its credential profile, which
        is empty when the lane uses the provider's default account.
    """
    parts = spec.split(":")
    name = parts[0]
    provider = parts[1] if len(parts) > 1 else name
    credentials = parts[2] if len(parts) > 2 else ""
    return name, provider, credentials


def launcher(cli: str, home: Path, repo: Path, lane: str) -> list[str]:
    """Spells the command that starts one lane's native client.

    Args:
        cli: Executable that speaks the coordination CLI.
        home: Private state directory the estate runs under.
        repo: Throwaway project the lanes coordinate over.
        lane: Lane specification as ``name:provider[:credentials]``.

    Returns:
        The argument vector the run hands to the lane's launcher.
    """
    name, provider, credentials = _lane(lane)
    command = [
        cli,
        "--home",
        str(home),
        "run",
        name,
        "--provider",
        provider,
        "--repo",
        str(repo),
        "--task",
        TASK,
    ]
    if credentials:
        command.extend(["--credentials", credentials])
    return command


def rehearse(cli: str, home: Path, repo: Path, lanes: list[str]) -> list[str]:
    """Samples a seeded estate once without starting any lane.

    Args:
        cli: Executable that speaks the coordination CLI.
        home: Private state directory the estate runs under.
        repo: Throwaway project the lanes coordinate over.
        lanes: Lane specifications as ``name:provider[:credentials]``.

    Returns:
        Each reading the run depends on that could not be taken, empty
        when the estate answered every one.

    The rehearsal starts the service the way a lane's launcher would, so
    a service that cannot serve this home is found before the period. It
    writes each lane's launch command to ``plan.json`` and
    one frame to ``frames.jsonl``, so the owner can read exactly what the
    period would start and confirm that the service the run samples is
    serving this project before any model quota is spent.
    """
    directory = repo / "acceptance"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "plan.json").write_text(
        json.dumps(
            {_lane(lane)[0]: launcher(cli, home, repo, lane) for lane in lanes},
            indent=1,
        )
        + "\n"
    )
    started = _run([cli, "--home", str(home), "up"])
    faults = (
        [f"up: {started.stderr.strip() or started.stdout.strip()}"]
        if started.returncode
        else []
    )
    taken = frame(cli, home, repo, lanes)
    with (directory / "frames.jsonl").open("a") as output:
        output.write(json.dumps(taken) + "\n")
    faults.extend(
        f"{reading}: {taken[reading]['error']}"
        for reading in ("issues", "problems", "metrics")
        if "error" in taken[reading]
    )
    faults.extend(
        f"problems: {row.get('detail', '')} ({row.get('command', '')})"
        for row in taken["problems"].get("problems") or []
        if row.get("condition") == "service"
    )
    if not taken["status"]:
        faults.append(f"status: no reading names {repo}")
    return faults


def launch(cli: str, home: Path, repo: Path, lanes: list[str]) -> dict:
    """Starts every lane's native client detached from this process.

    Args:
        cli: Executable that speaks the coordination CLI.
        home: Private state directory the estate runs under.
        repo: Throwaway project the lanes coordinate over.
        lanes: Lane specifications as ``name:provider[:credentials]``.

    Returns:
        Each lane's launcher process identifier, so the report can say
        which lanes were started and the operator can find them.

    Each launcher is given a pseudo-terminal of its own on standard
    input. A launcher started with no terminal runs its client without
    one, which `codex` refuses outright and which leaves a `claude` lane
    with no screen for the dialog watcher to read. Nothing ever writes to
    the controlling side; it is held open only so the client's terminal
    never reports end of file.
    """
    started: dict[str, int] = {}
    logs = repo / "acceptance" / "launch"
    logs.mkdir(parents=True, exist_ok=True)
    for lane in lanes:
        name = _lane(lane)[0]
        command = launcher(cli, home, repo, lane)
        controller, lane_terminal = pty.openpty()
        fcntl.ioctl(
            lane_terminal,
            termios.TIOCSWINSZ,
            struct.pack("HHHH", ROWS, COLUMNS, 0, 0),
        )
        with (logs / f"{name}.log").open("ab") as output:
            child = subprocess.Popen(
                command,
                stdin=lane_terminal,
                stdout=output,
                stderr=output,
                start_new_session=True,
            )
        os.close(lane_terminal)
        TERMINALS.append(controller)
        started[name] = child.pid
        time.sleep(SETTLE)
    return started


def lane_counters(home: Path, repo: Path, lanes: list[str]) -> dict:
    """Reads the durable per-lane counters the run is judged on.

    Args:
        home: Private state directory the estate runs under.
        repo: Throwaway project the lanes coordinate over.
        lanes: Lane specifications as ``name:provider[:credentials]``.

    Returns:
        Each lane's wake attempts, escalation and exhaustion marks, its
        published activity, and any dialog its launcher detected. A lane
        with no record yet reports zeros rather than nothing, so a lane
        that was never woken is visible as such.
    """
    directory = _directory(home, repo)
    counters: dict[str, dict] = {}
    for lane in lanes:
        name = _lane(lane)[0]
        wake = _read(directory / f"{name}-wake.json")
        state = _read(directory / f"{name}-activity.json")
        dialog = state.get("dialog") or {}
        counters[name] = {
            "wake_attempts": int(wake.get("attempts", 0) or 0),
            "wake_result": str(wake.get("result", "")),
            "wake_blocked": str(wake.get("blocked", "")),
            "escalated": bool(wake.get("escalated_at")),
            "exhausted": bool(wake.get("exhausted_at")),
            "activity": str(state.get("activity", "")),
            "dialog": str(dialog.get("name", "")),
            "dialog_escalated": bool(dialog.get("escalated")),
            "session_alive": state.get("session_pid") is not None,
        }
    return counters


def _read(path: Path) -> dict:
    """Reads one JSON state file, tolerating a file that is not there.

    Args:
        path: File to read.

    Returns:
        The parsed record, empty when the file is missing or unreadable.
    """
    try:
        record = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return record if isinstance(record, dict) else {}


def _directory(home: Path, repo: Path) -> Path:
    """Finds the private project directory the estate publishes into.

    Args:
        home: Private state directory the estate runs under.
        repo: Throwaway project the lanes coordinate over.

    Returns:
        The project's state directory, or the home directory itself when
        no project directory names this repository, which leaves the
        counters empty rather than raising inside a sampling loop.
    """
    for candidate in sorted((home / "projects").glob("*")):
        record = _read(candidate / "project.json")
        if record.get("root") == str(repo):
            return candidate
    return home


def frame(cli: str, home: Path, repo: Path, lanes: list[str]) -> dict:
    """Samples everything the service can see about the estate right now.

    Args:
        cli: Executable that speaks the coordination CLI.
        home: Private state directory the estate runs under.
        repo: Throwaway project the lanes coordinate over.
        lanes: Lane specifications as ``name:provider[:credentials]``.

    Returns:
        One frame holding the issue ledger, the problems view, the metric
        counters and the per-lane counters, stamped with the time it was
        taken.
    """
    scope = ["--repo", str(repo)]
    return {
        "at": time.time(),
        "issues": _document(cli, home, ["issue", "list", *scope]),
        "problems": _document(cli, home, ["problems"]),
        "metrics": _document(cli, home, ["metrics"]),
        "status": _project(_document(cli, home, ["status"]), repo),
        "lanes": lane_counters(home, repo, lanes),
    }


def _project(document: dict, repo: Path) -> dict:
    """Takes the throwaway project's own reading out of a status document.

    Args:
        document: Status document as the coordination CLI reported it.
        repo: Throwaway project the lanes coordinate over.

    Returns:
        The project's reading, empty when the document names no such
        project or could not be read at all.
    """
    for project in document.get("projects") or []:
        if project.get("root") == str(repo):
            return project
    return {}


def watch(
    cli: str,
    home: Path,
    repo: Path,
    lanes: list[str],
    hours: float,
    interval: float,
    frames: Path,
) -> None:
    """Samples the estate until the run's period ends.

    Args:
        cli: Executable that speaks the coordination CLI.
        home: Private state directory the estate runs under.
        repo: Throwaway project the lanes coordinate over.
        lanes: Lane specifications as ``name:provider[:credentials]``.
        hours: Length of the run.
        interval: Seconds between frames.
        frames: File each frame is appended to as one JSON line.
    """
    deadline = time.time() + hours * 3600
    frames.parent.mkdir(parents=True, exist_ok=True)
    while time.time() < deadline:
        with frames.open("a") as output:
            output.write(json.dumps(frame(cli, home, repo, lanes)) + "\n")
        time.sleep(min(interval, max(0.0, deadline - time.time())))


def _reports(cli: str, home: Path, repo: Path, issues: int) -> dict:
    """Reads how each backlog issue ended.

    Args:
        cli: Executable that speaks the coordination CLI.
        home: Private state directory the estate runs under.
        repo: Throwaway project the lanes coordinate over.
        issues: Number of backlog tasks the run seeded.

    Returns:
        Each issue number mapped to the last report recorded on it and
        the reason that report carried. An issue nobody reported on maps
        to an empty state, which is what fails the run.
    """
    endings: dict[str, dict] = {}
    for number in range(1, issues + 1):
        document = _document(
            cli, home, ["issue", "show", str(number), "--repo", str(repo)]
        )
        history = document.get("history") or []
        ending = {"state": "", "reason": "", "owner": ""}
        for event in history:
            action = str(event.get("action", ""))
            if action in ("report", "bounce", "release"):
                ending = {
                    "state": str(event.get("detail", {}).get("state", action)),
                    "reason": str(event.get("detail", {}).get("summary", "")),
                    "owner": str(event.get("participant", "")),
                }
        endings[str(number)] = ending
    return endings


def _log_faults(home: Path, repo: Path) -> dict:
    """Counts the service faults the run refuses to pass with.

    Args:
        home: Private state directory the estate runs under.
        repo: Throwaway project the lanes coordinate over.

    Returns:
        The number of broken pipes and hook expiries the service log
        holds, and whether the log could be read at all. Only the part
        of the log written after the run started is counted, because one
        service writes the log for every project on the machine and a
        fault from last week is not this run's.
    """
    path = home / "server.log"
    start = int(_read(repo / "acceptance" / "start.json").get("log", 0) or 0)
    try:
        with path.open(errors="replace") as handle:
            handle.seek(start)
            text = handle.read()
    except OSError:
        return {"readable": False, "broken_pipe": 0, "hook_expiry": 0}
    return {
        "readable": True,
        "broken_pipe": text.count("BrokenPipeError"),
        "hook_expiry": text.count("retry later") + text.count("expired"),
    }


def _worktrees(repo: Path, endings: dict) -> list[str]:
    """Lists worktrees still held for an issue that already reported.

    Args:
        repo: Throwaway project the lanes coordinate over.
        endings: Each issue's last recorded report.

    Returns:
        The worktree paths that outlived the claim they were created for.
    """
    result = _run(["git", "worktree", "list", "--porcelain"], cwd=repo)
    held = [
        line.split(" ", 1)[1]
        for line in result.stdout.splitlines()
        if line.startswith("worktree ")
    ]
    done = {
        number
        for number, ending in endings.items()
        if ending["state"] in ("ready", "merged")
    }
    return [path for path in held if Path(path).name.rsplit("-", 1)[-1] in done]


def samples(frames: Path) -> list[dict]:
    """Reads every frame the run recorded.

    Args:
        frames: File the run appended its frames to.

    Returns:
        The frames in the order they were taken. A line that cannot be
        parsed is dropped rather than raised, because a frame written
        while the run was killed must not lose the day behind it.
    """
    if not frames.exists():
        return []
    records: list[dict] = []
    with frames.open() as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                records.append(record)
    return records


def _idle_claims(taken: list[dict]) -> list[str]:
    """Names every lane that held a claim while it read as stalled.

    Args:
        taken: Frames the run recorded.

    Returns:
        One entry per lane and issue seen stalled with that claim open,
        which is the condition an operator would have had to rescue by
        hand.
    """
    found: set[str] = set()
    for record in taken:
        for lane in (record.get("status") or {}).get("participants") or []:
            if not (lane.get("idle") or {}).get("stalled"):
                continue
            for claim in lane.get("claims") or []:
                found.add(f"{lane.get('participant', '?')}#{claim['issue']}")
    return sorted(found)


def _stale_leases(taken: list[dict]) -> list[str]:
    """Names every lane that held an expired lease with no live session.

    Args:
        taken: Frames the run recorded.

    Returns:
        One entry per lane seen holding a reservation past its deadline
        while no session process of its own was alive, which is a key no
        peer can take and nobody is working under.
    """
    found: set[str] = set()
    for record in taken:
        for lane in (record.get("status") or {}).get("participants") or []:
            mail = lane.get("mail") or {}
            alive = (lane.get("availability") or {}).get("process_alive")
            if mail.get("stale_reservations") and not alive:
                found.add(str(lane.get("participant", "?")))
    return sorted(found)


def measure(
    home: Path,
    repo: Path,
    lanes: list[str],
    endings: dict,
    taken: list[dict],
) -> dict:
    """Measures the numbers the live acceptance record carries.

    Args:
        home: Private state directory the estate runs under.
        repo: Throwaway project the lanes coordinate over.
        lanes: Lane specifications as ``name:provider[:credentials]``.
        endings: Each backlog issue's last recorded report.
        taken: Frames the run recorded.

    Returns:
        The lane count, the issues any lane claimed, how many of those
        reported ready, the idle lane-minutes the lanes' event logs measure
        over the period, and the claim-minutes nothing accounted for.

    A claim's minute is accounted for when its owner reads as active or
    the problems view names the owner or a service or store fault, which
    is the rule the fault-injection suite applies. Each frame stands for
    the time until the next one, so the last frame adds nothing.
    """
    claimed = {number for number, ending in endings.items() if ending["state"]}
    unaccounted = 0.0
    for index, record in enumerate(taken):
        gap = (
            float(taken[index + 1].get("at", 0) or 0)
            - float(record.get("at", 0) or 0)
            if index + 1 < len(taken)
            else 0.0
        )
        rows = [
            row
            for row in (record.get("problems") or {}).get("problems") or []
            if row.get("project") == str(repo)
        ]
        faulted = any(
            row.get("condition") in {problems.SERVICE, problems.STORE}
            for row in rows
        )
        named = {row.get("participant") for row in rows}
        for lane in (record.get("status") or {}).get("participants") or []:
            claims = [str(claim["issue"]) for claim in lane.get("claims") or []]
            claimed.update(claims)
            state = (lane.get("availability") or {}).get("state")
            if (
                claims
                and not faulted
                and state != supervision.ACTIVE
                and lane.get("participant") not in named
            ):
                unaccounted += gap * len(claims)
    directory = _directory(home, repo)
    idle = 0
    if taken and directory != home:
        idle = sum(
            metrics.idle_intervals(
                directory,
                _lane(lane)[0],
                since=float(taken[0].get("at", 0) or 0),
                now=float(taken[-1].get("at", 0) or 0),
            )["seconds"]
            for lane in lanes
        )
    return {
        "lanes": len(lanes),
        "claims": len(claimed),
        "claims_completed": sum(
            1
            for number in claimed
            if endings.get(number, {}).get("state") == "ready"
        ),
        "idle_lane_minutes": round(idle / 60, 1),
        "unaccountable_claim_minutes": round(unaccounted / 60, 1),
    }


def acceptance_record(decided: dict, version: str, run: str) -> dict:
    """Builds the live acceptance record a release reads.

    Args:
        decided: The verdict a finished run produced.
        version: Version the run validates.
        run: Link to the run's published report.

    Returns:
        The record ``scripts.release_publish`` checks, holding the version,
        the report link and the measured numbers exactly as the run took
        them.
    """
    return {"version": version, "run": run, **decided["measured"]}


def verdict(
    cli: str,
    home: Path,
    repo: Path,
    lanes: list[str],
    issues: int,
    frames: Path,
) -> dict:
    """Decides the run against the conditions an operator would check.

    Args:
        cli: Executable that speaks the coordination CLI.
        home: Private state directory the estate runs under.
        repo: Throwaway project the lanes coordinate over.
        lanes: Lane specifications as ``name:provider[:credentials]``.
        issues: Number of backlog tasks the run seeded.
        frames: File the run's frames were appended to.

    Returns:
        Each condition's own result with the evidence behind it, and the
        run's overall result. A condition that could not be measured
        fails rather than passes, because an unmeasured estate is exactly
        what this run exists to replace.

    The problems view reports the whole estate, so its rows are narrowed
    to this project: a home that also runs the operator's real work would
    otherwise decide this run on somebody else's lanes.
    """
    endings = _reports(cli, home, repo, issues)
    unreported = [
        number
        for number, ending in endings.items()
        if ending["state"] not in ("ready", "blocked", "bounced")
        or (ending["state"] != "ready" and not ending["reason"])
    ]
    final = _document(cli, home, ["problems"])
    rows = [
        row
        for row in (final.get("problems") or [])
        if row.get("project") == str(repo)
    ]
    unattended = [
        row
        for row in rows
        if row.get("actor") != "operator" or not row.get("command")
    ]
    if "error" in final:
        unattended.append({"condition": "unreadable", "detail": final})
    faults = _log_faults(home, repo)
    stranded = _worktrees(repo, endings)
    counters = lane_counters(home, repo, lanes)
    taken = samples(frames)
    idled = _idle_claims(taken)
    leaked = _stale_leases(taken)
    escalations = {
        name: record
        for name, record in counters.items()
        if record["escalated"] or record["exhausted"]
    }
    conditions = {
        "every backlog issue reported": {
            "passed": not unreported,
            "evidence": f"{issues - len(unreported)} of {issues} reported",
            "detail": unreported,
        },
        "problems holds only operator rows": {
            "passed": not unattended,
            "evidence": f"{len(rows)} rows, {len(unattended)} unattended",
            "detail": unattended,
        },
        "no worktree outlived its claim": {
            "passed": not stranded,
            "evidence": f"{len(stranded)} stranded",
            "detail": stranded,
        },
        "service log is clean": {
            "passed": faults["readable"]
            and not faults["broken_pipe"]
            and not faults["hook_expiry"],
            "evidence": json.dumps(faults),
            "detail": faults,
        },
        "no lane idled on an open claim": {
            "passed": not idled,
            "evidence": f"{len(idled)} lanes stalled holding a claim",
            "detail": idled,
        },
        "no lease outlived its holder": {
            "passed": not leaked,
            "evidence": f"{len(leaked)} lanes held an expired lease",
            "detail": leaked,
        },
        "no lane escalated or exhausted": {
            "passed": not escalations,
            "evidence": f"{len(escalations)} lanes escalated",
            "detail": sorted(escalations),
        },
    }
    return {
        "passed": all(entry["passed"] for entry in conditions.values()),
        "conditions": conditions,
        "lanes": counters,
        "endings": endings,
        "frames": len(taken),
        "measured": measure(home, repo, lanes, endings, taken),
    }


def report(decided: dict, repo: Path, hours: float) -> str:
    """Renders the run's report as Markdown.

    Args:
        decided: The verdict the run produced.
        repo: Throwaway project the lanes coordinated over.
        hours: Length of the run.

    Returns:
        The report text, which is the artifact attached to a release.
    """
    lines = [
        "# Unattended acceptance run",
        "",
        f"Project: `{repo}`",
        f"Period: {hours:g}h, {decided['frames']} frames",
        f"Result: {'PASS' if decided['passed'] else 'FAIL'}",
        "",
        "## Conditions",
        "",
        "| Condition | Result | Evidence |",
        "| --- | --- | --- |",
    ]
    for name, entry in decided["conditions"].items():
        state = "pass" if entry["passed"] else "FAIL"
        lines.append(f"| {name} | {state} | {entry['evidence']} |")
    lines.extend(
        [
            "",
            "## Lanes",
            "",
            "| Lane | Wakes | Result | Dialog | Activity |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for name, record in sorted(decided["lanes"].items()):
        lines.append(
            f"| {name} | {record['wake_attempts']} | "
            f"{record['wake_result'] or '-'} | "
            f"{record['dialog'] or '-'} | {record['activity'] or '-'} |"
        )
    lines.extend(
        [
            "",
            "## Backlog",
            "",
            "| Issue | State | Owner |",
            "| --- | --- | --- |",
        ]
    )
    for number, ending in sorted(
        decided["endings"].items(), key=lambda item: int(item[0])
    ):
        lines.append(
            f"| {number} | {ending['state'] or 'none'} | "
            f"{ending['owner'] or '-'} |"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    """Runs one acceptance run end to end.

    Args:
        argv: Command line arguments, taken from the process when None.

    Returns:
        Zero when the run passed every condition, one when it did not.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "command", choices=("run", "verdict", "rehearse", "record")
    )
    parser.add_argument("--home", default=os.environ.get("AGENT_PARLEY_HOME"))
    parser.add_argument("--workspace", default="")
    parser.add_argument(
        "--cli", default=str(Path(sys.executable).with_name("agent-parley"))
    )
    parser.add_argument("--hours", type=float, default=HOURS)
    parser.add_argument("--interval", type=float, default=INTERVAL)
    parser.add_argument("--issues", type=int, default=BACKLOG)
    parser.add_argument("--lane", action="append", default=[])
    parser.add_argument("--trust", action="store_true")
    parser.add_argument("--version", default="")
    parser.add_argument("--run", default="")
    arguments = parser.parse_args(argv)
    if arguments.command == "record":
        if not (arguments.workspace and arguments.version and arguments.run):
            parser.error("record needs --workspace, --version and --run")
        decided = _read(
            Path(arguments.workspace).expanduser()
            / "acceptance"
            / "verdict.json"
        )
        if "measured" not in decided:
            parser.error("the workspace holds no finished verdict")
        path = RECORDS / f"{arguments.version}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                acceptance_record(decided, arguments.version, arguments.run),
                indent=2,
            )
            + "\n"
        )
        sys.stdout.write(f"{path}\n")
        return 0
    if not arguments.home:
        parser.error("--home or AGENT_PARLEY_HOME is required")
    home = Path(arguments.home).expanduser()
    lanes = arguments.lane or list(LANES)
    repo = Path(
        arguments.workspace or ROOT / f"run-{time.strftime('%Y%m%d-%H%M%S')}"
    ).expanduser()
    frames = repo / "acceptance" / "frames.jsonl"
    if arguments.command == "rehearse":
        workspace(repo, arguments.issues)
        register(arguments.cli, home, repo, lanes)
        mark(home, repo)
        faults = rehearse(arguments.cli, home, repo, lanes)
        sys.stdout.write(
            "".join(f"{fault}\n" for fault in faults)
            or f"rehearsed {len(lanes)} lanes in {repo}\n"
        )
        return 1 if faults else 0
    if arguments.command == "run":
        workspace(repo, arguments.issues)
        register(arguments.cli, home, repo, lanes)
        if arguments.trust:
            trust(home, repo, lanes)
        mark(home, repo)
        started = launch(arguments.cli, home, repo, lanes)
        (repo / "acceptance" / "launched.json").write_text(
            json.dumps(started, indent=1)
        )
        watch(
            arguments.cli,
            home,
            repo,
            lanes,
            arguments.hours,
            arguments.interval,
            frames,
        )
    decided = verdict(
        arguments.cli, home, repo, lanes, arguments.issues, frames
    )
    text = report(decided, repo, arguments.hours)
    (repo / "acceptance" / "report.md").write_text(text)
    (repo / "acceptance" / "verdict.json").write_text(
        json.dumps(decided, indent=1)
    )
    sys.stdout.write(text)
    return 0 if decided["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
