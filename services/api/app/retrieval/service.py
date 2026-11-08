"""Internal, user-scoped semantic retrieval for future RAG features.

There is deliberately NO public API endpoint for this. Callers are trusted application code that
pass the authenticated `user_id` (from `CurrentUser`), never a client-supplied identity.

Security: ownership is enforced inside the SQL query (`user_id` on both the vector row and the
chunk row), not by filtering results in application memory, and only vectors produced by the
SAME embedding model as the query are compared (vectors of different models are incomparable).
Retrieval is always constrained to one user's rows, so an exact scan over that filtered set is
used (no approximate index that could drop results under a restrictive filter; D-31).
"""

import asyncio
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.types import vector_literal
from app.embeddings.base import EmbeddingProvider

MAX_QUERY_CHARS = 2000
DEFAULT_LIMIT = 5


class RetrievalError(ValueError):
    """Invalid retrieval request (safe to surface to callers)."""


@dataclass(frozen=True)
class ChunkHit:
    chunk_id: uuid.UUID
    source_type: str
    source_id: uuid.UUID
    chunk_index: int
    text: str
    similarity: float  # cosine similarity in [-1, 1]; higher is more similar


async def search_chunks(
    session: AsyncSession,
    embedder: EmbeddingProvider,
    user_id: uuid.UUID,
    query: str,
    *,
    limit: int = DEFAULT_LIMIT,
    max_limit: int = 20,
    source_type: str | None = None,
    source_ids: Sequence[uuid.UUID] | None = None,
) -> list[ChunkHit]:
    """Return the user's most similar chunks, best first.

    `source_type` / `source_ids` optionally restrict the search to specific files (they can only
    narrow the user's own data; another user's source id simply yields no rows).
    """
    cleaned = query.strip()
    if not cleaned:
        raise RetrievalError("query must not be empty")
    if len(cleaned) > MAX_QUERY_CHARS:
        raise RetrievalError(f"query must be at most {MAX_QUERY_CHARS} characters")
    if not 1 <= limit <= max_limit:
        raise RetrievalError(f"limit must be between 1 and {max_limit}")
    if source_type is not None and source_type not in ("resume", "document"):
        raise RetrievalError("unknown source_type")
    if source_ids is not None and not source_ids:
        return []  # an explicit empty filter matches nothing

    query_vector = await asyncio.to_thread(embedder.embed_query, cleaned)
    if len(query_vector) != embedder.dimension:
        raise RetrievalError("embedding dimension mismatch")

    where = ["e.user_id = :uid", "c.user_id = :uid", "e.embedding_model = :model"]
    params: dict[str, object] = {
        "uid": user_id,
        "model": embedder.model_id,
        "q": vector_literal(query_vector),
        "limit": limit,
    }
    if source_type is not None:
        where.append("c.source_type = :st")
        params["st"] = source_type
    stmt_text = (
        "SELECT c.id AS chunk_id, c.source_type, c.source_id, c.chunk_index, c.text, "
        "       1 - (e.embedding <=> CAST(CAST(:q AS text) AS vector)) AS similarity "
        "FROM chunk_embeddings e JOIN chunks c ON c.id = e.chunk_id "
        "WHERE " + " AND ".join(where)
    )
    if source_ids is not None:
        stmt_text += " AND c.source_id = ANY(:sids)"
    stmt_text += (
        " ORDER BY e.embedding <=> CAST(CAST(:q AS text) AS vector), c.source_id, c.chunk_index "
        "LIMIT :limit"
    )
    stmt = text(stmt_text)
    if source_ids is not None:
        stmt = stmt.bindparams(bindparam("sids", type_=ARRAY(UUID(as_uuid=True))))
        params["sids"] = list(source_ids)

    rows = (await session.execute(stmt, params)).all()
    return [
        ChunkHit(
            chunk_id=r.chunk_id,
            source_type=r.source_type,
            source_id=r.source_id,
            chunk_index=r.chunk_index,
            text=r.text,
            similarity=float(r.similarity),
        )
        for r in rows
    ]
