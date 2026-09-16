"""Read-only status queries a participant asks for over the Telegram bot.

The reader is the one path that carries anything back from a chat, and it
carries exactly one verb: `status`, with the filters the command line already
accepts. Nothing here claims, hands off, wakes, answers a native permission
prompt, or puts text into a session. The filters are declared once by the
command line and reused here, so the two readings cannot drift apart.

Admission is two independent checks. The update must come from the configured
chat, and its first word must be the passcode read from the environment at
service start. Only a salted digest of that passcode is held, it is compared in
constant time, and a message that fails either check is dropped in silence
rather than answered with a hint. Five failures inside ten minutes lock the
path for an hour and send one outbound notification saying so; the counter and
the lock live only in memory and are forgotten with the process.

Updates arrive by long polling the Bot API from this process, so no port is
opened, no webhook is registered, and nothing is exposed on the network.
"""

import argparse
import contextlib
import hmac
import io
import os
import secrets
import sqlite3
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

from agent_parley import notify
from agent_parley.state import BridgeError

if TYPE_CHECKING:
    from agent_parley import cli

TRANSPORT = "telegram"
VERB = "status"
MINIMUM_PASSCODE = 12
FAILURE_LIMIT = 5
FAILURE_WINDOW = 600.0
LOCK_SECONDS = 3600.0
POLL_SECONDS = 25
RETRY_SECONDS = 5.0
MAX_REPLY_CHARS = 4096
NOTICE_ROOM = 96

ACCEPTED = "accepted"
REFUSED = "refused"
LOCKED = "locked"
DROPPED = "dropped"

USAGE = (
    "usage: status [PARTICIPANT] [--project ROOT] [--provider NAME] "
    "[--outcome ready|blocked|unknown] [--drifted] [--pending] [--idle] "
    "[--over-budget] [--since WINDOW] [--issue N]"
)
UNREADABLE = "The status reading could not be taken."
LOCKED_DETAIL = (
    f"Inbound status queries are refused for {int(LOCK_SECONDS // 60)} "
    f"minutes after {FAILURE_LIMIT} wrong passcodes."
)


class Passcode:
    """Holds the inbound passcode as a salted digest and nothing else.

    The secret is read once, hashed with a per-process salt, and dropped. A
    comparison runs over the digests in constant time, so a chat message can
    neither recover the passcode from this object nor learn how much of a
    guess was right from how long the answer took.
    """

    def __init__(self, secret: str) -> None:
        """Hashes one passcode and forgets the text it was given.

        Args:
            secret: Passcode read from the environment.
        """
        self._salt = secrets.token_bytes(16)
        self._digest = self._hash(secret)

    def _hash(self, candidate: str) -> bytes:
        """Returns the salted digest of one candidate passcode.

        Args:
            candidate: Text to hash with this reader's salt.

        Returns:
            The digest to compare against the held one.
        """
        return hmac.digest(self._salt, candidate.encode(), "sha256")

    def matches(self, candidate: str) -> bool:
        """Reports whether one candidate is the configured passcode.

        Args:
            candidate: First word of an incoming message.

        Returns:
            Whether the candidate hashes to the held digest.
        """
        return hmac.compare_digest(self._digest, self._hash(candidate))


class Gate:
    """Admits an incoming message and locks the path after repeated failures.

    The gate holds the failure times of the current window and the instant a
    lock expires. Both live only in this process: a restart forgets them, and
    neither is written to coordination state or to any log.
    """

    def __init__(self, passcode: Passcode) -> None:
        """Starts an unlocked gate with no recorded failures.

        Args:
            passcode: Digest of the configured passcode.
        """
        self._passcode = passcode
        self._failures: list[float] = []
        self._until = 0.0

    def locked(self, now: float) -> bool:
        """Reports whether the path is inside a lock.

        Args:
            now: Monotonic instant to read the lock against.

        Returns:
            Whether an earlier run of failures still refuses every message.
        """
        return now < self._until

    def admits(self, candidate: str, now: float) -> str:
        """Decides one message's admission and records a failed passcode.

        Args:
            candidate: First word of the incoming message.
            now: Monotonic instant the message arrived.

        Returns:
            `ACCEPTED` when the passcode matched, `LOCKED` when this failure
            is the one that locked the path, and `REFUSED` otherwise. Every
            answer other than `ACCEPTED` means the caller replies nothing.
        """
        if self.locked(now):
            return REFUSED
        if self._passcode.matches(candidate):
            self._failures.clear()
            return ACCEPTED
        self._failures = [
            moment
            for moment in [*self._failures, now]
            if now - moment < FAILURE_WINDOW
        ]
        if len(self._failures) < FAILURE_LIMIT:
            return REFUSED
        self._failures.clear()
        self._until = now + LOCK_SECONDS
        return LOCKED


