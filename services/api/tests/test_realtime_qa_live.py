"""Phase 9B over a REAL socket: uvicorn in a thread and a real WebSocket client.

TestClient cannot disconnect abruptly or stop reading, so these tests start the actual application
and verify what only a real socket shows:
- a full question, and a cancel, through the real ASGI server;
- a client that vanishes mid-stream (abrupt TCP drop, or a normal close) leaves a `cancelled`
  answer with its partial text, a closed provider stream, a released concurrency slot and a freed
  connection;
- a client that stops reading is closed as a slow consumer (4010) while generation is NOT stalled,
  everything is cleaned up, and another user's connection keeps working.
"""

import json
import logging
import socket
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import ClientConnection, connect

from app.ai.fake import FakeLLMProvider
from app.ai.types import GenerationConfig, Message, Token, Usage
from app.core.config import get_settings
from app.embeddings.fake import FakeEmbeddingProvider
from app.qa.limits import QuestionLimits
from app.qa.service import QuestionAnswerer
from tests.conftest import RunSql
from tests.helpers import register
from tests.qa_fakes import SlowTokensProvider
from tests.realtime_fakes import install_fake_tickets
from tests.test_qa_service import answers, make_router
from tests.test_realtime_live import Live, wait_for
from tests.test_realtime_live import live as live  # noqa: F401  (fixture reused)
from tests.test_sessions import body, create, new_file

TERMINAL = ("answer.done", "answer.error", "answer.cancelled")


class FloodProvider(FakeLLMProvider):
    """Produces huge tokens as fast as it is allowed to: a generation no client can keep up with."""

    def __init__(self, provider_id: str = "flood", *, tokens: int = 400, size: int = 65536) -> None:
        super().__init__(provider_id)
        self.tokens = tokens
        self.size = size
        self.emitted = 0
        self.closed = False
        self.finished = False

    async def stream_chat(
        self, messages: Sequence[Message], model: str, config: GenerationConfig
    ) -> Any:
        self.calls += 1
        chunk = "y" * self.size
        try:
            for _ in range(self.tokens):
                self.emitted += 1
                yield Token(chunk)
            self.finished = True
            yield Usage(1, self.tokens)
        finally:
            self.closed = True


class Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def closed_codes(self) -> list[int]:
        return [r.close_code for r in self.records if r.getMessage() == "ws closed"]  # type: ignore[attr-defined]


@dataclass
class Person:
    headers: dict[str, str]
    session_id: str


@dataclass
class Rig:
    live: Live
    alice: Person
    bob: Person
    limits: QuestionLimits
    records: Records
    run_sql: RunSql
    ready: dict[str, Any] = field(default_factory=dict)

    def ticket(self, who: Person) -> str:
        response = httpx.post(
            self.live.http("/api/v1/auth/ws-ticket"),
            json={"session_id": who.session_id},
            headers=who.headers,
            timeout=10,
        )
        assert response.status_code == 200, response.text
        return str(response.json()["ticket"])

    def open(self, who: Person, resume_from_seq: int | None = None) -> ClientConnection:
        ws = connect(self.live.ws(who.session_id, self.ticket(who)), open_timeout=10)
        self.ready = json.loads(ws.recv(timeout=10))  # session.ready
        payload: dict[str, Any] = {"protocol_version": 1}
        if resume_from_seq is not None:
            payload["resume_from_seq"] = resume_from_seq
        ws.send(json.dumps({"type": "client.hello", "payload": payload}))
        return ws

    def history(self, who: Person) -> list[dict[str, Any]]:
        """The stored questions and answers (the source of truth), through the REST endpoint."""
        response = httpx.get(
            self.live.http(f"/api/v1/sessions/{who.session_id}/questions"),
            headers=who.headers,
            timeout=10,
        )
        assert response.status_code == 200, response.text
        items: list[dict[str, Any]] = response.json()["items"]
        return items

    def user_id(self, who: Person) -> Any:
        row = self.run_sql("SELECT user_id FROM sessions WHERE id = :i", i=who.session_id)[0]
        return row["user_id"]

    def registry_size(self) -> int:
        return len(self.live.app.state.connection_registry)


def say(ws: ClientConnection, text: str = "Tell me about yourself") -> None:
    ws.send(json.dumps({"type": "question.submit", "payload": {"text": text}}))


