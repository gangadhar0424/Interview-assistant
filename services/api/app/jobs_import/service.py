"""Job import use case: rate limit, fetch, extract, and map every outcome to a safe message.

Failures never raise to the caller (the user must always be able to continue manually); only
rate/concurrency limits are errors (429). Every network-level failure (blocked address, DNS,
connect, TLS, timeout, redirects, HTTP errors) produces the SAME message, so the endpoint cannot
be used to probe internal hosts or ports. URLs and page content are never logged.
"""

import asyncio
import logging
import uuid
from typing import Protocol

from app.core.errors import AppError
from app.jobs_import import errors
from app.jobs_import.extract import JobInfo, extract_job
from app.jobs_import.fetch import FetchedPage
from app.jobs_import.limiter import SlidingWindowLimiter
from app.jobs_import.schemas import JobImportResponse

logger = logging.getLogger(__name__)

MESSAGES = {
    "succeeded": "Job details imported. Please review them before continuing.",
    "partial": "Some details were found. Please check them and fill in the rest manually.",
    "no_job_info": "No job details could be found on that page. Please enter them manually.",
    errors.INVALID_URL: "That does not look like a valid web link. Please enter details manually.",
    errors.CONTENT: "That link is not an importable web page. Please enter the details manually.",
    errors.TOO_LARGE: "That page is too large to import. Please enter the details manually.",
    # Everything else (blocked, DNS, connect, TLS, timeout, redirects, HTTP errors) is identical.
    "unreachable": "The page could not be retrieved. Please enter the details manually.",
}
_SPECIFIC_REASONS = {errors.INVALID_URL, errors.CONTENT, errors.TOO_LARGE, errors.NO_INFO}


class PageFetcher(Protocol):
    def fetch(self, url: str) -> FetchedPage: ...


def _failed(reason: str) -> JobImportResponse:
    key = reason if reason in _SPECIFIC_REASONS else "unreachable"
    if reason == errors.NO_INFO:
        key = "no_job_info"
    return JobImportResponse(status="failed", message=MESSAGES[key])


def _from_info(info: JobInfo) -> JobImportResponse:
    found = [info.company, info.title, info.description]
    if not any(found):
        return _failed(errors.NO_INFO)
    complete = info.structured and all(found)  # a guess from a generic page is never "succeeded"
    return JobImportResponse(
        status="succeeded" if complete else "partial",
        company=info.company,
        title=info.title,
        description=info.description,
        message=MESSAGES["succeeded" if complete else "partial"],
    )


class JobImporter:
    def __init__(
        self,
        fetcher: PageFetcher,
        limiter: SlidingWindowLimiter,
        *,
        max_description_chars: int,
        max_concurrent: int,
        overall_timeout: float,
    ) -> None:
        self._fetcher = fetcher
        self._limiter = limiter
        self._max_chars = max_description_chars
        self._max_concurrent = max_concurrent
        self._overall_timeout = overall_timeout
        self._active = 0

    async def import_job(self, user_id: uuid.UUID, url: str) -> JobImportResponse:
        key = str(user_id)  # the authenticated user, never a request value
        if not self._limiter.allow(key):
            raise AppError(
                429,
                "rate_limited",
                "Too many import attempts. Please wait a minute or enter the details manually.",
                headers={"Retry-After": str(self._limiter.retry_after(key))},
            )
        if self._active >= self._max_concurrent:
            raise AppError(
                429,
                "import_busy",
                "The importer is busy. Please try again shortly or enter the details manually.",
                headers={"Retry-After": "5"},
            )
        self._active += 1
        try:
            return await self._run(user_id, url)
        finally:
            self._active -= 1

    async def _run(self, user_id: uuid.UUID, url: str) -> JobImportResponse:
        reason: str | None = None
        result: JobImportResponse | None = None
        try:
            async with asyncio.timeout(self._overall_timeout + 2):
                page = await asyncio.to_thread(self._fetcher.fetch, url)
                info = await asyncio.to_thread(
                    extract_job, page.body, page.charset, self._max_chars
                )
            result = _from_info(info)
            if result.status == "failed":
                reason = errors.NO_INFO
        except errors.ImportFailure as failure:
            reason = failure.reason
        except TimeoutError:
            reason = errors.TIMEOUT
        except Exception as exc:  # noqa: BLE001 - nothing may escape and break manual entry
            reason = errors.INTERNAL
            logger.warning("job import crashed", extra={"error_type": type(exc).__name__})
        if result is None:
            result = _failed(reason or errors.INTERNAL)
        logger.info(
            "job import finished",
            extra={"user_id": str(user_id), "status": result.status, "reason": reason},
        )
        return result
