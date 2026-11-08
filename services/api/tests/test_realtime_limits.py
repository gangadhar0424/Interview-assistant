"""Connection registry and message-rate limiter (Phase 9A)."""

import uuid

from app.realtime.limits import Connection, ConnectionRegistry, MessageRateLimiter
from tests.realtime_fakes import FakeClock


def conn(user: uuid.UUID, session: uuid.UUID) -> Connection:
    return Connection(user, session)


def test_a_newer_connection_supersedes_the_older_one_for_the_same_user_and_session() -> None:
    registry = ConnectionRegistry(3, 10)
    user, session = uuid.uuid4(), uuid.uuid4()
    first, second = conn(user, session), conn(user, session)
    assert registry.admit(first).superseded is None
    admission = registry.admit(second)
    assert admission.admitted and admission.superseded is first
    assert len(registry) == 1
    registry.release(first)  # the superseded socket's cleanup must not evict its successor
    assert len(registry) == 1
    registry.release(second)
    assert len(registry) == 0


def test_the_same_session_id_for_two_users_is_two_connections() -> None:
    registry = ConnectionRegistry(3, 10)
    session = uuid.uuid4()
    a, b = conn(uuid.uuid4(), session), conn(uuid.uuid4(), session)
    assert registry.admit(a).superseded is None and registry.admit(b).superseded is None
    assert len(registry) == 2


def test_the_per_user_cap_counts_across_sessions_and_other_users_are_unaffected() -> None:
    registry = ConnectionRegistry(2, 10)
    user, other = uuid.uuid4(), uuid.uuid4()
    assert registry.admit(conn(user, uuid.uuid4())).admitted
    assert registry.admit(conn(user, uuid.uuid4())).admitted
    assert not registry.admit(conn(user, uuid.uuid4())).admitted
    assert registry.admit(conn(other, uuid.uuid4())).admitted


def test_the_total_cap_applies_and_a_refused_connection_supersedes_nothing() -> None:
    registry = ConnectionRegistry(5, 2)
    user = uuid.uuid4()
    first = conn(user, uuid.uuid4())
    assert registry.admit(first).admitted
    assert registry.admit(conn(uuid.uuid4(), uuid.uuid4())).admitted
    assert not registry.admit(conn(uuid.uuid4(), uuid.uuid4())).admitted
    assert len(registry) == 2
    # replacing an existing connection frees its own slot even at the cap
    again = conn(user, first.session_id)
    assert registry.admit(again).superseded is first


def test_releasing_frees_capacity() -> None:
    registry = ConnectionRegistry(1, 5)
    user = uuid.uuid4()
    first = conn(user, uuid.uuid4())
    registry.admit(first)
    assert not registry.admit(conn(user, uuid.uuid4())).admitted
    registry.release(first)
    assert registry.admit(conn(user, uuid.uuid4())).admitted


def test_request_close_keeps_the_first_code() -> None:
    c = conn(uuid.uuid4(), uuid.uuid4())
    c.request_close(4001)
    c.request_close(4002)
    assert c.close_code == 4001 and c.closing.is_set()


def test_message_rate_limiter_allows_n_per_window_then_recovers() -> None:
    clock = FakeClock()
    limiter = MessageRateLimiter(3, 1.0, clock)
    assert [limiter.allow() for _ in range(5)] == [True, True, True, False, False]
    clock.now += 1.0
    assert limiter.allow()
