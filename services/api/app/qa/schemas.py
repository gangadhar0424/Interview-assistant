"""Request/response models for the question endpoints (contract: docs/API.md, D-39).

Answers deliberately expose only user-facing fields: never the prompt, the stored generation
config, retrieved chunk ids or any provider internals.
"""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class QuestionCreate(BaseModel):
    # Only the question (and the optional per-question type): no user id, no session id, no
    # materials; ownership and sources come from the authenticated user and the session record.
    model_config = ConfigDict(extra="forbid")

    # The real limit (2000 characters after cleaning) is enforced by the orchestrator; this bound
    # only stops absurd payloads from being processed at all.
    text: str = Field(min_length=1, max_length=20000)
    question_type_override: str | None = Field(default=None, max_length=64)
    # Where the text came from (Phase 11A, D-51): only recorded on the question.
    input_source: Literal["typed", "screenshot"] = "typed"


class AnswerOut(BaseModel):
    id: uuid.UUID
    question_id: uuid.UUID
    status: str  # streaming | complete | cancelled | error
    text: str
    provider: str | None
    model: str | None
    retrieval_ms: int | None
    first_token_ms: int | None
    total_ms: int | None
    fallback_used: bool
    error_code: str | None
    input_tokens: int | None
    output_tokens: int | None
    created_at: datetime


class QuestionOut(BaseModel):
    id: uuid.UUID
    text: str
    source: str
    kind: str
    type: str | None  # the question type used (override or the session's)
    confidence: float | None  # always null for typed questions (detection comes later)
    status: str
    created_at: datetime
    answers: list[AnswerOut]


class QuestionList(BaseModel):
    items: list[QuestionOut]
    next_cursor: str | None
