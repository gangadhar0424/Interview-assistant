"""Validation of the resume/documents a session selects (owner decisions 1-4, D-35).

Order of checks is deliberate so nothing leaks:
1. input-only checks (duplicates, count): reveal nothing about the database;
2. existence and ownership of EVERY selected id, in one user-scoped query: a foreign id and a
   non-existent id are indistinguishable (same 404);
3. readiness (extraction AND indexing completed), reached only for ids the user owns.
Rows are read with FOR SHARE so a concurrent file deletion cannot slip in between the check and
the insert. Extracted text is never read here.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.db.models import Document, Resume
from app.ingestion import repository as ingestion_repository
from app.ingestion.repository import JobsByStage


@dataclass(frozen=True)
class Materials:
    resume: Resume | None  # None = the caller did not ask to (re)select a resume
    documents: list[Document] | None  # None = not (re)selected; [] = explicitly none


def material_not_found() -> AppError:
    return AppError(404, "material_not_found", "A selected resume or document was not found.")


def is_ready(jobs: JobsByStage) -> bool:
    """Ready = text extracted AND chunked/embedded (the same rule the files API reports)."""
    extract, embed = jobs.get("extract"), jobs.get("embed")
    return (
        extract is not None
        and embed is not None
        and extract.status == "completed"
        and embed.status == "completed"
    )


def _check_document_selection(document_ids: Sequence[uuid.UUID], max_documents: int) -> None:
    if len(set(document_ids)) != len(document_ids):
        raise AppError(422, "duplicate_material", "Each document can only be selected once.")
    if len(document_ids) > max_documents:
        raise AppError(
            422,
            "too_many_documents",
            f"At most {max_documents} documents can be selected for a session.",
        )


async def resolve_materials(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    resume_id: uuid.UUID | None,
    document_ids: Sequence[uuid.UUID] | None,
    max_documents: int,
) -> Materials:
    if document_ids is not None:
        _check_document_selection(document_ids, max_documents)

    resume: Resume | None = None
    if resume_id is not None:
        resume = (
            await session.execute(
                select(Resume)
                .where(Resume.id == resume_id, Resume.user_id == user_id)
                .with_for_update(read=True)
            )
        ).scalar_one_or_none()
        if resume is None:
            raise material_not_found()

    documents: list[Document] | None = None
    if document_ids is not None:
        documents = []
        if document_ids:
            found = (
                (
                    await session.execute(
                        select(Document)
                        .where(Document.user_id == user_id, Document.id.in_(document_ids))
                        .with_for_update(read=True)
                    )
                )
                .scalars()
                .all()
            )
            if len(found) != len(document_ids):
                raise material_not_found()
            by_id = {d.id: d for d in found}
            documents = [by_id[i] for i in document_ids]  # keep the user's selection order

    not_ready = False
    if resume is not None:
        jobs = await ingestion_repository.jobs_for_sources(session, user_id, "resume", [resume.id])
        not_ready = not is_ready(jobs.get(resume.id, {}))
    if documents:
        doc_jobs = await ingestion_repository.jobs_for_sources(
            session, user_id, "document", [d.id for d in documents]
        )
        not_ready = not_ready or any(not is_ready(doc_jobs.get(d.id, {})) for d in documents)
    if not_ready:
        raise AppError(
            422,
            "material_not_ready",
            "A selected file is still being processed or failed. Use only files marked ready.",
        )
    return Materials(resume=resume, documents=documents)
