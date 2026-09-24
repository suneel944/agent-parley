"""Owns a native terminal and admits bounded coordination wake requests."""

import contextlib
import fcntl
import hashlib
import json
import os
import pty
import re
import select
import signal
import socket
import stat
import struct
import termios
import time
import tty
from pathlib import Path

from agent_parley import dialogs
from agent_parley.state import BridgeError, lock

PROMPT = "Review pending coordination messages and handoff reminders."
MAX_WORK_PROMPT = 2_000
SUBMIT_DELAY = 0.2
ANSWERING = re.compile(rb"[\r\n0-9]")
DETACHED_ROWS = 24
DETACHED_COLUMNS = 80
STRING_SEQUENCES = frozenset({ord("]"), ord("P"), ord("X"), ord("^"), ord("_")})
LOG_LIMIT = 1 << 20
TITLE_LIMIT = 96
TITLE_CARRY = 4096
EXEC_FAILED = 127
DRAIN_READS = 64
STOP_SIGNALS = (signal.SIGTERM, signal.SIGHUP)
TITLE_SAVE = b"\x1b[22;0t"
TITLE_RESTORE = b"\x1b[23;0t"
_TITLE_KINDS = (b"0;", b"2;")

_DETACHED_TERMINAL_QUERIES = (
    (b"\x1b[6n", b"\x1b[1;1R"),
    (b"\x1b]10;?\x07", b"\x1b]10;rgb:ffff/ffff/ffff\x07"),
    (b"\x1b]10;?\x1b\\", b"\x1b]10;rgb:ffff/ffff/ffff\x1b\\"),
    (b"\x1b]11;?\x07", b"\x1b]11;rgb:0000/0000/0000\x07"),
    (b"\x1b]11;?\x1b\\", b"\x1b]11;rgb:0000/0000/0000\x1b\\"),
    (b"\x1b[c", b"\x1b[?1;2c"),
)


def _wake_flags(directory: Path, name: str, home: Path | None) -> dict:
    """Reads lane and project wake gates under the setup lock."""
    with lock(directory / "setup.lock", timeout=1):
        manifest = json.loads((directory / "project.json").read_text())
    participants = manifest.get("participants", {})
    participant = participants.get(name) or {}
    project_wake = (manifest.get("supervision") or {}).get("wake", True)
    global_wake = True
    if home is not None:
        path = home / "supervision.json"
        if path.exists():
            global_wake = json.loads(path.read_text()).get("wake", True)
    return {
        "present": name in participants,
        "enabled": bool(project_wake and global_wake),
        "participant_wake": bool(participant.get("wake", True)),
        "paused": bool(participant.get("paused", False)),
    }


def _work_bindings(ledger: dict, numbers: list[str]) -> list[dict]:
    """Captures fields that must still match at prompt admission."""
    bindings = []
    for number in numbers:
        issue = ledger.get("issues", {}).get(number, {})
        bindings.append(
            {
                "issue": number,
                "owner": issue.get("owner"),
                "claim_id": issue.get("claim_id"),
                "blocked_by": issue.get("blocked_by", []),
                "offer": (issue.get("offer") or {}).get("id"),
                "execution": issue.get("execution"),
            }
        )
    return bindings


