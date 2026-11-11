"""Utterance transcription (Phase 12, D-53): validate WAV, ask a provider, return text.

Nothing is persisted. The audio exists only as in-memory bytes for the duration of the request;
there is no file, table, cache or queue. The transcript is untrusted: it is cleaned, capped at the
question limit and returned to the user. Only if the user (or an opt-in auto-answer) asks it as a
question does it enter the answer pipeline, through the normal question endpoint.

Privacy: logs, metrics and usage records carry identifiers, byte counts, durations, character
counts, status and latency only. Never the audio, any digest of it, or the transcript.
"""

import logging
import time
import uuid
from collections.abc import Callable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.stt import AudioInput
from app.ai.stt_router import SttRouter
from app.audio.schemas import TranscriptionOut
from app.audio.validation import INVALID_AUDIO_MESSAGE, SAMPLE_RATE, InvalidAudio, validate_wav
from app.core.config import Settings
from app.core.errors import AppError
from app.core.metrics import audio_transcriptions
from app.db.session import set_tenant_context
from app.preferences.sanitize import clean_text
from app.qa.detection import classify
from app.retrieval.service import MAX_QUERY_CHARS
from app.sessions import repository as sessions_repository
from app.sessions.service import session_not_found

logger = logging.getLogger("app.audio")


def stt_unavailable() -> AppError:
    return AppError(503, "stt_unavailable", "Audio input is not available on this server.")


def audio_too_large() -> AppError:
    return AppError(413, "audio_too_large", "The audio is too large.")


def _count(status: str) -> None:
    try:
        audio_transcriptions.labels(status=status).inc()
    except Exception:  # noqa: BLE001, S110 - telemetry is best effort
        pass


class Transcriber:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
        stt: SttRouter,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._settings = settings
        self._stt = stt
        self._clock = clock

    @property
    def available(self) -> bool:
        return self._stt.available

    @property
    def max_bytes(self) -> int:
        return self._settings.audio_max_bytes

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
                    409, "session_not_ready", "Audio can only be used in a ready session."
                )

    async def transcribe(
        self, user_id: uuid.UUID, session_id: uuid.UUID, data: bytes
    ) -> TranscriptionOut:
        started = self._clock()
        if len(data) > self.max_bytes:
            _count("too_large")
            raise audio_too_large()
        try:
            wav = validate_wav(data, max_seconds=self._settings.audio_max_seconds)
        except InvalidAudio:
            _count("invalid_audio")
            logger.info(
                "audio rejected",
                extra={
                    "session_id": str(session_id),
                    "user_id": str(user_id),
                    "size_bytes": len(data),
                },
            )
            raise AppError(422, "invalid_audio", INVALID_AUDIO_MESSAGE) from None
        audio = AudioInput(wav.data, SAMPLE_RATE, wav.duration_ms)
        try:
            result = await self._stt.transcribe(audio, "en", user_id=user_id)
        except AppError:
            raise
        except Exception as exc:
            _count("provider_error")
            logger.info(
                "transcription failed",
                extra={
                    "session_id": str(session_id),
                    "user_id": str(user_id),
                    "size_bytes": audio.size_bytes,
                    "duration_ms": audio.duration_ms,
                    "error_code": getattr(exc, "code", type(exc).__name__),
                },
            )
            raise
        text = clean_text(result.text)
        truncated = len(text) > MAX_QUERY_CHARS
        if truncated:
            text = text[:MAX_QUERY_CHARS].rstrip()
        detection = classify(text) if text else None
        latency_ms = int((self._clock() - started) * 1000)
        _count("ok" if text else "empty")
        logger.info(
            "audio transcribed",
            extra={
                "session_id": str(session_id),
                "user_id": str(user_id),
                "size_bytes": audio.size_bytes,
                "duration_ms": audio.duration_ms,
                "provider": result.provider,
                "model": result.model,
                "text_chars": len(text),
                "is_question": bool(detection and detection.is_question),
                "latency_ms": latency_ms,
            },
        )
        return TranscriptionOut(
            text=text,
            truncated=truncated,
            is_question=bool(detection and detection.is_question),
            confidence=detection.confidence if detection else 0.0,
            provider=result.provider,
            model=result.model,
            latency_ms=latency_ms,
        )
