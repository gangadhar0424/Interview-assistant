"""Answer pipeline (D-66): section metadata and a full-text index on chunks, source on answers.

Additive and nullable, so the previous code keeps working on the new schema and nothing is
rewritten:

* ``chunks.heading`` / ``section_start`` / ``section_end``: the heading path of a section-aware
  chunk and the character range of the section it belongs to (offsets into the extracted text), so
  an exact-text answer can return a whole section. NULL for chunks made by the fixed-size chunker.
* ``chunks.tsv``: a generated full-text vector over heading + text (``simple`` configuration, no
  stemming, so every script is indexed alike) with a GIN index, for the lexical half of hybrid
  search. Generated columns are computed for existing rows when added.
* ``generated_answers.source_kind`` / ``source_refs``: where an answer came from (resume,
  document, synthesis or general) and, for sources, ids/headings only (never text).

Row-level security and the runtime role's grants already cover both tables (column-agnostic).

Revision ID: 0011
Revises: 0010
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("chunks", sa.Column("heading", sa.Text(), nullable=True))
    op.add_column("chunks", sa.Column("section_start", sa.Integer(), nullable=True))
    op.add_column("chunks", sa.Column("section_end", sa.Integer(), nullable=True))
    op.create_check_constraint(
        op.f("ck_chunks_section_range"),
        "chunks",
        "(section_start IS NULL AND section_end IS NULL) "
        "OR (section_start IS NOT NULL AND section_end IS NOT NULL "
        "AND section_start >= 0 AND section_end > section_start)",
    )
    op.execute(
        "ALTER TABLE chunks ADD COLUMN tsv tsvector GENERATED ALWAYS AS "
        "(to_tsvector('simple'::regconfig, coalesce(heading, '') || ' ' || text)) STORED"
    )
    op.create_index("ix_chunks_tsv", "chunks", ["tsv"], postgresql_using="gin")

    op.add_column("generated_answers", sa.Column("source_kind", sa.String(16), nullable=True))
    op.add_column(
        "generated_answers",
        sa.Column("source_refs", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_generated_answers_source_kind"),
        "generated_answers",
        "source_kind IS NULL OR source_kind IN "
        "('resume', 'document', 'synthesis', 'general')",
    )
    op.create_check_constraint(
        op.f("ck_generated_answers_source_refs_array"),
        "generated_answers",
        "source_refs IS NULL OR jsonb_typeof(source_refs) = 'array'",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_generated_answers_source_refs_array"), "generated_answers", type_="check"
    )
    op.drop_constraint(op.f("ck_generated_answers_source_kind"), "generated_answers", type_="check")
    op.drop_column("generated_answers", "source_refs")
    op.drop_column("generated_answers", "source_kind")
    op.drop_index("ix_chunks_tsv", table_name="chunks")
    op.drop_column("chunks", "tsv")
    op.drop_constraint(op.f("ck_chunks_section_range"), "chunks", type_="check")
    op.drop_column("chunks", "section_end")
    op.drop_column("chunks", "section_start")
    op.drop_column("chunks", "heading")
