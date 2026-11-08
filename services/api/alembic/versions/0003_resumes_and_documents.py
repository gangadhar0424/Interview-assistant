"""resumes and documents (user-owned uploaded file records)

Revision ID: 0003
Revises: 0002
Create Date: 2026-01-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _create_file_table(table: str) -> None:
    op.create_table(
        table,
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("original_filename", sa.Text(), nullable=False),
        sa.Column("content_type", sa.String(length=127), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("size_bytes > 0", name=f"ck_{table}_size_positive"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=f"fk_{table}_user_id_users", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=f"pk_{table}"),
        sa.UniqueConstraint("storage_key", name=f"uq_{table}_storage_key"),
    )
    op.create_index(
        f"ix_{table}_user_id_created_at", table, ["user_id", sa.text("created_at DESC")]
    )


def upgrade() -> None:
    _create_file_table("resumes")
    _create_file_table("documents")


def downgrade() -> None:
    op.drop_table("documents")
    op.drop_table("resumes")
