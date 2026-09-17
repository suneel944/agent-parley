"""Outbound notification of the coordination changes an owner waits on.

The notifier reads the decision a checkpoint already recorded and the idle
stretch the supervisor already measured; it observes no new state of its own
and decides no outcome. Only a change that moves ownership or blocks a lane is
forwarded, so the signal stays as small as the event log's own selection.
Configuration and every secret arrive through environment variables and are
never written into coordination state. Delivery is outbound only: no transport
answers a native permission prompt or opens a port. The Telegram calls this
module makes are shared with the read-only reader in `inbound`, which can ask
for a status reading and can carry no other command back.
"""

import hashlib
import json
import os
import threading
from collections.abc import Callable, Mapping
from email.message import EmailMessage
from enum import StrEnum
from pathlib import Path

from agent_parley import checkpoints
from agent_parley.state import BridgeError, lock, write_json

MAX_MESSAGE_BYTES = 1536
MAX_SUBJECT_BYTES = 200
TIMEOUT = 10.0
JOIN_SECONDS = 5.0
TELEGRAM_API = "https://api.telegram.org"
TLS_MODES = ("starttls", "implicit", "none")
IMPLICIT_PORT = 465
SUBMISSION_PORT = 587


class Event(StrEnum):
    """Coordination changes worth an outbound notification."""

    HANDOFF_OFFERED = "handoff_offered"
    PERMISSION_PROMPT = "permission_prompt"
    LANE_IDLE = "lane_idle"
    RUN_FINISHED = "run_finished"
    HOOK_REFUSAL = "hook_refusal"
    INBOUND_LOCKED = "inbound_locked"


TITLES: dict[str, str] = {
    Event.HANDOFF_OFFERED: "A handoff offer is waiting",
    Event.PERMISSION_PROMPT: "A lane is blocked on a permission prompt",
    Event.LANE_IDLE: "A lane is idle with no claim",
    Event.RUN_FINISHED: "A lane run finished",
    Event.HOOK_REFUSAL: "A hook refused a lane action",
    Event.INBOUND_LOCKED: "Inbound status queries are locked",
}

KEY_FIELDS: dict[str, tuple[str, ...]] = {
    Event.HANDOFF_OFFERED: ("offer",),
    Event.PERMISSION_PROMPT: ("session", "tool"),
    Event.LANE_IDLE: ("since",),
    Event.RUN_FINISHED: ("session",),
    Event.HOOK_REFUSAL: ("session", "reason"),
    Event.INBOUND_LOCKED: ("detail",),
}

REFUSALS = frozenset({"branch_drift", "branch_switch"})

BODY_FIELDS = (
    "repo",
    "lane",
    "provider",
    "event",
    "issue",
    "offer",
    "detail",
)

_THREADS: list[threading.Thread] = []
_THREADS_LOCK = threading.Lock()


def enabled() -> bool:
    """Reports whether any transport is named in the environment.

    The reading costs one environment lookup and no validation, so a caller
    on the hook or supervision path can skip the notifier's work entirely
    without paying for a configuration parse.

    Returns:
        True when ``AGENT_PARLEY_NOTIFY`` names at least one transport.
    """
    return bool(os.environ.get("AGENT_PARLEY_NOTIFY", "").strip())


def _port(values: Mapping[str, str], mode: str) -> int:
    """Resolves the SMTP port from the environment or the TLS mode.

    Args:
        values: Environment mapping the settings are read from.
        mode: Resolved TLS mode.

    Returns:
        The configured port, the implicit-TLS port for an implicit session,
        or the submission port otherwise.

    Raises:
        BridgeError: If the configured port is not a positive integer.
    """
    raw = values.get("AGENT_PARLEY_SMTP_PORT", "").strip()
    if not raw:
        return IMPLICIT_PORT if mode == "implicit" else SUBMISSION_PORT
    if not raw.isdigit() or int(raw) == 0:
        raise BridgeError("AGENT_PARLEY_SMTP_PORT must be a positive integer.")
    return int(raw)


