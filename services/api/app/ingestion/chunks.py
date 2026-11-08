"""Chunk + vector persistence. Everything is scoped by `user_id` and the embedding model id."""

import uuid
from collections.abc import Sequence

from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Chunk
from app.db.types import vector_literal
from app.ingestion.chunking import TextChunk


async def is_indexed(
    session: AsyncSession,
    user_id: uuid.UUID,
    source_type: str,
    source_id: uuid.UUID,
    embedding_model: str,
    expected: Sequence[TextChunk],
) -> bool:
    """True if the stored chunks are exactly `expected` and all carry a vector for this model.

    Lets the worker skip re-embedding an unchanged file (no repeated provider calls).
    """
    rows = (
        await session.execute(
            text(
                "SELECT c.chunk_index, c.content_hash, (e.chunk_id IS NOT NULL) AS embedded "
                "FROM chunks c LEFT JOIN chunk_embeddings e "
                "  ON e.chunk_id = c.id AND e.embedding_model = :model AND e.user_id = :uid "
                "WHERE c.user_id = :uid AND c.source_type = :st AND c.source_id = :sid "
                "ORDER BY c.chunk_index"
            ),
            {"uid": user_id, "st": source_type, "sid": source_id, "model": embedding_model},
        )
    ).all()
    if len(rows) != len(expected):
        return False
    return all(
        r.chunk_index == c.index and r.content_hash == c.content_hash and r.embedded
        for r, c in zip(rows, expected, strict=True)
    )


async def replace_chunks(
    session: AsyncSession,
    user_id: uuid.UUID,
    source_type: str,
    source_id: uuid.UUID,
    embedding_model: str,
    chunks: Sequence[TextChunk],
    vectors: Sequence[Sequence[float]],
) -> None:
    """Replace a source's chunk set (and thereby every old vector) inside the caller's transaction.

    Deleting the old chunks cascades to their embeddings, so no duplicate chunks and no vectors
    from another model can survive a re-index.
    """
    await session.execute(
        delete(Chunk).where(
            Chunk.user_id == user_id, Chunk.source_type == source_type, Chunk.source_id == source_id
        )
    )
    ids = [uuid.uuid4() for _ in chunks]
    await session.execute(
        text(
            "INSERT INTO chunks (id, user_id, source_type, source_id, chunk_index, text, "
            "char_start, char_end, content_hash) "
            "VALUES (:id, :uid, :st, :sid, :idx, :text, :cs, :ce, :h)"
        ),
        [
            {
                "id": chunk_id,
                "uid": user_id,
                "st": source_type,
                "sid": source_id,
                "idx": c.index,
                "text": c.text,
                "cs": c.start,
                "ce": c.end,
                "h": c.content_hash,
            }
            for chunk_id, c in zip(ids, chunks, strict=True)
        ],
    )
    await session.execute(
        text(
            "INSERT INTO chunk_embeddings (chunk_id, embedding_model, user_id, embedding) "
            "VALUES (:cid, :model, :uid, CAST(CAST(:emb AS text) AS vector))"
        ),
        [
            {
                "cid": chunk_id,
                "model": embedding_model,
                "uid": user_id,
                "emb": vector_literal(list(v)),
            }
            for chunk_id, v in zip(ids, vectors, strict=True)
        ],
    )
