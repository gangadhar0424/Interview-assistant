"""SEC-13 Decision 1 (option 1b, D-85): cached answers are purged when their session, source or
account is deleted, without touching another user's entries, and an answer that finishes while its
session is being deleted never leaves an entry behind."""

import asyncio
import builtins
import os
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from dotenv import dotenv_values

from app.core.config import REPO_ROOT
from app.qa.cache import (
    INDEX_PREFIX,
    KEY_PREFIX,
    TOMBSTONE_PREFIX,
    AnswerCache,
    cache_key,
)

U1, U2 = uuid.uuid4(), uuid.uuid4()
S1, S2, S3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
R1, D1, D2 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
SECRET_QUESTION = "what is the CANARY-QUESTION-7731 about"
SECRET_ANSWER = "CANARY-ANSWER-5512"


class FakeRedis:
    """In-memory Redis with the commands the cache uses, a clock for expiry, and a hook that runs
    before a named command (to force interleavings)."""

    def __init__(self) -> None:
        self.now = 0.0
        self.values: dict[str, tuple[Any, float | None]] = {}
        self.before: dict[str, Callable[[], Awaitable[None]]] = {}
        self.fail = False

    async def _enter(self, command: str) -> None:
        if self.fail:
            raise ConnectionError("redis down")
        hook = self.before.pop(command, None)
        if hook is not None:
            await hook()

    def _live(self, name: str) -> Any:
        item = self.values.get(name)
        if item is None:
            return None
        value, expires = item
        if expires is not None and expires <= self.now:
            del self.values[name]
            return None
        return value

    async def get(self, name: str) -> Any:
        await self._enter("get")
        value = self._live(name)
        return value if isinstance(value, str) else None

    async def set(self, name: str, value: str, *, ex: int) -> bool:
        await self._enter("set")
        self.values[name] = (value, self.now + ex)
        return True

    async def sadd(self, name: str, *values: str) -> int:
        await self._enter("sadd")
        current = self._live(name)
        members: builtins.set[str] = (
            current if isinstance(current, builtins.set) else builtins.set()
        )
        expires = self.values[name][1] if name in self.values else None
        members.update(values)
        self.values[name] = (members, expires)
        return len(values)

    async def smembers(self, name: str) -> builtins.set[str]:
        await self._enter("smembers")
        value = self._live(name)
        return builtins.set(value) if isinstance(value, builtins.set) else builtins.set()

    async def expire(self, name: str, time: int) -> bool:
        await self._enter("expire")
        if self._live(name) is None:
            return False
        self.values[name] = (self.values[name][0], self.now + time)
        return True

    async def exists(self, *names: str) -> int:
        await self._enter("exists")
        return sum(1 for n in names if self._live(n) is not None)

    async def delete(self, *names: str) -> int:
        await self._enter("delete")
        return sum(1 for n in names if self.values.pop(n, None) is not None)

    def keys(self) -> builtins.set[str]:
        return {k for k in list(self.values) if self._live(k) is not None}


def run[T](coro: Awaitable[T]) -> T:
    async def go() -> T:
        return await coro

    return asyncio.run(go())


def key_for(user: uuid.UUID, session: uuid.UUID, q: str, sources: list[uuid.UUID]) -> str:
    return cache_key(user, session, q, "prefs", "v5", sources)


async def put(
    cache: AnswerCache, user: uuid.UUID, session: uuid.UUID, q: str, sources: list[uuid.UUID]
) -> str:
    key = key_for(user, session, q, sources)
    await cache.put(
        key, {"text": SECRET_ANSWER}, user_id=user, session_id=session, source_ids=sources
    )
    return key


def test_deleting_a_session_removes_only_that_sessions_entries() -> None:
    redis = FakeRedis()
    cache = AnswerCache(redis, 300)

    async def scenario() -> tuple[str, str, str]:
        a = await put(cache, U1, S1, "q one", [R1])
        b = await put(cache, U1, S1, "q two", [R1, D1])
        c = await put(cache, U1, S2, "q one", [R1])
        assert await cache.purge_session(U1, S1)
        return a, b, c

    a, b, c = run(scenario())
    assert a not in redis.keys() and b not in redis.keys()
    assert c in redis.keys()  # same user, other session: kept
    assert run(cache.get(c)) is not None


def test_deleting_a_document_or_resume_removes_every_entry_that_used_it() -> None:
    redis = FakeRedis()
    cache = AnswerCache(redis, 300)

    async def scenario() -> list[str]:
        uses_d1 = await put(cache, U1, S1, "q", [R1, D1])
        uses_d1_other_session = await put(cache, U1, S2, "q", [R1, D1])
        without_d1 = await put(cache, U1, S3, "q", [R1, D2])
        assert await cache.purge_source(U1, D1)
        return [uses_d1, uses_d1_other_session, without_d1]

    uses_d1, other, without = run(scenario())
    assert uses_d1 not in redis.keys() and other not in redis.keys()
    assert without in redis.keys()
    run(cache.purge_source(U1, R1))  # the resume was used by the remaining entry
    assert without not in redis.keys()


def test_deleting_an_account_removes_all_of_its_entries_and_no_one_elses() -> None:
    redis = FakeRedis()
    cache = AnswerCache(redis, 300)

    async def scenario() -> tuple[list[str], list[str]]:
        mine = [await put(cache, U1, S1, "q", [R1]), await put(cache, U1, S2, "x", [D1])]
        theirs = [await put(cache, U2, S3, "q", [D2])]
        assert await cache.purge_user(U1)
        return mine, theirs

    mine, theirs = run(scenario())
    assert not set(mine) & redis.keys()
    assert set(theirs) <= redis.keys()


