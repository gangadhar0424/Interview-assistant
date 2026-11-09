"""Queue re-embedding after the embedding model changed, then exit:

    uv run python -m app.workers.reindex

Finds every extracted text that has no vectors for the CURRENT `EMBEDDING_MODEL`, creates/resets
its `embed` job and dispatches it. Re-indexing replaces a file's chunks and vectors atomically (old
model's vectors are removed, never mixed). Files that are already indexed for the current model
are not touched, and unchanged files are never re-embedded.
"""

import asyncio
import sys

from sqlalchemy import select

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.models import IngestionJob
from app.db.session import create_engine, create_maintenance_sessionmaker
from app.embeddings.factory import build_embedding_provider
from app.ingestion.dispatch import DramatiqDispatcher
from app.ingestion.service import dispatch_best_effort, enqueue_missing_embeddings


async def main() -> int:
    settings = get_settings()
    configure_logging(settings.log_level)
    model_id = build_embedding_provider(settings).model_id  # does not load the model
    engine = create_engine(settings.maintenance_database_url or settings.database_url)
    try:
        async with create_maintenance_sessionmaker(engine)() as session:
            job_ids = await enqueue_missing_embeddings(session, model_id)
            job_users = (
                await session.execute(
                    select(IngestionJob.id, IngestionJob.user_id).where(
                        IngestionJob.id.in_(job_ids)
                    )
                )
            ).all()
        dispatcher = DramatiqDispatcher()
        sent = sum(
            [
                await dispatch_best_effort(dispatcher, job_id, user_id)
                for job_id, user_id in job_users
            ]
        )
        print(  # noqa: T201
            f"model={model_id} queued={len(job_ids)} dispatched={sent} "
            f"(undelivered jobs stay pending for the recovery sweep)"
        )
        return 0
    finally:
        await engine.dispose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
