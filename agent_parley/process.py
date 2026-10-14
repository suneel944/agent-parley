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

The module also takes the one host load and memory reading a launch is
gated on, through `host_load`.
"""

import ctypes
import functools
import os
import select
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

from agent_parley.state import BridgeError

STOP_TIMEOUT = 10
KILL_TIMEOUT = 5
POLL_INTERVAL = 0.05
PS_TIMEOUT = 5
ANCESTRY_LIMIT = 8
BOOT_ID = Path("/proc/sys/kernel/random/boot_id")

PsReader = Callable[[str, int], str]
CwdLister = Callable[[], str]


class IdentityUnknown(OSError):
    """Signals that a running process's identity could not be read.

    A ``ps`` or ``lsof`` call that times out, cannot start or fails says
    nothing about whether the process it asked about has exited. The
    error derives from ``OSError`` so a caller that treats any failure
    as "not that process" stays safe, while a caller that must not act
    on a live process can tell "unknown" from "gone".
    """


def read_ps_field(
    field: str, pid: int, overrides: dict[str, str] | None = None
) -> str:
    """Reads one ``ps`` output field for a process.

    ``ps`` runs under the C locale and UTC so a time field such as
    ``lstart`` prints the same text for the same process whatever the
    caller's ``TZ`` or locale; process identity built from it must
    compare equal across the service, hooks and every operator shell.

    Args:
        field: A ``ps`` field specifier ending in ``=`` so no header is
            printed, such as ``lstart=`` or ``args=``.
        pid: Process ID to inspect.
        overrides: Environment values set for ``ps`` over the caller's
            own; None selects the C locale and UTC.

    Returns:
        The field value without surrounding whitespace, or an empty
        string when ``ps`` succeeds with no output or the process no
        longer exists.

    Raises:
        IdentityUnknown: If ``ps`` times out, cannot start or fails
            while the process still answers the signal-zero check.
    """
    if overrides is None:
        overrides = {"LC_ALL": "C", "TZ": "UTC"}
    try:
        result = subprocess.run(
            ["ps", "-ww", "-o", field, "-p", str(pid)],
            env={**os.environ, **overrides},
            capture_output=True,
            text=True,
            check=False,
            timeout=PS_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as error:
        if darwin_running(pid):
            raise IdentityUnknown(
                f"ps could not read {field} for process {pid}."
            ) from error
        return ""
    if result.returncode != 0:
        if darwin_running(pid):
            raise IdentityUnknown(
                f"ps failed reading {field} for process {pid}."
            )
        return ""
    return result.stdout.strip()


def read_local_ps_field(field: str, pid: int) -> str:
    """Reads one ``ps`` output field in the caller's own TZ and locale.

    Earlier releases recorded ``lstart`` this way, so a process that
    one of them recorded is recognized again only in this form.

    Args:
        field: A ``ps`` field specifier ending in ``=``.
        pid: Process ID to inspect.

    Returns:
        The field value, or an empty string when ``ps`` succeeds with no
        output or the process no longer exists.

    Raises:
        IdentityUnknown: If ``ps`` cannot read a process that still runs.
    """
    return read_ps_field(field, pid, {})


def read_lsof_cwds() -> str:
    """Lists every visible process's working directory through ``lsof``.

    ``lsof`` ships with macOS and is the only base tool there that
    reports another process's working directory. It exits non-zero when
    it could not inspect some process, which is routine for processes of
    other users, so its output is used whenever it printed any.

    Returns:
        ``lsof -Fpn`` field output: a ``p`` line naming each process ID,
        followed by an ``n`` line naming its working directory.

    Raises:
        IdentityUnknown: If ``lsof`` cannot start, times out, or fails
            without printing anything.
    """
    try:
        result = subprocess.run(
            ["lsof", "-nP", "-a", "-d", "cwd", "-Fpn"],
            capture_output=True,
            text=True,
            check=False,
            timeout=PS_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as error:
        detail = "lsof could not list working directories."
        raise IdentityUnknown(detail) from error
    if result.returncode != 0 and not result.stdout.strip():
        raise IdentityUnknown("lsof failed listing working directories.")
    return result.stdout


def parse_lsof_cwds(output: str) -> dict[int, Path]:
    """Pairs each process ID in ``lsof -Fpn`` output with its directory.

    Args:
        output: Field output as :func:`read_lsof_cwds` returns it.

    Returns:
        The working directory of every process the output names one for.
    """
    found: dict[int, Path] = {}
    pid = None
    for line in output.splitlines():
        if line.startswith("p") and line[1:].isdigit():
            pid = int(line[1:])
        elif line.startswith("n") and pid is not None:
            found[pid] = Path(line[1:])
    return found


def linux_start_ticks(pid: int) -> str:
    """Reads Linux process creation ticks, independent of wall-clock changes.

    Args:
        pid: Process ID to inspect.

    Returns:
        Creation ticks since boot, which no later clock change rewrites.
    """
    return Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()[19]


def linux_same_start(pid: int, ticks: str) -> bool:
    """Reports whether a Linux process carries recorded creation ticks.

    Args:
        pid: Process ID to inspect.
        ticks: Creation ticks recorded beside that process ID.

    Returns:
        Whether the process's creation ticks equal the recorded ones.
    """
    return linux_start_ticks(pid) == ticks


def linux_running(pid: int) -> bool:
    """Reports whether the kernel still lists a process in ``/proc``.

    Args:
        pid: Process ID to inspect.

    Returns:
        Whether that process ID currently exists.
    """
    return Path(f"/proc/{pid}").exists()


def linux_zombie(pid: int) -> bool:
    """Reports whether a Linux process has exited but not been reaped.

    A zombie keeps its process ID and its recorded creation ticks until
    its parent reaps it, so neither alone tells an exited process from a
    running one.

    Args:
        pid: Process ID to inspect.

    Returns:
        Whether `/proc` lists the process in the zombie state.
    """
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
    return fields[0] == "Z"


def linux_foreground_pid(pid: int) -> int:
    """Reads the foreground process group of a Linux hook process.

    Args:
        pid: Hook process whose controlling terminal identifies the native
            session.

    Returns:
        Process ID of the controlling terminal's foreground group leader.

    Raises:
        ProcessLookupError: If the hook has no foreground terminal process.
    """
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
    foreground = int(fields[5])
    if foreground <= 1:
        raise ProcessLookupError(f"Process {pid} has no foreground terminal.")
    return foreground


def linux_parent_pid(pid: int) -> int:
    """Reads the parent of a Linux process.

    Args:
        pid: Process ID to inspect.

    Returns:
        Process ID of the parent, which is 0 for a reaped ancestry.
    """
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
    return int(fields[1])


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


def linux_worktree_clients(lane: Path, command: str) -> list[int]:
    """Lists Linux processes running a native client inside a worktree.

    A client counts when its working directory is the worktree or lies
    inside it, and the base name of its first or second argument is the
    client command, so both a native binary and a script run by an
    interpreter such as `node` are recognized. A process that exits or
    denies access while it is read is skipped.

    Args:
        lane: Lane worktree the client runs in.
        command: Native client command, such as `claude`.

    Returns:
        Process IDs of every matching process, in `/proc` order.
    """
    found = []
    target = lane.resolve()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            words = (entry / "cmdline").read_bytes().split(b"\x00")[:2]
            if command.encode() not in (
                os.path.basename(word) for word in words
            ):
                continue
            if Path(os.readlink(entry / "cwd")).is_relative_to(target):
                found.append(int(entry.name))
        except OSError:
            continue
    return found


def linux_cwd_pids(target: Path) -> list[int] | None:
    """Lists Linux processes whose working directory lies inside a path.

    A process that exits or denies access while it is read is skipped.

    Args:
        target: Directory, such as a worktree about to be moved.

    Returns:
        Process IDs whose working directory is the path or lies inside
        it, or None when ``/proc`` cannot be listed.
    """
    root = target.resolve()
    found = []
    try:
        entries = list(Path("/proc").iterdir())
    except OSError:
        return None
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            if Path(os.readlink(entry / "cwd")).is_relative_to(root):
                found.append(int(entry.name))
        except OSError:
            continue
    return found


def libc_pidfd(name: str, *arguments: int | None) -> int:
    """Calls the host's pidfd API when Python was built without its wrappers.

    Portable Python builds can omit pidfd wrappers even on a capable host.
    Calling the same libc API preserves the pinned-process signaling contract;
    a missing host API fails closed instead of falling back to a numeric PID.

    Args:
        name: Either pidfd_open or pidfd_send_signal.
        *arguments: Native arguments, including a null siginfo pointer.

    Returns:
        The file descriptor or successful signal result.

    Raises:
        BridgeError: If the host libc lacks the requested API.
        OSError: If the native call fails, retaining its errno subclass.
    """
    library = ctypes.CDLL(None, use_errno=True)
    try:
        function = getattr(library, name)
    except AttributeError as exc:
        raise BridgeError(
            f"This Python build requires a host libc providing {name}."
        ) from exc
    function.argtypes = (
        [ctypes.c_int, ctypes.c_uint]
        if name == "pidfd_open"
        else [ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint]
    )
    function.restype = ctypes.c_int
    result = int(function(*arguments))
    if result < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return result


def linux_signal(fd: int, number: int) -> None:
    """Signals the process a pidfd pins.

    Args:
        fd: Pidfd of the recorded process.
        number: Signal to deliver.
    """
    if hasattr(signal, "pidfd_send_signal"):
        signal.pidfd_send_signal(fd, number)
    else:
        libc_pidfd("pidfd_send_signal", fd, number, None, 0)


def linux_terminate(pid: int, ticks: str) -> None:
    """Pins the process with pidfd before signaling and waiting for exit.

    A process that ignores `SIGTERM`, such as a client wedged in a native
    dialog or left behind by a system hang, is sent `SIGKILL` once the
    shutdown timeout passes, through the same pinned descriptor, so the
    escalation can never reach a recycled process ID.

    Args:
        pid: Process ID recorded for the process.
        ticks: Creation ticks recorded beside that process ID.

    Raises:
        BridgeError: If the creation ticks changed or the process did
            not exit even after `SIGKILL`.
    """
    try:
        fd = (
            os.pidfd_open(pid)
            if hasattr(os, "pidfd_open")
            else libc_pidfd("pidfd_open", pid, 0)
        )
    except ProcessLookupError:
        return
    try:
        if linux_start_ticks(pid) != ticks:
            raise BridgeError("Server PID changed; refusing to signal it.")
        linux_signal(fd, signal.SIGTERM)
        if not select.select([fd], [], [], STOP_TIMEOUT)[0]:
            linux_signal(fd, signal.SIGKILL)
            if not select.select([fd], [], [], KILL_TIMEOUT)[0]:
                raise BridgeError(
                    f"Process {pid} did not exit after SIGTERM and SIGKILL."
                )
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


def darwin_zombie(reader: PsReader, pid: int) -> bool:
    """Reports whether a macOS process has exited but not been reaped.

    A zombie still answers the signal-zero existence check, so only the
    state ``ps`` prints tells it from a running process.

    Args:
        reader: Reads one ``ps`` field for a process ID.
        pid: Process ID to inspect.

    Returns:
        Whether ``ps`` reports the process in the zombie state.
    """
    return reader("stat=", pid).startswith("Z")


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
    recorded value. The reader renders that instant in UTC under the C
    locale, so callers with different ``TZ`` or locale settings read
    the same value.

    Args:
        reader: Reads one ``ps`` field for a process ID.
        pid: Process ID to inspect.

    Returns:
        The recorded creation time exactly as ``ps`` prints it.

    Raises:
        ProcessLookupError: If no such process exists.
        IdentityUnknown: If the process still runs but ``ps`` could not
            report its creation time, so it is neither proven gone nor
            proven to be the recorded process.
    """
    if not darwin_running(pid):
        raise ProcessLookupError(f"No process with ID {pid}.")
    started = reader("lstart=", pid)
    if started:
        return started
    if darwin_running(pid):
        raise IdentityUnknown(f"No creation time for running process {pid}.")
    raise ProcessLookupError(f"No process with ID {pid}.")