def test_another_users_deletion_with_the_same_session_id_cannot_touch_my_entries() -> None:
    """Index names are scoped by owner, so a purge keyed by another user never matches."""
    redis = FakeRedis()
    cache = AnswerCache(redis, 300)
    mine = run(put(cache, U1, S1, "q", [R1, D1]))
    run(cache.purge_session(U2, S1))
    run(cache.purge_source(U2, D1))
    run(cache.purge_user(U2))
    assert mine in redis.keys()


def test_no_question_or_answer_text_appears_in_any_key_index_or_tombstone() -> None:
    redis = FakeRedis()
    cache = AnswerCache(redis, 300)
    run(put(cache, U1, S1, SECRET_QUESTION, [R1]))
    run(cache.purge_session(U1, S2))
    for name in redis.keys():
        assert "CANARY" not in name
        value = redis.values[name][0]
        if name.startswith((INDEX_PREFIX, TOMBSTONE_PREFIX)):
            assert "CANARY" not in repr(value)


def test_an_answer_finishing_during_deletion_never_survives_in_any_interleaving() -> None:
    """The purge runs at each point inside the writer's sequence; the entry must never remain."""
    for point in ("sadd", "expire", "set", "exists"):
        redis = FakeRedis()
        cache = AnswerCache(redis, 300)

        async def purge(c: AnswerCache = cache) -> None:
            await c.purge_session(U1, S1)

        redis.before[point] = purge
        key = run(put(cache, U1, S1, "q", [R1]))
        assert key not in redis.keys(), f"entry survived a purge run before {point}"


def test_a_writer_that_finishes_after_the_deletion_does_not_recreate_the_entry() -> None:
    redis = FakeRedis()
    cache = AnswerCache(redis, 300)
    run(cache.purge_session(U1, S1))  # the session is deleted first
    key = run(put(cache, U1, S1, "q", [R1]))  # a slow generation finishes afterwards
    assert key not in redis.keys()


def test_entries_and_indexes_expire_and_purging_expired_entries_is_harmless() -> None:
    redis = FakeRedis()
    cache = AnswerCache(redis, 300)
    key = run(put(cache, U1, S1, "q", [R1]))
    redis.now += 301
    assert key not in redis.keys()
    assert not any(k.startswith(INDEX_PREFIX) for k in redis.keys())  # indexes expire too
    assert run(cache.purge_session(U1, S1))  # nothing left; still succeeds


def test_an_index_outlives_every_entry_it_names() -> None:
    redis = FakeRedis()
    cache = AnswerCache(redis, 300)
    run(put(cache, U1, S1, "first", [R1]))
    redis.now += 200
    second = run(put(cache, U1, S1, "second", [R1]))
    redis.now += 200  # the first entry and the original index lifetime have passed
    assert second in redis.keys()
    assert run(cache.purge_session(U1, S1))
    assert second not in redis.keys()


def test_a_redis_failure_during_purge_is_reported_not_raised() -> None:
    redis = FakeRedis()
    cache = AnswerCache(redis, 300)
    run(put(cache, U1, S1, "q", [R1]))
    redis.fail = True
    assert run(cache.purge_session(U1, S1)) is False


def test_a_purge_only_deletes_cache_entry_names() -> None:
    """A corrupted index naming a non-cache key never deletes that key."""
    redis = FakeRedis()
    cache = AnswerCache(redis, 300)
    run(redis.set("ws_ticket:abc", "u:s", ex=30))
    run(redis.sadd(f"{INDEX_PREFIX}s:{U1}:{S1}", "ws_ticket:abc"))
    run(cache.purge_session(U1, S1))
    assert "ws_ticket:abc" in redis.keys()


def test_purge_still_runs_when_the_cache_is_disabled() -> None:
    redis = FakeRedis()
    run(put(AnswerCache(redis, 300), U1, S1, "q", [R1]))
    assert run(AnswerCache(redis, 0).purge_session(U1, S1))
    assert not any(k.startswith(KEY_PREFIX) for k in redis.keys())


# ------------------------------------------------------------------ real Redis


def _real_redis_url() -> str | None:
    return os.environ.get("REDIS_URL_REAL") or dotenv_values(REPO_ROOT / ".env").get("REDIS_URL")


def test_concurrent_writers_and_a_purge_on_real_redis_leave_nothing_behind() -> None:
    """Many answers finish concurrently with the session's deletion on a real Redis: afterwards no
    entry of that session exists, while another session's entries are untouched."""
    url = _real_redis_url()
    if not url:
        pytest.skip("REDIS_URL not configured")
    import redis.asyncio as aioredis

    async def go() -> tuple[int, int]:
        client = aioredis.from_url(url, decode_responses=True, socket_connect_timeout=2)  # type: ignore[no-untyped-call]
        try:
            await client.ping()
        except Exception:  # noqa: BLE001
            await client.aclose()
            pytest.skip("Redis not reachable")
        user, gone, kept = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        cache = AnswerCache(client, 300)
        try:
            writers = [put(cache, user, gone, f"q{i}", [R1]) for i in range(60)]
            others = [put(cache, user, kept, f"q{i}", [R1]) for i in range(5)]
            results = await asyncio.gather(
                *writers[:30], cache.purge_session(user, gone), *writers[30:], *others
            )
            gone_keys = [r for r in results[:30] + results[31:61] if isinstance(r, str)]
            kept_keys = [r for r in results[61:] if isinstance(r, str)]
            surviving = sum(await asyncio.gather(*(client.exists(k) for k in gone_keys)))
            present = sum(await asyncio.gather(*(client.exists(k) for k in kept_keys)))
            return surviving, present
        finally:
            await cache.purge_user(user)
            await client.aclose()

    surviving, present = asyncio.run(go())
    assert surviving == 0
    assert present == 5
