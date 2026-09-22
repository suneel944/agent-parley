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
continuously and neither emits line breaks between redraws.

Publication reuses the lane surfaces a reader already has: the activity file
carries the dialog under `dialog` and names it in `activity`, provider capacity
is recorded through the supervisor's durable observation, and the operator gets
one notification per situation. Nothing here observes liveness or moves
ownership.
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

from agent_parley.state import BridgeError, lock, write_json

SCREEN_BYTES = 8192
SIGNATURE_CHARS = 200
REPORT_CHARS = 1200
REPORT_LINES = 4
REPORT_WIDTH = 100
OPTION_LABEL = 60
ESCALATE_AFTER = 30.0
REPEAT_LIMIT = 2
MARKER = "dialog: "
EXHAUSTED = "exhausted"
ANSWER = "answer"

ESCAPES = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"
    r"|\x1b[P^_X][^\x1b]*(?:\x1b\\)?"
    r"|\x1b\[[0-?]*[ -/]*[@-~]"
    r"|\x1b[@-Z\\-_]"
)
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
OPTION = re.compile(r"(?:›|❯|>)?\s*(\d)\.\s+")
LABEL_END = re.compile(r"·|Esc to |Press enter|esc to ")
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
        pattern: Recognizer applied to the flattened screen.
        action: Either an exhausted provider capacity or an offered answer.
    """

    name: str
    label: str
    pattern: re.Pattern[str]
    action: str


DIALOGS: tuple[Dialog, ...] = (
    Dialog(
        "usage-limit",
        "provider usage limit",
        re.compile(
            r"hit your [\w-]+ limit|usage limit reached|"
            r"weekly limit\b.{0,40}\bresets",
            re.IGNORECASE,
        ),
        EXHAUSTED,
    ),
    Dialog(
        "hook-review",
        "native hook trust review",
        re.compile(r"hooks need review", re.IGNORECASE),
        ANSWER,
    ),
    Dialog(
        "tool-permission",
        "native tool permission prompt",
        re.compile(r"do you want to proceed\?", re.IGNORECASE),
        ANSWER,
    ),
)

NAMES = frozenset(item.name for item in DIALOGS)


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


def match(screen: str) -> Dialog | None:
    """Returns the recorded dialog a flattened screen shows.

    Args:
        screen: Flattened screen text.

    Returns:
        The first policy entry whose recognizer matches, or None.
    """
    return next((item for item in DIALOGS if item.pattern.search(screen)), None)


def prompted(screen: str) -> bool:
    """Reports whether a flattened screen holds an answer-shaped prompt.

    Args:
        screen: Flattened screen text.

    Returns:
        True when the screen carries a marker a client only draws while it is
        waiting for a keypress.
    """
    return bool(PROMPT_SHAPE.search(screen))


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
    ) -> None:
        """Prepares a watcher for one lane's pseudo-terminal.

        Args:
            directory: Private project state directory.
            name: Participant that owns the lane.
            answers: Dialog name to configured option text.
            deadline: Seconds an unrecognized prompt may hold an unchanged
                screen before it escalates.
        """
        self._directory = directory
        self._name = name
        self._answers = dict(answers or {})
        self._deadline = deadline
        self._tail = b""
        self._signature = ""
        self._since = 0.0
        self._resolved = False
        self._parked = False
        self._repeats: dict[str, int] = {}

    @property
    def holding(self) -> bool:
        """Reports whether a dialog is published as holding the screen."""
        return self._parked

    def advance(self, output: bytes, now: float, held: bool = False) -> bytes:
        """Observes new terminal output and decides one action.

        A screen is acted on only once it has stopped changing, so a dialog
        drawn across several reads is answered as one screen rather than half
        of one.

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
            self._tail = (self._tail + output)[-SCREEN_BYTES:]
        if not self._tail:
            return b""
        screen = flatten(self._tail)
        dialog = match(screen)
        signature = self._describe(screen, dialog)
        if signature != self._signature:
            self._signature = signature
            self._since = now
            self._resolved = False
            if not signature:
                self._release()
            return b""
        if not signature or self._resolved:
            return b""
        return self._act(screen, dialog, now, held)

    def _describe(self, screen: str, dialog: Dialog | None) -> str:
        """Names the screen so a redraw of it compares equal to itself."""
        if dialog is not None:
            return "\x00".join((dialog.name, *options(screen).values()))
        if prompted(screen):
            return "prompt\x00" + screen[-SIGNATURE_CHARS:]
        return ""

    def _act(
        self, screen: str, dialog: Dialog | None, now: float, held: bool
    ) -> bytes:
        """Applies the policy for a screen that has stopped changing."""
        if dialog is None:
            if now - self._since < self._deadline:
                return b""
            self._escalate(None, "an unrecognized native prompt", screen)
            return b""
        if dialog.action == EXHAUSTED:
            self._exhaust(dialog, screen)
            return b""
        answer = self._answers.get(dialog.name, "")
        pressed = keys(screen, answer) if answer else b""
        repeats = self._repeats.get(self._signature, 0)
        if not pressed or repeats >= REPEAT_LIMIT:
            self._escalate(dialog, dialog.label, screen)
            return b""
        if held:
            return b""
        self._repeats = {self._signature: repeats + 1}
        self._publish(
            dialog,
            screen,
            {"answer": answer, "keys": pressed.decode().strip()},
        )
        return pressed

    def _exhaust(self, dialog: Dialog, screen: str) -> None:
        """Parks the lane and records the provider capacity it reported."""
        observed = time.time()
        reset = reset_at(screen, observed)
        self._publish(dialog, screen, {"reset_at": reset})
        from agent_parley import supervision

        with contextlib.suppress(BridgeError, OSError, ValueError):
            supervision.record_capacity(
                self._directory,
                self._name,
                {
                    "state": "exhausted",
                    "observed_at": observed,
                    "reset_at": reset,
                    "source": "native-dialog",
                    "observation_id": self._evidence(dialog.name, reset),
                },
            )
        self._notify(dialog.label, screen)

    def _escalate(self, dialog: Dialog | None, label: str, screen: str) -> None:
        """Parks the lane on a screen no configured answer covers."""
        self._publish(dialog, screen, {"label": label, "escalated": True})
        self._notify(label, screen)

    def _evidence(self, name: str, reset: float | None) -> str:
        """Identifies one screen situation for a durable observation."""
        digest = hashlib.sha256(
            f"{self._name}\x00{name}\x00{reset}".encode()
        ).hexdigest()[:16]
        return f"dialog:{digest}"

    def _publish(
        self, dialog: Dialog | None, screen: str, detail: dict
    ) -> None:
        """Records the dialog on the lane's own activity state.

        The last observed native checkpoint is left untouched: a dialog is
        evidence that the lane stopped, never fresh activity.
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

    def _release(self) -> None:
        """Clears a published dialog once the screen no longer shows one."""
        if not self._parked:
            return
        self._parked = False
        from agent_parley import checkpoints

        path = self._directory / f"{self._name}-activity.json"
        with contextlib.suppress(BridgeError, OSError, ValueError):
            with lock(
                self._directory / f"{self._name}-checkpoint.lock", timeout=1
            ):
                state = checkpoints.activity(self._directory, self._name)
                if not state:
                    return
                if str(state.get("activity", "")).startswith(MARKER):
                    state["activity"] = (
                        str((state.get("dialog") or {}).get("previous", ""))
                        or "idle"
                    )
                state.pop("dialog", None)
                write_json(path, state)

    def _notify(self, label: str, screen: str) -> None:
        """Sends the operator one message carrying the screen text."""
        from agent_parley import notify

        with contextlib.suppress(BridgeError, OSError, ValueError):
            notify.deliver(
                self._directory,
                self._name,
                notify.Event.NATIVE_DIALOG,
                {"dialog": label, "detail": " ".join(report(screen))},
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
    with contextlib.suppress(BridgeError, OSError, ValueError, KeyError):
        answers = configured(roster.read(directory), name)
    return Watch(directory, name, answers, deadline=ESCALATE_AFTER)