def darwin_same_start(
    reader: PsReader, local_reader: PsReader, pid: int, ticks: str
) -> bool:
    """Reports whether a macOS process carries a recorded creation time.

    A record written before ``ps`` ran in UTC and the C locale holds
    ``lstart`` in the recorder's own ``TZ`` and locale. When the UTC
    reading differs, the caller's own reading is accepted too, so a
    live lane recorded before an upgrade is not taken for a dead one.

    Args:
        reader: Reads one ``ps`` field in UTC and the C locale.
        local_reader: Reads one ``ps`` field in the caller's own ``TZ``
            and locale.
        pid: Process ID to inspect.
        ticks: Creation time recorded beside that process ID.

    Returns:
        Whether either reading equals the recorded creation time.

    Raises:
        ProcessLookupError: If no such process exists.
        IdentityUnknown: If the process runs but ``ps`` could not report
            its creation time.
    """
    if darwin_start_ticks(reader, pid) == ticks:
        return True
    return local_reader("lstart=", pid) == ticks


def darwin_foreground_pid(reader: PsReader, pid: int) -> int:
    """Reads the foreground terminal process group of a macOS hook.

    Args:
        reader: Reads one ``ps`` field for a process ID.
        pid: Hook process whose controlling terminal identifies the native
            session.

    Returns:
        Process ID of the controlling terminal's foreground group leader.

    Raises:
        ProcessLookupError: If the hook has no foreground terminal process.
    """
    value = reader("tpgid=", pid)
    try:
        foreground = int(value)
    except ValueError:
        foreground = -1
    if foreground <= 1:
        raise ProcessLookupError(f"Process {pid} has no foreground terminal.")
    return foreground


