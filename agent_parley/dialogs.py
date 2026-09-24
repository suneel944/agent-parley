"""Recognizes the native client dialogs a launcher's terminal is holding.

A native client draws a blocking dialog on its own terminal: a usage limit it
cannot get past, a tool permission prompt, a hook trust review at startup. None
of those screens reaches the coordination substrate. The client runs no hook
while it waits for a keypress and its process stays alive, so the lane reads as
working, starting or merely quiet while nobody is driving it, and messages,
wakes and load shares keep arriving.

The launcher already owns the pseudo-terminal it started the client on, so that
byte stream is the only observation available without a vendor interface. This
module reads it, recognizes the screens recorded from live clients, and maps
each to one action: an exhausted provider capacity with the reset instant the
screen names, the option the operator configured for an answerable prompt, and
escalation for everything else. An answer is the digit an operator would press
on an option the client itself is offering, and it is sent only for a dialog the
operator named for that lane. Nothing here weakens a native permission decision
or adds a way around one, and an unnamed dialog is escalated rather than
answered.

The patterns are recorded from `claude` CLI 2.1.270 and Codex CLI 0.153.4.
Matching runs on a flattened screen with escape sequences removed and
whitespace collapsed, because both clients redraw and rewrap the same dialog
continuously and neither emits line breaks between redraws. Only the bottom
of that screen is read as a dialog, and a dialog that offers a choice must
draw its option list and selection footer there, so output a lane printed
that merely quotes a dialog's words is never taken for one.

A dialog is released as soon as it is answered, by this watcher or by the
operator typing at an attached terminal: the screen read before the answer is
dropped, and the client's next output is judged on its own.

A question the lane asks its operator through the client's question picker is
published with the question and the options it offers. It is answered only by
the standing reply an operator recorded for the lane, typed as free text.

Publication reuses the lane surfaces a reader already has: the activity file
carries the dialog under `dialog` and names it in `activity`, provider capacity
is recorded through the supervisor's durable observation, and the operator gets
one notification per situation. Nothing here observes liveness or moves
ownership.

A permission prompt is also visible without the screen: the client runs its
`PermissionRequest` hook, which names the tool it is asking about. The
checkpoint publishes that observation through the same record, so status, the
fit checks, wake admission and the problem report read one surface whether the
prompt was seen on the terminal or reported by the client. A resumed session
asks again for tools the operator already allowed, and no answer the bridge can
give is a decision the operator made, so carrying that decision forward is an
opt-in the operator records per project or per lane, scoped to this bridge's own
MCP server.
"""

import calendar
import contextlib
import datetime
import hashlib
import re
import textwrap
import time
import zoneinfo
from pathlib import Path
from typing import NamedTuple

from agent_parley import protocol
from agent_parley.state import BridgeError, lock, write_json

SCREEN_BYTES = 8192
REGION_CHARS = 1200
FOOTER_CHARS = 120
PROMPT_CHARS = 300
QUESTION_CHARS = 120
SIGNATURE_CHARS = 200
REPORT_CHARS = 1200
REPORT_LINES = 4
REPORT_WIDTH = 100
OPTION_LABEL = 60
ESCALATE_AFTER = 30.0
REPEAT_LIMIT = 2
MARKER = "dialog: "
APPROVAL = "waiting for approval"
PERMISSION = "tool-permission"
PRE_APPROVE = "approve_bridge_tools"
STANDING_REPLY = "answer_questions"
QUESTION = "question"
TRUST = "directory-trust"
EXHAUSTED = "exhausted"
ANSWER = "answer"
ASK = "ask"
FREE_TEXT = "type something"
SHELL_TITLE = "Bash command"
BRIDGE_ANSWER = "Yes"
SHELL_OPERATORS = re.compile(r"[;&|<>$`\\]")
BOX = re.compile(r"[─-╿]")

