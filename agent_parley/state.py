"""Small shared persistence helpers; safe to import in per-tool hooks."""

import contextlib
import fcntl
import json
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path


class BridgeError(Exception):
    """An actionable operational failure."""


def write_json(path: Path, value: dict) -> None:
    """Writes private JSON using atomic replacement.

    Args:
        path: Destination in an existing private directory.
        value: JSON-serializable state to publish.

    Raises:
        OSError: If writing or replacing the destination fails.
    """
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
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


@contextlib.contextmanager
def lock(path: Path, busy: str = "") -> Iterator[None]:
    """Holds an exclusive nonblocking lock for the context lifetime.

    Args:
        path: Lock file in an existing private directory.
        busy: Message replacing the generic contention text.

    Yields:
        None while the caller holds the operation lock.

    Raises:
        BridgeError: If another process holds the lock.
    """
    with path.open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BridgeError(
                busy
                or f"Another bridge operation/session owns {path.name}; "
                "retry later."
            ) from None
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)
