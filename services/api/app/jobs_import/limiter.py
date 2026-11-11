"""In-process sliding-window rate limiter, keyed by authenticated user id.

Deliberately simple (no Redis, owner decision to keep infrastructure minimal): the limit is per API
process, so N workers allow up to N times the configured rate. Documented as R-30.
"""

import time
from collections import deque
from collections.abc import Callable

_PRUNE_THRESHOLD = 10_000


class SlidingWindowLimiter:
    def __init__(
        self,
        max_events: int,
        window_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max = max_events
        self._window = window_seconds
        self._clock = clock
        self._events: dict[str, deque[float]] = {}

    def allow(self, key: str) -> bool:
        """Record an attempt and return True if it is within the limit."""
        now = self._clock()
        events = self._events.setdefault(key, deque())
        while events and now - events[0] >= self._window:
            events.popleft()
        if len(events) >= self._max:
            return False
        events.append(now)
        if len(self._events) > _PRUNE_THRESHOLD:
            self._prune(now)
        return True

    def blocked(self, key: str) -> bool:
        """True if `key` is already at its limit. Does not record an attempt."""
        events = self._events.get(key)
        if not events:
            return False
        now = self._clock()
        while events and now - events[0] >= self._window:
            events.popleft()
        return len(events) >= self._max

    def record(self, key: str) -> None:
        """Record one event for `key` without checking the limit (used for failures)."""
        now = self._clock()
        self._events.setdefault(key, deque()).append(now)
        if len(self._events) > _PRUNE_THRESHOLD:
            self._prune(now)

    def reset(self, key: str) -> None:
        self._events.pop(key, None)

    def retry_after(self, key: str) -> int:
        events = self._events.get(key)
        if not events:
            return 1
        return max(1, int(self._window - (self._clock() - events[0])) + 1)

    def _prune(self, now: float) -> None:
        for key in [k for k, q in self._events.items() if not q or now - q[-1] >= self._window]:
            del self._events[key]
