"""Records the coordination demo and renders it as an animated SVG.

The recording is produced by running Agent Parley, never by writing terminal
text. Every frame holds one real command line and the bytes that command
wrote to a pseudo-terminal of fixed size, so the wrapping, the table widths
and the refusal text are the ones an operator sees.

The run is told in nine chapters, each opened by a title card: parallel
lanes on two providers, a work order every lane reads, claims and advisory
reservations with the collision a second lane meets, guardrails in the native
hooks, a handoff offer and its acceptance, a plan revision, a run budget,
unattended integration, and the operator dashboard. A closing card names the
install command.

The native CLIs are stubs. The recorder writes ``claude`` and ``codex``
executables that run ``exec sleep 900`` onto the front of ``PATH``, so the
lane processes are real, the coordination path is real, and only the model
session is a stand-in. Lanes are launched one at a time because two
simultaneous launches race for the same server lock.

Reservations have no command-line verb; they are MCP tools. Those steps call
the same tool dispatch the served transport calls, through
``agent_parley.store.call`` with the lane's own registration credential, and
the frame shows the tool name, its arguments and its real result.

Rendering needs no recorder binary. ``vhs``, ``asciinema`` and ``agg`` are
not used and are not required: this module writes the SVG itself from the
captured frames, giving each frame one ``animate`` element so exactly one is
visible at a time. A renderer that ignores animation shows the first frame.

Nothing from the recording machine survives into the asset. The temporary
state directory, the demo repository and the operator's home are rewritten
to a ``/home/dev`` shape before a frame is drawn.

Regenerate with ``make demo-stub``, or::

    uv run --locked python scripts/record_demo.py

The result is written to ``docs/assets/demo.svg`` and is the published
asset. ``scripts/record_live.py`` (``make demo``) records real clients
instead and needs model quota. The asset is
referenced from ``README.md`` through a pinned jsdelivr URL, because the
README is also the PyPI long description and relative image paths do not
resolve there.
"""

import contextlib
import dataclasses
import fcntl
import json
import os
import pty
import re
import select
import shlex
import socket
import struct
import subprocess
import sys
import tempfile
import termios
import textwrap
import time
from collections.abc import Sequence
from pathlib import Path

COLUMNS = 132
ROWS = 30
FRAME_LINES = 24
TYPE_RATE = 0.028
TYPE_LIMIT = 1.6
FADE = 0.35
CAPTION = 48.0
SANS = "system-ui, -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif"
CHARACTER = 7.81
LINE = 18.0
MARGIN = 18.0
HEADER = 30.0
DEFAULT_PORT = 8876
ISSUE = "41"
PATTERN = "src/payments/**"
DEMO_HOME = "/home/dev"
DEMO_REPOSITORY = "/home/dev/payments-api"
DEMO_STATE = "/home/dev/.local/state/agent-parley"
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[=>]|\x1b\][^\x07]*\x07")
OFFER_ID = re.compile(r'"id": "([0-9a-f]{16,})"')
PROPOSAL_ID = re.compile(r"^([0-9a-f]{16}) pending", re.MULTILINE)
INHERITED = ("AGENT_PARLEY_", "CLAUDE", "CODEX_")
WORK_ORDER = """[plan]
name = "Refund rework"

[dependencies]
"42" = ["41"]

[groups]
refunds = ["41", "42"]
"""
REFUND_FIX = '''"""Refund path the lanes negotiate over."""


def refund(cents: int) -> int:
    """Returns the ledger entry for a refund of whole cents."""
    return -abs(cents)
'''
BACKGROUND = "#0d1117"
BAR = "#161b22"
MUTED = "#8b949e"
PROMPT = "#7ee787"
COMMAND = "#e6edf3"
BODY = "#c9d1d9"
HEADING = "#79c0ff"
REFUSAL = "#ff7b72"
TOOL = "#d2a8ff"
REFUSED = ("deny", "denied", "refus", "conflict", "blocked", "queued")
EMPHASIS = (
    "PARTICIPANT",
    "Project:",
    "Server:",
    "State:",
    "project ",
    "agent-parley top",
    "projects ",
    "Lanes:",
    "Mail:",
)


@dataclasses.dataclass(frozen=True)
class Step:
    """One recorded frame.

    Attributes:
        prompt: The leading marker drawn before the command, ``$`` for a
            shell command and the lane name for a tool call.
        command: The command line or tool call as it was issued.
        output: The lines the step wrote, already stripped of escapes.
        caption: Narration drawn in the band under the terminal, never
            inside it, so it cannot be mistaken for captured output.
        limit: Most output lines the frame shows before it states that
            the rest is not shown.
    """

    prompt: str
    command: str
    output: tuple[str, ...]
    caption: str = ""
    limit: int = FRAME_LINES


@dataclasses.dataclass(frozen=True)
class Card:
    """One title card drawn between chapters of the recording.

    A card fills the whole canvas and draws no terminal, so its text reads
    as editorial narration rather than as anything a command printed.

    Attributes:
        kicker: Small line above the title, such as the chapter number.
        title: The chapter or recording title.
        lines: Lines drawn under the title.
        seconds: How long the card stays on screen.
    """

    kicker: str
    title: str
    lines: tuple[str, ...] = ()
    seconds: float = 3.6


def free_port() -> int:
    """Reserves a loopback port the demo server can bind.

    Returns:
        The documented default port when nothing holds it, so the frames
        read like a first run, and otherwise any free port, so a recording
        never collides with an operator's own server.
    """
    for candidate in (DEFAULT_PORT, 0):
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", candidate))
            except OSError:
                continue
            return int(probe.getsockname()[1])
    raise RuntimeError("no loopback port was available")


def git(*arguments: str, cwd: Path | None = None) -> None:
    """Runs one Git command for the demo fixtures.

    Args:
        *arguments: Arguments after the program name.
        cwd: Directory to run in, or None for the process directory.

    Raises:
        RuntimeError: If Git reported a failure.
    """
    done = subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode:
        raise RuntimeError(f"git {' '.join(arguments)}: {done.stderr.strip()}")


