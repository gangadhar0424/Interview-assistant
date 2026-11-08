"""Upload, list, download and delete use-cases for user-owned files.

Consistency strategy (local storage, no distributed transactions):
- Upload: bytes go to a temp file -> validated -> atomically moved to a server-generated key ->
  DB row inserted. If the DB step fails, the just-stored file is deleted (no orphan). A crash
  between the move and the insert can leave an unreferenced file; it is invisible to users and
  reconcilable later (see docs).
- Delete: file first (idempotent), then the DB row. If the file cannot be deleted, nothing
  changes and the user may retry. If the row cannot be deleted after the file is gone, the record
  remains and downloads return 410 `file_missing`; retrying the delete succeeds.
Logs contain IDs only, never paths or file names.
"""

import asyncio
import logging
import uuid
from collections.abc import Iterator

from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import UploadFile

from app.core.errors import AppError
from app.files import repository, validation
from app.files.kinds import FileKind
from app.files.repository import FileRow
from app.ingestion import repository as ingestion_repository
from app.ingestion import service as ingestion
from app.ingestion.dispatch import JobDispatcher
from app.ingestion.repository import JobsByStage
from app.storage.base import (
    FileStorage,
    FileTooLarge,
    InvalidStorageKey,
    StorageError,
    StoredFileMissing,
)

logger = logging.getLogger(__name__)


def not_found() -> AppError:
    # Identical for "does not exist" and "belongs to someone else": no existence leak.
    return AppError(404, "not_found", "Resource not found.")


def _unsupported(kind: FileKind) -> AppError:
    allowed = ", ".join(sorted(kind.allowed_extensions))
    return AppError(415, "unsupported_file_type", f"Unsupported file type. Allowed: {allowed}.")


async def create_file(
    session: AsyncSession,
    storage: FileStorage,
    dispatcher: JobDispatcher,
    user_id: uuid.UUID,
    kind: FileKind,
    upload: UploadFile,
) -> tuple[FileRow, JobsByStage]:
    filename = validation.sanitize_filename(upload.filename)
    extension = validation.extension_of(filename)
    fmt = validation.FORMATS.get(extension) if extension in kind.allowed_extensions else None
    if fmt is None or not validation.declared_type_acceptable(fmt, upload.content_type):
        raise _unsupported(kind)

    pending = None
    try:
        pending = await asyncio.to_thread(storage.begin, user_id, kind.namespace, kind.max_bytes)
        while chunk := await upload.read(validation.CHUNK_SIZE):
            await asyncio.to_thread(pending.write, chunk)
        if pending.size == 0:
            raise AppError(422, "empty_file", "The uploaded file is empty.")

        def check() -> None:
            assert pending is not None  # noqa: S101  (narrowing for the type checker)
            with pending.reader() as handle:
                fmt.validate(handle, pending.size)

        await asyncio.to_thread(check)
        stored = await asyncio.to_thread(pending.commit)
    except FileTooLarge:
        limit_mb = kind.max_bytes // (1024 * 1024)
        raise AppError(
            413, "file_too_large", f"The file is too large (maximum {limit_mb} MB)."
        ) from None
    except validation.InvalidContent as exc:
        raise AppError(422, "invalid_file_content", str(exc)) from None
    except (StorageError, OSError):
        logger.exception("storing upload failed", extra={"user_id": str(user_id)})
        raise AppError(
            500, "save_failed", "The file could not be saved. Please try again."
        ) from None
    finally:
        if pending is not None:
            await asyncio.to_thread(pending.abort)  # no-op once committed

    row = kind.model(
        id=uuid.uuid4(),
        user_id=user_id,
        original_filename=filename,
        content_type=fmt.mime,
        size_bytes=stored.size,
        sha256=stored.sha256,
        storage_key=stored.key,
    )
    job = ingestion.new_job(user_id, kind.name, row.id)
    session.add(row)
    session.add(job)  # same transaction as the file record: both exist or neither does
    try:
        await session.commit()
    except Exception:
        await session.rollback()
        logger.exception("saving file record failed", extra={"user_id": str(user_id)})
        await _discard_stored(storage, stored.key)
        raise AppError(
            500, "save_failed", "The file could not be saved. Please try again."
        ) from None
    logger.info(
        "file uploaded",
        extra={"user_id": str(user_id), "kind": kind.name, "file_id": str(row.id)},
    )
    # Dispatch only after the commit. If the queue is down the job stays `pending` and the
    # recovery sweep re-sends it; the upload itself still succeeds.
    await ingestion.dispatch_best_effort(dispatcher, job.id)
    return row, {job.stage: job}


