"""Unattended acceptance run for a live lane estate.

The integration suite drives synthetic clients, so it proves the protocol
and nothing about a day of real native clients. This module runs a whole
estate against a throwaway project for a fixed period with no operator
input, samples what the service can see while it runs, and decides the
run against the conditions an operator would otherwise have to check by
hand.

One command starts it::

    python -m scripts.acceptance run --hours 24

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
import json
import os
import subprocess
import sys
import time
from pathlib import Path

BACKLOG = 20
HOURS = 24.0
INTERVAL = 300.0
SETTLE = 20.0
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

Work this loop until the backlog is empty:

1. Run `agent-parley issue list` and pick the lowest numbered task in
   `tasks/` that no lane owns.
2. Claim it with `agent-parley issue claim <number>`.
3. Read `tasks/<number>.md` in your worktree and make exactly the change
   it asks for, with a test.
4. Run the project's verify command. When it passes, commit and run
   `agent-parley report ready <number> --summary "<what you changed>"`.
   When the task cannot be done as written, run
   `agent-parley report blocked <number> --summary "<the reason>"` and
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
        failed or printed something that is not JSON. A sampling failure
        is recorded rather than raised, because the run must survive one
        bad frame.
    """
    result = _run([cli, "--home", str(home), *args, "--json"])
    if result.returncode:
        return {"error": result.stderr.strip() or result.stdout.strip()}
    try:
        return json.loads(result.stdout)
    except ValueError:
        return {"error": result.stdout[:400]}


def workspace(path: Path, issues: int) -> None:
    """Creates the throwaway project the estate coordinates over.

    Args:
        path: Directory the project is created in.
        issues: Number of backlog tasks to write.

    Raises:
        SystemExit: The directory already holds a repository, which would
            put the run on somebody's real work.
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
    _run(["git", "init", "-b", "main"], cwd=path)
    _run(["git", "add", "-A"], cwd=path)
    _run(
        ["git", "commit", "-m", "chore: seed the acceptance backlog"], cwd=path
    )


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
    """
    started: dict[str, int] = {}
    logs = repo / "acceptance" / "launch"
    logs.mkdir(parents=True, exist_ok=True)
    for lane in lanes:
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
        with (logs / f"{name}.log").open("ab") as output:
            child = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=output,
                start_new_session=True,
            )
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


def _log_faults(home: Path) -> dict:
    """Counts the service faults the run refuses to pass with.

    Args:
        home: Private state directory the estate runs under.

    Returns:
        The number of broken pipes and hook expiries the service log
        holds, and whether the log could be read at all.
    """
    path = home / "server.log"
    try:
        text = path.read_text(errors="replace")
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
    """
    endings = _reports(cli, home, repo, issues)
    unreported = [
        number
        for number, ending in endings.items()
        if ending["state"] not in ("ready", "blocked", "bounced")
        or (ending["state"] != "ready" and not ending["reason"])
    ]
    final = _document(cli, home, ["problems"])
    rows = final.get("problems") or []
    unattended = [
        row
        for row in rows
        if row.get("actor") != "operator" or not row.get("command")
    ]
    faults = _log_faults(home)
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
    parser.add_argument("command", choices=("run", "verdict"))
    parser.add_argument("--home", default=os.environ.get("AGENT_PARLEY_HOME"))
    parser.add_argument("--workspace", default="")
    parser.add_argument("--cli", default="agent-parley")
    parser.add_argument("--hours", type=float, default=HOURS)
    parser.add_argument("--interval", type=float, default=INTERVAL)
    parser.add_argument("--issues", type=int, default=BACKLOG)
    parser.add_argument("--lane", action="append", default=[])
    arguments = parser.parse_args(argv)
    if not arguments.home:
        parser.error("--home or AGENT_PARLEY_HOME is required")
    home = Path(arguments.home).expanduser()
    lanes = arguments.lane or list(LANES)
    repo = Path(
        arguments.workspace
        or f"/tmp/parley-acceptance-{time.strftime('%Y%m%d-%H%M%S')}"
    ).expanduser()
    frames = repo / "acceptance" / "frames.jsonl"
    if arguments.command == "run":
        workspace(repo, arguments.issues)
        register(arguments.cli, home, repo, lanes)
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
