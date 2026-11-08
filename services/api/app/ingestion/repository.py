"""Ingestion data access. Reads are always scoped by `user_id` (isolation by construction)."""

import uuid
from collections.abc import Sequence

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Chunk, ExtractedText, IngestionJob

JobsByStage = dict[str, IngestionJob]


async def get_jobs_for_source(
    session: AsyncSession, user_id: uuid.UUID, source_type: str, source_id: uuid.UUID
) -> JobsByStage:
    stmt = select(IngestionJob).where(
        IngestionJob.user_id == user_id,
        IngestionJob.source_type == source_type,
        IngestionJob.source_id == source_id,
    )
    return {job.stage: job for job in (await session.execute(stmt)).scalars()}


async def get_job_for_source(
    session: AsyncSession,
    user_id: uuid.UUID,
    source_type: str,
    source_id: uuid.UUID,
    stage: str = "extract",
) -> IngestionJob | None:
    return (await get_jobs_for_source(session, user_id, source_type, source_id)).get(stage)


async def jobs_for_sources(
    session: AsyncSession, user_id: uuid.UUID, source_type: str, source_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, JobsByStage]:
    if not source_ids:
        return {}
    stmt = select(IngestionJob).where(
        IngestionJob.user_id == user_id,
        IngestionJob.source_type == source_type,
        IngestionJob.source_id.in_(source_ids),
    )
    result: dict[uuid.UUID, JobsByStage] = {}
    for job in (await session.execute(stmt)).scalars():
        result.setdefault(job.source_id, {})[job.stage] = job
    return result


async def get_extracted_text(
    session: AsyncSession, user_id: uuid.UUID, source_type: str, source_id: uuid.UUID
) -> ExtractedText | None:
    """Internal accessor for the embedding stage. Not exposed through any API."""
    stmt = select(ExtractedText).where(
        ExtractedText.user_id == user_id,
        ExtractedText.source_type == source_type,
        ExtractedText.source_id == source_id,
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def delete_for_source(
    session: AsyncSession, user_id: uuid.UUID, source_type: str, source_id: uuid.UUID
) -> None:
    """Remove a source's whole ingestion state. Call in the same transaction as the file delete.

    Order matters: jobs first, then text, then chunks (embeddings cascade from chunks). A worker
    finishing concurrently holds its job row's lock, so by the time the job delete proceeds any
    text/chunks it committed are visible to the deletes that follow: nothing is left orphaned.
    """
    await session.execute(
        delete(IngestionJob).where(
            IngestionJob.user_id == user_id,
            IngestionJob.source_type == source_type,
            IngestionJob.source_id == source_id,
        )
    )
    await session.execute(
        delete(ExtractedText).where(
            ExtractedText.user_id == user_id,
            ExtractedText.source_type == source_type,
            ExtractedText.source_id == source_id,
        )
    )
    await session.execute(
        delete(Chunk).where(
            Chunk.user_id == user_id,
            Chunk.source_type == source_type,
            Chunk.source_id == source_id,
        )
    )