def fixtures(base: Path) -> tuple[Path, Path, Path]:
    """Builds the temporary state directory, stub CLIs and demo repository.

    Args:
        base: Directory every artefact of this run is created under.

    Returns:
        The coordination home, the stub executable directory and the demo
        repository root.
    """
    home = base / "state"
    binaries = base / "bin"
    repository = base / "payments-api"
    for path in (home, binaries, repository / "src" / "payments"):
        path.mkdir(parents=True)
    home.chmod(0o700)
    for tool in ("claude", "codex"):
        stub = binaries / tool
        stub.write_text("#!/usr/bin/env bash\nexec sleep 900\n")
        stub.chmod(0o755)
    module = repository / "src" / "payments" / "refund.py"
    module.write_text('"""Refund path the lanes negotiate over."""\n')
    git("init", "-q", "-b", "main", str(repository))
    git("config", "user.name", "dev", cwd=repository)
    git("config", "user.email", "dev@example.com", cwd=repository)
    git("add", "--all", cwd=repository)
    git("commit", "-qm", "Add the refund path", cwd=repository)
    (base / "work-order.toml").write_text(WORK_ORDER)
    return home, binaries, repository


def environment(home: Path, binaries: Path, base: Path) -> dict[str, str]:
    """Builds the environment every recorded command runs under.

    Args:
        home: Coordination state directory for this run.
        binaries: Directory holding the stub native CLIs.
        base: Directory the native configuration homes are created under.

    Returns:
        A copy of the process environment pinned to the temporary state,
        the stub executables and a free loopback port. Variables a native
        client or an enclosing lane set are dropped, so a recording started
        from inside a lane runs with operator authority and carries none of
        that lane's credentials.
    """
    values = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(INHERITED)
    }
    values["PATH"] = f"{binaries}{os.pathsep}{values['PATH']}"
    values["AGENT_PARLEY_HOME"] = str(home)
    values["AGENT_PARLEY_PORT"] = str(free_port())
    values["CLAUDE_CONFIG_DIR"] = str(base / "native" / "claude")
    values["CODEX_HOME"] = str(base / "native" / "codex")
    values["COLUMNS"] = str(COLUMNS)
    values["LINES"] = str(ROWS)
    values["NO_COLOR"] = "1"
    return values


def lower_stall_windows(directory: Path) -> None:
    """Shortens a project's supervision windows for a screenshot run.

    A lane only reads as idle once its silence passes the project's
    ``stalled_after`` window, which defaults to ten minutes. A screenshot
    generator that waited that long to show a genuine idle label would
    make ``make demo screenshots`` impractical, so this rewrites the
    project's own manifest, the legitimate place that setting lives,
    rather than faking the label. ``interval`` drops to two seconds for
    the same reason: lane records and published fitness move only on a
    supervision poll, and a poll taken before the later lanes launched
    would otherwise stand for the default thirty seconds. Call this before
    the first launch, since the poll a launch triggers schedules the next
    one from the interval it reads. ``inactive_after`` is left at its
    default: it also governs whether a launched session still reads as
    running, and lowering it would misreport every quiet lane as stopped.

    Args:
        directory: Private project state directory holding
            ``project.json``.
    """
    path = directory / "project.json"
    manifest = json.loads(path.read_text())
    manifest["supervision"] = {
        **manifest.get("supervision", {}),
        "stalled_after": 2,
        "interval": 2,
    }
    path.write_text(json.dumps(manifest))


def terminal() -> tuple[int, int]:
    """Opens a pseudo-terminal sized like the recorded frame.

    Returns:
        The controlling and child file descriptors; the caller closes the
        child once the subprocess owns it.
    """
    controller, child = pty.openpty()
    size = struct.pack("HHHH", ROWS, COLUMNS, 0, 0)
    fcntl.ioctl(child, termios.TIOCSWINSZ, size)
    return controller, child


def drain(controller: int, deadline: float) -> str:
    """Reads a pseudo-terminal until it closes or the deadline passes.

    Args:
        controller: Controlling file descriptor of the pseudo-terminal.
        deadline: Monotonic time to stop reading at.

    Returns:
        Everything the child wrote, decoded and with escapes removed.
    """
    chunks: list[bytes] = []
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if not select.select([controller], [], [], remaining)[0]:
            break
        try:
            data = os.read(controller, 65536)
        except OSError:
            break
        if not data:
            break
        chunks.append(data)
    return clean(b"".join(chunks).decode("utf-8", "replace"))


def clean(text: str) -> str:
    """Removes terminal escapes and carriage returns from captured bytes.

    Args:
        text: Raw pseudo-terminal output.

    Returns:
        The same output as plain lines.
    """
    return ANSI.sub("", text).replace("\r\n", "\n").replace("\r", "")


