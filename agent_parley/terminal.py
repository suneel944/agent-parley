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

PROMPT = "Review pending coordination messages and handoff reminders."


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


def pending(entered: bytes, previous: bool) -> bool:
    """Decides whether the operator holds a partially entered line.

    Terminal control traffic shares the operator's input descriptor: cursor
    position reports answering the native client's own query, focus events,
    arrow keys and a bare Escape all arrive as sequences that begin with ESC
    and never end in a line terminator. They are not operator text, so they
    leave the pending state as it was rather than marking the line as held
    until the next Enter, which would refuse every wake in between.

    Args:
        entered: Bytes read from the operator's terminal in one call.
        previous: Pending state before this read.

    Returns:
        True while the operator has typed text without submitting it.
    """
    if entered.startswith(b"\x1b"):
        return previous
    return not entered.endswith((b"\r", b"\n", b"\x03"))


def run(
    command: list[str],
    lane: Path,
    env: dict[str, str],
    name: str,
    *,
    attached: bool = True,
    inactive_after: float = 300,
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
                    pending_input = pending(entered, pending_input)
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
                            elif pending_input:
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
                                os.write(master, (PROMPT + "\r").encode())
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
