"""detected_questions and generated_answers (Phase 8A: typed Q&A persistence)

Revision ID: 0007
Revises: 0006
Create Date: 2026-01-07
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _uuid_pk() -> sa.Column[Any]:
    return sa.Column(
        "id",
        postgresql.UUID(as_uuid=True),
        server_default=sa.text("gen_random_uuid()"),
        nullable=False,
    )


def upgrade() -> None:
    op.create_table(
        "detected_questions",
        _uuid_pk(),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source", sa.String(length=16), server_default="typed", nullable=False),
        sa.Column("raw_text", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(length=16), server_default="explicit", nullable=False),
        sa.Column("question_type", sa.String(length=32), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="detected", nullable=False),
        sa.Column(
            "detected_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "source IN ('audio', 'typed', 'screenshot')", name="ck_detected_questions_source"
        ),
        sa.CheckConstraint(
            "kind IN ('explicit', 'implicit', 'followup')", name="ck_detected_questions_kind"
        ),
        sa.CheckConstraint(
            "status IN ('detected', 'answered', 'dismissed', 'ignored')",
            name="ck_detected_questions_status",
        ),
        sa.CheckConstraint(
            "question_type IS NULL OR question_type IN ('auto', 'behavioral', 'coding', "
            "'experience_check', 'how_do_you', 'situational', 'system_design', "
            "'technical_knowledge', 'tell_me_about_yourself')",
            name="ck_detected_questions_question_type",
        ),
        sa.CheckConstraint(
            "char_length(raw_text) >= 1", name="ck_detected_questions_raw_text_not_empty"
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            name="fk_detected_questions_session_id_sessions",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_detected_questions_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_detected_questions"),
    )
    op.create_index(
        "ix_detected_questions_session_id_detected_at",
        "detected_questions",
        ["session_id", "detected_at"],
    )

    op.create_table(
        "generated_answers",
        _uuid_pk(),
        sa.Column("question_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("text", sa.Text(), server_default="", nullable=False),
        sa.Column("generation_config", postgresql.JSONB(), nullable=False),
        sa.Column("prompt_version", sa.String(length=16), nullable=False),
        sa.Column("provider", sa.String(length=100), nullable=True),
        sa.Column("model", sa.String(length=100), nullable=True),
        sa.Column(
            "retrieved_chunk_ids",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            server_default=sa.text("'{}'::uuid[]"),
            nullable=False,
        ),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("retrieval_ms", sa.Integer(), nullable=True),
        sa.Column("first_token_ms", sa.Integer(), nullable=True),
        sa.Column("total_ms", sa.Integer(), nullable=True),
        sa.Column("fallback_used", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("status", sa.String(length=16), server_default="streaming", nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('streaming', 'complete', 'cancelled', 'error')",
            name="ck_generated_answers_status",
        ),
        sa.CheckConstraint(
            "(retrieval_ms IS NULL OR retrieval_ms >= 0) AND "
            "(first_token_ms IS NULL OR first_token_ms >= 0) AND "
            "(total_ms IS NULL OR total_ms >= 0) AND "
            "(input_tokens IS NULL OR input_tokens >= 0) AND "
            "(output_tokens IS NULL OR output_tokens >= 0)",
            name="ck_generated_answers_metrics_non_negative",
        ),
        sa.ForeignKeyConstraint(
            ["question_id"],
            ["detected_questions.id"],
            name="fk_generated_answers_question_id_detected_questions",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            name="fk_generated_answers_session_id_sessions",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_generated_answers_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_generated_answers"),
    )
    op.create_index("ix_generated_answers_question_id", "generated_answers", ["question_id"])
    op.create_index(
        "ix_generated_answers_session_id_created_at",
        "generated_answers",
        ["session_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("generated_answers")
    op.drop_table("detected_questions")