class Recorder:
    """Drives the demo and collects one frame per step.

    Attributes:
        home: Coordination state directory for this run.
        repository: Demo repository root.
        env: Environment every recorded command runs under.
        steps: Frames captured so far, in order.
        lanes: Lane worktrees keyed by participant name.
        sessions: Launched lane processes, kept alive for the whole run.
        attached: Controlling terminals of those launches; closing one
            hangs up its lane, which would read as a stopped session in the
            dashboard, so they stay open until the recording ends.
    """

    def __init__(self, home: Path, repository: Path, env: dict[str, str]):
        """Stores the run's directories and prepares empty frame state.

        Args:
            home: Coordination state directory for this run.
            repository: Demo repository root.
            env: Environment every recorded command runs under.
        """
        self.home = home
        self.repository = repository
        self.env = env
        self.steps: list[Step | Card] = []
        self.lanes: dict[str, Path] = {}
        self.sessions: list[subprocess.Popen[bytes]] = []
        self.attached: list[int] = []

    def parley(self, *arguments: str) -> list[str]:
        """Builds the argument vector that runs the installed command.

        Args:
            *arguments: Arguments after the program name.

        Returns:
            The interpreter invocation of the package entry point, which is
            the installed ``agent-parley`` command without a launcher on
            PATH.
        """
        return [sys.executable, "-m", "agent_parley", *arguments]

    def run(
        self, *arguments: str, cwd: Path | None = None, timeout: float = 90.0
    ) -> str:
        """Records one ``agent-parley`` command and its terminal output.

        Args:
            *arguments: Arguments after the program name.
            cwd: Directory to run in, defaulting to the demo repository.
            timeout: Seconds to wait for the command to finish.

        Returns:
            The captured output, so a later step can read an identifier out
            of it.
        """
        controller, child = terminal()
        process = subprocess.Popen(
            self.parley(*arguments),
            cwd=cwd or self.repository,
            env=self.env,
            stdin=child,
            stdout=child,
            stderr=child,
        )
        os.close(child)
        output = drain(controller, time.monotonic() + timeout)
        os.close(controller)
        process.wait(timeout=timeout)
        self.steps.append(
            Step("$", shlex.join(("agent-parley", *arguments)), lines(output))
        )
        return output

    def shell(self, *arguments: str, cwd: Path) -> str:
        """Records one plain shell command run inside a lane worktree.

        Args:
            *arguments: The command and its arguments.
            cwd: Directory to run in.

        Returns:
            The captured output.
        """
        controller, child = terminal()
        process = subprocess.Popen(
            list(arguments),
            cwd=cwd,
            env=self.env,
            stdin=child,
            stdout=child,
            stderr=child,
        )
        os.close(child)
        output = drain(controller, time.monotonic() + 30.0)
        os.close(controller)
        process.wait(timeout=30.0)
        self.steps.append(Step("$", shlex.join(arguments), lines(output)))
        return output

    def launch(self, name: str) -> None:
        """Records one lane launch and keeps that lane's session alive.

        Args:
            name: Participant to launch.

        Raises:
            RuntimeError: If the launch printed no worktree path.
        """
        controller, child = terminal()
        process = subprocess.Popen(
            self.parley("run", name, "--repo", str(self.repository)),
            cwd=self.repository,
            env=self.env,
            stdin=child,
            stdout=child,
            stderr=child,
        )
        os.close(child)
        self.sessions.append(process)
        self.attached.append(controller)
        output = drain(controller, time.monotonic() + 8.0)
        captured = lines(output)
        if not captured or ": " not in captured[0]:
            raise RuntimeError(f"run {name} printed no worktree: {output!r}")
        self.lanes[name] = Path(captured[0].split(": ", 1)[1].strip())
        self.steps.append(Step("$", f"agent-parley run {name}", captured))

    def tool(self, name: str, tool: str, arguments: dict) -> dict:
        """Records one MCP tool call made with a lane's own credential.

        Args:
            name: Participant making the call.
            tool: Tool name as the served transport exposes it.
            arguments: Tool arguments.

        Returns:
            The tool result, so a later step can read it.

        Raises:
            RuntimeError: If the lane's credential was refused.
        """
        from agent_parley import store

        directory = next(iter(self.lanes.values())).parent
        identity = directory / f"{name}-identity.json"
        token = json.loads(identity.read_text())["registration_token"]
        actor = store.authenticate(self.home, token)
        if actor is None:
            raise RuntimeError(f"{name} was not registered")
        result = store.call(self.home, actor, tool, arguments)
        rendered = json.dumps(arguments, separators=(", ", ": "))
        self.steps.append(
            Step(
                name,
                f"{tool} {rendered}",
                lines(json.dumps(result, indent=2)),
            )
        )
        return result

    def event(self, name: str, payload: dict) -> str:
        """Serves one native hook event the way a launched client does.

        Args:
            name: Participant whose session raised the event.
            payload: The event body the native client writes on standard
                input.

        Returns:
            The decision document the hook wrote, with escapes removed.
        """
        lane = self.lanes[name]
        done = subprocess.run(
            [
                sys.executable,
                "-m",
                "agent_parley.hook",
                "--home",
                str(self.home),
                "--directory",
                str(lane.parent),
                "--agent",
                name,
            ],
            cwd=lane,
            env=self.env,
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            check=False,
        )
        return clean(done.stdout + done.stderr)

    def session(self, name: str) -> None:
        """Serves the session-start event a launched client raises.

        The stub native CLI raises no events of its own, so the recorder
        serves the one a real session would, which is what moves the lane
        out of its starting state and delivers its briefing.

        Args:
            name: Participant whose session started.
        """
        self.event(
            name,
            {
                "hook_event_name": "SessionStart",
                "session_id": f"demo-{name}",
                "source": "startup",
                "cwd": str(self.lanes[name]),
            },
        )

    def prompt(self, name: str, text: str) -> None:
        """Serves the prompt event a client raises when it is given work.

        Args:
            name: Participant whose session received the prompt.
            text: Prompt the operator typed, which ``top`` shows as TASK.
        """
        self.event(
            name,
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": f"demo-{name}",
                "cwd": str(self.lanes[name]),
                "prompt": text,
            },
        )

    def hook(self, name: str, command: str) -> None:
        """Records the native hook's decision on one tool call.

        Args:
            name: Participant whose session raised the event.
            command: Shell command the native client asked to run.
        """
        decision = self.event(
            name,
            {
                "hook_event_name": "PreToolUse",
                "session_id": f"demo-{name}",
                "cwd": str(self.lanes[name]),
                "tool_name": "Bash",
                "tool_input": {"command": command},
            },
        )
        self.steps.append(
            Step(
                name,
                f"PreToolUse Bash: {command}",
                lines(json.dumps(json.loads(decision), indent=2)),
            )
        )

    def chapter(
        self, kicker: str, title: str, *text: str, seconds: float = 3.6
    ) -> None:
        """Adds a title card before the steps that follow.

        Args:
            kicker: Small line above the title.
            title: Chapter title.
            *text: Lines drawn under the title.
            seconds: How long the card stays on screen.
        """
        self.steps.append(Card(kicker, title, text, seconds))

    def latest(self) -> Step:
        """Returns the step just recorded.

        Returns:
            The last frame recorded.

        Raises:
            RuntimeError: If the last frame is a title card.
        """
        step = self.steps[-1]
        if not isinstance(step, Step):
            raise RuntimeError("The last frame is a title card, not a step.")
        return step

    def caption(self, text: str, limit: int = FRAME_LINES) -> None:
        """Narrates the step just recorded.

        Args:
            text: Caption drawn in the band under the terminal.
            limit: Most output lines that step's frame shows.
        """
        self.steps[-1] = dataclasses.replace(
            self.latest(), caption=text, limit=limit
        )

    def stop(self, name: str) -> None:
        """Ends one lane's session the way a finished native client exits.

        Args:
            name: Participant whose session ends.
        """
        index = list(self.lanes).index(name)
        process = self.sessions[index]
        process.terminate()
        process.wait(timeout=10)

    def close(self) -> None:
        """Stops the lane sessions and the coordination server."""
        for process in self.sessions:
            with contextlib.suppress(OSError):
                process.terminate()
        for process in self.sessions:
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=10)
        for descriptor in self.attached:
            with contextlib.suppress(OSError):
                os.close(descriptor)
        subprocess.run(
            self.parley("down"),
            cwd=self.repository,
            env=self.env,
            capture_output=True,
            check=False,
        )


def lines(text: str) -> tuple[str, ...]:
    """Splits captured output into frame lines without trailing blanks.

    Args:
        text: Captured output.

    Returns:
        The lines of that output, right-stripped, wrapped at the frame
        width the way the terminal wraps, and without trailing blank
        lines. Output captured from a pseudo-terminal is already wrapped;
        a tool result rendered as a document is not.
    """
    rows: list[str] = []
    for row in text.split("\n"):
        stripped = row.rstrip()
        if not stripped:
            rows.append(stripped)
        while stripped:
            rows.append(stripped[:COLUMNS])
            stripped = stripped[COLUMNS:]
    while rows and not rows[-1]:
        rows.pop()
    return tuple(rows)


