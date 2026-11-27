"""Orphan-file sweep (SEC-13 Decision 7, D-85).

Finds two kinds of inconsistency between file storage and the database:

* **orphan file**: a stored file under `uploads/` that no resume or document row names (for
  example left by a crash between the file move and the row insert, or by an account deletion whose
  file removal failed). Removed only with `--apply`, only when older than
  ORPHAN_SWEEP_MIN_AGE_SECONDS (an upload writes the file BEFORE it inserts the row), and only after
  a second, per-file database check made immediately before removal, so a row inserted while the
  sweep ran is never broken.
* **missing file**: a resume or document row whose stored file is gone. Reported only (ids); the
  user already sees "file missing" and can delete the entry. Rows are never changed here.

Also reported, never removed: files under `uploads/` whose name does not have the generated key
shape (this storage never writes such names, so they need a person to look at them), and with
`--apply` stale upload temp parts older than the same age are removed.

Output is counts and safe identifiers (row ids, user ids); never file names supplied by users,
contents or paths outside the storage layout. Reading every tenant's rows needs the maintenance
login (MAINTENANCE_DATABASE_URL), like the ingestion recovery sweep.

    uv run python -m app.lifecycle.orphans            # dry run
    uv run python -m app.lifecycle.orphans --apply
"""

import argparse
import asyncio
import logging
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import select, union_all
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.metrics import orphan_sweep_findings
from app.db.models import Document, Resume
from app.db.session import create_engine, create_maintenance_sessionmaker
from app.storage.base import FileStorage
from app.storage.local import LocalFileStorage

logger = logging.getLogger(__name__)


@dataclass
class SweepReport:
    orphan_files: int = 0  # found, old enough to act on
    orphan_files_removed: int = 0
    orphan_files_kept_young: int = 0  # too recent: maybe an upload in progress
    orphan_files_rescued: int = 0  # a row appeared before removal: kept
    orphan_owner_ids: list[str] = field(default_factory=list)  # user ids from the key path
    missing_files: list[str] = field(default_factory=list)  # row ids ("resume:<id>")
    malformed_entries: int = 0
    stale_temp_removed: int = 0
    errors: int = 0


async def _all_keys(session: AsyncSession) -> set[str]:
    stmt = union_all(select(Resume.storage_key), select(Document.storage_key))
    return {row[0] for row in (await session.execute(stmt)).all()}


async def _key_is_referenced(session: AsyncSession, key: str) -> bool:
    stmt = union_all(
        select(Resume.id).where(Resume.storage_key == key),
        select(Document.id).where(Document.storage_key == key),
    )
    return (await session.execute(stmt)).first() is not None


async def _rows(session: AsyncSession) -> list[tuple[str, uuid.UUID, str]]:
    resumes = select(Resume.id, Resume.storage_key)
    documents = select(Document.id, Document.storage_key)
    rows: list[tuple[str, uuid.UUID, str]] = []
    rows += [("resume", r[0], r[1]) for r in (await session.execute(resumes)).all()]
    rows += [("document", r[0], r[1]) for r in (await session.execute(documents)).all()]
    return rows


async def sweep(
    sessions: async_sessionmaker[AsyncSession],
    storage: FileStorage,
    *,
    min_age_seconds: float,
    apply: bool,
) -> SweepReport:
    report = SweepReport()
    async with sessions() as session:
        referenced = await _all_keys(session)
        rows = await _rows(session)

    # Rows whose file is missing (report only).
    for kind, row_id, key in rows:
        try:
            present = await asyncio.to_thread(storage.exists, key)
        except Exception:  # noqa: BLE001 - a malformed stored key is itself a finding
            present = False
        if not present:
            report.missing_files.append(f"{kind}:{row_id}")

    # Files that no row names.
    entries = await asyncio.to_thread(lambda: list(storage.iter_stored()))
    for entry in entries:
        if entry.key is None:
            report.malformed_entries += 1
            continue
        if entry.key in referenced:
            continue
        if entry.age_seconds < min_age_seconds:
            report.orphan_files_kept_young += 1
            continue
        report.orphan_files += 1
        owner = entry.key.split("/")[1]
        if owner not in report.orphan_owner_ids:
            report.orphan_owner_ids.append(owner)
        if not apply:
            continue
        try:
            async with sessions() as session:  # re-check right before removal (race with insert)
                if await _key_is_referenced(session, entry.key):
                    report.orphan_files_rescued += 1
                    continue
            if await asyncio.to_thread(storage.delete, entry.key):
                report.orphan_files_removed += 1
        except Exception:  # noqa: BLE001 - keep sweeping; the next run retries
            report.errors += 1
            logger.exception("orphan file removal failed", extra={"user_id": owner})

    purge_temp = getattr(storage, "purge_stale_temp", None)
    if apply and purge_temp is not None:
        report.stale_temp_removed = await asyncio.to_thread(purge_temp, min_age_seconds)

    orphan_sweep_findings.labels(kind="orphan_file").inc(report.orphan_files)
    orphan_sweep_findings.labels(kind="missing_file").inc(len(report.missing_files))
    orphan_sweep_findings.labels(kind="malformed").inc(report.malformed_entries)
    logger.info(
        "orphan file sweep",
        extra={
            "apply": apply,
            "orphan_files": report.orphan_files,
            "orphan_files_removed": report.orphan_files_removed,
            "orphan_files_kept_young": report.orphan_files_kept_young,
            "orphan_files_rescued": report.orphan_files_rescued,
            "missing_files": len(report.missing_files),
            "malformed_entries": report.malformed_entries,
            "stale_temp_removed": report.stale_temp_removed,
            "errors": report.errors,
        },
    )
    return report


def render(report: SweepReport, apply: bool) -> list[str]:
    lines = [
        f"apply={apply} orphan_files={report.orphan_files} removed={report.orphan_files_removed} "
        f"kept_young={report.orphan_files_kept_young} rescued={report.orphan_files_rescued} "
        f"missing_files={len(report.missing_files)} malformed={report.malformed_entries} "
        f"stale_temp_removed={report.stale_temp_removed} errors={report.errors}"
    ]
    lines += [f"orphan owner_user_id={owner}" for owner in report.orphan_owner_ids]
    lines += [f"missing {row}" for row in report.missing_files]
    return lines


async def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description="Orphan-file sweep (SEC-13 Decision 7)")
    parser.add_argument("--apply", action="store_true", help="remove orphans (default: dry run)")
    args = parser.parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level)
    engine = create_engine(settings.require_maintenance_database_url(), settings)
    try:
        report = await sweep(
            create_maintenance_sessionmaker(engine),
            LocalFileStorage(settings.storage_root),
            min_age_seconds=settings.orphan_sweep_min_age_seconds,
            apply=args.apply,
        )
    finally:
        await engine.dispose()
    for line in render(report, args.apply):
        print(line)  # noqa: T201
    return 1 if report.errors else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
