"""WebSocket gateway, Phase 9A (D-41): tickets, handshake, hello, heartbeat, limits, isolation.

Runs the real application against the clean test database; Redis is replaced by `FakeRedis`
behind the production `TicketStore`. Q&A over WebSocket and cancel: test_realtime_qa.py (9B).
"""

import logging
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.core.config import get_settings
from app.realtime import schemas as proto
from tests.conftest import RunSql
from tests.helpers import as_legacy, register
from tests.realtime_fakes import FakeClock, FakeRedis, install_fake_tickets, request_ticket, ws_path
from tests.test_sessions import body, create, new_file

REFUSED = 1008


@dataclass
class World:
    client: TestClient
    redis: FakeRedis
    clock: FakeClock
    alice: dict[str, str]
    bob: dict[str, str]
    alice_id: uuid.UUID
    bob_id: uuid.UUID
    alice_session: str
    alice_session_2: str
    bob_session: str
    run_sql: RunSql

    def ticket(self, who: str = "alice", session: str | None = None) -> str:
        headers = self.alice if who == "alice" else self.bob
        default = self.alice_session if who == "alice" else self.bob_session
        return request_ticket(self.client, headers, session or default)

    def connect(self, who: str = "alice", session: str | None = None, **kw: Any) -> Any:
        default = self.alice_session if who == "alice" else self.bob_session
        return self.client.websocket_connect(
            ws_path(session or default, self.ticket(who, session)), **kw
        )

    def registry_size(self) -> int:
        return len(self.client.app.state.connection_registry)  # type: ignore[attr-defined]


@pytest.fixture
def build_world(make_client: Callable[..., TestClient], run_sql: RunSql) -> Callable[..., World]:
    def build(**env: str) -> World:
        client = make_client(**env)
        redis, clock = install_fake_tickets(client.app, get_settings().ws_ticket_ttl_seconds)
        alice = register(client, "alice@example.com")
        bob = register(client, "bob@example.com")

        def session_for(headers: dict[str, str]) -> str:
            resume = new_file(client, run_sql, headers, "resumes")
            response = create(client, headers, body(resume, []))
            assert response.status_code == 201, response.text
            return as_legacy(str(response.json()["id"]))

        def uid(email: str) -> uuid.UUID:
            value: uuid.UUID = run_sql("SELECT id FROM users WHERE email = :e", e=email)[0]["id"]
            return value

        return World(
            client,
            redis,
            clock,
            alice,
            bob,
            uid("alice@example.com"),
            uid("bob@example.com"),
            session_for(alice),
            session_for(alice),
            session_for(bob),
            run_sql,
        )

    return build


@pytest.fixture
def world(build_world: Callable[..., World]) -> World:
    return build_world()


class LogCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        extra = {
            k: v for k, v in record.__dict__.items() if k not in logging.makeLogRecord({}).__dict__
        }
        self.lines.append(f"{record.getMessage()} {extra}")


@pytest.fixture
def logs() -> Iterator[LogCapture]:
    handler = LogCapture()
    logger = logging.getLogger("app")
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    yield handler
    logger.removeHandler(handler)
    logger.setLevel(previous)


def hello(**payload: Any) -> dict[str, Any]:
    return {"type": "client.hello", "payload": {"protocol_version": 1, **payload}}


def ping() -> dict[str, Any]:
    return {"type": "ping", "payload": {}}


def expect_close(ws: Any) -> WebSocketDisconnect:
    with pytest.raises(WebSocketDisconnect) as caught:
        ws.receive_json()
    return caught.value


def refused(world: World, path: str, **kw: Any) -> WebSocketDisconnect:
    with pytest.raises(WebSocketDisconnect) as caught, world.client.websocket_connect(path, **kw):
        pass
    return caught.value


def say_hello(ws: Any) -> dict[str, Any]:
    ready: dict[str, Any] = ws.receive_json()
    ws.send_json(hello())
    return ready


# ============================================================================ ticket endpoint
def test_issuing_a_ticket_requires_authentication(world: World) -> None:
    response = world.client.post("/api/v1/auth/ws-ticket", json={"session_id": world.alice_session})
    assert response.status_code == 401
    assert world.redis.data == {}