def record(recorder: Recorder) -> None:
    """Drives every coordination feature the recording shows.

    Args:
        recorder: Recorder collecting the frames.

    Each participant is launched before the next is added: a lane added
    but never launched would otherwise sit beside a launched one while the
    first supervision poll runs, which is not the order an operator uses.

    Raises:
        RuntimeError: If a handoff offer or a plan proposal carried no
            identifier.
    """
    root = str(recorder.repository)
    order = str(recorder.repository.parent / "work-order.toml")
    recorder.chapter(
        "AGENT PARLEY 0.13.0",
        "Many coding agents. One repository.",
        "Claims, advisory reservations, handoffs, plans, budgets",
        "and unattended integration, for claude and codex side by side.",
        "Every frame below is real command output.",
        seconds=6.0,
    )

    recorder.chapter(
        "CHAPTER 1",
        "Parallel lanes",
        "One coordination server. Each agent gets its own worktree,",
        "identity and inbox; the base checkout is never edited.",
    )
    recorder.run("up")
    recorder.caption("One loopback coordination server serves every project.")
    recorder.run("setup", root)
    recorder.caption("Register the repository once.", limit=10)
    recorder.run("forge", "set", "null", "--repo", root)
    recorder.caption("No forge for this demo: everything stays local.")
    recorder.run("participant", "add", "ada", "--provider", "claude")
    recorder.launch("ada")
    recorder.caption("ada runs on claude, in a worktree of its own.")
    recorder.run("participant", "add", "grace", "--provider", "codex")
    recorder.launch("grace")
    recorder.caption("grace runs on codex, right beside it.")
    recorder.session("ada")
    recorder.session("grace")
    ada = recorder.lanes["ada"]
    grace = recorder.lanes["grace"]
    recorder.run("status")
    recorder.caption("Two providers, two lanes, one shared view.")

    recorder.chapter(
        "CHAPTER 2",
        "A work order everyone reads",
        "Dependencies live in one reviewed TOML file,",
        "not in anyone's head.",
    )
    recorder.run("plan", "apply", order)
    recorder.caption("#42 waits on #41; both belong to the refunds group.")
    recorder.run("plan", "show")
    recorder.caption("The plan as a tree, with each issue's current owner.")

    recorder.chapter(
        "CHAPTER 3",
        "Claims and advisory reservations",
        "An agent claims an issue and reserves what it will touch.",
        "A second agent is told, not silently overwritten.",
    )
    recorder.run("issue", "claim", ISSUE, cwd=ada)
    recorder.caption("ada claims #41 from inside its lane.", limit=12)
    recorder.tool("ada", "file_reservation_paths", {"paths": [PATTERN]})
    recorder.caption("ada reserves the refund module.", limit=16)
    recorder.tool("grace", "file_reservation_paths", {"paths": [PATTERN]})
    recorder.caption("grace asks for the same paths: a named collision.")
    recorder.tool("grace", "request_reservation", {"paths": [PATTERN]})
    recorder.caption("So grace queues behind ada instead of waiting blind.")

    recorder.chapter(
        "CHAPTER 4",
        "Guardrails in the native hooks",
        "Lanes stay on their assigned branch.",
        "The refusal says why, and what to do instead.",
    )
    recorder.hook("ada", "git checkout -b hotfix/refund")
    recorder.caption("A branch switch inside a lane is refused before it runs.")

    recorder.chapter(
        "CHAPTER 5",
        "Handoffs",
        "Work moves between agents explicitly,",
        "with a summary, and only when the recipient accepts.",
    )
    offered = recorder.run(
        "issue",
        "offer",
        ISSUE,
        "--to",
        "grace",
        "--summary",
        "Rounding traced to refund(); fix and tests remain.",
        cwd=ada,
    )
    recorder.caption("ada offers #41 to grace with a summary.", limit=12)
    found = OFFER_ID.search(offered)
    if found is None:
        raise RuntimeError(f"no offer identifier in {offered!r}")
    recorder.run(
        "issue", "accept", ISSUE, "--offer-id", found.group(1), cwd=grace
    )
    recorder.caption("grace accepts; only then does ownership move.", limit=12)
    recorder.tool("ada", "release_file_reservations", {"paths": [PATTERN]})
    recorder.caption("ada releases the paths; grace's queued request is next.")
    recorder.run("issue", "list", cwd=grace)
    recorder.caption("Who holds what, at a glance.")

    recorder.chapter(
        "CHAPTER 6",
        "Plan revisions",
        "A lane can propose a change to the work order.",
        "The operator approves it; the ledger never drifts silently.",
    )
    proposed = recorder.run(
        "plan",
        "propose",
        "--base",
        "1",
        "--add",
        "42:43",
        "--reason",
        "#42 reuses the ledger #43 introduces",
        cwd=ada,
    )
    recorder.caption("ada proposes a new dependency from its lane.")
    found = PROPOSAL_ID.search(proposed)
    if found is None:
        raise RuntimeError(f"no proposal identifier in {proposed!r}")
    recorder.run("plan", "proposals")
    recorder.caption("Proposals outside the [revisions] envelope wait here.")
    recorder.run("plan", "approve", found.group(1))
    recorder.caption("The operator approves it.")
    recorder.run("plan", "show")
    recorder.caption("The plan now carries the new edge.")

    recorder.chapter(
        "CHAPTER 7",
        "A run budget",
        "Cap the tokens, calls and hours a whole run may spend.",
        "Past the cap the service starts no wake, dispatch or launch.",
    )
    recorder.run("budget", "enforce", "--calls", "500", "--hours", "8")
    recorder.caption("Enforced for the project, observed by Parley itself.")

    recorder.chapter(
        "CHAPTER 8",
        "Unattended integration",
        "Named issues may merge without an operator,",
        "only after the recorded gate passes on a ready report.",
    )
    recorder.run("verify", "set", "git diff --check")
    recorder.caption("The gate every merge runs in the base checkout.")
    recorder.run("unattended", "set", ISSUE, "--target", "main")
    recorder.caption("#41 may be integrated into main unattended.")
    (grace / "src" / "payments" / "refund.py").write_text(REFUND_FIX)
    recorder.shell("git", "commit", "-am", "Refund whole cents only", cwd=grace)
    recorder.caption("grace commits the fix in its lane.")
    recorder.run(
        "report",
        "--issue",
        ISSUE,
        "--state",
        "ready",
        "--summary",
        "Refunds clamp to whole cents",
        "--evidence",
        "git diff --check: clean",
        cwd=grace,
    )
    recorder.caption("She reports ready, with evidence.")
    recorder.stop("grace")
    recorder.run("unattended", "run", "grace")
    recorder.caption("Her session has exited; the merge runs behind the gate.")
    recorder.shell("git", "log", "--oneline", "-3", cwd=recorder.repository)
    recorder.caption("main now carries grace's commit.")
    recorder.run("history", "participant", "grace")
    recorder.caption("Every claim, handoff, report and merge is on record.")

    recorder.chapter(
        "CHAPTER 9",
        "One pane for the operator",
        "Live state of every lane, and only the problems",
        "that need a human.",
    )
    recorder.run("top", "--once")
    recorder.caption("The dashboard: state, issues, mail, leases, denials.")
    recorder.run("problems")
    recorder.caption("The operator's queue of things to decide.")

    recorder.chapter(
        "GET STARTED",
        "uv tool install agent-parley",
        "agent-parley setup .   ·   agent-parley run <lane>",
        "Local-first. Standard library only. Your native CLIs, unchanged.",
        seconds=6.0,
    )


