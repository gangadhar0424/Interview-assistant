# ruff: noqa: F811  (pytest fixtures are imported and then used as test arguments)
"""Hybrid retrieval and section-aware indexing against real PostgreSQL (D-66)."""

import uuid
from typing import Any

from fastapi.testclient import TestClient

from app.db.session import set_tenant_context
from app.embeddings.fake import FakeEmbeddingProvider
from app.ingestion.service import enqueue_missing_embeddings
from app.retrieval.hybrid import search_hybrid
from tests.conftest import RunSql
from tests.helpers import run_async, run_maintenance_async
from tests.test_embedding_pipeline import upload_txt, user_id
from tests.test_ingestion_jobs import (  # noqa: F401  (fixtures reused as-is)
    Env,
    alice,
    bob,
    env,
)

FILLER = "This section explains the topic in some detail so that it is long enough. " * 4


def handbook(extra: str = "") -> bytes:
    return "\n\n".join(
        [
            "1. What is the inbound process?",
            FILLER + "ERP creates the purchase order and the inbound delivery.",
            "2. How do you create bins in mass?",
            FILLER + "Use the mass creation transaction for storage bins.",
            "3. Explain replenishment",
            FILLER + "Replenishment moves stock from reserve to the pick bin." + extra,
        ]
    ).encode()


def hybrid(
    url: str, uid: uuid.UUID, query: str, sources: list[uuid.UUID], **kwargs: Any
) -> list[Any]:
    embedder = FakeEmbeddingProvider()

    async def go(sessions: Any) -> list[Any]:
        async with sessions() as session:
            await set_tenant_context(session, uid)
            return await search_hybrid(session, embedder, uid, query, source_ids=sources, **kwargs)

    return run_async(url, go)


def ingest(client: TestClient, env: Env, headers: dict[str, str], data: bytes) -> uuid.UUID:  # noqa: F811
    file_id = upload_txt(client, headers, data)
    env.work_file(file_id)
    return uuid.UUID(file_id)


def test_section_metadata_is_stored_with_every_chunk(
    client: TestClient,
    alice: dict[str, str],
    env: Env,
    run_sql: RunSql,  # noqa: F811
) -> None:
    doc = ingest(client, env, alice, handbook())
    rows = run_sql(
        "SELECT heading, section_start, section_end, char_start, char_end, text, "
        "tsv IS NOT NULL AS has_tsv "
        "FROM chunks WHERE source_id = :s ORDER BY chunk_index",
        s=doc,
    )
    assert rows and all(r["has_tsv"] for r in rows)
    assert {r["heading"] for r in rows} >= {
        "1. What is the inbound process?",
        "3. Explain replenishment",
    }
    for row in rows:
        assert row["section_start"] <= row["char_start"] < row["char_end"] <= row["section_end"]


def test_the_keyword_branch_finds_a_section_by_its_title(
    client: TestClient,
    alice: dict[str, str],
    env: Env,
    run_sql: RunSql,
    test_db_url: str,  # noqa: F811
) -> None:
    doc = ingest(client, env, alice, handbook())
    uid = user_id(run_sql, "alice@example.com")
    hits = hybrid(test_db_url, uid, "replenishment", [doc])
    assert hits and hits[0].heading == "3. Explain replenishment"
    assert hits[0].lexical_rank is not None and hits[0].fused_score > 0
    assert hits[0].section_start is not None


def test_results_are_best_first_with_fused_scores(
    client: TestClient,
    alice: dict[str, str],
    env: Env,
    run_sql: RunSql,
    test_db_url: str,  # noqa: F811
) -> None:
    doc = ingest(client, env, alice, handbook())
    uid = user_id(run_sql, "alice@example.com")
    hits = hybrid(test_db_url, uid, "create bins mass", [doc])
    scores = [h.fused_score for h in hits]
    assert scores == sorted(scores, reverse=True)
    assert hits[0].heading == "2. How do you create bins in mass?"