ESCAPES = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"
    r"|\x1b[P^_X][^\x1b]*(?:\x1b\\)?"
    r"|\x1b\[[0-?]*[ -/]*[@-~]"
    r"|\x1b[@-Z\\-_]"
)
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
OPTION = re.compile(r"(?:›|❯|>)?\s*(\d)\.\s+")
LABEL_END = re.compile(r"·|Esc to |Press enter|esc to |Enter to ")
FOOTER = re.compile(
    r"esc to (?:cancel|go back|exit)|press enter|enter to (?:select|confirm)",
    re.IGNORECASE,
)
QUESTION_LINE = re.compile(r"([^?!›❯☐☒✔]{3,}\?)")
PROMPT_SHAPE = re.compile(
    r"do you want to|\(y/n\)|press enter to confirm|esc to cancel"
    r"|\d\.\s+yes\b",
    re.IGNORECASE,
)
RESETS = re.compile(
    r"resets\s+(?P<month>[A-Za-z]{3})[a-z]*\s+(?P<day>\d{1,2}),?\s+"
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<meridiem>am|pm)"
    r"(?:\s*\((?P<zone>[A-Za-z_]+(?:/[A-Za-z_+-]+)*)\))?",
    re.IGNORECASE,
)
MONTHS = {
    name.lower(): index
    for index, name in enumerate(calendar.month_abbr)
    if name
}


class Dialog(NamedTuple):
    """One recorded native screen and the action policy maps it to.

    Attributes:
        name: Stable identifier an operator names to configure an answer.
        label: Short description shown on the lane's status line.
        pattern: Recognizer applied to the screen's dialog region.
        action: An exhausted provider capacity, an offered answer, or a
            question the operator is asked.
        framed: Whether the screen must also draw an option list and a
            selection footer after the recognized text. Only the usage-limit
            notice is drawn without one.
    """

    name: str
    label: str
    pattern: re.Pattern[str]
    action: str
    framed: bool = True


DIALOGS: tuple[Dialog, ...] = (
    Dialog(
        "usage-limit",
        "provider usage limit",
        re.compile(
            r"[⎿■]\s*[^⎿■]{0,24}?"
            r"(?:hit your [\w-]+ limit|usage limit reached"
            r"|weekly limit\b.{0,40}\bresets)",
            re.IGNORECASE,
        ),
        EXHAUSTED,
        framed=False,
    ),
    Dialog(
        QUESTION,
        "a question for the operator",
        re.compile(
            r"\d\.\s+type something\.?\s+\d\.\s+chat about this\b"
            r".{0,40}?enter to select\b.{0,20}?to navigate",
            re.IGNORECASE,
        ),
        ASK,
    ),
    Dialog(
        "hook-review",
        "native hook trust review",
        re.compile(r"hooks need review", re.IGNORECASE),
        ANSWER,
    ),
    Dialog(
        TRUST,
        "native directory trust prompt",
        re.compile(
            r"\btrust\b[^?]{0,60}\b(?:directory|folder|workspace)\b[^?]{0,40}\?"
            r"|\bproject you created or one you trust\b"
            r"|\bi trust this (?:folder|directory)\b",
            re.IGNORECASE,
        ),
        ANSWER,
    ),
    Dialog(
        PERMISSION,
        "native tool permission prompt",
        re.compile(r"do you want to proceed\?", re.IGNORECASE),
        ANSWER,
    ),
)

NAMES = frozenset(item.name for item in DIALOGS if item.action == ANSWER)
BY_NAME = {item.name: item for item in DIALOGS}


class Frame(NamedTuple):
    """The recognized dialog and the part of the screen that draws it.

    Attributes:
        dialog: Policy entry the screen matched.
        text: Screen text from the dialog's first recognized line to the end
            of the screen, which is where its options and footer are read.
    """

    dialog: Dialog
    text: str


def flatten(data: bytes) -> str:
    """Renders a pseudo-terminal byte stream as one comparable line.

    Args:
        data: Bytes read from the client's pseudo-terminal, possibly cut mid
            character and mid escape sequence.

    Returns:
        The same screen with escape sequences and control bytes removed and
        every run of whitespace collapsed to one space, so a redrawn or
        rewrapped dialog compares equal to itself.
    """
    text = ESCAPES.sub(" ", data.decode("utf-8", "replace"))
    return " ".join(CONTROL.sub(" ", text).split())


