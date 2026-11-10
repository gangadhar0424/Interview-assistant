"""Screenshot extraction endpoint (Phase 11A, D-51). Ownership comes only from `CurrentUser`.

POST /sessions/{id}/screenshots   raw `image/png` body -> {extracted_text, truncated, ...}

Order of checks: authentication (401) -> vision enabled (503) -> Content-Type (415) -> declared
size (413) -> per-user limiter (429) -> session ownership/readiness (404/409) -> body read with a
hard byte cap (413) -> PNG validation (422) -> vision provider. Nothing is persisted; the body is
read from the ASGI stream (no multipart, so nothing is spooled to disk). If the client goes away
while the provider is working, the extraction is cancelled and the limiter slot is released.
"""

import asyncio
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.auth.deps import CurrentUser
from app.core.errors import AppError
from app.screenshots.limits import ScreenshotLimits
from app.screenshots.schemas import ScreenshotText
from app.screenshots.service import ScreenshotExtractor, too_large, vision_unavailable

router = APIRouter(prefix="/sessions", tags=["screenshots"])

DISCONNECT_POLL_SECONDS = 0.25


def get_extractor(request: Request) -> ScreenshotExtractor:
    extractor: ScreenshotExtractor = request.app.state.screenshot_extractor
    return extractor


def get_limits(request: Request) -> ScreenshotLimits:
    limits: ScreenshotLimits = request.app.state.screenshot_limits
    return limits


ExtractorDep = Annotated[ScreenshotExtractor, Depends(get_extractor)]
LimitsDep = Annotated[ScreenshotLimits, Depends(get_limits)]


def _require_png_content_type(request: Request) -> None:
    media_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if media_type != "image/png":
        raise AppError(415, "unsupported_image_type", "Only PNG screenshots are accepted.")


def _check_declared_size(request: Request, max_bytes: int) -> None:
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > max_bytes:
        raise too_large()


async def _read_capped(request: Request, max_bytes: int) -> bytes:
    """Read the body into memory, refusing as soon as it exceeds the cap (declared or not)."""
    buffer = bytearray()
    async for chunk in request.stream():
        buffer.extend(chunk)
        if len(buffer) > max_bytes:
            raise too_large()
    return bytes(buffer)


async def _await_or_cancel(
    request: Request, work: "asyncio.Task[ScreenshotText]"
) -> ScreenshotText:
    """Await `work`; if the client disconnects first, cancel it (the provider call is abandoned)."""
    while True:
        done, _ = await asyncio.wait({work}, timeout=DISCONNECT_POLL_SECONDS)
        if done:
            return work.result()  # re-raises the provider's or the service's safe error
        if await request.is_disconnected():
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)  # let the provider call unwind
            raise AppError(499, "client_closed_request", "The request was cancelled.")


@router.post("/{session_id}/screenshots", response_model=ScreenshotText)
async def extract_screenshot(
    session_id: uuid.UUID,
    request: Request,
    current: CurrentUser,  # authentication first (401), then everything else
    extractor: ExtractorDep,
    limits: LimitsDep,
) -> ScreenshotText:
    if not extractor.available:
        raise vision_unavailable()
    _require_png_content_type(request)
    _check_declared_size(request, extractor.max_bytes)
    slot = limits.admit(current.id)  # 429 before any work; released exactly once below
    try:
        await extractor.require_ready_session(current.id, session_id)
        data = await _read_capped(request, extractor.max_bytes)
        work = asyncio.create_task(extractor.extract(current.id, session_id, data))
        try:
            return await _await_or_cancel(request, work)
        finally:
            if not work.done():
                work.cancel()
    finally:
        slot.release()
