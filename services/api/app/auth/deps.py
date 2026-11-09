"""Reusable auth dependencies.

`CurrentUser` is the single way route handlers learn who is calling. Future user-owned
repositories/services should accept an `AuthenticatedUser` (or its `id`) from here and never
a user ID taken from the request body, path or query.
"""

from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import security
from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.db.models import User
from app.db.session import get_session, set_tenant_context

_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class AuthenticatedUser:
    id: UUID
    email: str


def _unauthenticated() -> AppError:
    return AppError(
        401,
        "not_authenticated",
        "Authentication is required.",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AuthenticatedUser:
    if credentials is None:
        raise _unauthenticated()
    try:
        user_id = security.decode_access_token(credentials.credentials, settings)
    except security.TokenError:
        raise _unauthenticated() from None

    # Load the user so disabled/deleted accounts lose access immediately.
    await set_tenant_context(session, user_id)
    user = await session.get(User, user_id)
    if user is None or user.status != "active":
        raise _unauthenticated()
    return AuthenticatedUser(id=user.id, email=user.email)


CurrentUser = Annotated[AuthenticatedUser, Depends(get_current_user)]


def require_trusted_origin(
    request: Request, settings: Annotated[Settings, Depends(get_settings)]
) -> None:
    """CSRF defence-in-depth for cookie-authenticated endpoints (refresh, logout).

    Browsers always send `Origin` on cross-origin and POST requests; reject any that is not in the
    configured allow-list. Requests without `Origin` (non-browser clients) are not CSRF-able.
    """
    origin = request.headers.get("origin")
    if origin is not None and origin not in settings.cors_origin_list:
        raise AppError(403, "forbidden_origin", "Request origin is not allowed.")
