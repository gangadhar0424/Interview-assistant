"""OPT-IN smoke test with the REAL multilingual embedding model (slow, needs the model files).

Skipped unless RUN_REAL_EMBEDDING_TESTS=1. Requires `uv sync --extra embeddings` and either network
access (first run downloads ~2.2 GB) or an existing cache (EMBEDDING_CACHE_DIR). Run with:

    RUN_REAL_EMBEDDING_TESTS=1 EMBEDDING_CACHE_DIR=<dir> uv run pytest tests/test_real_embeddings.py

It runs the full pipeline (upload -> extract -> chunk -> real embeddings -> pgvector) and checks
that retrieval finds the right passage for English, Hindi and Telugu queries, including
cross-language queries. This is a smoke check, not a quality benchmark (docs/DECISIONS.md D-30).
"""

import os
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.embeddings.fastembed_provider import FastEmbedProvider
from app.ingestion.chunking import ChunkConfig
from app.ingestion.service import process_job
from app.retrieval.service import search_chunks
from app.storage.local import LocalFileStorage
from tests.conftest import RunSql
from tests.helpers import register, run_async
from tests.test_files import upload
from tests.test_ingestion_jobs import Env, env, job_row  # noqa: F401  (fixture reused)

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_REAL_EMBEDDING_TESTS") != "1",
    reason="opt-in: set RUN_REAL_EMBEDDING_TESTS=1 (needs the real model; slow)",
)

# Each passage is sized (172-178 chars) so that, with CHUNK size 200 and no overlap, the chunker
# keeps it as exactly one chunk (a neighbouring sentence would not fit alongside it).
ENGLISH = (
    "I have eight years of experience building backend services in Python and I led a team. "
    "I also designed REST APIs and mentored junior developers. I enjoy code reviews and testing."
)
HINDI = (
    "मुझे खाना पकाने का शौक है। मैं सप्ताहांत में शाकाहारी व्यंजन बनाता हूँ। "
    "मुझे बगीचे में टमाटर और धनिया उगाना भी पसंद है। "
    "मेरे परिवार को मेरी बनाई दाल और सब्ज़ी बहुत पसंद है।"
)
TELUGU = (
    "నాకు ఖగోళ శాస్త్రం అంటే ఇష్టం. నేను టెలిస్కోప్‌తో ఆకాశాన్ని చూస్తాను. "
    "నాకు రాత్రిపూట నక్షత్రాలను గమనించడం కూడా ఇష్టం. "
    "నా స్నేహితులతో కలిసి చంద్రుడిని చూడటం నాకు నచ్చుతుంది."
)

# (query, expected passage): same-language and cross-language queries.
CASES = [
    ("backend Python experience", ENGLISH),
    ("शाकाहारी खाना बनाना", HINDI),
    ("టెలిస్కోప్ ఆకాశం", TELUGU),
    ("vegetarian cooking hobby", HINDI),  # English query -> Hindi passage
    ("stargazing with a telescope", TELUGU),  # English query -> Telugu passage
    ("पायथन बैकएंड अनुभव", ENGLISH),  # Hindi query -> English passage
]


def test_real_model_pipeline_retrieves_the_right_passage_in_three_languages(
    client: TestClient,
    run_sql: RunSql,
    env: Env,  # noqa: F811
    test_db_url: str,
    storage_root: Path,
) -> None:
    alice = register(client, "alice@example.com")
    body = f"{ENGLISH}\n\n{HINDI}\n\n{TELUGU}\n".encode()
    file_id = str(upload(client, "documents", alice, "mixed.txt", body, "text/plain").json()["id"])

    cache = os.environ.get("EMBEDDING_CACHE_DIR")
    provider = FastEmbedProvider(
        "intfloat/multilingual-e5-large", Path(cache) if cache else None, batch_size=8
    )
    env.work(job_row(run_sql, file_id)["id"])  # extract
    embed_id = job_row(run_sql, file_id, "embed")["id"]
    small = ChunkConfig(size=200, overlap=0)  # one paragraph per chunk for this tiny document
    storage = LocalFileStorage(storage_root)

    async def embed(sessions: Any) -> str:
        return await process_job(sessions, storage, embed_id, embedder=provider, chunk_config=small)

    assert run_async(test_db_url, embed) == "completed"
    stored = run_sql("SELECT embedding_model, vector_dims(embedding) AS d FROM chunk_embeddings")
    assert len(stored) == 3 and {(r["embedding_model"], r["d"]) for r in stored} == {
        ("intfloat/multilingual-e5-large", 1024)
    }

    uid: uuid.UUID = run_sql("SELECT id FROM users")[0]["id"]

    async def ask(sessions: Any) -> list[tuple[str, str, float]]:
        results = []
        async with sessions() as session:
            for query, _expected in CASES:
                hit = (await search_chunks(session, provider, uid, query, limit=1))[0]
                results.append((query, hit.text, hit.similarity))
        return results

    for (query, expected), (_q, got, similarity) in zip(
        CASES, run_async(test_db_url, ask), strict=True
    ):
        assert got == expected, f"{query!r} -> {got!r} (similarity {similarity:.3f})"


def test_real_vectors_respect_user_isolation(
    client: TestClient,
    run_sql: RunSql,
    env: Env,  # noqa: F811
    test_db_url: str,
    storage_root: Path,
) -> None:
    """Real e5 vectors: Alice's best-matching query must never surface Bob's chunk."""
    alice = register(client, "alice@example.com")
    bob = register(client, "bob@example.com")
    alice_file = str(
        upload(client, "documents", alice, "a.txt", ENGLISH.encode(), "text/plain").json()["id"]
    )
    bob_file = str(
        upload(client, "documents", bob, "b.txt", TELUGU.encode(), "text/plain").json()["id"]
    )

    cache = os.environ.get("EMBEDDING_CACHE_DIR")
    provider = FastEmbedProvider(
        "intfloat/multilingual-e5-large", Path(cache) if cache else None, batch_size=8
    )
    storage = LocalFileStorage(storage_root)
    small = ChunkConfig(size=200, overlap=0)
    for fid in (alice_file, bob_file):
        env.work(job_row(run_sql, fid)["id"])  # extract
        embed_id = job_row(run_sql, fid, "embed")["id"]

        async def embed(sessions: Any, jid: Any = embed_id) -> str:
            return await process_job(sessions, storage, jid, embedder=provider, chunk_config=small)

        assert run_async(test_db_url, embed) == "completed"

    alice_id: uuid.UUID = run_sql("SELECT id FROM users WHERE email = 'alice@example.com'")[0]["id"]
    bob_id: uuid.UUID = run_sql("SELECT id FROM users WHERE email = 'bob@example.com'")[0]["id"]

    async def ask(sessions: Any) -> tuple[list[Any], list[Any]]:
        async with sessions() as session:
            # Bob's Telugu passage is the best semantic match for this query, but it is Bob's.
            a = await search_chunks(session, provider, alice_id, "టెలిస్కోప్ ఆకాశం", limit=20)
            b = await search_chunks(session, provider, bob_id, "టెలిస్కోప్ ఆకాశం", limit=20)
        return a, b

    a_hits, b_hits = run_async(test_db_url, ask)
    assert a_hits and {str(h.source_id) for h in a_hits} == {alice_file}
    assert all(h.text != TELUGU for h in a_hits)
    assert b_hits and {str(h.source_id) for h in b_hits} == {bob_file}
