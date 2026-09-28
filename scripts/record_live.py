"""Records real ``claude`` and ``codex`` lanes and renders an animated SVG.

``scripts/record_demo.py`` proves the coordination path with stub clients.
This module records the same story with the real native clients: two model
sessions working one small issue on a throwaway repository while
``agent-parley top`` redraws beside them. No terminal text is written by
hand. Every frame is a ``tmux capture-pane`` reading of three panes, taken
while the lanes run.

What the recording shows, in order: ``agent-parley run`` starting each
lane, a claim, an advisory reservation, the collision the second lane meets
on the same path, the queued reservation request it files instead, a
refused branch switch, the handoff offer and its acceptance, and ``top``
updating throughout. The models are told those steps in a short brief the
throwaway repository carries under ``lanes/``; what they do with it is not
edited.

The run is driven from outside the clients. Commands are typed into the
panes one character at a time with ``tmux send-keys``, so the typing in the
recording is real typing. The lanes are launched one at a time, and the
second only after the first holds its claim and reservation, so the
collision is the one the brief describes.

Timing is kept except for silence. A stretch in which no pane changes is
shortened to ``HOLD`` seconds, and the title bar carries the real elapsed
time of every frame, so a viewer can see where time was cut.

Nothing from the recording machine survives into the asset. The throwaway
repository, the coordination state, the operator's home, user name, host
name and Git identity are rewritten to a ``/home/dev`` shape, and every
``--redact`` value to ``dev``. The render refuses to write while any of
those values, an email address outside ``example.com``, or a token shape is
still present.

Requirements: ``tmux``, the ``claude`` and ``codex`` clients signed in on
the operator's default accounts, and model quota for a few minutes of two
sessions. The run uses its own coordination home and loopback port, never
the operator's.

Both clients open a new directory with a trust screen no launcher can
answer. ``--trust`` records the throwaway directories as trusted in each
client's own trust record, as ``scripts/acceptance.py`` does; without it
the operator answers each screen in the recording. Run it with::

    uv run --locked python scripts/record_live.py --trust

The SVG is written to ``docs/assets/demo.svg`` and the raw frames to
``frames.jsonl`` under the kept workspace, which is printed at the end and
never committed because it holds unredacted text.
"""

from __future__ import annotations

import argparse
import dataclasses
import getpass
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from scripts import acceptance
from scripts.record_demo import (
    BACKGROUND,
    BAR,
    CHARACTER,
    DEMO_HOME,
    DEMO_REPOSITORY,
    DEMO_STATE,
    HEADER,
    LINE,
    MARGIN,
    MUTED,
    colour,
    escape,
    free_port,
)

WIDTH = 160
HEIGHT = 46
BOTTOM = 14
ISSUE = "41"
SOCKET = "parley-demo"
SAMPLE = 0.5
TYPING = 0.05
HOLD = 2.0
TIMEOUT = 1200.0
SETTLE = 20.0
LANES = ("claude:claude", "codex:codex")
DESTINATION = Path(__file__).resolve().parents[1] / "docs/assets/demo.svg"
EMAIL = re.compile(r"[\w.+-]+@(?!example\.com\b)[\w-]+(?:\.[\w-]+)+")
TOKEN = re.compile(r"\b(?:sk|ghp|gho|xox[bp])[-_][A-Za-z0-9_-]{12,}")
BRIEF_HEAD = """# {name}'s part in the demo

Keep every reply to one short sentence. Run one command per shell call.
`agent-parley` below is the full command the bridge gave you.

"""
BRIEFS = {
    "claude": """1. Claim the issue: `agent-parley issue claim {issue}`.
2. Reserve `src/payments/**` with the `file_reservation_paths` tool, reason
   "refund rounding".
3. Make the change `tasks/{issue}.md` asks for, run `python -m pytest -q`
   and commit.
4. When codex asks for the refund path, release your reservation and hand
   the issue over: `agent-parley issue offer {issue} --to codex --summary
   "rounding landed; tests pass; codex adds the zero-decimal test"`.
5. Stop.
""",
    "codex": """1. Try to claim the issue: `agent-parley issue claim {issue}`.
   claude owns it, so expect a refusal.
2. Reserve `src/payments/refund.py` with the `file_reservation_paths`
   tool. Expect a conflict naming claude.
3. Queue for it with the `request_reservation` tool, then send claude a
   message asking for the refund path.
4. Run `git switch main`. Expect the bridge to refuse it.
5. Stop and wait. When the bridge wakes you with claude's offer, accept it
   with `agent-parley issue accept {issue} --offer-id <id>`, add a test for
   a zero-decimal currency to `tests/test_refund.py`, run `python -m pytest
   -q`, commit, and run `agent-parley report --issue {issue} --state ready
   --summary "..." --evidence "..."`.
""",
}
TASK = """# Task {issue}: round refunds to the currency's minor unit

`refund(amount, decimals)` in `src/payments/refund.py` returns the amount
unchanged. Make it round half up to `decimals` places, and cover it in
`tests/test_refund.py`.
"""
MODULE = '''"""Refund path the lanes negotiate over."""


def refund(amount: float, decimals: int) -> float:
    """Returns the amount to refund."""
    return amount
'''


