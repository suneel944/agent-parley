"""Read-only loopback web view of the live dashboard.

The view is a separate server from the MCP transport, so the transport's
bearer authentication is untouched. It binds the loopback address only and
every request must carry a random path token printed at start, name the
loopback authority in its ``Host`` header, as a guard against DNS rebinding,
and use ``GET``. No route writes state: the page, the ``top --json`` frame and
the Prometheus exposition are all rendered from one ``dashboard.collect``
reading, cached for the refresh interval so several open tabs share it.
"""

import contextlib
import hmac
import html
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from agent_parley import dashboard, status, views
from agent_parley.state import BridgeError

HOST = "127.0.0.1"
EXPOSITION = "text/plain; version=0.0.4; charset=utf-8"


class Server(ThreadingHTTPServer):
    """Serves one cached dashboard reading on the loopback address.

    Attributes:
        token: Random path segment every request must carry.
        interval: Seconds a reading is reused, also the page refresh period.
    """

    daemon_threads = True

    def __init__(
        self, port: int, read: Callable[[], dict], interval: float
    ) -> None:
        """Binds the loopback address.

        Args:
            port: TCP port to bind; zero picks a free one.
            read: Returns one filtered ``dashboard.collect`` snapshot.
            interval: Seconds to reuse a snapshot; at least one.

        Raises:
            OSError: The port cannot be bound.
        """
        super().__init__((HOST, port), Handler)
        self.token = secrets.token_urlsafe(16)
        self.interval = max(interval, 1.0)
        self._read = read
        self._lock = threading.Lock()
        self._view: dict | None = None
        self._read_at = 0.0

    @property
    def url(self) -> str:
        """The page address, carrying the path token."""
        return f"http://{HOST}:{self.server_port}/{self.token}/"

    def view(self) -> dict:
        """Returns the cached snapshot, reading a new one once it expires."""
        with self._lock:
            now = time.monotonic()
            if self._view is None or now - self._read_at >= self.interval:
                self._view = self._read()
                self._read_at = now
            return self._view


class Handler(BaseHTTPRequestHandler):
    """Answers read-only requests for the page, the frame and the metrics."""

    server: Server

    def log_message(self, *args: object) -> None:
        """Keeps request lines, which carry the path token, out of stderr."""

    def _reply(
        self,
        status: int,
        body: str = "",
        kind: str = "text/plain; charset=utf-8",
    ) -> None:
        """Sends one complete response that no cache keeps."""
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'")
        if status == 405:
            self.send_header("Allow", "GET")
        self.end_headers()
        if data:
            self.wfile.write(data)

    def do_GET(self) -> None:
        """Serves a route under the token to a loopback authority."""
        port = self.server.server_port
        if self.headers.get("Host") not in (
            f"{HOST}:{port}",
            f"localhost:{port}",
        ):
            self._reply(403)
            return
        token, separator, route = (
            self.path.partition("?")[0].removeprefix("/").partition("/")
        )
        if not separator or not hmac.compare_digest(
            token.encode(), self.server.token.encode()
        ):
            self._reply(404)
            return
        if route not in ("", "frame.json", "metrics"):
            self._reply(404)
            return
        try:
            view = self.server.view()
        except (BridgeError, OSError, sqlite3.Error):
            self._reply(503)
            return
        if route == "frame.json":
            self._reply(
                200,
                views.render("top", views.frame(view)) + "\n",
                "application/json",
            )
        elif route == "metrics":
            self._reply(200, views.exposition(view), EXPOSITION)
        else:
            self._reply(
                200,
                page(view, self.server.interval),
                "text/html; charset=utf-8",
            )

    def _refuse(self) -> None:
        """Refuses a method that could carry a change."""
        self._reply(405)

    do_POST = do_PUT = do_PATCH = do_DELETE = _refuse


def page(view: dict, refresh: float) -> str:
    """Renders a snapshot as one HTML page that refreshes itself.

    Args:
        view: Snapshot produced by ``dashboard.collect``.
        refresh: Seconds between page reloads.

    Returns:
        A page with one table per project, every ``top`` column and the
        lane's problem notes, each value escaped. Rows are grouped by
        bucket, needs input first, each group under a heading row that
        counts its lanes.
    """
    names = [*(name for name, _ in dashboard.COLUMNS), "PROBLEMS"]
    header = "".join(f"<th>{name}</th>" for name in names)
    server = "running" if view["running"] else "stopped"
    parts = [
        "<!DOCTYPE html>",
        '<html lang="en"><head><meta charset="utf-8">',
        f'<meta http-equiv="refresh" content="{max(1, round(refresh))}">',
        "<title>agent-parley top</title></head><body>",
        f"<h1>agent-parley top</h1><p>server {server}</p>",
    ]
    for project in view["projects"]:
        parts.append(f"<h2>{html.escape(str(project['root']))}</h2>")
        parts.append(f"<table><tr>{header}</tr>")
        rows = status.bucket_order(project["rows"])
        buckets = [status.bucket_of(row)["bucket"] for row in rows]
        for index, row in enumerate(rows):
            if not index or buckets[index - 1] != buckets[index]:
                parts.append(
                    f'<tr><th colspan="{len(names)}">'
                    f"{html.escape(buckets[index].capitalize())} "
                    f"({buckets.count(buckets[index])})</th></tr>"
                )
            cells = [
                *dashboard.row_cells(row),
                "; ".join(dashboard.notes(row)),
            ]
            parts.append(
                "<tr>"
                + "".join(
                    f"<td>{html.escape(str(cell))}</td>" for cell in cells
                )
                + "</tr>"
            )
        parts.append("</table>")
    parts.append("</body></html>")
    return "\n".join(parts) + "\n"


def serve(port: int, read: Callable[[], dict], interval: float) -> None:
    """Prints the page address and serves until interrupted.

    Args:
        port: TCP port to bind on the loopback address; zero picks one.
        read: Returns one filtered ``dashboard.collect`` snapshot.
        interval: Seconds to reuse a snapshot and between page reloads.

    Raises:
        OSError: The port cannot be bound.
    """
    with Server(port, read, interval) as server:
        print(f"Serving a read-only view at {server.url}", flush=True)
        print(
            f"Frame: {server.url}frame.json  Metrics: {server.url}metrics",
            flush=True,
        )
        with contextlib.suppress(KeyboardInterrupt):
            server.serve_forever()
