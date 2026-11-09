"""Enable tenant row-level security for application tables.

Revision ID: 0008
Revises: 0007
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RUNTIME_ROLE = "interview_app_runtime"
USER_TABLES = (
    "users",
    "refresh_tokens",
    "resumes",
    "documents",
    "ingestion_jobs",
    "extracted_texts",
    "chunks",
    "chunk_embeddings",
    "sessions",
    "session_documents",
    "session_preferences",
    "detected_questions",
    "generated_answers",
)


def upgrade() -> None:
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{RUNTIME_ROLE}') THEN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_roles
                    WHERE rolname = current_user AND (rolsuper OR rolcreaterole)
                ) THEN
                    RAISE EXCEPTION
                        'Provision {RUNTIME_ROLE} with a database administrator before migration';
                END IF;
                CREATE ROLE {RUNTIME_ROLE}
                    NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
            END IF;
        END
        $$;
        ALTER ROLE {RUNTIME_ROLE}
            NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
        GRANT {RUNTIME_ROLE} TO CURRENT_USER;
        GRANT USAGE ON SCHEMA public TO {RUNTIME_ROLE};
        GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {RUNTIME_ROLE};
        GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO {RUNTIME_ROLE};
        ALTER DEFAULT PRIVILEGES IN SCHEMA public
            GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {RUNTIME_ROLE};
        ALTER DEFAULT PRIVILEGES IN SCHEMA public
            GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO {RUNTIME_ROLE};
        """
    )

    for table in USER_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

    tenant = "NULLIF(current_setting('app.user_id', true), '')::uuid"
    for table in (
        "resumes",
        "documents",
        "ingestion_jobs",
        "extracted_texts",
        "chunks",
        "chunk_embeddings",
        "sessions",
        "session_documents",
        "detected_questions",
        "generated_answers",
    ):
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
            USING (user_id = {tenant})
            WITH CHECK (user_id = {tenant})
            """
        )

    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON users
        USING (
            id = {tenant}
            OR email = NULLIF(current_setting('app.login_email', true), '')
        )
        WITH CHECK (id = {tenant})
        """
    )
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON refresh_tokens
        USING (
            user_id = {tenant}
            OR token_hash = NULLIF(current_setting('app.refresh_token_hash', true), '')
        )
        WITH CHECK (user_id = {tenant})
        """
    )
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON session_preferences
        USING (
            EXISTS (
                SELECT 1 FROM sessions
                WHERE sessions.id = session_preferences.session_id
                  AND sessions.user_id = {tenant}
            )
        )
        WITH CHECK (
            EXISTS (
                SELECT 1 FROM sessions
                WHERE sessions.id = session_preferences.session_id
                  AND sessions.user_id = {tenant}
            )
        )
        """
    )


def downgrade() -> None:
    for table in USER_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    op.execute(
        f"""
        ALTER DEFAULT PRIVILEGES IN SCHEMA public
            REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM {RUNTIME_ROLE};
        ALTER DEFAULT PRIVILEGES IN SCHEMA public
            REVOKE USAGE, SELECT, UPDATE ON SEQUENCES FROM {RUNTIME_ROLE};
        REVOKE SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public
            FROM {RUNTIME_ROLE};
        REVOKE USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public
            FROM {RUNTIME_ROLE};
        REVOKE USAGE ON SCHEMA public FROM {RUNTIME_ROLE};
        REVOKE {RUNTIME_ROLE} FROM CURRENT_USER;
        DROP ROLE {RUNTIME_ROLE};
        """
    )