class Refusing(argparse.ArgumentParser):
    """Parses one inbound command line without exiting the process.

    Argparse reports a bad command line by printing to standard error and
    raising `SystemExit`, which would take the service down and leak a usage
    dump into the service log. Both are turned into one error the caller
    answers with a single usage line.
    """

    def error(self, message: str) -> NoReturn:
        """Raises the parse failure instead of printing and exiting.

        Args:
            message: Argparse's own account of the failure.

        Raises:
            BridgeError: Always.
        """
        raise BridgeError(message)

    def exit(self, status: int = 0, message: str | None = None) -> NoReturn:
        """Raises rather than ending the service the reader runs inside.

        Args:
            status: Exit status argparse asked for.
            message: Text argparse would have printed.

        Raises:
            BridgeError: Always.
        """
        raise BridgeError(message or "status refused the command line.")


def parser() -> argparse.ArgumentParser:
    """Builds the inbound parser from the command line's own status filters.

    Returns:
        A parser accepting exactly the participant and filters `agent-parley
        status` accepts, which refuses a bad command line by raising instead
        of exiting.
    """
    from agent_parley import cli

    made = Refusing(prog=VERB, add_help=False)
    cli.add_status_filters(made)
    return made


def command(tokens: Sequence[str]) -> "cli.Selection":
    """Reads one accepted message as the selection it asks for.

    Args:
        tokens: Words of the message after the passcode.

    Returns:
        The filters the message asked for.

    Raises:
        BridgeError: If the message names another verb or any token the
            status parser rejects.
    """
    from agent_parley import cli

    if not tokens or tokens[0] != VERB:
        raise BridgeError(USAGE)
    return cli.selected_status(parser().parse_args(list(tokens[1:])))


def reading(home: Path, selection: "cli.Selection") -> str:
    """Renders the status reading the command line prints for one selection.

    Args:
        home: Private state directory the reading is taken from.
        selection: Filters the message asked for.

    Returns:
        The same text `agent-parley status` writes to a redirected stream,
        which prints every column rather than fitting a terminal width.
    """
    from agent_parley import cli

    written = io.StringIO()
    with contextlib.redirect_stdout(written):
        cli.Bridge(home).status(selection, None)
    return written.getvalue()


def clip(text: str, limit: int = MAX_REPLY_CHARS) -> str:
    """Caps one reply at the message limit and says how much was cut.

    Args:
        text: Reading to send.
        limit: Characters one message may carry.

    Returns:
        The reading unchanged when it fits, or its leading rows followed by
        one line naming how many rows were left out.
    """
    if len(text) <= limit:
        return text
    rows = text.splitlines()
    kept: list[str] = []
    size = 0
    for row in rows:
        if size + len(row) + 1 > limit - NOTICE_ROOM:
            break
        kept.append(row)
        size += len(row) + 1
    kept.append(
        f"{len(rows) - len(kept)} more row(s) not shown; narrow the query "
        "with a filter."
    )
    return "\n".join(kept)


def enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Reports whether the environment asks for the inbound reader at all.

    Args:
        environ: Environment mapping to read; the process environment by
            default.

    Returns:
        Whether ``AGENT_PARLEY_INBOUND`` names a transport.
    """
    values = os.environ if environ is None else environ
    return bool(values.get("AGENT_PARLEY_INBOUND", "").strip())


def fault(environ: Mapping[str, str] | None = None) -> str:
    """Names the configuration fault that keeps the reader from starting.

    A reader that is asked for but cannot be trusted is a fault an operator
    has to see, so the refusal is reported here and printed by `status`
    rather than leaving a silently dead poller behind.

    Args:
        environ: Environment mapping to read; the process environment by
            default.

    Returns:
        One sentence naming what to fix, or an empty string when the reader
        is off or fully configured.
    """
    values = os.environ if environ is None else environ
    if not enabled(values):
        return ""
    named = values.get("AGENT_PARLEY_INBOUND", "").strip().lower()
    if named != TRANSPORT:
        return (
            f"AGENT_PARLEY_INBOUND names no such transport: {named}. "
            f"Available: {TRANSPORT}."
        )
    if len(values.get("AGENT_PARLEY_INBOUND_PASSCODE", "")) < MINIMUM_PASSCODE:
        return (
            "AGENT_PARLEY_INBOUND_PASSCODE must be set and at least "
            f"{MINIMUM_PASSCODE} characters; inbound status queries are off."
        )
    try:
        config = notify.settings(values)
    except BridgeError as exc:
        return str(exc)
    if not config["telegram"]["token"] or not config["telegram"]["chat"]:
        return (
            "Inbound status queries need AGENT_PARLEY_TELEGRAM_TOKEN and "
            "AGENT_PARLEY_TELEGRAM_CHAT."
        )
    return ""


def reported(environ: Mapping[str, str] | None = None) -> dict:
    """Describes the inbound reader for one status reading.

    Args:
        environ: Environment mapping to read; the process environment by
            default.

    Returns:
        Whether the reader was asked for and the configuration fault, if any,
        that stops it from running.
    """
    return {"enabled": enabled(environ), "fault": fault(environ)}


def announce(config: dict) -> None:
    """Sends the one outbound notification a fresh lock deserves.

    Args:
        config: Resolved notification configuration.
    """
    subject, body = notify.compose(
        notify.Event.INBOUND_LOCKED,
        {"event": notify.Event.INBOUND_LOCKED.value, "detail": LOCKED_DETAIL},
    )
    notify.send(config, subject, body)


def erase(config: dict, chat: str, identifier: object) -> None:
    """Removes the accepted message so its passcode leaves the chat history.

    Deletion is best effort: a bot without the permission, or a message older
    than the Bot API allows, leaves the message in place and changes nothing
    about the reply.

    Args:
        config: Resolved notification configuration.
        chat: Chat the message arrived in.
        identifier: Message identifier the update carried.
    """
    if not identifier:
        return
    with contextlib.suppress(OSError, ValueError, BridgeError):
        notify.call(
            config,
            "deleteMessage",
            {"chat_id": chat, "message_id": identifier},
        )


def serve(
    home: Path,
    config: dict,
    gate: Gate,
    update: Mapping[str, object],
    now: float | None = None,
) -> str:
    """Answers one Bot API update, or drops it without a reply.

    Args:
        home: Private state directory the reading is taken from.
        config: Resolved notification configuration.
        gate: Passcode and lockout state of this process.
        update: One update as the Bot API reported it.
        now: Monotonic instant the update arrived; read from the clock when
            omitted.

    Returns:
        `DROPPED` when the update is not a message from the configured chat,
        `REFUSED` when the passcode did not match, `LOCKED` when this message
        locked the path, and `ACCEPTED` when a reply was sent.
    """
    message = update.get("message")
    if not isinstance(message, dict):
        return DROPPED
    chat = message.get("chat")
    identity = str(chat.get("id", "")) if isinstance(chat, dict) else ""
    if not identity or identity != config["telegram"]["chat"]:
        return DROPPED
    words = str(message.get("text") or "").split()
    if not words:
        return DROPPED
    verdict = gate.admits(words[0], time.monotonic() if now is None else now)
    if verdict == LOCKED:
        announce(config)
        return LOCKED
    if verdict != ACCEPTED:
        return REFUSED
    erase(config, identity, message.get("message_id"))
    try:
        reply = clip(reading(home, command(words[1:])))
    except BridgeError:
        reply = USAGE
    except (OSError, ValueError, sqlite3.Error):
        reply = UNREADABLE
    with contextlib.suppress(OSError, ValueError, BridgeError):
        notify.call(config, "sendMessage", {"chat_id": identity, "text": reply})
    return ACCEPTED


def updates(config: dict, offset: int) -> list[dict]:
    """Reads the next batch of updates with one long poll.

    Args:
        config: Resolved notification configuration.
        offset: First update identifier still wanted, which acknowledges
            every earlier one.

    Returns:
        The updates the Bot API reported, oldest first.
    """
    answer = notify.call(
        config,
        "getUpdates",
        {"offset": offset, "timeout": POLL_SECONDS},
        timeout=POLL_SECONDS + notify.TIMEOUT,
    )
    reported_updates = answer.get("result")
    if not isinstance(reported_updates, list):
        return []
    return [item for item in reported_updates if isinstance(item, dict)]


def run(home: Path, stopped: threading.Event) -> None:
    """Polls for status queries until the local service stops.

    The reader starts only when the environment asks for it and every
    configuration fault is clear, so a missing or short passcode leaves no
    poller running and is reported by `status` instead. A failed poll is
    retried after a short pause rather than ending the reader, because the
    Bot API is a remote service and a service restart is not an operator's
    remedy for one dropped connection.

    Args:
        home: Private state directory the readings are taken from.
        stopped: Event the service sets when it is shutting down.
    """
    from agent_parley import server

    if not enabled():
        return
    if refusal := fault():
        server.log(home, "inbound", refusal)
        return
    config = notify.settings()
    gate = Gate(Passcode(os.environ["AGENT_PARLEY_INBOUND_PASSCODE"]))
    server.log(home, "inbound", "reading status queries from Telegram")
    offset = 0
    while not stopped.is_set():
        try:
            batch = updates(config, offset)
        except (OSError, ValueError, BridgeError):
            stopped.wait(RETRY_SECONDS)
            continue
        for update in batch:
            offset = max(offset, int(update.get("update_id", 0)) + 1)
            if stopped.is_set():
                return
            with contextlib.suppress(OSError, ValueError, BridgeError):
                serve(home, config, gate, update)
