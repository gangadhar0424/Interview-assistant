"""generated_answers.grounding_check: content-free citation-integrity result (Issue 2)

Additive and nullable: existing rows keep NULL (never checked) and nothing is rewritten. The column
holds tag numbers and counts only, never answer text, and it is not proof that an answer is
grounded. Row-level security and the runtime role's table-level grants already cover the table.

Revision ID: 0009
Revises: 0008
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "generated_answers",
        sa.Column("grounding_check", postgresql.JSONB(none_as_null=True), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_generated_answers_grounding_check_object"),  # op.f: already the final name
        "generated_answers",
        "grounding_check IS NULL OR jsonb_typeof(grounding_check) = 'object'",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_generated_answers_grounding_check_object"), "generated_answers", type_="check"
    )
    op.drop_column("generated_answers", "grounding_check")
