"""Serves the bounded coordination tools over authenticated local MCP HTTP."""

import argparse
import contextlib
import hmac
import json
import os
import signal
import socket
import socketserver
import sqlite3
import threading
import time
import traceback
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import FrameType

from agent_parley import (
    checkpoints,
    hook,
    metrics,
    protocol,
    recommend,
    retries,
    roster,
    store,
    waits,
)
from agent_parley.state import BridgeError, trim_log

VERSIONS = ("2025-03-26", "2025-06-18", "2025-11-25")
MAX_REQUEST_BYTES = 16384
MAX_HOOK_BYTES = 1_048_576
HOOK_PATH = "/hook/"
REVISION_SECONDS = 2.0
LOG_NAME = "server.log"
WORKERS = 16
DECISION_SECONDS = hook.REPLY_TIMEOUT - 0.5
REFUSAL_SECONDS = 5.0
SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
DRIFTED = (
    "Sources changed under the running service; it is answering from a "
    "module set the checkout no longer holds and is stopping. "
    + protocol.RELAUNCH
)


def log(home: Path, event: str, detail: str = "") -> None:
    """Writes one timestamped lifecycle entry to the service log.

    The launcher gives this process the service log as its output streams, so
    printing is what writes the log. Every entry starts with a local
    timestamp and a single event word, and an ordinary event fits one line, so
    an operator reading the tail of a service that died can see when it bound
    its port, when it stopped and what refused work in between. A failure
    carries its traceback after that first line. Entries name paths,
    participants and counts, never a credential and never peer content.

    The log is bounded by the rotation every line log here shares, so a
    long-lived service on a shared machine does not grow the file without
    end. A log that cannot be written or bounded never fails a request.

    Args:
        home: Private bridge state root holding the service log.
        event: Single word naming what happened, such as ``bound``.
        detail: Remainder of the entry, already free of credentials.
    """
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    try:
        print(f"{stamp} {event} {detail}".rstrip(), flush=True)
    except OSError:
        return
    trim_log(home / LOG_NAME)


def _tool(
    name: str, description: str, properties: dict, required: list[str]
) -> dict:
    """Describes one narrow tool without repeating credentials or identity."""
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": name in store.READ_ONLY,
            "destructiveHint": False,
            "openWorldHint": False,
        },
    }


