"""Utterance transcription endpoint (Phase 12, D-53). Ownership comes only from `CurrentUser`.

POST /sessions/{id}/transcribe   raw `audio/wav` body -> {text, truncated, is_question, ...}

Order of checks: authentication (401) -> speech-to-text enabled (503) -> Content-Type (415) ->
declared size (413) -> per-user limiter (429) -> session ownership, readiness and its voice
provider (404/409) -> body read with a hard byte cap (413) -> WAV validation (422) -> provider.
Nothing is persisted; the body is read from the ASGI stream (no multipart, so nothing is spooled
to disk). If the client goes away while the provider is working, the call is cancelled and the
limiter slot is released.
"""

import asyncio
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.audio.limits import AudioLimits
from app.audio.schemas import TranscriptionOut
from app.audio.service import Transcriber, audio_too_large, stt_unavailable
from app.auth.deps import CurrentUser
from app.core.errors import AppError

router = APIRouter(prefix="/sessions", tags=["audio"])

DISCONNECT_POLL_SECONDS = 0.25


def get_transcriber(request: Request) -> Transcriber:
    transcriber: Transcriber = request.app.state.transcriber
    return transcriber


def get_limits(request: Request) -> AudioLimits:
    limits: AudioLimits = request.app.state.audio_limits
    return limits


TranscriberDep = Annotated[Transcriber, Depends(get_transcriber)]
LimitsDep = Annotated[AudioLimits, Depends(get_limits)]


def _require_wav_content_type(request: Request) -> None:
    media_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if media_type not in {"audio/wav", "audio/x-wav", "audio/wave"}:
        raise AppError(415, "unsupported_audio_type", "Only WAV audio is accepted.")


def _check_declared_size(request: Request, max_bytes: int) -> None:
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > max_bytes:
        raise audio_too_large()


async def _read_capped(request: Request, max_bytes: int) -> bytes:
    """Read the body into memory, refusing as soon as it exceeds the cap (declared or not)."""
    buffer = bytearray()
    async for chunk in request.stream():
        buffer.extend(chunk)
        if len(buffer) > max_bytes:
            raise audio_too_large()
    return bytes(buffer)


async def _await_or_cancel(
    request: Request, work: "asyncio.Task[TranscriptionOut]"
) -> TranscriptionOut:
    """Await `work`; if the client disconnects first, cancel it (the provider call is abandoned)."""
    while True:
        done, _ = await asyncio.wait({work}, timeout=DISCONNECT_POLL_SECONDS)
        if done:
            return work.result()  # re-raises the provider's or the service's safe error
        if await request.is_disconnected():
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
            raise AppError(499, "client_closed_request", "The request was cancelled.")


@router.post("/{session_id}/transcribe", response_model=TranscriptionOut)
async def transcribe_utterance(
    session_id: uuid.UUID,
    request: Request,
    current: CurrentUser,  # authentication first (401), then everything else
    transcriber: TranscriberDep,
    limits: LimitsDep,
) -> TranscriptionOut:
    if not transcriber.available:
        raise stt_unavailable()
    _require_wav_content_type(request)
    _check_declared_size(request, transcriber.max_bytes)
    slot = limits.admit(current.id)  # 429 before any work; released exactly once below
    try:
        voice = await transcriber.require_ready_session(current.id, session_id)
        data = await _read_capped(request, transcriber.max_bytes)
        work = asyncio.create_task(transcriber.transcribe(current.id, session_id, data, voice))
        try:
            return await _await_or_cancel(request, work)
        finally:
            if not work.done():
                work.cancel()
    finally:
        slot.release()
