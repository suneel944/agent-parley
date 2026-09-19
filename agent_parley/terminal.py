"""Owns a native terminal and admits bounded coordination wake requests."""

import contextlib
import fcntl
import hashlib
import json
import os
import pty
import select
import signal
import socket
import termios
import time
import tty
from pathlib import Path

from agent_parley.state import BridgeError, lock

PROMPT = "Review pending coordination messages and handoff reminders."
MAX_WORK_PROMPT = 2_000


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
    may also contain operator text. Only recognized CSI, SS3 and OSC sequences
    are removed. An unknown escape prefix remains operator input so a wake
    cannot overwrite text the detector did not understand.

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
        if kind == ord("]"):
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


def run(
    command: list[str],
    lane: Path,
    env: dict[str, str],
    name: str,
    *,
    attached: bool = True,
    inactive_after: float = 300,
    home: Path | None = None,
) -> int:
    """Runs the native CLI with its own controlling terminal and permissions.

    Wakes are admitted only at a native idle checkpoint with no partially
    entered operator input. Permission prompts, active turns and unknown
    states cannot receive injected text. The launcher holds the session lock
    outside this function and owns the private control socket throughout.

    Args:
        command: Native argument vector, without a shell.
        lane: Assigned participant worktree.
        env: Existing native authentication and launch environment.
        name: Validated participant name.
        attached: Whether to forward the operator's terminal input.
        inactive_after: Seconds without a new checkpoint before one retry.
        home: Private bridge state root for admission-time wake validation.

    Returns:
        Native process exit status.
    """
    saved = termios.tcgetattr(0) if attached else None
    path = socket_path(lane.parent, name)
    activity = lane.parent / f"{name}-activity.json"
    with socket.socket(socket.AF_UNIX) as listener:
        path.unlink(missing_ok=True)
        listener.bind(str(path))
        path.chmod(0o600)
        listener.listen(1)
        pid, master = pty.fork()
        if pid == 0:
            os.chdir(lane)
            os.execvpe(command[0], command, env)
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
        wake_checkpoint = None
        wake_checkpoint_at = 0.0
        wake_retried = False
        try:
            if attached:
                tty.setraw(0)
            while True:
                descriptors = [master, listener.fileno()]
                if attached:
                    descriptors.append(0)
                ready, _, _ = select.select(descriptors, [], [], 1)
                if 0 in ready:
                    entered = os.read(0, 4096)
                    if not entered:
                        break
                    operator, pending_control = operator_input(
                        entered, pending_control
                    )
                    pending_input = pending(operator, pending_input)
                    os.write(master, entered)
                if master in ready:
                    try:
                        output = os.read(master, 65536)
                    except OSError:
                        break
                    if not output:
                        break
                    os.write(1, output)
                if listener.fileno() in ready:
                    connection, _ = listener.accept()
                    with connection:
                        connection.settimeout(1)
                        with contextlib.suppress(OSError, ValueError):
                            requested = connection.recv(32)
                            state = json.loads(activity.read_text())
                            checkpoint = state.get("updated")
                            accepted = False
                            result = "busy:turn"
                            if requested != b"wake\n":
                                result = "unavailable"
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
                                prompt = selected_prompt(
                                    lane.parent, name, home
                                )
                                if prompt is None:
                                    accepted = False
                                    result = "busy:stale"
                                else:
                                    os.write(master, (prompt + "\r").encode())
                                    wake_checkpoint = checkpoint
                                    wake_checkpoint_at = time.monotonic()
                                    result = "accepted"
                            connection.sendall(result.encode())
        finally:
            if saved is not None:
                termios.tcsetattr(0, termios.TCSADRAIN, saved)
            signal.signal(signal.SIGWINCH, previous_handler)
            os.close(master)
            path.unlink(missing_ok=True)
        _, status = os.waitpid(pid, 0)
        return os.waitstatus_to_exitcode(status)