async def _discard_stored(storage: FileStorage, key: str) -> None:
    """Best-effort removal of a stored file whose DB row could not be created."""
    try:
        await asyncio.to_thread(storage.delete, key)
    except (StorageError, OSError):
        # Orphan: unreferenced and unreachable through the API. Reconcile later.
        logger.error("orphaned stored file could not be removed", extra={"storage_key": key})


async def list_files(
    session: AsyncSession, user_id: uuid.UUID, kind: FileKind, limit: int
) -> list[tuple[FileRow, JobsByStage]]:
    rows = await repository.list_for_user(session, kind.model, user_id, limit)
    jobs = await ingestion_repository.jobs_for_sources(
        session, user_id, kind.name, [r.id for r in rows]
    )
    return [(r, jobs.get(r.id, {})) for r in rows]


async def get_file(
    session: AsyncSession, user_id: uuid.UUID, kind: FileKind, file_id: uuid.UUID
) -> tuple[FileRow, JobsByStage]:
    row = await repository.get_for_user(session, kind.model, user_id, file_id)
    if row is None:
        raise not_found()
    jobs = await ingestion_repository.get_jobs_for_source(session, user_id, kind.name, row.id)
    return row, jobs


async def retry_ingestion(
    session: AsyncSession,
    dispatcher: JobDispatcher,
    user_id: uuid.UUID,
    kind: FileKind,
    file_id: uuid.UUID,
) -> tuple[FileRow, JobsByStage]:
    """Re-queue a failed ingestion (extract or embed). Ownership-checked; double-submit safe."""
    row = await repository.get_for_user(session, kind.model, user_id, file_id)
    if row is None:
        raise not_found()
    job_ids = await ingestion.reset_failed_jobs(session, user_id, kind.name, row.id)
    if not job_ids:
        raise AppError(409, "not_retryable", "Only files whose processing failed can be retried.")
    for job_id in job_ids:
        await ingestion.dispatch_best_effort(dispatcher, job_id)
    jobs = await ingestion_repository.get_jobs_for_source(session, user_id, kind.name, row.id)
    return row, jobs


async def open_for_download(
    session: AsyncSession,
    storage: FileStorage,
    user_id: uuid.UUID,
    kind: FileKind,
    file_id: uuid.UUID,
) -> tuple[FileRow, Iterator[bytes]]:
    row = await repository.get_for_user(session, kind.model, user_id, file_id)
    if row is None:
        raise not_found()
    try:
        stream = await asyncio.to_thread(storage.open_stream, row.storage_key)
    except (StoredFileMissing, InvalidStorageKey):
        logger.error(
            "stored file missing for existing record",
            extra={"user_id": str(user_id), "kind": kind.name, "file_id": str(row.id)},
        )
        raise AppError(
            410,
            "file_missing",
            "The stored file is no longer available. You can delete this entry.",
        ) from None
    except OSError:
        logger.exception("opening stored file failed", extra={"file_id": str(row.id)})
        raise AppError(
            500, "read_failed", "The file could not be read. Please try again."
        ) from None
    return row, stream


async def delete_file(
    session: AsyncSession,
    storage: FileStorage,
    user_id: uuid.UUID,
    kind: FileKind,
    file_id: uuid.UUID,
) -> None:
    row = await repository.get_for_user(session, kind.model, user_id, file_id)
    if row is None:
        raise not_found()
    try:
        await asyncio.to_thread(storage.delete, row.storage_key)  # idempotent: missing is fine
    except (StorageError, OSError):
        logger.exception("deleting stored file failed", extra={"file_id": str(row.id)})
        raise AppError(
            500, "delete_failed", "The file could not be deleted. Please try again."
        ) from None
    try:
        await ingestion_repository.delete_for_source(session, user_id, kind.name, row.id)
        await repository.delete_row(session, row)
        await session.commit()
    except Exception:
        await session.rollback()
        logger.error(
            "file removed from storage but record deletion failed",
            extra={"user_id": str(user_id), "kind": kind.name, "file_id": str(file_id)},
        )
        raise AppError(
            500, "delete_failed", "The file could not be deleted. Please try again."
        ) from None
    logger.info(
        "file deleted",
        extra={"user_id": str(user_id), "kind": kind.name, "file_id": str(file_id)},
    )