def _picker_start(region: str, end: int) -> int:
    """Finds where a question picker's question begins before its options.

    Args:
        region: Dialog region of the screen.
        end: Offset of the picker's own footer options.

    Returns:
        Offset of the question line drawn above the first option, or of the
        first option when no question line precedes it.
    """
    firsts = [
        mark.start()
        for mark in OPTION.finditer(region, 0, end)
        if mark.group(1) == "1"
    ]
    first = firsts[-1] if firsts else end
    floor = max(0, first - 2 * QUESTION_CHARS)
    lines = list(QUESTION_LINE.finditer(region, floor, first))
    return lines[-1].start() if lines else first


def locate(screen: str) -> Frame | None:
    """Finds the recorded dialog the bottom of a flattened screen draws.

    The flattened tail holds scrollback as well as the dialog: model output,
    diffs and file contents a lane printed. A dialog is therefore recognized
    only in the last `REGION_CHARS` of the screen, and a dialog that offers a
    choice must also draw at least two numbered options after its recognized
    text and a selection footer at the very end of the screen. A usage-limit
    notice draws no options, so it must carry the client's own notice marker
    instead, which quoted text in a code block or a log does not.

    Args:
        screen: Flattened screen text.

    Returns:
        The first policy entry drawn in the dialog region, with the text that
        draws it, or None.
    """
    region = screen[-REGION_CHARS:]
    for dialog in DIALOGS:
        found = dialog.pattern.search(region)
        if found is None:
            continue
        if not dialog.framed:
            return Frame(dialog, region[found.start() :])
        start = found.start()
        if dialog.name == QUESTION:
            start = _picker_start(region, start)
        text = region[start:]
        if len(options(text)) >= 2 and FOOTER.search(text[-FOOTER_CHARS:]):
            return Frame(dialog, text)
    return None


def match(screen: str) -> Dialog | None:
    """Returns the recorded dialog a flattened screen shows.

    Args:
        screen: Flattened screen text.

    Returns:
        The policy entry drawn in the screen's dialog region, or None.
    """
    found = locate(screen)
    return found.dialog if found else None


def prompted(screen: str) -> bool:
    """Reports whether a flattened screen holds an answer-shaped prompt.

    Only the bottom of the screen is read, because a client draws a waiting
    prompt last and the same words further up are output it printed.

    Args:
        screen: Flattened screen text.

    Returns:
        True when the end of the screen carries a marker a client only draws
        while it is waiting for a keypress.
    """
    return bool(PROMPT_SHAPE.search(screen[-PROMPT_CHARS:]))


def question(text: str) -> str:
    """Reads the question a question picker asks.

    Args:
        text: Screen text drawing the picker, from its question line on.

    Returns:
        The last question sentence drawn above the first option, bounded to
        `QUESTION_CHARS`, or empty text when the picker drew none.
    """
    first = OPTION.search(text)
    head = text[: first.start() if first else len(text)]
    lines = QUESTION_LINE.findall(head)
    if not lines:
        return ""
    line = lines[-1].rsplit(". ", 1)[-1].strip()
    return line[-QUESTION_CHARS:].strip()


def options(screen: str) -> dict[str, str]:
    """Reads the numbered choices the client is currently offering.

    A screen tail can hold several redraws of the same dialog, so a later
    rendering of one number replaces an earlier one.

    Args:
        screen: Flattened screen text.

    Returns:
        Option digit to its bounded label, in the order the client drew them.
    """
    found: dict[str, str] = {}
    marks = list(OPTION.finditer(screen))
    for index, mark in enumerate(marks):
        end = len(screen)
        if index + 1 < len(marks):
            end = marks[index + 1].start()
        label = LABEL_END.split(screen[mark.end() : end])[0]
        label = label[:OPTION_LABEL].strip()
        if label:
            found[mark.group(1)] = label
    return found