@dataclasses.dataclass(frozen=True)
class Pane:
    """One tmux pane's place in the recorded window.

    Attributes:
        name: Label drawn above the pane.
        target: tmux pane identifier.
        left: Column of the pane's first cell.
        top: Row of the pane's first cell.
        width: Pane width in cells.
        height: Pane height in cells.
    """

    name: str
    target: str
    left: int
    top: int
    width: int
    height: int


@dataclasses.dataclass(frozen=True)
class Frame:
    """One reading of every pane.

    Attributes:
        at: Seconds since the recording started.
        screens: Each pane's visible lines, keyed by pane label.
    """

    at: float
    screens: dict[str, tuple[str, ...]]


def tmux(*arguments: str) -> str:
    """Runs one command against the recording's own tmux server.

    Args:
        *arguments: tmux command and arguments.

    Returns:
        The command's standard output.

    Raises:
        RuntimeError: tmux reported a failure.
    """
    done = subprocess.run(
        ["tmux", "-L", SOCKET, "-f", os.devnull, *arguments],
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode:
        raise RuntimeError(f"tmux {arguments[0]}: {done.stderr.strip()}")
    return done.stdout


def seed(repository: Path) -> None:
    """Creates the throwaway repository the two lanes work in.

    Builds on ``acceptance.workspace`` so the project carries the same
    edit and test permissions, then adds the refund module, the one task
    and each lane's brief.

    Args:
        repository: Directory to create the repository in.
    """
    acceptance.workspace(repository, 0)
    payments = repository / "src" / "payments"
    payments.mkdir(parents=True)
    (repository / "src" / "__init__.py").write_text("")
    (payments / "__init__.py").write_text("")
    (payments / "refund.py").write_text(MODULE)
    (repository / "tasks" / f"{ISSUE}.md").write_text(TASK.format(issue=ISSUE))
    (repository / "lanes").mkdir()
    for name, body in BRIEFS.items():
        (repository / "lanes" / f"{name}.md").write_text(
            BRIEF_HEAD.format(name=name) + body.format(issue=ISSUE)
        )
    for command in (
        ["git", "add", "-A"],
        ["git", "commit", "-qm", "chore: add the refund task"],
    ):
        subprocess.run(command, cwd=repository, check=True)


def window(repository: Path, env: dict[str, str]) -> list[Pane]:
    """Starts the recording window: two lane panes over one operator pane.

    Args:
        repository: Directory every pane starts in.
        env: Variables every pane's shell is given.

    Returns:
        The three panes, lanes first, with their geometry.
    """
    startup = repository.parent / "shellrc"
    startup.write_text("PS1='$ '\n")
    shell = f"bash --noprofile --rcfile {startup}"
    settings = [
        argument for key in env for argument in ("-e", key + "=" + env[key])
    ]
    tmux(
        "new-session",
        "-d",
        "-s",
        "demo",
        "-x",
        str(WIDTH),
        "-y",
        str(HEIGHT),
        "-c",
        str(repository),
        *settings,
        shell,
    )
    tmux("set-option", "-g", "status", "off")
    tmux(
        "split-window",
        "-v",
        "-l",
        str(BOTTOM),
        "-c",
        str(repository),
        *settings,
        shell,
    )
    tmux(
        "split-window",
        "-h",
        "-t",
        "demo:0.0",
        "-c",
        str(repository),
        *settings,
        shell,
    )
    listed = tmux(
        "list-panes",
        "-t",
        "demo",
        "-F",
        "#{pane_id} #{pane_left} #{pane_top} #{pane_width} #{pane_height}",
    ).split("\n")
    rows = sorted(
        (tuple(int(x) for x in line.split()[1:]), line.split()[0])
        for line in listed
        if line
    )
    names = {(0, 0): "claude", (0, 1): "codex"}
    panes = []
    for (left, top, width, height), target in rows:
        name = "operator" if top else names[(top, int(left > 0))]
        panes.append(Pane(name, target, left, top, width, height))
    return sorted(
        panes,
        key=lambda pane: (
            ("claude", "codex").index(pane.name)
            if pane.name != "operator"
            else 2
        ),
    )


class Session:
    """Drives the panes and samples them into frames.

    Attributes:
        panes: The recorded panes, keyed by label.
        frames: Readings taken so far.
        started: Monotonic time the recording began.
    """

    def __init__(self, panes: list[Pane]):
        """Starts an empty recording over the given panes.

        Args:
            panes: The panes to sample.
        """
        self.panes = {pane.name: pane for pane in panes}
        self.frames: list[Frame] = []
        self.started = time.monotonic()

    def sample(self) -> None:
        """Reads every pane once and keeps the reading when it changed."""
        screens = {
            name: tuple(
                tmux("capture-pane", "-p", "-t", pane.target).split("\n")[
                    : pane.height
                ]
            )
            for name, pane in self.panes.items()
        }
        if self.frames and self.frames[-1].screens == screens:
            return
        self.frames.append(Frame(time.monotonic() - self.started, screens))

    def hold(self, seconds: float) -> None:
        """Keeps sampling for a fixed time.

        Args:
            seconds: How long to sample.
        """
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            self.sample()
            time.sleep(SAMPLE)

    def type(self, pane: str, text: str) -> None:
        """Types a command into a pane one character at a time and runs it.

        Args:
            pane: Label of the pane to type into.
            text: Command line to type.
        """
        target = self.panes[pane].target
        for character in text:
            tmux("send-keys", "-t", target, "-l", character)
            time.sleep(TYPING)
            self.sample()
        tmux("send-keys", "-t", target, "Enter")
        self.hold(1.0)

    def until(self, reached: Callable[[], bool], timeout: float) -> bool:
        """Samples until a condition holds or the time runs out.

        Args:
            reached: Callable returning True once the awaited state holds.
            timeout: Seconds to wait at most.

        Returns:
            True when the condition held in time.
        """
        deadline = time.monotonic() + timeout
        checked = 0.0
        while time.monotonic() < deadline:
            self.sample()
            if time.monotonic() - checked > 5:
                checked = time.monotonic()
                if reached():
                    return True
            time.sleep(SAMPLE)
        return False


def issue_state(cli: str, home: Path, repository: Path) -> tuple[str, str]:
    """Reads who holds the demo issue and who last reported it ready.

    Args:
        cli: Coordination command.
        home: Coordination home of the run.
        repository: Throwaway repository.

    Returns:
        The issue's owner and the participant whose ``ready`` report is
        the last one recorded, each empty when there is none.
    """
    document = acceptance._document(
        cli, home, ["issue", "show", ISSUE, "--repo", str(repository)]
    )
    owner = str((document.get("record") or {}).get("owner") or "")
    ready = ""
    for event in (document.get("history") or {}).get("records") or []:
        if event.get("kind") == "report":
            ready = (
                str(event.get("participant", ""))
                if event.get("action") == "ready"
                else ""
            )
    return owner, ready


def forbidden(extra: list[str]) -> dict[str, str]:
    """Collects the machine-identifying values and their published form.

    Args:
        extra: Operator-supplied values, such as an account display name.

    Returns:
        Each value mapped to its replacement, longest first.
    """
    values = {str(Path.home()): DEMO_HOME, getpass.getuser(): "dev"}
    values[socket.gethostname()] = "dev"
    for key in ("user.name", "user.email"):
        done = subprocess.run(
            ["git", "config", "--global", key],
            capture_output=True,
            text=True,
            check=False,
        )
        if done.stdout.strip():
            values[done.stdout.strip()] = (
                "dev@example.com" if key == "user.email" else "dev"
            )
    for value in extra:
        values[value] = "dev"
    return dict(
        sorted(values.items(), key=lambda item: len(item[0]), reverse=True)
    )


def redact(frames: list[Frame], places: dict[str, str]) -> list[Frame]:
    """Rewrites every machine value in every frame.

    Args:
        frames: Frames as captured.
        places: Values mapped to their published form, applied in order.

    Returns:
        The frames with every value replaced.
    """

    def fix(text: str) -> str:
        """Replaces every value in one line.

        Args:
            text: A captured line.

        Returns:
            The line with each value replaced.
        """
        for place, published in places.items():
            text = text.replace(place, published)
        return text

    return [
        Frame(
            frame.at,
            {
                name: tuple(fix(line) for line in screen)
                for name, screen in frame.screens.items()
            },
        )
        for frame in frames
    ]


def leaks(frames: list[Frame], places: dict[str, str]) -> list[str]:
    """Finds anything identifying that survived the rewrite.

    Args:
        frames: Redacted frames.
        places: The values that were rewritten.

    Returns:
        One description per line that still carries a value, an email
        address outside ``example.com`` or a token shape.
    """
    found = []
    watched = [place.lower() for place in places if len(place) > 2]
    for frame in frames:
        for name, screen in frame.screens.items():
            for line in screen:
                lowered = line.lower()
                if any(place in lowered for place in watched):
                    found.append(f"{name} at {frame.at:.1f}s: machine value")
                elif EMAIL.search(line) or TOKEN.search(line):
                    found.append(f"{name} at {frame.at:.1f}s: {line.strip()}")
    return found


def timeline(frames: list[Frame]) -> list[float]:
    """Places each frame on the published timeline.

    Args:
        frames: Frames in capture order.

    Returns:
        Each frame's start on a timeline where no frame is shown longer
        than ``HOLD`` seconds, followed by the timeline's total length.
    """
    starts = [0.0]
    for before, after in zip(frames, frames[1:], strict=False):
        starts.append(starts[-1] + min(after.at - before.at, HOLD))
    starts.append(starts[-1] + HOLD * 2)
    return starts


def visibility(start: float, end: float, total: float) -> str:
    """Builds the animation that shows one element for one stretch.

    Args:
        start: Seconds into the loop the element appears.
        end: Seconds into the loop the element disappears.
        total: Length of one loop in seconds.

    Returns:
        An ``animate`` element, or an empty string for an element visible
        for the whole loop.
    """
    begin = round(start / total, 5)
    finish = round(end / total, 5)
    if begin <= 0 and finish >= 1:
        return ""
    if begin <= 0:
        values, times = "1;0", f"0;{finish}"
    elif finish >= 1:
        values, times = "0;1", f"0;{begin}"
    else:
        values, times = "0;1;0", f"0;{begin};{finish}"
    return (
        '<animate attributeName="opacity" calcMode="discrete" '
        f'values="{values}" keyTimes="{times}" dur="{round(total, 2)}s" '
        'repeatCount="indefinite"/>'
    )


def spans(
    frames: list[Frame], starts: list[float], name: str, row: int
) -> list[tuple[str, float, float]]:
    """Groups one pane row into stretches of unchanged text.

    Args:
        frames: Frames in order.
        starts: Frame starts and the total, from ``timeline``.
        name: Pane label.
        row: Row within the pane.

    Returns:
        Each stretch's text, start and end on the published timeline.
        Blank stretches are left out.
    """
    found = []
    current, since = None, 0.0
    for index, frame in enumerate(frames):
        screen = frame.screens.get(name, ())
        text = screen[row].rstrip() if row < len(screen) else ""
        if text != current:
            if current:
                found.append((current, since, starts[index]))
            current, since = text, starts[index]
    if current:
        found.append((current, since, starts[-1]))
    return found


def render(frames: list[Frame], panes: list[Pane], destination: Path) -> None:
    """Writes the frames as one animated SVG.

    Each pane row becomes one text element per stretch it held unchanged,
    so an unchanging line costs one element for the whole recording rather
    than one per frame.

    Args:
        frames: Redacted frames in order.
        panes: Pane geometry.
        destination: File to write.
    """
    starts = timeline(frames)
    total = starts[-1]
    width = round(WIDTH * CHARACTER + MARGIN * 2)
    height = round(HEADER + MARGIN * 2 + LINE * (HEIGHT + len(panes)))
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{height}" viewBox="0 0 {width} {height}" '
        'font-family="ui-monospace, SFMono-Regular, Menlo, Consolas, '
        '\'Liberation Mono\', monospace" font-size="13">',
        f'<rect width="{width}" height="{height}" rx="10" '
        f'fill="{BACKGROUND}"/>',
        f'<rect width="{width}" height="30" rx="10" fill="{BAR}"/>',
        f'<rect y="20" width="{width}" height="10" fill="{BAR}"/>',
        '<circle cx="18" cy="15" r="5.5" fill="#ff5f57"/>'
        '<circle cx="36" cy="15" r="5.5" fill="#febc2e"/>'
        '<circle cx="54" cy="15" r="5.5" fill="#28c840"/>',
    ]
    for index, frame in enumerate(frames):
        minutes, seconds = divmod(int(frame.at), 60)
        label = escape(
            f"agent-parley 0.13.0 · real time {minutes:02d}:{seconds:02d}"
        )
        parts.append(
            f'<text x="{width / 2}" y="19" text-anchor="middle" '
            f'fill="{MUTED}" font-size="12">'
            f"{visibility(starts[index], starts[index + 1], total)}"
            f"{label}</text>"
        )
    for number, pane in enumerate(sorted(panes, key=lambda p: (p.top, p.left))):
        x = MARGIN + pane.left * CHARACTER
        top = HEADER + MARGIN + LINE * (pane.top + number // 2 + 1)
        parts.append(
            f'<text x="{x}" y="{top - LINE * 0.3}" fill="{MUTED}" '
            f'font-size="12">{escape(pane.name)}</text>'
        )
        if pane.left:
            parts.append(
                f'<rect x="{x - CHARACTER * 0.6}" y="{top - LINE}" width="1" '
                f'height="{LINE * (pane.height + 1)}" fill="{BAR}"/>'
            )
        for row in range(pane.height):
            y = round(top + LINE * (row + 0.8), 1)
            for text, start, end in spans(frames, starts, pane.name, row):
                parts.append(
                    f'<text x="{x}" y="{y}" xml:space="preserve" '
                    f'fill="{colour(text)}">'
                    f"{visibility(start, end, total)}{escape(text)}</text>"
                )
    parts.append("</svg>")
    destination.write_text("\n".join(parts) + "\n")


def record(arguments: argparse.Namespace) -> int:
    """Runs the two lanes, records them and writes the SVG.

    Args:
        arguments: Parsed command line.

    Returns:
        Zero when the story completed and the SVG was written.
    """
    cli = str(Path(sys.executable).with_name("agent-parley"))
    base = Path(tempfile.mkdtemp(prefix="parley-demo-"))
    home = base / "state"
    repository = base / "payments-api"
    home.mkdir(mode=0o700)
    seed(repository)
    os.environ["AGENT_PARLEY_HOME"] = str(home)
    os.environ["AGENT_PARLEY_PORT"] = str(free_port())
    acceptance.register(cli, home, repository, list(LANES))
    if arguments.trust:
        acceptance.trust(home, repository, list(LANES))
    env = {
        "PATH": f"{Path(cli).parent}{os.pathsep}{os.environ['PATH']}",
        "AGENT_PARLEY_HOME": os.environ["AGENT_PARLEY_HOME"],
        "AGENT_PARLEY_PORT": os.environ["AGENT_PARLEY_PORT"],
        "HOME": str(Path.home()),
        "TERM": "xterm-256color",
    }
    session = Session(window(repository, env))
    reached = False
    try:
        session.hold(1.0)
        session.type("operator", "agent-parley top --interval 2")
        session.type(
            "claude", 'agent-parley run claude --task "Follow lanes/claude.md"'
        )
        claimed = session.until(
            lambda: issue_state(cli, home, repository)[0] == "claude",
            arguments.timeout,
        )
        session.hold(SETTLE)
        if claimed:
            session.type(
                "codex", 'agent-parley run codex --task "Follow lanes/codex.md"'
            )
            reached = session.until(
                lambda: (
                    issue_state(cli, home, repository) == ("codex", "codex")
                ),
                arguments.timeout,
            )
            session.hold(SETTLE)
    finally:
        (base / "frames.jsonl").write_text(
            "".join(
                json.dumps(dataclasses.asdict(frame)) + "\n"
                for frame in session.frames
            )
        )
        panes = list(session.panes.values())
        subprocess.run(
            ["tmux", "-L", SOCKET, "kill-server"],
            capture_output=True,
            check=False,
        )
        subprocess.run(
            [cli, "--home", str(home), "down"], capture_output=True, check=False
        )
    places = {
        str(repository): DEMO_REPOSITORY,
        str(home): DEMO_STATE,
        str(base): DEMO_HOME,
        **forbidden(arguments.redact),
    }
    frames = redact(session.frames, places)
    found = leaks(frames, places)
    print(f"workspace {base}, {len(frames)} frames, story completed: {reached}")
    if found:
        print(
            "refusing to write: identifying text survived",
            *found[:20],
            sep="\n",
        )
        return 1
    if not reached and not arguments.partial:
        print("refusing to write: the story did not complete; see frames.jsonl")
        return 1
    render(frames, panes, arguments.output)
    print(f"wrote {arguments.output}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parses the command line and records the demo.

    Args:
        argv: Arguments after the program name, or None for ``sys.argv``.

    Returns:
        The process exit status.
    """
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--trust",
        action="store_true",
        help="Record the throwaway directories as trusted by both clients.",
    )
    parser.add_argument(
        "--redact",
        action="append",
        default=[],
        help="Another value to rewrite, such as an account display name.",
    )
    parser.add_argument("--timeout", type=float, default=TIMEOUT)
    parser.add_argument(
        "--partial",
        action="store_true",
        help="Write the SVG even when the story did not complete.",
    )
    parser.add_argument("--output", type=Path, default=DESTINATION)
    return record(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
