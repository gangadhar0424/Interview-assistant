"""sessions, session_documents, session_preferences (Phase 7B)

Revision ID: 0006
Revises: 0005
Create Date: 2026-01-06
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamp(name: str) -> sa.Column[Any]:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def upgrade() -> None:
    op.create_table(
        "sessions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("type", sa.String(length=16), server_default="interview", nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("company_name", sa.Text(), nullable=False),
        sa.Column("job_url", sa.Text(), nullable=True),
        sa.Column("job_description", sa.Text(), nullable=False),
        sa.Column("resume_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("resume_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column(
            "materials_snapshot",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=16), server_default="ready", nullable=False),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        sa.CheckConstraint("type IN ('interview')", name="ck_sessions_type"),
        sa.CheckConstraint(
            "status IN ('draft', 'ready', 'active', 'ended', 'archived')",
            name="ck_sessions_status",
        ),
        sa.CheckConstraint(
            "char_length(company_name) BETWEEN 1 AND 200", name="ck_sessions_company_name_length"
        ),
        sa.CheckConstraint(
            "char_length(job_description) >= 1", name="ck_sessions_job_description_not_empty"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_sessions_user_id_users", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["resume_id"], ["resumes.id"], name="fk_sessions_resume_id_resumes", ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_sessions"),
    )
    op.create_index(
        "ix_sessions_user_id_created_at_id",
        "sessions",
        ["user_id", sa.text("created_at DESC"), sa.text("id DESC")],
    )
    op.create_index("ix_sessions_user_id_status", "sessions", ["user_id", "status"])
    op.create_index("ix_sessions_resume_id", "sessions", ["resume_id"])

    op.create_table(
        "session_documents",
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            name="fk_session_documents_session_id_sessions",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name="fk_session_documents_document_id_documents",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_session_documents_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("session_id", "document_id", name="pk_session_documents"),
    )
    op.create_index("ix_session_documents_document_id", "session_documents", ["document_id"])

    op.create_table(
        "session_preferences",
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("language", sa.String(length=8), nullable=False),
        sa.Column("provider", sa.String(length=100), nullable=True),
        sa.Column("model", sa.String(length=100), nullable=True),
        sa.Column("question_type", sa.String(length=32), nullable=False),
        sa.Column("answer_format", sa.String(length=32), nullable=False),
        sa.Column("answer_length", sa.String(length=32), nullable=False),
        sa.Column("answer_tone", sa.String(length=32), nullable=False),
        sa.Column("extra_instructions", sa.Text(), server_default="", nullable=False),
        sa.Column("generation_config", postgresql.JSONB(), nullable=False),
        sa.Column("prompt_version", sa.String(length=16), nullable=False),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        sa.CheckConstraint("language IN ('en', 'hi', 'te')", name="ck_session_preferences_language"),
        sa.CheckConstraint(
            "question_type IN ('auto', 'behavioral', 'coding', 'experience_check', 'how_do_you', "
            "'situational', 'system_design', 'technical_knowledge', 'tell_me_about_yourself')",
            name="ck_session_preferences_question_type",
        ),
        sa.CheckConstraint(
            "answer_format IN ('full_script', 'script_bullets', 'bullets')",
            name="ck_session_preferences_answer_format",
        ),
        sa.CheckConstraint(
            "answer_length IN ('short', 'balanced', 'long')",
            name="ck_session_preferences_answer_length",
        ),
        sa.CheckConstraint(
            "answer_tone IN ('simple', 'formal')", name="ck_session_preferences_answer_tone"
        ),
        sa.CheckConstraint(
            "model IS NULL OR provider IS NOT NULL", name="ck_session_preferences_model_needs_provider"
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            name="fk_session_preferences_session_id_sessions",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("session_id", name="pk_session_preferences"),
    )


def downgrade() -> None:
    op.drop_table("session_preferences")
    op.drop_table("session_documents")
    op.drop_table("sessions")
