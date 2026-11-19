"""Duplicate-request protection for `POST /sessions` (M-9).

A client that loses the response to a create (network drop, timeout) retries with the same
`Idempotency-Key`; the server then returns the session it already made instead of a second one.
Keys live in Redis (works across API workers) as `idem:session:<sha256(user:key)>`, so a key can
never match another user's request and a Redis dump holds no usable client value. A claim is an
atomic `SET NX`; it holds `pending` while the create runs and the session id afterwards.

Redis being down must not stop users from creating sessions: the store then reports
`unavailable` and the create proceeds without protection (documented limitation).
"""

import hashlib
import logging
import re
import uuid
from enum import Enum
from typing import Any, Protocol

logger = logging.getLogger(__name__)

KEY_PREFIX = "idem:session:"
TTL_SECONDS = 24 * 3600
PENDING_TTL_SECONDS = 60  # a crashed create frees its key quickly
PENDING = "pending"
_KEY_SHAPE = re.compile(r"[A-Za-z0-9_.:-]{8,128}")


class RedisClient(Protocol):
    def set(self, name: str, value: str, *, ex: int, nx: bool) -> Any: ...

    def get(self, name: str) -> Any: ...

    def delete(self, name: str) -> Any: ...


class Claim(Enum):
    NEW = "new"  # first sight: go ahead and create
    DONE = "done"  # already created: the session id is returned
    IN_PROGRESS = "in_progress"  # the original request is still running
    UNAVAILABLE = "unavailable"  # Redis down: proceed unprotected


def valid_key(key: str) -> bool:
    return _KEY_SHAPE.fullmatch(key) is not None


def _redis_key(user_id: uuid.UUID, key: str) -> str:
    return KEY_PREFIX + hashlib.sha256(f"{user_id}:{key}".encode()).hexdigest()


class IdempotencyStore:
    def __init__(self, client: RedisClient) -> None:
        self._client = client

    async def claim(self, user_id: uuid.UUID, key: str) -> tuple[Claim, uuid.UUID | None]:
        name = _redis_key(user_id, key)
        try:
            if await self._client.set(name, PENDING, ex=PENDING_TTL_SECONDS, nx=True):
                return Claim.NEW, None
            value = await self._client.get(name)
        except Exception:
            logger.warning("idempotency store unavailable")
            return Claim.UNAVAILABLE, None
        if value is None:  # expired between the two calls: treat as a fresh request
            return Claim.NEW, None
        try:
            return Claim.DONE, uuid.UUID(str(value))
        except ValueError:
            return Claim.IN_PROGRESS, None

    async def complete(self, user_id: uuid.UUID, key: str, session_id: uuid.UUID) -> None:
        try:
            await self._client.set(
                _redis_key(user_id, key), str(session_id), ex=TTL_SECONDS, nx=False
            )
        except Exception:
            logger.warning("idempotency store unavailable")

    async def release(self, user_id: uuid.UUID, key: str) -> None:
        """The create failed: let the client retry with the same key."""
        try:
            await self._client.delete(_redis_key(user_id, key))
        except Exception:
            logger.warning("idempotency store unavailable")
