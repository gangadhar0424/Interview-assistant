"""Request/response models for /sessions.

Requests forbid unknown fields, so a client-supplied `user_id`, storage path or status is rejected
rather than silently ignored. Enum-typed fields reuse the Phase 7A preference enums (no copies).
Responses never contain extracted text, storage keys or hashes.
"""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.preferences.enums import AnswerFormat, AnswerLength, AnswerTone, Language, QuestionType


class PreferencesIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    language: Language
    question_type: QuestionType
    answer_format: AnswerFormat
    answer_length: AnswerLength
    answer_tone: AnswerTone
    # The real cap is EXTRA_INSTRUCTIONS_MAX_CHARS after cleaning (PreferenceCompiler); this
    # generous bound only keeps absurd payloads from being processed at all.
    extra_instructions: str | None = Field(default=None, max_length=20000)
    # Optional: omitted = server default chain. Validated against the server allowlist.
    provider: str | None = Field(default=None, max_length=100)
    model: str | None = Field(default=None, max_length=100)


class PreferencesPatch(BaseModel):
    """Partial preferences update. Explicit `null` clears extra_instructions/provider/model."""

    model_config = ConfigDict(extra="forbid")

    language: Language | None = None
    question_type: QuestionType | None = None
    answer_format: AnswerFormat | None = None
    answer_length: AnswerLength | None = None
    answer_tone: AnswerTone | None = None
    extra_instructions: str | None = Field(default=None, max_length=20000)
    provider: str | None = Field(default=None, max_length=100)
    model: str | None = Field(default=None, max_length=100)


class SessionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["interview"] = "interview"
    title: str | None = Field(default=None, max_length=200)
    company_name: str = Field(min_length=1, max_length=200)
    job_url: str | None = Field(default=None, max_length=2048)
    # The real cap is SESSION_JOB_DESCRIPTION_MAX_CHARS (configurable), checked in the service.
    job_description: str = Field(min_length=1, max_length=400000)
    resume_id: uuid.UUID
    document_ids: list[uuid.UUID] = Field(default_factory=list, max_length=1000)
    preferences: PreferencesIn


class SessionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=200)
    company_name: str | None = Field(default=None, min_length=1, max_length=200)
    job_url: str | None = Field(default=None, max_length=2048)
    job_description: str | None = Field(default=None, min_length=1, max_length=400000)
    resume_id: uuid.UUID | None = None
    document_ids: list[uuid.UUID] | None = Field(default=None, max_length=1000)
    preferences: PreferencesPatch | None = None


class ResumeRef(BaseModel):
    id: uuid.UUID | None  # None once the resume itself was deleted (label kept for history)
    filename: str


class DocumentRef(BaseModel):
    id: uuid.UUID
    filename: str
    available: bool  # False when the document was deleted after the session was created


class PreferencesOut(BaseModel):
    language: str
    provider: str | None
    model: str | None
    question_type: str
    answer_format: str
    answer_length: str
    answer_tone: str
    extra_instructions: str
    prompt_version: str


class SessionSummary(BaseModel):
    id: uuid.UUID
    type: str
    title: str | None
    company_name: str
    status: str
    created_at: datetime
    updated_at: datetime
    resume: ResumeRef
    document_count: int


class SessionDetail(SessionSummary):
    job_url: str | None
    job_description: str
    documents: list[DocumentRef]
    preferences: PreferencesOut


class SessionList(BaseModel):
    items: list[SessionSummary]
    next_cursor: str | None
