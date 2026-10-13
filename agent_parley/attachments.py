"""Spills oversized coordination payloads to bounded files under state."""

import json
import re
from collections.abc import Iterable
from pathlib import Path

from agent_parley.state import BridgeError, write_json, write_text

FOLDER = "attachments"
MAX_ATTACHMENT_BYTES = 65536
MAX_LANE_BYTES = 1048576
PAGE_CHARACTERS = 2048
KINDS = ("message", "report", "offer")
REFERENCE = re.compile(r"^(message|report|offer)-[A-Za-z0-9]{1,32}$")
MARKER = re.compile(
    r"\[attachment ((?:message|report|offer)-[A-Za-z0-9]{1,32}): "
    r"(\d+) bytes\]$"
)


def folder(directory: Path) -> Path:
    """Returns the attachment folder inside one project's state directory.

    Args:
        directory: Private state directory for the common repository.

    Returns:
        The folder every attachment of that project is kept in.
    """
    return directory / FOLDER


def validate(reference: object) -> str:
    """Accepts an opaque attachment reference and refuses anything else.

    A reference is a record kind, a dash and an alphanumeric identifier. It
    is never a path: separators, dots and every other character are refused
    before the reference reaches the file system, so a reference cannot
    name a file outside the attachment folder.

    Args:
        reference: Value a caller presented as a reference.

    Returns:
        The validated reference.

    Raises:
        BridgeError: If the value is not a well-formed reference.
    """
    if not isinstance(reference, str) or not REFERENCE.match(reference):
        raise BridgeError(
            "reference must read kind-identifier, such as message-12."
        )
    return reference


def reference(kind: str, identifier: object) -> str:
    """Builds the reference an attachment is keyed by.

    Args:
        kind: Record kind, one of message, report or offer.
        identifier: Identifier of the record the attachment belongs to.

    Returns:
        The validated reference.
    """
    return validate(f"{kind}-{identifier}")


def marker(reference: str, size: int) -> str:
    """Returns the closing line a spilled record carries in place of its tail.

    Args:
        reference: Attachment the record refers to.
        size: Full size of the spilled body in UTF-8 bytes.

    Returns:
        The marker line, which names the reference and the byte count.
    """
    return f"[attachment {reference}: {size} bytes]"


def find(text: str) -> tuple[str, int] | None:
    """Reads the attachment reference a bounded record ends with, if any.

    Args:
        text: Stored body of a message, report field or handoff summary.

    Returns:
        The reference and the byte count it was recorded with, or None when
        the text carries no attachment.
    """
    found = MARKER.search(text.rstrip())
    if not found:
        return None
    return found.group(1), int(found.group(2))


def bounded(kind: str, identifier: object, text: str, cap: int) -> str:
    """Returns the slice of an oversized body that a record keeps.

    The slice is the longest UTF-8 prefix that, followed by the marker line,
    fits within the record's own cap, so the record stays exactly as bounded
    as an unattached one and still ends with the reference and byte count.

    Args:
        kind: Record kind, one of message, report or offer.
        identifier: Identifier of the record the attachment belongs to.
        text: Full body being spilled.
        cap: Record cap in UTF-8 bytes.

    Returns:
        The bounded body the record stores.
    """
    tail = "\n\n" + marker(reference(kind, identifier), len(text.encode()))
    budget = max(cap - len(tail.encode()), 0)
    return text.encode()[:budget].decode(errors="ignore").rstrip() + tail


def used(directory: Path, writer: str) -> int:
    """Sums the attachment bytes one lane currently holds.

    Args:
        directory: Private state directory for the common repository.
        writer: Participant whose attachments are counted.

    Returns:
        Total recorded size of that lane's attachments in bytes; a released
        attachment holds none.
    """
    total = 0
    for path in folder(directory).glob("*.json"):
        try:
            meta = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if meta.get("writer") == writer and not meta.get("released"):
            total += int(meta.get("bytes", 0))
    return total


