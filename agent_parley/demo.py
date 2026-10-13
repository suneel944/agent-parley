"""Throwaway sandbox that runs the coordination story with stub lanes.

``agent-parley demo`` shows lanes claiming, colliding, being refused, handing
off and appearing in ``top`` without a native CLI, a model, an account or a
network. It builds everything under one new temporary directory: a Git
repository, a private state home, a coordination service on a free loopback
port and stub ``claude`` and ``codex`` executables that run
``exec sleep 900``. The lane processes, the service, the MCP tool dispatch
and the native hook are all real; only the model session is a stand-in. The
user's state home, repositories and any running service are never read or
written, because every child runs with ``AGENT_PARLEY_HOME`` and
``AGENT_PARLEY_PORT`` pinned to the sandbox and with no inherited lane
credential.

The same harness, `Recorder`, drives ``scripts/record_demo.py``
(``make demo-stub``), which renders the full story from
`agent_parley.demo_scenario` as the published recording.

Every child is started in a session of its own, so a Ctrl-C at the terminal
reaches only the demo and never interrupts a command half way through. The
demo then waits for the command in flight, stops the service and the lanes,
removes the sandbox and, where ``/proc`` is available, ends any process still
working inside it. Cleanup runs with interrupts ignored, on success, on
``q``, on Ctrl-C and on an exception alike.

The sandbox is created under ``/tmp`` when that directory exists, because the
per-user temporary directory on macOS is long enough to push a lane's control
socket past the Unix socket path limit.
"""

from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import json
import os
import pty
import re
import select
import shlex
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import termios
import textwrap
import threading
import time
import tty
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TextIO

from agent_parley import demo_scenario

COLUMNS = 132
ROWS = 30
FRAME_LINES = 24
DEFAULT_PORT = 8876
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[=>]|\x1b\][^\x07]*\x07")
INHERITED = ("AGENT_PARLEY_", "CLAUDE", "CODEX_")
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
RESET = "\x1b[0m"
Popen = subprocess.Popen[bytes]


@dataclasses.dataclass(frozen=True)
class Step:
    """One recorded step.

    Attributes:
        prompt: The leading marker drawn before the command, ``$`` for a
            shell command and the lane name for a tool call.
        command: The command line or tool call as it was issued.
        output: The lines the step wrote, already stripped of escapes.
        caption: Narration of what the step did, never part of the output.
        limit: Most output lines a rendered frame shows before it states
            that the rest is not shown.
    """

    prompt: str
    command: str
    output: tuple[str, ...]
    caption: str = ""
    limit: int = FRAME_LINES


@dataclasses.dataclass(frozen=True)
class Card:
    """One chapter title between the steps of the story.

    Attributes:
        kicker: Small line above the title, such as the chapter number.
        title: The chapter or recording title.
        lines: Lines drawn under the title.
        seconds: How long a recording keeps the card on screen.
    """

    kicker: str
    title: str
    lines: tuple[str, ...] = ()
    seconds: float = 3.6


def free_port(preferred: int = DEFAULT_PORT) -> int:
    """Finds a loopback port the sandbox service can bind.

    Args:
        preferred: Port to use when nothing holds it, or 0 for any.

    Returns:
        The preferred port when it is free, and otherwise any free port.

    Raises:
        RuntimeError: If no loopback port could be bound.
    """
    for candidate in dict.fromkeys((preferred, 0)):
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", candidate))
            except OSError:
                continue
            return int(probe.getsockname()[1])
    raise RuntimeError("no loopback port was available")


def git(*arguments: str, cwd: Path | None = None) -> None:
    """Runs one Git command for the sandbox fixtures.

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
    """Builds the temporary state home, stub CLIs and demo repository.

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
    (base / "work-order.toml").write_text(demo_scenario.WORK_ORDER)
    return home, binaries, repository


def environment(
    home: Path, binaries: Path, base: Path, port: int | None = None
) -> dict[str, str]:
    """Builds the environment every sandbox command runs under.

    Args:
        home: Coordination state directory for this run.
        binaries: Directory holding the stub native CLIs.
        base: Directory the native configuration homes are created under.
        port: Loopback port for the sandbox service, or None for the
            documented default when it is free.

    Returns:
        A copy of the process environment pinned to the temporary state,
        the stub executables and a free loopback port. Variables a native
        client or an enclosing lane set are dropped, so a run started from
        inside a lane has operator authority in the sandbox and carries none
        of that lane's credentials.
    """
    values = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(INHERITED)
    }
    values["PATH"] = f"{binaries}{os.pathsep}{values.get('PATH', '')}"
    values["AGENT_PARLEY_HOME"] = str(home)
    values["AGENT_PARLEY_PORT"] = str(free_port() if port is None else port)
    values["CLAUDE_CONFIG_DIR"] = str(base / "native" / "claude")
    values["CODEX_HOME"] = str(base / "native" / "codex")
    values["COLUMNS"] = str(COLUMNS)
    values["LINES"] = str(ROWS)
    values["NO_COLOR"] = "1"
    return values


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