def darwin_parent_pid(reader: PsReader, pid: int) -> int:
    """Reads the parent of a macOS process.

    Args:
        reader: Reads one ``ps`` field for a process ID.
        pid: Process ID to inspect.

    Returns:
        Process ID of the parent, which is 0 for a reaped ancestry.
    """
    value = reader("ppid=", pid)
    try:
        return int(value)
    except ValueError:
        return 0


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


def darwin_cwd_pids(lister: CwdLister, target: Path) -> list[int] | None:
    """Lists macOS processes whose working directory lies inside a path.

    ``ps`` does not report a working directory on macOS, so the listing
    comes from ``lsof``.

    Args:
        lister: Returns ``lsof -Fpn`` working-directory output.
        target: Directory, such as a worktree about to be moved.

    Returns:
        Process IDs whose working directory is the path or lies inside
        it, or None when ``lsof`` could not list them.
    """
    root = target.resolve()
    try:
        cwds = parse_lsof_cwds(lister())
    except OSError:
        return None
    return [pid for pid, path in cwds.items() if path.is_relative_to(root)]


def darwin_worktree_clients(
    lister: CwdLister, reader: PsReader, lane: Path, command: str
) -> list[int]:
    """Lists macOS processes running a native client inside a worktree.

    A process counts when ``lsof`` places its working directory inside
    the worktree and the base name of the first or second word ``ps``
    prints for its arguments is the client command, matching the Linux
    rule for a native binary and for a script run by an interpreter. A
    process whose arguments cannot be read is skipped.

    Args:
        lister: Returns ``lsof -Fpn`` working-directory output.
        reader: Reads one ``ps`` field for a process ID.
        lane: Lane worktree the client runs in.
        command: Native client command, such as `claude`.

    Returns:
        Process IDs of every matching process, or an empty list when
        ``lsof`` could not list working directories.
    """
    found = []
    for pid in darwin_cwd_pids(lister, lane) or []:
        try:
            words = reader("args=", pid).split()[:2]
        except OSError:
            continue
        if command in (os.path.basename(word) for word in words):
            found.append(pid)
    return found


