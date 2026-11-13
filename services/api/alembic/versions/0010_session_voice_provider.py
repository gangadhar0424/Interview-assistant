"""session_preferences.stt_provider / stt_model: the session's voice-listening selection (D-57)

Additive and nullable: sessions created before D-57 keep NULL and nothing is rewritten. NULL means
"no voice provider chosen yet": transcription is refused for that session until the user selects
one (PATCH /sessions/{id}); there is deliberately no hidden default. The columns hold provider and
model identifiers only, never credentials. Row-level security and the runtime role's table-level
grants already cover the table.

Revision ID: 0010
Revises: 0009
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("session_preferences", sa.Column("stt_provider", sa.String(100), nullable=True))
    op.add_column("session_preferences", sa.Column("stt_model", sa.String(100), nullable=True))
    op.create_check_constraint(
        op.f("ck_session_preferences_stt_model_needs_provider"),  # op.f: already the final name
        "session_preferences",
        "stt_model IS NULL OR stt_provider IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_session_preferences_stt_model_needs_provider"),
        "session_preferences",
        type_="check",
    )
    op.drop_column("session_preferences", "stt_model")
    op.drop_column("session_preferences", "stt_provider")
