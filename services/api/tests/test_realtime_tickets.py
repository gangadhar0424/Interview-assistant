"""WebSocket ticket store (Phase 9A, D-41): hashed, single-use, short-lived, bound, never logged."""

import asyncio
import hashlib
import logging
import os
import uuid

import pytest
import redis.asyncio as aioredis

from app.realtime.tickets import KEY_PREFIX, TicketStore, TicketStoreUnavailable
from tests.realtime_fakes import FakeClock, FakeRedis

U = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
S = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")


def make() -> tuple[TicketStore, FakeRedis, FakeClock]:
    clock = FakeClock()
    redis = FakeRedis(clock)
    return TicketStore(redis, 30), redis, clock


def test_a_ticket_redeems_once_with_its_binding() -> None:
    async def go() -> None:
        store, _, _ = make()
        ticket = await store.issue(U, S)
        binding = await store.redeem(ticket)
        assert binding is not None and (binding.user_id, binding.session_id) == (U, S)
        assert await store.redeem(ticket) is None  # single use

    asyncio.run(go())


def test_a_ticket_expires_after_its_ttl() -> None:
    async def go() -> None:
        store, _, clock = make()
        fresh = await store.issue(U, S)
        clock.now += 29.9
        assert await store.redeem(fresh) is not None  # still valid just inside the TTL
        old = await store.issue(U, S)
        clock.now += 30
        assert await store.redeem(old) is None  # expired, and never redeemable afterwards

    asyncio.run(go())


def test_tickets_are_unique_random_and_well_formed() -> None:
    async def go() -> None:
        store, _, _ = make()
        tickets = {await store.issue(U, S) for _ in range(200)}
        assert len(tickets) == 200
        assert all(len(t) == 43 and t.replace("-", "").replace("_", "").isalnum() for t in tickets)

    asyncio.run(go())


def test_only_a_hash_is_stored_never_the_ticket() -> None:
    async def go() -> None:
        store, redis, _ = make()
        ticket = await store.issue(U, S)
        (key,) = redis.data
        assert key == KEY_PREFIX + hashlib.sha256(ticket.encode()).hexdigest()
        assert ticket not in key + redis.data[key][0]
        assert redis.data[key][0] == f"{U}:{S}"  # ids only

    asyncio.run(go())


def test_garbage_tickets_never_reach_redis() -> None:
    async def go() -> None:
        store, redis, _ = make()
        for bad in ["", "x", "a" * 42, "a" * 44, "../../etc", "a b" * 20, "é" * 43, "a" * 10_000]:
            assert await store.redeem(bad) is None
        assert redis.calls == []

    asyncio.run(go())


def test_an_unknown_but_well_formed_ticket_is_refused() -> None:
    async def go() -> None:
        store, _, _ = make()
        assert await store.redeem("A" * 43) is None

    asyncio.run(go())


def test_a_corrupt_stored_value_is_refused_and_consumed() -> None:
    async def go() -> None:
        store, redis, clock = make()
        ticket = await store.issue(U, S)
        (key,) = redis.data
        redis.data[key] = ("not-a-binding", clock.now + 30)
        assert await store.redeem(ticket) is None
        assert redis.data == {}

    asyncio.run(go())


def test_concurrent_redemption_has_exactly_one_winner() -> None:
    async def go() -> None:
        store, _, _ = make()
        ticket = await store.issue(U, S)
        results = await asyncio.gather(*(store.redeem(ticket) for _ in range(25)))
        assert sum(r is not None for r in results) == 1

    asyncio.run(go())


def test_a_redis_outage_is_reported_without_detail() -> None:
    async def go() -> None:
        store, redis, _ = make()
        redis.fail = True
        with pytest.raises(TicketStoreUnavailable) as issue_error:
            await store.issue(U, S)
        with pytest.raises(TicketStoreUnavailable) as redeem_error:
            await store.redeem("A" * 43)
        for error in (issue_error.value, redeem_error.value):
            assert "simulated" not in str(error) and error.__cause__ is None

    asyncio.run(go())


def test_the_store_never_logs(caplog: pytest.LogCaptureFixture) -> None:
    async def go() -> str:
        store, _, _ = make()
        ticket = await store.issue(U, S)
        await store.redeem(ticket)
        await store.redeem(ticket)
        return ticket

    with caplog.at_level(logging.DEBUG):
        ticket = asyncio.run(go())
    assert ticket not in caplog.text


# ---------------------------------------------------------------- real Redis (integration)
@pytest.mark.integration
def test_the_real_redis_getdel_is_atomic_single_use_and_expires() -> None:
    """Uses Redis database 15 and deletes only its own keys (the dev queue uses database 0)."""
    from dotenv import dotenv_values

    from app.core.config import REPO_ROOT

    base = os.environ.get("REDIS_URL_REAL") or dotenv_values(REPO_ROOT / ".env").get("REDIS_URL")
    if not base:
        pytest.skip("REDIS_URL not configured")
    url = base.rsplit("/", 1)[0] + "/15"

    async def go() -> None:
        client = aioredis.from_url(url, decode_responses=True)
        store = TicketStore(client, 5)
        issued: list[str] = []
        try:
            ticket = await store.issue(U, S)
            issued.append(KEY_PREFIX + hashlib.sha256(ticket.encode()).hexdigest())
            ttl = await client.ttl(issued[0])
            assert 0 < ttl <= 5
            results = await asyncio.gather(*(store.redeem(ticket) for _ in range(10)))
            assert sum(r is not None for r in results) == 1
            assert await client.exists(issued[0]) == 0
            short = TicketStore(client, 1)
            quick = await short.issue(U, S)
            issued.append(KEY_PREFIX + hashlib.sha256(quick.encode()).hexdigest())
            await asyncio.sleep(1.2)
            assert await short.redeem(quick) is None
        finally:
            if issued:
                await client.delete(*issued)
            await client.aclose()

    asyncio.run(go())
