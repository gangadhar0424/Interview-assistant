"""Web -> desktop handoff (D-55): a short-lived, single-use, opaque code bound to user + session.

The web app asks for a code for one of the caller's prepared sessions and opens the desktop app
with it (`interviewassistant://start?h=<code>`). The desktop app, signed in as its own user,
redeems the code once. Only the code travels in the link: no session data, API URL or token.

Redemption needs the desktop's own Bearer token and the code's bound user must be that user, so
a leaked or hijacked link is useless on its own. Every failure (malformed, unknown, expired,
reused, another user's code, a session that is gone or not startable) is the same
`404 handoff_invalid`. The code is never logged and never stored in clear (SHA-256 key in Redis).
Handoff changes nothing: the session status is left as is and no capture is started.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import CurrentUser
from app.core.errors import AppError
from app.db.session import get_session
from app.jobs_import.limiter import SlidingWindowLimiter
from app.realtime.tickets import TicketStore, TicketStoreUnavailable
from app.sessions import repository, service
from app.sessions.schemas import PreferencesOut, SessionSummary

router = APIRouter(prefix="/sessions", tags=["handoff"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]

# Only a prepared (`ready`) session is handed off: the desktop gates audio, screenshots and answers
# on `ready` too, so a different status would show READY for a session it then refuses to use.
STARTABLE_STATUSES = frozenset({"ready"})


class HandoffIssueResponse(BaseModel):
    code: str
    expires_in: int


class HandoffRedeemRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Shape is checked again by the store (43 urlsafe characters); the cap only bounds the body.
    code: str = Field(min_length=1, max_length=128)


class HandoffRedeemResponse(BaseModel):
    """The approved prepared-session state: no document text, storage keys or credentials."""

    session: SessionSummary
    preferences: PreferencesOut


def handoff_invalid() -> AppError:
    return AppError(404, "handoff_invalid", "This handoff is invalid or has expired.")


def _limit(limiter: SlidingWindowLimiter, user_id: uuid.UUID) -> None:
    key = str(user_id)
    if not limiter.allow(key):
        raise AppError(
            429,
            "rate_limited",
            "Too many requests. Please wait a moment.",
            headers={"Retry-After": str(limiter.retry_after(key))},
        )


@router.post("/{session_id}/handoff", response_model=HandoffIssueResponse)
async def issue_handoff(
    session_id: uuid.UUID,
    request: Request,
    response: Response,
    current: CurrentUser,
    db: SessionDep,
) -> HandoffIssueResponse:
    # Order: authentication (401) -> rate limit (every attempt counts, so probing foreign session
    # ids is throttled) -> ownership (same 404 as an unknown id) -> state -> code.
    _limit(request.app.state.handoff_issue_limiter, current.id)
    row = await repository.get_for_user(db, current.id, session_id)
    if row is None:
        raise service.session_not_found()
    if row.status not in STARTABLE_STATUSES:
        raise AppError(409, "session_not_startable", "This session cannot be started.")
    store: TicketStore = request.app.state.handoff_store
    try:
        code = await store.issue(current.id, session_id)
    except TicketStoreUnavailable:
        raise AppError(
            503, "handoff_unavailable", "Starting the desktop app is temporarily unavailable."
        ) from None
    response.headers["Cache-Control"] = "no-store"
    return HandoffIssueResponse(code=code, expires_in=store.ttl_seconds)


@router.post("/handoff/redeem", response_model=HandoffRedeemResponse)
async def redeem_handoff(
    body: HandoffRedeemRequest,
    request: Request,
    response: Response,
    current: CurrentUser,
    db: SessionDep,
) -> HandoffRedeemResponse:
    _limit(request.app.state.handoff_redeem_limiter, current.id)
    store: TicketStore = request.app.state.handoff_store
    try:
        binding = await store.redeem(body.code)  # consumed here, whoever presents it
    except TicketStoreUnavailable:
        raise AppError(
            503, "handoff_unavailable", "Starting the desktop app is temporarily unavailable."
        ) from None
    if binding is None or binding.user_id != current.id:
        raise handoff_invalid()  # unknown/expired/reused/another user's: indistinguishable
    # Re-validated now: the session may have been deleted or ended since the code was issued.
    row = await repository.get_for_user(db, current.id, binding.session_id)
    if row is None or row.status not in STARTABLE_STATUSES:
        raise handoff_invalid()
    detail = await service.get_session(db, current.id, binding.session_id)
    response.headers["Cache-Control"] = "no-store"
    return HandoffRedeemResponse(
        session=SessionSummary.model_validate(detail, from_attributes=True),
        preferences=detail.preferences,
    )
