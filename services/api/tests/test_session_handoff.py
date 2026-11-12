"""Web -> desktop handoff (D-55): issue, redeem, expiry, reuse, ownership, state, rate limits.

Runs the real application against the clean test database; Redis is replaced by `FakeRedis`
behind the production `TicketStore` (under the `handoff:` prefix).
"""

import asyncio
import hashlib
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.realtime.tickets import TicketStore
from tests.conftest import RunSql
from tests.helpers import register
from tests.realtime_fakes import FakeClock, FakeRedis
from tests.test_sessions import body, create, new_file

PREFIX = "handoff:"


@dataclass
class World:
    client: TestClient
    redis: FakeRedis
    clock: FakeClock
    alice: dict[str, str]
    bob: dict[str, str]
    alice_session: str
    bob_session: str
    run_sql: RunSql

    def issue(self, who: str = "alice", session: str | None = None) -> Any:
        headers = self.alice if who == "alice" else self.bob
        default = self.alice_session if who == "alice" else self.bob_session
        return self.client.post(f"/api/v1/sessions/{session or default}/handoff", headers=headers)

    def code(self, who: str = "alice", session: str | None = None) -> str:
        response = self.issue(who, session)
        assert response.status_code == 200, response.text
        code: str = response.json()["code"]
        return code

    def redeem(self, code: str, who: str = "alice") -> Any:
        headers = self.alice if who == "alice" else self.bob
        return self.client.post(
            "/api/v1/sessions/handoff/redeem", json={"code": code}, headers=headers
        )


def shape(response: Any) -> dict[str, Any]:
    """The error body without its per-request id."""
    error: dict[str, Any] = dict(response.json()["error"])
    error.pop("request_id", None)
    return error


@pytest.fixture
def build_world(make_client: Callable[..., TestClient], run_sql: RunSql) -> Callable[..., World]:
    def build(**env: str) -> World:
        client = make_client(**env)
        settings = get_settings()
        clock = FakeClock()
        redis = FakeRedis(clock)
        client.app.state.handoff_store = TicketStore(  # type: ignore[attr-defined]
            redis, settings.handoff_code_ttl_seconds, key_prefix=PREFIX
        )
        alice = register(client, "alice@example.com")
        bob = register(client, "bob@example.com")

        def session_for(headers: dict[str, str]) -> str:
            resume = new_file(client, run_sql, headers, "resumes")
            response = create(client, headers, body(resume, []))
            assert response.status_code == 201, response.text
            return str(response.json()["id"])

        return World(
            client, redis, clock, alice, bob, session_for(alice), session_for(bob), run_sql
        )

    return build


@pytest.fixture
def world(build_world: Callable[..., World]) -> World:
    return build_world()


def test_default_ttl_is_sixty_seconds(world: World) -> None:
    assert get_settings().handoff_code_ttl_seconds == 60


# ------------------------------------------------------------------------------------ issue
def test_issue_requires_authentication(world: World) -> None:
    response = world.client.post(f"/api/v1/sessions/{world.alice_session}/handoff")
    assert response.status_code == 401
    assert world.redis.data == {}


def test_issue_returns_an_opaque_non_cacheable_code_stored_only_as_a_hash(world: World) -> None:
    response = world.issue()
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    data = response.json()
    assert data["expires_in"] == 60
    code = data["code"]
    assert len(code) == 43 and code.replace("-", "").replace("_", "").isalnum()
    (key,) = world.redis.data
    assert key == PREFIX + hashlib.sha256(code.encode()).hexdigest()
    assert code not in key + world.redis.data[key][0]
    assert world.redis.data[key][0].count(":") == 1  # "<user_id>:<session_id>", ids only


def test_codes_are_unique(world: World) -> None:
    assert len({world.code() for _ in range(5)}) == 5


def test_issue_for_unknown_and_foreign_sessions_is_the_same_404(world: World) -> None:
    unknown = world.issue(session=str(uuid.uuid4()))
    foreign = world.issue(session=world.bob_session)
    assert unknown.status_code == foreign.status_code == 404
    assert shape(unknown) == shape(foreign)
    assert unknown.json()["error"]["code"] == "session_not_found"
    assert world.redis.data == {}  # nothing issued for a session the caller does not own


@pytest.mark.parametrize("status", ["draft", "active", "ended", "archived"])
def test_issue_refuses_sessions_that_are_not_startable(world: World, status: str) -> None:
    world.run_sql("UPDATE sessions SET status = :s WHERE id = :i", s=status, i=world.alice_session)
    response = world.issue()
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "session_not_startable"
    assert world.redis.data == {}


def test_issue_is_rate_limited_and_probing_counts(build_world: Callable[..., World]) -> None:
    w = build_world(HANDOFF_ISSUE_RATE_LIMIT_PER_MINUTE="3")
    for _ in range(2):
        assert w.issue(session=str(uuid.uuid4())).status_code == 404  # probes use the budget
    assert w.issue().status_code == 200
    limited = w.issue()
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "rate_limited"
    assert int(limited.headers["retry-after"]) >= 1
    assert w.issue("bob").status_code == 200  # another user is unaffected


