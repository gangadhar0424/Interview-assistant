"""Per-user protection of the transcription endpoint: request rate and concurrent requests.

In-process only (same trade-off as `QuestionLimits`/`ScreenshotLimits`, R-30). Keyed by the
authenticated user id, never by a request value. `admit()` counts the attempt and reserves a slot
that the caller MUST release exactly when the request ends; `AudioSlot.release()` is idempotent.
"""

import time
import uuid
from collections.abc import Callable

from app.core.errors import AppError
from app.jobs_import.limiter import SlidingWindowLimiter


class AudioSlot:
    def __init__(self, limits: "AudioLimits", user_id: uuid.UUID) -> None:
        self._limits = limits
        self._user_id = user_id
        self._released = False

    def release(self) -> None:
        if not self._released:
            self._released = True
            self._limits._release(self._user_id)


class AudioLimits:
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

    def admit(self, user_id: uuid.UUID) -> AudioSlot:
        key = str(user_id)
        if not self._rate.allow(key):
            raise AppError(
                429,
                "rate_limited",
                "Too much audio is being sent. Please wait a moment.",
                headers={"Retry-After": str(self._rate.retry_after(key))},
            )
        if self.active(user_id) >= self._max_concurrent:
            raise AppError(
                429,
                "too_many_transcriptions",
                "Audio is still being processed. Wait for it to finish.",
                headers={"Retry-After": "2"},
            )
        self._active[user_id] = self.active(user_id) + 1
        return AudioSlot(self, user_id)

    def _release(self, user_id: uuid.UUID) -> None:
        remaining = self.active(user_id) - 1
        if remaining > 0:
            self._active[user_id] = remaining
        else:
            self._active.pop(user_id, None)
