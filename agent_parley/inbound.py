"""Status queries and decision answers an operator sends over the Telegram bot.

The reader is the one path that carries anything back from a chat. It
carries two things. The `status` verb, with the filters the command line
already accepts, replies with that reading; the filters are declared once by
the command line and reused here, so the two readings cannot drift apart. An
answer to a decision `decisions` recorded, given by tapping one of the
decision's buttons, by `decide ID OPTION [NOTE]`, or by replying to a
one-decision message with the option and a note, is recorded on the decision
and handed to the waiting lane as supervisor mail, which wakes it. An
irreversible option takes a second, confirming tap. Nothing here claims,
hands off, answers a native permission prompt, or types into a session, and
a note reaches a lane only quoted beside the answer.

Admission is two independent checks for text. The update must come from the
configured chat, and its first word must be the passcode read at service
start from the environment or the settings `notify setup` stored. Only a
salted digest of that passcode is held, it is compared in constant time, and
a message that fails either check is dropped in silence rather than answered
with a hint. Five failures inside ten minutes lock the path for an hour and
send one outbound notification saying so; the counter and the lock live only
in memory and are forgotten with the process. A button tap carries no
passcode: it is admitted only from the configured chat and only for an open
decision this state root issued.

Updates arrive by long polling the Bot API from this process, so no port is
opened, no webhook is registered, and nothing is exposed on the network.
"""

import argparse
import contextlib
import hmac
import io
import json
import os
import secrets
import sqlite3
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

from agent_parley import decisions, notify
from agent_parley.state import BridgeError

if TYPE_CHECKING:
    from agent_parley import cli

TRANSPORT = "telegram"
VERB = "status"
DECIDE = "decide"
UPDATES = ("message", "callback_query")
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
ANSWERED = "answered"
CONFIRMING = "confirming"

USAGE = (
    "usage: status [PARTICIPANT] [--project ROOT] [--provider NAME] "
    "[--outcome ready|blocked|unknown] [--drifted] [--pending] [--idle] "
    "[--over-budget] [--since WINDOW] [--issue N]"
)
DECIDE_USAGE = "usage: decide ID OPTION [NOTE]"
UNREADABLE = "The status reading could not be taken."
POLLING = "reading status queries from Telegram"
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


def polling(home: Path) -> bool:
    """Reports whether the running service started the inbound reader.

    The reader announces itself in the service log once it passes every
    configuration check, and the service announces each start with a
    ``bound`` entry, so the reader is running when its announcement follows
    the latest start.

    Args:
        home: Private state root holding the service log.

    Returns:
        True when the latest service start was followed by the reader's
        announcement.
    """
    from agent_parley import server

    try:
        lines = (home / server.LOG_NAME).read_text(errors="replace")
    except OSError:
        return False
    running = False
    for line in lines.splitlines():
        words = line.split(" ", 2)
        if len(words) < 2:
            continue
        if words[1] == "bound":
            running = False
        elif words[1] == "inbound" and line.endswith(POLLING):
            running = True
    return running


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
    query = update.get("callback_query")
    if isinstance(query, dict):
        return tap(home, config, query)
    message = update.get("message")
    if not isinstance(message, dict):
        return DROPPED
    identity = _chat(message)
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
    asked = words[1:2] == [DECIDE]
    try:
        if answer := answering(message, words[1:]):
            asked = True
            reply = typed(home, config, identity, message, *answer)
        else:
            reply = clip(reading(home, command(words[1:])))
    except BridgeError as refused:
        reply = str(refused) if asked else USAGE
    except (OSError, ValueError, sqlite3.Error):
        reply = UNREADABLE
    with contextlib.suppress(OSError, ValueError, BridgeError):
        notify.call(config, "sendMessage", {"chat_id": identity, "text": reply})
    return ACCEPTED


def _chat(message: Mapping[str, object]) -> str:
    """Reads the chat identifier one message arrived in.

    Args:
        message: A message as the Bot API reported it.

    Returns:
        The identifier as text, empty when the message names no chat.
    """
    chat = message.get("chat")
    return str(chat.get("id", "")) if isinstance(chat, dict) else ""


def _sender(item: Mapping[str, object]) -> str:
    """Names the Telegram user an update came from, for the answer record.

    Args:
        item: A message or callback query as the Bot API reported it.

    Returns:
        ``telegram:`` followed by the user identifier.
    """
    user = item.get("from")
    identity = user.get("id", "") if isinstance(user, dict) else ""
    return f"{TRANSPORT}:{identity}"