def selected_prompt(
    directory: Path, name: str, home: Path | None = None
) -> str | None:
    """Adds current actionable work to a supervisor-requested turn.

    The launcher reads supervisor-owned state after admitting the wake. Work
    context does not cross the control socket, and a malformed or obsolete
    publication refuses delivery. A missing selection is the ordinary
    coordination prompt used by launchers outside the supervisor wake path.

    Args:
        directory: Private project state directory.
        name: Participant owning the terminal socket.
        home: Private bridge state root for the global wake gate.

    Returns:
        Bounded prompt naming current work, the default prompt, or None when
        the selected lane or issue state changed before admission.
    """
    try:
        record = json.loads((directory / f"{name}-wake-work.json").read_text())
    except FileNotFoundError:
        return PROMPT
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or not isinstance(
        record.get("flags"), dict
    ):
        return None
    try:
        flags = _wake_flags(directory, name, home)
    except (BridgeError, OSError, ValueError):
        return None
    if flags != record["flags"] or (
        not flags["present"]
        or not flags["enabled"]
        or not flags["participant_wake"]
        or flags["paused"]
    ):
        return None
    offer = record.get("offer") if isinstance(record, dict) else None
    if offer is None:
        try:
            return (
                PROMPT if _wake_flags(directory, name, home) == flags else None
            )
        except (BridgeError, OSError, ValueError):
            return None
    if not isinstance(offer, dict):
        return None
    try:
        with lock(directory / "issues.lock", timeout=1):
            ledger = json.loads((directory / "issues.json").read_text())
    except (BridgeError, OSError, ValueError):
        return None
    if _work_bindings(ledger, offer.get("issues", [])) != record.get(
        "bindings"
    ):
        return None
    try:
        if _wake_flags(directory, name, home) != flags:
            return None
    except (BridgeError, OSError, ValueError):
        return None
    identifier = str(offer.get("id", ""))
    detail = str(offer.get("text", ""))
    if not identifier or not detail:
        return PROMPT
    issues = ", ".join(f"#{number}" for number in offer.get("issues", [])[:5])
    heading = f"Act on work offer {identifier}"
    if issues:
        heading += f" for {issues}"
    return (f"{heading}. {detail} Delivery does not claim or complete work.")[
        :MAX_WORK_PROMPT
    ]


def socket_path(directory: Path, name: str) -> Path:
    """Keeps control socket names short while retaining private state scope."""
    if directory.parent.name == "projects":
        digest = hashlib.sha256(str(directory / name).encode()).hexdigest()[:20]
        return directory.parent.parent / f"wake-{digest}.sock"
    return directory / f"{name}-wake.sock"


def request(directory: Path, name: str) -> str:
    """Asks the owning launcher to start a turn without typing into a peer.

    Args:
        directory: Private project state directory.
        name: Participant owning the terminal socket.

    Returns:
        Accepted, a reasoned busy refusal, manual attention, or unavailable.
        No user content crosses the socket.
    """
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(1)
        try:
            client.connect(str(socket_path(directory, name)))
            client.sendall(b"wake\n")
            return client.recv(32).decode()
        except OSError:
            return "unavailable"


def operator_input(entered: bytes, control: bytes = b"") -> tuple[bytes, bytes]:
    """Separates operator bytes from complete terminal control sequences.

    A control sequence may be split across terminal reads, while the final read
    may also contain operator text. Recognized CSI and SS3 sequences are
    removed, as are the string sequences a terminal answers with: OSC, DCS, SOS,
    PM and APC, each ended by a string terminator or a bell. A terminal that
    reports its version or its capabilities answers on the operator's input
    descriptor, and an unrecognized reply would read as a partially typed line
    that no keystroke of the lane's own can clear. An unknown escape prefix
    remains operator input so a wake cannot overwrite text the detector did not
    understand.

    Args:
        entered: Newly read terminal bytes.
        control: Incomplete recognized control prefix from the previous read.

    Returns:
        Operator bytes and an incomplete recognized control suffix to carry to
        the next read.
    """
    data = control + entered
    operator = bytearray()
    position = 0
    while position < len(data):
        if data[position] != 0x1B:
            operator.append(data[position])
            position += 1
            continue
        if position + 1 == len(data):
            return bytes(operator), data[position:]
        kind = data[position + 1]
        if kind == ord("["):
            end = position + 2
            while end < len(data) and not 0x40 <= data[end] <= 0x7E:
                end += 1
            if end == len(data):
                return bytes(operator), data[position:]
            position = end + 1
            continue
        if kind == ord("O"):
            if position + 2 == len(data):
                return bytes(operator), data[position:]
            if 0x40 <= data[position + 2] <= 0x7E:
                position += 3
                continue
        if kind in STRING_SEQUENCES:
            end = position + 2
            while end < len(data):
                if data[end] == 0x07:
                    position = end + 1
                    break
                if data[end : end + 2] == b"\x1b\\":
                    position = end + 2
                    break
                end += 1
            else:
                return bytes(operator), data[position:]
            continue
        operator.append(data[position])
        position += 1
    return bytes(operator), b""


