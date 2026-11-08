"""Connection bookkeeping and per-connection message-rate limiting (in-process, R-14/R-38).

`ConnectionRegistry` enforces one socket per (user, session) (a newer one supersedes the older),
a cap per user and a cap for the process. Both caps are checked only AFTER authentication, so they
reveal nothing to an unauthenticated caller.
"""

import asyncio
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass(eq=False)
class Connection:
    user_id: uuid.UUID
    session_id: uuid.UUID
    closing: asyncio.Event = field(default_factory=asyncio.Event)
    close_code: int | None = None

    def request_close(self, code: int) -> None:
        """Ask the connection's own task to close the socket (single writer, no races)."""
        if self.close_code is None:
            self.close_code = code
        self.closing.set()


@dataclass(frozen=True, slots=True)
class Admission:
    admitted: bool
    superseded: Connection | None = None


class ConnectionRegistry:
    def __init__(self, max_per_user: int, max_total: int) -> None:
        self._max_per_user = max_per_user
        self._max_total = max_total
        self._by_key: dict[tuple[uuid.UUID, uuid.UUID], Connection] = {}

    def __len__(self) -> int:
        return len(self._by_key)

    def count_for_user(self, user_id: uuid.UUID) -> int:
        return sum(1 for (user, _), _conn in self._by_key.items() if user == user_id)

    def admit(self, conn: Connection) -> Admission:
        key = (conn.user_id, conn.session_id)
        previous = self._by_key.get(key)
        # The connection being superseded frees its own slot, so it is not counted.
        others_total = len(self._by_key) - (1 if previous else 0)
        others_user = self.count_for_user(conn.user_id) - (1 if previous else 0)
        if others_user >= self._max_per_user or others_total >= self._max_total:
            return Admission(False)
        self._by_key[key] = conn
        return Admission(True, previous)

    def release(self, conn: Connection) -> None:
        key = (conn.user_id, conn.session_id)
        if self._by_key.get(key) is conn:  # a superseded socket must not evict its successor
            del self._by_key[key]


class MessageRateLimiter:
    """Sliding window of `max_messages` per `window_seconds` for ONE connection."""

    def __init__(
        self,
        max_messages: int,
        window_seconds: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max = max_messages
        self._window = window_seconds
        self._clock = clock
        self._events: deque[float] = deque()

    def allow(self) -> bool:
        now = self._clock()
        while self._events and now - self._events[0] >= self._window:
            self._events.popleft()
        if len(self._events) >= self._max:
            return False
        self._events.append(now)
        return True
