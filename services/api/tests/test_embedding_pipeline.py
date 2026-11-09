"""Phase 5 integration tests (real PostgreSQL + pgvector, deterministic fake embeddings)."""

import asyncio
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.db.session import set_tenant_context
from app.embeddings.base import EmbeddingError, EmbeddingUnavailable
from app.embeddings.fake import FakeEmbeddingProvider
from app.ingestion.chunking import ChunkConfig, chunk_text
from app.ingestion.extractors import TextExtractor
from app.ingestion.service import (
    enqueue_missing_embeddings,
    process_job,
    recover_stale_jobs,
)
from app.retrieval.service import RetrievalError, search_chunks
from app.storage.local import LocalFileStorage
from tests.conftest import RunSql
from tests.helpers import (
    RecordingDispatcher,
    job_owner_id,
    real_pdf,
    run_async,
    run_maintenance_async,
)
from tests.test_files import upload
from tests.test_ingestion_jobs import (  # noqa: F401  (fixtures + helpers reused as-is)
    Env,
    alice,
    bob,
    dispatcher_of,
    env,
    job_row,
)

TOPICS = ["kubernetes", "pottery", "astronomy", "violin", "chemistry"]


def long_text() -> bytes:
    """Five paragraphs, each about one distinct topic and ~700 characters (one chunk each)."""
    paragraphs = []
    for topic in TOPICS:
        sentence = f"My {topic} work involved {topic} projects and {topic} research every year. "
        paragraphs.append((sentence * 9).strip())
    return "\n\n".join(paragraphs).encode()


N_CHUNKS = len(chunk_text(long_text().decode(), ChunkConfig()))  # overlap adds a chunk or so


def upload_txt(
    client: TestClient, headers: dict[str, str], data: bytes, kind: str = "documents"
) -> str:
    return str(upload(client, kind, headers, "notes.txt", data, "text/plain").json()["id"])


def rows(run_sql: RunSql, table: str) -> list[dict[str, Any]]:
    return run_sql(f"SELECT * FROM {table} ORDER BY 1")  # noqa: S608


def user_id(run_sql: RunSql, email: str) -> uuid.UUID:
    value: uuid.UUID = run_sql("SELECT id FROM users WHERE email = :e", e=email)[0]["id"]
    return value


def search(
    url: str,
    uid: uuid.UUID,
    query: str,
    embedder: FakeEmbeddingProvider | None = None,
    **kwargs: Any,
) -> list[Any]:
    provider = embedder or FakeEmbeddingProvider()

    async def go(sessions: Any) -> list[Any]:
        async with sessions() as session:
            await set_tenant_context(session, uid)
            return await search_chunks(session, provider, uid, query, **kwargs)

    return run_async(url, go)


# ---------------------------------------------------------------- schema


def test_migrated_schema_has_pgvector_column_and_constraints(run_sql: RunSql) -> None:
    ext = run_sql("SELECT extname FROM pg_extension WHERE extname = 'vector'")
    assert ext
    dims = run_sql(
        "SELECT format_type(atttypid, atttypmod) AS t FROM pg_attribute "
        "WHERE attrelid = 'chunk_embeddings'::regclass AND attname = 'embedding'"
    )
    assert dims[0]["t"] == "vector(1024)"
    indexes = {r["indexname"] for r in run_sql("SELECT indexname FROM pg_indexes")}
    assert {
        "ix_chunks_user_id_source",
        "ix_chunk_embeddings_user_id_model",
        "uq_chunks_source_index",
        "uq_ingestion_jobs_source",
    } <= indexes
    cascades = run_sql(
        "SELECT conname FROM pg_constraint WHERE contype = 'f' AND confdeltype = 'c' "
        "AND conrelid IN ('chunks'::regclass, 'chunk_embeddings'::regclass)"
    )
    assert {r["conname"] for r in cascades} >= {
        "fk_chunks_user_id_users",
        "fk_chunk_embeddings_chunk_id_chunks",
        "fk_chunk_embeddings_user_id_users",
    }