def pending(entered: bytes, previous: bool) -> bool:
    """Decides whether the operator holds a partially entered line.

    Terminal control traffic shares the operator's input descriptor. Complete
    recognized control sequences are removed, but bytes after them and unknown
    escape-prefixed input are still inspected. Line submission, interruption
    and line clearing reset the state in byte order, so later text in the same
    read can establish a new pending line.

    Args:
        entered: Bytes read from the operator's terminal in one call.
        previous: Pending state before this read.

    Returns:
        True while the operator has typed text without submitting it.
    """
    entered, control = operator_input(entered)
    if control:
        return True
    result = previous
    for value in entered:
        if value in (0x03, 0x0A, 0x0D, 0x15):
            result = False
        else:
            result = True
    return result


def detached_terminal_replies(
    output: bytes, control: bytes = b""
) -> tuple[bytes, bytes]:
    """Answers terminal probes emitted into an unattended pseudo-terminal.

    Detached native clients still own a real pseudo-terminal, but no terminal
    emulator sits beyond its master descriptor. This supplies the small set of
    standard replies needed during native startup. A primary device-attribute
    reply deliberately follows the unanswered keyboard-protocol query, which
    reports an ordinary VT100 terminal rather than claiming keyboard features
    this transport does not implement.

    Args:
        output: Newly read native terminal output.
        control: A possible query prefix split from the previous read.

    Returns:
        Replies to write to the pseudo-terminal and an incomplete query suffix.
    """
    data = control + output
    replies = bytearray()
    position = 0
    while position < len(data):
        match = next(
            (
                (query, response)
                for query, response in _DETACHED_TERMINAL_QUERIES
                if data.startswith(query, position)
            ),
            None,
        )
        if match is None:
            position += 1
            continue
        query, response = match
        replies.extend(response)
        position += len(query)
    maximum = min(
        len(data), max(len(query) for query, _ in _DETACHED_TERMINAL_QUERIES)
    )
    pending_bytes = b""
    for length in range(maximum, 0, -1):
        suffix = data[-length:]
        if any(
            length < len(query) and query.startswith(suffix)
            for query, _ in _DETACHED_TERMINAL_QUERIES
        ):
            pending_bytes = suffix
            break
    return bytes(replies), pending_bytes


def _issue_order(number: str) -> tuple[int, str]:
    """Sorts numeric issue keys numerically and any other key after them."""
    return (int(number), "") if number.isdigit() else (1 << 62, number)


def lane_title(
    name: str, activity: dict | None, ledger: dict | None, holding: bool
) -> str:
    """Names a lane, its state and its claim progress for a terminal tab.

    The lane name leads so tabs read and sort by lane, and every state is a
    word rather than a glyph so the title stays legible without colour, emoji
    fonts or a screen reader's symbol table. An idle lane that still owns an
    open claim reads ``idle with claim``, because that is the lane an operator
    most needs to find. Progress counts the lane's open claims and the issues
    it completed, from the same ledger the coordination tools read.

    Args:
        name: Participant owning the terminal.
        activity: Published lane activity, or None when unreadable.
        ledger: Issue ledger, or None when unreadable.
        holding: Whether a native dialog holds the lane's screen.

    Returns:
        One plain-text title line.
    """
    issues = (ledger or {}).get("issues", {})
    if not isinstance(issues, dict):
        issues = {}
    records = [
        (number, issue)
        for number, issue in issues.items()
        if isinstance(issue, dict)
    ]
    claimed = sorted(
        (number for number, issue in records if issue.get("owner") == name),
        key=_issue_order,
    )
    done = sum(1 for _, issue in records if issue.get("completed_by") == name)
    state = str((activity or {}).get("activity", ""))
    if holding or state.startswith(dialogs.MARKER):
        word = "blocked: dialog"
    elif state.startswith(dialogs.APPROVAL):
        word = "blocked: approval"
    elif state == "idle":
        word = "idle with claim" if claimed else "idle"
    elif state in ("", "stopped"):
        word = state or "unknown"
    elif state.startswith(("starting", "not started")):
        word = state.split(";", 1)[0]
    else:
        word = "working"
    parts = [f"[{name}] {word}"]
    if claimed:
        parts.append(f"#{claimed[0]}")
    total = len(claimed) + done
    parts.append(f"- {done}/{total} done" if total else "- 0 open")
    return " ".join(parts)


