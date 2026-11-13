"""Authenticated /sessions endpoints. Ownership is derived only from `CurrentUser`.

Not in Phase 7B (see docs/PHASES.md): job import (7C), `start`/`end` (later phase), questions and
answers (Phase 8).
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.registry import ProviderRegistry
from app.ai.router_api import get_registry, get_stt_router
from app.ai.stt_router import SttRouter
from app.auth.deps import CurrentUser
from app.core.config import Settings, get_settings
from app.db.session import get_session
from app.sessions import service
from app.sessions.schemas import SessionCreate, SessionDetail, SessionList, SessionUpdate

router = APIRouter(prefix="/sessions", tags=["sessions"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
RegistryDep = Annotated[ProviderRegistry, Depends(get_registry)]
VoiceDep = Annotated[SttRouter, Depends(get_stt_router)]


@router.post("", status_code=status.HTTP_201_CREATED, response_model=SessionDetail)
async def create_session(
    body: SessionCreate,
    current: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    registry: RegistryDep,
    voice: VoiceDep,
) -> SessionDetail:
    return await service.create_session(session, current.id, settings, registry, voice, body)


@router.get("", response_model=SessionList)
async def list_sessions(
    current: CurrentUser,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    status_filter: Annotated[str | None, Query(alias="status", max_length=16)] = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
    cursor: Annotated[str | None, Query(max_length=300)] = None,
) -> SessionList:
    return await service.list_sessions(
        session, current.id, limit=limit, status=status_filter, q=q, cursor=cursor
    )


@router.get("/{session_id}", response_model=SessionDetail)
async def get_session_detail(
    session_id: uuid.UUID, current: CurrentUser, session: SessionDep
) -> SessionDetail:
    return await service.get_session(session, current.id, session_id)


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
    session_id: uuid.UUID, current: CurrentUser, session: SessionDep
) -> Response:
    await service.delete_session(session, current.id, session_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
