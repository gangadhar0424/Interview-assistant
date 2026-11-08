"""Password hashing (Argon2id), access-token (JWT) and refresh-token primitives."""

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from app.core.config import Settings

JWT_ALGORITHM = "HS256"
JWT_ISSUER = "interview-assistant-api"

# argon2-cffi's default PasswordHasher is Argon2id with RFC 9106 low-memory parameters.
_hasher = PasswordHasher()
# Verified against when the email is unknown so response time doesn't reveal account existence.
_DUMMY_HASH = _hasher.hash("timing-equalisation-dummy-password")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


def burn_password_check(password: str) -> None:
    verify_password(_DUMMY_HASH, password)


class TokenError(Exception):
    """Access token missing claims, expired, or failing signature validation."""


def create_access_token(user_id: uuid.UUID, settings: Settings) -> tuple[str, int]:
    """Return (jwt, expires_in_seconds). Claims are the minimum: sub, iat, exp, iss."""
    now = datetime.now(UTC)
    lifetime = timedelta(minutes=settings.access_token_ttl_minutes)
    claims = {"sub": str(user_id), "iat": now, "exp": now + lifetime, "iss": JWT_ISSUER}
    token = jwt.encode(claims, settings.jwt_secret.get_secret_value(), algorithm=JWT_ALGORITHM)
    return token, int(lifetime.total_seconds())


def decode_access_token(token: str, settings: Settings) -> uuid.UUID:
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=[JWT_ALGORITHM],  # never trust the token's own `alg`
            issuer=JWT_ISSUER,
            options={"require": ["exp", "iat", "sub", "iss"]},
        )
        return uuid.UUID(claims["sub"])
    except (jwt.PyJWTError, ValueError) as exc:
        raise TokenError from exc


def generate_refresh_token() -> str:
    return secrets.token_urlsafe(48)


def hash_refresh_token(token: str) -> str:
    # Tokens are 384-bit random values, so an unsalted SHA-256 digest is sufficient.
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