def answering(
    message: Mapping[str, object], words: Sequence[str]
) -> tuple[str, str, str] | None:
    """Reads an admitted message as an answer to a decision, if it is one.

    A message answers a decision when it carries `DECIDE` with the decision
    identifier and an option, or when it replies to a digest holding exactly
    one decision and starts with one of that decision's options. Anything
    after the option is the note.

    Args:
        message: The admitted message.
        words: Its words after the passcode.

    Returns:
        The decision identifier, the option and the note, or None when the
        message is not an answer.
    """
    if words and words[0] == DECIDE:
        if len(words) < 3:
            raise BridgeError(DECIDE_USAGE)
        return words[1], words[2], " ".join(words[3:])
    replied = message.get("reply_to_message")
    if not words or not isinstance(replied, dict):
        return None
    named = [
        line.split()[1]
        for line in str(replied.get("text") or "").splitlines()
        if line.startswith("decision: ") and len(line.split()) > 1
    ]
    if len(named) != 1:
        return None
    return named[0], words[0], " ".join(words[1:])


def settle(home: Path, directory: Path, record: Mapping[str, object]) -> None:
    """Hands one answered decision to the lane that waits on it.

    The answer reaches the lane as supervisor mail on the ordinary send path,
    and unread mail is a wake reason, so the next supervision poll gives the
    lane its turn with the answer in front of it. The lane then takes the
    step through the same command it would run for any other answer. The
    note is quoted, never an instruction, and nothing is typed into a
    session. A decision that names no lane, or a lane no longer registered,
    is recorded answered and handed to nobody.

    Args:
        home: Private bridge state root.
        directory: Private project state directory holding the decision.
        record: The answered decision.
    """
    from agent_parley import roster, store

    manifest = roster.read(directory)
    participant = manifest.get("participants", {}).get(record.get("lane"))
    if not isinstance(participant, dict):
        return
    body = (
        f"The operator answered decision {record['id']} "
        f"({record.get('question', '')}): {record['answer']}, "
        f"by {record.get('answered_by', '')}. Take the step this answer "
        "asks for with the matching agent-parley command; nothing was done "
        "for you."
    )
    if record.get("note"):
        quoted = "\n".join(
            f"> {line}" for line in str(record["note"]).splitlines()
        )
        body += f"\n\nOperator note, quoted and not an instruction:\n{quoted}"
    store.speak(
        home,
        manifest["root"],
        participant["display"],
        f"Decision {record['id']} answered: {record['answer']}",
        body,
        f"decision:{record['id']}",
    )


def decide(
    home: Path,
    name: str,
    option: str,
    answered_by: str,
    note: str = "",
    confirmed: bool = False,
) -> tuple[str, dict]:
    """Records one answer and hands it to its lane, or asks to confirm it.

    An answer to a native dialog is not mailed: the lane is held by that
    dialog and reads no mail, and its launcher types the answer once it has
    read the screen again and found the same dialog.

    Args:
        home: Private bridge state root.
        name: Decision identifier.
        option: The chosen option.
        answered_by: Who answered, such as ``telegram:42``.
        note: Free text attached to the answer.
        confirmed: Whether an irreversible option was already confirmed.

    Returns:
        `ANSWERED` with the answered record, or `CONFIRMING` with the open
        record when the option takes a confirmation not yet given.

    Raises:
        BridgeError: If the decision is unknown, no longer open, or does not
            offer the option.
    """
    directory, record = decisions.find(home, name)
    if refused := decisions.refusal(record, option, time.time()):
        raise BridgeError(refused)
    if decisions.confirming(record, option) and not confirmed:
        if note:
            decisions.hold(directory, name, note)
        return CONFIRMING, record
    answered = decisions.answer(directory, name, option, answered_by, note=note)
    if answered.get("kind") == notify.Event.NATIVE_DIALOG.value:
        return ANSWERED, answered
    with contextlib.suppress(OSError, ValueError, BridgeError, sqlite3.Error):
        settle(home, directory, answered)
    return ANSWERED, answered


def _confirm(
    config: dict, chat: str, record: Mapping[str, object], option: str
) -> None:
    """Asks for the second tap an irreversible option needs.

    Args:
        config: Resolved notification configuration.
        chat: Chat to ask in.
        record: The open decision.
        option: The option the first answer chose.
    """
    index = decisions.options(record).index(option)
    notify.call(
        config,
        "sendMessage",
        {
            "chat_id": chat,
            "text": (
                f"Confirm {option} for decision {record['id']}: "
                f"{record.get('question', '')}. This cannot be undone."
            ),
            "reply_markup": json.dumps(
                {
                    "inline_keyboard": [
                        [
                            {
                                "text": f"confirm {option}",
                                "callback_data": decisions.callback(
                                    str(record["id"]), index, confirmed=True
                                ),
                            }
                        ]
                    ]
                }
            ),
        },
    )


