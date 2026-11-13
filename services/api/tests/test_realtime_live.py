"""Phase 9A over a REAL socket: uvicorn in a thread and a real WebSocket client.

TestClient cannot show what a browser or the desktop app sees on the wire, so these tests start the
actual application (built by the same factory production uses, after uvicorn configured logging)
and verify:
- a refused handshake is a plain HTTP 403 with no body and no hint, for every cause;
- the ticket never appears in uvicorn's own logs (the query string is redacted);
- an abrupt client disconnect releases the connection's slot;
- a normal connect / hello / ping round trip works through the real ASGI server.
"""

import json
import logging
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient
from websockets.exceptions import InvalidStatus
from websockets.sync.client import connect

from app.core.config import get_settings
from app.main import create_app
from tests.conftest import RunSql
from tests.helpers import as_legacy, register
from tests.realtime_fakes import install_fake_tickets
from tests.test_sessions import body, create, new_file


class Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


@dataclass
class Live:
    app: Any
    port: int
    capture: Capture

    def http(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def ws(self, session_id: str, ticket: str | None) -> str:
        query = "" if ticket is None else f"?ticket={ticket}"
        return f"ws://127.0.0.1:{self.port}/api/v1/ws/sessions/{session_id}{query}"


@pytest.fixture
def live(client: TestClient) -> Iterator[Live]:  # `client` prepares the clean database + env
    holder: dict[str, Any] = {}

    def factory() -> Any:
        holder["app"] = create_app()
        return holder["app"]

    server = uvicorn.Server(
        uvicorn.Config(
            factory,
            factory=True,
            host="127.0.0.1",
            port=0,
            log_level="info",
            access_log=True,
            lifespan="on",
            timeout_graceful_shutdown=2,
        )
    )
    capture = Capture()
    loggers = [logging.getLogger(n) for n in ("uvicorn.error", "uvicorn.access")]
    for logger in loggers:
        logger.addHandler(capture)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started, "the live server did not start"
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield Live(app=holder["app"], port=port, capture=capture)
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        for logger in loggers:
            logger.removeHandler(capture)


def wait_for(condition: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return condition()


@dataclass
class Setup:
    live: Live
    headers: dict[str, str]
    session_id: str

    def ticket(self) -> str:
        response = httpx.post(
            self.live.http("/api/v1/auth/ws-ticket"),
            json={"session_id": self.session_id},
            headers=self.headers,
            timeout=10,
        )
        assert response.status_code == 200, response.text
        return str(response.json()["ticket"])


@pytest.fixture
def setup(live: Live, client: TestClient, run_sql: RunSql) -> Setup:
    install_fake_tickets(live.app, get_settings().ws_ticket_ttl_seconds)
    headers = register(client, "alice@example.com")
    resume = new_file(client, run_sql, headers, "resumes")
    response = create(client, headers, body(resume, []))
    assert response.status_code == 201, response.text
    return Setup(live, headers, as_legacy(str(response.json()["id"])))


def test_a_real_client_connects_says_hello_and_pings(setup: Setup) -> None:
    with connect(setup.live.ws(setup.session_id, setup.ticket()), open_timeout=10) as ws:
        ready = json.loads(ws.recv(timeout=10))
        assert (
            ready["type"] == "session.ready" and ready["payload"]["session_id"] == setup.session_id
        )
        ws.send(json.dumps({"type": "client.hello", "payload": {"protocol_version": 1}}))
        ws.send(json.dumps({"type": "ping", "payload": {}}))
        assert json.loads(ws.recv(timeout=10))["type"] == "pong"


def test_every_refused_handshake_is_a_bare_403(setup: Setup) -> None:
    used = setup.ticket()
    with connect(setup.live.ws(setup.session_id, used), open_timeout=10):
        pass
    attempts = {
        "no ticket": (setup.live.ws(setup.session_id, None), {}),
        "garbage ticket": (setup.live.ws(setup.session_id, "garbage"), {}),
        "unknown ticket": (setup.live.ws(setup.session_id, "A" * 43), {}),
        "reused ticket": (setup.live.ws(setup.session_id, used), {}),
        "other session": (setup.live.ws(str(uuid.uuid4()), setup.ticket()), {}),
        "bad origin": (
            setup.live.ws(setup.session_id, setup.ticket()),
            {"Origin": "https://evil.example"},
        ),
    }
    seen: set[tuple[int, bytes]] = set()
    for name, (url, headers) in attempts.items():
        with pytest.raises(InvalidStatus) as caught:
            connect(url, additional_headers=headers, open_timeout=10)
        response = caught.value.response
        assert response.status_code == 403, name
        seen.add((response.status_code, bytes(response.body or b"")))
    assert len(seen) == 1, seen  # identical status and body for every cause


def test_the_ticket_never_appears_in_uvicorn_logs(setup: Setup) -> None:
    accepted = setup.ticket()
    rejected = "Z" * 43
    with connect(setup.live.ws(setup.session_id, accepted), open_timeout=10) as ws:
        ws.recv(timeout=10)
    with pytest.raises(InvalidStatus):
        connect(setup.live.ws(setup.session_id, rejected), open_timeout=10)
    with pytest.raises(InvalidStatus):
        connect(setup.live.ws(setup.session_id, accepted), open_timeout=10)  # reused
    time.sleep(0.3)
    text = "\n".join(setup.live.capture.lines)
    assert "/api/v1/ws/sessions/" in text, "the test must see the logged request target"
    assert "ticket=[redacted]" in text
    for secret in (accepted, rejected):
        assert secret not in text


def test_an_abrupt_disconnect_releases_the_connection(setup: Setup) -> None:
    registry = setup.live.app.state.connection_registry
    with connect(setup.live.ws(setup.session_id, setup.ticket()), open_timeout=10) as ws:
        ws.recv(timeout=10)
        assert wait_for(lambda: len(registry) == 1)
        ws.socket.close()  # no close handshake: the TCP connection just disappears
        assert wait_for(lambda: len(registry) == 0), "the slot of a dead connection was not freed"
    # and the user can connect again immediately
    with connect(setup.live.ws(setup.session_id, setup.ticket()), open_timeout=10) as again:
        assert json.loads(again.recv(timeout=10))["type"] == "session.ready"