_REFUSED = object()
_INVALID = object()
TEXT = {"type": "string"}
INTEGER = {"type": "integer"}
FLAG = {"type": "boolean"}
RETRY_KEY = {
    "type": "string",
    "maxLength": retries.KEY_CHARACTERS,
    "description": "Optional. Reuse on a retry; the repeat changes nothing.",
}
TOOLS = [
    _tool(
        "send_message",
        "Send a concise update. Reuse the key only on retries.",
        {
            "to": {
                "type": "array",
                "items": TEXT,
                "maxItems": store.MAX_RECIPIENTS,
            },
            "subject": {**TEXT, "maxLength": 160},
            "body_md": {**TEXT, "description": "At most 4096 UTF-8 bytes."},
            "idempotency_key": {**TEXT, "maxLength": 80},
            "thread_id": {**TEXT, "maxLength": 80},
            "reply_to": {
                **INTEGER,
                "description": "Message answered; joins its thread.",
            },
            "ack_required": FLAG,
            "decision": {
                **FLAG,
                "description": "Also record in the shared decision log.",
            },
        },
        ["to", "subject", "body_md", "idempotency_key"],
    ),
    _tool(
        "fetch_inbox",
        "Read incremental mail. Bodies opt-in; page long bodies.",
        {
            "after_id": INTEGER,
            "limit": {**INTEGER, "minimum": 1, "maximum": 5},
            "include_bodies": FLAG,
            "body_offset": INTEGER,
            "unread": FLAG,
            "unacknowledged": FLAG,
        },
        [],
    ),
    _tool(
        waits.TOOL,
        "Await your next matching mail instead of polling or ending your "
        "turn. Filters as fetch_inbox; an expired wait returns nothing.",
        {
            "timeout_seconds": {
                **INTEGER,
                "minimum": 0,
                "maximum": int(waits.MAX_SECONDS),
            },
            "thread_id": {**TEXT, "maxLength": 80},
            "after_id": INTEGER,
            "include_bodies": FLAG,
            "unread": FLAG,
            "unacknowledged": FLAG,
        },
        [],
    ),
    _tool(
        "acknowledge_message",
        "Explicitly acknowledge a reviewed message.",
        {"message_id": INTEGER, "idempotency_key": RETRY_KEY},
        ["message_id"],
    ),
    _tool(
        "mark_message_read",
        "Mark an ordinary message reviewed; does not ack.",
        {"message_id": INTEGER, "idempotency_key": RETRY_KEY},
        ["message_id"],
    ),
    _tool(
        "file_reservation_paths",
        "Reserve repository-relative paths, or named resources written with a "
        "scheme such as port:5432, db:local, suite:integration or "
        "device:android-1, atomically; conflicts grant nothing and name the "
        "blocking owner with that owner's reason.",
        {
            "paths": {
                "type": "array",
                "items": TEXT,
                "maxItems": 16,
                "description": (
                    "Repository-relative paths, or named resources written "
                    "as SCHEME:NAME. A named resource conflicts on an exact "
                    "match only."
                ),
            },
            "ttl_seconds": {
                **INTEGER,
                "minimum": 30,
                "maximum": 3600,
                "description": (
                    "Optional. Past it the lease reports stale and still "
                    "holds; omit it and the lease never reports stale."
                ),
            },
            "exclusive": FLAG,
            "reason": {
                **TEXT,
                "maxLength": 160,
                "description": "Declared scope, shown to peers you block.",
            },
            "idempotency_key": RETRY_KEY,
        },
        ["paths"],
    ),
    _tool(
        "request_reservation",
        "Reserve the same keys, and where a peer holds one, queue for it "
        "instead of failing; the refusal names the holder and your place. "
        "The holder's release grants it and sends you one notice.",
        {
            "paths": {
                "type": "array",
                "items": TEXT,
                "maxItems": 16,
                "description": "Keys as file_reservation_paths takes them.",
            },
            "ttl_seconds": {**INTEGER, "minimum": 30, "maximum": 3600},
            "exclusive": FLAG,
            "reason": {**TEXT, "maxLength": 160},
            "idempotency_key": RETRY_KEY,
        },
        ["paths"],
    ),
    _tool(
        "cancel_reservation_request",
        "Withdraw one queued reservation request, or every one of yours.",
        {"request_id": INTEGER, "idempotency_key": RETRY_KEY},
        [],
    ),
    _tool(
        "release_file_reservations",
        "Release your file reservations. A key another lane queued for is "
        "granted to it here, and that lane is told in the same commit.",
        {"idempotency_key": RETRY_KEY},
        [],
    ),
    _tool(
        "list_participants",
        "List the participants you can address in this project.",
        {},
        [],
    ),
    _tool(
        "read_thread",
        "Read your mail in one thread, oldest first.",
        {
            "thread_id": {**TEXT, "maxLength": 80},
            "after_id": INTEGER,
            "limit": {
                **INTEGER,
                "minimum": 1,
                "maximum": store.MAX_THREAD_PAGE,
            },
        },
        ["thread_id"],
    ),
    _tool(
        "search_messages",
        "Search your mail by text, newest match first.",
        {
            "query": {**TEXT, "maxLength": store.MAX_QUERY_BYTES},
            "limit": {
                **INTEGER,
                "minimum": 1,
                "maximum": store.MAX_SEARCH_HITS,
            },
        },
        ["query"],
    ),
    _tool(
        "search_decisions",
        "Search decisions any lane recorded. Empty query lists the newest.",
        {
            "query": {**TEXT, "maxLength": store.MAX_QUERY_BYTES},
            "limit": {
                **INTEGER,
                "minimum": 1,
                "maximum": store.MAX_SEARCH_HITS,
            },
            "since": {**INTEGER, "minimum": 0},
        },
        [],
    ),
    _tool(
        "review_report",
        "Record your verdict on a peer's report, never your own. A verdict "
        "is your claim, not independent verification.",
        {
            "report_id": {**TEXT, "maxLength": 32},
            "verdict": {"type": "string", "enum": list(metrics.VERDICTS)},
            "evidence": {
                **TEXT,
                "description": "What you checked; at most 4096 UTF-8 bytes.",
            },
        },
        ["report_id", "verdict", "evidence"],
    ),
    _tool(
        "next_issues",
        "Rank unclaimed issues you could take next, with the reason for "
        "each. Read this before claiming; it claims nothing.",
        {
            "limit": {
                **INTEGER,
                "minimum": 1,
                "maximum": recommend.MAX_SHORTLIST,
            }
        },
        [],
    ),
]


