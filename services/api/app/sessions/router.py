"""Authenticated /sessions endpoints. Ownership is derived only from `CurrentUser`.

Not in Phase 7B (see docs/PHASES.md): job import (7C), `start`/`end` (later phase), questions and
answers (Phase 8).
"""

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.ai.registry import ProviderRegistry
from app.ai.router_api import get_registry, get_stt_router
from app.ai.stt_router import SttRouter
from app.auth.deps import CurrentUser
from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.db.session import get_session
from app.lifecycle.deps import DeletionRecorderDep
from app.sessions import export as session_export
from app.sessions import service
from app.sessions.idempotency import Claim, IdempotencyStore, valid_key
from app.sessions.schemas import SessionCreate, SessionDetail, SessionList, SessionUpdate

router = APIRouter(prefix="/sessions", tags=["sessions"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
RegistryDep = Annotated[ProviderRegistry, Depends(get_registry)]
VoiceDep = Annotated[SttRouter, Depends(get_stt_router)]


@router.post("", status_code=status.HTTP_201_CREATED, response_model=SessionDetail)
async def create_session(
    body: SessionCreate,
    request: Request,
    response: Response,
    current: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    registry: RegistryDep,
    voice: VoiceDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=200)] = None,
) -> SessionDetail:
    """With an `Idempotency-Key`, a retry of the same create returns the session it already made
    (`200`, never a second record). The key is scoped to the caller."""
    store: IdempotencyStore | None = getattr(request.app.state, "idempotency_store", None)
    if idempotency_key is None or store is None:
        return await service.create_session(session, current.id, settings, registry, voice, body)
    if not valid_key(idempotency_key):
        raise AppError(422, "validation_error", "Idempotency-Key is invalid.")
    claim, existing = await store.claim(current.id, idempotency_key)
    if claim is Claim.IN_PROGRESS:
        raise AppError(
            409,
            "request_in_progress",
            "This request is still being processed. Please wait a moment.",
            headers={"Retry-After": "2"},
        )
    if claim is Claim.DONE and existing is not None:
        try:
            detail = await service.get_session(
                session, current.id, existing, settings.qa_request_timeout_seconds
            )
        except AppError:
            # The session was deleted since: the retry creates a new one with a fresh claim.
            await store.release(current.id, idempotency_key)
            claim, existing = await store.claim(current.id, idempotency_key)
        else:
            response.status_code = status.HTTP_200_OK
            return detail
    try:
        created = await service.create_session(session, current.id, settings, registry, voice, body)
    except BaseException:
        if claim is Claim.NEW:
            await store.release(current.id, idempotency_key)
        raise
    if claim is Claim.NEW:
        await store.complete(current.id, idempotency_key, created.id)
    return created


@router.get("", response_model=SessionList)
async def list_sessions(
    current: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    status_filter: Annotated[str | None, Query(alias="status", max_length=16)] = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
    cursor: Annotated[str | None, Query(max_length=300)] = None,
) -> SessionList:
    return await service.list_sessions(
        session,
        current.id,
        limit=limit,
        status=status_filter,
        q=q,
        cursor=cursor,
        stale_after_seconds=settings.qa_request_timeout_seconds,
    )


@router.get("/{session_id}", response_model=SessionDetail)
async def get_session_detail(
    session_id: uuid.UUID, current: CurrentUser, session: SessionDep, settings: SettingsDep
) -> SessionDetail:
    return await service.get_session(
        session, current.id, session_id, settings.qa_request_timeout_seconds
    )


@router.patch("/{session_id}", response_model=SessionDetail)
async def update_session(
    session_id: uuid.UUID,
    body: SessionUpdate,
    current: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    registry: RegistryDep,
    voice: VoiceDep,
) -> SessionDetail:
    return await service.update_session(
        session, current.id, settings, registry, voice, session_id, body
    )


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(
    session_id: uuid.UUID,
    current: CurrentUser,
    session: SessionDep,
    recorder: DeletionRecorderDep,
) -> Response:
    await service.delete_session(session, current.id, session_id)
    # After the commit only: purge cached answers and record the deletion (SEC-13, D-85).
    await recorder.session_deleted(current.id, session_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{session_id}/end", response_model=SessionDetail)
async def end_session(
    session_id: uuid.UUID, current: CurrentUser, session: SessionDep, settings: SettingsDep
) -> SessionDetail:
    return await service.end_session(session, current.id, settings, session_id)


@router.post("/{session_id}/reopen", response_model=SessionDetail)
async def reopen_session(
    session_id: uuid.UUID, current: CurrentUser, session: SessionDep, settings: SettingsDep
) -> SessionDetail:
    return await service.reopen_session(session, current.id, settings, session_id)


@router.get("/{session_id}/export")
async def export_session(
    session_id: uuid.UUID,
    current: CurrentUser,
    session: SessionDep,
    export_format: Annotated[Literal["markdown", "pdf"], Query(alias="format")] = "markdown",
) -> Response:
    """A user-triggered download of one owned session (what it contains is declared in the file
    and in docs/SESSION_MANAGEMENT.md). Nothing is stored or sent anywhere by the server."""
    data = await session_export.build_export(session, current.id, session_id)
    content: str | bytes
    if export_format == "pdf":
        content = await run_in_threadpool(session_export.render_pdf, data)  # SEC-15: off the loop
        media, extension = "application/pdf", "pdf"
    else:
        content = session_export.render_markdown(data)
        media, extension = "text/markdown; charset=utf-8", "md"
    short = str(session_id)[:8]
    return Response(
        content=content,
        media_type=media,
        headers={
            # The file name carries no company, title or user data.
            "Content-Disposition": f'attachment; filename="session-{short}.{extension}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
