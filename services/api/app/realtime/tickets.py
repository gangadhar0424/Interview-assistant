"""Single-use WebSocket tickets (D-41).

A ticket is 32 random bytes (urlsafe). Only its SHA-256 is stored, as `ws_ticket:<hash>` ->
`<user_id>:<session_id>` with a short TTL (`SET NX EX`), so a Redis dump or log never contains a
usable ticket. Redemption is one atomic `GETDEL`: it succeeds once, for the holder of the exact
ticket, and the stored binding names the user and the session the ticket was issued for. The
production store is always Redis (it must work across API workers); tests drive this same class
with a fake Redis client. The ticket itself is never logged.
"""

import hashlib
import re
import secrets
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

KEY_PREFIX = "ws_ticket:"
_TICKET_SHAPE = re.compile(r"[A-Za-z0-9_-]{43}")  # token_urlsafe(32) is always 43 characters


class RedisClient(Protocol):
    """The two commands used (a redis.asyncio client satisfies this)."""

    def set(self, name: str, value: str, *, ex: int, nx: bool) -> Any: ...

    def getdel(self, name: str) -> Any: ...


class TicketStoreUnavailable(Exception):  # noqa: N818 - infrastructure condition, not a bug
    """Redis could not be reached; carries no detail."""


@dataclass(frozen=True, slots=True)
class TicketBinding:
    user_id: uuid.UUID
    session_id: uuid.UUID


def _key(ticket: str) -> str:
    return KEY_PREFIX + hashlib.sha256(ticket.encode("ascii")).hexdigest()


class TicketStore:
    def __init__(self, client: RedisClient, ttl_seconds: int) -> None:
        self._client = client
        self.ttl_seconds = ttl_seconds

    async def issue(self, user_id: uuid.UUID, session_id: uuid.UUID) -> str:
        ticket = secrets.token_urlsafe(32)
        try:
            stored = await self._client.set(
                _key(ticket), f"{user_id}:{session_id}", ex=self.ttl_seconds, nx=True
            )
        except Exception:
            raise TicketStoreUnavailable from None
        if not stored:  # a 256-bit collision cannot happen; fail closed rather than reuse a key
            raise TicketStoreUnavailable
        return ticket

    async def redeem(self, ticket: str) -> TicketBinding | None:
        """Consume the ticket. None for anything that is not a live, unused ticket."""
        if not _TICKET_SHAPE.fullmatch(ticket):  # garbage never reaches Redis
            return None
        try:
            raw = await self._client.getdel(_key(ticket))
        except Exception:
            raise TicketStoreUnavailable from None
        if raw is None:
            return None
        text = raw.decode("ascii", "replace") if isinstance(raw, bytes) else str(raw)
        try:
            user, session = text.split(":")
            return TicketBinding(uuid.UUID(user), uuid.UUID(session))
        except ValueError:
            return None