class Server(ThreadingHTTPServer):
    """Bounds simultaneous requests and keeps state isolated per connection."""

    daemon_threads = True

    def __init__(self, home: Path, config: dict) -> None:
        """Binds only the configured loopback port.

        The version and the source fingerprint of the code being served are
        read here, once, so every later reading is a comparison against what
        this process actually started with. A successful bind is recorded as
        one log entry, because a service that later dies leaves that entry as
        the only evidence of when it started and what it was serving.
        """
        self.home = home
        self.token = config["token"]
        self.slots = threading.BoundedSemaphore(WORKERS)
        self.waiters = threading.BoundedSemaphore(waits.MAX_WAITERS)
        self.version = protocol.launcher_version()
        self.revision = protocol.revision()
        self.checked = time.monotonic()
        self.reading = threading.Lock()
        self.stopping = threading.Event()
        self.counting = threading.Lock()
        self.refusals = 0
        self.reported: float | None = None
        super().__init__(("127.0.0.1", config["port"]), Handler)
        log(
            home,
            "bound",
            f"127.0.0.1:{self.server_port} version {self.version} "
            f"pid {os.getpid()} workers {WORKERS}",
        )

    def drifted(self) -> bool:
        """Reports whether the served modules left the sources on disk.

        The fingerprint is re-read at most once every `REVISION_SECONDS`,
        because the caller is a hook decision whose whole budget is a few
        milliseconds and a merge is not an event worth paying for on every
        request. The first reading that differs from the one taken at start
        is final: it is logged once as a single line, the service stops
        accepting connections so in-flight requests finish and the process
        exits, and every request until then is answered as stale rather than
        with a traceback from a module that is no longer importable.

        Returns:
            Whether the service is serving code the checkout has moved past.
        """
        if self.stopping.is_set():
            return True
        with self.reading:
            if self.stopping.is_set():
                return True
            now = time.monotonic()
            if now - self.checked < REVISION_SECONDS:
                return False
            self.checked = now
            if protocol.revision() == self.revision:
                return False
            self.stopping.set()
        log(self.home, "drifted", DRIFTED)
        threading.Thread(target=self.shutdown, daemon=True).start()
        return True

    @contextlib.contextmanager
    def waiting(self) -> Iterator[float]:
        """Lends this request's worker slot back while it waits for mail.

        A wait spends nearly all of its time asleep, so holding one of the
        sixteen worker slots for it would let a handful of lanes waiting on
        their peers exhaust the service for every other call, including the
        sends they are waiting for. The slot is returned for the duration of
        the wait and taken again before the reply is written, so the number
        of requests doing work is bounded exactly as before while the number
        of lanes waiting is bounded separately.

        Yields:
            The longest wait to serve: the ordinary ceiling, or zero once as
            many waits are already held as this service admits, which reads
            the inbox once and answers rather than refusing the call.
        """
        if not self.waiters.acquire(blocking=False):
            yield 0.0
            return
        self.slots.release()
        try:
            yield waits.MAX_SECONDS
        finally:
            self.slots.acquire()
            self.waiters.release()

    def health(self) -> dict:
        """Describes the code this service is answering from.

        Returns:
            The readiness document, naming the package version the service
            started with and whether the sources still match it. A stale
            service names the command that replaces it, so an operator reads
            the drift from `status` and `doctor` instead of from a log.
        """
        document = {
            "status": "ready",
            "engine": "agent-parley",
            "version": self.version,
        }
        if self.drifted():
            return {**document, "status": protocol.STALE, "detail": DRIFTED}
        return document

    def server_bind(self) -> None:
        """Binds loopback without HTTPServer's unnecessary reverse DNS lookup.

        Readiness is authenticated at a numeric loopback address. Resolving
        the runner's host name can block startup on macOS without providing
        information used by this service.
        """
        socketserver.TCPServer.server_bind(self)
        self.server_name = "127.0.0.1"
        self.server_port = self.server_address[1]

    def process_request(
        self,
        request: socket.socket | tuple[bytes, socket.socket],
        client_address: tuple,
    ) -> None:
        """Rejects overload rather than creating unbounded worker threads."""
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            self.refuse()
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(
        self,
        request: socket.socket | tuple[bytes, socket.socket],
        client_address: tuple,
    ) -> None:
        """Releases capacity even when clients disconnect or time out."""
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def refuse(self) -> None:
        """Records that a connection was closed with every worker slot busy.

        A refused connection used to be a silent close: the client saw no
        status line, fell back to its own decision, and readiness still read
        as ready, so the overload left no trace anywhere. It is recorded as
        one entry naming how many connections were refused and how many
        worker slots the service has. Refusals arrive in bursts, so one entry
        covers a window rather than a connection and a burst cannot fill the
        log it is reported in.
        """
        with self.counting:
            self.refusals += 1
            now = time.monotonic()
            if (
                self.reported is not None
                and now - self.reported < REFUSAL_SECONDS
            ):
                return
            refused, self.refusals, self.reported = self.refusals, 0, now
        log(
            self.home,
            "refused",
            f"{refused} connection(s) closed unanswered with all "
            f"{WORKERS} worker slots busy",
        )

    def handle_error(
        self,
        request: socket.socket | tuple[bytes, socket.socket],
        client_address: tuple,
    ) -> None:
        """Records an unexpected request failure as one timestamped entry.

        Args:
            request: Connection whose handling raised; never logged.
            client_address: Loopback peer, which identifies nothing here.
        """
        log(self.home, "failed", "request handling\n" + traceback.format_exc())


