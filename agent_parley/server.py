"""Serves nine bounded coordination tools over authenticated local MCP HTTP."""

import argparse
import hmac
import json
import os
import socket
import socketserver
import sqlite3
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from agent_parley import checkpoints, protocol, retries, roster, store
from agent_parley.state import BridgeError

VERSIONS = ("2025-03-26", "2025-06-18", "2025-11-25")
MAX_REQUEST_BYTES = 16384
MAX_HOOK_BYTES = 1_048_576
HOOK_PATH = "/hook/"


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
        "release_file_reservations",
        "Release your file reservations.",
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
]


class Server(ThreadingHTTPServer):
    """Bounds simultaneous requests and keeps state isolated per connection."""

    daemon_threads = True

    def __init__(self, home: Path, config: dict) -> None:
        """Binds only the configured loopback port."""
        self.home = home
        self.token = config["token"]
        self.slots = threading.BoundedSemaphore(16)
        super().__init__(("127.0.0.1", config["port"]), Handler)

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
                self._reply(200, {"status": "ready", "engine": "agent-parley"})
            else:
                self._reply(405)

    def do_DELETE(self) -> None:
        """Rejects session deletion because transport state is not retained."""
        if self._authorize() is not None:
            self._reply(405)

    def do_POST(self) -> None:
        """Authenticates and handles one size-limited JSON-RPC request."""
        actor = self._authorize()
        if actor is None:
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
        try:
            served = checkpoints.serve(self.server.home, request)
        except Exception:
            traceback.print_exc()
            self._reply(500)
            return
        self._reply(200, served)

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
        """Validates the tool envelope and contains expected domain failures."""
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
            result = store.call(self.server.home, actor, tool["name"], args)
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


def main() -> None:
    """Runs the detached coordination service using its private config."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    config = json.loads((args.home / "config.json").read_text())
    store.initialize(args.home)
    from agent_parley import supervision

    stopped = threading.Event()
    observer = threading.Thread(
        target=supervision.run, args=(args.home, stopped), daemon=True
    )
    observer.start()
    try:
        with Server(args.home, config) as server:
            server.serve_forever(poll_interval=0.2)
    finally:
        stopped.set()
        observer.join(timeout=2)


if __name__ == "__main__":
    main()