def compose_title(lane_label: str, child: str) -> str:
    """Joins the lane title and the client's own title within the limit.

    The client's title is kept as a suffix and is the part truncated first,
    so a narrow limit still shows the lane and its state.

    Args:
        lane_label: Title from `lane_title`.
        child: Last title the native client set, possibly empty.

    Returns:
        Printable title text of at most `TITLE_LIMIT` characters.
    """
    text = lane_label
    child = "".join(char for char in child if char.isprintable()).strip()
    if child:
        text += " | " + child
    return text[:TITLE_LIMIT]


def title_sequence(title: str) -> bytes:
    """Encodes a window and tab title for the operator's terminal."""
    return b"\x1b]0;" + title.encode(errors="replace") + b"\x07"


def retitle(
    output: bytes, control: bytes, lane_label: str
) -> tuple[bytes, bytes, str | None]:
    """Replaces titles the native client sets with the lane-first title.

    Only OSC 0 and OSC 2 set a title; every other byte passes through in
    order. A title sequence split across reads is carried to the next read,
    and an unterminated sequence longer than `TITLE_CARRY` passes through
    unchanged rather than holding the client's output.

    Args:
        output: Newly read native terminal output.
        control: Incomplete title sequence carried from the previous read.
        lane_label: Current lane title from `lane_title`.

    Returns:
        Bytes to forward, an incomplete sequence to carry, and the last
        title the client set in this read, or None when it set none.
    """
    data = control + output
    result = bytearray()
    child = None
    position = 0
    while True:
        start = data.find(b"\x1b]", position)
        if start < 0:
            tail = data[position:]
            if tail.endswith(b"\x1b"):
                return bytes(result + tail[:-1]), b"\x1b", child
            return bytes(result + tail), b"", child
        result += data[position:start]
        kind = data[start + 2 : start + 4]
        if len(kind) < 2 and any(
            prefix.startswith(kind) for prefix in _TITLE_KINDS
        ):
            return bytes(result), data[start:], child
        if kind not in _TITLE_KINDS:
            result += b"\x1b]"
            position = start + 2
            continue
        bell = data.find(b"\x07", start + 4)
        string = data.find(b"\x1b\\", start + 4)
        ends = [
            (end, size) for end, size in ((bell, 1), (string, 2)) if end >= 0
        ]
        if not ends:
            if len(data) - start > TITLE_CARRY:
                return bytes(result + data[start:]), b"", child
            return bytes(result), data[start:], child
        end, size = min(ends)
        child = data[start + 4 : end].decode(errors="replace")
        result += title_sequence(compose_title(lane_label, child))
        position = end + size


def _read_json(path: Path) -> dict | None:
    """Reads a private state record, or None when it is missing or partial."""
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def lane_summary(start: Path) -> str:
    """Summarizes the lane containing a directory for a native status line.

    A lane worktree sits directly under its project's private state
    directory, named for its participant, so the nearest ancestor whose
    parent holds a manifest naming it is the lane. The summary is the same
    line `compose_title` gives the tab, without a client title.

    Args:
        start: Working directory of the native client.

    Returns:
        The lane summary, or an empty string outside a lane.
    """
    for lane in (start, *start.parents):
        manifest = _read_json(lane.parent / "project.json")
        if manifest is None:
            continue
        if lane.name not in (manifest.get("participants") or {}):
            return ""
        return compose_title(
            lane_title(
                lane.name,
                _read_json(lane.parent / f"{lane.name}-activity.json"),
                _read_json(lane.parent / "issues.json"),
                False,
            ),
            "",
        )
    return ""


