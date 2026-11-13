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

The story and the stub-lane harness live in the package, in
`agent_parley.demo_scenario` and `agent_parley.demo`, so ``agent-parley
demo`` runs a short cut of the same chapters and cannot drift from this
recording. The native CLIs are stubs. The harness writes ``claude`` and
``codex`` executables that run ``exec sleep 900`` onto the front of
``PATH``, so the lane processes are real, the coordination path is real, and
only the model session is a stand-in. Lanes are launched one at a time
because two simultaneous launches race for the same server lock.

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

The result is written to ``docs/assets/demo.svg``, the full nine-chapter
walkthrough; ``--short`` writes the short cut to
``docs/assets/demo-short.svg`` instead, and ``--video`` writes the launch
video's frames to ``docs/video/frames.js``. ``scripts/record_live.py``
(``make demo``) records real clients instead and needs model quota. The
README shows the launch video, not these SVGs.

``--video`` records the short tour and writes no SVG. It keeps the steps
named in ``VIDEO``, each after its chapter card, and writes their text,
colours and hold times to ``docs/video/frames.js`` for the launch video
composition beside it. The composition reads only that file for terminal
text, so the video uses the same captured frames and timing as the SVG.
"""

import dataclasses
import json
import sys
import tempfile
import textwrap
import time
from collections.abc import Sequence
from pathlib import Path

from agent_parley.demo import (
    COLUMNS,
    FRAME_LINES,
    Card,
    Recorder,
    Step,
    environment,
    fixtures,
    git,
)
from agent_parley.demo_scenario import record, tour

SHORT = (
    "ada claims #41 from inside its lane.",
    "grace asks for the same paths: a named collision.",
    "grace accepts; only then does ownership move.",
    "The dashboard: state, issues, mail, leases, denials.",
)
VIDEO = (
    "ada runs on claude, in a worktree of its own.",
    "grace runs on codex, right beside it.",
    "Two providers, two lanes, one shared view.",
    "ada claims #41 from inside its lane.",
    "ada reserves the refund module.",
    "grace asks for the same paths: a named collision.",
    "So grace queues behind ada instead of waiting blind.",
    "ada offers #41 to grace with a summary.",
    "grace accepts; only then does ownership move.",
    "The dashboard: state, issues, mail, leases, denials.",
)
TYPE_RATE = 0.028
TYPE_LIMIT = 1.6
FADE = 0.35
CAPTION = 48.0
SANS = "system-ui, -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif"
CHARACTER = 7.81
LINE = 18.0
MARGIN = 18.0
HEADER = 30.0
DEMO_HOME = "/home/dev"
DEMO_REPOSITORY = "/home/dev/payments-api"
DEMO_STATE = "/home/dev/.local/state/agent-parley"
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


def cut(
    items: Sequence[Step | Card], captions: Sequence[str]
) -> list[Step | Card]:
    """Selects the steps a cut shows, each chapter kept with its card.

    Args:
        items: Every card and step a recording captured, in order.
        captions: Captions of the steps the cut shows, in recording order.

    Returns:
        The chosen steps in recording order. The card that opened a chapter
        comes before the first chosen step of that chapter; a chapter with
        no chosen step loses its card.

    Raises:
        RuntimeError: If the recording lost or reordered one of the
            captions.
    """
    chosen: list[Step | Card] = []
    card: Card | None = None
    for item in items:
        if isinstance(item, Card):
            card = item
        elif item.caption in captions:
            if card is not None:
                chosen.append(card)
                card = None
            chosen.append(item)
    found = [item.caption for item in chosen if isinstance(item, Step)]
    if found != list(captions):
        raise RuntimeError(f"cut captions changed: {found}")
    return chosen


def timeline(items: Sequence[Step | Card]) -> dict[str, object]:
    """Lays a cut on the recording's timing model for the video.

    The video composition in ``docs/video`` draws exactly what this returns,
    so a frame there holds the same clipped rows, colours and hold times as
    the animated SVG.

    Args:
        items: Cards and steps of the cut, in order.

    Returns:
        The frame size in columns and rows, the cut's length in seconds and
        one entry per item with its start second and hold time. A card
        entry carries its kicker, title and lines; a step entry carries its
        typing time, prompt, command, caption and each row `frame` shows,
        paired with the colour `colour` gives it.
    """
    start = 0.0
    entries: list[dict[str, object]] = []
    for item in items:
        seconds = hold_seconds(item)
        entry: dict[str, object] = {
            "start": round(start, 3),
            "seconds": round(seconds, 3),
        }
        if isinstance(item, Card):
            entry |= {
                "kind": "card",
                "kicker": item.kicker,
                "title": item.title,
                "lines": list(item.lines),
            }
        else:
            entry |= {
                "kind": "step",
                "typing": round(typing_seconds(item), 3),
                "prompt": item.prompt,
                "command": item.command,
                "caption": item.caption,
                "rows": [[row, colour(row)] for row in frame(item)],
            }
        entries.append(entry)
        start += seconds
    return {
        "columns": COLUMNS,
        "rows": FRAME_LINES,
        "seconds": round(start, 3),
        "items": entries,
    }


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

    Run with ``--screenshots`` to record the static screenshots instead, or
    with ``--short`` to record the README's first-screen cut: the `tour`
    steps whose captions `SHORT` names, written to ``demo-short.svg``. With
    ``--video`` the `tour` steps whose captions `VIDEO` names, and their
    chapter cards, are laid out by `timeline` and written to
    ``docs/video/frames.js`` for the launch video composition.

    Returns:
        Zero when the recording and the asset were written.

    Raises:
        RuntimeError: If the short cut lost one of its captions.
    """
    destination = Path(__file__).resolve().parent.parent / "docs" / "assets"
    if "--screenshots" in sys.argv[1:]:
        return screenshots(destination)
    short = "--short" in sys.argv[1:]
    video = "--video" in sys.argv[1:]
    with tempfile.TemporaryDirectory(prefix="agent-parley-demo-") as path:
        base = Path(path)
        home, binaries, repository = fixtures(base)
        recorder = Recorder(home, repository, environment(home, binaries, base))
        try:
            (tour if short or video else record)(recorder)
        finally:
            recorder.close()
        places = {
            str(home): DEMO_STATE,
            str(repository): DEMO_REPOSITORY,
            str(base): DEMO_HOME,
            str(Path.home()): DEMO_HOME,
        }
        steps = rewritten(recorder.steps, places)
    if video:
        kept = cut(steps, VIDEO)
        document = timeline(kept)
        script = destination.parent / "video" / "frames.js"
        script.write_text(
            f"window.PARLEY_CUT = {json.dumps(document, indent=1)};\n"
        )
        seconds = document["seconds"]
        print(f"{len(kept)} frames, {seconds}s, written to {script}")
        return 0
    name = "demo.svg"
    if short:
        name = "demo-short.svg"
        chosen = [
            step
            for step in steps
            if isinstance(step, Step) and step.caption in SHORT
        ]
        if [step.caption for step in chosen] != list(SHORT):
            raise RuntimeError(f"short cut captions changed: {chosen}")
        steps = list(chosen)
    total = render(steps, destination / name)
    print(f"{len(steps)} frames, {total:.0f}s, written to {destination / name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