def lines(text: str) -> tuple[str, ...]:
    """Splits captured output into frame lines without trailing blanks.

    Args:
        text: Captured output.

    Returns:
        The lines of that output, right-stripped and without trailing
        blank lines. A line wider than the frame wraps at the last space
        that fits, so a frame never splits a word; a word wider than the
        frame breaks at the frame edge. The words, their order and the
        indentation that starts a line are kept.
    """
    rows: list[str] = []
    for row in text.split("\n"):
        stripped = row.rstrip()
        if len(stripped) <= COLUMNS:
            rows.append(stripped)
            continue
        rows.extend(
            textwrap.wrap(
                stripped,
                COLUMNS,
                expand_tabs=False,
                replace_whitespace=False,
                break_on_hyphens=False,
            )
        )
    while rows and not rows[-1]:
        rows.pop()
    return tuple(rows)


class Recorder:
    """Drives stub lanes through the story and collects one step per call.

    Attributes:
        home: Coordination state directory for this run.
        repository: Demo repository root.
        env: Environment every sandbox command runs under.
        narrate: Called with each captioned step and each chapter card as
            it is added, or None to collect silently.
        steps: Steps and cards captured so far, in order.
        lanes: Lane worktrees keyed by participant name.
        sessions: Launched lane processes, kept alive for the whole run.
        attached: Controlling terminals of those launches; closing one
            hangs up its lane, which would read as a stopped session in the
            dashboard, so they stay open until the run ends.
        pending: The command in flight, which `close` lets finish so an
            interrupted run never leaves a half-started service behind.
    """

    def __init__(
        self,
        home: Path,
        repository: Path,
        env: dict[str, str],
        narrate: Callable[[Step | Card], None] | None = None,
    ) -> None:
        """Stores the run's directories and prepares empty step state.

        Args:
            home: Coordination state directory for this run.
            repository: Demo repository root.
            env: Environment every sandbox command runs under.
            narrate: Called with each captioned step and each card.
        """
        self.home = home
        self.repository = repository
        self.env = env
        self.narrate = narrate
        self.steps: list[Step | Card] = []
        self.lanes: dict[str, Path] = {}
        self.sessions: list[Popen] = []
        self.attached: list[int] = []
        self.pending: Popen | None = None

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

    def attach(self, arguments: list[str], cwd: Path) -> tuple[int, Popen]:
        """Starts one child on a fresh pseudo-terminal in its own session.

        Args:
            arguments: Program and arguments.
            cwd: Directory to run in.

        Returns:
            The controlling descriptor and the started process.
        """
        controller, child = terminal()
        try:
            process = subprocess.Popen(
                arguments,
                cwd=cwd,
                env=self.env,
                stdin=child,
                stdout=child,
                stderr=child,
                start_new_session=True,
            )
        except BaseException:
            os.close(controller)
            raise
        finally:
            os.close(child)
        return controller, process

    def capture(self, arguments: list[str], cwd: Path, timeout: float) -> str:
        """Runs one child to completion and returns what it wrote.

        Args:
            arguments: Program and arguments.
            cwd: Directory to run in.
            timeout: Seconds to wait for the child to finish.

        Returns:
            The captured output.
        """
        controller, process = self.attach(arguments, cwd)
        self.pending = process
        try:
            output = drain(controller, time.monotonic() + timeout)
        finally:
            os.close(controller)
        process.wait(timeout=timeout)
        self.pending = None
        return output

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
        output = self.capture(
            self.parley(*arguments), cwd or self.repository, timeout
        )
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
        output = self.capture(list(arguments), cwd, 30.0)
        self.steps.append(Step("$", shlex.join(arguments), lines(output)))
        return output

    def launch(self, name: str) -> None:
        """Records one lane launch and keeps that lane's session alive.

        Args:
            name: Participant to launch.

        The worktree is read from the line naming the lane, which follows
        any notice the launch prints first.

        Raises:
            RuntimeError: If the launch printed no worktree path.
        """
        controller, process = self.attach(
            self.parley("run", name, "--repo", str(self.repository)),
            self.repository,
        )
        self.sessions.append(process)
        self.attached.append(controller)
        output = drain(controller, time.monotonic() + 8.0)
        captured = lines(output)
        named = [
            row
            for row in output.split("\n")
            if row.startswith(f"{name} (") and ": " in row
        ]
        if not named:
            raise RuntimeError(f"run {name} printed no worktree: {output!r}")
        self.lanes[name] = Path(named[0].split(": ", 1)[1].strip())
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
            start_new_session=True,
        )
        return clean(done.stdout + done.stderr)

    def session(self, name: str) -> None:
        """Serves the session-start event a launched client raises.

        The stub native CLI raises no events of its own, so the harness
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
        """Adds a chapter card before the steps that follow.

        Args:
            kicker: Small line above the title.
            title: Chapter title.
            *text: Lines drawn under the title.
            seconds: How long a recording keeps the card on screen.
        """
        card = Card(kicker, title, text, seconds)
        self.steps.append(card)
        if self.narrate is not None:
            self.narrate(card)

    def latest(self) -> Step:
        """Returns the step just recorded.

        Returns:
            The last step recorded.

        Raises:
            RuntimeError: If the last entry is a chapter card.
        """
        step = self.steps[-1]
        if not isinstance(step, Step):
            raise RuntimeError("The last frame is a title card, not a step.")
        return step

    def caption(
        self, text: str, limit: int = FRAME_LINES, *, shown: bool = False
    ) -> None:
        """Narrates the step just recorded.

        Args:
            text: What the step did.
            limit: Most output lines that step's rendered frame shows.
            shown: Whether the live demo prints the step's output as well
                as its one-line narration.
        """
        step = dataclasses.replace(self.latest(), caption=text, limit=limit)
        self.steps[-1] = step
        if self.narrate is not None:
            self.narrate(
                step if shown else dataclasses.replace(step, output=())
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
        """Stops the command in flight, the service and the lane sessions.

        The service is stopped before the lanes and once more after them,
        so a supervision poll that sees a lane end cannot leave a resumed
        lane or a restarted service behind.
        """
        if self.pending is not None:
            finish(self.pending, 30.0)
            self.pending = None
        self.down()
        for process in self.sessions:
            with contextlib.suppress(OSError):
                process.terminate()
        for process in self.sessions:
            finish(process, 10.0)
        for descriptor in self.attached:
            with contextlib.suppress(OSError):
                os.close(descriptor)
        self.attached.clear()
        self.down()

    def down(self) -> None:
        """Stops the sandbox service, whether or not it is running."""
        with contextlib.suppress(subprocess.SubprocessError, OSError):
            subprocess.run(
                self.parley("down"),
                cwd=self.repository,
                env=self.env,
                capture_output=True,
                check=False,
                timeout=60,
                start_new_session=True,
            )


def finish(process: Popen, timeout: float) -> None:
    """Waits for a child to exit, killing it once the timeout passes.

    Args:
        process: Child to reap.
        timeout: Seconds to wait before killing it.
    """
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            process.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=5)


def leftovers(base: Path) -> list[int]:
    """Lists processes still running inside a sandbox directory.

    A process counts when its working directory lies under the sandbox or
    its command line names the sandbox, which covers the service, the lane
    launchers and the stub sessions. Without ``/proc`` nothing is listed.

    Args:
        base: Sandbox directory.

    Returns:
        Identifiers of those processes, excluding this one.
    """
    marker = str(base)
    found: list[int] = []
    with contextlib.suppress(OSError):
        entries = [entry for entry in os.listdir("/proc") if entry.isdigit()]
        for entry in entries:
            if int(entry) == os.getpid():
                continue
            with contextlib.suppress(OSError):
                command = Path(f"/proc/{entry}/cmdline").read_bytes()
                if not command:
                    continue
                named = marker in command.decode("utf-8", "replace")
                cwd = ""
                with contextlib.suppress(OSError):
                    cwd = os.readlink(f"/proc/{entry}/cwd")
                if named or cwd == marker or cwd.startswith(marker + "/"):
                    found.append(int(entry))
    return found


def sweep(base: Path) -> None:
    """Ends every process still running inside a sandbox directory.

    Args:
        base: Sandbox directory.
    """
    for number in (signal.SIGTERM, signal.SIGKILL):
        remaining = leftovers(base)
        for pid in remaining:
            with contextlib.suppress(OSError):
                os.kill(pid, number)
        deadline = time.monotonic() + 5.0
        while remaining and time.monotonic() < deadline:
            time.sleep(0.1)
            remaining = leftovers(base)
        if not remaining:
            return


@contextlib.contextmanager
def quitting(stream: TextIO) -> Iterator[None]:
    """Turns a ``q`` typed at the terminal into an interrupt.

    Standard input is put in cbreak mode for the duration and restored on
    exit. Nothing is read when standard input is not a terminal.

    Args:
        stream: Standard input.

    Yields:
        None while ``q`` is being listened for.
    """
    try:
        descriptor = stream.fileno()
        interactive = os.isatty(descriptor)
    except (OSError, ValueError):
        interactive = False
    if not interactive:
        yield
        return
    saved = termios.tcgetattr(descriptor)
    done = threading.Event()

    def listen() -> None:
        """Interrupts the demo when ``q`` arrives on the terminal."""
        while not done.is_set():
            if not select.select([descriptor], [], [], 0.2)[0]:
                continue
            key = os.read(descriptor, 1)
            if key in (b"q", b"Q"):
                os.kill(os.getpid(), signal.SIGINT)
                return
            if not key:
                return

    tty.setcbreak(descriptor)
    listener = threading.Thread(target=listen, daemon=True)
    listener.start()
    try:
        yield
    finally:
        done.set()
        listener.join(timeout=1.0)
        termios.tcsetattr(descriptor, termios.TCSADRAIN, saved)


def narrator(stream: TextIO) -> Callable[[Step | Card], None]:
    """Builds the printer for one line per step.

    Args:
        stream: Where the story is printed.

    Returns:
        A callable printing a card as a heading and a step as what happened
        followed by the real command or tool call behind it. Styling is
        added only when the stream is a terminal.
    """
    styled = stream.isatty()

    def style(text: str, code: str) -> str:
        """Wraps text in one escape when the stream is a terminal."""
        return f"{code}{text}{RESET}" if styled else text

    def say(item: Step | Card) -> None:
        """Prints one card or step."""
        if isinstance(item, Card):
            print(f"\n{style(item.title, BOLD)}", file=stream, flush=True)
            return
        issued = item.command
        if item.prompt != "$":
            issued = f"{item.prompt}: {issued}"
        print(
            f"  {item.caption}  {style('$ ' + issued, DIM)}",
            file=stream,
            flush=True,
        )
        for row in item.output:
            print(f"    {row}", file=stream, flush=True)

    return say


def sandbox_directory() -> Path:
    """Creates the private sandbox directory.

    Returns:
        A new directory readable only by this user.
    """
    parent = "/tmp" if os.path.isdir("/tmp") else None
    return Path(tempfile.mkdtemp(prefix="agent-parley-demo-", dir=parent))


def main(
    stream: TextIO | None = None,
    keys: TextIO | None = None,
    story: Callable[[Recorder], None] = demo_scenario.tour,
) -> int:
    """Runs the story in a sandbox and removes everything it created.

    Args:
        stream: Where the story is printed, standard output by default.
        keys: Terminal ``q`` is read from, standard input by default.
        story: Chapters to run.

    Returns:
        Zero when the story finished, 130 when it was interrupted.
    """
    out = sys.stdout if stream is None else stream
    base = sandbox_directory().resolve()
    recorder: Recorder | None = None
    status = 0
    started = time.monotonic()
    try:
        print(
            "Agent Parley demo: stub lanes on the real coordination path, "
            "in a throwaway sandbox. No native CLI, model or network is "
            "used. Press q or Ctrl-C to stop.",
            file=out,
            flush=True,
        )
        with quitting(sys.stdin if keys is None else keys):
            home, binaries, repository = fixtures(base)
            recorder = Recorder(
                home,
                repository,
                environment(home, binaries, base, port=free_port(0)),
                narrator(out),
            )
            story(recorder)
    except KeyboardInterrupt:
        status = 130
        print("\nStopped.", file=out, flush=True)
    finally:
        previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            if recorder is not None:
                recorder.close()
            sweep(base)
            shutil.rmtree(base, ignore_errors=True)
        finally:
            signal.signal(signal.SIGINT, previous)
    if status == 0:
        print(
            f"\nDone in {time.monotonic() - started:.0f}s; the sandbox is "
            "removed. Try it on your repository: agent-parley setup . && "
            "agent-parley run <lane>",
            file=out,
            flush=True,
        )
    else:
        print("The sandbox is removed.", file=out, flush=True)
    return status
