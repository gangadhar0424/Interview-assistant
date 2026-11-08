"""POST /api/v1/sessions/job-import (authenticated, rate limited).

This router MUST be included before the sessions router: `/sessions/{session_id}` would otherwise
treat "job-import" as an id (tests/test_job_import_api.py pins the order).
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.auth.deps import CurrentUser
from app.jobs_import.schemas import JobImportRequest, JobImportResponse
from app.jobs_import.service import JobImporter

router = APIRouter(prefix="/sessions", tags=["sessions"])


def get_importer(request: Request) -> JobImporter:
    importer: JobImporter = request.app.state.job_importer
    return importer


ImporterDep = Annotated[JobImporter, Depends(get_importer)]


@router.post("/job-import", response_model=JobImportResponse)
async def job_import(
    body: JobImportRequest, current: CurrentUser, importer: ImporterDep
) -> JobImportResponse:
    # Only the authenticated user's id is used (for rate limiting and logs); nothing is stored.
    return await importer.import_job(current.id, body.url)