def darwin_terminate(
    reader: PsReader, local_reader: PsReader, pid: int, ticks: str
) -> None:
    """Rechecks the creation time, then signals and waits for exit.

    macOS offers no pidfd, so the process cannot be pinned for the
    interval between the identity check and the signal. Re-reading the
    creation time immediately before signaling keeps the guarantee that
    only the recorded process is ever signaled and narrows the remaining
    window to the kernel's own scheduling gap. Each poll reaps the
    process first, because a child that has exited but not been reaped
    still answers an existence check. A process still running once the
    shutdown timeout passes is sent `SIGKILL` after the same creation-time
    check.

    Args:
        reader: Reads one ``ps`` field for a process ID.
        local_reader: Reads one ``ps`` field in the caller's own ``TZ``
            and locale, for a creation time recorded in that form.
        pid: Process ID recorded for the process.
        ticks: Creation time recorded beside that process ID.

    Raises:
        BridgeError: If the creation time changed or could not be read,
            the process belongs to another user, or it did not exit even
            after `SIGKILL`.
    """
    for number, timeout in (
        (signal.SIGTERM, STOP_TIMEOUT),
        (signal.SIGKILL, KILL_TIMEOUT),
    ):
        try:
            if not darwin_same_start(reader, local_reader, pid, ticks):
                raise BridgeError("Server PID changed; refusing to signal it.")
            os.kill(pid, number)
        except IdentityUnknown as error:
            raise BridgeError(
                "Server identity could not be read; refusing to signal it."
            ) from error
        except ProcessLookupError:
            return
        except PermissionError as error:
            raise BridgeError(
                "Server process belongs to another user; not signaling it."
            ) from error
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass
            if not darwin_running(pid):
                return
            try:
                if darwin_zombie(reader, pid):
                    return
            except IdentityUnknown:
                pass
            time.sleep(POLL_INTERVAL)
    raise BridgeError(f"Process {pid} did not exit after SIGTERM and SIGKILL.")


class Platform(NamedTuple):
    """Bundles the process primitives one operating system provides.

    A named tuple rather than a dataclass keeps this module, which the
    lifecycle hook imports on every native tool call, clear of the
    `dataclasses` and `inspect` import cost.

    Attributes:
        start_ticks: Returns a process's recorded creation identity.
        same_start: Reports whether a process carries a recorded
            creation identity, in any form a release has recorded.
        foreground_pid: Returns a hook's foreground terminal process group.
        parent_pid: Returns a process's parent, for walking a hook's
            ancestry when it has no controlling terminal.
        running: Reports whether a process ID currently exists.
        zombie: Reports whether a process ID has exited but is not yet
            reaped, so its creation identity still matches while nothing
            can be signaled or waited for.
        matches_command: Reports whether a process runs this server for
            a given private state directory.
        terminate: Verifies identity, signals the process, and waits for
            it to exit.
        boot_id: Names the current boot of the host, so a process record
            written before a restart is known to be from an earlier boot.
        worktree_clients: Lists processes running a named native client
            with a lane worktree as their working directory.
        cwd_pids: Lists every process whose working directory lies
            inside a path, or None when the platform cannot tell.
    """

    start_ticks: Callable[[int], str]
    same_start: Callable[[int, str], bool]
    foreground_pid: Callable[[int], int]
    parent_pid: Callable[[int], int]
    running: Callable[[int], bool]
    zombie: Callable[[int], bool]
    matches_command: Callable[[int, Path], bool]
    terminate: Callable[[int, str], None]
    boot_id: Callable[[], str]
    worktree_clients: Callable[[Path, str], list[int]]
    cwd_pids: Callable[[Path], list[int] | None]