class Handler(BaseHTTPRequestHandler):
    """Implements the JSON-response subset of stateless Streamable HTTP."""

    server: Server

    def setup(self) -> None:
        """Bounds slow-client reads before processing headers or bodies."""
        self.request.settimeout(3)
        super().setup()

    def log_message(self, format: str, *args: object) -> None:
        """Keeps bearer tokens and peer content out of access logs."""

    def _reply(self, status: int, value: dict | None = None) -> None:
        """Writes one non-cacheable JSON response with an explicit length."""
        body = (
            json.dumps(
                value, ensure_ascii=False, separators=(",", ":")
            ).encode()
            if value is not None
            else b""
        )
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _authorize(self) -> dict | None:
        """Rejects foreign origins and resolves scoped bearer credentials."""
        authority = f"127.0.0.1:{self.server.server_port}"
        if (
            self.headers.get("Host") != authority
            or self.headers.get("Origin", f"http://{authority}")
            != f"http://{authority}"
        ):
            self._reply(403)
            return None
        authorization = self.headers.get("Authorization", "")
        if (
            not authorization.startswith("Bearer ")
            or len(self.headers.get_all("Authorization", [])) != 1
        ):
            self._reply(401)
            return None
        token = authorization.removeprefix("Bearer ")
        if self.path == "/health/readiness":
            if hmac.compare_digest(token.encode(), self.server.token.encode()):
                return {"health": True}
        elif self.path in ("/mcp/", HOOK_PATH):
            try:
                actor = store.authenticate(self.server.home, token)
            except sqlite3.Error:
                self._reply(503)
                return None
            if actor:
                return actor
        self._reply(401)
        return None

    def do_GET(self) -> None:
        """Reports authenticated health without opening SSE streams."""
        if self._authorize() is not None:
            if self.path == "/health/readiness":
                self._reply(200, self.server.health())
            else:
                self._reply(405)

    def do_DELETE(self) -> None:
        """Rejects session deletion because transport state is not retained."""
        if self._authorize() is not None:
            self._reply(405)

    def do_POST(self) -> None:
        """Authenticates and handles one size-limited JSON-RPC request.

        A service whose sources have moved refuses before it reaches the code
        that would import them. The refusal is the status a client already
        treats as an outage, so a hook decides in-process on the same path a
        stopped service sends it down, and one log line replaces a traceback
        for every request the drift would otherwise break.
        """
        actor = self._authorize()
        if actor is None:
            return
        if self.server.drifted():
            self._reply(503, {"status": protocol.STALE, "detail": DRIFTED})
            return
        if self.path == HOOK_PATH:
            self._hook(actor)
            return
        if self.path != "/mcp/":
            self._reply(405)
            return
        version = self.headers.get("MCP-Protocol-Version", VERSIONS[0])
        if version not in VERSIONS:
            self._reply(400)
            return
        message = self._body(MAX_REQUEST_BYTES)
        if message is _REFUSED:
            return
        if message is _INVALID:
            self._reply(
                400,
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": "Invalid JSON"},
                },
            )
            return
        if (
            not isinstance(message, dict)
            or message.get("jsonrpc") != "2.0"
            or not isinstance(message.get("method"), str)
            or not isinstance(message.get("params", {}), dict)
            or ("id" in message and type(message["id"]) not in (int, str))
        ):
            self._reply(400)
            return
        method = message["method"]
        if "id" not in message:
            self._reply(202 if method.startswith("notifications/") else 400)
            return
        response = {"jsonrpc": "2.0", "id": message["id"]}
        params = message.get("params", {})
        if method == "initialize":
            requested = params.get("protocolVersion")
            response["result"] = {
                "protocolVersion": requested
                if requested in VERSIONS
                else VERSIONS[-1],
                "capabilities": {"tools": {}},
                "serverInfo": {
                    "name": "agent-parley",
                    "version": protocol.launcher_version(),
                },
            }
        elif method == "ping":
            response["result"] = {}
        elif method == "tools/list":
            response["result"] = {"tools": TOOLS}
        elif method == "tools/call":
            response["result"] = self._call(actor, params)
        else:
            response["error"] = {"code": -32601, "message": "Method not found"}
        self._reply(200, response)

    def _body(self, limit: int) -> object:
        """Reads one bounded JSON body, or replies and returns ``_REFUSED``.

        Args:
            limit: Largest body accepted on this path, in bytes.

        Returns:
            The decoded JSON value, ``_INVALID`` when the body was not JSON,
            or ``_REFUSED`` after a refusal has already been written. A body
            over the limit but under twice it is drained before the refusal,
            so a peer still writing it reads the reply instead of seeing its
            connection reset; anything larger is refused unread.
        """
        if self.headers.get_content_type() != "application/json":
            self._reply(415)
            return _REFUSED
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._reply(400)
            return _REFUSED
        if not 0 < length <= limit or self.headers.get("Transfer-Encoding"):
            if 0 < length <= 2 * limit and not self.headers.get(
                "Transfer-Encoding"
            ):
                self.rfile.read(length)
            self._reply(413)
            return _REFUSED
        try:
            return json.loads(self.rfile.read(length))
        except (ValueError, RecursionError):
            return _INVALID

    def _hook(self, actor: dict) -> None:
        """Serves one lifecycle hook decision for the lane the credential names.

        The request carries what the hook command line carries: the lane's
        state directory, its participant name, the adapter and protocol it
        was configured with, and the native payload. The credential resolves
        to one registered identity, and the identity file in the named
        directory must hold that same credential, so a token cannot decide
        for a lane it was not registered for. The reply is the hook process's
        own contract, produced by the code the in-process path runs. A
        failure inside that code answers 500 with its traceback in the
        service log, so the hook process decides in-process rather than
        parsing a connection that closed without a status line, as it did
        when a reinstall removed modules from under a running service.

        Args:
            actor: Registered identity the bearer credential resolved to.
        """
        request = self._body(MAX_HOOK_BYTES)
        if request is _REFUSED:
            return
        if (
            not isinstance(request, dict)
            or not isinstance(request.get("directory"), str)
            or not isinstance(request.get("participant"), str)
            or "payload" not in request
        ):
            self._reply(400)
            return
        token = self.headers.get("Authorization", "").removeprefix("Bearer ")
        path = Path(request["directory"])
        try:
            identity = json.loads(
                (path / f"{request['participant']}-identity.json").read_text()
            )
        except (OSError, ValueError):
            self._reply(403)
            return
        if (
            not isinstance(identity, dict)
            or identity.get("name") != actor["name"]
            or not hmac.compare_digest(
                str(identity.get("registration_token", "")).encode(),
                token.encode(),
            )
        ):
            self._reply(403)
            return
        served = self._decide(request, actor["name"])
        if served is None:
            return
        if self.headers.get("Accept") == hook.RAW_REPLY:
            self._raw_hook(served)
            return
        self._reply(200, served)

    def _decide(self, request: dict, participant: str) -> dict | None:
        """Produces one hook decision under a deadline of its own.

        The client stops reading after `hook.REPLY_TIMEOUT` and decides in
        process, so a decision still running shortly before that is worth
        nothing to it. A checkpoint that stalls on a Git subprocess or a busy
        store is therefore answered with the status a stopped service already
        sends, which is the fallback path the client handles, and the worker
        slot is released with the reply rather than held for as long as the
        stall lasts. The stalled decision finishes on its own thread, where
        it holds nothing this service counts.

        Args:
            request: Hook request the credential was accepted for.
            participant: Registered identity the credential resolved to.

        Returns:
            The decision, or None once the client has been answered because
            the decision failed or ran past its deadline.
        """
        outcome: dict = {}

        def decide() -> None:
            """Records the decision or the failure that ended it."""
            try:
                outcome["served"] = checkpoints.serve(self.server.home, request)
            except Exception:
                outcome["failed"] = traceback.format_exc()

        worker = threading.Thread(target=decide, daemon=True)
        worker.start()
        worker.join(DECISION_SECONDS)
        served = outcome.get("served")
        if isinstance(served, dict):
            return served
        if "failed" in outcome:
            log(
                self.server.home,
                "failed",
                f"{self.path} {participant}\n{outcome['failed']}",
            )
            self._reply(500)
            return None
        log(
            self.server.home,
            "expired",
            f"{self.path} {participant} undecided after "
            f"{DECISION_SECONDS} seconds; answered as unavailable",
        )
        self._reply(503, {"status": protocol.STALE, "detail": DRIFTED})
        return None

    def _raw_hook(self, served: dict) -> None:
        """Answers a hook decision without a reply a client must decode.

        A shell client can frame bytes by length and read an integer header,
        but decoding JSON string escapes in a shell is where a wrong byte
        would quietly change what a hook injects or what status it exits
        with. The decision is therefore carried as its own two streams: the
        exit status and the length of the standard-output stream travel in
        headers, and the body is that stream followed by standard error.

        Args:
            served: Decision the in-process path produced, holding the hook's
                standard output, standard error and exit status.
        """
        out = str(served.get("stdout", "")).encode()
        err = str(served.get("stderr", "")).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header(hook.STATUS_HEADER, str(int(served.get("status", 0))))
        self.send_header(hook.STDOUT_HEADER, str(len(out)))
        self.send_header("Content-Length", str(len(out) + len(err)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(out + err)

    def _declared_protocol(self) -> int:
        """Returns the wire protocol this caller declared, or this build's.

        A caller that declares nothing is served, because the header was added
        after the first protocol and its absence means exactly that. A caller
        that declares something unreadable is treated as declaring an unknown
        protocol and is refused by name rather than guessed at.
        """
        header = self.headers.get(protocol.HEADER)
        if header is None:
            return protocol.PROTOCOL
        try:
            return int(header)
        except ValueError:
            return protocol.UNKNOWN

    def _call(self, actor: dict, params: dict) -> dict:
        """Validates the tool envelope and contains expected domain failures.

        A wait is served beside the store rather than inside a transaction,
        so it holds no lock while it sleeps, and it is told how long this
        service is willing to hold it open. Every other tool is the served
        call it has always been, and a delivered message wakes the lanes
        waiting for one before the sender is answered. A release that grants
        a queued request delivers such a message, so it wakes them too.
        """
        try:
            declared = self._declared_protocol()
            if not protocol.compatible(declared):
                store.refused(
                    self.server.home, actor, str(params.get("name", ""))[:80]
                )
                raise BridgeError(protocol.mismatch("plugin", declared))
            tool = next(
                (t for t in TOOLS if t["name"] == params.get("name")), None
            )
            args = params.get("arguments", {})
            if tool is None or not isinstance(args, dict):
                raise BridgeError("Unknown tool or invalid arguments.")
            schema = tool["inputSchema"]
            if set(args) - set(schema["properties"]) or set(
                schema["required"]
            ) - set(args):
                raise BridgeError("Unexpected or missing tool arguments.")
            if roster.paused(self.server.home, actor["project"], actor["name"]):
                raise BridgeError(roster.PAUSED_REASON)
            if tool["name"] == waits.TOOL:
                with self.server.waiting() as ceiling:
                    result = waits.wait(
                        self.server.home,
                        actor,
                        args,
                        self.server.stopping,
                        ceiling,
                    )
            else:
                result = store.call(self.server.home, actor, tool["name"], args)
                if tool["name"] == "send_message" or (
                    tool["name"] == "release_file_reservations"
                    and result["granted"]
                ):
                    waits.delivered()
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            result, ensure_ascii=False, separators=(",", ":")
                        ),
                    }
                ]
            }
        except BridgeError as exc:
            return {
                "isError": True,
                "content": [{"type": "text", "text": str(exc)}],
            }
        except sqlite3.OperationalError as exc:
            retryable = getattr(exc, "sqlite_errorcode", 0) & 0xFF in (
                sqlite3.SQLITE_BUSY,
                sqlite3.SQLITE_LOCKED,
            )
            return {
                "isError": True,
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Store unavailable or busy; retry later "
                            "using the same message key."
                            if retryable
                            else f"Store error: {exc}"
                        ),
                    }
                ],
            }


