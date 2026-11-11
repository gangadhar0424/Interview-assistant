import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 128  # bounds Argon2 work per request


def _normalize_email(value: str) -> str:
    return value.strip().lower()


class SignupRequest(BaseModel):
    email: EmailStr
    password: Annotated[str, Field(min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH)]
    # Signup access code (Phase 13A Batch 2): optional here, required by policy outside local.
    invite_code: str | None = Field(default=None, max_length=128, repr=False)

    @field_validator("email", mode="after")
    @classmethod
    def _lower(cls, v: str) -> str:
        return _normalize_email(v)

    @field_validator("password")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Password must not be blank.")
        return v


class LoginRequest(BaseModel):
    email: EmailStr
    # No policy checks at login: only bound the size so it can't be used to burn CPU.
    password: Annotated[str, Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)]

    @field_validator("email", mode="after")
    @classmethod
    def _lower(cls, v: str) -> str:
        return _normalize_email(v)


class UserPublic(BaseModel):
    """Safe user representation. Never includes password material."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    created_at: datetime


class SignupResponse(BaseModel):
    user: UserPublic


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"  # noqa: S105
    expires_in: int
    user: UserPublic
