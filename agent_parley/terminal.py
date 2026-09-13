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
        Accepted, busy, or unavailable. No user content crosses the socket.
    """
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(1)
        try:
            client.connect(str(socket_path(directory, name)))
            client.sendall(b"wake\n")
            return client.recv(32).decode()
        except OSError:
            return "unavailable"


def run(
    command: list[str],
    lane: Path,
    env: dict[str, str],
    name: str,
    *,
    attached: bool = True,
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
                    pending_input = not entered.endswith(
                        (b"\r", b"\n", b"\x03")
                    )
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
                            accepted = (
                                requested == b"wake\n"
                                and state.get("activity") == "idle"
                                and not pending_input
                                and state.get("updated") != wake_checkpoint
                            )
                            if accepted:
                                os.write(master, (PROMPT + "\r").encode())
                                wake_checkpoint = state.get("updated")
                            connection.sendall(
                                b"accepted" if accepted else b"busy"
                            )
        finally:
            if saved is not None:
                termios.tcsetattr(0, termios.TCSADRAIN, saved)
            signal.signal(signal.SIGWINCH, previous_handler)
            os.close(master)
            path.unlink(missing_ok=True)
        _, status = os.waitpid(pid, 0)
        return os.waitstatus_to_exitcode(status)