def stop_on_signal(home: Path, service: Server) -> None:
    """Records a terminating signal and stops the service it names.

    A service that was signalled used to leave nothing behind, so an operator
    finding a dead process could not tell a stop from a kill. Each handled
    signal is recorded as one entry and then stops the service the way a
    clean stop does, on a separate thread because the serving thread is the
    one that has to return.

    Args:
        home: Private bridge state root holding the service log.
        service: Running service to stop when a signal arrives.
    """

    def received(number: int, frame: FrameType | None) -> None:
        """Records the signal and starts a clean stop for it.

        Args:
            number: Signal the process was sent.
            frame: Interrupted stack frame, which is not inspected.
        """
        log(
            home,
            "signalled",
            f"{signal.Signals(number).name}; stopping",
        )
        threading.Thread(target=service.shutdown, daemon=True).start()

    for number in SIGNALS:
        signal.signal(number, received)


def main() -> None:
    """Runs the detached coordination service using its private config."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    config = json.loads((args.home / "config.json").read_text())
    store.initialize(args.home)
    from agent_parley import inbound, supervision

    stopped = threading.Event()
    workers = [
        threading.Thread(target=run, args=(args.home, stopped), daemon=True)
        for run in (supervision.run, inbound.run)
    ]
    for worker in workers:
        worker.start()
    try:
        with Server(args.home, config) as server:
            stop_on_signal(args.home, server)
            server.serve_forever(poll_interval=0.2)
            log(args.home, "stopped", "no longer accepting connections")
    finally:
        stopped.set()
        for worker in workers:
            worker.join(timeout=2)


if __name__ == "__main__":
    main()
