"""Identifies and stops server processes without PID-reuse races.

Linux and macOS expose different process primitives, so one bundle of
them is selected once at import and every caller reaches the operating
system through that bundle instead of testing the platform itself.
Linux keeps the ``/proc`` and pidfd path it has always used; macOS reads
``ps`` for identity and signals through :func:`os.kill`.

Neither platform reports mere existence as identity. A recorded process
is recognized only when its creation time still matches the one stored
beside its process ID, so a reused process ID never passes for the
session that first claimed it.
"""

import functools
import os
import select
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from agent_parley.state import BridgeError

STOP_TIMEOUT = 10
POLL_INTERVAL = 0.05
PS_TIMEOUT = 5

PsReader = Callable[[str, int], str]


def read_ps_field(field: str, pid: int) -> str:
    """Reads one ``ps`` output field for a process.

    Args:
        field: A ``ps`` field specifier ending in ``=`` so no header is
            printed, such as ``lstart=`` or ``args=``.
        pid: Process ID to inspect.

    Returns:
        The field value without surrounding whitespace, or an empty
        string when ``ps`` reports no such process or cannot be run.
    """
    try:
        result = subprocess.run(
            ["ps", "-ww", "-o", field, "-p", str(pid)],
            capture_output=True,
            text=True,
            check=False,
            timeout=PS_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def linux_start_ticks(pid: int) -> str:
    """Reads Linux process creation ticks, independent of wall-clock changes.

    Args:
        pid: Process ID to inspect.

    Returns:
        Creation ticks since boot, which no later clock change rewrites.
    """
    return Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()[19]


def linux_running(pid: int) -> bool:
    """Reports whether the kernel still lists a process in ``/proc``.

    Args:
        pid: Process ID to inspect.

    Returns:
        Whether that process ID currently exists.
    """
    return Path(f"/proc/{pid}").exists()


def linux_matches_command(pid: int, home: Path) -> bool:
    """Compares a Linux argument vector with the expected server launch.

    Args:
        pid: Process ID to inspect.
        home: Private state directory the server was launched with.

    Returns:
        Whether the process runs this package's server for that home.
    """
    command = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\x00")
    expected = [
        b"-m",
        b"agent_parley.server",
        b"--home",
        str(home).encode(),
    ]
    return command[1:5] == expected


def linux_terminate(pid: int, ticks: str) -> None:
    """Pins the process with pidfd before signaling and waiting for exit.

    Args:
        pid: Process ID recorded for the server.
        ticks: Creation ticks recorded beside that process ID.

    Raises:
        BridgeError: If the creation ticks changed or the process did
            not exit within the shutdown timeout.
    """
    try:
        fd = os.pidfd_open(pid)
    except ProcessLookupError:
        return
    try:
        if linux_start_ticks(pid) != ticks:
            raise BridgeError("Server PID changed; refusing to signal it.")
        signal.pidfd_send_signal(fd, signal.SIGTERM)
        if not select.select([fd], [], [], STOP_TIMEOUT)[0]:
            raise BridgeError("Server did not stop within 10s.")
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass
    finally:
        os.close(fd)


def darwin_running(pid: int) -> bool:
    """Reports whether a process exists on macOS, whoever owns it.

    Signal number zero runs the kernel's existence and permission checks
    without delivering anything. A missing process raises a lookup
    error; a process owned by another user raises a permission error,
    which is itself proof that the process exists and must therefore
    report as running rather than as gone.

    Args:
        pid: Process ID to inspect.

    Returns:
        Whether that process ID currently exists.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def darwin_start_ticks(reader: PsReader, pid: int) -> str:
    """Reads a macOS process creation time through ``ps``.

    macOS publishes no ``/proc``, so creation identity comes from
    ``ps``. Of the fields it offers, ``lstart`` is the stable one: it
    prints the absolute weekday, date, time and year the kernel recorded
    when the process was forked, at one-second resolution. ``start``
    abbreviates that same instant, printing only a time of day, and only
    a weekday once the process is more than a day old, so two processes
    created on different days can print identical values and a reused
    process ID would compare equal to the session it replaced. Both
    fields are rendered from the creation timestamp stored with the
    process, so a later change to the system clock does not rewrite the
    recorded value.

    Args:
        reader: Reads one ``ps`` field for a process ID.
        pid: Process ID to inspect.

    Returns:
        The recorded creation time exactly as ``ps`` prints it.

    Raises:
        ProcessLookupError: If no such process exists or ``ps`` reports
            no creation time for it.
    """
    if not darwin_running(pid):
        raise ProcessLookupError(f"No process with ID {pid}.")
    started = reader("lstart=", pid)
    if not started:
        raise ProcessLookupError(f"No creation time for process {pid}.")
    return started


def darwin_matches_command(reader: PsReader, pid: int, home: Path) -> bool:
    """Compares a macOS command line with the expected server launch.

    ``ps`` joins the argument vector with single spaces, so a split back
    into words would be ambiguous for a state directory whose path
    contains one. The recorded arguments are the last ones the launcher
    passes, so the comparison is made against the end of the printed
    command line and stays exact.

    Args:
        reader: Reads one ``ps`` field for a process ID.
        pid: Process ID to inspect.
        home: Private state directory the server was launched with.

    Returns:
        Whether the process runs this package's server for that home.
    """
    command = reader("args=", pid)
    expected = f"-m agent_parley.server --home {home}"
    return bool(command) and command.endswith(expected)


def darwin_terminate(reader: PsReader, pid: int, ticks: str) -> None:
    """Rechecks the creation time, then signals and waits for exit.

    macOS offers no pidfd, so the process cannot be pinned for the
    interval between the identity check and the signal. Re-reading the
    creation time immediately before signaling keeps the guarantee that
    only the recorded process is ever signaled and narrows the remaining
    window to the kernel's own scheduling gap. Each poll reaps the
    process first, because a child that has exited but not been reaped
    still answers an existence check.

    Args:
        reader: Reads one ``ps`` field for a process ID.
        pid: Process ID recorded for the server.
        ticks: Creation time recorded beside that process ID.

    Raises:
        BridgeError: If the creation time changed, the process belongs
            to another user, or it did not exit within the shutdown
            timeout.
    """
    try:
        if darwin_start_ticks(reader, pid) != ticks:
            raise BridgeError("Server PID changed; refusing to signal it.")
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError as error:
        raise BridgeError(
            "Server process belongs to another user; not signaling it."
        ) from error
    deadline = time.monotonic() + STOP_TIMEOUT
    while time.monotonic() < deadline:
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass
        if not darwin_running(pid):
            return
        time.sleep(POLL_INTERVAL)
    raise BridgeError("Server did not stop within 10s.")


@dataclass(frozen=True)
class Platform:
    """Bundles the process primitives one operating system provides.

    Attributes:
        start_ticks: Returns a process's recorded creation identity.
        running: Reports whether a process ID currently exists.
        matches_command: Reports whether a process runs this server for
            a given private state directory.
        terminate: Verifies identity, signals the process, and waits for
            it to exit.
    """

    start_ticks: Callable[[int], str]
    running: Callable[[int], bool]
    matches_command: Callable[[int, Path], bool]
    terminate: Callable[[int, str], None]


def linux_platform() -> Platform:
    """Builds the Linux primitives around ``/proc`` and pidfd.

    Returns:
        The process primitives used on Linux.
    """
    return Platform(
        start_ticks=linux_start_ticks,
        running=linux_running,
        matches_command=linux_matches_command,
        terminate=linux_terminate,
    )


def darwin_platform(reader: PsReader = read_ps_field) -> Platform:
    """Builds the macOS primitives around one ``ps`` reader.

    Args:
        reader: Reads one ``ps`` field for a process ID. Tests supply a
            reader of their own so the macOS path runs on any host.

    Returns:
        The process primitives used on macOS.
    """
    return Platform(
        start_ticks=functools.partial(darwin_start_ticks, reader),
        running=darwin_running,
        matches_command=functools.partial(darwin_matches_command, reader),
        terminate=functools.partial(darwin_terminate, reader),
    )


def platform_for(system: str) -> Platform:
    """Selects the process primitives for one ``sys.platform`` value.

    Args:
        system: Platform identifier, as ``sys.platform`` reports it.

    Returns:
        The primitives that system provides.

    Raises:
        BridgeError: If the system provides neither primitive set.
    """
    if system.startswith("linux"):
        return linux_platform()
    if system == "darwin":
        return darwin_platform()
    raise BridgeError(f"Unsupported operating system: {system}.")


PLATFORM = platform_for(sys.platform)


def start_ticks(pid: int) -> str:
    """Reads a process's creation identity on the running platform.

    Args:
        pid: Process ID to inspect.

    Returns:
        A value that differs whenever a process ID is reused, so a
        recorded process ID and creation identity together name one
        process rather than one slot.
    """
    return PLATFORM.start_ticks(pid)


def running(pid: int) -> bool:
    """Reports whether a process ID currently exists.

    Existence is not identity. Pair this with ``start_ticks`` whenever a
    previously recorded process has to be recognized again.

    Args:
        pid: Process ID to inspect.

    Returns:
        Whether the kernel still holds that process ID.
    """
    return PLATFORM.running(pid)


def alive(pid: int | None, ticks: str | None) -> bool:
    """Reports whether a recorded process is still that same live process.

    Args:
        pid: Process ID recorded when the process started, if any.
        ticks: Creation identity recorded beside that process ID.

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
        """Stops the recorded process through the platform's primitives.

        Raises:
            BridgeError: If the creation identity changed or the process
                did not exit within the shutdown timeout.
        """
        PLATFORM.terminate(self.pid, self.ticks)


def identify(record: dict, home: Path) -> ServerProcess | None:
    """Matches creation identity, module, and home before accepting a PID.

    Args:
        record: Published server record naming a process ID and the
            creation identity recorded beside it.
        home: Private state directory the server was launched with.

    Returns:
        The verified process, or None when the record names no process
        that is still this server.
    """
    try:
        pid = record["pid"]
        ticks = PLATFORM.start_ticks(pid)
        matched = PLATFORM.matches_command(pid, home)
        if matched and ticks == record.get("start_ticks"):
            return ServerProcess(pid, ticks)
    except (OSError, KeyError, IndexError):
        pass
    return None