def test_hybrid_search_never_returns_another_users_or_an_unlisted_file(
    client: TestClient,
    alice: dict[str, str],  # noqa: F811
    bob: dict[str, str],  # noqa: F811
    env: Env,  # noqa: F811
    run_sql: RunSql,
    test_db_url: str,
) -> None:
    mine = ingest(client, env, alice, handbook())
    unselected = ingest(client, env, alice, b"unselected zebra secret notes about replenishment")
    bobs = ingest(client, env, bob, b"bob private replenishment salary notes zebra")
    alice_id, bob_id = user_id(run_sql, "alice@example.com"), user_id(run_sql, "bob@example.com")

    hits = hybrid(test_db_url, alice_id, "zebra replenishment secret salary", [mine])
    assert hits and {h.source_id for h in hits} == {mine}  # the listed file only
    assert all("zebra" not in h.text and "salary" not in h.text for h in hits)
    # Alice naming Bob's file id gets nothing: ownership is enforced inside the SQL.
    assert hybrid(test_db_url, alice_id, "replenishment salary", [bobs]) == []
    assert hybrid(test_db_url, alice_id, "zebra", [unselected, mine])  # her own, if she lists it
    own = hybrid(test_db_url, bob_id, "salary", [bobs])
    assert own and {h.source_id for h in own} == {bobs}
    assert hybrid(test_db_url, uuid.uuid4(), "replenishment", [mine]) == []
    assert hybrid(test_db_url, alice_id, "replenishment", []) == []


def test_ten_documents_can_be_searched_at_once(
    client: TestClient,
    alice: dict[str, str],
    env: Env,
    run_sql: RunSql,
    test_db_url: str,  # noqa: F811
) -> None:
    topics = [
        "alpha",
        "bravo",
        "charlie",
        "delta",
        "echo",
        "foxtrot",
        "golf",
        "hotel",
        "india",
        "juliet",
    ]
    ids = [
        ingest(
            client,
            env,
            alice,
            (
                f"1. About {topic}\n\n{FILLER}\n\n"
                f"2. More about {topic}\n\n{FILLER}{topic} specific words."
            ).encode(),
        )
        for topic in topics
    ]
    uid = user_id(run_sql, "alice@example.com")
    for topic, doc in zip(topics, ids, strict=True):
        hits = hybrid(test_db_url, uid, f"about {topic}", ids)
        assert hits[0].source_id == doc, topic  # each topic is found in its own document


def test_a_blank_or_oversized_query_is_refused(
    client: TestClient,
    alice: dict[str, str],
    env: Env,
    run_sql: RunSql,
    test_db_url: str,  # noqa: F811
) -> None:
    doc = ingest(client, env, alice, handbook())
    uid = user_id(run_sql, "alice@example.com")
    for bad in ("   ", "x" * 2001):
        try:
            hybrid(test_db_url, uid, bad, [doc])
        except Exception as exc:  # noqa: BLE001
            assert type(exc).__name__ == "RetrievalError"
        else:
            raise AssertionError("expected a RetrievalError")


def test_rechunk_requeues_every_file_but_the_default_only_those_without_vectors(
    client: TestClient,
    alice: dict[str, str],
    env: Env,
    test_db_url: str,  # noqa: F811
) -> None:
    ingest(client, env, alice, handbook())
    model = FakeEmbeddingProvider().model_id

    async def requeue(maker: Any, rechunk: bool) -> list[uuid.UUID]:
        async with maker() as session:
            return await enqueue_missing_embeddings(session, model, rechunk=rechunk)

    default = run_maintenance_async(test_db_url, lambda maker: requeue(maker, False))
    forced = run_maintenance_async(test_db_url, lambda maker: requeue(maker, True))
    assert default == []  # every file already has vectors for this model
    assert len(forced) == 1  # rechunk queues the file again; the embed stage skips unchanged ones
