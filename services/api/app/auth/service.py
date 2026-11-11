"""Authentication use-cases. Never logs passwords, hashes or tokens; logs IDs only."""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import security
from app.core.config import Settings
from app.core.errors import AppError
from app.db.models import RefreshToken, User
from app.db.session import set_login_email, set_refresh_token_context, set_tenant_context

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IssuedSession:
    user: User
    access_token: str
    expires_in: int
    refresh_token: str  # raw value; only ever sent to the client in an HttpOnly cookie


def _invalid_credentials() -> AppError:
    return AppError(401, "invalid_credentials", "Invalid email or password.")


def invalid_refresh_error() -> AppError:
    return AppError(
        401,
        "invalid_refresh_token",
        "Your session is no longer valid. Please log in again.",
        clear_refresh_cookie=True,
    )


def signup_failed_error() -> AppError:
    return AppError(409, "signup_failed", "An account could not be created with these details.")


def burn_signup_work(password: str) -> None:
    """Spend the hashing time of a real signup, so refusals are not distinguishable by timing."""
    security.hash_password(password)


async def signup(session: AsyncSession, email: str, password: str) -> User:
    user = User(id=uuid.uuid4(), email=email, password_hash=security.hash_password(password))
    await set_tenant_context(session, user.id)
    session.add(user)
    try:
        await session.commit()
    except IntegrityError:
        # Unique violation on email (enforced by the database, so race-safe).
        await session.rollback()
        raise signup_failed_error() from None
    return user


def _new_refresh_row(
    user_id: uuid.UUID, family_id: uuid.UUID, raw_token: str, settings: Settings, now: datetime
) -> RefreshToken:
    return RefreshToken(
        id=uuid.uuid4(),
        user_id=user_id,
        family_id=family_id,
        token_hash=security.hash_refresh_token(raw_token),
        client_type="web",
        expires_at=now + timedelta(days=settings.refresh_token_ttl_days),
    )


async def login(
    session: AsyncSession, settings: Settings, email: str, password: str
) -> IssuedSession:
    await set_login_email(session, email)
    user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if user is None:
        security.burn_password_check(password)
        raise _invalid_credentials()
    if not security.verify_password(user.password_hash, password) or user.status != "active":
        raise _invalid_credentials()

    now = datetime.now(UTC)
    await set_tenant_context(session, user.id)
    if security.needs_rehash(user.password_hash):
        user.password_hash = security.hash_password(password)
    user.last_login_at = now

    raw = security.generate_refresh_token()
    session.add(_new_refresh_row(user.id, uuid.uuid4(), raw, settings, now))
    await session.commit()

    access, expires_in = security.create_access_token(user.id, settings)
    logger.info("login succeeded", extra={"user_id": str(user.id)})
    return IssuedSession(user, access, expires_in, raw)


async def _revoke_family(session: AsyncSession, family_id: uuid.UUID, now: datetime) -> None:
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=now)
    )


async def refresh(session: AsyncSession, settings: Settings, raw_token: str) -> IssuedSession:
    now = datetime.now(UTC)
    token_hash = security.hash_refresh_token(raw_token)
    await set_refresh_token_context(session, token_hash)
    row = (
        await session.execute(
            select(RefreshToken)
            .where(RefreshToken.token_hash == token_hash)
            .with_for_update()  # serialise concurrent use of the same token
        )
    ).scalar_one_or_none()
    if row is None:
        raise invalid_refresh_error()
    await set_tenant_context(session, row.user_id)

    if row.revoked_at is not None:
        # A rotated or logged-out token was presented again: assume theft, kill the whole family.
        await _revoke_family(session, row.family_id, now)
        await session.commit()
        logger.warning(
            "refresh token reuse detected; family revoked",
            extra={"user_id": str(row.user_id), "family_id": str(row.family_id)},
        )
        raise invalid_refresh_error()

    if row.expires_at <= now:
        row.revoked_at = now
        await session.commit()
        raise AppError(
            401,
            "session_expired",
            "Your session has expired. Please log in again.",
            clear_refresh_cookie=True,
        )

    user = await session.get(User, row.user_id)
    if user is None or user.status != "active":
        await _revoke_family(session, row.family_id, now)
        await session.commit()
        raise invalid_refresh_error()

    new_raw = security.generate_refresh_token()
    new_row = _new_refresh_row(user.id, row.family_id, new_raw, settings, now)
    session.add(new_row)
    row.revoked_at = now
    row.last_used_at = now
    row.replaced_by = new_row.id
    await session.commit()

    access, expires_in = security.create_access_token(user.id, settings)
    return IssuedSession(user, access, expires_in, new_raw)


async def logout(session: AsyncSession, raw_token: str | None) -> None:
    """Revoke the whole refresh-token family of the presented token. Idempotent."""
    if not raw_token:
        return
    token_hash = security.hash_refresh_token(raw_token)
    await set_refresh_token_context(session, token_hash)
    row = (
        await session.execute(select(RefreshToken).where(RefreshToken.token_hash == token_hash))
    ).scalar_one_or_none()
    if row is None:
        return
    await set_tenant_context(session, row.user_id)
    await _revoke_family(session, row.family_id, datetime.now(UTC))
    await session.commit()
    logger.info("logout", extra={"user_id": str(row.user_id)})