def verdict(step: Step) -> Step:
    """Reads a recorded hook decision the way the agent receives it.

    The hook writes a JSON document for the native client. A screenshot
    shows the decision and the reason the agent is told, wrapped to the
    frame, instead of the envelope around them.

    Args:
        step: Step recorded by ``Recorder.hook``.

    Returns:
        The same step with its output replaced by the decision and reason.
    """
    text = "".join(step.output).strip()
    output = (json.loads(text) if text else {}).get("hookSpecificOutput", {})
    decision = output.get("permissionDecision", "allow")
    reason = output.get("permissionDecisionReason", "")
    wrapped = textwrap.wrap(
        reason,
        COLUMNS - 12,
        initial_indent=f"{decision:<7}",
        subsequent_indent=" " * 7,
    )
    return dataclasses.replace(step, output=tuple(wrapped or [decision]))


def screenshot_scenario(recorder: Recorder) -> dict[str, list[Step]]:
    """Drives the coordination features the static screenshots show.

    Unlike ``record``, this leaves one lane genuinely idle and one branch
    genuinely drifted, so ``top``, ``status`` and the native hook each
    capture a real refusal or a real idle label instead of a scripted one.

    ``codex-1`` never receives a synthetic ``SessionStart``. That event
    always clears a lane's recorded session identity until a later hook is
    confirmed to descend from the same process; this recorder's hook calls
    are spawned by the script itself rather than by the launched process, so
    that confirmation never comes, and the lane would misread as stopped.
    Leaving the identity ``launch`` already recorded untouched keeps
    ``codex-1`` genuinely alive, so the idle reading ``status`` later
    captures for it is real rather than staged.

    Args:
        recorder: Recorder collecting the frames.

    Returns:
        Each screenshot's file name mapped to the blocks ``render_static``
        draws for it, in order.
    """
    path = "src/payments/refund.py"
    root = str(recorder.repository)
    recorder.run("up")
    recorder.run("setup", root)
    recorder.run("forge", "set", "null", "--repo", root)
    recorder.run("participant", "add", "claude-1", "--provider", "claude")
    recorder.run("participant", "add", "codex-1", "--provider", "codex")
    recorder.run("participant", "add", "claude-2", "--provider", "claude")
    manifest = next((recorder.home / "projects").glob("*/project.json"))
    lower_stall_windows(manifest.parent)
    recorder.launch("claude-1")
    recorder.launch("codex-1")
    recorder.launch("claude-2")
    recorder.session("claude-1")
    recorder.session("claude-2")
    claude_lane = recorder.lanes["claude-1"]
    codex_lane = recorder.lanes["codex-1"]
    docs_lane = recorder.lanes["claude-2"]
    recorder.run("issue", "claim", "17", cwd=codex_lane)
    recorder.run("issue", "claim", "42", cwd=claude_lane)
    recorder.run("issue", "claim", "58", cwd=docs_lane)
    recorder.prompt("claude-1", "Fix refund rounding for partial captures")
    recorder.prompt("codex-1", "Rename the shared money helper")
    recorder.prompt("claude-2", "Document the refund API for merchants")
    recorder.tool(
        "claude-2",
        "file_reservation_paths",
        {"paths": ["docs/refunds.md"], "reason": "refund API guide"},
    )
    recorder.tool(
        "claude-1",
        "file_reservation_paths",
        {"paths": [path], "exclusive": True, "reason": "refund rounding fix"},
    )
    reserved = recorder.latest()
    recorder.tool(
        "codex-1",
        "file_reservation_paths",
        {"paths": [path], "exclusive": True, "reason": "shared helper rename"},
    )
    conflicted = recorder.latest()
    recorder.run("issue", "block", "42", "--on", "17", cwd=claude_lane)
    recorder.run(
        "issue",
        "offer",
        "42",
        "--to",
        "codex-1",
        "--summary",
        "Capture path committed; rounding table left to check.",
        cwd=claude_lane,
    )
    recorder.run("issue", "list", cwd=claude_lane)
    issue_list = recorder.latest()
    recorder.tool(
        "claude-1",
        "send_message",
        {
            "to": ["codex-1"],
            "subject": "Refund path needs a second pass",
            "body_md": (
                "Rounding fixed in refund.py; capture path still needs a "
                "review."
            ),
            "idempotency_key": "screenshot-1",
            "ack_required": True,
        },
    )
    sent = recorder.latest()
    recorder.tool("codex-1", "fetch_inbox", {"limit": 5})
    fetched = recorder.latest()
    git("switch", "-c", "refund-spike", cwd=codex_lane)
    recorder.hook("codex-1", "cat src/payments/refund.py")
    drifted = recorder.latest()
    recorder.hook("claude-1", "git switch -c hotfix")
    blocked = recorder.latest()
    time.sleep(5.0)
    recorder.run(
        "top",
        "--once",
        "--columns",
        "PARTICIPANT,STATE,ISSUES,MAIL,LEASES,DENIALS,IDLE,TASK",
    )
    top = recorder.latest()
    recorder.run("status")
    status = recorder.latest()

    return {
        "screenshot-top.svg": [top],
        "screenshot-status.svg": [status],
        "screenshot-issues.svg": [issue_list],
        "screenshot-hooks.svg": [
            annotate("# a branch switch attempted inside an assigned lane"),
            verdict(blocked),
            annotate(""),
            annotate(
                "# a lane left on another branch is stopped at its next call"
            ),
            verdict(drifted),
        ],
        "screenshot-coordination.svg": [
            annotate("# the agent reserves the file it is about to change"),
            reserved,
            annotate(""),
            annotate(
                "# a second agent asks for the same path and is granted nothing"
            ),
            conflicted,
            annotate(""),
            annotate(
                "# messages are addressed by participant name and carry "
                "an acknowledgement flag"
            ),
            sent,
            annotate(""),
            annotate(
                "# inboxes page incrementally and never mark a message read"
            ),
            fetched,
        ],
    }


