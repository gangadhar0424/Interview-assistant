-- Runs once on first initialization of the dev Postgres volume.
-- pgvector is also created by the baseline Alembic migration (idempotent).
CREATE EXTENSION IF NOT EXISTS vector;