def read_until(ws: ClientConnection, *types: str, timeout: float = 20.0) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    deadline = time.monotonic() + timeout
    while True:
        message = json.loads(ws.recv(timeout=max(0.1, deadline - time.monotonic())))
        out.append(message)
        if message["type"] in types:
            return out


@pytest.fixture
def small_grace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WS_RESUME_GRACE_SECONDS", "1")


@pytest.fixture
def small_buffers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WS_SEND_QUEUE_EVENTS", "10")
    monkeypatch.setenv("WS_SEND_TIMEOUT_SECONDS", "1")


def _build(server: Live, client: TestClient, run_sql: RunSql, provider: Any) -> Iterator[Rig]:
    install_fake_tickets(server.app, get_settings().ws_ticket_ttl_seconds)
    app: Any = server.app
    limits = QuestionLimits(1000, 1000)
    app.state.question_answerer = QuestionAnswerer(
        app.state.sessionmaker, get_settings(), make_router([provider]), FakeEmbeddingProvider()
    )
    app.state.question_limits = limits

    def person(email: str) -> Person:
        headers = register(client, email)
        resume = new_file(client, run_sql, headers, "resumes")
        response = create(client, headers, body(resume, []))
        assert response.status_code == 201, response.text
        return Person(headers, str(response.json()["id"]))

    logger = logging.getLogger("app.realtime")
    previous = logger.level
    records = Records()
    logger.addHandler(records)
    logger.setLevel(logging.INFO)
    try:
        yield Rig(
            server, person("alice@example.com"), person("bob@example.com"), limits, records, run_sql
        )
    finally:
        logger.removeHandler(records)
        logger.setLevel(previous)


@pytest.fixture
def rig_factory(
    request: pytest.FixtureRequest, client: TestClient, run_sql: RunSql
) -> Iterator[Callable[[Any], Rig]]:
    generators: list[Iterator[Rig]] = []

    def make(provider: Any) -> Rig:
        server: Live = request.getfixturevalue("live")  # the real uvicorn server (imported above)
        generator = _build(server, client, run_sql, provider)
        generators.append(generator)
        return next(generator)

    yield make
    for generator in generators:
        for _ in generator:
            pass


# ----------------------------------------------------------------------------- normal flows
def test_a_full_question_over_a_real_socket(rig_factory: Callable[[Any], Rig]) -> None:
    rig = rig_factory(SlowTokensProvider(token_delay=0.01, tokens=8))
    ws = rig.open(rig.alice)
    try:
        say(ws)
        events = read_until(ws, *TERMINAL)
    finally:
        ws.close()
    kinds = [m["type"] for m in events]
    assert kinds[:2] == ["question.created", "retrieval.done"] and kinds[-1] == "answer.done"
    assert [m["seq"] for m in events] == list(range(1, len(events) + 1))
    assert wait_for(lambda: rig.limits.active(rig.user_id(rig.alice)) == 0)
    assert wait_for(lambda: rig.registry_size() == 0)


def test_cancel_over_a_real_socket(rig_factory: Callable[[Any], Rig]) -> None:
    provider = SlowTokensProvider(token_delay=0.05, tokens=400)
    rig = rig_factory(provider)
    ws = rig.open(rig.alice)
    try:
        say(ws)
        head = read_until(ws, "answer.delta")
        ws.send(
            json.dumps(
                {
                    "type": "answer.cancel",
                    "payload": {"question_id": head[0]["payload"]["question_id"]},
                }
            )
        )
        tail = read_until(ws, "answer.cancelled")
    finally:
        ws.close()
    assert wait_for(lambda: provider.closed and not provider.finished)
    (answer,) = answers(rig.run_sql)
    assert answer["status"] == "cancelled"
    assert answer["text"] == "".join(
        m["payload"]["text"] for m in head + tail if m["type"] == "answer.delta"
    )
    assert wait_for(lambda: rig.limits.active(rig.user_id(rig.alice)) == 0)