def rewritten(
    steps: Sequence[Step | Card], places: dict[str, str]
) -> list[Step | Card]:
    """Replaces recording paths with a stable demo shape.

    Args:
        steps: Frames as they were captured.
        places: Recording paths mapped to their published form.

    Returns:
        The same frames with every recording path replaced, longest path
        first so a nested directory is not half-rewritten.
    """
    order = sorted(places, key=len, reverse=True)

    def fix(text: str) -> str:
        """Replaces every recording path in one line.

        Args:
            text: A captured command line or output line.

        Returns:
            That line with each recording path replaced.
        """
        for place in order:
            text = text.replace(place, places[place])
        return text

    return [
        dataclasses.replace(
            step,
            command=fix(step.command),
            output=tuple(fix(x) for x in step.output),
        )
        if isinstance(step, Step)
        else step
        for step in steps
    ]


def colour(text: str) -> str:
    """Chooses the colour one output line is drawn in.

    Args:
        text: The output line.

    Returns:
        A hexadecimal colour: a refusal or collision reads as a warning, a
        header or table heading reads as a heading, everything else reads
        as body text.
    """
    if any(text.lstrip().startswith(word) for word in EMPHASIS):
        return HEADING
    lowered = text.lower()
    if any(word in lowered for word in REFUSED):
        return REFUSAL
    return BODY


