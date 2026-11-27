"""Shared seeding for the SEC-13 data-lifecycle tests (D-85).

`seed_user` fills every user-owned table for one account: resume and document (rows, stored files,
ingestion jobs) through the API, a session with a document link through the API, and extracted
text, chunks, embeddings, a question and an answer through SQL as the test database owner.
"""

import uuid
from dataclasses import dataclass
from pathlib import Path

from fastapi.testclient import TestClient

from tests.conftest import RunSql
from tests.helpers import register
from tests.test_sessions import body, create, new_file

# Every table that holds a user's rows, with how to count that user's rows in it.
USER_TABLES: dict[str, str] = {
    "users": "SELECT count(*) AS n FROM users WHERE id = :u",
    "refresh_tokens": "SELECT count(*) AS n FROM refresh_tokens WHERE user_id = :u",
    "resumes": "SELECT count(*) AS n FROM resumes WHERE user_id = :u",
    "documents": "SELECT count(*) AS n FROM documents WHERE user_id = :u",
    "ingestion_jobs": "SELECT count(*) AS n FROM ingestion_jobs WHERE user_id = :u",
    "extracted_texts": "SELECT count(*) AS n FROM extracted_texts WHERE user_id = :u",
    "chunks": "SELECT count(*) AS n FROM chunks WHERE user_id = :u",
    "chunk_embeddings": "SELECT count(*) AS n FROM chunk_embeddings WHERE user_id = :u",
    "sessions": "SELECT count(*) AS n FROM sessions WHERE user_id = :u",
    "session_documents": "SELECT count(*) AS n FROM session_documents WHERE user_id = :u",
    "session_preferences": (
        "SELECT count(*) AS n FROM session_preferences p JOIN sessions s ON s.id = p.session_id "
        "WHERE s.user_id = :u"
    ),
    "detected_questions": "SELECT count(*) AS n FROM detected_questions WHERE user_id = :u",
    "generated_answers": "SELECT count(*) AS n FROM generated_answers WHERE user_id = :u",
}


@dataclass
class Seeded:
    email: str
    headers: dict[str, str]
    user_id: uuid.UUID
    resume_id: uuid.UUID
    document_id: uuid.UUID
    session_id: uuid.UUID
    question_id: uuid.UUID


def counts(run_sql: RunSql, user_id: uuid.UUID) -> dict[str, int]:
    return {table: int(run_sql(sql, u=user_id)[0]["n"]) for table, sql in USER_TABLES.items()}


def user_files(storage_root: Path, user_id: uuid.UUID) -> list[Path]:
    directory = storage_root / "uploads" / str(user_id)
    return [p for p in directory.rglob("*") if p.is_file()] if directory.exists() else []


def seed_user(client: TestClient, run_sql: RunSql, email: str) -> Seeded:
    headers = register(client, email)
    user_id: uuid.UUID = run_sql("SELECT id FROM users WHERE email = :e", e=email)[0]["id"]
    resume = new_file(client, run_sql, headers, "resumes")
    document = new_file(client, run_sql, headers, "documents")
    response = create(client, headers, body(resume, [document]))
    assert response.status_code == 201, response.text
    session_id = uuid.UUID(str(response.json()["id"]))
    run_sql(
        "INSERT INTO extracted_texts (user_id, source_type, source_id, text, char_count, "
        "extractor, text_sha256) VALUES (:u, 'document', :d, 'synthetic', 9, 'test', :h)",
        u=user_id,
        d=uuid.UUID(document),
        h="0" * 64,
    )
    chunk_id = run_sql(
        "INSERT INTO chunks (user_id, source_type, source_id, chunk_index, text, char_start, "
        "char_end, content_hash) VALUES (:u, 'document', :d, 0, 'synthetic', 0, 9, :h) "
        "RETURNING id",
        u=user_id,
        d=uuid.UUID(document),
        h="1" * 64,
    )[0]["id"]
    run_sql(
        "INSERT INTO chunk_embeddings (chunk_id, embedding_model, user_id, embedding) VALUES "
        "(:c, 'test-model', :u, ('[' || array_to_string(array_fill(0.1::float8, "
        "ARRAY[1024]), ',') || ']')::vector)",
        c=chunk_id,
        u=user_id,
    )
    question_id = run_sql(
        "INSERT INTO detected_questions (session_id, user_id, raw_text) "
        "VALUES (:s, :u, 'synthetic question') RETURNING id",
        s=session_id,
        u=user_id,
    )[0]["id"]
    run_sql(
        "INSERT INTO generated_answers (question_id, session_id, user_id, generation_config, "
        "prompt_version) VALUES (:q, :s, :u, '{}'::jsonb, 'test')",
        q=question_id,
        s=session_id,
        u=user_id,
    )
    return Seeded(
        email,
        headers,
        user_id,
        uuid.UUID(resume),
        uuid.UUID(document),
        session_id,
        question_id,
    )
