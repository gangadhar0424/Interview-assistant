"""Re-apply the deletion ledger after a database restore (SEC-13 Decision 3-R2, D-85).

Run AFTER `infra/backup/restore.sh` and BEFORE the API and worker are started, with the restored
environment's settings (DATABASE_URL, FILE_STORAGE_ROOT, DELETION_LEDGER_PATH, DELETION_LEDGER_KEY):

    uv run python -m app.lifecycle.replay            # dry run: what would be deleted
    uv run python -m app.lifecycle.replay --apply    # delete again everything the ledger names

Each entry is applied as the runtime role under the owning user's tenant context, by id only:
  account  -> delete the user row (everything cascades) and `uploads/<user_id>/`
  session  -> delete the session (questions, answers, links cascade)
  resume / document -> delete the stored file, its ingestion rows and the row (the normal path)
Idempotent: an object that no longer exists counts as `absent`, so a second run changes nothing.
Lines that fail their MAC are never applied; their count makes the exit status non-zero so an
operator investigates. Output is ids and counts only. Nothing is appended to the ledger.
"""

import argparse
import asyncio
import logging
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.core.logging import configure_logging
from app.db.models import CallSession, Document, Resume, User
from app.db.session import create_engine, create_sessionmaker, set_tenant_context
from app.files import service as files_service
from app.files.kinds import document_kind, resume_kind
from app.lifecycle.ledger import DeletionLedger, LedgerEntry
from app.sessions import repository as sessions_repository
from app.storage.base import FileStorage
from app.storage.local import LocalFileStorage

logger = logging.getLogger(__name__)

Outcome = Literal["deleted", "absent", "would_delete", "failed"]


@dataclass
class ReplayReport:
    outcomes: dict[str, int] = field(default_factory=dict)
    failed_entries: list[uuid.UUID] = field(default_factory=list)
    invalid_lines: int = 0

    def add(self, outcome: Outcome) -> None:
        self.outcomes[outcome] = self.outcomes.get(outcome, 0) + 1

    @property
    def ok(self) -> bool:
        return not self.failed_entries and self.invalid_lines == 0


async def _exists(session: AsyncSession, entry: LedgerEntry) -> bool:
    owner, subject = entry.owner_user_id, entry.subject_id
    if entry.kind == "account":
        stmt = select(User.id).where(User.id == subject)
    elif entry.kind == "session":
        stmt = select(CallSession.id).where(CallSession.id == subject, CallSession.user_id == owner)
    elif entry.kind == "resume":
        stmt = select(Resume.id).where(Resume.id == subject, Resume.user_id == owner)
    else:
        stmt = select(Document.id).where(Document.id == subject, Document.user_id == owner)
    return (await session.execute(stmt)).first() is not None


async def apply_entry(
    session: AsyncSession,
    storage: FileStorage,
    settings: Settings,
    entry: LedgerEntry,
    *,
    apply: bool,
) -> Outcome:
    """Delete one ledger entry's object again. `session` must be fresh (one tenant per session)."""
    await set_tenant_context(session, entry.owner_user_id)
    present = await _exists(session, entry)
    if entry.kind == "account":
        if not apply:
            return "would_delete" if present else "absent"
        if present:
            await session.execute(delete(User).where(User.id == entry.owner_user_id))
            await session.commit()
        # The uploads archive is restored separately from the database: always clear the tree.
        removed = await asyncio.to_thread(storage.delete_user_tree, entry.owner_user_id)
        return "deleted" if present or removed else "absent"
    if not present:
        return "absent"
    if not apply:
        return "would_delete"
    if entry.kind == "session":
        await sessions_repository.delete_for_user(session, entry.owner_user_id, entry.subject_id)
        await session.commit()
        return "deleted"
    kind = resume_kind(settings) if entry.kind == "resume" else document_kind(settings)
    try:
        await files_service.delete_file(
            session, storage, entry.owner_user_id, kind, entry.subject_id
        )
    except AppError as exc:
        if exc.status_code == 404:
            return "absent"
        raise
    return "deleted"


async def replay(
    sessions: async_sessionmaker[AsyncSession],
    storage: FileStorage,
    settings: Settings,
    entries: Sequence[LedgerEntry],
    *,
    apply: bool,
    invalid_lines: int = 0,
) -> ReplayReport:
    report = ReplayReport(invalid_lines=invalid_lines)
    for entry in entries:  # oldest first
        try:
            async with sessions() as session:
                outcome = await apply_entry(session, storage, settings, entry, apply=apply)
        except Exception:  # noqa: BLE001 - keep going; the run as a whole fails
            logger.exception(
                "ledger replay failed for an entry",
                extra={"entry_id": str(entry.entry_id), "kind": entry.kind},
            )
            report.failed_entries.append(entry.entry_id)
            outcome = "failed"
        report.add(outcome)
    logger.info(
        "deletion ledger replay",
        extra={"apply": apply, **report.outcomes, "invalid_lines": invalid_lines},
    )
    return report


async def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--apply", action="store_true", help="delete (default: dry run)")
    args = parser.parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level)
    if settings.deletion_ledger_path is None or settings.deletion_ledger_key is None:
        print("DELETION_LEDGER_PATH and DELETION_LEDGER_KEY must be set")  # noqa: T201
        return 2
    ledger = DeletionLedger(
        settings.deletion_ledger_path, settings.deletion_ledger_key.get_secret_value()
    )
    read = ledger.read()
    engine = create_engine(settings.database_url, settings)
    try:
        report = await replay(
            create_sessionmaker(engine),
            LocalFileStorage(settings.storage_root),
            settings,
            read.entries,
            apply=args.apply,
            invalid_lines=read.invalid_lines,
        )
    finally:
        await engine.dispose()
    counts = " ".join(f"{k}={v}" for k, v in sorted(report.outcomes.items()))
    print(  # noqa: T201
        f"files={read.files} entries={len(read.entries)} invalid_lines={read.invalid_lines} "
        f"apply={args.apply} {counts}".strip()
    )
    for entry_id in report.failed_entries:
        print(f"failed entry_id={entry_id}")  # noqa: T201
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
