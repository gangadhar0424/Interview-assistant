"""Screenshot extraction (Phase 11A, D-51): validate a PNG, ask a vision provider, return text.

Nothing is persisted. The image exists only as in-memory bytes for the duration of the request;
there is no file, table, cache or queue. The extracted text is untrusted: it is cleaned, capped at
the question limit and returned to the user for review; the answer pipeline only ever sees it after
the user explicitly asks it as a question.

Privacy: logs, metrics and usage records carry identifiers, byte counts, dimensions, character
counts, status and latency only. Never the image, any digest of it, or the extracted text.
"""

import logging
import time
import uuid
from collections.abc import Callable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.vision import ImageInput
from app.ai.vision_router import VisionRouter
from app.core.config import Settings
from app.core.errors import AppError
from app.core.metrics import screenshot_extractions
from app.db.session import set_tenant_context
from app.preferences.prompt_builder import build_vision_extraction_prompt
from app.preferences.sanitize import clean_text
from app.retrieval.service import MAX_QUERY_CHARS
from app.screenshots.schemas import ScreenshotText
from app.screenshots.validation import INVALID_IMAGE_MESSAGE, InvalidImage, validate_png
from app.sessions import repository as sessions_repository
from app.sessions.service import session_not_found

logger = logging.getLogger("app.screenshots")


def vision_unavailable() -> AppError:
    return AppError(503, "vision_unavailable", "Screenshot input is not available on this server.")


def too_large() -> AppError:
    return AppError(413, "screenshot_too_large", "The screenshot is too large.")


def _count(status: str) -> None:
    try:
        screenshot_extractions.labels(status=status).inc()
    except Exception:  # noqa: BLE001, S110 - telemetry is best effort
        pass


class ScreenshotExtractor:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
        vision: VisionRouter,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._settings = settings
        self._vision = vision
        self._clock = clock

    @property
    def available(self) -> bool:
        return self._vision.available

    @property
    def max_bytes(self) -> int:
        return self._settings.screenshot_max_bytes

    async def require_ready_session(self, user_id: uuid.UUID, session_id: uuid.UUID) -> None:
        """A short transaction; no database session is open while the provider is called."""
        async with self._sessionmaker() as db:
            await set_tenant_context(db, user_id)
            row = await sessions_repository.get_for_user(db, user_id, session_id)
            if row is None:
                raise session_not_found()  # another user's session looks exactly like a missing one
            if row.status != "ready":
                _count("session_not_ready")
                raise AppError(
                    409, "session_not_ready", "Screenshots can only be used in a ready session."
                )

    async def extract(
        self, user_id: uuid.UUID, session_id: uuid.UUID, data: bytes
    ) -> ScreenshotText:
        started = self._clock()
        if len(data) > self.max_bytes:
            _count("too_large")
            raise too_large()
        try:
            png = validate_png(
                data,
                max_long_edge=self._settings.screenshot_max_long_edge,
                max_pixels=self._settings.screenshot_max_pixels,
            )
        except InvalidImage:
            _count("invalid_image")
            logger.info(
                "screenshot rejected",
                extra={
                    "session_id": str(session_id),
                    "user_id": str(user_id),
                    "size_bytes": len(data),
                },
            )
            raise AppError(422, "invalid_image", INVALID_IMAGE_MESSAGE) from None
        image = ImageInput(png.data, png.width, png.height)
        try:
            result = await self._vision.extract(
                build_vision_extraction_prompt(), image, user_id=user_id
            )
        except AppError:
            raise
        except Exception as exc:
            _count("provider_error")
            logger.info(
                "screenshot extraction failed",
                extra={
                    "session_id": str(session_id),
                    "user_id": str(user_id),
                    "size_bytes": image.size_bytes,
                    "width": image.width,
                    "height": image.height,
                    "error_code": getattr(exc, "code", type(exc).__name__),
                },
            )
            raise
        text = clean_text(result.text)
        truncated = len(text) > MAX_QUERY_CHARS
        if truncated:
            text = text[:MAX_QUERY_CHARS].rstrip()
        latency_ms = int((self._clock() - started) * 1000)
        _count("ok" if text else "empty")
        logger.info(
            "screenshot extracted",
            extra={
                "session_id": str(session_id),
                "user_id": str(user_id),
                "size_bytes": image.size_bytes,
                "width": image.width,
                "height": image.height,
                "provider": result.provider,
                "model": result.model,
                "text_chars": len(text),
                "truncated": truncated,
                "latency_ms": latency_ms,
            },
        )
        return ScreenshotText(
            extracted_text=text,
            truncated=truncated,
            provider=result.provider,
            model=result.model,
            latency_ms=latency_ms,
        )
