"""Hybrid (dense + lexical) retrieval with Reciprocal Rank Fusion (D-66).

One SQL statement runs two ranked branches over the user's chunks of the given sources and fuses
them by rank: the dense branch (pgvector cosine distance, exact scan as in D-31) and the lexical
branch (Postgres full-text search over heading + text, prefix-matched OR of the question's content
words). Fusing ranks instead of scores means no similarity threshold has to be guessed: the dense
scores of this embedding model are low and close together, which is what broke the D-64 floor.

Ownership is enforced in SQL exactly as in `search_chunks` (`user_id` on the vector row and the
chunk row, same embedding model only). Callers pass `source_ids` that come from the session record,
never from the client. There is deliberately no public endpoint for this.
"""

import asyncio
import hashlib
import re
import uuid
from collections.abc import Collection, Sequence

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.types import vector_literal
from app.embeddings.base import EmbeddingProvider
from app.retrieval.service import MAX_QUERY_CHARS, ChunkHit, RetrievalError
from app.retrieval.terms import content_terms, tsquery_or

_HYBRID_SQL = text(
    "WITH q AS (SELECT CAST(CAST(:q AS text) AS vector) AS v, "
    "                  to_tsquery('simple', :tsq) AS tq), "
    "dense AS ("
    "  SELECT c.id, e.embedding <=> q.v AS dist "
    "  FROM q, chunk_embeddings e JOIN chunks c ON c.id = e.chunk_id "
    "  WHERE e.user_id = :uid AND c.user_id = :uid AND e.embedding_model = :model "
    "    AND c.source_id = ANY(:sids) "
    "  ORDER BY e.embedding <=> q.v LIMIT :n), "
    "dense_r AS (SELECT id, dist, ROW_NUMBER() OVER (ORDER BY dist, id) AS r FROM dense), "
    "lex AS ("
    "  SELECT c.id, ts_rank_cd(c.tsv, q.tq) AS lr "
    "  FROM q, chunks c "
    "  WHERE :has_lex AND c.user_id = :uid AND c.source_id = ANY(:sids) AND c.tsv @@ q.tq "
    "  ORDER BY lr DESC, c.id LIMIT :n), "
    "lex_r AS (SELECT id, lr, ROW_NUMBER() OVER (ORDER BY lr DESC, id) AS r FROM lex), "
    "fused AS ("
    "  SELECT COALESCE(d.id, l.id) AS id, "
    "         COALESCE(1.0 / (:k + d.r), 0) + COALESCE(1.0 / (:k + l.r), 0) AS score, "
    "         1 - d.dist AS sim, l.lr AS lex_rank "
    "  FROM dense_r d FULL OUTER JOIN lex_r l ON l.id = d.id) "
    "SELECT c.id AS chunk_id, c.source_type, c.source_id, c.chunk_index, c.text, c.heading, "
    "       c.section_start, c.section_end, f.score, f.sim, f.lex_rank "
    "FROM fused f JOIN chunks c ON c.id = f.id "
    "ORDER BY f.score DESC, c.source_id, c.chunk_index LIMIT :n"
).bindparams(bindparam("sids", type_=ARRAY(UUID(as_uuid=True))))


async def search_hybrid(
    session: AsyncSession,
    embedder: EmbeddingProvider,
    user_id: uuid.UUID,
    query: str,
    *,
    source_ids: Sequence[uuid.UUID],
    candidates: int = 30,
    rrf_k: int = 60,
    query_vector: Sequence[float] | None = None,
) -> list[ChunkHit]:
    """Dense + lexical search over the user's chunks of `source_ids`, fused by RRF, best first.

    `query_vector` lets the caller embed the question once for several searches. An empty
    `source_ids` matches nothing; another user's source id simply yields no rows.
    """
    cleaned = query.strip()
    if not cleaned:
        raise RetrievalError("query must not be empty")
    if len(cleaned) > MAX_QUERY_CHARS:
        raise RetrievalError(f"query must be at most {MAX_QUERY_CHARS} characters")
    if not source_ids:
        return []
    vector = (
        list(query_vector)
        if query_vector is not None
        else await asyncio.to_thread(embedder.embed_query, cleaned)
    )
    if len(vector) != embedder.dimension:
        raise RetrievalError("embedding dimension mismatch")
    terms = content_terms(cleaned)
    rows = (
        await session.execute(
            _HYBRID_SQL,
            {
                "uid": user_id,
                "model": embedder.model_id,
                "q": vector_literal(vector),
                "tsq": tsquery_or(terms) or "x",
                "has_lex": bool(terms),
                "sids": list(source_ids),
                "n": candidates,
                "k": rrf_k,
            },
        )
    ).all()
    return [
        ChunkHit(
            chunk_id=r.chunk_id,
            source_type=r.source_type,
            source_id=r.source_id,
            chunk_index=r.chunk_index,
            text=r.text,
            similarity=float(r.sim) if r.sim is not None else 0.0,
            heading=r.heading,
            section_start=r.section_start,
            section_end=r.section_end,
            fused_score=float(r.score),
            lexical_rank=float(r.lex_rank) if r.lex_rank is not None else None,
        )
        for r in rows
    ]


_FINGERPRINT_STRIP = re.compile(r"\W+", re.UNICODE)


def text_fingerprint(value: str) -> str:
    """Case/punctuation/whitespace-insensitive fingerprint, to spot a passage that appears in two
    files (a resume summary repeated inside a document, for example)."""
    normalised = _FINGERPRINT_STRIP.sub("", value.casefold())
    return hashlib.sha1(normalised.encode("utf-8"), usedforsecurity=False).hexdigest()


def limit_per_source(
    hits: Sequence[ChunkHit], cap: int, exempt: Collection[uuid.UUID] = ()
) -> list[ChunkHit]:
    """Keep the best `cap` hits of each source (hits are already best first), so one large file
    cannot crowd out the other selected documents. Sources in `exempt` are not capped."""
    seen: dict[uuid.UUID, int] = {}
    kept: list[ChunkHit] = []
    for hit in hits:
        count = seen.get(hit.source_id, 0)
        if hit.source_id not in exempt and count >= cap:
            continue
        seen[hit.source_id] = count + 1
        kept.append(hit)
    return kept


def drop_duplicates(hits: Sequence[ChunkHit], already_seen: Collection[str] = ()) -> list[ChunkHit]:
    """Remove passages whose text repeats an earlier hit (or one in `already_seen`), best first."""
    fingerprints = set(already_seen)
    kept: list[ChunkHit] = []
    for hit in hits:
        fingerprint = text_fingerprint(hit.text)
        if fingerprint in fingerprints:
            continue
        fingerprints.add(fingerprint)
        kept.append(hit)
    return kept
