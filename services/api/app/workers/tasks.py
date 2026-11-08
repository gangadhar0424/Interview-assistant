"""Background tasks. Start a worker from services/api:

    uv run --extra embeddings dramatiq app.workers.tasks --processes 1 --threads 2

(`--extra embeddings` is only needed with EMBEDDING_PROVIDER=fastembed.) The worker uses the same
settings as the API (DATABASE_URL, FILE_STORAGE_ROOT, EMBEDDING_*, ...). Retries are managed by
the database job state and the recovery sweep, not by Dramatiq (max_retries=0), so a failing job
is recorded exactly once instead of being replayed blindly. One actor serves both pipeline
stages (`extract`, `embed`); the stage is stored on the job row.
"""

import asyncio
import logging
import uuid

import dramatiq

from app.core.config import get_settings
from app.db.session import create_engine, create_sessionmaker
from app.embeddings.factory import build_embedding_provider
from app.ingestion.chunking import ChunkConfig
from app.ingestion.dispatch import DramatiqDispatcher
from app.ingestion.service import process_job
from app.storage.local import LocalFileStorage
from app.workers import broker as _broker  # noqa: F401  (configures the broker before actors)

logger = logging.getLogger(__name__)


async def _run(job_id: uuid.UUID) -> str:
    settings = get_settings()
    engine = create_engine(settings.database_url)
    try:
        return await process_job(
            create_sessionmaker(engine),
            LocalFileStorage(settings.storage_root),
            job_id,
            embedder=build_embedding_provider(settings),
            dispatcher=DramatiqDispatcher(),
            chunk_config=ChunkConfig(settings.chunk_size_chars, settings.chunk_overlap_chars),
            max_attempts=settings.ingestion_max_attempts,
        )
    finally:
        await engine.dispose()


@dramatiq.actor(queue_name="ingestion", max_retries=0, time_limit=30 * 60 * 1000)
def ingest_job(job_id: str) -> None:
    outcome = asyncio.run(_run(uuid.UUID(job_id)))
    logger.info("ingest_job finished", extra={"job_id": job_id, "outcome": outcome})
