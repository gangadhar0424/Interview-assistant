"""Hybrid retrieval for the answer router (D-66).

Searches the session's resume and its selected documents in parallel, keeps the results per corpus
so the router can apply "resume first", and builds the prompt context for the synthesis path.
All queries are scoped by `user_id`; the source ids come from the session record, never from the
client.
"""

import asyncio
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.session import set_tenant_context
from app.embeddings.base import EmbeddingProvider
from app.qa.context import ContextConfig, apply_budget, merge_hits
from app.retrieval.hybrid import drop_duplicates, limit_per_source, search_hybrid, text_fingerprint
from app.retrieval.service import ChunkHit


@dataclass
class Ranked:
    """Hybrid search results, kept per corpus so the router can apply "resume first"."""

    resume: list[ChunkHit]
    documents: list[ChunkHit]
    opening: ChunkHit | None  # the resume's first chunk (for "tell me about yourself")
    names: dict[uuid.UUID, str] = field(default_factory=dict)  # source id -> file name (display)
    ms: int = 0

    @property
    def all_hits(self) -> list[ChunkHit]:
        return order_by_fused([*self.resume, *self.documents])


def _fused_key(hit: ChunkHit) -> tuple[float, str, int]:
    return (-hit.fused_score, str(hit.source_id), hit.chunk_index)


def order_by_fused(hits: Sequence[ChunkHit]) -> list[ChunkHit]:
    return sorted(hits, key=_fused_key)


_IDS = bindparam("ids", type_=ARRAY(UUID(as_uuid=True)))
_NAMES_SQL = {
    "resumes": text(
        "SELECT id, original_filename FROM resumes WHERE user_id = :uid AND id = ANY(:ids)"
    ).bindparams(_IDS),
    "documents": text(
        "SELECT id, original_filename FROM documents WHERE user_id = :uid AND id = ANY(:ids)"
    ).bindparams(_IDS),
}


async def _source_names(
    session: AsyncSession,
    user_id: uuid.UUID,
    resume_id: uuid.UUID,
    document_ids: Sequence[uuid.UUID],
) -> dict[uuid.UUID, str]:
    """File names for the source label (display only), restricted to this user's own files."""
    names: dict[uuid.UUID, str] = {}
    for table, ids in (("resumes", [resume_id]), ("documents", list(document_ids))):
        if not ids:
            continue
        rows = (await session.execute(_NAMES_SQL[table], {"uid": user_id, "ids": ids})).all()
        names.update({r.id: r.original_filename for r in rows})
    return names


async def _resume_opening(
    session: AsyncSession, user_id: uuid.UUID, resume_id: uuid.UUID
) -> ChunkHit | None:
    row = (
        await session.execute(
            text(
                "SELECT id, source_type, source_id, chunk_index, text, heading, "
                "       section_start, section_end "
                "FROM chunks WHERE user_id = :uid AND source_type = 'resume' "
                "  AND source_id = :sid ORDER BY chunk_index LIMIT 1"
            ),
            {"uid": user_id, "sid": resume_id},
        )
    ).first()
    if row is None:
        return None
    return ChunkHit(
        chunk_id=row.id,
        source_type=row.source_type,
        source_id=row.source_id,
        chunk_index=row.chunk_index,
        text=row.text,
        similarity=0.0,
        heading=row.heading,
        section_start=row.section_start,
        section_end=row.section_end,
    )


async def retrieve_ranked(
    sessionmaker: async_sessionmaker[AsyncSession],
    embedder: EmbeddingProvider,
    user_id: uuid.UUID,
    question: str,
    *,
    resume_id: uuid.UUID,
    document_ids: Sequence[uuid.UUID],
    config: ContextConfig,
    query_vector: Sequence[float] | None = None,
) -> Ranked:
    """Hybrid search of the resume and of the selected documents, in parallel.

    The question is embedded once; the two searches run concurrently, each in its own short
    session. Documents are capped per file and stripped of passages that repeat a resume passage.
    """
    started = time.monotonic()
    vector = (
        list(query_vector)
        if query_vector is not None
        else await asyncio.to_thread(embedder.embed_query, question)
    )

    async def search_resume() -> tuple[list[ChunkHit], ChunkHit | None, dict[uuid.UUID, str]]:
        async with sessionmaker() as db:
            await set_tenant_context(db, user_id)
            hits = await search_hybrid(
                db,
                embedder,
                user_id,
                question,
                source_ids=[resume_id],
                candidates=config.hybrid_candidates,
                rrf_k=config.rrf_k,
                query_vector=vector,
            )
            opening = await _resume_opening(db, user_id, resume_id)
            names = await _source_names(db, user_id, resume_id, document_ids)
        return hits, opening, names

    async def search_documents() -> list[ChunkHit]:
        if not document_ids:
            return []
        async with sessionmaker() as db:
            await set_tenant_context(db, user_id)
            return await search_hybrid(
                db,
                embedder,
                user_id,
                question,
                source_ids=list(document_ids),
                candidates=config.hybrid_candidates,
                rrf_k=config.rrf_k,
                query_vector=vector,
            )

    (resume_hits, opening, names), document_hits = await asyncio.gather(
        search_resume(), search_documents()
    )
    resume_hits = drop_duplicates(resume_hits)
    # Not capped per file here: the router judges every candidate. The per-file cap is applied to
    # the prompt context only (`context_from_ranked`).
    documents = drop_duplicates(document_hits, {text_fingerprint(h.text) for h in resume_hits})
    return Ranked(
        resume=resume_hits,
        documents=documents,
        opening=opening,
        names=names,
        ms=int((time.monotonic() - started) * 1000),
    )


def context_from_ranked(ranked: Ranked, config: ContextConfig) -> list[ChunkHit]:
    """Prompt context from hybrid results: the resume's best passages first (its quota), then the
    best of the rest, within the character budget, most relevant first."""
    resume_best = ranked.resume[: config.resume_quota] if config.resume_quota > 0 else []
    # Fair share per selected document, so one large file cannot take every slot (up to 10 files).
    document_ids = {hit.source_id for hit in ranked.documents}
    slots = max(1, config.top_k - len(resume_best))
    cap = max(config.per_document_cap, -(-slots // max(1, len(document_ids))))
    documents = limit_per_source(ranked.documents, cap)
    candidates = order_by_fused([*ranked.resume, *documents])
    merged = merge_hits(resume_best, candidates, config.top_k)
    return order_by_fused(apply_budget(merged, config.max_chars))