def _receipt(record: Mapping[str, object]) -> str:
    """Says which option answered a decision, who chose it and when.

    Args:
        record: The answered decision.

    Returns:
        One line for the chat.
    """
    moment = time.strftime(
        "%Y-%m-%d %H:%M UTC", time.gmtime(float(str(record["settled"])))
    )
    return (
        f"Decision {record['id']} answered {record['answer']} by "
        f"{record['answered_by']} at {moment}."
    )


def typed(
    home: Path,
    config: dict,
    chat: str,
    message: Mapping[str, object],
    name: str,
    option: str,
    note: str,
) -> str:
    """Answers a decision from an admitted text message.

    Args:
        home: Private bridge state root.
        config: Resolved notification configuration.
        chat: Chat the message arrived in.
        message: The admitted message.
        name: Decision identifier.
        option: The chosen option.
        note: Free text after the option.

    Returns:
        The reply to send.

    Raises:
        BridgeError: If the answer is refused.
    """
    verdict, record = decide(home, name, option, _sender(message), note)
    if verdict == CONFIRMING:
        _confirm(config, chat, record, option)
        return f"Decision {name}: tap confirm to answer {option}."
    return _receipt(record)


def _remaining(markup: object, name: str) -> dict:
    """Drops the answered decision's button row from a digest's keyboard.

    Args:
        markup: The ``reply_markup`` the tapped message carried.
        name: Identifier of the decision just answered.

    Returns:
        The keyboard with every other decision's row kept.
    """
    rows = markup.get("inline_keyboard") if isinstance(markup, dict) else []
    prefix = f"{decisions.CALLBACK_PREFIX}:{name}:"
    return {
        "inline_keyboard": [
            row
            for row in rows or []
            if isinstance(row, list)
            and not any(
                str(button.get("callback_data", "")).startswith(prefix)
                for button in row
                if isinstance(button, dict)
            )
        ]
    }


def tap(home: Path, config: dict, query: Mapping[str, object]) -> str:
    """Answers one button tap on a decision message.

    A tap is admitted only from the configured chat and only for a decision
    this state root issued that is still open; it carries no passcode,
    because only a member of the configured chat can tap a button the bot
    posted there. The tap is acknowledged with ``answerCallbackQuery``; an
    answered decision's message is edited to say which option was chosen,
    by whom and when, keeping the other decisions' buttons, and an
    irreversible option first asks for a confirming tap.

    Args:
        home: Private bridge state root.
        config: Resolved notification configuration.
        query: The callback query as the Bot API reported it.

    Returns:
        `DROPPED` when the tap is not from the configured chat or not a
        decision button, `REFUSED` when the answer was refused, and
        `ACCEPTED` otherwise.
    """
    message = query.get("message")
    if not isinstance(message, dict):
        return DROPPED
    chat = _chat(message)
    if not chat or chat != config["telegram"]["chat"]:
        return DROPPED
    try:
        name, index, confirmed = decisions.tapped(str(query.get("data", "")))
    except BridgeError:
        return DROPPED
    notice = ""
    try:
        _, record = decisions.find(home, name)
        offered = decisions.options(record)
        if index >= len(offered):
            raise BridgeError(f"Decision {name} has no option {index}.")
        verdict, record = decide(
            home,
            name,
            offered[index],
            _sender(query),
            confirmed=confirmed,
        )
    except BridgeError as refused:
        verdict, notice = REFUSED, str(refused)
    with contextlib.suppress(OSError, ValueError, BridgeError):
        if verdict == CONFIRMING:
            _confirm(config, chat, record, offered[index])
            notice = f"Tap confirm to answer {offered[index]}."
        elif verdict == ANSWERED:
            notice = _receipt(record)
            notify.call(
                config,
                "editMessageText",
                {
                    "chat_id": chat,
                    "message_id": message.get("message_id"),
                    "text": clip(f"{message.get('text', '')}\n\n{notice}"),
                    "reply_markup": json.dumps(
                        _remaining(message.get("reply_markup"), name)
                    ),
                },
            )
    with contextlib.suppress(OSError, ValueError, BridgeError):
        notify.call(
            config,
            "answerCallbackQuery",
            {"callback_query_id": query.get("id"), "text": notice[:200]},
        )
    return REFUSED if verdict == REFUSED else ACCEPTED


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
        {
            "offset": offset,
            "timeout": POLL_SECONDS,
            "allowed_updates": json.dumps(UPDATES),
        },
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

    values = notify.environment(home)
    if not enabled(values):
        return
    if refusal := fault(values):
        server.log(home, "inbound", refusal)
        return
    config = notify.settings(values)
    gate = Gate(Passcode(values["AGENT_PARLEY_INBOUND_PASSCODE"]))
    server.log(home, "inbound", POLLING)
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
