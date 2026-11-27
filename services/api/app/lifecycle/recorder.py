"""What happens after a deletion has committed (SEC-13, D-85).

Called by the delete routes AFTER the database commit, never before, so a refused or failed
deletion leaves no trace. Two follow-up steps, in this order:

1. Purge the answer cache entries indexed under the deleted object (Decision 1, option 1b).
2. Append a ledger entry so a restore cannot bring the object back (Decision 3-R2).

Neither step can fail the user's deletion: the data is already gone from the database. A failure
is logged with ids only and counted (`interview_deletion_steps{result="failed"}`), which the
operator must alert on (`docs/OPERATIONS.md`).
"""

import asyncio
import logging
import uuid

from app.core.config import Settings
from app.core.metrics import deletion_steps
from app.lifecycle.ledger import DeletionLedger, Kind
from app.qa.cache import AnswerCache

logger = logging.getLogger(__name__)


class DeletionRecorder:
    def __init__(self, cache: AnswerCache | None, ledger: DeletionLedger | None) -> None:
        self._cache = cache
        self._ledger = ledger

    @property
    def ledger_enabled(self) -> bool:
        return self._ledger is not None

    async def _record(self, kind: Kind, subject_id: uuid.UUID, owner_user_id: uuid.UUID) -> bool:
        if self._ledger is None:  # only possible in APP_ENV=local (config requires it elsewhere)
            deletion_steps.labels(step="ledger", result="skipped").inc()
            return False
        try:
            await asyncio.to_thread(self._ledger.append, kind, subject_id, owner_user_id)
        except Exception:  # noqa: BLE001 - never fail a deletion that has already committed
            deletion_steps.labels(step="ledger", result="failed").inc()
            logger.error(
                "deletion ledger write failed",
                extra={"kind": kind, "subject_id": str(subject_id), "user_id": str(owner_user_id)},
            )
            return False
        deletion_steps.labels(step="ledger", result="ok").inc()
        return True

    async def _purge_result(self, ok: bool) -> None:
        deletion_steps.labels(step="cache_purge", result="ok" if ok else "failed").inc()

    async def session_deleted(self, user_id: uuid.UUID, session_id: uuid.UUID) -> None:
        if self._cache is not None:
            await self._purge_result(await self._cache.purge_session(user_id, session_id))
        await self._record("session", session_id, user_id)

    async def file_deleted(self, user_id: uuid.UUID, kind: Kind, file_id: uuid.UUID) -> None:
        if self._cache is not None:
            await self._purge_result(await self._cache.purge_source(user_id, file_id))
        await self._record(kind, file_id, user_id)

    async def account_deleted(self, user_id: uuid.UUID) -> None:
        if self._cache is not None:
            await self._purge_result(await self._cache.purge_user(user_id))
        await self._record("account", user_id, user_id)


def build_deletion_ledger(settings: Settings) -> DeletionLedger | None:
    """The configured ledger, or None in local development when none is configured."""
    if settings.deletion_ledger_path is None or settings.deletion_ledger_key is None:
        return None
    return DeletionLedger(
        settings.deletion_ledger_path, settings.deletion_ledger_key.get_secret_value()
    )
