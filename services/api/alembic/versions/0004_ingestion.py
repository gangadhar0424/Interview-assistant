"""ingestion_jobs and extracted_texts (file ingestion foundation)

Revision ID: 0004
Revises: 0003
Create Date: 2026-01-04
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _uuid_pk() -> sa.Column[Any]:
    return sa.Column(
        "id",
        postgresql.UUID(as_uuid=True),
        server_default=sa.text("gen_random_uuid()"),
        nullable=False,
    )


def _timestamp(name: str) -> sa.Column[Any]:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def upgrade() -> None:
    op.create_table(
        "ingestion_jobs",
        _uuid_pk(),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_type", sa.String(length=16), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("attempt", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'failed')",
            name="ck_ingestion_jobs_status",
        ),
        sa.CheckConstraint(
            "source_type IN ('resume', 'document')", name="ck_ingestion_jobs_source_type"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_ingestion_jobs_user_id_users", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ingestion_jobs"),
        sa.UniqueConstraint("source_type", "source_id", name="uq_ingestion_jobs_source"),
    )
    op.create_index("ix_ingestion_jobs_user_id", "ingestion_jobs", ["user_id"])
    op.create_index(
        "ix_ingestion_jobs_status_updated_at", "ingestion_jobs", ["status", "updated_at"]
    )

    op.create_table(
        "extracted_texts",
        _uuid_pk(),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_type", sa.String(length=16), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("char_count", sa.Integer(), nullable=False),
        sa.Column("extractor", sa.String(length=32), nullable=False),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        sa.CheckConstraint(
            "source_type IN ('resume', 'document')", name="ck_extracted_texts_source_type"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_extracted_texts_user_id_users", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_extracted_texts"),
        sa.UniqueConstraint("source_type", "source_id", name="uq_extracted_texts_source"),
    )
    op.create_index("ix_extracted_texts_user_id", "extracted_texts", ["user_id"])

    # Files uploaded before ingestion existed get a pending job; the recovery sweep dispatches them.
    for table, source_type in (("resumes", "resume"), ("documents", "document")):
        op.execute(
            f"INSERT INTO ingestion_jobs (user_id, source_type, source_id) "  # noqa: S608
            f"SELECT user_id, '{source_type}', id FROM {table}"
        )


def downgrade() -> None:
    op.drop_table("extracted_texts")
    op.drop_table("ingestion_jobs")
