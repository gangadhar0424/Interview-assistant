"""Per-session answer cache for validated general answers (D-66, Phase 4; purge on delete SEC-13).

Only the slow path is cached: a general-knowledge answer costs a model call plus validation, and an
interview often repeats or rephrases a generic question. Routing and retrieval always run fresh, so
a question that a document now covers is never served from the cache.

The key binds the user, the session, the normalised question, the answer preferences, the prompt
version and the selected materials, and is hashed, so one user's entry can never be read by another.
Entries are short-lived (`QA_ANSWER_CACHE_TTL_SECONDS`, at most 300 s outside local) and hold the
answer text, which is already stored in the database. The cache is best effort: any Redis error is
ignored and the answer is simply generated.

Purge on deletion (SEC-13 Decision 1, option 1b, D-85). Every entry is recorded in index sets:
one per (user, session), one per (user, source) for the resume and each selected document, and one
per user. Deleting a session, a resume, a document or the account removes every entry named in the
matching index. Index names and tombstones hold ids only, never question or answer text.

Race with an answer that finishes while its session is being deleted, without a Redis script:
  purge: (1) SET tombstone   (2) SMEMBERS index   (3) DEL members and index
  put:   (a) SADD indexes    (b) SET entry        (c) EXISTS tombstones -> DEL entry if any
Redis applies commands in one order. If (a) comes before (2), the purge finds and deletes the
entry. Otherwise (a) comes after (2), so (c) comes after (1) and the writer sees the tombstone and
deletes its own entry. Tombstones live longer than any generation can run, so a late writer always
sees one.
"""

import hashlib
import json
import logging
import re
import uuid
from collections.abc import Sequence
from typing import Any, Protocol

from app.core.metrics import answer_cache

logger = logging.getLogger(__name__)

KEY_PREFIX = "qa_general:"
INDEX_PREFIX = "qa_idx:"
TOMBSTONE_PREFIX = "qa_dead:"
# Longer than the longest generation (QA_REQUEST_TIMEOUT_SECONDS is capped at 900 s), so a writer
# that started before a deletion always finds the tombstone when it re-checks.
TOMBSTONE_TTL_SECONDS = 1800
_SPACE = re.compile(r"\s+")


class CacheClient(Protocol):
    """The commands used (a redis.asyncio client satisfies this)."""

    def get(self, name: str) -> Any: ...

    def set(self, name: str, value: str, *, ex: int) -> Any: ...

    def sadd(self, name: str, *values: str) -> Any: ...

    def smembers(self, name: str) -> Any: ...

    def expire(self, name: str, time: int) -> Any: ...

    def exists(self, *names: str) -> Any: ...

    def delete(self, *names: str) -> Any: ...


def normalise_question(question: str) -> str:
    return _SPACE.sub(" ", question.casefold()).strip(" ?!.\t\n")


def cache_key(
    user_id: uuid.UUID,
    session_id: uuid.UUID,
    question: str,
    preferences_digest: str,
    prompt_version: str,
    source_ids: Sequence[uuid.UUID],
) -> str:
    material = "|".join(
        [
            str(user_id),
            str(session_id),
            normalise_question(question),
            preferences_digest,
            prompt_version,
            ",".join(sorted(str(s) for s in source_ids)),
        ]
    )
    return KEY_PREFIX + hashlib.sha256(material.encode("utf-8")).hexdigest()


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def session_scope(user_id: uuid.UUID, session_id: uuid.UUID) -> str:
    return f"s:{user_id}:{session_id}"


def source_scope(user_id: uuid.UUID, source_id: uuid.UUID) -> str:
    return f"src:{user_id}:{source_id}"


def user_scope(user_id: uuid.UUID) -> str:
    return f"u:{user_id}"


def _text(value: Any) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


class AnswerCache:
    def __init__(self, client: CacheClient, ttl_seconds: int) -> None:
        self._client = client
        self.ttl_seconds = ttl_seconds

    @property
    def enabled(self) -> bool:
        return self.ttl_seconds > 0

    async def get(self, key: str) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        try:
            raw = await self._client.get(key)
        except Exception:  # noqa: BLE001 - best effort: a cache problem never blocks an answer
            logger.warning("answer cache unavailable")
            return None
        if raw is None:
            answer_cache.labels(result="miss").inc()
            return None
        try:
            data = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        except ValueError:
            return None
        if not isinstance(data, dict) or not isinstance(data.get("text"), str):
            return None
        answer_cache.labels(result="hit").inc()
        return data

    async def put(
        self,
        key: str,
        value: dict[str, Any],
        *,
        user_id: uuid.UUID,
        session_id: uuid.UUID,
        source_ids: Sequence[uuid.UUID] = (),
    ) -> None:
        if not self.enabled:
            return
        scopes = [
            user_scope(user_id),
            session_scope(user_id, session_id),
            *(source_scope(user_id, s) for s in dict.fromkeys(source_ids)),
        ]
        try:
            for scope in scopes:  # (a) index first, so a purge that reads the index finds it
                await self._client.sadd(INDEX_PREFIX + scope, key)
                # An index lives as long as its newest member; members never outlive it.
                await self._client.expire(INDEX_PREFIX + scope, self.ttl_seconds)
            await self._client.set(key, json.dumps(value), ex=self.ttl_seconds)  # (b)
            if await self._client.exists(*(TOMBSTONE_PREFIX + s for s in scopes)):  # (c)
                await self._client.delete(key)  # deleted meanwhile: never keep this entry
        except Exception:  # noqa: BLE001
            logger.warning("answer cache write failed")

    async def _purge(self, scope: str) -> int:
        """Remove every entry indexed under `scope`. Returns how many entries were named."""
        await self._client.set(TOMBSTONE_PREFIX + scope, "1", ex=TOMBSTONE_TTL_SECONDS)  # (1)
        members = [_text(m) for m in (await self._client.smembers(INDEX_PREFIX + scope)) or ()]
        keys = [m for m in members if m.startswith(KEY_PREFIX)]  # never delete a foreign key name
        await self._client.delete(*keys, INDEX_PREFIX + scope)  # (3)
        return len(keys)

    async def purge(self, scope: str) -> bool:
        """Best effort, like the rest of the cache: a failure is logged (ids only) and reported as
        False; it never fails the deletion that called it. Entries left behind by a failure still
        expire within the cache lifetime. Runs even when the cache is disabled, so entries written
        before a TTL change are still removed."""
        try:
            await self._purge(scope)
        except Exception:  # noqa: BLE001
            logger.warning("answer cache purge failed")
            return False
        return True

    async def purge_session(self, user_id: uuid.UUID, session_id: uuid.UUID) -> bool:
        return await self.purge(session_scope(user_id, session_id))

    async def purge_source(self, user_id: uuid.UUID, source_id: uuid.UUID) -> bool:
        return await self.purge(source_scope(user_id, source_id))

    async def purge_user(self, user_id: uuid.UUID) -> bool:
        return await self.purge(user_scope(user_id))
