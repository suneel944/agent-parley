"""Records the coordination demo and renders it as an animated SVG.

The recording is produced by running Agent Parley, never by writing terminal
text. Every frame holds one real command line and the bytes that command
wrote to a pseudo-terminal of fixed size, so the wrapping, the table widths
and the refusal text are the ones an operator sees.

What the run exercises, in order: a coordination server, a registered
repository, two lanes on different providers, a lane launch each, a claim, an
advisory path reservation, the collision a second lane meets on the same
pattern, the queued request it files instead, a handoff offer and its
acceptance, the dashboard, and a native hook refusing a branch switch.

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

Regenerate with ``make demo``, or::

    uv run --locked python scripts/record_demo.py

The result is written to ``docs/assets/demo.svg`` and committed. It is
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
import socket
import struct
import subprocess
import sys
import tempfile
import termios
import time
from pathlib import Path

COLUMNS = 132
ROWS = 30
FRAME_LINES = 24
SECONDS_PER_FRAME = 4.5
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
)


@dataclasses.dataclass(frozen=True)
class Step:
    """One recorded frame.

    Attributes:
        prompt: The leading marker drawn before the command, ``$`` for a
            shell command and the lane name for a tool call.
        command: The command line or tool call as it was issued.
        output: The lines the step wrote, already stripped of escapes.
    """

    prompt: str
    command: str
    output: tuple[str, ...]


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
    git("add", "--all", cwd=repository)
    git(
        "-c",
        "user.name=dev",
        "-c",
        "user.email=dev@example.com",
        "commit",
        "-qm",
        "Add the refund path",
        cwd=repository,
    )
    return home, binaries, repository


def environment(home: Path, binaries: Path, base: Path) -> dict[str, str]:
    """Builds the environment every recorded command runs under.

    Args:
        home: Coordination state directory for this run.
        binaries: Directory holding the stub native CLIs.
        base: Directory the native configuration homes are created under.

    Returns:
        A copy of the process environment pinned to the temporary state,
        the stub executables and a free loopback port.
    """
    values = dict(os.environ)
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
    rather than faking the label. ``inactive_after`` is left at its
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
        self.steps: list[Step] = []
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
            Step("$", " ".join(("agent-parley", *arguments)), lines(output))
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
        self.steps.append(Step("$", " ".join(arguments), lines(output)))
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

    Raises:
        RuntimeError: If the handoff offer carried no identifier.
    """
    root = str(recorder.repository)
    recorder.run("up")
    recorder.run("setup", root)
    recorder.run("forge", "set", "null", "--repo", root)
    recorder.run("participant", "add", "ada", "--provider", "claude")
    recorder.run("participant", "add", "grace", "--provider", "codex")
    recorder.launch("ada")
    recorder.launch("grace")
    recorder.session("ada")
    recorder.session("grace")
    ada = recorder.lanes["ada"]
    grace = recorder.lanes["grace"]
    recorder.run("status")
    recorder.run("issue", "claim", ISSUE, cwd=ada)
    recorder.tool("ada", "file_reservation_paths", {"paths": [PATTERN]})
    recorder.tool("grace", "file_reservation_paths", {"paths": [PATTERN]})
    recorder.tool("grace", "request_reservation", {"paths": [PATTERN]})
    offered = recorder.run(
        "issue",
        "offer",
        ISSUE,
        "--to",
        "grace",
        "--summary",
        "Refund path reserved; tests green.",
        cwd=ada,
    )
    found = OFFER_ID.search(offered)
    if found is None:
        raise RuntimeError(f"no offer identifier in {offered!r}")
    recorder.run(
        "issue", "accept", ISSUE, "--offer-id", found.group(1), cwd=grace
    )
    recorder.run("issue", "list", cwd=grace)
    recorder.run("top", "--once")
    recorder.hook("ada", "git checkout -b hotfix/refund")


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
    recorder.run("participant", "add", "kimi-1", "--provider", "kimi")
    recorder.launch("claude-1")
    recorder.launch("codex-1")
    recorder.session("claude-1")
    claude_lane = recorder.lanes["claude-1"]
    codex_lane = recorder.lanes["codex-1"]
    lower_stall_windows(claude_lane.parent)
    recorder.env["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:0"
    recorder.env["ANTHROPIC_AUTH_TOKEN"] = "screenshot-fixture"
    recorder.launch("kimi-1")
    recorder.session("kimi-1")
    dormant_process = recorder.sessions[-1]
    dormant_process.terminate()
    dormant_process.wait(timeout=10.0)
    recorder.run("issue", "claim", "17", cwd=codex_lane)
    recorder.run("issue", "claim", "42", cwd=claude_lane)
    recorder.tool(
        "claude-1",
        "file_reservation_paths",
        {"paths": [path], "exclusive": True, "reason": "refund rounding fix"},
    )
    reserved = recorder.steps[-1]
    recorder.tool(
        "codex-1",
        "file_reservation_paths",
        {"paths": [path], "exclusive": True, "reason": "shared helper rename"},
    )
    conflicted = recorder.steps[-1]
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
    issue_list = recorder.steps[-1]
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
    sent = recorder.steps[-1]
    recorder.tool("codex-1", "fetch_inbox", {"limit": 5})
    fetched = recorder.steps[-1]
    git("switch", "-c", "refund-spike", cwd=codex_lane)
    recorder.hook("codex-1", "cat src/payments/refund.py")
    drifted = recorder.steps[-1]
    recorder.hook("claude-1", "git switch -c hotfix")
    blocked = recorder.steps[-1]
    time.sleep(3.0)
    recorder.run("top", "--once")
    top = recorder.steps[-1]
    recorder.run("status")
    status = recorder.steps[-1]

    return {
        "screenshot-top.svg": [top],
        "screenshot-status.svg": [status],
        "screenshot-issues.svg": [issue_list],
        "screenshot-hooks.svg": [
            annotate("# a branch switch attempted inside an assigned lane"),
            annotate(""),
            blocked,
            annotate(""),
            annotate(
                "# the same lane after any bypass leaves it on another branch"
            ),
            annotate(""),
            drifted,
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


def rewritten(steps: list[Step], places: dict[str, str]) -> list[Step]:
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
        Step(step.prompt, fix(step.command), tuple(fix(x) for x in step.output))
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
    lowered = text.lower()
    if any(word in lowered for word in REFUSED):
        return REFUSAL
    if any(text.lstrip().startswith(word) for word in EMPHASIS):
        return HEADING
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
        Every output line when the step fits, and otherwise its first and
        last lines with one marker naming how many lines between them the
        frame does not show. Nothing is rewritten: a clipped frame states
        that it is clipped.
    """
    if len(step.output) <= FRAME_LINES:
        return step.output
    head = (FRAME_LINES - 1) * 2 // 3
    tail = FRAME_LINES - 1 - head
    elided = len(step.output) - head - tail
    return (
        *step.output[:head],
        f"[{elided} lines not shown]",
        *step.output[-tail:],
    )


def schedule(index: int, count: int, total: float) -> str:
    """Builds the animation that shows one frame in its turn.

    The timing is SMIL rather than CSS keyframes because an SVG served as
    an image animates through its own timeline in every renderer that
    animates at all, and because a headless browser can be asked what that
    timeline shows at a given second, which makes the result checkable.

    Args:
        index: Position of the frame in the recording.
        count: Number of frames in the recording.
        total: Length of one loop in seconds.

    Returns:
        One ``animate`` element holding the frame's opacity at zero until
        its turn, at one for its turn, and at zero afterwards.
    """
    start = round(index / count, 6)
    end = round((index + 1) / count, 6)
    values = "1;0" if index == 0 else "0;1;0"
    times = f"0;{end}" if index == 0 else f"0;{start};{end}"
    return (
        '<animate attributeName="opacity" calcMode="discrete" '
        f'values="{values}" keyTimes="{times}" dur="{total}s" '
        'repeatCount="indefinite"/>'
    )


def render(steps: list[Step], destination: Path) -> None:
    """Writes the frames as one animated SVG.

    Args:
        steps: Frames to draw, in order.
        destination: File to write.
    """
    widest = max(
        max(
            [len(step.prompt) + 1 + len(step.command)]
            + [len(row) for row in frame(step)]
        )
        for step in steps
    )
    columns = min(widest, COLUMNS)
    width = round(columns * CHARACTER + MARGIN * 2)
    rows = max(len(frame(step)) for step in steps)
    height = round(HEADER + MARGIN * 2 + LINE * (rows + 1))
    total = round(len(steps) * SECONDS_PER_FRAME, 2)
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
    for index, step in enumerate(steps):
        opacity = "1" if index == 0 else "0"
        parts.append(f'<g opacity="{opacity}">')
        parts.append(schedule(index, len(steps), total))
        title = escape(f"{step.prompt} {step.command}"[:columns])
        parts.append(
            f'<text x="{width / 2}" y="19" text-anchor="middle" '
            f'fill="{MUTED}" font-size="12">{title}</text>'
        )
        marker = PROMPT if step.prompt == "$" else TOOL
        baseline = HEADER + MARGIN + LINE
        parts.append(
            f'<text x="{MARGIN}" y="{baseline}" xml:space="preserve">'
            f'<tspan fill="{marker}">{escape(step.prompt)} </tspan>'
            f'<tspan fill="{COMMAND}">{escape(step.command)}</tspan></text>'
        )
        for number, row in enumerate(frame(step), start=1):
            parts.append(
                f'<text x="{MARGIN}" y="{baseline + number * LINE}" '
                f'xml:space="preserve" fill="{colour(row)}">'
                f"{escape(row)}</text>"
            )
        parts.append("</g>")
    parts.append("</svg>")
    destination.write_text("\n".join(parts) + "\n")


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
            rewrite = rewritten(steps, places)
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
    render(steps, destination / "demo.svg")
    print(f"{len(steps)} frames written to {destination / 'demo.svg'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
