"""Per-user protection of the question endpoint: request rate and concurrent generations.

In-process only (no Redis, same trade-off as the job-import limiter, R-30): with several API
workers the limits are per worker. The limiter is keyed by the authenticated user id.

`admit()` counts the attempt against the rate limit and reserves a generation slot; the caller MUST
release the returned `Slot` exactly when the generation ends (normal end, failure, refusal before
the stream starts, or client disconnect). `Slot.release()` is idempotent.
"""

import time
import uuid
from collections.abc import Callable

from app.core.errors import AppError
from app.jobs_import.limiter import SlidingWindowLimiter


class Slot:
    def __init__(self, limits: "QuestionLimits", user_id: uuid.UUID) -> None:
        self._limits = limits
        self._user_id = user_id
        self._released = False

    def release(self) -> None:
        if not self._released:
            self._released = True
            self._limits._release(self._user_id)


class QuestionLimits:
    def __init__(
        self,
        per_minute: int,
        max_concurrent: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._rate = SlidingWindowLimiter(per_minute, 60.0, clock)
        self._max_concurrent = max_concurrent
        self._active: dict[uuid.UUID, int] = {}

    def active(self, user_id: uuid.UUID) -> int:
        return self._active.get(user_id, 0)

    def admit(self, user_id: uuid.UUID) -> Slot:
        key = str(user_id)  # the authenticated user, never a request value
        if not self._rate.allow(key):
            raise AppError(
                429,
                "rate_limited",
                "Too many questions. Please wait a moment before asking again.",
                headers={"Retry-After": str(self._rate.retry_after(key))},
            )
        if self.active(user_id) >= self._max_concurrent:
            raise AppError(
                429,
                "too_many_generations",
                "Too many answers are being generated at once. Wait for one to finish.",
                headers={"Retry-After": "2"},
            )
        self._active[user_id] = self.active(user_id) + 1
        return Slot(self, user_id)

    def _release(self, user_id: uuid.UUID) -> None:
        remaining = self.active(user_id) - 1
        if remaining > 0:
            self._active[user_id] = remaining
        else:
            self._active.pop(user_id, None)
