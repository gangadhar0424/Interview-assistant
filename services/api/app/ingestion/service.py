"""Ingestion workflow, two database-backed stages per file (docs/DECISIONS.md D-29, D-32):

    extract:  claim -> read file -> extract -> normalise -> store text   (Phase 4)
              \\--> in the SAME transaction: create/reset the `embed` job
    embed:    claim -> chunk -> embed (provider call, outside any DB transaction) -> replace
              chunks + vectors and mark completed in ONE transaction

State machine of every job (all transitions are conditional UPDATEs, so atomic and race-safe):

    pending --claim--> processing --success--> completed
       ^                   |  \\--error-----> failed --retry--> pending
       |                   |  \\--transient provider error, attempts left--> pending (auto retry)
       |                   +--stuck too long, attempts left--> pending (recovery)
       +-------------------+--stuck too long, attempts exhausted--> failed (recovery)

Safety rules: error codes/messages stored on a job are fixed strings (never exception text,
content or paths); logs contain IDs only; a worker only writes results while its job is still
`processing`, so a file deleted mid-run never gets orphaned text/chunks/vectors. Because the
`embed` stage is its own job, an embedding failure is retried without re-running extraction.
"""

import asyncio
import hashlib
import logging
import uuid
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Document, ExtractedText, IngestionJob, Resume
from app.embeddings.base import (
    EMBEDDING_DIMENSION,
    EmbeddingError,
    EmbeddingProvider,
    EmbeddingUnavailable,
)
from app.files import repository as file_repository
from app.ingestion import chunks as chunk_store
from app.ingestion import repository as ingestion_repository
from app.ingestion.chunking import ChunkConfig, chunk_text
from app.ingestion.dispatch import JobDispatcher
from app.ingestion.extractors import MAX_TEXT_CHARS, ExtractionError, extractor_for
from app.ingestion.normalize import is_effectively_empty, normalize_text
from app.storage.base import FileStorage, InvalidStorageKey, StoredFileMissing

logger = logging.getLogger(__name__)

SOURCE_MODELS: dict[str, type[Resume] | type[Document]] = {"resume": Resume, "document": Document}

INTERNAL_ERROR = ("internal_error", "Processing failed unexpectedly. You can retry.")
TIMEOUT_ERROR = ("timeout", "Processing did not finish. You can retry.")
UNAVAILABLE_FINAL = (
    "embedding_unavailable",
    "The embedding service is unavailable. You can retry later.",
)
UNAVAILABLE_RETRYING = (
    "embedding_unavailable",
    "The embedding service is unavailable; this will be retried automatically.",
)
MAX_CHUNKS_PER_FILE = 10_000

Sessions = async_sessionmaker[AsyncSession]


