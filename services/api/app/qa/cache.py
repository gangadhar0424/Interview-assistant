"""Per-session answer cache for validated general answers (D-66, Phase 4).

Only the slow path is cached: a general-knowledge answer costs a model call plus validation, and an
interview often repeats or rephrases a generic question. Routing and retrieval always run fresh, so
a question that a document now covers is never served from the cache.

The key binds the user, the session, the normalised question, the answer preferences, the prompt
version and the selected materials, and is hashed, so one user's entry can never be read by another.
Entries are short-lived (`QA_ANSWER_CACHE_TTL_SECONDS`) and hold the answer text, which is already
stored in the database. The cache is best effort: any Redis error is ignored and the answer is
simply generated.
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
_SPACE = re.compile(r"\s+")


class CacheClient(Protocol):
    """The two commands used (a redis.asyncio client satisfies this)."""

    def get(self, name: str) -> Any: ...

    def set(self, name: str, value: str, *, ex: int) -> Any: ...


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

    async def put(self, key: str, value: dict[str, Any]) -> None:
        if not self.enabled:
            return
        try:
            await self._client.set(key, json.dumps(value), ex=self.ttl_seconds)
        except Exception:  # noqa: BLE001
            logger.warning("answer cache write failed")
