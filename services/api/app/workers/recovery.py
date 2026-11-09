"""Run one ingestion recovery sweep and exit (schedule it every minute or so, or run on demand):

    uv run python -m app.workers.recovery            # act
    uv run python -m app.workers.recovery --dry-run  # only list stuck jobs

See app/ingestion/service.py:recover_stale_jobs for the exact rules.
"""

import asyncio
import sys

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.session import create_engine, create_maintenance_sessionmaker
from app.ingestion.dispatch import DramatiqDispatcher
from app.ingestion.service import find_stuck_jobs, recover_stale_jobs


async def main(dry_run: bool) -> int:
    settings = get_settings()
    configure_logging(settings.log_level)
    engine = create_engine(settings.maintenance_database_url or settings.database_url)
    sessions = create_maintenance_sessionmaker(engine)
    try:
        if dry_run:
            async with sessions() as session:
                stuck = await find_stuck_jobs(
                    session,
                    processing_timeout_seconds=settings.ingestion_processing_timeout_seconds,
                    pending_redispatch_seconds=settings.ingestion_pending_redispatch_seconds,
                )
            for job in stuck:
                print(f"{job.id} status={job.status} attempt={job.attempt}")  # noqa: T201
            print(f"{len(stuck)} stuck job(s)")  # noqa: T201
            return 0
        report = await recover_stale_jobs(
            sessions,
            DramatiqDispatcher(),
            max_attempts=settings.ingestion_max_attempts,
            processing_timeout_seconds=settings.ingestion_processing_timeout_seconds,
            pending_redispatch_seconds=settings.ingestion_pending_redispatch_seconds,
        )
        print(  # noqa: T201
            f"failed={len(report.failed)} requeued={len(report.requeued)} "
            f"stale_pending={len(report.stale_pending)} dispatched={len(report.dispatched)} "
            f"dispatch_failed={len(report.dispatch_failed)}"
        )
        return 1 if report.dispatch_failed else 0
    finally:
        await engine.dispose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main("--dry-run" in sys.argv)))