class StageError(Exception):
    """A deterministic, user-safe failure of the embed stage (code/message are fixed strings)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code)
        self.code = code
        self.message = message


def new_job(
    user_id: uuid.UUID, source_type: str, source_id: uuid.UUID, stage: str = "extract"
) -> IngestionJob:
    return IngestionJob(
        id=uuid.uuid4(),
        user_id=user_id,
        source_type=source_type,
        source_id=source_id,
        stage=stage,
        status="pending",
        attempt=0,
    )


async def dispatch_best_effort(dispatcher: JobDispatcher, job_id: uuid.UUID) -> bool:
    """Try to enqueue; on failure the job stays `pending` for the recovery sweep. Never raises."""
    try:
        await asyncio.to_thread(dispatcher.dispatch, job_id)
    except Exception:
        logger.warning(
            "ingestion dispatch failed; job stays pending for recovery",
            extra={"job_id": str(job_id)},
        )
        return False
    return True


# ------------------------------------------------------------------ worker side


@dataclass(frozen=True)
class _Claim:
    job_id: uuid.UUID
    user_id: uuid.UUID
    source_type: str
    source_id: uuid.UUID
    stage: str
    attempt: int


async def _claim(sessions: Sessions, job_id: uuid.UUID) -> _Claim | None:
    """Atomically move pending -> processing. Only one worker can win; duplicates get None."""
    async with sessions() as session:
        stmt = (
            update(IngestionJob)
            .where(IngestionJob.id == job_id, IngestionJob.status == "pending")
            .values(
                status="processing",
                attempt=IngestionJob.attempt + 1,
                locked_at=func.now(),
                started_at=func.coalesce(IngestionJob.started_at, func.now()),
                finished_at=None,
                error_code=None,
                error_message=None,
                updated_at=func.now(),
            )
            .returning(
                IngestionJob.user_id,
                IngestionJob.source_type,
                IngestionJob.source_id,
                IngestionJob.stage,
                IngestionJob.attempt,
            )
        )
        row = (await session.execute(stmt)).one_or_none()
        await session.commit()
    if row is None:
        return None
    return _Claim(job_id, row.user_id, row.source_type, row.source_id, row.stage, row.attempt)


def _read_all(storage: FileStorage, key: str) -> bytes:
    # Bounded: uploads are capped at 10/20 MB by validation.
    buffer = bytearray()
    for chunk in storage.open_stream(key):
        buffer.extend(chunk)
    return bytes(buffer)


async def _extract(sessions: Sessions, storage: FileStorage, claim: _Claim) -> tuple[str, str]:
    async with sessions() as session:
        model = SOURCE_MODELS[claim.source_type]
        row = await file_repository.get_for_user(session, model, claim.user_id, claim.source_id)
    if row is None:
        raise ExtractionError("source_missing", "The uploaded file no longer exists.")
    extractor = extractor_for(row.content_type)
    try:
        data = await asyncio.to_thread(_read_all, storage, row.storage_key)
    except (StoredFileMissing, InvalidStorageKey):
        raise ExtractionError("file_missing", "The stored file is no longer available.") from None
    raw = await asyncio.to_thread(extractor.extract, data)
    extracted = normalize_text(raw)
    if len(extracted) > MAX_TEXT_CHARS:
        raise ExtractionError("text_too_large", "The file contains too much text to process.")
    if is_effectively_empty(extracted):
        raise ExtractionError(
            "no_text", "No extractable text was found (for example, a scanned document)."
        )
    return extracted, extractor.name


async def _complete_extract(
    sessions: Sessions, claim: _Claim, extracted: str, extractor: str
) -> uuid.UUID | None:
    """Store text, mark the extract job completed and (re)queue the embed job: ONE transaction.

    Returns the embed job id, or None if the job was deleted/recovered meanwhile (nothing stored).
    """
    async with sessions() as session:
        done = await session.execute(
            update(IngestionJob)
            .where(IngestionJob.id == claim.job_id, IngestionJob.status == "processing")
            .values(
                status="completed",
                error_code=None,
                error_message=None,
                locked_at=None,
                finished_at=func.now(),
                updated_at=func.now(),
            )
            .returning(IngestionJob.id)
        )
        if done.scalar_one_or_none() is None:
            await session.rollback()  # job deleted/recovered meanwhile: store nothing
            return None
        upsert = insert(ExtractedText).values(
            user_id=claim.user_id,
            source_type=claim.source_type,
            source_id=claim.source_id,
            text=extracted,
            char_count=len(extracted),
            text_sha256=hashlib.sha256(extracted.encode("utf-8")).hexdigest(),
            extractor=extractor,
        )
        await session.execute(
            upsert.on_conflict_do_update(
                constraint="uq_extracted_texts_source",
                set_={
                    "text": upsert.excluded.text,
                    "char_count": upsert.excluded.char_count,
                    "text_sha256": upsert.excluded.text_sha256,
                    "extractor": upsert.excluded.extractor,
                    "updated_at": func.now(),
                },
            )
        )
        embed = insert(IngestionJob).values(
            user_id=claim.user_id,
            source_type=claim.source_type,
            source_id=claim.source_id,
            stage="embed",
            status="pending",
            attempt=0,
        )
        embed_id = (
            await session.execute(
                embed.on_conflict_do_update(
                    constraint="uq_ingestion_jobs_source",
                    set_={
                        "status": "pending",
                        "attempt": 0,
                        "error_code": None,
                        "error_message": None,
                        "locked_at": None,
                        "started_at": None,
                        "finished_at": None,
                        "updated_at": func.now(),
                    },
                ).returning(IngestionJob.id)
            )
        ).scalar_one()
        await session.commit()
    return embed_id


async def _fail(sessions: Sessions, job_id: uuid.UUID, code: str, message: str) -> None:
    async with sessions() as session:
        await session.execute(
            update(IngestionJob)
            .where(IngestionJob.id == job_id, IngestionJob.status == "processing")
            .values(
                status="failed",
                error_code=code,
                error_message=message,
                locked_at=None,
                finished_at=func.now(),
                updated_at=func.now(),
            )
        )
        await session.commit()


async def _retry_later(sessions: Sessions, job_id: uuid.UUID, code: str, message: str) -> None:
    """Transient failure with attempts left: back to `pending`; the recovery sweep re-sends it."""
    async with sessions() as session:
        await session.execute(
            update(IngestionJob)
            .where(IngestionJob.id == job_id, IngestionJob.status == "processing")
            .values(
                status="pending",
                error_code=code,
                error_message=message,
                locked_at=None,
                updated_at=func.now(),
            )
        )
        await session.commit()


# ------------------------------------------------------------------- embed stage


def _embed_all(embedder: EmbeddingProvider, texts: list[str]) -> list[list[float]]:
    vectors = embedder.embed_documents(texts)
    if len(vectors) != len(texts) or any(len(v) != EMBEDDING_DIMENSION for v in vectors):
        raise EmbeddingError(
            "embedding_dimension_mismatch", "The embedding model returned unexpected output."
        )
    return vectors


async def _run_embed_stage(
    sessions: Sessions, claim: _Claim, embedder: EmbeddingProvider, chunk_config: ChunkConfig
) -> bool:
    """Chunk + embed + persist. Returns False if the work was discarded (job gone/superseded)."""
    async with sessions() as session:
        stored = await ingestion_repository.get_extracted_text(
            session, claim.user_id, claim.source_type, claim.source_id
        )
    if stored is None:
        raise StageError("text_missing", "The extracted text is no longer available.")
    chunks = chunk_text(stored.text, chunk_config)
    if not chunks:
        raise StageError("no_chunks", "The text could not be split into searchable parts.")
    if len(chunks) > MAX_CHUNKS_PER_FILE:
        raise StageError("text_too_large", "The file contains too much text to index.")

    async with sessions() as session:
        unchanged = await chunk_store.is_indexed(
            session, claim.user_id, claim.source_type, claim.source_id, embedder.model_id, chunks
        )
    vectors: list[list[float]] | None = None
    if not unchanged:  # an unchanged, already-embedded file is never embedded again
        vectors = await asyncio.to_thread(_embed_all, embedder, [c.text for c in chunks])

    async with sessions() as session:
        done = await session.execute(
            update(IngestionJob)
            .where(IngestionJob.id == claim.job_id, IngestionJob.status == "processing")
            .values(
                status="completed",
                error_code=None,
                error_message=None,
                locked_at=None,
                finished_at=func.now(),
                updated_at=func.now(),
            )
            .returning(IngestionJob.id)
        )
        if done.scalar_one_or_none() is None:
            await session.rollback()  # deleted or superseded while embedding: write nothing
            return False
        current = (
            await session.execute(
                select(ExtractedText.text_sha256).where(
                    ExtractedText.user_id == claim.user_id,
                    ExtractedText.source_type == claim.source_type,
                    ExtractedText.source_id == claim.source_id,
                )
            )
        ).scalar_one_or_none()
        if current != stored.text_sha256:
            await session.rollback()  # the text changed while we worked: our chunks are stale
            return False
        if vectors is not None:
            await chunk_store.replace_chunks(
                session,
                claim.user_id,
                claim.source_type,
                claim.source_id,
                embedder.model_id,
                chunks,
                vectors,
            )
        await session.commit()
    return True


async def process_job(
    sessions: Sessions,
    storage: FileStorage,
    job_id: uuid.UUID,
    *,
    embedder: EmbeddingProvider,
    dispatcher: JobDispatcher | None = None,
    chunk_config: ChunkConfig | None = None,
    max_attempts: int = 3,
) -> str:
    """Run one job (either stage). Returns 'skipped' | 'completed' | 'failed' | 'discarded' |
    'retry_later'.

    Safe to call repeatedly or concurrently for the same job: only the claimant does the work.
    """
    claim = await _claim(sessions, job_id)
    if claim is None:
        return "skipped"
    ids = {
        "job_id": str(job_id),
        "user_id": str(claim.user_id),
        "source": claim.source_type,
        "stage": claim.stage,
    }
    try:
        if claim.stage == "embed":
            stored = await _run_embed_stage(
                sessions, claim, embedder, chunk_config or ChunkConfig()
            )
        else:
            extracted, extractor = await _extract(sessions, storage, claim)
            embed_job_id = await _complete_extract(sessions, claim, extracted, extractor)
            stored = embed_job_id is not None
            if embed_job_id is not None and dispatcher is not None:
                await dispatch_best_effort(dispatcher, embed_job_id)  # after the commit
    except EmbeddingUnavailable:
        if claim.attempt < max_attempts:
            await _retry_later(sessions, job_id, *UNAVAILABLE_RETRYING)
            logger.warning(
                "embedding unavailable; will retry", extra={**ids, "attempt": claim.attempt}
            )
            return "retry_later"
        await _fail(sessions, job_id, *UNAVAILABLE_FINAL)
        return "failed"
    except (ExtractionError, StageError, EmbeddingError) as exc:
        await _fail(sessions, job_id, exc.code, exc.message)
        logger.info("ingestion failed", extra={**ids, "error_code": exc.code})
        return "failed"
    except Exception:
        logger.exception("ingestion crashed", extra=ids)  # traceback in logs only, never stored
        await _fail(sessions, job_id, *INTERNAL_ERROR)
        return "failed"
    logger.info("ingestion %s", "completed" if stored else "discarded", extra=ids)
    return "completed" if stored else "discarded"


# ------------------------------------------------------------- user-triggered retry


async def reset_failed_jobs(
    session: AsyncSession, user_id: uuid.UUID, source_type: str, source_id: uuid.UUID
) -> list[uuid.UUID]:
    """failed -> pending (attempts reset) for this file's failed job(s); returns their ids.

    Conditional on status, so double-clicking retry cannot create duplicate processing. Retrying
    a failed `embed` job does not re-run extraction.
    """
    stmt = (
        update(IngestionJob)
        .where(
            IngestionJob.user_id == user_id,
            IngestionJob.source_type == source_type,
            IngestionJob.source_id == source_id,
            IngestionJob.status == "failed",
        )
        .values(
            status="pending",
            attempt=0,
            error_code=None,
            error_message=None,
            locked_at=None,
            finished_at=None,
            updated_at=func.now(),
        )
        .returning(IngestionJob.id)
    )
    job_ids = list((await session.execute(stmt)).scalars())
    await session.commit()
    return job_ids


async def enqueue_missing_embeddings(
    session: AsyncSession, embedding_model: str
) -> list[uuid.UUID]:
    """Queue `embed` jobs for every extracted text lacking vectors for `embedding_model`.

    Used when the embedding model changes (re-embedding). Skips sources whose embed job is already
    pending/processing. Returns the job ids to dispatch.
    """
    rows = (
        await session.execute(
            text(
                "INSERT INTO ingestion_jobs (user_id, source_type, source_id, stage) "
                "SELECT t.user_id, t.source_type, t.source_id, 'embed' FROM extracted_texts t "
                "WHERE NOT EXISTS ("
                "  SELECT 1 FROM chunks c JOIN chunk_embeddings e ON e.chunk_id = c.id "
                "  WHERE c.user_id = t.user_id AND c.source_type = t.source_type "
                "    AND c.source_id = t.source_id AND e.embedding_model = :model) "
                "ON CONFLICT ON CONSTRAINT uq_ingestion_jobs_source DO UPDATE SET "
                "  status = 'pending', attempt = 0, error_code = NULL, error_message = NULL, "
                "  locked_at = NULL, started_at = NULL, finished_at = NULL, updated_at = now() "
                "WHERE ingestion_jobs.status IN ('completed', 'failed') "
                "RETURNING id"
            ),
            {"model": embedding_model},
        )
    ).all()
    await session.commit()
    return [r.id for r in rows]


# ------------------------------------------------------------------------ recovery


@dataclass
class RecoveryReport:
    failed: list[uuid.UUID] = field(default_factory=list)  # stuck, attempts exhausted
    requeued: list[uuid.UUID] = field(default_factory=list)  # stuck, reset to pending
    stale_pending: list[uuid.UUID] = field(default_factory=list)  # never delivered
    dispatched: list[uuid.UUID] = field(default_factory=list)
    dispatch_failed: list[uuid.UUID] = field(default_factory=list)


async def recover_stale_jobs(
    sessions: Sessions,
    dispatcher: JobDispatcher,
    *,
    max_attempts: int,
    processing_timeout_seconds: int,
    pending_redispatch_seconds: int,
) -> RecoveryReport:
    """Deterministic reconciliation sweep (run periodically, e.g. every minute, or on demand).

    Applies to jobs of every stage:
    1. `processing` longer than the timeout (worker crashed/hung):
       attempts exhausted -> `failed` (code `timeout`); otherwise back to `pending`.
    2. `pending` longer than the redispatch age (queue message lost, dispatch failed, or a
       transient embedding error waiting for its retry): re-send.
    Jobs are handled oldest first. Re-dispatching is harmless: workers claim atomically.
    """
    report = RecoveryReport()
    processing_cutoff = func.now() - timedelta(seconds=processing_timeout_seconds)
    pending_cutoff = func.now() - timedelta(seconds=pending_redispatch_seconds)

    async with sessions() as session:
        stale_pending = (
            await session.execute(
                select(IngestionJob.created_at, IngestionJob.id).where(
                    IngestionJob.status == "pending", IngestionJob.updated_at < pending_cutoff
                )
            )
        ).all()
        stuck = IngestionJob.status == "processing", IngestionJob.locked_at < processing_cutoff

        failed = await session.execute(
            update(IngestionJob)
            .where(*stuck, IngestionJob.attempt >= max_attempts)
            .values(
                status="failed",
                error_code=TIMEOUT_ERROR[0],
                error_message=TIMEOUT_ERROR[1],
                locked_at=None,
                finished_at=func.now(),
                updated_at=func.now(),
            )
            .returning(IngestionJob.id)
        )
        report.failed = sorted(failed.scalars())

        requeued = await session.execute(
            update(IngestionJob)
            .where(*stuck, IngestionJob.attempt < max_attempts)
            .values(status="pending", locked_at=None, updated_at=func.now())
            .returning(IngestionJob.created_at, IngestionJob.id)
        )
        requeued_rows = requeued.all()
        await session.commit()

    report.stale_pending = sorted(row.id for row in stale_pending)
    report.requeued = sorted(row.id for row in requeued_rows)
    # Oldest first, de-duplicated: deterministic order for a given database state.
    candidates = {row.id: row.created_at for row in [*stale_pending, *requeued_rows]}
    to_dispatch = sorted(candidates, key=lambda job_id: (candidates[job_id], job_id))

    for job_id in to_dispatch:
        if await dispatch_best_effort(dispatcher, job_id):
            report.dispatched.append(job_id)
        else:
            report.dispatch_failed.append(job_id)

    if report.dispatched:
        async with sessions() as session:
            # Restart the pending clock so the next sweep does not immediately re-send.
            await session.execute(
                update(IngestionJob)
                .where(IngestionJob.id.in_(report.dispatched), IngestionJob.status == "pending")
                .values(updated_at=func.now())
            )
            await session.commit()

    logger.info(
        "ingestion recovery sweep",
        extra={
            "failed": len(report.failed),
            "requeued": len(report.requeued),
            "stale_pending": len(report.stale_pending),
            "dispatched": len(report.dispatched),
            "dispatch_failed": len(report.dispatch_failed),
        },
    )
    return report


async def find_stuck_jobs(
    session: AsyncSession, *, processing_timeout_seconds: int, pending_redispatch_seconds: int
) -> list[IngestionJob]:
    """Identify jobs the recovery sweep would act on, without changing anything."""
    processing_cutoff = func.now() - timedelta(seconds=processing_timeout_seconds)
    pending_cutoff = func.now() - timedelta(seconds=pending_redispatch_seconds)
    stmt = (
        select(IngestionJob)
        .where(
            ((IngestionJob.status == "processing") & (IngestionJob.locked_at < processing_cutoff))
            | ((IngestionJob.status == "pending") & (IngestionJob.updated_at < pending_cutoff))
        )
        .order_by(IngestionJob.created_at, IngestionJob.id)
    )
    return list((await session.execute(stmt)).scalars())