def settings(environ: Mapping[str, str] | None = None) -> dict:
    """Resolves the notification configuration from the environment.

    Args:
        environ: Environment mapping to read; the process environment by
            default.

    Returns:
        The selected transport names and the credentials and addresses each
        transport needs. An empty transport list means notification is off.

    Raises:
        BridgeError: If a transport name or the TLS mode is not recognised,
            or the SMTP port is not a positive integer.
    """
    values = os.environ if environ is None else environ
    names = [
        name.strip().lower()
        for name in values.get("AGENT_PARLEY_NOTIFY", "").split(",")
        if name.strip()
    ]
    unknown = sorted({name for name in names if name not in TRANSPORTS})
    if unknown:
        raise BridgeError(
            "AGENT_PARLEY_NOTIFY names no such transport: "
            + ", ".join(unknown)
            + ". Available: "
            + ", ".join(sorted(TRANSPORTS))
            + "."
        )
    mode = values.get("AGENT_PARLEY_SMTP_TLS", "starttls").strip().lower()
    if mode not in TLS_MODES:
        raise BridgeError(
            "AGENT_PARLEY_SMTP_TLS must be one of: " + ", ".join(TLS_MODES)
        )
    recipients = [
        address.strip()
        for address in values.get("AGENT_PARLEY_SMTP_TO", "").split(",")
        if address.strip()
    ]
    return {
        "transports": names,
        "telegram": {
            "token": values.get("AGENT_PARLEY_TELEGRAM_TOKEN", "").strip(),
            "chat": values.get("AGENT_PARLEY_TELEGRAM_CHAT", "").strip(),
            "api": values.get("AGENT_PARLEY_TELEGRAM_API", TELEGRAM_API),
        },
        "email": {
            "host": values.get("AGENT_PARLEY_SMTP_HOST", "").strip(),
            "port": _port(values, mode),
            "user": values.get("AGENT_PARLEY_SMTP_USER", "").strip(),
            "password": values.get("AGENT_PARLEY_SMTP_PASSWORD", ""),
            "sender": values.get("AGENT_PARLEY_SMTP_FROM", "").strip(),
            "recipients": recipients,
            "tls": mode,
        },
    }


def select(
    event: str, reason: str, context: Mapping[str, object]
) -> Event | None:
    """Chooses the notification one recorded checkpoint decision deserves.

    A refusal is reported before anything else, because it is the decision
    that stopped the lane. An idle stretch is not decided here: it has no
    native event of its own and is measured by the supervisor instead.

    Args:
        event: Native lifecycle event name the checkpoint observed.
        reason: Enumerated cause recorded for the decision.
        context: Situation fields, including any waiting ``offer``.

    Returns:
        The notification to send, or None when the decision changes nothing
        the owner has to hear about.
    """
    if reason in REFUSALS:
        return Event.HOOK_REFUSAL
    if event == "PermissionRequest":
        return Event.PERMISSION_PROMPT
    if event == "SessionEnd":
        return Event.RUN_FINISHED
    if context.get("offer"):
        return Event.HANDOFF_OFFERED
    return None


def offered(ledger: Mapping[str, object], agent: str) -> dict:
    """Reports the handoff offer the ledger records as waiting on one lane.

    Args:
        ledger: Published issue ledger snapshot.
        agent: Lane an offer must name as its recipient.

    Returns:
        The issue number and offer identifier of the first waiting offer, or
        an empty mapping when no offer names the lane.
    """
    issues = ledger.get("issues") or {}
    if not isinstance(issues, dict):
        return {}
    for number in sorted(issues):
        offer = issues[number].get("offer") or {}
        if offer.get("to") == agent:
            return {"issue": number, "offer": str(offer.get("id", ""))}
    return {}


def compose(event: str, fields: Mapping[str, object]) -> tuple[str, str]:
    """Renders one notification as a subject line and a bounded body.

    The body carries one field per line in a fixed order, so a message read
    on a phone reports the repository, lane, provider, event and the issue or
    offer identifier without scrolling. An empty field is left out rather
    than printed blank.

    Args:
        event: Notification name, or any label a test message carries.
        fields: Values to report; unknown keys are ignored.

    Returns:
        The subject and the body, each clipped to its byte cap without
        splitting a multibyte character.
    """
    title = TITLES.get(event, "Agent Parley notification")
    lane = str(fields.get("lane", ""))
    subject = f"Agent Parley: {title}" + (f" ({lane})" if lane else "")
    lines = [title]
    lines += [
        f"{name}: {fields[name]}"
        for name in BODY_FIELDS
        if str(fields.get(name, ""))
    ]
    return (
        checkpoints.clip(subject, MAX_SUBJECT_BYTES),
        checkpoints.clip("\n".join(lines), MAX_MESSAGE_BYTES),
    )