def _changed_at(path: Path) -> int:
    """Returns a record's modification time, or zero when it is missing."""
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def _relay(data: bytes, bounded: bool) -> None:
    """Forwards native output to the launcher's standard output.

    A resumed lane's standard output is its private wake log. That stream
    carries every spinner and redraw frame, so a bounded log is truncated
    when the next write would take it past `LOG_LIMIT`. A write failure,
    including a full disk, is dropped rather than raised: output is a
    record of the session, and losing it must not end the session.

    Args:
        data: Bytes to forward.
        bounded: Whether standard output is a regular file to keep bounded.
    """
    with contextlib.suppress(OSError):
        if bounded:
            data = data[-LOG_LIMIT:]
            if os.fstat(1).st_size + len(data) > LOG_LIMIT:
                os.ftruncate(1, 0)
                os.lseek(1, 0, os.SEEK_SET)
        os.write(1, data)


def run(
    command: list[str],
    lane: Path,
    env: dict[str, str],
    name: str,
    *,
    attached: bool = True,
    inactive_after: float = 300,
    home: Path | None = None,
    titles: bool = True,
) -> int:
    """Runs the native CLI with its own controlling terminal and permissions.

    Wakes are admitted only at a native idle checkpoint with no partially
    entered operator input. Permission prompts, active turns and unknown
    states cannot receive injected text. The launcher holds the session lock
    outside this function and owns the private control socket throughout.

    An admitted prompt is written first and its carriage return follows as a
    separate delivery a short moment later. A client that reads the text and
    the return together treats the burst as pasted input and leaves the prompt
    unsent in its composer.

    The same output the launcher forwards is read back by the dialog watcher,
    which publishes a blocking native screen on the lane's activity state and
    refuses wakes while one holds. It answers only a dialog the operator named
    for this lane, only with an option the client itself is offering, and never
    while the operator holds a partially entered line. An operator's Enter or
    option digit while a dialog holds is an answer, so the watcher releases
    that dialog on the client's next output rather than once it scrolls away.

    A lane the client itself reported as waiting for approval refuses the wake
    as busy and names that prompt, because injecting a turn cannot answer it and
    counting the refusal as a failed wake would escalate a lane that is simply
    holding a question for its operator. A wake request always receives an
    explicit reply: ``unknown`` when the lane's activity cannot be read.

    The session ends when the client exits, even while a background process
    it started still holds the pseudo-terminal. ``SIGTERM`` and ``SIGHUP``
    end it through the same cleanup as a normal exit, restoring the terminal
    and removing the control socket, and the signal is then forwarded to the
    client. A client that cannot be started exits the forked child with
    status 127 and never unwinds the launcher's own stack.

    An attached launcher owns the tab title: the lane name, its state and
    its claim progress lead, and the client's own title follows as a suffix.
    The title is refreshed when the lane's activity record, the issue ledger
    or a held dialog changes, and the operator's previous title is restored
    on exit. A detached launcher's output is its wake log, kept under
    `LOG_LIMIT`, and a failed write to it never ends the session.

    Args:
        command: Native argument vector, without a shell.
        lane: Assigned participant worktree.
        env: Existing native authentication and launch environment.
        name: Validated participant name.
        attached: Whether to forward the operator's terminal input.
        inactive_after: Seconds without a new checkpoint before one retry.
        home: Private bridge state root for admission-time wake validation.
        titles: Whether an attached launcher sets the lane tab title.

    Returns:
        Native process exit status.
    """
    saved = termios.tcgetattr(0) if attached else None
    path = socket_path(lane.parent, name)
    activity = lane.parent / f"{name}-activity.json"
    ledger = lane.parent / "issues.json"
    titling = attached and titles
    bounded = False
    if not attached:
        with contextlib.suppress(OSError):
            bounded = stat.S_ISREG(os.fstat(1).st_mode)
    with socket.socket(socket.AF_UNIX) as listener:
        path.unlink(missing_ok=True)
        listener.bind(str(path))
        path.chmod(0o600)
        listener.listen(1)
        pid, master = pty.fork()
        if pid == 0:
            try:
                if not attached:
                    size = struct.pack(
                        "HHHH", DETACHED_ROWS, DETACHED_COLUMNS, 0, 0
                    )
                    with contextlib.suppress(OSError):
                        fcntl.ioctl(0, termios.TIOCSWINSZ, size)
                os.chdir(lane)
                os.execvpe(command[0], command, env)
            except OSError as error:
                with contextlib.suppress(OSError):
                    os.write(2, f"{command[0]}: {error}\n".encode())
            finally:
                os._exit(EXEC_FAILED)
        previous_stops = {
            number: signal.getsignal(number) for number in STOP_SIGNALS
        }
        stopping: list[int] = []

        def stop(signum: int, frame: object = None) -> None:
            """Records a termination request for the loop to honour."""
            stopping.append(signum)

        for number in STOP_SIGNALS:
            signal.signal(number, stop)
        try:
            status = _session(
                pid,
                master,
                listener,
                stopping,
                name=name,
                lane=lane,
                activity=activity,
                ledger=ledger,
                attached=attached,
                saved=saved,
                titling=titling,
                bounded=bounded,
                inactive_after=inactive_after,
                home=home,
            )
            path.unlink(missing_ok=True)
            if status is None:
                if stopping:
                    with contextlib.suppress(ProcessLookupError):
                        os.kill(pid, stopping[0])
                _, status = os.waitpid(pid, 0)
        finally:
            path.unlink(missing_ok=True)
            for number, handler in previous_stops.items():
                signal.signal(number, handler)
        return os.waitstatus_to_exitcode(status)


