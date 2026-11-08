"""chunks, chunk_embeddings (pgvector) and the `embed` ingestion stage

Revision ID: 0005
Revises: 0004
Create Date: 2026-01-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.db.types import Vector
from app.embeddings.base import EMBEDDING_DIMENSION

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ---- ingestion_jobs: one job per (source, stage) ----
    op.add_column(
        "ingestion_jobs",
        sa.Column("stage", sa.String(length=16), server_default="extract", nullable=False),
    )
    op.create_check_constraint(
        "ck_ingestion_jobs_stage", "ingestion_jobs", "stage IN ('extract', 'embed')"
    )
    op.drop_constraint("uq_ingestion_jobs_source", "ingestion_jobs", type_="unique")
    op.create_unique_constraint(
        "uq_ingestion_jobs_source", "ingestion_jobs", ["source_type", "source_id", "stage"]
    )

    # ---- extracted_texts: content fingerprint ----
    op.add_column("extracted_texts", sa.Column("text_sha256", sa.String(length=64), nullable=True))
    op.execute(
        "UPDATE extracted_texts SET text_sha256 = encode(sha256(convert_to(text, 'UTF8')), 'hex')"
    )
    op.alter_column("extracted_texts", "text_sha256", nullable=False)

    # ---- chunks ----
    op.create_table(
        "chunks",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_type", sa.String(length=16), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("char_start", sa.Integer(), nullable=False),
        sa.Column("char_end", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("source_type IN ('resume', 'document')", name="ck_chunks_source_type"),
        sa.CheckConstraint("chunk_index >= 0", name="ck_chunks_chunk_index_nonneg"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_chunks_user_id_users", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_chunks"),
        sa.UniqueConstraint("source_type", "source_id", "chunk_index", name="uq_chunks_source_index"),
    )
    op.create_index(
        "ix_chunks_user_id_source", "chunks", ["user_id", "source_type", "source_id"]
    )

    # ---- chunk_embeddings (pgvector) ----
    op.create_table(
        "chunk_embeddings",
        sa.Column("chunk_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("embedding_model", sa.String(length=128), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_DIMENSION), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["chunk_id"],
            ["chunks.id"],
            name="fk_chunk_embeddings_chunk_id_chunks",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_chunk_embeddings_user_id_users", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("chunk_id", "embedding_model", name="pk_chunk_embeddings"),
    )
    op.create_index(
        "ix_chunk_embeddings_user_id_model", "chunk_embeddings", ["user_id", "embedding_model"]
    )
    # No ANN (HNSW/IVFFlat) index yet: retrieval is always constrained to one user's chunks (a few
    # thousand rows at most), where an exact scan over the btree-filtered set is fast and has
    # perfect recall. An ANN index with a restrictive filter can silently drop results (D-31).

    # Files whose text was already extracted before Phase 5 get an `embed` job.
    op.execute(
        "INSERT INTO ingestion_jobs (user_id, source_type, source_id, stage) "
        "SELECT user_id, source_type, source_id, 'embed' FROM ingestion_jobs "
        "WHERE stage = 'extract' AND status = 'completed'"
    )


def downgrade() -> None:
    op.drop_table("chunk_embeddings")
    op.drop_table("chunks")
    op.drop_column("extracted_texts", "text_sha256")
    op.execute("DELETE FROM ingestion_jobs WHERE stage = 'embed'")
    op.drop_constraint("uq_ingestion_jobs_source", "ingestion_jobs", type_="unique")
    op.create_unique_constraint(
        "uq_ingestion_jobs_source", "ingestion_jobs", ["source_type", "source_id"]
    )
    op.drop_constraint("ck_ingestion_jobs_stage", "ingestion_jobs", type_="check")
    op.drop_column("ingestion_jobs", "stage")