def test_a_ticket_response_is_not_cacheable_and_stored_only_as_a_hash(world: World) -> None:
    response = world.client.post(
        "/api/v1/auth/ws-ticket", json={"session_id": world.alice_session}, headers=world.alice
    )
    assert response.status_code == 200
    data = response.json()
    assert set(data) == {"ticket", "expires_in"} and data["expires_in"] == 30
    assert response.headers["cache-control"] == "no-store"
    (key,) = world.redis.data
    assert data["ticket"] not in key and data["ticket"] not in world.redis.data[key][0]
    assert world.redis.data[key][0] == f"{world.alice_id}:{world.alice_session}"


def test_a_foreign_session_and_an_unknown_one_are_indistinguishable(world: World) -> None:
    def attempt(session_id: str) -> tuple[int, dict[str, Any]]:
        r = world.client.post(
            "/api/v1/auth/ws-ticket", json={"session_id": session_id}, headers=world.alice
        )
        payload = r.json()
        payload["error"].pop("request_id", None)
        return r.status_code, payload

    foreign = attempt(world.bob_session)
    unknown = attempt(str(uuid.uuid4()))
    assert foreign == unknown
    assert foreign[0] == 404 and foreign[1]["error"]["code"] == "session_not_found"
    assert world.redis.data == {}  # nothing was issued


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"session_id": "not-a-uuid"},
        {"session_id": None},
        {"session_id": "x", "user_id": "y"},
    ],
)
def test_ticket_request_bodies_are_validated(world: World, payload: dict[str, Any]) -> None:
    response = world.client.post("/api/v1/auth/ws-ticket", json=payload, headers=world.alice)
    assert response.status_code == 422


def test_the_user_id_in_a_ticket_request_is_rejected(world: World) -> None:
    response = world.client.post(
        "/api/v1/auth/ws-ticket",
        json={"session_id": world.bob_session, "user_id": str(world.bob_id)},
        headers=world.alice,
    )
    assert response.status_code == 422 and world.redis.data == {}


def test_ticket_requests_are_rate_limited_per_user_before_ownership(
    build_world: Callable[..., World],
) -> None:
    world = build_world(WS_TICKET_RATE_LIMIT_PER_MINUTE="3")
    foreign = {"session_id": world.bob_session}
    statuses = [
        world.client.post("/api/v1/auth/ws-ticket", json=foreign, headers=world.alice).status_code
        for _ in range(4)
    ]
    assert statuses == [404, 404, 404, 429]  # every attempt counts, even refused ones
    limited = world.client.post(
        "/api/v1/auth/ws-ticket", json={"session_id": world.alice_session}, headers=world.alice
    )
    assert limited.status_code == 429 and int(limited.headers["retry-after"]) >= 1
    assert limited.json()["error"]["code"] == "rate_limited"
    # another user is unaffected
    assert world.ticket("bob")


def test_a_disabled_user_cannot_get_a_ticket(world: World) -> None:
    world.run_sql("UPDATE users SET status = 'disabled' WHERE id = :i", i=world.alice_id)
    response = world.client.post(
        "/api/v1/auth/ws-ticket", json={"session_id": world.alice_session}, headers=world.alice
    )
    assert response.status_code == 401 and world.redis.data == {}