def _session(
    pid: int,
    master: int,
    listener: socket.socket,
    stopping: list[int],
    *,
    name: str,
    lane: Path,
    activity: Path,
    ledger: Path,
    attached: bool,
    saved: list | None,
    titling: bool,
    bounded: bool,
    inactive_after: float,
    home: Path | None,
) -> int | None:
    """Relays one native session until the client exits or is stopped.

    Args:
        pid: Native client process.
        master: Pseudo-terminal master descriptor, closed on return.
        listener: Bound wake control socket.
        stopping: Termination signals received so far.
        name: Validated participant name.
        lane: Assigned participant worktree.
        activity: Lane activity record.
        ledger: Project issue ledger.
        attached: Whether to forward the operator's terminal input.
        saved: Operator terminal attributes to restore, when attached.
        titling: Whether to own the operator's tab title.
        bounded: Whether standard output is a wake log to keep bounded.
        inactive_after: Seconds without a new checkpoint before one retry.
        home: Private bridge state root for admission-time wake validation.

    Returns:
        The client's wait status when it was reaped here, else None.
    """
    previous_handler = signal.getsignal(signal.SIGWINCH)

    def resize(signum: int = 0, frame: object = None) -> None:
        """Copies terminal dimensions to the native child."""
        with contextlib.suppress(OSError):
            size = fcntl.ioctl(0, termios.TIOCGWINSZ, b"\0" * 8)
            fcntl.ioctl(master, termios.TIOCSWINSZ, size)

    signal.signal(signal.SIGWINCH, resize)
    if attached:
        resize()
    pending_input = False
    pending_control = b""
    detached_control = b""
    title_control = b""
    child_title = ""
    lane_label = ""
    title_state: tuple | None = None
    wake_checkpoint = None
    wake_checkpoint_at = 0.0
    wake_retried = False
    submit_at = 0.0
    status = None
    watch = dialogs.watcher(lane.parent, name)

    def forward(output: bytes) -> None:
        """Relays native output, replacing its titles when titling."""
        nonlocal title_control, child_title
        if titling:
            output, title_control, title = retitle(
                output, title_control, lane_label
            )
            if title is not None:
                child_title = title
        _relay(output, bounded)

    try:
        if attached:
            tty.setraw(0)
        if titling:
            _relay(TITLE_SAVE, False)
        while not stopping:
            if titling:
                observed = (
                    _changed_at(activity),
                    _changed_at(ledger),
                    watch.holding,
                )
                if observed != title_state:
                    title_state = observed
                    label = lane_title(
                        name,
                        _read_json(activity),
                        _read_json(ledger),
                        watch.holding,
                    )
                    if label != lane_label:
                        lane_label = label
                        _relay(
                            title_sequence(compose_title(label, child_title)),
                            False,
                        )
            screen = b""
            descriptors = [master, listener.fileno()]
            if attached:
                descriptors.append(0)
            waiting = 1.0
            if submit_at:
                waiting = max(0.0, submit_at - time.monotonic())
            ready, _, _ = select.select(descriptors, [], [], waiting)
            if submit_at and time.monotonic() >= submit_at:
                submit_at = 0.0
                with contextlib.suppress(OSError):
                    os.write(master, b"\r")
            if 0 in ready:
                entered = os.read(0, 4096)
                if not entered:
                    break
                operator, pending_control = operator_input(
                    entered, pending_control
                )
                pending_input = pending(operator, pending_input)
                os.write(master, entered)
                if watch.holding and ANSWERING.search(entered):
                    watch.answered()
            if master in ready:
                try:
                    output = os.read(master, 65536)
                except OSError:
                    break
                if not output:
                    break
                forward(output)
                screen = output
                if not attached:
                    replies, detached_control = detached_terminal_replies(
                        output, detached_control
                    )
                    if replies:
                        os.write(master, replies)
            answer = watch.advance(
                screen,
                time.monotonic(),
                bool(pending_input or pending_control),
            )
            if answer:
                with contextlib.suppress(OSError):
                    os.write(master, answer)
            if listener.fileno() in ready:
                connection, _ = listener.accept()
                with connection:
                    connection.settimeout(1)
                    result = "unknown"
                    with contextlib.suppress(OSError, ValueError):
                        requested = connection.recv(32)
                        state = _read_json(activity) or {}
                        checkpoint = state.get("updated")
                        accepted = False
                        if requested != b"wake\n":
                            result = "unavailable"
                        elif watch.holding:
                            result = "manual attention required"
                        elif not state:
                            result = "unknown"
                        elif str(state.get("activity", "")).startswith(
                            dialogs.APPROVAL
                        ):
                            result = "busy:approval"
                        elif state.get("activity") != "idle":
                            result = "busy:turn"
                        elif pending_input or pending_control:
                            result = "busy:input"
                        elif checkpoint != wake_checkpoint:
                            accepted = True
                            wake_retried = False
                        elif (
                            time.monotonic() - wake_checkpoint_at
                            < inactive_after
                        ):
                            result = "busy:repeat"
                        elif not wake_retried:
                            accepted = True
                            wake_retried = True
                        else:
                            result = "manual attention required"
                        if accepted:
                            prompt = selected_prompt(lane.parent, name, home)
                            if prompt is None:
                                result = "busy:stale"
                            else:
                                result = "unavailable"
                                os.write(master, prompt.encode())
                                submit_at = time.monotonic() + SUBMIT_DELAY
                                wake_checkpoint = checkpoint
                                wake_checkpoint_at = time.monotonic()
                                result = "accepted"
                    with contextlib.suppress(OSError):
                        connection.sendall(result.encode())
            reaped, code = os.waitpid(pid, os.WNOHANG)
            if reaped:
                status = code
                for _ in range(DRAIN_READS):
                    if not select.select([master], [], [], 0)[0]:
                        break
                    try:
                        output = os.read(master, 65536)
                    except OSError:
                        break
                    if not output:
                        break
                    forward(output)
                break
    finally:
        if titling:
            _relay(TITLE_RESTORE, False)
        if saved is not None:
            with contextlib.suppress(OSError, termios.error):
                termios.tcsetattr(0, termios.TCSADRAIN, saved)
        signal.signal(signal.SIGWINCH, previous_handler)
        os.close(master)
    return status
