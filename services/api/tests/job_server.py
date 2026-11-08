"""A tiny local HTTP server and helpers for the job-import tests (never touches the internet).

Production code blocks 127.0.0.1 (SSRF policy), so tests that need a real socket use a deliberately
looser `UrlPolicy` built here. That policy exists only in tests; app wiring always uses the default.
"""

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from app.jobs_import.ssrf import IPAddress, UrlPolicy, is_public_ip

Route = Callable[[BaseHTTPRequestHandler], None]


class LocalServer:
    def __init__(self) -> None:
        self.routes: dict[str, Route] = {}
        self.requests: list[tuple[str, dict[str, str]]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                outer.requests.append((self.path, {k: v for k, v in self.headers.items()}))
                route = outer.routes.get(self.path.split("?")[0])
                if route is None:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                try:
                    route(self)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, format: str, *args: Any) -> None:
                return

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port: int = self.httpd.server_address[1]

    def policy(self) -> UrlPolicy:
        """Allow this server's port and 127.0.0.1 (plus whatever is genuinely public)."""

        def allowed(ip: IPAddress) -> bool:
            return str(ip) == "127.0.0.1" or is_public_ip(ip)

        return UrlPolicy(allowed_ports=frozenset({self.port}), ip_allowed=allowed)

    def url(self, path: str, host: str = "jobs.test") -> str:
        return f"http://{host}:{self.port}{path}"

    @staticmethod
    def resolver(mapping: dict[str, str] | None = None) -> Callable[[str, int], list[str]]:
        table = {"jobs.test": "127.0.0.1", **(mapping or {})}

        def resolve(host: str, port: int) -> list[str]:
            if host not in table:
                raise OSError("unknown host")
            return [table[host]]

        return resolve


@contextmanager
def local_server() -> Iterator[LocalServer]:
    server = LocalServer()
    thread = threading.Thread(target=server.httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.httpd.shutdown()
        server.httpd.server_close()
        thread.join(timeout=5)


def page(
    body: bytes | str,
    *,
    status: int = 200,
    content_type: str | None = "text/html; charset=utf-8",
    headers: dict[str, str] | None = None,
    declare_length: bool = True,
) -> Route:
    payload = body.encode("utf-8") if isinstance(body, str) else body

    def route(h: BaseHTTPRequestHandler) -> None:
        h.send_response(status)
        if content_type is not None:
            h.send_header("Content-Type", content_type)
        if declare_length:
            h.send_header("Content-Length", str(len(payload)))
        for key, value in (headers or {}).items():
            h.send_header(key, value)
        h.end_headers()
        h.wfile.write(payload)

    return route


def redirect(location: str, status: int = 302, headers: dict[str, str] | None = None) -> Route:
    def route(h: BaseHTTPRequestHandler) -> None:
        h.send_response(status)
        h.send_header("Location", location)
        h.send_header("Content-Length", "0")
        for key, value in (headers or {}).items():
            h.send_header(key, value)
        h.end_headers()

    return route


def slow(delay: float, then: Route) -> Route:
    def route(h: BaseHTTPRequestHandler) -> None:
        time.sleep(delay)
        then(h)

    return route


def drip(interval: float, total_chunks: int = 200) -> Route:
    """Sends a valid HTML response very slowly (one byte per `interval`)."""

    def route(h: BaseHTTPRequestHandler) -> None:
        h.send_response(200)
        h.send_header("Content-Type", "text/html")
        h.end_headers()
        for _ in range(total_chunks):
            h.wfile.write(b"x")
            h.wfile.flush()
            time.sleep(interval)

    return route
