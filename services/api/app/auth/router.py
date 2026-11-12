from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import service
from app.auth.client_ip import client_key
from app.auth.deps import CurrentUser, require_trusted_origin
from app.auth.limits import AuthLimits, invite_allows
from app.auth.schemas import LoginRequest, SignupRequest, SignupResponse, TokenResponse, UserPublic
from app.core.config import Settings, get_settings
from app.core.errors import REFRESH_COOKIE_NAME, REFRESH_COOKIE_PATH, AppError
from app.db.models import User
from app.db.session import get_session

router = APIRouter(prefix="/auth", tags=["auth"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def set_refresh_cookie(response: Response, token: str, settings: Settings) -> None:
    response.set_cookie(
        REFRESH_COOKIE_NAME,
        token,
        max_age=settings.refresh_token_ttl_days * 86400,
        path=REFRESH_COOKIE_PATH,  # only sent to /api/v1/auth/*
        httponly=True,  # not readable by JavaScript
        secure=settings.cookie_secure,
        samesite="lax",
    )


def _token_response(issued: service.IssuedSession) -> TokenResponse:
    return TokenResponse(
        access_token=issued.access_token,
        expires_in=issued.expires_in,
        user=UserPublic.model_validate(issued.user),
    )


def _client(request: Request, settings: Settings) -> str | None:
    """Per-client limit key, or None when no trustworthy client address exists (see client_ip)."""
    return client_key(
        request.client.host if request.client else None,
        request.headers.get("x-forwarded-for"),
        trusted_proxy=settings.trusted_proxy_enabled,
        hops=settings.trusted_proxy_hops,
    )


@router.post("/signup", status_code=status.HTTP_201_CREATED, response_model=SignupResponse)
async def signup(
    body: SignupRequest, request: Request, session: SessionDep, settings: SettingsDep
) -> SignupResponse:
    limits: AuthLimits = request.app.state.auth_limits
    limits.check_signup(_client(request, settings))
    if not invite_allows(settings, body.invite_code):
        limits.record_signup_failure()
        service.burn_signup_work(body.password)  # same cost as a real signup: no timing oracle
        # Identical to the duplicate-email failure: a closed gate or a wrong code is not revealed.
        raise service.signup_failed_error()
    user = await service.signup(session, body.email, body.password)
    return SignupResponse(user=UserPublic.model_validate(user))


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
) -> TokenResponse:
    limits: AuthLimits = request.app.state.auth_limits
    client = _client(request, settings)
    limits.check_login(body.email, client)  # before any password hashing
    try:
        issued = await service.login(session, settings, body.email, body.password)
    except AppError as exc:
        if exc.code == "invalid_credentials":
            limits.record_login_failure(body.email, client)
        raise
    limits.record_login_success(body.email)
    set_refresh_cookie(response, issued.refresh_token, settings)
    response.headers["Cache-Control"] = "no-store"
    return _token_response(issued)


@router.post(
    "/refresh", response_model=TokenResponse, dependencies=[Depends(require_trusted_origin)]
)
async def refresh(
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
    refresh_token: Annotated[str | None, Cookie()] = None,
) -> TokenResponse:
    if not refresh_token:
        raise service.invalid_refresh_error()
    issued = await service.refresh(session, settings, refresh_token)
    set_refresh_cookie(response, issued.refresh_token, settings)
    response.headers["Cache-Control"] = "no-store"
    return _token_response(issued)


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_trusted_origin)],
)
async def logout(
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
    refresh_token: Annotated[str | None, Cookie()] = None,
) -> None:
    await service.logout(session, refresh_token)
    response.delete_cookie(
        REFRESH_COOKIE_NAME,
        path=REFRESH_COOKIE_PATH,
        secure=settings.cookie_secure,
        httponly=True,
        samesite="lax",
    )


@router.get("/me", response_model=UserPublic)
async def me(current: CurrentUser, session: SessionDep) -> UserPublic:
    user = (await session.execute(select(User).where(User.id == current.id))).scalar_one()
    return UserPublic.model_validate(user)