def test_a_store_outage_fails_closed_on_issue(world: World) -> None:
    world.redis.fail = True
    response = world.issue()
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "handoff_unavailable"


# ---------------------------------------------------------------------------------- redeem
def test_a_valid_code_redeems_once_to_the_prepared_session(world: World) -> None:
    code = world.code()
    response = world.redeem(code)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    data = response.json()
    assert data["session"]["id"] == world.alice_session
    assert data["session"]["status"] == "ready"
    assert "answer_tone" in data["preferences"]
    # Approved summary only: no job text, URL or document list.
    assert set(data) == {"session", "preferences"}
    assert not {"job_description", "job_url", "documents"} & set(data["session"])
    assert code not in response.text


def test_redeeming_leaves_the_session_status_unchanged(world: World) -> None:
    assert world.redeem(world.code()).status_code == 200
    rows = world.run_sql("SELECT status FROM sessions WHERE id = :i", i=world.alice_session)
    assert rows[0]["status"] == "ready"


def test_a_reused_code_is_invalid(world: World) -> None:
    code = world.code()
    assert world.redeem(code).status_code == 200
    reused = world.redeem(code)
    assert reused.status_code == 404
    assert reused.json()["error"]["code"] == "handoff_invalid"


def test_an_expired_code_is_invalid(world: World) -> None:
    fresh = world.code()
    world.clock.now += 59.9
    assert world.redeem(fresh).status_code == 200  # still valid just inside the TTL
    old = world.code()
    world.clock.now += 60
    expired = world.redeem(old)
    assert expired.status_code == 404
    assert expired.json()["error"]["code"] == "handoff_invalid"


def test_another_users_code_is_invalid_and_is_consumed(world: World) -> None:
    code = world.code("alice")
    wrong = world.redeem(code, who="bob")
    assert wrong.status_code == 404
    assert wrong.json()["error"]["code"] == "handoff_invalid"
    assert world.redeem(code, who="alice").status_code == 404  # burned: fails closed


def test_all_failures_are_the_same_response(world: World) -> None:
    code = world.code()
    assert world.redeem(code).status_code == 200
    bodies = []
    for bad in ["x" * 43, "short", "a" * 128, "../../etc/passwd", code]:
        r = world.redeem(bad)
        assert r.status_code == 404
        bodies.append(shape(r))
    assert all(b == bodies[0] for b in bodies)
    assert shape(world.redeem(world.code("alice"), who="bob")) == bodies[0]


def test_garbage_codes_never_reach_redis(world: World) -> None:
    for bad in ["x", "a" * 42, "é" * 43, "a b" * 20]:
        assert world.redeem(bad).status_code == 404
    assert world.redis.calls == []


def test_redeem_rejects_extra_fields_and_missing_code(world: World) -> None:
    extra = world.client.post(
        "/api/v1/sessions/handoff/redeem",
        json={"code": "a" * 43, "user_id": str(uuid.uuid4())},
        headers=world.alice,
    )
    assert extra.status_code == 422
    missing = world.client.post("/api/v1/sessions/handoff/redeem", json={}, headers=world.alice)
    assert missing.status_code == 422


def test_redeem_requires_authentication(world: World) -> None:
    code = world.code()
    response = world.client.post("/api/v1/sessions/handoff/redeem", json={"code": code})
    assert response.status_code == 401
    assert world.redeem(code).status_code == 200  # the unauthenticated attempt consumed nothing


def test_a_deleted_or_ended_session_is_invalid_at_redemption(world: World) -> None:
    deleted = world.code()
    gone = world.client.delete(f"/api/v1/sessions/{world.alice_session}", headers=world.alice)
    assert gone.status_code == 204
    assert world.redeem(deleted).status_code == 404

    resume = new_file(world.client, world.run_sql, world.alice, "resumes")
    other = str(create(world.client, world.alice, body(resume, [])).json()["id"])
    code = world.code(session=other)
    world.run_sql("UPDATE sessions SET status = 'ended' WHERE id = :i", i=other)
    assert world.redeem(code).status_code == 404


def test_a_websocket_ticket_is_not_a_handoff_code(world: World) -> None:
    ws_store = TicketStore(world.redis, 30)  # the WebSocket namespace on the same Redis

    async def go() -> str:
        return await ws_store.issue(uuid.UUID(int=1), uuid.UUID(world.alice_session))

    assert world.redeem(asyncio.run(go())).status_code == 404


def test_redeem_is_rate_limited(build_world: Callable[..., World]) -> None:
    w = build_world(HANDOFF_REDEEM_RATE_LIMIT_PER_MINUTE="3")
    for _ in range(3):
        assert w.redeem("x" * 43).status_code == 404
    limited = w.redeem("x" * 43)
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "rate_limited"


def test_a_store_outage_fails_closed_on_redeem(world: World) -> None:
    code = world.code()
    world.redis.fail = True
    response = world.redeem(code)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "handoff_unavailable"


def test_the_code_is_never_logged(world: World, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    code = world.code()
    world.redeem(code)
    world.redeem(code)
    world.redeem(code, who="bob")
    assert code not in caplog.text
