"""Identifies and stops Linux server processes without PID-reuse races."""

import os
import select
import signal
from dataclasses import dataclass
from pathlib import Path

from agent_parley.state import BridgeError


def start_ticks(pid: int) -> str:
    """Reads Linux process creation ticks, independent of wall-clock changes."""
    return Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()[19]


def alive(pid: int | None, ticks: str | None) -> bool:
    """Reports whether a recorded process is still that same live process.

    Args:
        pid: Process ID recorded when the process started, if any.
        ticks: Creation ticks recorded beside that process ID.

    Returns:
        Whether the process runs and was created at the recorded time. A
        recycled process ID reports false, and so does a missing record.
    """
    try:
        return pid is not None and start_ticks(pid) == ticks
    except (OSError, IndexError, ValueError, TypeError):
        return False


@dataclass(frozen=True)
class ServerProcess:
    """Holds a verified process ID and kernel creation identity."""

    pid: int
    ticks: str

    def stop(self) -> None:
        """Pins the process with pidfd before signaling and waiting for exit."""
        try:
            fd = os.pidfd_open(self.pid)
        except ProcessLookupError:
            return
        try:
            if start_ticks(self.pid) != self.ticks:
                raise BridgeError("Server PID changed; refusing to signal it.")
            signal.pidfd_send_signal(fd, signal.SIGTERM)
            if not select.select([fd], [], [], 10)[0]:
                raise BridgeError("Server did not stop within 10s.")
            try:
                os.waitpid(self.pid, os.WNOHANG)
            except ChildProcessError:
                pass
        finally:
            os.close(fd)


def identify(record: dict, home: Path) -> ServerProcess | None:
    """Matches creation ticks, module, and home before accepting a PID."""
    try:
        pid = record["pid"]
        ticks = start_ticks(pid)
        command = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\x00")
        expected = [
            b"-m",
            b"agent_parley.server",
            b"--home",
            str(home).encode(),
        ]
        if command[1:5] == expected and ticks == record.get("start_ticks"):
            return ServerProcess(pid, ticks)
    except (OSError, KeyError, IndexError):
        pass
    return None