# ------------------------------------------------------- pipeline happy path


def test_extraction_queues_embedding_and_pipeline_stores_chunks_and_vectors(
    client: TestClient,
    alice: dict[str, str],  # noqa: F811
    run_sql: RunSql,
    env: Env,  # noqa: F811
) -> None:
    file_id = upload_txt(client, alice, long_text())
    dispatcher = RecordingDispatcher()
    extract_id = job_row(run_sql, file_id)["id"]
    assert env.work(extract_id, dispatcher=dispatcher) == "completed"

    embed = job_row(run_sql, file_id, "embed")  # created in the same transaction as the text
    assert embed["status"] == "pending" and dispatcher.dispatched == [embed["id"]]
    assert rows(run_sql, "chunks") == []  # nothing is chunked/embedded inside the extract stage
    indexing = client.get(f"/api/v1/documents/{file_id}", headers=alice).json()
    assert indexing["ingestion"]["status"] == "completed"
    assert indexing["indexing"]["status"] == "pending"

    assert env.work(embed["id"]) == "completed"
    stored_text = run_sql("SELECT text FROM extracted_texts")[0]["text"]
    chunks = run_sql("SELECT * FROM chunks ORDER BY chunk_index")
    expected = chunk_text(stored_text, ChunkConfig())
    assert [c["chunk_index"] for c in chunks] == list(range(len(expected))) and len(
        chunks
    ) == N_CHUNKS
    for stored, want in zip(chunks, expected, strict=True):
        assert stored["text"] == want.text == stored_text[stored["char_start"] : stored["char_end"]]
        assert stored["content_hash"] == want.content_hash
        assert stored["user_id"] == user_id(run_sql, "alice@example.com")
        assert stored["source_id"] == uuid.UUID(file_id) and stored["source_type"] == "document"
        assert stored["created_at"] is not None
    vectors = run_sql(
        "SELECT embedding_model, user_id, vector_dims(embedding) AS dims FROM chunk_embeddings"
    )
    assert len(vectors) == N_CHUNKS
    assert {(v["embedding_model"], v["dims"]) for v in vectors} == {("fake-hash-v1", 1024)}
    assert job_row(run_sql, file_id, "embed")["status"] == "completed"
    done = client.get(f"/api/v1/documents/{file_id}", headers=alice).json()
    assert done["indexing"]["status"] == "completed"
    assert "chunk" not in str(done).lower()  # chunk text/vectors are never exposed by the API