def linux_boot_id() -> str:
    """Reads the identifier the Linux kernel draws afresh at every boot.

    Returns:
        The boot identifier.
    """
    return BOOT_ID.read_text().strip()


def darwin_boot_id() -> str:
    """Reads the instant the macOS kernel records for the current boot.

    Returns:
        The boot time in epoch seconds, the ``sec`` field `sysctl` prints.
        The local-time rendering after it is dropped because it changes
        with the timezone while the host keeps running; output without a
        ``sec`` field is returned whole.
    """
    output = subprocess.run(
        ["sysctl", "-n", "kern.boottime"],
        capture_output=True,
        text=True,
        timeout=PS_TIMEOUT,
        check=True,
    ).stdout.strip()
    seconds = output.partition("sec = ")[2].partition(",")[0].strip()
    return seconds if seconds.isdigit() else output


def linux_platform() -> Platform:
    """Builds the Linux primitives around ``/proc`` and pidfd.

    Returns:
        The process primitives used on Linux.
    """
    return Platform(
        start_ticks=linux_start_ticks,
        same_start=linux_same_start,
        foreground_pid=linux_foreground_pid,
        parent_pid=linux_parent_pid,
        running=linux_running,
        zombie=linux_zombie,
        matches_command=linux_matches_command,
        terminate=linux_terminate,
        boot_id=linux_boot_id,
        worktree_clients=linux_worktree_clients,
        cwd_pids=linux_cwd_pids,
    )


