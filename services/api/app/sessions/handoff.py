"""Web -> desktop handoff (D-55, sign-in changed by D-56): a short-lived, single-use, opaque code.

The signed-in web user asks for a code for one of their prepared sessions and the web app opens the
desktop app with it (`interviewassistant://start?h=<code>`). The desktop app redeems the code once,
WITHOUT a login of its own: the code proves the user (only their authenticated web session could
create it) and redemption mints the desktop's own login session (a separate refresh-token family,
client type `desktop`) plus the approved prepared-session state. Only the code travels in the link:
no session data, API URL or token.

Because the code is now the credential, redemption is public but hardened like login: every attempt
counts against a per-client limit before the code is examined, the code is 256-bit random, lives 60
seconds, is consumed by the first attempt (atomic GETDEL, whoever makes it), and is stored only as a
SHA-256 key. Every failure (malformed, unknown, expired, reused, a session that is gone or not
startable, a disabled account) is the same `404 handoff_invalid`. The code is never logged.
Handoff changes no session status and starts no capture.
"""

import asyncio
import base64
import hashlib
import hmac
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import service as auth_service
from app.auth.client_ip import client_key
from app.auth.deps import CurrentUser
from app.auth.router import set_refresh_cookie
from app.auth.schemas import TokenResponse, UserPublic
from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.db.session import get_session, set_tenant_context
from app.jobs_import.limiter import SlidingWindowLimiter
from app.realtime.tickets import TicketStore, TicketStoreUnavailable
from app.sessions import repository, service
from app.sessions.schemas import PreferencesOut, SessionSummary

router = APIRouter(prefix="/sessions", tags=["handoff"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]

# Only a prepared (`ready`) session is handed off: the desktop gates audio, screenshots and answers
# on `ready` too, so a different status would show READY for a session it then refuses to use.
STARTABLE_STATUSES = frozenset({"ready"})


class HandoffIssueRequest(BaseModel):
    """Optional body (SEC-08). `code_challenge` = base64url(SHA-256(verifier)) of a secret the
    DESKTOP generated and keeps; the code is then useless to anyone without the verifier."""

    model_config = ConfigDict(extra="forbid")

    code_challenge: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{43}$")] | None = None


def challenge_of(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


class HandoffIssueResponse(BaseModel):
    code: str
    expires_in: int


class HandoffRedeemRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Shape is checked again by the store (43 urlsafe characters); the cap only bounds the body.
    code: str = Field(min_length=1, max_length=128)
    # SEC-08: required when the code was issued with a challenge (RFC 7636 verifier rules).
    code_verifier: (
        Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._~-]{43,128}$")] | None
    ) = None


class HandoffRedeemResponse(TokenResponse):
    """The desktop's own login (same shape as `/auth/login`; the refresh token travels only in the
    HttpOnly cookie) plus the approved prepared-session state: no document text or storage keys."""

    session: SessionSummary
    preferences: PreferencesOut


def handoff_invalid() -> AppError:
    return AppError(404, "handoff_invalid", "This handoff is invalid or has expired.")


def _limit(limiter: SlidingWindowLimiter, key: str) -> None:
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
    settings: SettingsDep,
    body: HandoffIssueRequest | None = None,
) -> HandoffIssueResponse:
    # Order: authentication (401) -> rate limit (every attempt counts, so probing foreign session
    # ids is throttled) -> ownership (same 404 as an unknown id) -> state -> code.
    _limit(request.app.state.handoff_issue_limiter, str(current.id))
    row = await repository.get_for_user(db, current.id, session_id)
    if row is None:
        raise service.session_not_found()
    if row.status not in STARTABLE_STATUSES:
        raise AppError(409, "session_not_startable", "This session cannot be started.")
    challenge = body.code_challenge if body is not None else None
    if challenge is None and settings.handoff_require_challenge:
        raise AppError(
            422, "challenge_required", "This server requires the desktop app to bind the handoff."
        )
    store: TicketStore = request.app.state.handoff_store
    try:
        code = await store.issue(current.id, session_id, challenge=challenge)
    except TicketStoreUnavailable:
        raise AppError(
            503, "handoff_unavailable", "Starting the desktop app is temporarily unavailable."
        ) from None
    # D-66 (Phase 4): the interview is about to start, so make sure the first question is fast
    # (embedding model loaded, database pool warm). Fire and forget; a failure is ignored.
    tasks: set[asyncio.Task[None]] = request.app.state.background_tasks
    warm_up = asyncio.create_task(request.app.state.question_answerer.warm_up())
    tasks.add(warm_up)
    warm_up.add_done_callback(tasks.discard)
    response.headers["Cache-Control"] = "no-store"
    return HandoffIssueResponse(code=code, expires_in=store.ttl_seconds)


@router.post("/handoff/redeem", response_model=HandoffRedeemResponse)
async def redeem_handoff(
    body: HandoffRedeemRequest,
    request: Request,
    response: Response,
    db: SessionDep,
    settings: SettingsDep,
) -> HandoffRedeemResponse:
    # Public on purpose (D-56): the desktop has no login yet, so the code is the credential. Every
    # attempt, valid or not, counts against the client's budget before the code is looked at.
    limiter: SlidingWindowLimiter = request.app.state.handoff_redeem_limiter
    key = client_key(
        request.client.host if request.client else None,
        request.headers.get("x-forwarded-for"),
        trusted_proxy=settings.trusted_proxy_enabled,
        hops=settings.trusted_proxy_hops,
    )
    _limit(limiter, key or "unknown-client")
    store: TicketStore = request.app.state.handoff_store
    try:
        binding = await store.redeem(body.code)  # consumed here, whoever presents it
    except TicketStoreUnavailable:
        raise AppError(
            503, "handoff_unavailable", "Starting the desktop app is temporarily unavailable."
        ) from None
    if binding is None:
        raise handoff_invalid()  # unknown/expired/reused/malformed: indistinguishable
    # SEC-08: a bound code is worthless without the verifier only the desktop holds. The code was
    # consumed above, so a wrong or missing verifier also burns it (same response as every failure).
    if binding.challenge is not None:
        if body.code_verifier is None or not hmac.compare_digest(
            challenge_of(body.code_verifier), binding.challenge
        ):
            raise handoff_invalid()
    elif settings.handoff_require_challenge:
        raise handoff_invalid()  # an unbound (legacy) code is refused when binding is mandatory
    # Re-validated now: the account or session may have gone since the code was issued.
    await set_tenant_context(db, binding.user_id)
    row = await repository.get_for_user(db, binding.user_id, binding.session_id)
    if row is None or row.status not in STARTABLE_STATUSES:
        raise handoff_invalid()
    detail = await service.get_session(db, binding.user_id, binding.session_id)
    issued = await auth_service.issue_desktop_session(db, settings, binding.user_id)
    if issued is None:
        raise handoff_invalid()
    set_refresh_cookie(response, issued.refresh_token, settings)
    response.headers["Cache-Control"] = "no-store"
    return HandoffRedeemResponse(
        access_token=issued.access_token,
        expires_in=issued.expires_in,
        user=UserPublic.model_validate(issued.user),
        session=SessionSummary.model_validate(detail, from_attributes=True),
        preferences=detail.preferences,
    )