def test_a_redis_outage_gives_a_generic_503(world: World) -> None:
    world.redis.fail = True
    response = world.client.post(
        "/api/v1/auth/ws-ticket", json={"session_id": world.alice_session}, headers=world.alice
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "realtime_unavailable"
    assert "simulated" not in response.text and "redis" not in response.text.lower()


# ============================================================================ handshake
def test_a_valid_ticket_connects_and_the_welcome_has_the_documented_shape(world: World) -> None:
    with world.connect() as ws:
        ready = ws.receive_json()
        assert set(ready) == {"type", "id", "seq", "ts", "payload"}
        assert ready["type"] == "session.ready" and ready["seq"] == 0
        payload = ready["payload"]
        assert payload["session_id"] == world.alice_session
        assert payload["resume_seq"] == 0 and payload["protocol_version"] == 1
        settings = get_settings()
        assert payload["limits"] == {
            "max_frame_bytes": settings.ws_max_frame_bytes,
            "heartbeat_interval_seconds": settings.ws_heartbeat_interval_seconds,
            "idle_timeout_seconds": settings.ws_idle_timeout_seconds,
            "max_messages_per_second": settings.ws_max_messages_per_second,
            "resume_grace_seconds": settings.ws_resume_grace_seconds,
            "resume_buffer_events": settings.ws_resume_buffer_events,
        }
        assert proto.server_adapter.validate_python(ready).type == "session.ready"
        ws.send_json(hello())
        ws.send_json(ping())
        pong = ws.receive_json()
        assert pong["type"] == "pong" and pong["payload"] == {} and pong["seq"] == 0
        assert proto.server_adapter.validate_python(pong).type == "pong"


def test_every_handshake_failure_is_the_same_refusal(world: World) -> None:
    sid = world.alice_session
    used = world.ticket()
    with world.client.websocket_connect(ws_path(sid, used)):
        pass  # consumes the ticket
    stale = world.ticket()
    world.clock.now += 31  # expired
    other_session_ticket = world.ticket(session=world.alice_session_2)
    bob_ticket = world.ticket("bob")
    deleted_ticket = world.ticket(session=world.alice_session_2)
    disabled_ticket = world.ticket()

    outcomes = {
        "missing": refused(world, ws_path(sid, None)),
        "empty": refused(world, ws_path(sid, "")),
        "garbage": refused(world, ws_path(sid, "not-a-ticket")),
        "unknown": refused(world, ws_path(sid, "A" * 43)),
        "reused": refused(world, ws_path(sid, used)),
        "expired": refused(world, ws_path(sid, stale)),
        "other_session": refused(world, ws_path(sid, other_session_ticket)),
        "bobs_ticket_on_alices_session": refused(world, ws_path(sid, bob_ticket)),
        "bad_session_id": refused(world, ws_path("not-a-uuid", world.ticket())),
    }
    world.run_sql("DELETE FROM sessions WHERE id = :i", i=world.alice_session_2)
    outcomes["deleted_session"] = refused(world, ws_path(world.alice_session_2, deleted_ticket))
    world.run_sql("UPDATE users SET status = 'disabled' WHERE id = :i", i=world.alice_id)
    outcomes["disabled_user"] = refused(world, ws_path(sid, disabled_ticket))
    assert {(o.code, o.reason) for o in outcomes.values()} == {(REFUSED, "")}, {
        k: (v.code, v.reason) for k, v in outcomes.items()
    }


def test_a_ticket_bound_to_another_users_session_is_refused_even_with_a_valid_path(
    world: World,
) -> None:
    """Defence in depth: a ticket naming (bob, alice's session) never opens that session."""
    import asyncio

    store = world.client.app.state.ticket_store  # type: ignore[attr-defined]
    forged = asyncio.run(store.issue(world.bob_id, uuid.UUID(world.alice_session)))
    assert refused(world, ws_path(world.alice_session, forged)).code == REFUSED
    # and it cannot be replayed afterwards (it was consumed by the failed attempt)
    assert refused(world, ws_path(world.alice_session, forged)).code == REFUSED


def test_a_ticket_for_one_session_cannot_open_another_session_of_the_same_user(
    world: World,
) -> None:
    ticket = world.ticket(session=world.alice_session)
    assert refused(world, ws_path(world.alice_session_2, ticket)).code == REFUSED
    # the mismatch consumed it: it no longer opens the session it was issued for either
    assert refused(world, ws_path(world.alice_session, ticket)).code == REFUSED


def test_a_ticket_is_single_use_across_connections(world: World) -> None:
    ticket = world.ticket()
    with world.client.websocket_connect(ws_path(world.alice_session, ticket)) as ws:
        assert ws.receive_json()["type"] == "session.ready"
    assert refused(world, ws_path(world.alice_session, ticket)).code == REFUSED


def test_a_user_disabled_after_the_ticket_was_issued_is_refused(world: World) -> None:
    ticket = world.ticket()
    world.run_sql("UPDATE users SET status = 'disabled' WHERE id = :i", i=world.alice_id)
    assert refused(world, ws_path(world.alice_session, ticket)).code == REFUSED


def test_a_session_deleted_after_the_ticket_was_issued_is_refused(world: World) -> None:
    ticket = world.ticket()
    world.run_sql("DELETE FROM sessions WHERE id = :i", i=world.alice_session)
    assert refused(world, ws_path(world.alice_session, ticket)).code == REFUSED


def test_the_origin_is_checked_when_present(world: World) -> None:
    sid = world.alice_session
    bad = refused(world, ws_path(sid, world.ticket()), headers={"origin": "https://evil.example"})
    assert bad.code == REFUSED
    ok_headers = {"origin": "http://localhost:3000"}
    with world.client.websocket_connect(ws_path(sid, world.ticket()), headers=ok_headers) as ws:
        assert ws.receive_json()["type"] == "session.ready"
    # no Origin header at all (non-browser client such as the desktop app) is fine
    with world.client.websocket_connect(ws_path(sid, world.ticket())) as ws:
        assert ws.receive_json()["type"] == "session.ready"


def test_a_refused_origin_does_not_consume_the_ticket(world: World) -> None:
    ticket = world.ticket()
    path = ws_path(world.alice_session, ticket)
    assert refused(world, path, headers={"origin": "https://evil.example"}).code == REFUSED
    with world.client.websocket_connect(path) as ws:
        assert ws.receive_json()["type"] == "session.ready"


def test_a_ticket_store_outage_refuses_the_handshake(world: World) -> None:
    ticket = world.ticket()
    world.redis.fail = True
    assert refused(world, ws_path(world.alice_session, ticket)).code == REFUSED


# ============================================================================ protocol rules
def test_the_first_message_must_be_hello(build_world: Callable[..., World]) -> None:
    world = build_world(WS_HELLO_TIMEOUT_SECONDS="3")
    with world.connect() as ws:
        ws.receive_json()
        started = time.monotonic()
        ws.send_json(ping())
        error = ws.receive_json()
        # rejected at once, not merely closed later by the hello timeout (same error and code)
        assert time.monotonic() - started < 1.5
        assert error["type"] == "error" and error["payload"] == {
            "code": "protocol_error",
            "message": proto.ERROR_MESSAGES["protocol_error"],
            "fatal": True,
        }
        assert expect_close(ws).code == proto.CLOSE_PROTOCOL_ERROR


@pytest.mark.parametrize(
    "first",
    [
        "not json",
        "[]",
        '{"type":"nope","payload":{}}',
        '{"type":"client.hello","payload":{"protocol_version":1,"user_id":"x"}}',
    ],
)
def test_an_unparseable_first_message_is_fatal(world: World, first: str) -> None:
    with world.connect() as ws:
        ws.receive_json()
        ws.send_text(first)
        assert ws.receive_json()["payload"]["fatal"] is True
        assert expect_close(ws).code == proto.CLOSE_PROTOCOL_ERROR


def test_an_unsupported_protocol_version_is_fatal(world: World) -> None:
    with world.connect() as ws:
        ws.receive_json()
        ws.send_json({"type": "client.hello", "payload": {"protocol_version": 2}})
        error = ws.receive_json()
        assert error["payload"]["code"] == "unsupported_protocol_version"
        assert expect_close(ws).code == proto.CLOSE_UNSUPPORTED_VERSION


def test_a_second_hello_is_fatal(world: World) -> None:
    with world.connect() as ws:
        say_hello(ws)
        ws.send_json(hello())
        assert ws.receive_json()["payload"]["code"] == "protocol_error"
        assert expect_close(ws).code == proto.CLOSE_PROTOCOL_ERROR


def test_resume_is_not_available_before_9c_and_hello_zero_is_fine(world: World) -> None:
    with world.connect() as ws:
        ws.receive_json()
        ws.send_json(hello(resume_from_seq=0))
        ws.send_json(ping())
        assert ws.receive_json()["type"] == "pong"  # no error for resume_from_seq 0
    with world.connect() as ws:
        ws.receive_json()
        ws.send_json(hello(resume_from_seq=5))
        error = ws.receive_json()
        assert error["payload"] == {
            "code": "resume_unavailable",
            "message": proto.ERROR_MESSAGES["resume_unavailable"],
            "fatal": False,
        }
        ws.send_json(ping())  # the connection stays usable
        assert ws.receive_json()["type"] == "pong"


def test_invalid_and_unknown_messages_are_non_fatal_until_the_strike_limit(world: World) -> None:
    with world.connect() as ws:
        say_hello(ws)
        ws.send_text("{broken")
        assert ws.receive_json()["payload"]["code"] == "invalid_message"
        ws.send_json({"type": "audio.start", "payload": {}})
        assert ws.receive_json()["payload"]["code"] == "unknown_message_type"
        ws.send_json(ping())
        assert ws.receive_json()["type"] == "pong"  # still usable
        for _ in range(2):
            ws.send_text("{broken")
            assert ws.receive_json()["payload"]["fatal"] is False
        ws.send_text("{broken")  # the fifth strike
        assert ws.receive_json()["payload"]["fatal"] is True
        assert expect_close(ws).code == proto.CLOSE_PROTOCOL_ERROR


def test_cancel_without_a_running_generation_is_a_non_fatal_error_since_9b(world: World) -> None:
    # 9A answered question.submit / answer.cancel with `not_available`; 9B implements them
    # (the Q&A flows are in test_realtime_qa.py). The code is no longer sent.
    with world.connect() as ws:
        say_hello(ws)
        ws.send_json({"type": "answer.cancel", "payload": {"question_id": str(uuid.uuid4())}})
        error = ws.receive_json()
        assert error["payload"] == {
            "code": "nothing_to_cancel",
            "message": proto.ERROR_MESSAGES["nothing_to_cancel"],
            "fatal": False,
        }
        ws.send_json(ping())
        assert ws.receive_json()["type"] == "pong"


def test_identity_fields_in_messages_are_never_accepted(world: World) -> None:
    with world.connect() as ws:
        say_hello(ws)
        for extra in ({"user_id": str(world.bob_id)}, {"session_id": world.bob_session}):
            ws.send_json({"type": "ping", "payload": {}, **extra})
            assert ws.receive_json()["payload"]["code"] == "invalid_message"


def test_binary_frames_close_the_socket(world: World) -> None:
    with world.connect() as ws:
        say_hello(ws)
        ws.send_bytes(b"\x00\x01\x02")
        assert expect_close(ws).code == proto.CLOSE_UNSUPPORTED_DATA


def test_oversized_frames_close_the_socket(build_world: Callable[..., World]) -> None:
    world = build_world(WS_MAX_FRAME_BYTES="256")
    with world.connect() as ws:
        say_hello(ws)
        ws.send_text(" " * 257)
        assert expect_close(ws).code == proto.CLOSE_TOO_BIG
    with world.connect() as ws:
        say_hello(ws)
        ws.send_text("é" * 200)  # 200 characters but 400 bytes
        assert expect_close(ws).code == proto.CLOSE_TOO_BIG
    with world.connect() as ws:
        say_hello(ws)
        ws.send_json(ping())  # a frame within the limit is fine
        assert ws.receive_json()["type"] == "pong"


def test_message_rate_is_limited(build_world: Callable[..., World]) -> None:
    world = build_world(WS_MAX_MESSAGES_PER_SECOND="3")
    with world.connect() as ws:
        ws.receive_json()
        ws.send_json(hello())  # 1
        ws.send_json(ping())  # 2
        ws.send_json(ping())  # 3
        ws.send_json(ping())  # 4: over the limit
        assert [ws.receive_json()["type"] for _ in range(2)] == ["pong", "pong"]
        assert expect_close(ws).code == proto.CLOSE_RATE_LIMITED


def test_the_hello_must_arrive_in_time(build_world: Callable[..., World]) -> None:
    world = build_world(WS_HELLO_TIMEOUT_SECONDS="0.5")
    with world.connect() as ws:
        ws.receive_json()
        started = time.monotonic()
        assert ws.receive_json()["payload"]["code"] == "protocol_error"
        assert expect_close(ws).code == proto.CLOSE_PROTOCOL_ERROR
        assert 0.4 <= time.monotonic() - started < 3


def test_silence_closes_the_socket_and_pings_keep_it_alive(
    build_world: Callable[..., World],
) -> None:
    world = build_world(WS_IDLE_TIMEOUT_SECONDS="1.0", WS_HEARTBEAT_INTERVAL_SECONDS="0.4")
    with world.connect() as ws:
        say_hello(ws)
        for _ in range(4):  # 1.6 s in total: longer than the idle timeout
            time.sleep(0.4)
            ws.send_json(ping())
            assert ws.receive_json()["type"] == "pong"
        started = time.monotonic()
        outcome: list[WebSocketDisconnect] = []

        def wait_for_close() -> None:
            with pytest.raises(WebSocketDisconnect) as caught:
                ws.receive_json()
            outcome.append(caught.value)

        waiter = threading.Thread(target=wait_for_close, daemon=True)
        waiter.start()
        waiter.join(4)  # bounded: a gateway without an idle timeout fails here instead of hanging
        assert not waiter.is_alive(), "the idle socket was never closed"
        assert outcome[0].code == proto.CLOSE_IDLE_TIMEOUT
        assert 0.7 <= time.monotonic() - started < 4


# ============================================================================ connection rules
def test_a_newer_connection_supersedes_the_older_one(world: World) -> None:
    with world.connect() as first:
        say_hello(first)
        with world.connect() as second:
            say_hello(second)
            assert expect_close(first).code == proto.CLOSE_SUPERSEDED
            second.send_json(ping())
            assert second.receive_json()["type"] == "pong"
            assert world.registry_size() == 1  # the superseded socket did not evict its successor
    assert wait_for(lambda: world.registry_size() == 0)


def test_the_same_session_for_two_users_never_supersedes(world: World) -> None:
    with world.connect("alice") as a, world.connect("bob") as b:
        say_hello(a)
        say_hello(b)
        a.send_json(ping())
        b.send_json(ping())
        assert a.receive_json()["type"] == "pong" and b.receive_json()["type"] == "pong"
        assert world.registry_size() == 2


def test_the_per_user_connection_cap_applies_after_authentication(
    build_world: Callable[..., World],
) -> None:
    world = build_world(WS_MAX_CONNECTIONS_PER_USER="1")
    with world.connect(session=world.alice_session) as first:
        say_hello(first)
        with world.connect(session=world.alice_session_2) as second:
            error = second.receive_json()
            assert error["payload"]["code"] == "too_many_connections" and error["payload"]["fatal"]
            assert expect_close(second).code == proto.CLOSE_TOO_MANY_CONNECTIONS
        first.send_json(ping())  # the existing connection is untouched
        assert first.receive_json()["type"] == "pong"
        with world.connect("bob") as other_user:  # other users are unaffected
            assert other_user.receive_json()["type"] == "session.ready"
    assert wait_for(lambda: world.registry_size() == 0)
    with world.connect(session=world.alice_session_2) as again:  # capacity was released
        assert again.receive_json()["type"] == "session.ready"


def test_the_total_connection_cap(build_world: Callable[..., World]) -> None:
    world = build_world(WS_MAX_TOTAL_CONNECTIONS="1", WS_MAX_CONNECTIONS_PER_USER="1")
    with world.connect("alice") as first:
        first.receive_json()
        with world.connect("bob") as second:
            assert second.receive_json()["payload"]["code"] == "too_many_connections"
            assert expect_close(second).code == proto.CLOSE_TOO_MANY_CONNECTIONS


def test_capacity_is_released_however_the_connection_ends(
    build_world: Callable[..., World],
) -> None:
    world = build_world(WS_MAX_FRAME_BYTES="256")
    with world.connect() as ws:  # client closes
        say_hello(ws)
    assert wait_for(lambda: world.registry_size() == 0)
    with world.connect() as ws:  # server closes (protocol error)
        ws.receive_json()
        ws.send_text("junk")
        expect_close_after_error(ws)
    assert wait_for(lambda: world.registry_size() == 0)
    with world.connect() as ws:  # server closes (frame too large)
        say_hello(ws)
        ws.send_text(" " * 300)
        expect_close(ws)
    assert wait_for(lambda: world.registry_size() == 0)


def expect_close_after_error(ws: Any) -> None:
    assert ws.receive_json()["type"] == "error"
    expect_close(ws)


def wait_for(condition: Callable[[], bool], timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


# ============================================================================ logging
def test_logs_never_contain_the_ticket_or_message_content(world: World, logs: LogCapture) -> None:
    ticket = world.ticket()
    canary = "CANARY-QUESTION-TEXT-777"
    with world.client.websocket_connect(ws_path(world.alice_session, ticket)) as ws:
        say_hello(ws)
        ws.send_json({"type": "question.submit", "payload": {"text": canary}})
        ws.receive_json()
    refused(world, ws_path(world.alice_session, ticket))  # reused ticket
    refused(world, ws_path(world.alice_session, "B" * 43))
    text = "\n".join(logs.lines)
    assert ticket not in text and "B" * 43 not in text and canary not in text
    assert "ws connected" in text and "ws closed" in text
    assert "reason" in text  # a fixed reason code for refusals


def test_refusals_log_a_fixed_reason_code_only(world: World, logs: LogCapture) -> None:
    refused(world, ws_path(world.alice_session, "A" * 43))
    refused(world, ws_path(world.alice_session, None))
    reasons = [line for line in logs.lines if line.startswith("ws refused")]
    assert any("ticket_invalid" in r for r in reasons) and any(
        "ticket_missing" in r for r in reasons
    )
    assert all(world.alice_session not in r and str(world.alice_id) not in r for r in reasons)