def _comparable(value: str) -> str:
    """Reduces a label or a configured answer to letters and digits."""
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def keys(screen: str, answer: str) -> bytes:
    """Returns the keystrokes that choose an offered option by its text.

    The configured answer names option text rather than a position, because
    the clients reorder and add options between versions. An exact label wins
    over a longer label the answer only begins.

    Args:
        screen: Flattened screen text.
        answer: Option text the operator configured for this dialog.

    Returns:
        The option's digit and a carriage return, which is what an operator
        would press, or empty bytes when the screen offers no such option.
    """
    wanted = _comparable(answer)
    if not wanted:
        return b""
    offered = options(screen)
    for number, label in offered.items():
        if _comparable(label) == wanted:
            return f"{number}\r".encode()
    for number, label in offered.items():
        if _comparable(label).startswith(wanted):
            return f"{number}\r".encode()
    return b""


def _zone(name: str | None) -> datetime.tzinfo | None:
    """Resolves the zone a screen named, or the host's own zone."""
    if not name:
        return datetime.datetime.now().astimezone().tzinfo
    try:
        return zoneinfo.ZoneInfo(name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return None


def reset_at(screen: str, now: float) -> float | None:
    """Reads the reset instant a usage-limit screen names.

    The recorded screen names a month, a day, an hour and a zone but no year,
    so the year chosen is the one placing the instant nearest the observation.

    Args:
        screen: Flattened screen text.
        now: Unix time the screen was observed.

    Returns:
        Unix time the provider named, or None when the screen states no reset
        or names a zone this host cannot resolve.
    """
    found = RESETS.search(screen)
    if found is None:
        return None
    month = MONTHS.get(str(found["month"]).lower())
    zone = _zone(found["zone"])
    if month is None or zone is None:
        return None
    hour = int(found["hour"]) % 12
    if str(found["meridiem"]).lower() == "pm":
        hour += 12
    reference = datetime.datetime.fromtimestamp(now, zone).year
    stamps = []
    for year in (reference - 1, reference, reference + 1):
        try:
            moment = datetime.datetime(
                year,
                month,
                int(found["day"]),
                hour,
                int(found["minute"] or 0),
                tzinfo=zone,
            )
        except ValueError:
            continue
        stamps.append(moment.timestamp())
    return min(stamps, key=lambda value: abs(value - now), default=None)


def report(screen: str) -> list[str]:
    """Rewraps the end of a screen into the lines a report shows.

    Args:
        screen: Flattened screen text.

    Returns:
        The last lines of the screen, rewrapped to a readable width.
    """
    return textwrap.wrap(screen[-REPORT_CHARS:], REPORT_WIDTH)[-REPORT_LINES:]


def configured(manifest: dict, name: str) -> dict[str, str]:
    """Reads the dialog answers the operator recorded for one lane.

    A project-level entry applies to every lane and a lane's own entry
    overrides it by dialog name. Only a recorded dialog name is honoured, so a
    typo leaves the screen escalating rather than answering something else.

    Args:
        manifest: Project manifest as the roster reports it.
        name: Participant that owns the lane.

    Returns:
        Dialog name to the option text the operator chose. An empty mapping
        means every dialog escalates, which is the shipped default.
    """
    project = (manifest.get("supervision") or {}).get("dialogs")
    participant = (manifest.get("participants") or {}).get(name) or {}
    lane = participant.get("dialogs")
    merged: dict[str, str] = {}
    for source in (project, lane):
        if not isinstance(source, dict):
            continue
        for key, value in source.items():
            if str(key) in NAMES and str(value).strip():
                merged[str(key)] = str(value).strip()
    return merged


def requested(tool: str, previous: object, now: float) -> dict:
    """Records the tool a native permission prompt is waiting on.

    A client that asks for permission reaches the coordination substrate twice:
    as the screen the launcher's watcher reads, and as the `PermissionRequest`
    checkpoint, which names the tool but draws nothing. Both publish the same
    record on the same lane surface, so a reader has one place to look.

    Args:
        tool: Fully qualified tool name the client asked about.
        previous: Record already published for this lane, if any.
        now: Wall-clock instant this request was observed.

    Returns:
        The dialog record naming the tool, whether it belongs to this bridge's
        own MCP server, and the instant the wait began. A repeated request for
        the same tool keeps the earlier instant, so the recorded wait measures
        how long the prompt has stood unanswered rather than how recently the
        client redrew it.
    """
    dialog = BY_NAME[PERMISSION]
    since = now
    if isinstance(previous, dict) and previous.get("tool") == tool:
        recorded = previous.get("since")
        if isinstance(recorded, (int, float)):
            since = float(recorded)
    return {
        "name": dialog.name,
        "label": dialog.label,
        "action": dialog.action,
        "tool": tool,
        "bridge": tool.startswith(protocol.TOOL_PREFIX),
        "since": since,
        "at": now,
    }


def waited(state: dict, now: float) -> int | None:
    """Measures how long a recorded approval prompt has stood unanswered.

    Args:
        state: Lane activity record.
        now: Wall-clock instant to measure against.

    Returns:
        Seconds the prompt has waited, or None when the lane records no
        approval prompt with an instant to measure from.
    """
    record = state.get("dialog")
    if not str(state.get("activity", "")).startswith(APPROVAL):
        return None
    if not isinstance(record, dict) or record.get("name") != PERMISSION:
        return None
    since = record.get("since")
    if not isinstance(since, (int, float)):
        return None
    return max(0, int(now - float(since)))


def pre_approved(manifest: dict, name: str) -> bool:
    """Reports whether a lane may pre-approve this bridge's own MCP tools.

    A resumed session asks again for permission to use the tools the operator
    already allowed, and nothing the bridge records can answer that prompt. The
    client's own permission settings can carry the decision into the next
    session, but granting it is the operator's to make, so it is off until a
    project or one of its lanes records it. A lane entry overrides the project.

    Args:
        manifest: Project manifest as the roster reports it.
        name: Participant that owns the lane.

    Returns:
        True when the operator recorded the opt-in for this lane, which scopes
        the pre-approval to this bridge's own MCP server and nothing else.
    """
    project = (manifest.get("supervision") or {}).get(PRE_APPROVE)
    participant = (manifest.get("participants") or {}).get(name) or {}
    lane = participant.get(PRE_APPROVE)
    chosen = lane if isinstance(lane, bool) else project
    return chosen is True


def bridge_shell(screen: str, text: str) -> bool:
    """Reports whether a shell permission prompt asks for this bridge's CLI.

    The protocol prompt orders every lane to run `protocol.cli_command`
    through its shell tool, so a lane launched before the operator's opt-in
    parks on the first such command. Only a prompt titled as a shell command
    whose command begins with that exact interpreter and module, and whose
    remainder carries no shell operator that could chain a second command,
    qualifies. Any other screen escalates as before.

    Args:
        screen: Flattened screen text.
        text: Part of the screen that draws the prompt's question and options.

    Returns:
        True when the prompt asks to run this bridge's CLI and nothing else.
    """
    region = screen[-REGION_CHARS:]
    head = region[: max(0, len(region) - len(text))]
    title = head.rfind(SHELL_TITLE)
    if title < 0:
        return False
    shown = " ".join(BOX.sub(" ", head[title + len(SHELL_TITLE) :]).split())
    command = protocol.cli_command() + " "
    if not shown.startswith(command):
        return False
    return SHELL_OPERATORS.search(shown[len(command) :]) is None


def standing_reply(manifest: dict, name: str) -> str:
    """Reads the reply an operator recorded for a lane's own questions.

    A lane can ask its operator a design question through the client's
    question picker, and an unattended lane would otherwise hold that picker
    until someone opens its terminal. The reply is text the operator wrote
    once for every question, so it is off until a project or one of its lanes
    records it, and a lane entry overrides the project.

    Args:
        manifest: Project manifest as the roster reports it.
        name: Participant that owns the lane.

    Returns:
        The recorded reply, or empty text when every question escalates.
    """
    project = (manifest.get("supervision") or {}).get(STANDING_REPLY)
    participant = (manifest.get("participants") or {}).get(name) or {}
    lane = participant.get(STANDING_REPLY)
    chosen = lane if isinstance(lane, str) else project
    return chosen.strip() if isinstance(chosen, str) else ""


def typed(text: str, reply: str) -> bytes:
    """Returns the keystrokes that answer a question picker in free text.

    Args:
        text: Screen text drawing the picker.
        reply: Standing reply the operator recorded.

    Returns:
        The digit of the picker's free-text option, the reply and a carriage
        return, or empty bytes when the picker offers no free-text option or
        the reply is empty.
    """
    if not reply:
        return b""
    for number, label in options(text).items():
        if _comparable(label).startswith(FREE_TEXT):
            return f"{number}{reply}\r".encode()
    return b""


class Watch:
    """Watches one launcher's terminal and publishes the dialog it holds.

    The watcher is fed the same bytes the launcher already forwards, so it adds
    no reader, no thread and no second source of truth about the screen.
    """

    def __init__(
        self,
        directory: Path,
        name: str,
        answers: dict[str, str] | None = None,
        *,
        deadline: float = ESCALATE_AFTER,
        reply: str = "",
        bridge: bool | None = None,
    ) -> None:
        """Prepares a watcher for one lane's pseudo-terminal.

        Args:
            directory: Private project state directory.
            name: Participant that owns the lane.
            answers: Dialog name to configured option text.
            deadline: Seconds an unrecognized prompt may hold an unchanged
                screen before it escalates.
            reply: Standing reply typed into a question picker, empty when
                every question escalates.
            bridge: Whether the operator opted this lane into approving this
                bridge's own tools, which lets a shell prompt for its CLI be
                answered as the launch's permission rule would allow it. None
                reads the opt-in from the manifest when such a prompt is
                drawn, so a lane launched before the opt-in is unparked too.
        """
        self._directory = directory
        self._name = name
        self._answers = dict(answers or {})
        self._deadline = deadline
        self._reply = reply
        self._bridge = bridge
        self._tail = b""
        self._signature = ""
        self._since = 0.0
        self._resolved = False
        self._parked = False
        self._answered = False
        self._repeats: dict[str, int] = {}

    def _opted_in(self) -> bool:
        """Reads whether this lane's operator allows this bridge's own tools.

        A manifest that cannot be read counts as no opt-in, so the prompt
        escalates rather than being answered by guess.
        """
        if self._bridge is not None:
            return self._bridge
        from agent_parley import roster

        with contextlib.suppress(BridgeError, OSError, ValueError, KeyError):
            return pre_approved(roster.read(self._directory), self._name)
        return False

    @property
    def holding(self) -> bool:
        """Reports whether a dialog is published as holding the screen."""
        return self._parked

    def answered(self) -> None:
        """Records that the dialog on screen was just answered.

        The screen already read is the dialog as it stood before the answer,
        so it is dropped rather than left to scroll out of the tail. The next
        output the client draws releases the published dialog and is judged on
        its own, which makes a dialog drawn again after an answer a new dialog
        rather than one already handled.
        """
        self._tail = b""
        self._answered = True

    def advance(self, output: bytes, now: float, held: bool = False) -> bytes:
        """Observes new terminal output and decides one action.

        A screen is acted on only once it has stopped changing, so a dialog
        drawn across several reads is answered as one screen rather than half
        of one. Output after an answer, from this watcher or the operator,
        releases the dialog the answer dismissed before it is read.

        Args:
            output: Bytes just read from the client, empty when the launcher
                only woke on its own timeout.
            now: Monotonic time of this pass.
            held: Whether the operator is holding a partially entered line, in
                which case an answer waits for the line rather than racing it.

        Returns:
            Keystrokes to write to the client, which is the configured answer
            to a recognized dialog and nothing else.
        """
        if output:
            if self._answered:
                self._answered = False
                self._signature = ""
                self._resolved = False
                self._release()
            self._tail = (self._tail + output)[-SCREEN_BYTES:]
        if not self._tail:
            return b""
        screen = flatten(self._tail)
        found = locate(screen)
        signature = self._describe(screen, found)
        if signature != self._signature:
            self._signature = signature
            self._since = now
            self._resolved = False
            if not signature:
                self._release()
            return b""
        if not signature:
            self._release()
            return b""
        if self._resolved and not self._approvable(screen, found):
            return b""
        return self._act(screen, found, now, held)

    def _approvable(self, screen: str, found: Frame | None) -> bool:
        """Reports whether an escalated prompt became answerable in place.

        A lane parked on a shell prompt for this bridge's CLI stays parked
        until someone answers it, so the operator's later opt-in is read again
        while that one prompt holds the screen. Any other escalated screen is
        left as it was published.
        """
        return (
            self._parked
            and found is not None
            and found.dialog.name == PERMISSION
            and not self._answers.get(PERMISSION)
            and bridge_shell(screen, found.text)
            and self._opted_in()
        )

    def _describe(self, screen: str, found: Frame | None) -> str:
        """Names the screen so a redraw of it compares equal to itself."""
        if found is not None:
            return "\x00".join(
                (found.dialog.name, *options(found.text).values())
            )
        if prompted(screen):
            return "prompt\x00" + screen[-SIGNATURE_CHARS:]
        return ""

    def _act(
        self, screen: str, found: Frame | None, now: float, held: bool
    ) -> bytes:
        """Applies the policy for a screen that has stopped changing."""
        if found is None:
            if now - self._since < self._deadline:
                return b""
            self._escalate(None, "an unrecognized native prompt", screen)
            return b""
        dialog = found.dialog
        if dialog.action == EXHAUSTED:
            self._exhaust(dialog, found.text)
            return b""
        detail: dict = {}
        label = dialog.label
        if dialog.action == ASK:
            asked = question(found.text)
            label = f"asks the operator: {asked or 'an unnamed question'}"
            answer = self._reply
            pressed = typed(found.text, answer)
            detail = {
                "question": asked,
                "options": [
                    f"{number}. {text}"
                    for number, text in options(found.text).items()
                ],
            }
        else:
            answer = self._answers.get(dialog.name, "")
            if (
                not answer
                and dialog.name == PERMISSION
                and bridge_shell(screen, found.text)
                and self._opted_in()
            ):
                answer = BRIDGE_ANSWER
            pressed = keys(found.text, answer) if answer else b""
        repeats = self._repeats.get(self._signature, 0)
        if not pressed or repeats >= REPEAT_LIMIT:
            self._escalate(dialog, label, found.text, detail)
            return b""
        if held:
            return b""
        self._repeats = {self._signature: repeats + 1}
        self._publish(
            dialog,
            found.text,
            {
                **detail,
                "label": label,
                "answer": answer,
                "keys": pressed.decode().strip(),
            },
        )
        self.answered()
        return pressed

    def _exhaust(self, dialog: Dialog, screen: str) -> None:
        """Parks the lane and records the provider capacity it reported.

        The capacity is recorded before the dialog is published, so a reader
        that sees the lane parked on a usage limit also sees the exhaustion
        that parked it rather than a lane with no capacity record. The
        observation names the session the lane last published, because a
        stranded-claim candidate without one is evidence recovery refuses,
        and supervision rejected every such candidate before it could offer
        the claim to a peer.
        """
        observed = time.time()
        reset = reset_at(screen, observed)
        from agent_parley import checkpoints, supervision

        with contextlib.suppress(BridgeError, OSError, ValueError):
            session = checkpoints.activity(self._directory, self._name).get(
                "session_id"
            )
            supervision.record_capacity(
                self._directory,
                self._name,
                {
                    "state": "exhausted",
                    "observed_at": observed,
                    "reset_at": reset,
                    "source": "native-dialog",
                    "session_id": str(session or ""),
                    "observation_id": self._evidence(dialog.name, reset),
                },
            )
        if self._publish(dialog, screen, {"reset_at": reset}):
            self._notify(dialog.label, " ".join(report(screen)))

    def _escalate(
        self,
        dialog: Dialog | None,
        label: str,
        screen: str,
        detail: dict | None = None,
    ) -> None:
        """Parks the lane on a screen no configured answer covers.

        A question carries its own text and option lines into the record and
        the notice, so the operator can answer from the notice alone.
        """
        extra = dict(detail or {})
        if not self._publish(
            dialog, screen, {**extra, "label": label, "escalated": True}
        ):
            return
        shown = " ".join(report(screen))
        if extra.get("options"):
            shown = " | ".join((label, *extra["options"]))
        self._notify(label, shown)

    def _evidence(self, name: str, reset: float | None) -> str:
        """Identifies one screen situation for a durable observation."""
        digest = hashlib.sha256(
            f"{self._name}\x00{name}\x00{reset}".encode()
        ).hexdigest()[:16]
        return f"dialog:{digest}"

    def _publish(
        self, dialog: Dialog | None, screen: str, detail: dict
    ) -> bool:
        """Records the dialog on the lane's own activity state.

        The last observed native checkpoint is left untouched: a dialog is
        evidence that the lane stopped, never fresh activity. A record that
        cannot take the lane's checkpoint lock is not marked handled, so the
        next pass publishes it again rather than dropping it.

        Returns:
            Whether the record was written.
        """
        from agent_parley import checkpoints

        label = str(detail.pop("label", dialog.label if dialog else "a dialog"))
        record = {
            "name": dialog.name if dialog else "unknown",
            "label": label,
            "action": dialog.action if dialog else "escalate",
            "screen": report(screen),
            "at": time.time(),
            **detail,
        }
        path = self._directory / f"{self._name}-activity.json"
        with contextlib.suppress(BridgeError, OSError, ValueError):
            with lock(
                self._directory / f"{self._name}-checkpoint.lock", timeout=1
            ):
                state = checkpoints.activity(self._directory, self._name)
                previous = str(state.get("activity", ""))
                if previous.startswith(MARKER):
                    previous = str(
                        (state.get("dialog") or {}).get("previous", "")
                    )
                record["previous"] = previous
                state["activity"] = MARKER + label
                state["dialog"] = record
                write_json(path, state)
            self._parked = True
            self._resolved = True
            return True
        return False

    def _release(self) -> None:
        """Clears a published dialog once the screen no longer shows one.

        A release that cannot take the lane's checkpoint lock leaves the
        dialog recorded as held, so the next pass retries it rather than the
        lane reading as parked after its dialog is gone.
        """
        if not self._parked:
            return
        from agent_parley import checkpoints

        path = self._directory / f"{self._name}-activity.json"
        with contextlib.suppress(BridgeError, OSError, ValueError):
            with lock(
                self._directory / f"{self._name}-checkpoint.lock", timeout=1
            ):
                state = checkpoints.activity(self._directory, self._name)
                if state:
                    if str(state.get("activity", "")).startswith(MARKER):
                        state["activity"] = (
                            str((state.get("dialog") or {}).get("previous", ""))
                            or "idle"
                        )
                    state.pop("dialog", None)
                    write_json(path, state)
            self._parked = False

    def _notify(self, label: str, detail: str) -> None:
        """Sends the operator one message carrying what the screen shows."""
        from agent_parley import notify

        with contextlib.suppress(BridgeError, OSError, ValueError):
            notify.deliver(
                self._directory,
                self._name,
                notify.Event.NATIVE_DIALOG,
                {"dialog": label, "detail": detail},
            )


def watcher(directory: Path, name: str) -> Watch:
    """Builds the watcher for one lane from the operator's configuration.

    Args:
        directory: Private project state directory.
        name: Participant that owns the lane.

    Returns:
        A watcher carrying the answers the operator recorded for that lane. A
        manifest that cannot be read yields a watcher that escalates every
        dialog rather than one that answers by guess.
    """
    from agent_parley import roster

    answers: dict[str, str] = {}
    reply = ""
    with contextlib.suppress(BridgeError, OSError, ValueError, KeyError):
        manifest = roster.read(directory)
        answers = configured(manifest, name)
        reply = standing_reply(manifest, name)
    return Watch(directory, name, answers, deadline=ESCALATE_AFTER, reply=reply)
