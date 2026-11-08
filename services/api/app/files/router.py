"""Authenticated resume/document endpoints (no public file URLs; downloads stream via the API)."""

import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import UploadFile

from app.auth.deps import CurrentUser
from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.db.models import IngestionJob
from app.db.session import get_session
from app.files import service
from app.files.kinds import FileKind, document_kind, resume_kind
from app.files.repository import FileRow
from app.ingestion.dispatch import JobDispatcher
from app.ingestion.repository import JobsByStage
from app.storage.base import FileStorage

SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def get_storage(request: Request) -> FileStorage:
    storage: FileStorage = request.app.state.storage
    return storage


StorageDep = Annotated[FileStorage, Depends(get_storage)]


def get_dispatcher(request: Request) -> JobDispatcher:
    dispatcher: JobDispatcher = request.app.state.dispatcher
    return dispatcher


DispatcherDep = Annotated[JobDispatcher, Depends(get_dispatcher)]


class IngestionOut(BaseModel):
    """Ingestion status. `error_*` are fixed safe strings; never traces, content or paths."""

    status: str  # pending | processing | completed | failed
    error_code: str | None
    error_message: str | None
    updated_at: datetime


class FileOut(BaseModel):
    """Public file metadata. Deliberately excludes storage keys, hashes and any paths."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    filename: str
    content_type: str
    size_bytes: int
    created_at: datetime
    # Text extraction stage and chunk/embedding (search indexing) stage. A file is usable for
    # retrieval when `indexing.status == "completed"`.
    ingestion: IngestionOut | None
    indexing: IngestionOut | None


class FileList(BaseModel):
    items: list[FileOut]


def _job_out(job: IngestionJob | None) -> IngestionOut | None:
    if job is None:
        return None
    return IngestionOut(
        status=job.status,
        error_code=job.error_code,
        error_message=job.error_message,
        updated_at=job.updated_at,
    )


def _to_out(row: FileRow, jobs: JobsByStage) -> FileOut:
    return FileOut(
        id=row.id,
        filename=row.original_filename,
        content_type=row.content_type,
        size_bytes=row.size_bytes,
        created_at=row.created_at,
        ingestion=_job_out(jobs.get("extract")),
        indexing=_job_out(jobs.get("embed")),
    )


def _content_disposition(filename: str) -> str:
    fallback = (
        "".join(c if c.isascii() and c not in '"\\;' else "_" for c in filename) or "download"
    )
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename, safe='')}"


def build_router(path: str, kind_factory: Callable[[Settings], FileKind]) -> APIRouter:
    router = APIRouter(prefix=path, tags=[path.strip("/")])

    @router.post("", status_code=status.HTTP_201_CREATED, response_model=FileOut)
    async def upload(
        request: Request,
        current: CurrentUser,  # authentication runs before the request body is read
        session: SessionDep,
        storage: StorageDep,
        dispatcher: DispatcherDep,
        settings: SettingsDep,
    ) -> FileOut:
        kind = kind_factory(settings)
        form = await request.form(max_files=1, max_fields=4)
        try:
            file = form.get("file")
            if not isinstance(file, UploadFile):
                raise AppError(422, "file_required", "A file must be uploaded in the 'file' field.")
            row, jobs = await service.create_file(
                session, storage, dispatcher, current.id, kind, file
            )
        finally:
            await form.close()
        return _to_out(row, jobs)

    @router.get("", response_model=FileList)
    async def list_files(
        current: CurrentUser,
        session: SessionDep,
        settings: SettingsDep,
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
    ) -> FileList:
        pairs = await service.list_files(session, current.id, kind_factory(settings), limit)
        return FileList(items=[_to_out(row, jobs) for row, jobs in pairs])

    @router.get("/{file_id}", response_model=FileOut)
    async def details(
        file_id: uuid.UUID, current: CurrentUser, session: SessionDep, settings: SettingsDep
    ) -> FileOut:
        row, jobs = await service.get_file(session, current.id, kind_factory(settings), file_id)
        return _to_out(row, jobs)

    @router.post("/{file_id}/retry", status_code=status.HTTP_202_ACCEPTED, response_model=FileOut)
    async def retry(
        file_id: uuid.UUID,
        current: CurrentUser,
        session: SessionDep,
        dispatcher: DispatcherDep,
        settings: SettingsDep,
    ) -> FileOut:
        row, jobs = await service.retry_ingestion(
            session, dispatcher, current.id, kind_factory(settings), file_id
        )
        return _to_out(row, jobs)

    @router.get("/{file_id}/file")
    async def download(
        file_id: uuid.UUID,
        current: CurrentUser,
        session: SessionDep,
        storage: StorageDep,
        settings: SettingsDep,
    ) -> StreamingResponse:
        row, stream = await service.open_for_download(
            session, storage, current.id, kind_factory(settings), file_id
        )
        return StreamingResponse(
            stream,
            media_type=row.content_type,
            headers={
                "Content-Disposition": _content_disposition(row.original_filename),
                "Content-Length": str(row.size_bytes),
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @router.delete("/{file_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete(
        file_id: uuid.UUID,
        current: CurrentUser,
        session: SessionDep,
        storage: StorageDep,
        settings: SettingsDep,
    ) -> Response:
        await service.delete_file(session, storage, current.id, kind_factory(settings), file_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router


resumes_router = build_router("/resumes", resume_kind)
documents_router = build_router("/documents", document_kind)
