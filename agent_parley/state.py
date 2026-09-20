"""Small shared persistence helpers; safe to import in per-tool hooks."""

import contextlib
import fcntl
import json
import os
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

from agent_parley import BridgeError

MAX_LOG_BYTES = 262144
MAX_LOG_RECORDS = 2000


class LockBusy(BridgeError):
    """Contention a current holder of the same lock caused.

    A caller that cannot distinguish contention from a real operational
    failure has to treat both as a failure, and a hook that treats
    contention as a failure denies the native call it was deciding. This
    names the case so a caller can wait, defer, or degrade instead.
    """


def write_json(path: Path, value: dict) -> None:
    """Writes private JSON using atomic replacement.

    Args:
        path: Destination in an existing private directory.
        value: JSON-serializable state to publish.

    Raises:
        OSError: If writing or replacing the destination fails.
    """
    write_text(path, json.dumps(value, indent=2) + "\n")


def write_text(path: Path, text: str) -> None:
    """Writes text using atomic replacement.

    A reader of the destination sees either the previous file or the whole new
    one, never a partial frame, because the content is written to a temporary
    file in the same directory and renamed over the destination.

    Args:
        path: Destination in an existing directory.
        text: Content to publish.

    Raises:
        OSError: If writing or replacing the destination fails.
    """
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def trim_log(
    path: Path,
    ceiling: int = MAX_LOG_BYTES,
    records: int = MAX_LOG_RECORDS,
) -> list[str]:
    """Keeps a line log bounded by dropping its oldest lines in place.

    One rotation serves every line log the runtime keeps, so a lane's report
    log and the service log are bounded by the same reading and the same
    ceiling. The rewrite happens in the file rather than through an atomic
    replacement, because the service writes its log through a descriptor its
    launcher opened for appending: replacing the file would leave that
    descriptor writing to an unlinked one, and the log would silently stop.
    A caller that needs the two appends around a rewrite to be ordered holds
    its own lock, as the lane report log does.

    The rewrite keeps the newest lines that fit under both the count and the
    ceiling, and always keeps the newest line, so a log of long lines is
    bounded by the same number of bytes as a log of short ones. Lines long
    enough to fill the ceiling on their own are cut back to half of it, so a
    log at its bound is not rewritten again on the next line.

    Args:
        path: Line log to bound; a missing or unreadable file is left alone.
        ceiling: Size in bytes above which the log is rewritten, and under
            which the rewrite leaves it.
        records: Number of newest lines the rewrite may keep.

    Returns:
        The lines the rewrite dropped, oldest first, so a caller can release
        whatever they referenced.
    """
    try:
        if path.stat().st_size < ceiling:
            return []
        lines = path.read_text(errors="ignore").splitlines()
        sizes = [len(line.encode()) + 1 for line in lines]
        start = max(len(lines) - records, 0)
        total = sum(sizes[start:])
        target = ceiling if total <= ceiling else ceiling // 2
        while start < len(lines) - 1 and total > target:
            total -= sizes[start]
            start += 1
        with path.open("r+", encoding="utf-8") as stream:
            stream.write("\n".join(lines[start:]) + "\n")
            stream.truncate()
    except OSError:
        return []
    return lines[:start]


@contextlib.contextmanager
def lock(path: Path, busy: str = "", *, timeout: float = 0) -> Iterator[None]:
    """Holds an exclusive lock with an optional bounded acquisition wait.

    Args:
        path: Lock file in an existing private directory.
        busy: Message replacing the generic contention text.
        timeout: Seconds to wait for short operations; sessions never wait.

    Yields:
        None while the caller holds the operation lock.

    Raises:
        LockBusy: If another process holds the lock.
    """
    with path.open("a") as stream:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise LockBusy(
                        busy
                        or "Another bridge operation/session owns "
                        f"{path.name}; "
                        "retry later."
                    ) from None
                time.sleep(min(0.01, remaining))
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)