# ----------------------------------------------------------------------------- disconnects
@pytest.mark.parametrize("abrupt", [True, False])
def test_a_client_that_disconnects_mid_stream_leaves_nothing_behind_after_the_grace(
    small_grace: None, rig_factory: Callable[[Any], Rig], abrupt: bool
) -> None:
    provider = SlowTokensProvider(token_delay=0.05, tokens=400)
    rig = rig_factory(provider)
    ws = rig.open(rig.alice)
    say(ws)
    read_until(ws, "answer.delta")
    if abrupt:  # the TCP connection just disappears: no close frame
        ws.socket.shutdown(socket.SHUT_RDWR)
        ws.socket.close()
    else:
        ws.close()
    uid = rig.user_id(rig.alice)
    assert wait_for(lambda: rig.registry_size() == 0, timeout=10)  # the connection is freed ...
    assert rig.limits.active(uid) == 1  # ... but the detached generation still runs (grace)
    assert wait_for(lambda: rig.limits.active(uid) == 0, timeout=10)  # until the 1 s expiry
    assert wait_for(lambda: provider.closed, timeout=10) and not provider.finished
    assert wait_for(lambda: answers(rig.run_sql)[0]["status"] == "cancelled", timeout=10)
    assert answers(rig.run_sql)[0]["text"] != ""  # the partial text is kept
    assert provider.emitted < provider.tokens  # generation really stopped


def test_a_second_connection_after_a_disconnect_and_the_grace_works_normally(
    small_grace: None, rig_factory: Callable[[Any], Rig]
) -> None:
    provider = SlowTokensProvider(token_delay=0.05, tokens=400)
    rig = rig_factory(provider)
    first = rig.open(rig.alice)
    say(first)
    read_until(first, "answer.delta")
    first.socket.shutdown(socket.SHUT_RDWR)
    first.socket.close()
    uid = rig.user_id(rig.alice)
    assert wait_for(lambda: rig.limits.active(uid) == 0, timeout=10)
    provider.tokens = 3
    second = rig.open(rig.alice)
    try:
        say(second, "After the drop")
        assert read_until(second, *TERMINAL)[-1]["type"] == "answer.done"
    finally:
        second.close()


# ----------------------------------------------------------------------------- slow consumer
def test_a_client_that_stops_reading_is_closed_as_a_slow_consumer_without_losing_the_answer(
    small_buffers: None, rig_factory: Callable[[Any], Rig]
) -> None:
    provider = FloodProvider()
    rig = rig_factory(provider)
    ws = rig.open(rig.alice)
    say(ws)  # ... and now the client reads NOTHING
    uid = rig.user_id(rig.alice)
    assert wait_for(lambda: 4010 in rig.records.closed_codes(), timeout=30), "not closed (4010)"
    assert wait_for(lambda: rig.registry_size() == 0, timeout=10)  # the connection is released
    # Generation was never held back by the slow client, and the close did not cancel it (9C):
    # it ran to the end detached, the slot was released, and the database holds the whole answer.
    assert wait_for(lambda: provider.finished, timeout=30)
    assert wait_for(lambda: rig.limits.active(uid) == 0, timeout=10)
    assert wait_for(lambda: answers(rig.run_sql)[0]["status"] == "complete", timeout=10)
    assert len(answers(rig.run_sql)[0]["text"]) == provider.tokens * provider.size
    # The client, once it reads again, finds the connection ended (it does not hang).
    deadline = time.monotonic() + 20
    closed = False
    while time.monotonic() < deadline and not closed:
        try:
            ws.recv(timeout=5)
        except ConnectionClosed:
            closed = True
        except TimeoutError:
            break
    assert closed
    assert not any("yyyy" in r.getMessage() for r in rig.records.records)  # no text in logs


def test_one_slow_client_does_not_stall_another_user(
    small_buffers: None, rig_factory: Callable[[Any], Rig]
) -> None:
    class Mixed(FloodProvider):
        """Floods for the first request, answers quickly for any later one."""

        async def stream_chat(
            self, messages: Sequence[Message], model: str, config: GenerationConfig
        ) -> Any:
            if self.calls == 0:
                async for event in super().stream_chat(messages, model, config):
                    yield event
                return
            self.calls += 1
            yield Token("a short answer")
            yield Usage(1, 3)

    rig = rig_factory(Mixed())
    stalled = rig.open(rig.alice)
    say(stalled)  # Alice never reads
    time.sleep(0.5)
    bob = rig.open(rig.bob)
    try:
        started = time.monotonic()
        say(bob)
        assert read_until(bob, *TERMINAL, timeout=10)[-1]["type"] == "answer.done"
        assert time.monotonic() - started < 8  # Bob was not held up by Alice's stuck socket
    finally:
        bob.close()
    assert wait_for(lambda: 4010 in rig.records.closed_codes(), timeout=30)
