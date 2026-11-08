"""`POST /api/v1/auth/ws-ticket` (Phase 9A, D-41): a single-use WebSocket ticket.

Order: authentication (401) -> per-user rate limit (every attempt counts, so probing foreign
session ids is throttled) -> ownership (the same `404 session_not_found` for an unknown session and
another user's) -> ticket. The user comes only from the Bearer token.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import CurrentUser
from app.core.errors import AppError
from app.db.session import get_session
from app.jobs_import.limiter import SlidingWindowLimiter
from app.realtime.tickets import TicketStore, TicketStoreUnavailable
from app.sessions import repository as sessions_repository
from app.sessions.service import session_not_found

router = APIRouter(prefix="/auth", tags=["realtime"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


class WsTicketRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: uuid.UUID


class WsTicketResponse(BaseModel):
    ticket: str
    expires_in: int


@router.post("/ws-ticket", response_model=WsTicketResponse)
async def issue_ws_ticket(
    body: WsTicketRequest,
    request: Request,
    response: Response,
    current: CurrentUser,
    db: SessionDep,
) -> WsTicketResponse:
    limiter: SlidingWindowLimiter = request.app.state.ws_ticket_limiter
    key = str(current.id)
    if not limiter.allow(key):
        raise AppError(
            429,
            "rate_limited",
            "Too many connection requests. Please wait a moment.",
            headers={"Retry-After": str(limiter.retry_after(key))},
        )
    if await sessions_repository.get_for_user(db, current.id, body.session_id) is None:
        raise session_not_found()

    store: TicketStore = request.app.state.ticket_store
    try:
        ticket = await store.issue(current.id, body.session_id)
    except TicketStoreUnavailable:
        raise AppError(
            503, "realtime_unavailable", "Live connections are temporarily unavailable."
        ) from None
    response.headers["Cache-Control"] = "no-store"
    return WsTicketResponse(ticket=ticket, expires_in=store.ttl_seconds)
