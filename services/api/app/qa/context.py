"""Context assembly: which chunks go into the prompt (docs/ARCHITECTURE.md section 5, D-38).

Rules (owner decisions): top-k 8 with a guaranteed resume quota of 3, a configurable character
budget, the raw question as the retrieval query. No query rewriting, MMR, reranking or hybrid
search. The pure helpers (`merge_hits`, `apply_budget`, `order_for_prompt`) are unit-tested; the
only database access is `retrieve_context`, which calls the user-scoped `search_chunks` with
source ids that come from the session record, never from the client.
"""

import dataclasses
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.embeddings.base import EmbeddingProvider
from app.retrieval.service import ChunkHit, search_chunks

MIN_PARTIAL_PASSAGE_CHARS = 200  # a passage is truncated to fit only if at least this much fits


@dataclass(frozen=True, slots=True)
class ContextConfig:
    top_k: int = 8
    resume_quota: int = 3
    max_chars: int = 6000
    history_pairs: int = 3
    history_item_max_chars: int = 1000
    retrieval_max_limit: int = 20

    @classmethod
    def from_settings(cls, settings: Settings) -> "ContextConfig":
        return cls(
            top_k=settings.qa_retrieval_top_k,
            resume_quota=settings.qa_resume_quota,
            max_chars=settings.qa_context_max_chars,
            history_pairs=settings.qa_history_pairs,
            history_item_max_chars=settings.qa_history_item_max_chars,
            retrieval_max_limit=settings.retrieval_max_limit,
        )


class _CachedQueryEmbedder:
    """Embeds the (identical) retrieval query once even though two searches run."""

    def __init__(self, inner: EmbeddingProvider) -> None:
        self._inner = inner
        self.model_id = inner.model_id
        self.dimension = inner.dimension
        self._last: tuple[str, list[float]] | None = None

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return self._inner.embed_documents(texts)

    def embed_query(self, text: str) -> list[float]:
        if self._last is None or self._last[0] != text:
            self._last = (text, self._inner.embed_query(text))
        return list(self._last[1])


def _rank_key(hit: ChunkHit) -> tuple[float, str, int]:
    return (-hit.similarity, str(hit.source_id), hit.chunk_index)


def merge_hits(
    resume_hits: Sequence[ChunkHit], all_hits: Sequence[ChunkHit], top_k: int
) -> list[ChunkHit]:
    """Priority order: the resume quota first, then the best remaining hits, at most `top_k`."""
    chosen: list[ChunkHit] = []
    seen: set[uuid.UUID] = set()
    for hit in [*resume_hits, *all_hits]:
        if hit.chunk_id in seen:
            continue
        if len(chosen) >= top_k:
            break
        seen.add(hit.chunk_id)
        chosen.append(hit)
    return chosen


def apply_budget(hits: Sequence[ChunkHit], max_chars: int) -> list[ChunkHit]:
    """Keep hits in priority order while they fit the character budget (never exceeds it).

    The first hit that does not fit is truncated if a useful amount of room remains, and
    selection stops there; lower-priority hits are dropped.
    """
    kept: list[ChunkHit] = []
    used = 0
    for hit in hits:
        remaining = max_chars - used
        if remaining <= 0:
            break
        if len(hit.text) <= remaining:
            kept.append(hit)
            used += len(hit.text)
            continue
        if remaining >= MIN_PARTIAL_PASSAGE_CHARS:
            kept.append(dataclasses.replace(hit, text=hit.text[:remaining].rstrip()))
        break
    return kept


def order_for_prompt(hits: Sequence[ChunkHit]) -> list[ChunkHit]:
    """Most similar first (ties broken deterministically)."""
    return sorted(hits, key=_rank_key)


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rstrip()


async def retrieve_context(
    session: AsyncSession,
    embedder: EmbeddingProvider,
    user_id: uuid.UUID,
    question: str,
    *,
    resume_id: uuid.UUID,
    document_ids: Sequence[uuid.UUID],
    config: ContextConfig,
) -> list[ChunkHit]:
    """Passages for one question, strictly from the session's selected sources of this user."""
    cached = _CachedQueryEmbedder(embedder)
    source_ids = [resume_id, *document_ids]  # from the session record, never from the client
    resume_hits: list[ChunkHit] = []
    if config.resume_quota > 0:
        resume_hits = await search_chunks(
            session,
            cached,
            user_id,
            question,
            limit=config.resume_quota,
            max_limit=config.retrieval_max_limit,
            source_ids=[resume_id],
        )
    all_hits = await search_chunks(
        session,
        cached,
        user_id,
        question,
        limit=config.top_k,
        max_limit=config.retrieval_max_limit,
        source_ids=source_ids,
    )
    merged = merge_hits(resume_hits, all_hits, config.top_k)
    return order_for_prompt(apply_budget(merged, config.max_chars))
