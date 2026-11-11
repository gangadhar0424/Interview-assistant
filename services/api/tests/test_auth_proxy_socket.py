"""Phase 13A Batch 2: over a REAL uvicorn socket, what does the app see as the client address?

This verifies the deployment rule "run uvicorn with --no-proxy-headers" (docs/OPERATIONS.md):
uvicorn's default silently rewrites the peer from a forged X-Forwarded-For sent by a trusted
(local) peer, which is exactly how the Next.js proxy reaches the API. With the flag, the peer stays
the real TCP peer and `client_key` is the only place forwarded headers are interpreted.
"""

import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

import httpx
import uvicorn
from fastapi import FastAPI, Request

from app.auth.client_ip import client_key

FORGED = "9.9.9.9"


def build_app() -> FastAPI:
    app = FastAPI()

    @app.get("/who")
    def who(request: Request) -> dict[str, str | None]:
        peer = request.client.host if request.client else None
        header = request.headers.get("x-forwarded-for")
        return {
            "peer": peer,
            "key_untrusted": client_key(peer, header, trusted_proxy=False, hops=1),
            "key_trusted": client_key(peer, header, trusted_proxy=True, hops=1),
        }

    return app


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@contextmanager
def serve(**options: object) -> Iterator[str]:
    port = free_port()
    config = uvicorn.Config(
        build_app(),
        host="127.0.0.1",
        port=port,
        log_level="error",
        **options,  # type: ignore[arg-type]
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started, "uvicorn did not start"
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def who(base: str, forwarded: str | None) -> dict[str, str | None]:
    headers = {"X-Forwarded-For": forwarded} if forwarded else {}
    response = httpx.get(f"{base}/who", headers=headers, timeout=10)
    assert response.status_code == 200
    out: dict[str, str | None] = response.json()
    return out


def test_with_no_proxy_headers_a_forged_header_does_not_change_the_peer() -> None:
    with serve(proxy_headers=False) as base:
        seen = who(base, FORGED)
        assert seen["peer"] == "127.0.0.1"  # the real TCP peer
        assert seen["key_untrusted"] is None  # trusted proxy OFF: header ignored, loopback skipped
        # Trusted-proxy handling is the app's own, explicit decision (not uvicorn's):
        assert seen["key_trusted"] == FORGED


def test_with_no_proxy_headers_and_no_header_the_app_has_no_client_key() -> None:
    with serve(proxy_headers=False) as base:
        seen = who(base, None)
        assert (seen["peer"], seen["key_untrusted"], seen["key_trusted"]) == (
            "127.0.0.1",
            None,
            None,
        )


def test_uvicorn_default_would_let_a_forged_header_replace_the_peer() -> None:
    """Why the flag is required: this is the behaviour deployments must NOT run with."""
    with serve() as base:  # default: proxy_headers=True, trusts 127.0.0.1
        seen = who(base, FORGED)
        assert seen["peer"] == FORGED  # attacker-chosen "client address" via a trusted local peer


def test_forwarded_allow_ips_empty_also_keeps_the_real_peer() -> None:
    with serve(proxy_headers=True, forwarded_allow_ips="") as base:
        assert who(base, FORGED)["peer"] == "127.0.0.1"
