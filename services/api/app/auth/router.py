from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import service
from app.auth.deps import CurrentUser, require_trusted_origin
from app.auth.schemas import LoginRequest, SignupRequest, SignupResponse, TokenResponse, UserPublic
from app.core.config import Settings, get_settings
from app.core.errors import REFRESH_COOKIE_NAME, REFRESH_COOKIE_PATH
from app.db.models import User
from app.db.session import get_session

router = APIRouter(prefix="/auth", tags=["auth"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def _set_refresh_cookie(response: Response, token: str, settings: Settings) -> None:
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


@router.post("/signup", status_code=status.HTTP_201_CREATED, response_model=SignupResponse)
async def signup(body: SignupRequest, session: SessionDep) -> SignupResponse:
    user = await service.signup(session, body.email, body.password)
    return SignupResponse(user=UserPublic.model_validate(user))


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest, response: Response, session: SessionDep, settings: SettingsDep
) -> TokenResponse:
    issued = await service.login(session, settings, body.email, body.password)
    _set_refresh_cookie(response, issued.refresh_token, settings)
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
    _set_refresh_cookie(response, issued.refresh_token, settings)
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