def release(directory: Path, reference: str, writer: str) -> int:
    """Frees one settled attachment of a writer from its allowance.

    The body is deleted and the metadata stays marked released, so a later
    read names why the body is gone instead of reporting it unknown. The
    record keeps its bounded first slice.

    Args:
        directory: Private state directory for the common repository.
        reference: Attachment reference to release.
        writer: Participant that must have written it.

    Returns:
        Bytes freed; zero when the attachment is absent, already released
        or written by someone else.
    """
    if not isinstance(reference, str) or not REFERENCE.match(reference):
        return 0
    home = folder(directory)
    meta_path = _path(home, reference, "json")
    try:
        meta = json.loads(meta_path.read_text())
    except (OSError, ValueError):
        return 0
    if meta.get("writer") != writer or meta.get("released"):
        return 0
    _path(home, reference, "md").unlink(missing_ok=True)
    write_json(meta_path, {**meta, "released": True})
    return int(meta.get("bytes", 0))


def keep(
    directory: Path,
    kind: str,
    identifier: object,
    text: str,
    writer: str,
    readers: list[str],
    settled: Iterable[str] = (),
) -> str:
    """Stores one body whole beside the record that refers to it.

    A handoff payload such as a diff is kept in full rather than clipped,
    because the record refers to it instead of carrying it. The caller decides
    whether a body is worth keeping; this function only bounds it against the
    attachment cap and the writer's allowance. Messages are never pruned, so
    a full allowance is freed by releasing the writer's settled attachments,
    oldest first, only as far as the new body needs.

    Args:
        directory: Private state directory for the common repository.
        kind: Record kind, one of message, report or offer.
        identifier: Identifier the attachment is keyed by.
        text: Full body to retain.
        writer: Participant that wrote the body.
        readers: Participants allowed to read it.
        settled: References of the writer's attachments every reader has
            finished with, oldest first; they may be released to make room.

    Returns:
        The reference the record refers to the stored body by.

    Raises:
        BridgeError: If the body exceeds the attachment cap or the writer's
            total attachment allowance after releasing settled attachments.
    """
    size = len(text.encode())
    if size > MAX_ATTACHMENT_BYTES:
        raise BridgeError(
            f"{kind} body is {size} bytes; an attachment is capped at "
            f"{MAX_ATTACHMENT_BYTES} bytes."
        )
    held = used(directory, writer)
    for ref in settled:
        if held + size <= MAX_LANE_BYTES:
            break
        held -= release(directory, ref, writer)
    if held + size > MAX_LANE_BYTES:
        raise BridgeError(
            f"{writer} holds its {MAX_LANE_BYTES}-byte attachment "
            "allowance in bodies its readers have not finished with; "
            "send a shorter body, or wait until its older long messages "
            "are read and its older reports are pruned."
        )
    ref = reference(kind, identifier)
    home = folder(directory)
    home.mkdir(parents=True, exist_ok=True)
    write_text(_path(home, ref, "md"), text)
    write_json(
        _path(home, ref, "json"),
        {
            "reference": ref,
            "kind": kind,
            "bytes": size,
            "writer": writer,
            "readers": sorted(set(readers)),
        },
    )
    return ref


def spill(
    directory: Path,
    kind: str,
    identifier: object,
    text: str,
    cap: int,
    writer: str,
    readers: list[str],
    settled: Iterable[str] = (),
) -> tuple[str, str]:
    """Stores a body above its cap as an attachment and bounds the record.

    A body within the cap is returned unchanged and nothing is written. A
    body above it is written whole under the attachment folder, keyed by the
    record's reference, and the returned record body is the bounded slice
    ending with that reference and the byte count.

    Args:
        directory: Private state directory for the common repository.
        kind: Record kind, one of message, report or offer.
        identifier: Identifier of the record the attachment belongs to.
        text: Full body the caller submitted.
        cap: Record cap in UTF-8 bytes.
        writer: Participant that wrote the body.
        readers: Participants the record is addressed to.
        settled: The writer's releasable attachments, oldest first; see
            `keep`.

    Returns:
        The body the record stores and the reference, which is empty when
        nothing was attached.

    Raises:
        BridgeError: If the body exceeds the attachment cap or the writer's
            total attachment allowance.
    """
    if len(text.encode()) <= cap:
        return text, ""
    ref = keep(directory, kind, identifier, text, writer, readers, settled)
    return bounded(kind, identifier, text, cap), ref