def test_no_public_search_endpoint_exists(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    assert not [
        p for p in paths if "search" in p or "retriev" in p or "chunk" in p or "vector" in p
    ]


# ------------------------------------------------------------- retrieval


def test_retrieval_returns_the_most_similar_chunks_best_first(
    client: TestClient,
    alice: dict[str, str],  # noqa: F811
    run_sql: RunSql,
    env: Env,  # noqa: F811
    test_db_url: str,
) -> None:
    file_id = upload_txt(client, alice, long_text())
    env.work_file(file_id)
    uid = user_id(run_sql, "alice@example.com")

    hits = search(test_db_url, uid, "astronomy research projects", limit=3)
    assert len(hits) == 3
    assert "astronomy" in hits[0].text and hits[0].source_id == uuid.UUID(file_id)
    assert hits[0].similarity > hits[1].similarity >= hits[2].similarity
    assert all(h.source_type == "document" for h in hits)
    assert {h.chunk_index for h in hits} <= set(range(N_CHUNKS))
    for topic in TOPICS:
        best = search(test_db_url, uid, f"{topic} work", limit=1)[0]
        assert topic in best.text  # each topic query finds its own paragraph


def test_retrieval_validates_its_inputs(
    client: TestClient,
    alice: dict[str, str],  # noqa: F811
    run_sql: RunSql,
    test_db_url: str,
) -> None:
    uid = user_id(run_sql, "alice@example.com")
    for bad in (
        {"query": "   "},
        {"query": "x" * 2001},
        {"query": "ok", "limit": 0},
        {"query": "ok", "limit": 21},
        {"query": "ok", "source_type": "image"},
    ):
        with pytest.raises(RetrievalError):
            search(test_db_url, uid, bad.pop("query"), **bad)  # type: ignore[arg-type]
    assert search(test_db_url, uid, "anything", source_ids=[]) == []  # explicit empty filter


def test_retrieval_source_filters_narrow_only_the_users_own_data(
    client: TestClient,
    alice: dict[str, str],  # noqa: F811
    run_sql: RunSql,
    env: Env,  # noqa: F811
    test_db_url: str,
) -> None:
    doc = upload_txt(client, alice, long_text())
    resume_id = str(
        upload(client, "resumes", alice, "cv.pdf", real_pdf("kubernetes engineer resume")).json()[
            "id"
        ]
    )
    env.work_file(doc)
    env.work_file(resume_id)
    uid = user_id(run_sql, "alice@example.com")

    only_doc = search(test_db_url, uid, "kubernetes", source_ids=[uuid.UUID(doc)])
    assert {h.source_id for h in only_doc} == {uuid.UUID(doc)}
    only_resume = search(test_db_url, uid, "kubernetes", source_type="resume")
    assert {h.source_id for h in only_resume} == {uuid.UUID(resume_id)}
    both = search(test_db_url, uid, "kubernetes", limit=20)
    assert {h.source_id for h in both} == {uuid.UUID(doc), uuid.UUID(resume_id)}


# ------------------------------------------------------------- isolation


def test_retrieval_never_returns_another_users_vectors(
    client: TestClient,
    alice: dict[str, str],  # noqa: F811
    bob: dict[str, str],  # noqa: F811
    run_sql: RunSql,
    env: Env,  # noqa: F811
    test_db_url: str,
) -> None:
    secret = b"bob private salary negotiation notes about confidential compensation figures"
    bob_file = upload_txt(client, bob, secret)
    alice_file = upload_txt(client, alice, b"alice gardening notes about tomatoes and compost")
    env.work_file(bob_file)
    env.work_file(alice_file)
    alice_id = user_id(run_sql, "alice@example.com")
    bob_id = user_id(run_sql, "bob@example.com")

    # Bob's text is a near-perfect match for this query, yet Alice must never see it.
    hits = search(test_db_url, alice_id, "bob private salary negotiation confidential", limit=20)
    assert {h.source_id for h in hits} == {uuid.UUID(alice_file)}
    assert all("salary" not in h.text for h in hits)
    # Pointing the filter at Bob's file id yields nothing for Alice (it can only narrow her data).
    assert search(test_db_url, alice_id, "salary", source_ids=[uuid.UUID(bob_file)]) == []
    # And Bob sees his own.
    own = search(test_db_url, bob_id, "salary negotiation", limit=5)
    assert own and {h.source_id for h in own} == {uuid.UUID(bob_file)}
    # A user with no data gets nothing, and an unknown user id gets nothing.
    assert search(test_db_url, uuid.uuid4(), "salary negotiation") == []


# ------------------------------------------------------ embedding versioning


def test_vectors_of_another_model_are_never_compared_or_mixed(
    client: TestClient,
    alice: dict[str, str],  # noqa: F811
    run_sql: RunSql,
    env: Env,  # noqa: F811
    test_db_url: str,
) -> None:
    file_id = upload_txt(client, alice, long_text())
    env.work_file(file_id)  # embedded with fake-hash-v1
    uid = user_id(run_sql, "alice@example.com")
    model_b = FakeEmbeddingProvider(model_id="fake-hash-v2")

    assert search(test_db_url, uid, "pottery work", model_b) == []  # no incomparable matches
    assert search(test_db_url, uid, "pottery work")  # model A still works

    # Switching models: queue the files that lack vectors for model B, then re-embed them.
    async def enqueue(sessions: Any) -> list[uuid.UUID]:
        async with sessions() as session:
            return await enqueue_missing_embeddings(session, model_b.model_id)

    job_ids = run_maintenance_async(test_db_url, enqueue)
    assert job_ids == [job_row(run_sql, file_id, "embed")["id"]]
    assert env.work(job_ids[0], embedder=model_b) == "completed"

    assert len(rows(run_sql, "chunks")) == N_CHUNKS  # replaced, not accumulated
    models = run_sql("SELECT DISTINCT embedding_model FROM chunk_embeddings")
    assert [m["embedding_model"] for m in models] == ["fake-hash-v2"]  # old vectors are gone
    assert search(test_db_url, uid, "pottery work") == []
    best = search(test_db_url, uid, "pottery work", model_b, limit=1)[0]
    assert "pottery" in best.text

    assert run_maintenance_async(test_db_url, enqueue) == []  # nothing left for model B


# ---------------------------------------------- idempotency and duplicates


def test_unchanged_file_is_not_embedded_again(
    client: TestClient,
    alice: dict[str, str],  # noqa: F811
    run_sql: RunSql,
    env: Env,  # noqa: F811
) -> None:
    file_id = upload_txt(client, alice, long_text())
    embedder = FakeEmbeddingProvider()
    env.work(job_row(run_sql, file_id)["id"])
    embed_id = job_row(run_sql, file_id, "embed")["id"]
    assert env.work(embed_id, embedder=embedder) == "completed"
    assert len(embedder.document_calls) == 1
    before = [r["id"] for r in run_sql("SELECT id FROM chunks ORDER BY chunk_index")]

    run_sql("UPDATE ingestion_jobs SET status = 'pending' WHERE stage = 'embed'")
    assert env.work(embed_id, embedder=embedder) == "completed"
    assert len(embedder.document_calls) == 1  # no second provider call
    after = [r["id"] for r in run_sql("SELECT id FROM chunks ORDER BY chunk_index")]
    assert after == before  # same rows, nothing rewritten


def test_duplicate_and_concurrent_embed_delivery_creates_no_duplicate_chunks(
    client: TestClient,
    alice: dict[str, str],  # noqa: F811
    run_sql: RunSql,
    env: Env,  # noqa: F811
    test_db_url: str,
    storage_root: Path,
) -> None:
    file_id = upload_txt(client, alice, long_text())
    env.work(job_row(run_sql, file_id)["id"])
    embed_id = job_row(run_sql, file_id, "embed")["id"]
    owner_id = job_owner_id(test_db_url, embed_id)
    storage = LocalFileStorage(storage_root)

    async def both(sessions: Any) -> list[str]:
        return list(
            await asyncio.gather(
                process_job(
                    sessions,
                    storage,
                    embed_id,
                    user_id=owner_id,
                    embedder=FakeEmbeddingProvider(),
                ),
                process_job(
                    sessions,
                    storage,
                    embed_id,
                    user_id=owner_id,
                    embedder=FakeEmbeddingProvider(),
                ),
            )
        )

    assert sorted(run_async(test_db_url, both)) == ["completed", "skipped"]
    assert env.work(embed_id) == "skipped"  # a late duplicate delivery is a no-op
    assert len(rows(run_sql, "chunks")) == N_CHUNKS == len(rows(run_sql, "chunk_embeddings"))
    assert job_row(run_sql, file_id, "embed")["attempt"] == 1


def test_reprocessing_with_changed_text_replaces_chunks_without_accumulating(
    client: TestClient,
    alice: dict[str, str],  # noqa: F811
    run_sql: RunSql,
    env: Env,  # noqa: F811
    test_db_url: str,
) -> None:
    file_id = upload_txt(client, alice, long_text())
    env.work_file(file_id)
    new_text = "Completely different resume text about gardening. " * 30
    import hashlib

    run_sql(
        "UPDATE extracted_texts SET text = :t, text_sha256 = :h",
        t=new_text,
        h=hashlib.sha256(new_text.encode()).hexdigest(),
    )
    run_sql("UPDATE ingestion_jobs SET status = 'pending' WHERE stage = 'embed'")
    assert env.work(job_row(run_sql, file_id, "embed")["id"]) == "completed"
    stored = run_sql("SELECT text FROM chunks")
    assert stored and all("gardening" in r["text"] for r in stored)  # no old chunks remain
    expected = len(chunk_text(new_text, ChunkConfig()))
    assert len(rows(run_sql, "chunks")) == expected == len(rows(run_sql, "chunk_embeddings"))
    uid = user_id(run_sql, "alice@example.com")
    assert search(test_db_url, uid, "kubernetes", limit=1)[0].text.count("gardening") >= 1


# --------------------------------------------------- deletion and lifecycle


def test_deleting_a_file_removes_chunks_and_vectors(
    client: TestClient,
    alice: dict[str, str],  # noqa: F811
    run_sql: RunSql,
    env: Env,  # noqa: F811
    test_db_url: str,
) -> None:
    keep = upload_txt(client, alice, b"keep this file about woodworking and furniture")
    gone = upload_txt(client, alice, long_text())
    env.work_file(keep)
    env.work_file(gone)
    assert len(rows(run_sql, "chunks")) == N_CHUNKS + 1

    assert client.delete(f"/api/v1/documents/{gone}", headers=alice).status_code == 204
    left = rows(run_sql, "chunks")
    assert len(left) == 1 and left[0]["source_id"] == uuid.UUID(keep)
    assert {r["chunk_id"] for r in rows(run_sql, "chunk_embeddings")} == {left[0]["id"]}
    for table in ("extracted_texts", "ingestion_jobs"):
        assert uuid.UUID(gone) not in {r["source_id"] for r in rows(run_sql, table)}
    uid = user_id(run_sql, "alice@example.com")
    assert {h.source_id for h in search(test_db_url, uid, "kubernetes", limit=20)} == {
        uuid.UUID(keep)
    }


def test_delete_during_embedding_leaves_no_orphans_and_is_not_resurrected(
    client: TestClient,
    alice: dict[str, str],  # noqa: F811
    run_sql: RunSql,
    env: Env,  # noqa: F811
    test_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    file_id = upload_txt(client, alice, long_text())
    env.work(job_row(run_sql, file_id)["id"])
    embed_id = job_row(run_sql, file_id, "embed")["id"]
    original = FakeEmbeddingProvider.embed_documents
    seen: dict[str, Any] = {}

    def delete_midway(self: FakeEmbeddingProvider, texts: Any) -> Any:
        seen["status"] = job_row(run_sql, file_id, "embed")["status"]
        seen["delete"] = client.delete(f"/api/v1/documents/{file_id}", headers=alice)
        return original(self, texts)

    monkeypatch.setattr(FakeEmbeddingProvider, "embed_documents", delete_midway)
    assert env.work(embed_id) == "discarded"
    monkeypatch.undo()

    assert seen["status"] == "processing" and seen["delete"].status_code == 204
    for table in ("chunks", "chunk_embeddings", "extracted_texts", "ingestion_jobs", "documents"):
        assert rows(run_sql, table) == [], table
    assert env.work(embed_id) == "skipped"
    dispatcher = RecordingDispatcher()

    async def sweep(sessions: Any) -> Any:
        return await recover_stale_jobs(
            sessions,
            dispatcher,
            max_attempts=3,
            processing_timeout_seconds=900,
            pending_redispatch_seconds=120,
        )

    report = run_maintenance_async(test_db_url, sweep)
    assert (report.dispatched, report.requeued) == ([], []) and rows(run_sql, "chunks") == []


def test_embedding_worker_holding_outdated_text_discards_its_result(
    client: TestClient,
    alice: dict[str, str],  # noqa: F811
    run_sql: RunSql,
    env: Env,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    file_id = upload_txt(client, alice, long_text())
    env.work(job_row(run_sql, file_id)["id"])
    embed_id = job_row(run_sql, file_id, "embed")["id"]
    original = FakeEmbeddingProvider.embed_documents

    def text_changes_midway(self: FakeEmbeddingProvider, texts: Any) -> Any:
        run_sql("UPDATE extracted_texts SET text_sha256 = repeat('0', 64)")  # re-extracted
        return original(self, texts)

    monkeypatch.setattr(FakeEmbeddingProvider, "embed_documents", text_changes_midway)
    assert env.work(embed_id) == "discarded"
    monkeypatch.undo()
    assert rows(run_sql, "chunks") == [] and rows(run_sql, "chunk_embeddings") == []
    assert job_row(run_sql, file_id, "embed")["status"] == "processing"  # left for the new worker


# ---------------------------------------------------------- failure handling


def test_transient_provider_failure_retries_automatically_then_succeeds(
    client: TestClient,
    alice: dict[str, str],  # noqa: F811
    run_sql: RunSql,
    env: Env,  # noqa: F811
    test_db_url: str,
) -> None:
    file_id = upload_txt(client, alice, long_text())
    env.work(job_row(run_sql, file_id)["id"])
    embed_id = job_row(run_sql, file_id, "embed")["id"]
    down = FakeEmbeddingProvider(error=EmbeddingUnavailable())

    assert env.work(embed_id, embedder=down) == "retry_later"
    job = job_row(run_sql, file_id, "embed")
    assert job["status"] == "pending" and job["error_code"] == "embedding_unavailable"
    assert job["attempt"] == 1 and rows(run_sql, "chunks") == []
    api = client.get(f"/api/v1/documents/{file_id}", headers=alice).json()
    assert api["ingestion"]["status"] == "completed"  # extraction is unaffected
    assert api["indexing"]["status"] == "pending"

    # The recovery sweep re-sends it after the pending age; the provider is back.
    run_sql(
        "UPDATE ingestion_jobs SET updated_at = now() - interval '10 minutes' WHERE stage='embed'"
    )
    dispatcher = RecordingDispatcher()

    async def sweep(sessions: Any) -> Any:
        return await recover_stale_jobs(
            sessions,
            dispatcher,
            max_attempts=3,
            processing_timeout_seconds=900,
            pending_redispatch_seconds=120,
        )

    assert run_maintenance_async(test_db_url, sweep).dispatched == [embed_id]
    assert env.work(embed_id) == "completed"
    assert job_row(run_sql, file_id, "embed")["attempt"] == 2
    assert len(rows(run_sql, "chunk_embeddings")) == N_CHUNKS


def test_transient_failures_become_failed_after_max_attempts_and_retry_skips_extraction(
    client: TestClient,
    alice: dict[str, str],  # noqa: F811
    run_sql: RunSql,
    env: Env,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    file_id = upload_txt(client, alice, long_text())
    env.work(job_row(run_sql, file_id)["id"])
    embed_id = job_row(run_sql, file_id, "embed")["id"]
    down = FakeEmbeddingProvider(error=EmbeddingUnavailable())
    outcomes = []
    for _ in range(3):
        outcomes.append(env.work(embed_id, embedder=down, max_attempts=3))
        run_sql("UPDATE ingestion_jobs SET status = 'pending' WHERE status = 'pending'")
    assert outcomes == ["retry_later", "retry_later", "failed"]
    job = job_row(run_sql, file_id, "embed")
    assert job["status"] == "failed" and job["error_code"] == "embedding_unavailable"
    assert "retried automatically" not in job["error_message"]

    api = client.get(f"/api/v1/documents/{file_id}", headers=alice).json()
    assert (api["ingestion"]["status"], api["indexing"]["status"]) == ("completed", "failed")
    extract_before = job_row(run_sql, file_id)
    text_before = run_sql("SELECT updated_at FROM extracted_texts")[0]["updated_at"]

    calls: list[int] = []
    real = TextExtractor.extract

    def counting(self: Any, data: bytes) -> str:
        calls.append(1)
        return str(real(self, data))

    monkeypatch.setattr("app.ingestion.extractors.TextExtractor.extract", counting)
    retry = client.post(f"/api/v1/documents/{file_id}/retry", headers=alice)
    assert retry.status_code == 202 and retry.json()["indexing"]["status"] == "pending"
    assert env.work(embed_id) == "completed"  # provider recovered (default fake embedder)
    assert calls == []  # extraction was NOT re-run
    assert job_row(run_sql, file_id)["updated_at"] == extract_before["updated_at"]
    assert run_sql("SELECT updated_at FROM extracted_texts")[0]["updated_at"] == text_before
    assert len(rows(run_sql, "chunks")) == N_CHUNKS
    assert client.post(f"/api/v1/documents/{file_id}/retry", headers=alice).status_code == 409


@pytest.mark.parametrize(
    ("embedder", "code"),
    [
        (
            FakeEmbeddingProvider(error=EmbeddingError("embedding_failed", "Embedding failed.")),
            "embedding_failed",
        ),
        (FakeEmbeddingProvider(dimension=8), "embedding_dimension_mismatch"),
    ],
)
def test_permanent_embedding_errors_fail_the_job_safely(
    client: TestClient,
    alice: dict[str, str],  # noqa: F811
    run_sql: RunSql,
    env: Env,  # noqa: F811
    embedder: FakeEmbeddingProvider,
    code: str,
) -> None:
    file_id = upload_txt(client, alice, long_text())
    env.work(job_row(run_sql, file_id)["id"])
    assert env.work(job_row(run_sql, file_id, "embed")["id"], embedder=embedder) == "failed"
    job = job_row(run_sql, file_id, "embed")
    assert job["status"] == "failed" and job["error_code"] == code
    assert rows(run_sql, "chunks") == [] and rows(run_sql, "chunk_embeddings") == []
    assert run_sql("SELECT count(*) AS n FROM extracted_texts")[0]["n"] == 1  # text is kept


def test_unexpected_embedding_exception_is_recorded_without_details(
    client: TestClient,
    alice: dict[str, str],  # noqa: F811
    run_sql: RunSql,
    env: Env,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    file_id = upload_txt(client, alice, long_text())
    env.work(job_row(run_sql, file_id)["id"])

    def explode(self: Any, texts: Any) -> Any:
        raise RuntimeError("SECRET-DETAIL token=abc123 at C:\\internal")

    monkeypatch.setattr(FakeEmbeddingProvider, "embed_documents", explode)
    assert env.work(job_row(run_sql, file_id, "embed")["id"]) == "failed"
    job = job_row(run_sql, file_id, "embed")
    assert job["error_code"] == "internal_error"
    assert "SECRET" not in f"{job['error_code']} {job['error_message']}"
    assert "SECRET" not in client.get(f"/api/v1/documents/{file_id}", headers=alice).text


def test_embed_job_without_extracted_text_fails_cleanly(
    client: TestClient,
    alice: dict[str, str],  # noqa: F811
    run_sql: RunSql,
    env: Env,  # noqa: F811
) -> None:
    file_id = upload_txt(client, alice, long_text())
    env.work(job_row(run_sql, file_id)["id"])
    run_sql("DELETE FROM extracted_texts")
    assert env.work(job_row(run_sql, file_id, "embed")["id"]) == "failed"
    assert job_row(run_sql, file_id, "embed")["error_code"] == "text_missing"


# ------------------------------------------------------ ownership of retry


def test_embedding_retry_is_ownership_protected(
    client: TestClient,
    alice: dict[str, str],  # noqa: F811
    bob: dict[str, str],  # noqa: F811
    run_sql: RunSql,
    env: Env,  # noqa: F811
) -> None:
    file_id = upload_txt(client, alice, long_text())
    env.work(job_row(run_sql, file_id)["id"])
    env.work(
        job_row(run_sql, file_id, "embed")["id"],
        embedder=FakeEmbeddingProvider(error=EmbeddingError("embedding_failed", "x")),
    )
    assert job_row(run_sql, file_id, "embed")["status"] == "failed"
    assert client.post(f"/api/v1/documents/{file_id}/retry", headers=bob).status_code == 404
    assert client.get(f"/api/v1/documents/{file_id}", headers=bob).status_code == 404
    assert job_row(run_sql, file_id, "embed")["status"] == "failed"  # untouched by Bob