def escape(text: str) -> str:
    """Escapes text for an SVG text node.

    Args:
        text: Line to escape.

    Returns:
        The line with XML metacharacters replaced.
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def frame(step: Step) -> tuple[str, ...]:
    """Clips one step to the lines a frame shows.

    Args:
        step: The captured step.

    Returns:
        Every output line when the step fits its limit, and otherwise its
        first and last lines with one marker naming how many lines between
        them the frame does not show. Nothing is rewritten: a clipped frame
        states that it is clipped.
    """
    limit = min(step.limit, FRAME_LINES)
    if len(step.output) <= limit:
        return step.output
    head = (limit - 1) * 2 // 3
    tail = limit - 1 - head
    elided = len(step.output) - head - tail
    return (
        *step.output[:head],
        f"[{elided} lines not shown]",
        *step.output[-tail:],
    )


def typing_seconds(step: Step) -> float:
    """Returns how long a step's command takes to type out.

    Args:
        step: The captured step.

    Returns:
        Seconds proportional to the command's length, capped so a long
        tool call does not stall the recording.
    """
    return min(len(step.command) * TYPE_RATE, TYPE_LIMIT)


def hold_seconds(item: "Step | Card") -> float:
    """Returns how long one item stays on screen.

    Args:
        item: A captured step or a title card.

    Returns:
        A card's own duration, or for a step the typing time plus a reading
        time that grows with the lines and the caption it shows.
    """
    if isinstance(item, Card):
        return item.seconds
    reading = 2.4 + 0.1 * len(frame(item)) + 0.03 * len(item.caption)
    return typing_seconds(item) + min(max(reading, 3.0), 7.5)


def keytimes(moments: list[float], total: float) -> str:
    """Formats absolute moments as SMIL key times over one loop.

    Args:
        moments: Seconds from the start of the loop, non-decreasing, the
            first zero and the last the loop length.
        total: Length of one loop in seconds.

    Returns:
        The ``keyTimes`` attribute value.
    """
    return ";".join(f"{min(max(x / total, 0.0), 1.0):.6f}" for x in moments)


def animate(
    attribute: str, values: list[str], moments: list[float], total: float
) -> str:
    """Builds one looping SMIL animation on the shared recording timeline.

    Every animation in the file shares one duration and repeats forever, so
    they stay in step on every loop without referencing each other. The
    timing is SMIL rather than CSS keyframes because an SVG served as an
    image animates through its own timeline in every renderer that animates
    at all, and because a headless browser can be asked what that timeline
    shows at a given second, which makes the result checkable.

    Args:
        attribute: Attribute the animation drives.
        values: One value per moment.
        moments: Seconds from the start of the loop, starting at zero and
            ending at the loop length.
        total: Length of one loop in seconds.

    Returns:
        One ``animate`` element.
    """
    return (
        f'<animate attributeName="{attribute}" values="{";".join(values)}" '
        f'keyTimes="{keytimes(moments, total)}" dur="{total}s" '
        'repeatCount="indefinite"/>'
    )


def visibility(start: float, end: float, total: float) -> str:
    """Builds the opacity animation that fades one item in and out.

    Args:
        start: Second the item appears.
        end: Second the item has gone.
        total: Length of one loop in seconds.

    Returns:
        One ``animate`` element; the first item starts visible, so a
        renderer that ignores animation still shows the opening card.
    """
    if start <= 0:
        return animate(
            "opacity", ["1", "1", "0", "0"], [0, end - FADE, end, total], total
        )
    if end >= total:
        return animate(
            "opacity",
            ["0", "0", "1", "1"],
            [0, start, start + FADE, total],
            total,
        )
    return animate(
        "opacity",
        ["0", "0", "1", "1", "0", "0"],
        [0, start, start + FADE, end - FADE, end, total],
        total,
    )


def definitions(width: int) -> str:
    """Returns the gradients shared by every card and the progress bar.

    Args:
        width: Canvas width in pixels.

    Returns:
        One ``defs`` element.
    """
    return (
        "<defs>"
        '<radialGradient id="backdrop" cx="50%" cy="38%" r="75%">'
        '<stop offset="0" stop-color="#1b2a4a"/>'
        '<stop offset="0.55" stop-color="#0d1117"/>'
        '<stop offset="1" stop-color="#010409"/></radialGradient>'
        f'<linearGradient id="accent" x1="0" x2="{width}" y1="0" y2="0" '
        'gradientUnits="userSpaceOnUse">'
        '<stop offset="0.2" stop-color="#79c0ff"/>'
        '<stop offset="0.5" stop-color="#d2a8ff"/>'
        '<stop offset="0.8" stop-color="#7ee787"/></linearGradient>'
        "</defs>"
    )


def draw_card(
    card: Card, width: int, height: int, start: float, total: float
) -> list[str]:
    """Draws one title card as a full-canvas group.

    Args:
        card: The card to draw.
        width: Canvas width in pixels.
        height: Canvas height in pixels.
        start: Second the card appears.
        total: Length of one loop in seconds.

    Returns:
        The SVG elements of the card, without the enclosing group.
    """
    middle = width / 2
    top = height / 2 - 26 - len(card.lines) * 13
    rise = animate(
        "transform",
        ["0 14", "0 14", "0 0", "0 0"],
        [0, start, start + 0.8, total],
        total,
    ).replace("<animate ", '<animateTransform type="translate" ')
    parts = [
        f'<rect width="{width}" height="{height}" rx="10" '
        'fill="url(#backdrop)"/>',
        f"<g>{rise}",
        f'<text x="{middle}" y="{top}" text-anchor="middle" '
        f'fill="{HEADING}" font-family="{SANS}" font-size="14" '
        f'letter-spacing="4" font-weight="600">{escape(card.kicker)}</text>',
        f'<text x="{middle}" y="{top + 56}" text-anchor="middle" '
        f'fill="url(#accent)" font-family="{SANS}" font-size="44" '
        f'font-weight="800">{escape(card.title)}</text>',
    ]
    for number, line in enumerate(card.lines):
        parts.append(
            f'<text x="{middle}" y="{top + 104 + number * 28}" '
            f'text-anchor="middle" fill="{BODY}" font-family="{SANS}" '
            f'font-size="18">{escape(line)}</text>'
        )
    parts.append("</g>")
    return parts


def draw_step(
    step: Step,
    chapter: str,
    width: int,
    start: float,
    total: float,
) -> list[str]:
    """Draws one terminal frame with its typed command and caption.

    The command is revealed left to right behind a cursor, and the output
    fades in once it is typed, so the frame reads as a command being run.
    Nothing in the terminal area is written by hand: the command and every
    output line are the captured ones.

    Args:
        step: The captured step.
        chapter: Chapter name drawn in the title bar.
        width: Canvas width in pixels.
        start: Second the frame appears.
        total: Length of one loop in seconds.

    Returns:
        The SVG elements of the frame, without the enclosing group.
    """
    typed = start + FADE + typing_seconds(step)
    baseline = HEADER + MARGIN + LINE
    origin = MARGIN + (len(step.prompt) + 1) * CHARACTER
    span = min(len(step.command) * CHARACTER, width - origin - MARGIN)
    marker = PROMPT if step.prompt == "$" else TOOL
    clip = f"type{round(start * 1000)}"
    parts = [
        f'<text x="{width / 2}" y="19" text-anchor="middle" fill="{MUTED}" '
        f'font-family="{SANS}" font-size="12">{escape(chapter)}</text>',
        f'<clipPath id="{clip}"><rect x="{origin}" y="{baseline - 14}" '
        f'height="{LINE}" width="0">'
        + animate(
            "width",
            ["0", "0", f"{span:.1f}", f"{span:.1f}"],
            [0, start + FADE, typed, total],
            total,
        )
        + "</rect></clipPath>",
        f'<text x="{MARGIN}" y="{baseline}" xml:space="preserve" '
        f'fill="{marker}">{escape(step.prompt)}</text>',
        f'<text x="{origin}" y="{baseline}" xml:space="preserve" '
        f'fill="{COMMAND}" clip-path="url(#{clip})">'
        f"{escape(step.command)}</text>",
        f'<rect y="{baseline - 12}" width="8" height="15" fill="{COMMAND}">'
        + animate(
            "x",
            [f"{origin:.1f}", f"{origin:.1f}", f"{origin + span:.1f}"]
            + [f"{origin + span:.1f}"],
            [0, start + FADE, typed, total],
            total,
        )
        + animate(
            "opacity",
            ["1", "1", "0", "0"],
            [0, typed + 0.2, typed + 0.2, total],
            total,
        )
        + "</rect>",
        "<g>"
        + animate(
            "opacity",
            ["0", "0", "1", "1"],
            [0, typed + 0.15, typed + 0.45, total],
            total,
        ),
    ]
    for number, row in enumerate(frame(step), start=1):
        parts.append(
            f'<text x="{MARGIN}" y="{baseline + number * LINE}" '
            f'xml:space="preserve" fill="{colour(row)}">'
            f"{escape(row)}</text>"
        )
    parts.append("</g>")
    if step.caption:
        band = HEADER + MARGIN * 2 + LINE * (FRAME_LINES + 1)
        parts.append(
            f'<rect x="{MARGIN}" y="{band + 12}" width="4" height="24" '
            f'rx="2" fill="url(#accent)"/>'
            f'<text x="{MARGIN + 16}" y="{band + 29}" fill="{COMMAND}" '
            f'font-family="{SANS}" font-size="16">'
            f"{escape(step.caption)}</text>"
        )
    return parts


def render(items: list["Step | Card"], destination: Path) -> float:
    """Writes the recording as one animated SVG.

    Each item is a group whose opacity fades in and out on one shared
    timeline. Cards fill the canvas; steps draw the terminal window, a
    typed command, the captured output and a caption band. A progress bar
    runs along the bottom edge with a tick at every chapter.

    Args:
        items: Title cards and captured steps, in order.
        destination: File to write.

    Returns:
        Length of one loop in seconds.
    """
    width = round(COLUMNS * CHARACTER + MARGIN * 2)
    band = HEADER + MARGIN * 2 + LINE * (FRAME_LINES + 1)
    height = round(band + CAPTION)
    durations = [hold_seconds(item) for item in items]
    total = round(sum(durations), 2)
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{height}" viewBox="0 0 {width} {height}" '
        'font-family="ui-monospace, SFMono-Regular, Menlo, Consolas, '
        '\'Liberation Mono\', monospace" font-size="13">',
        definitions(width),
        f'<rect width="{width}" height="{height}" rx="10" '
        f'fill="{BACKGROUND}"/>',
        f'<rect width="{width}" height="30" rx="10" fill="{BAR}"/>',
        f'<rect y="20" width="{width}" height="10" fill="{BAR}"/>',
        '<circle cx="18" cy="15" r="5.5" fill="#ff5f57"/>'
        '<circle cx="36" cy="15" r="5.5" fill="#febc2e"/>'
        '<circle cx="54" cy="15" r="5.5" fill="#28c840"/>',
        f'<rect y="{band}" width="{width}" height="{CAPTION}" fill="{BAR}"/>',
        f'<rect y="{band}" width="{width}" height="1" fill="#30363d"/>',
    ]
    start = 0.0
    chapter = ""
    ticks = []
    for item, seconds in zip(items, durations, strict=True):
        end = start + seconds
        opacity = "1" if start <= 0 else "0"
        parts.append(f'<g opacity="{opacity}">')
        parts.append(visibility(start, end, total))
        if isinstance(item, Card):
            chapter = f"{item.kicker.title()} · {item.title}"
            ticks.append(start)
            parts.extend(draw_card(item, width, height, start, total))
        else:
            parts.extend(draw_step(item, chapter, width, start, total))
        parts.append("</g>")
        start = end
    for moment in ticks[1:]:
        parts.append(
            f'<rect x="{moment / total * width:.1f}" y="{height - 4}" '
            'width="2" height="4" fill="#30363d"/>'
        )
    parts.append(
        f'<rect y="{height - 3}" height="3" width="0" fill="url(#accent)">'
        f'<animate attributeName="width" values="0;{width}" dur="{total}s" '
        'repeatCount="indefinite"/></rect>'
    )
    parts.append("</svg>")
    destination.write_text("\n".join(parts) + "\n")
    return total


def annotate(text: str) -> Step:
    """Builds a narrative line for a static screenshot.

    Args:
        text: Line drawn without a command header, either a ``#`` comment
            or a blank spacer between blocks.

    Returns:
        A step ``render_static`` draws as one body line and no header.
    """
    return Step("", "", (text,))


def render_static(steps: list[Step], destination: Path, title: str) -> None:
    """Writes a fixed set of blocks as one unanimated terminal frame.

    Unlike ``render``, every block is visible at once: this is for a
    screenshot, not a looping recording. A step with an empty prompt, as
    ``annotate`` builds, is drawn as a narrative line with no header; any
    other step draws its prompt/command header followed by its output.

    Args:
        steps: Blocks to draw, in order.
        destination: File to write.
        title: Text centered in the terminal's title bar.
    """
    widths = [len(title)]
    for step in steps:
        if step.prompt:
            widths.append(len(step.prompt) + 1 + len(step.command))
        widths.extend(len(row) for row in step.output)
    columns = min(max(widths), COLUMNS)
    width = round(columns * CHARACTER + MARGIN * 2)
    total_lines = sum(
        (1 if step.prompt else 0) + len(step.output) for step in steps
    )
    height = round(HEADER + MARGIN * 2 + LINE * total_lines)
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{height}" viewBox="0 0 {width} {height}" '
        'font-family="ui-monospace, SFMono-Regular, Menlo, Consolas, '
        '\'Liberation Mono\', monospace" font-size="13">',
    ]
    parts.append(
        f'<rect width="{width}" height="{height}" rx="10" fill="{BACKGROUND}"/>'
    )
    parts.append(f'<rect width="{width}" height="30" rx="10" fill="{BAR}"/>')
    parts.append(f'<rect y="20" width="{width}" height="10" fill="{BAR}"/>')
    parts.append(
        '<circle cx="18" cy="15" r="5.5" fill="#ff5f57"/>'
        '<circle cx="36" cy="15" r="5.5" fill="#febc2e"/>'
        '<circle cx="54" cy="15" r="5.5" fill="#28c840"/>'
    )
    parts.append(
        f'<text x="{width / 2}" y="19" text-anchor="middle" fill="{MUTED}" '
        f'font-size="12">{escape(title[:columns])}</text>'
    )
    baseline = HEADER + MARGIN
    for step in steps:
        if step.prompt:
            marker = PROMPT if step.prompt == "$" else TOOL
            parts.append(
                f'<text x="{MARGIN}" y="{baseline}" xml:space="preserve">'
                f'<tspan fill="{marker}">{escape(step.prompt)} </tspan>'
                f'<tspan fill="{COMMAND}">{escape(step.command)}</tspan>'
                "</text>"
            )
            baseline += LINE
        for row in step.output:
            parts.append(
                f'<text x="{MARGIN}" y="{baseline}" xml:space="preserve" '
                f'fill="{colour(row)}">{escape(row)}</text>'
            )
            baseline += LINE
    parts.append("</svg>")
    destination.write_text("\n".join(parts) + "\n")


def screenshots(destination: Path) -> int:
    """Records the static screenshots and writes each asset.

    Args:
        destination: Directory the screenshot assets are written to.

    Returns:
        Zero when every screenshot was written.
    """
    from agent_parley import server

    titles = {
        "screenshot-top.svg": "agent-parley top",
        "screenshot-status.svg": "agent-parley status",
        "screenshot-issues.svg": "agent-parley issue list",
        "screenshot-hooks.svg": "native hooks · enforcement and delivery",
        "screenshot-coordination.svg": f"{len(server.TOOLS)} scoped MCP tools",
    }
    with tempfile.TemporaryDirectory(
        prefix="agent-parley-screenshots-"
    ) as path:
        base = Path(path)
        home, binaries, repository = fixtures(base)
        recorder = Recorder(home, repository, environment(home, binaries, base))
        try:
            blocks = screenshot_scenario(recorder)
        finally:
            recorder.close()
        places = {
            str(home): DEMO_STATE,
            str(repository): DEMO_REPOSITORY,
            str(base): DEMO_HOME,
            str(Path.home()): DEMO_HOME,
        }
        for name, steps in blocks.items():
            rewrite = [
                step
                for step in rewritten(steps, places)
                if isinstance(step, Step)
            ]
            render_static(rewrite, destination / name, titles[name])
            print(f"wrote {destination / name}")
    return 0


def main() -> int:
    """Records the demo and writes the asset.

    Run with ``--screenshots`` to record the static screenshots instead.

    Returns:
        Zero when the recording and the asset were written.
    """
    destination = Path(__file__).resolve().parent.parent / "docs" / "assets"
    if "--screenshots" in sys.argv[1:]:
        return screenshots(destination)
    with tempfile.TemporaryDirectory(prefix="agent-parley-demo-") as path:
        base = Path(path)
        home, binaries, repository = fixtures(base)
        recorder = Recorder(home, repository, environment(home, binaries, base))
        try:
            record(recorder)
        finally:
            recorder.close()
        places = {
            str(home): DEMO_STATE,
            str(repository): DEMO_REPOSITORY,
            str(base): DEMO_HOME,
            str(Path.home()): DEMO_HOME,
        }
        steps = rewritten(recorder.steps, places)
    total = render(steps, destination / "demo.svg")
    print(
        f"{len(steps)} frames, {total:.0f}s, written to "
        f"{destination / 'demo.svg'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