def darwin_platform(
    reader: PsReader = read_ps_field,
    local_reader: PsReader = read_local_ps_field,
    lister: CwdLister = read_lsof_cwds,
) -> Platform:
    """Builds the macOS primitives around ``ps`` and ``lsof`` readers.

    Args:
        reader: Reads one ``ps`` field for a process ID. Tests supply a
            reader of their own so the macOS path runs on any host.
        local_reader: Reads one ``ps`` field in the caller's own ``TZ``
            and locale, to recognize a creation time recorded that way.
        lister: Returns ``lsof -Fpn`` working-directory output. Tests
            supply output of their own.

    Returns:
        The process primitives used on macOS.
    """
    return Platform(
        start_ticks=functools.partial(darwin_start_ticks, reader),
        same_start=functools.partial(darwin_same_start, reader, local_reader),
        foreground_pid=functools.partial(darwin_foreground_pid, reader),
        parent_pid=functools.partial(darwin_parent_pid, reader),
        running=darwin_running,
        zombie=functools.partial(darwin_zombie, reader),
        matches_command=functools.partial(darwin_matches_command, reader),
        terminate=functools.partial(darwin_terminate, reader, local_reader),
        boot_id=darwin_boot_id,
        worktree_clients=functools.partial(
            darwin_worktree_clients, lister, reader
        ),
        cwd_pids=functools.partial(darwin_cwd_pids, lister),
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

OSRELEASE = Path("/proc/sys/kernel/osrelease")

MOUNTED_DRIVE = (
    "Git worktrees and locks on a mounted Windows drive are not supported; "
    "the repository must live in the Linux file system."
)


def kernel_release(path: Path = OSRELEASE) -> str:
    """Reads the running kernel's release string.

    Args:
        path: Kernel release file; tests supply a file of their own.

    Returns:
        The release string, or an empty string where the file is absent, as
        on macOS.
    """
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def wsl_version(release: str) -> str:
    """Names the Windows Subsystem for Linux generation a kernel belongs to.

    Both generations put ``microsoft`` in the release string; only the
    second names itself ``WSL2`` there.

    Args:
        release: Kernel release string, as ``kernel_release`` reads it.

    Returns:
        ``"2"``, ``"1"``, or ``"none"`` outside WSL.
    """
    lowered = release.lower()
    if "microsoft" not in lowered:
        return "none"
    return "2" if "wsl2" in lowered else "1"


def pidfd_available() -> bool:
    """Reports whether the host can open a pidfd for a process.

    A kernel without the call, as under WSL1, and a build whose libc lacks
    it both answer no.

    Returns:
        Whether ``pidfd_open`` succeeded for this process.
    """
    try:
        fd = (
            os.pidfd_open(os.getpid())
            if hasattr(os, "pidfd_open")
            else libc_pidfd("pidfd_open", os.getpid(), 0)
        )
    except (OSError, BridgeError):
        return False
    os.close(fd)
    return True


def host_report(release: str | None = None) -> dict:
    """Describes the kernel a ``doctor`` run found.

    Args:
        release: Kernel release string; read from the host when omitted.

    Returns:
        The kernel release, the WSL generation or ``"none"``, and whether
        ``pidfd_open`` is available.
    """
    if release is None:
        release = kernel_release()
    return {
        "kernel": release,
        "wsl": wsl_version(release),
        "pidfd_open": pidfd_available(),
    }


MEMINFO = Path("/proc/meminfo")
MIB = 1024 * 1024
DARWIN_AVAILABLE = ("Pages free", "Pages inactive", "Pages speculative")
LOAD_SKIPPED = (
    "Host load is not read on Windows, so the launch load threshold is skipped."
)


class HostLoad(NamedTuple):
    """One reading of the host's load and available memory.

    Attributes:
        ratio: One-minute load average per CPU, or None when unreadable.
        available_mib: Memory new work can take, or None when unreadable.
        skipped: Why no reading was taken, empty when one was.
    """

    ratio: float | None
    available_mib: int | None
    skipped: str = ""


def linux_available_mib(path: Path = MEMINFO) -> int | None:
    """Reads the memory Linux says new work can take without swapping.

    Args:
        path: Memory statistics file; tests supply a file of their own.

    Returns:
        ``MemAvailable`` in MiB, or None when the file or field is
        unreadable.
    """
    try:
        text = path.read_text()
    except OSError:
        return None
    for line in text.splitlines():
        field, _, value = line.partition(":")
        words = value.split()
        if field == "MemAvailable" and words and words[0].isdigit():
            return int(words[0]) // 1024
    return None


def darwin_available_mib(output: str | None = None) -> int | None:
    """Reads the free, inactive and speculative pages ``vm_stat`` reports.

    Args:
        output: ``vm_stat`` output; read from the host when omitted.

    Returns:
        Those pages in MiB, or None when ``vm_stat`` fails or prints no
        page size or one of the counts.
    """
    if output is None:
        try:
            output = subprocess.run(
                ["vm_stat"],
                capture_output=True,
                text=True,
                timeout=PS_TIMEOUT,
                check=True,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return None
    lines = output.splitlines()
    size = lines[0].partition("page size of ")[2].split() if lines else []
    counts = {}
    for line in lines[1:]:
        field, _, value = line.partition(":")
        counts[field.strip()] = value.strip().rstrip(".")
    pages = [counts.get(field, "") for field in DARWIN_AVAILABLE]
    if not size or not size[0].isdigit():
        return None
    if not all(page.isdigit() for page in pages):
        return None
    return sum(int(page) for page in pages) * int(size[0]) // MIB


def host_load(system: str = sys.platform) -> HostLoad:
    """Reads the host's load and available memory once, for one launch.

    The load ratio is the one-minute load average divided by the CPU
    count, so the same number means the same pressure on any machine.
    Nothing samples it continuously; each launch and each dispatch takes
    its own reading.

    Args:
        system: Platform name as ``sys.platform`` reports it.

    Returns:
        The ratio rounded to two places and the available memory, each
        None when unreadable; on Windows no reading, with `LOAD_SKIPPED`.
    """
    if system == "win32":
        return HostLoad(None, None, LOAD_SKIPPED)
    try:
        ratio: float | None = round(
            os.getloadavg()[0] / (os.cpu_count() or 1), 2
        )
    except OSError:
        ratio = None
    available = (
        darwin_available_mib() if system == "darwin" else linux_available_mib()
    )
    return HostLoad(ratio, available)


def overloaded(reading: HostLoad, max_ratio: float, min_mib: int) -> str:
    """Names each reading past the launch threshold.

    An unreadable reading is never judged, so a host that cannot report
    its load still launches.

    Args:
        reading: A `host_load` reading.
        max_ratio: The ``max_load_ratio`` supervision setting.
        min_mib: The ``min_available_mib`` supervision setting.

    Returns:
        Each reading past its threshold beside the setting that moves it,
        joined in one clause, or empty text when none is.
    """
    causes = []
    if reading.ratio is not None and reading.ratio > max_ratio:
        causes.append(
            f"load ratio {reading.ratio} per CPU is above max_load_ratio "
            f"{max_ratio}"
        )
    available = reading.available_mib
    if available is not None and available < min_mib:
        causes.append(
            f"{available} MiB available is below min_available_mib {min_mib}"
        )
    return " and ".join(causes)


def load_line(reading: HostLoad, max_ratio: float, min_mib: int) -> str:
    """Words a `host_load` reading against its threshold for ``doctor``.

    Args:
        reading: A `host_load` reading.
        max_ratio: The ``max_load_ratio`` supervision setting.
        min_mib: The ``min_available_mib`` supervision setting.

    Returns:
        The skipped line on Windows, else both readings beside their
        thresholds and whether a launch would go ahead.
    """
    if reading.skipped:
        return reading.skipped
    ratio = "unreadable" if reading.ratio is None else reading.ratio
    available = (
        "unreadable" if reading.available_mib is None else reading.available_mib
    )
    verdict = (
        "over the launch threshold, so run refuses and dispatch defers"
        if overloaded(reading, max_ratio, min_mib)
        else "under the launch threshold"
    )
    return (
        f"load ratio {ratio} of at most {max_ratio}, {available} MiB "
        f"available of at least {min_mib}; {verdict}"
    )


def check_repository_host(repo: Path, release: str | None = None) -> None:
    """Refuses a repository on a mounted Windows drive under WSL.

    Git worktree locks and the coordination locks do not hold across the
    9p and drvfs mounts under ``/mnt``, so a lane there would corrupt the
    checkout it shares with the operator.

    Args:
        repo: Target repository path.
        release: Kernel release string; read from the host when omitted.

    Raises:
        BridgeError: If the host is WSL and the repository lies under
            ``/mnt/``.
    """
    if release is None:
        release = kernel_release()
    if wsl_version(release) == "none":
        return
    if repo.resolve().is_relative_to("/mnt"):
        raise BridgeError(MOUNTED_DRIVE)


def boot_id() -> str:
    """Names the current boot of the host.

    Returns:
        A value that changes whenever the host starts again, or empty text
        when the platform cannot report one, in which case no restart can
        be told from an ordinary quiet lane.
    """
    try:
        return PLATFORM.boot_id()
    except (OSError, subprocess.SubprocessError):
        return ""


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
        recycled process ID reports false, a missing record reports
        false, and so does a process left in the zombie state, which
        keeps its process ID and creation identity after it has already
        exited. A process whose identity cannot be read reports false
        too; a caller that must not act on a live process uses
        :func:`alive_or_unknown` instead.
    """
    return alive_or_unknown(pid, ticks) is True


def alive_or_unknown(pid: int | None, ticks: str | None) -> bool | None:
    """Reports whether a recorded process still runs, or that none can tell.

    On macOS identity comes from ``ps``, which can time out or fail on a
    loaded host while the process keeps running. Reading that as "exited"
    would let supervision resume or replace a session that still runs and
    let a hook relaunch a running service, so it is reported apart.

    Args:
        pid: Process ID recorded when the process started, if any.
        ticks: Creation identity recorded beside that process ID.

    Returns:
        True for the same live process, False for a missing record, an
        exited, recycled or zombie process, and None when the process
        runs but its identity could not be read, so the caller skips the
        decision until a later reading.
    """
    try:
        return (
            pid is not None
            and ticks is not None
            and PLATFORM.same_start(pid, ticks)
            and not PLATFORM.zombie(pid)
        )
    except IdentityUnknown:
        return None
    except (OSError, IndexError, ValueError, TypeError):
        return False


def cwd_pids(target: Path) -> list[int] | None:
    """Lists the processes working inside a directory on this platform.

    Args:
        target: Directory, such as a worktree about to be moved.

    Returns:
        Process IDs whose working directory is the directory or lies
        inside it, or None when the platform cannot list them.
    """
    return PLATFORM.cwd_pids(target)


class ServerProcess(NamedTuple):
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


def foreground_process(hook_pid: int | None) -> ServerProcess | None:
    """Identifies the native session owning a hook's foreground terminal.

    The native client and every hook it starts share the terminal's foreground
    process group. Its leader remains the native client across short-lived hook
    shells, unlike a hook's immediate parent. A headless or detached hook has
    no trustworthy native identity and therefore returns no process.

    Args:
        hook_pid: Process ID of the generated hook client while it waits for
            the checkpoint response.

    Returns:
        Verified native process identity, or ``None`` when no foreground
        terminal process can be established.
    """
    if type(hook_pid) is not int or hook_pid <= 1:
        return None
    try:
        pid = PLATFORM.foreground_pid(hook_pid)
        ticks = PLATFORM.start_ticks(pid)
        if PLATFORM.running(pid):
            return ServerProcess(pid, ticks)
    except (OSError, IndexError, ValueError, TypeError):
        pass
    return None


def launched_process(
    hook_pid: int | None, launcher_pid: object, launcher_ticks: object
) -> ServerProcess | None:
    """Identifies the native session a recorded launcher started.

    A provider that starts its hooks with no controlling terminal leaves
    ``foreground_process`` nothing to read, and a lane with no recorded
    session identity is never eligible for work. The launcher's own
    identity is recorded when it starts the native client, and the client
    is its direct child, so walking a hook's ancestry to the process whose
    parent is that launcher names the same session without a terminal.

    The launcher's creation identity is rechecked before the walk, so a
    recycled process ID cannot adopt a hook, and the ancestry walk is
    bounded, so a long or looping chain cannot hold the hook.

    Args:
        hook_pid: Process ID of the generated hook client while it waits
            for the checkpoint response.
        launcher_pid: Process ID the launcher recorded for itself.
        launcher_ticks: Creation ticks the launcher recorded for itself.

    Returns:
        Verified native process identity, or ``None`` when no ancestor of
        the hook is a child of that launcher.
    """
    if type(hook_pid) is not int or hook_pid <= 1:
        return None
    if type(launcher_pid) is not int or launcher_pid <= 1:
        return None
    try:
        if not isinstance(launcher_ticks, str) or not PLATFORM.same_start(
            launcher_pid, launcher_ticks
        ):
            return None
        pid = hook_pid
        for _ in range(ANCESTRY_LIMIT):
            parent = PLATFORM.parent_pid(pid)
            if parent == launcher_pid:
                ticks = PLATFORM.start_ticks(pid)
                if PLATFORM.running(pid):
                    return ServerProcess(pid, ticks)
                return None
            if parent <= 1:
                return None
            pid = parent
    except (OSError, IndexError, ValueError, TypeError):
        pass
    return None


def recorded_process(
    hook_pid: int | None, session_pid: object, session_ticks: object
) -> ServerProcess | None:
    """Confirms a lane's recorded session from the hook's own ancestry.

    A native client can start a new session identity inside the same
    process, and a provider that starts its hooks without a controlling
    terminal leaves no reading that names that process again. The client
    still starts every hook of the session it serves, so a recorded
    session process that is both unchanged and an ancestor of the hook is
    the process that sent the event, and the lane keeps the identity it
    already had instead of reading as stopped until the next launch.

    The recorded creation identity is rechecked before the walk, so a
    recycled process ID is never adopted, and the walk is bounded, so a
    long or looping chain cannot hold the hook. A live process the hook
    does not descend from is never confirmed.

    Args:
        hook_pid: Process ID of the generated hook client while it waits
            for the checkpoint response.
        session_pid: Process ID recorded for the lane's native session.
        session_ticks: Creation ticks recorded beside that process ID.

    Returns:
        Verified native process identity, or ``None`` when the recorded
        session is missing, gone, recycled, or not an ancestor of the
        hook.
    """
    if type(hook_pid) is not int or hook_pid <= 1:
        return None
    if type(session_pid) is not int or session_pid <= 1:
        return None
    if not isinstance(session_ticks, str) or not alive(
        session_pid, session_ticks
    ):
        return None
    try:
        pid = hook_pid
        for _ in range(ANCESTRY_LIMIT):
            parent = PLATFORM.parent_pid(pid)
            if parent == session_pid:
                return ServerProcess(session_pid, session_ticks)
            if parent <= 1:
                return None
            pid = parent
    except (OSError, IndexError, ValueError, TypeError):
        pass
    return None


def worktree_client(lane: Path, command: str) -> ServerProcess | None:
    """Names the native client running in a lane worktree, if one does.

    A session forked or resumed outside the launcher keeps the lane's
    worktree but runs in a process the lane never recorded. Its working
    directory still names the lane, so it is found here and the lane is
    not read as gone while it runs. When several clients run there, the
    most recently started one is named, because a fork is started after
    the session it continues.

    Args:
        lane: Lane worktree the client runs in.
        command: Native client command, such as `claude`.

    Returns:
        Verified identity of the newest live client, or None when no live
        client runs there or the platform cannot tell.
    """
    found = []
    try:
        candidates = PLATFORM.worktree_clients(lane, command)
    except OSError:
        return None
    for pid in candidates:
        try:
            ticks = PLATFORM.start_ticks(pid)
        except (OSError, IndexError, ValueError):
            continue
        if alive(pid, ticks):
            found.append(ServerProcess(pid, ticks))
    if not found:
        return None
    return max(found, key=lambda client: (len(client.ticks), client.ticks))


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
        recorded = record.get("start_ticks")
        if (
            PLATFORM.matches_command(pid, home)
            and isinstance(recorded, str)
            and PLATFORM.same_start(pid, recorded)
        ):
            return ServerProcess(pid, ticks)
    except (OSError, KeyError, IndexError):
        pass
    return None