def body(directory: Path, reference: str, name: str) -> str:
    """Reads one attachment whole for a participant allowed to see it.

    Args:
        directory: Private state directory for the common repository.
        reference: Attachment reference as the record carries it.
        name: Participant reading; only the writer and the addressed
            participants may read.

    Returns:
        The full attachment body.

    Raises:
        BridgeError: If the reference is malformed, unknown, or not
            addressed to or written by the reader.
    """
    ref = validate(reference)
    home = folder(directory)
    try:
        meta = json.loads(_path(home, ref, "json").read_text())
    except (OSError, ValueError):
        raise BridgeError(
            f"No attachment {ref} is readable by {name}."
        ) from None
    if name != meta.get("writer") and name not in meta.get("readers", []):
        raise BridgeError(f"No attachment {ref} is readable by {name}.")
    if meta.get("released"):
        raise BridgeError(
            f"Attachment {ref} was released after every recipient read it "
            "to free its writer's allowance; the record keeps its first "
            "slice."
        )
    try:
        return _path(home, ref, "md").read_text()
    except OSError:
        raise BridgeError(
            f"No attachment {ref} is readable by {name}."
        ) from None


def page(directory: Path, reference: str, name: str, offset: int = 0) -> dict:
    """Reads one bounded page of an attachment.

    Args:
        directory: Private state directory for the common repository.
        reference: Attachment reference as the record carries it.
        name: Participant reading.
        offset: Character offset the page starts at.

    Returns:
        The reference, the full byte count, the page and the offset of the
        next page when more remains.

    Raises:
        BridgeError: If the reference is unreadable by this participant.
    """
    text = body(directory, reference, name)
    result = {
        "reference": reference,
        "bytes": len(text.encode()),
        "offset": offset,
        "body": text[offset : offset + PAGE_CHARACTERS],
    }
    if offset + PAGE_CHARACTERS < len(text):
        result["next_offset"] = offset + PAGE_CHARACTERS
    return result


def remove(directory: Path, reference: str) -> None:
    """Deletes an attachment beside the record that referred to it.

    Args:
        directory: Private state directory for the common repository.
        reference: Attachment reference; a malformed or absent one is
            ignored so pruning never fails on it.
    """
    if not isinstance(reference, str) or not REFERENCE.match(reference):
        return
    home = folder(directory)
    for suffix in ("md", "json"):
        _path(home, reference, suffix).unlink(missing_ok=True)


def retarget(text: str, reference: str) -> str:
    """Points the marker a bounded record ends with at another reference.

    Args:
        text: Stored body of a record.
        reference: Reference the marker names from now on.

    Returns:
        The body with its marker renamed, or the body unchanged when it
        carries no marker.
    """
    stripped = text.rstrip()
    found = MARKER.search(stripped)
    if not found:
        return text
    size = int(found.group(2))
    return stripped[: found.start()] + marker(validate(reference), size)


def renumber(directory: Path, moves: list[tuple[str, str]]) -> None:
    """Re-keys attachments whose records were given new identifiers.

    Every moved file is first parked under a hidden name, so one move never
    lands on a file another move is about to vacate. A destination that is
    still occupied afterwards belongs to a record outside the moves and is
    refused rather than overwritten.

    Args:
        directory: Private state directory for the common repository.
        moves: Pairs of the reference a record had and the one it now has.

    Raises:
        BridgeError: If a destination already holds an attachment.
    """
    home = folder(directory)
    parked = []
    for old, new in moves:
        for suffix in ("md", "json"):
            source = _path(home, validate(old), suffix)
            if source.exists():
                hidden = home / f".renumber-{old}.{suffix}"
                source.rename(hidden)
                parked.append((hidden, _path(home, validate(new), suffix)))
    for hidden, target in parked:
        if target.exists():
            raise BridgeError(
                f"Attachment {target.stem} already exists; the import would "
                "overwrite it."
            )
        if target.suffix == ".json":
            meta = json.loads(hidden.read_text())
            meta["reference"] = target.stem
            write_json(target, meta)
            hidden.unlink()
        else:
            hidden.rename(target)


def _path(home: Path, reference: str, suffix: str) -> Path:
    """Resolves a validated reference to a file inside the folder.

    Args:
        home: Attachment folder.
        reference: Validated reference.
        suffix: File extension without its dot.

    Returns:
        The file path.

    Raises:
        BridgeError: If the resolved path would leave the folder.
    """
    path = home / f"{reference}.{suffix}"
    if path.resolve().parent != home.resolve():
        raise BridgeError("Attachment reference resolves outside its folder.")
    return path