def call(
    config: dict,
    method: str,
    fields: Mapping[str, object],
    timeout: float = TIMEOUT,
) -> dict:
    """Calls one Telegram Bot API method and returns its decoded answer.

    Every Telegram exchange in the runtime goes through this one call, so a
    bot token is read from the same configuration, posted the same way and
    kept out of the query string whether the message is being sent or an
    update is being read. The HTTP client is imported here rather than at
    module scope, so a command line that never reaches Telegram does not pay
    for it at startup.

    Args:
        config: Resolved notification configuration.
        method: Bot API method name, such as ``sendMessage``.
        fields: Form fields the method takes.
        timeout: Seconds to wait for the answer; a long poll passes its own.

    Returns:
        The decoded answer document, or an empty mapping when the answer is
        not a JSON object.

    Raises:
        BridgeError: If the bot token or chat identifier is missing, or the
            API answers with a status other than 200.
        OSError: If the request cannot be completed.
    """
    import urllib.parse
    import urllib.request

    values = config["telegram"]
    if not values["token"] or not values["chat"]:
        raise BridgeError(
            "Telegram needs AGENT_PARLEY_TELEGRAM_TOKEN and "
            "AGENT_PARLEY_TELEGRAM_CHAT."
        )
    data = urllib.parse.urlencode(dict(fields)).encode()
    request = urllib.request.Request(
        f"{values['api']}/bot{values['token']}/{method}",
        data=data,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise BridgeError(f"Telegram answered {response.status}.")
        answer = json.loads(response.read() or b"{}")
    return answer if isinstance(answer, dict) else {}


def telegram(config: dict, subject: str, body: str) -> None:
    """Posts one message to the Telegram Bot API.

    Args:
        config: Resolved notification configuration.
        subject: Subject line, sent as the message's first line.
        body: Message body.

    Raises:
        BridgeError: If the bot token or chat identifier is missing, or the
            API answers with a status other than 200.
        OSError: If the request cannot be completed.
    """
    call(
        config,
        "sendMessage",
        {"chat_id": config["telegram"]["chat"], "text": f"{subject}\n\n{body}"},
    )


def email(config: dict, subject: str, body: str) -> None:
    """Sends one message over SMTP with STARTTLS or implicit TLS.

    The SMTP client is imported here rather than at module scope, so a
    command line that never notifies does not pay for it at startup.

    Args:
        config: Resolved notification configuration.
        subject: Subject header.
        body: Plain-text body.

    Raises:
        BridgeError: If the host, sender or recipient list is missing.
        OSError: If the session cannot be established.
        smtplib.SMTPException: If the server refuses the session or message.
    """
    import smtplib

    values = config["email"]
    if not values["host"]:
        raise BridgeError("Email needs AGENT_PARLEY_SMTP_HOST.")
    if not values["sender"] or not values["recipients"]:
        raise BridgeError(
            "Email needs AGENT_PARLEY_SMTP_FROM and AGENT_PARLEY_SMTP_TO."
        )
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = values["sender"]
    message["To"] = ", ".join(values["recipients"])
    message.set_content(body)
    session = (
        smtplib.SMTP_SSL(values["host"], values["port"], timeout=TIMEOUT)
        if values["tls"] == "implicit"
        else smtplib.SMTP(values["host"], values["port"], timeout=TIMEOUT)
    )
    with session:
        if values["tls"] == "starttls":
            session.starttls()
        if values["user"]:
            session.login(values["user"], values["password"])
        session.send_message(message)


Transport = Callable[[dict, str, str], None]

TRANSPORTS: dict[str, Transport] = {"telegram": telegram, "email": email}


def send(config: dict, subject: str, body: str) -> list[dict]:
    """Delivers one message on every configured transport.

    Delivery is best effort: a transport that fails is reported and the next
    one is still attempted. Nothing is queued for a later retry. Every SMTP
    failure is an ``OSError``, so one reading covers both transports.

    Args:
        config: Resolved notification configuration.
        subject: Subject line.
        body: Message body.

    Returns:
        One result per configured transport, in configuration order, each
        naming the transport, whether it accepted the message, and the
        failure text when it did not.
    """
    results = []
    for name in config["transports"]:
        detail = ""
        try:
            TRANSPORTS[name](config, subject, body)
        except (OSError, ValueError, BridgeError) as exc:
            detail = f"{type(exc).__name__}: {exc}"
        results.append({"transport": name, "ok": not detail, "error": detail})
    return results


def _claim(
    directory: Path, agent: str, event: Event, fields: Mapping[str, object]
) -> bool:
    """Records that one situation has notified, and reports whether it is new.

    The lane's notification marker holds a digest per event, so a situation
    that has not changed sends nothing further. The marker mirrors the
    checkpoint's own suppression of unchanged state and carries no secret.

    Args:
        directory: Private project state directory.
        agent: Lane the notification reports on.
        event: Notification being considered.
        fields: Situation fields the event's key is taken from.

    Returns:
        True when the situation is new and the caller should send.
    """
    key = "\x00".join(str(fields.get(name, "")) for name in KEY_FIELDS[event])
    digest = hashlib.sha256(key.encode()).hexdigest()[:16]
    path = directory / f"{agent}-notify.json"
    try:
        with lock(directory / f"{agent}-notify.lock", timeout=1):
            sent = json.loads(path.read_text()) if path.exists() else {}
            if sent.get(event.value) == digest:
                return False
            sent[event.value] = digest
            write_json(path, sent)
    except (OSError, ValueError, BridgeError):
        return False
    return True


def _report(
    directory: Path, agent: str, config: dict, text: tuple[str, str]
) -> None:
    """Sends one composed message and records every transport that failed.

    Args:
        directory: Private project state directory.
        agent: Lane the notification reports on.
        config: Resolved notification configuration.
        text: The composed subject and body.
    """
    for result in send(config, text[0], text[1]):
        if not result["ok"]:
            checkpoints.record(
                directory,
                agent,
                {"hook_event_name": "Notification"},
                checkpoints.Reason.NOTIFICATION_FAILED,
                None,
                "notifying",
                f"{result['transport']}: {result['error']}",
            )


def deliver(
    directory: Path, agent: str, event: Event, fields: Mapping[str, object]
) -> str:
    """Notifies the owner of one coordination change, at most once.

    The send runs on its own thread, so a hook decision or a supervision
    sweep never waits on a network round trip. The thread is a daemon: a
    short-lived hook process that exits first abandons the send rather than
    holding its agent, which is the trade the best-effort contract names.

    Args:
        directory: Private project state directory.
        agent: Lane the notification reports on.
        event: Notification to send.
        fields: Situation fields the message reports.

    Returns:
        The notification's name once a send has started, or an empty string
        when no transport is configured or the situation already notified.

    Raises:
        BridgeError: If the configured transports or SMTP settings are
            invalid.
    """
    if not enabled():
        return ""
    config = settings()
    if not config["transports"] or not _claim(directory, agent, event, fields):
        return ""
    text = compose(
        event.value, {**dict(fields), "lane": agent, "event": event.value}
    )
    thread = threading.Thread(
        target=_report,
        args=(directory, agent, config, text),
        daemon=True,
    )
    with _THREADS_LOCK:
        _THREADS.append(thread)
    thread.start()
    return event.value


def observe(
    directory: Path,
    agent: str,
    event: str,
    reason: str,
    context: Mapping[str, object],
) -> str:
    """Notifies the owner when a checkpoint decision changes their situation.

    Args:
        directory: Private project state directory.
        agent: Lane whose checkpoint produced the decision.
        event: Native lifecycle event name the checkpoint observed.
        reason: Enumerated cause recorded for the decision.
        context: Situation fields, including the project root, the provider,
            the session and the tool. A ``ledger`` entry carrying the
            published issue ledger is replaced by the handoff offer, if any,
            that names this lane.

    Returns:
        The notification's name once a send has started, or an empty string.
    """
    situation = {**dict(context), "reason": reason}
    ledger = situation.pop("ledger", None)
    if isinstance(ledger, dict):
        situation.update(offered(ledger, agent))
    chosen = select(event, reason, situation)
    if chosen is None:
        return ""
    return deliver(directory, agent, chosen, situation)


def drain(timeout: float = JOIN_SECONDS) -> int:
    """Waits for the sends already started to finish.

    Args:
        timeout: Seconds to wait for each in-flight send.

    Returns:
        The number of sends still running when the wait ended.
    """
    with _THREADS_LOCK:
        pending = list(_THREADS)
        _THREADS.clear()
    for thread in pending:
        thread.join(timeout=timeout)
    return sum(1 for thread in pending if thread.is_alive())


def probe(root: str) -> dict:
    """Sends one test message on each configured transport.

    The message is sent in the foreground and its per-transport result is
    returned, because an operator verifying credentials needs the failure
    text rather than a best-effort attempt.

    Args:
        root: Project root the test message names.

    Returns:
        The project root and one result per configured transport.

    Raises:
        BridgeError: If no transport is configured, or the configuration is
            invalid.
    """
    config = settings()
    if not config["transports"]:
        raise BridgeError(
            "No notification transport is configured. Set "
            "AGENT_PARLEY_NOTIFY to telegram, email, or both."
        )
    subject, body = compose(
        "test",
        {
            "repo": root,
            "event": "test",
            "detail": "Agent Parley notification test.",
        },
    )
    return {"root": root, "results": send(config, subject, body)}
