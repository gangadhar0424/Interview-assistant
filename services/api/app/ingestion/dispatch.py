"""Queue dispatch. The queue is only a delivery mechanism; the database is the source of truth.

If dispatching fails (e.g. Redis is down) the job simply stays `pending` and the recovery sweep
re-dispatches it, so a job is never lost.
"""

import uuid
from typing import Protocol


class JobDispatcher(Protocol):
    def dispatch(self, job_id: uuid.UUID, user_id: uuid.UUID) -> None:
        """Enqueue the job for a worker. May raise if the queue is unavailable."""
        ...


class DramatiqDispatcher:
    def dispatch(self, job_id: uuid.UUID, user_id: uuid.UUID) -> None:
        # Imported lazily: importing the tasks module configures the Redis broker.
        from app.workers.tasks import ingest_job

        ingest_job.send(str(job_id), str(user_id))
