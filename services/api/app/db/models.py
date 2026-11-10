"""ORM models: auth, files, ingestion/vectors (Phases 2-5), sessions (7B), typed Q&A (8A)."""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    desc,
    func,
    text,
)
from sqlalchemy import text as sql_text  # `text` is also a column name in GeneratedAnswer
from sqlalchemy.dialects.postgresql import ARRAY, CITEXT, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import Vector
from app.embeddings.base import EMBEDDING_DIMENSION
from app.preferences.enums import AnswerFormat, AnswerLength, AnswerTone, Language, QuestionType

USER_STATUSES = ("active", "disabled")
CLIENT_TYPES = ("web", "desktop")


class User(Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint("status IN ('active', 'disabled')", name="status"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    # citext: case-insensitive uniqueness enforced by the database.
    email: Mapped[str] = mapped_column(CITEXT, unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="active")
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class RefreshToken(Base):
    """One row per issued refresh token. Only a SHA-256 digest of the token is stored."""

    __tablename__ = "refresh_tokens"
    __table_args__ = (
        CheckConstraint("client_type IN ('web', 'desktop')", name="client_type"),
        Index("ix_refresh_tokens_user_id", "user_id"),
        Index("ix_refresh_tokens_family_id", "family_id"),
        Index("ix_refresh_tokens_expires_at", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # All tokens descended from one login share a family; reuse of a rotated token revokes it all.
    family_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    client_type: Mapped[str] = mapped_column(String(16), nullable=False, server_default="web")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replaced_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UserFileMixin:
    """Columns shared by user-owned uploaded files. Every row belongs to exactly one user."""

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # Display metadata only (sanitised); never used as a filesystem path.
    original_filename: Mapped[str] = mapped_column(Text, nullable=False)
    # Server-detected canonical type, not the client-declared one.
    content_type: Mapped[str] = mapped_column(String(127), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    # Server-generated key relative to the storage root (see app/storage/local.py).
    storage_key: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Resume(UserFileMixin, Base):
    __tablename__ = "resumes"
    __table_args__ = (
        CheckConstraint("size_bytes > 0", name="size_positive"),
        Index("ix_resumes_user_id_created_at", "user_id", desc("created_at")),
    )


class Document(UserFileMixin, Base):
    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint("size_bytes > 0", name="size_positive"),
        Index("ix_documents_user_id_created_at", "user_id", desc("created_at")),
    )


INGESTION_STATUSES = ("pending", "processing", "completed", "failed")
SOURCE_TYPES = ("resume", "document")


class IngestionJob(Base):
    """Database-backed state of one file's ingestion; PostgreSQL is the source of truth.

    One row per source file. `source_id` is polymorphic (resumes or documents) and is cleaned up
    explicitly when the file is deleted. `error_code`/`error_message` are fixed, safe strings:
    never exception text, file content or paths.
    """

    __tablename__ = "ingestion_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'failed')", name="status"
        ),
        CheckConstraint("source_type IN ('resume', 'document')", name="source_type"),
        CheckConstraint("stage IN ('extract', 'embed')", name="stage"),
        UniqueConstraint("source_type", "source_id", "stage", name="uq_ingestion_jobs_source"),
        Index("ix_ingestion_jobs_user_id", "user_id"),
        Index("ix_ingestion_jobs_status_updated_at", "status", "updated_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    source_type: Mapped[str] = mapped_column(String(16), nullable=False)
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # Pipeline stage: `extract` (text extraction) then `embed` (chunk + embed + index).
    stage: Mapped[str] = mapped_column(String(16), nullable=False, server_default="extract")
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    # Set when a worker claims the job; a long-stale value identifies a crashed/stuck worker.
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ExtractedText(Base):
    """Normalised text extracted from a file (input for future chunking). Sensitive content."""

    __tablename__ = "extracted_texts"
    __table_args__ = (
        CheckConstraint("source_type IN ('resume', 'document')", name="source_type"),
        UniqueConstraint("source_type", "source_id", name="uq_extracted_texts_source"),
        Index("ix_extracted_texts_user_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    source_type: Mapped[str] = mapped_column(String(16), nullable=False)
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    char_count: Mapped[int] = mapped_column(Integer, nullable=False)
    # sha256 of `text`: lets the embedding stage detect that it chunked outdated text.
    text_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    extractor: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Chunk(Base):
    """A searchable slice of a file's extracted text (replaced as a set on re-index)."""

    __tablename__ = "chunks"
    __table_args__ = (
        CheckConstraint("source_type IN ('resume', 'document')", name="source_type"),
        CheckConstraint("chunk_index >= 0", name="chunk_index_nonneg"),
        UniqueConstraint("source_type", "source_id", "chunk_index", name="uq_chunks_source_index"),
        Index("ix_chunks_user_id_source", "user_id", "source_type", "source_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    source_type: Mapped[str] = mapped_column(String(16), nullable=False)
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)  # sensitive
    char_start: Mapped[int] = mapped_column(Integer, nullable=False)  # offsets in extracted text
    char_end: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ChunkEmbedding(Base):
    """Vector for one chunk under one embedding model. Deleted with its chunk (cascade)."""

    __tablename__ = "chunk_embeddings"
    __table_args__ = (Index("ix_chunk_embeddings_user_id_model", "user_id", "embedding_model"),)

    chunk_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chunks.id", ondelete="CASCADE"), primary_key=True
    )
    embedding_model: Mapped[str] = mapped_column(String(128), primary_key=True)
    # Denormalised owner so a vector search can be constrained to the user without a join.
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIMENSION), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


SESSION_TYPES = ("interview",)
# `draft`/`active`/`ended`/`archived` are documented lifecycle states reserved for later phases;
# Phase 7B only ever creates `ready` sessions (materials must be ready).
SESSION_STATUSES = ("draft", "ready", "active", "ended", "archived")


def _in_list(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


class CallSession(Base):
    """An interview session: job context + selected materials + compiled preferences.

    Owned by exactly one user (`user_id`); every query must be scoped by it. The underlying
    resume/documents are referenced, never owned: deleting a session never deletes them, and
    deleting a resume only clears `resume_id` (the label survives in `resume_snapshot`).
    """

    __tablename__ = "sessions"
    __table_args__ = (
        CheckConstraint(_in_list("type", SESSION_TYPES), name="type"),
        CheckConstraint(_in_list("status", SESSION_STATUSES), name="status"),
        CheckConstraint("char_length(company_name) BETWEEN 1 AND 200", name="company_name_length"),
        CheckConstraint("char_length(job_description) >= 1", name="job_description_not_empty"),
        Index("ix_sessions_user_id_created_at_id", "user_id", desc("created_at"), desc("id")),
        Index("ix_sessions_user_id_status", "user_id", "status"),
        Index("ix_sessions_resume_id", "resume_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[str] = mapped_column(String(16), nullable=False, server_default="interview")
    title: Mapped[str | None] = mapped_column(Text)
    company_name: Mapped[str] = mapped_column(Text, nullable=False)
    # Stored as typed by the user; Phase 7B never fetches it (job import is Phase 7C).
    job_url: Mapped[str | None] = mapped_column(Text)
    job_description: Mapped[str] = mapped_column(Text, nullable=False)
    resume_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("resumes.id", ondelete="SET NULL")
    )
    # Labels captured when the materials were selected, so history survives file deletion.
    resume_snapshot: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    materials_snapshot: Mapped[list[dict[str, object]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="ready")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class SessionDocument(Base):
    """Link between a session and a selected document (cascades away with either side)."""

    __tablename__ = "session_documents"
    __table_args__ = (Index("ix_session_documents_document_id", "document_id"),)

    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"), primary_key=True
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True
    )
    # Denormalised owner (same value as the session's). Set only from the authenticated user.
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )


class SessionPreferences(Base):
    """Compiled answer preferences of a session (values come from app.preferences enums)."""

    __tablename__ = "session_preferences"
    __table_args__ = (
        CheckConstraint(_in_list("language", tuple(v.value for v in Language)), name="language"),
        CheckConstraint(
            _in_list("question_type", tuple(v.value for v in QuestionType)), name="question_type"
        ),
        CheckConstraint(
            _in_list("answer_format", tuple(v.value for v in AnswerFormat)), name="answer_format"
        ),
        CheckConstraint(
            _in_list("answer_length", tuple(v.value for v in AnswerLength)), name="answer_length"
        ),
        CheckConstraint(
            _in_list("answer_tone", tuple(v.value for v in AnswerTone)), name="answer_tone"
        ),
        CheckConstraint("model IS NULL OR provider IS NOT NULL", name="model_needs_provider"),
    )

    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"), primary_key=True
    )
    language: Mapped[str] = mapped_column(String(8), nullable=False)
    # NULL provider/model = use the server default chain (resolved when answering, Phase 8).
    provider: Mapped[str | None] = mapped_column(String(100))
    model: Mapped[str | None] = mapped_column(String(100))
    question_type: Mapped[str] = mapped_column(String(32), nullable=False)
    answer_format: Mapped[str] = mapped_column(String(32), nullable=False)
    answer_length: Mapped[str] = mapped_column(String(32), nullable=False)
    answer_tone: Mapped[str] = mapped_column(String(32), nullable=False)
    extra_instructions: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    # Compiled snapshot (CompiledPreferences.to_dict) + the template version it was built for.
    generation_config: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


# Documented domains (docs/DATABASE.md). Phase 8A only creates typed, explicit questions.
QUESTION_SOURCES = ("audio", "typed", "screenshot")
QUESTION_KINDS = ("explicit", "implicit", "followup")
QUESTION_STATUSES = ("detected", "answered", "dismissed", "ignored")
ANSWER_STATUSES = ("streaming", "complete", "cancelled", "error")


class DetectedQuestion(Base):
    """A question asked in a session (typed in Phase 8; audio/screenshot sources come later).

    Owned by one user; every query must be scoped by `user_id`. Question text is sensitive
    (stored as plaintext for now, like extracted document text; D-38). Cascades away with the
    session.
    """

    __tablename__ = "detected_questions"
    __table_args__ = (
        CheckConstraint(_in_list("source", QUESTION_SOURCES), name="source"),
        CheckConstraint(_in_list("kind", QUESTION_KINDS), name="kind"),
        CheckConstraint(_in_list("status", QUESTION_STATUSES), name="status"),
        CheckConstraint(
            "question_type IS NULL OR "
            + _in_list("question_type", tuple(v.value for v in QuestionType)),
            name="question_type",
        ),
        CheckConstraint("char_length(raw_text) >= 1", name="raw_text_not_empty"),
        Index("ix_detected_questions_session_id_detected_at", "session_id", "detected_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    source: Mapped[str] = mapped_column(String(16), nullable=False, server_default="typed")
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, server_default="explicit")
    # The question type actually used (the per-question override, else the session's).
    question_type: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="detected")
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class GeneratedAnswer(Base):
    """One generated answer to a question, with the config snapshot and latency metrics.

    Created as `streaming` before generation starts and finished in a second short transaction;
    no database session stays open while tokens stream. Answer text is sensitive (plaintext, D-38).
    """

    __tablename__ = "generated_answers"
    __table_args__ = (
        CheckConstraint(_in_list("status", ANSWER_STATUSES), name="status"),
        CheckConstraint(
            "(retrieval_ms IS NULL OR retrieval_ms >= 0) AND "
            "(first_token_ms IS NULL OR first_token_ms >= 0) AND "
            "(total_ms IS NULL OR total_ms >= 0) AND "
            "(input_tokens IS NULL OR input_tokens >= 0) AND "
            "(output_tokens IS NULL OR output_tokens >= 0)",
            name="metrics_non_negative",
        ),
        CheckConstraint(
            "grounding_check IS NULL OR jsonb_typeof(grounding_check) = 'object'",
            name="grounding_check_object",
        ),
        Index("ix_generated_answers_question_id", "question_id"),
        Index("ix_generated_answers_session_id_created_at", "session_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    question_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("detected_questions.id", ondelete="CASCADE"), nullable=False
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    text: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    # Compiled preferences used for this answer (incl. format/length/tone) + the template version.
    generation_config: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(16), nullable=False)
    provider: Mapped[str | None] = mapped_column(String(100))
    model: Mapped[str | None] = mapped_column(String(100))
    # Ids of the chunks put in the prompt (no FK: chunks are replaced when a file is re-indexed).
    retrieved_chunk_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False, server_default=sql_text("'{}'::uuid[]")
    )
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    retrieval_ms: Mapped[int | None] = mapped_column(Integer)
    first_token_ms: Mapped[int | None] = mapped_column(Integer)
    total_ms: Mapped[int | None] = mapped_column(Integer)
    fallback_used: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="streaming")
    error_code: Mapped[str | None] = mapped_column(String(64))
    # Citation-integrity check of a completed answer (tag numbers and counts only, never text).
    # NULL for answers that were not checked (streaming, cancelled, error, or created before 0009).
    # It is not proof that the answer is grounded.
    grounding_check: Mapped[dict[str, object] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
