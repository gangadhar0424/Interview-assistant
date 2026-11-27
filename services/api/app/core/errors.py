"""Uniform error envelope: {"error": {"code", "message", "request_id", "details"?}}."""

import errno
import logging
import socket
from typing import Any, cast

import redis.exceptions as redis_exc
import sqlalchemy.exc as sa_exc
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.ai.errors import LLMError
from app.core.config import get_settings
from app.core.logging import request_id_ctx

logger = logging.getLogger(__name__)

REFRESH_COOKIE_NAME = "refresh_token"
REFRESH_COOKIE_PATH = "/api/v1/auth"


class AppError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        headers: dict[str, str] | None = None,
        clear_refresh_cookie: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers = headers or {}
        self.clear_refresh_cookie = clear_refresh_cookie


def _body(code: str, message: str, details: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message, "request_id": request_id_ctx.get()}
    if details is not None:
        error["details"] = details
    return {"error": error}


def delete_refresh_cookie(response: JSONResponse) -> None:
    settings = get_settings()
    response.delete_cookie(
        REFRESH_COOKIE_NAME,
        path=REFRESH_COOKIE_PATH,
        secure=settings.cookie_secure,
        httponly=True,
        samesite="lax",
    )


async def _app_error_handler(_: Request, exc: Exception) -> JSONResponse:
    exc = cast(AppError, exc)
    response = JSONResponse(
        status_code=exc.status_code,
        content=_body(exc.code, exc.message),
        headers=exc.headers,
    )
    if exc.clear_refresh_cookie:
        delete_refresh_cookie(response)
    return response


async def _validation_error_handler(_: Request, exc: Exception) -> JSONResponse:
    exc = cast(RequestValidationError, exc)
    # FastAPI's default 422 body echoes the submitted input (e.g. passwords). Never do that.
    details = [
        {"field": ".".join(str(p) for p in err["loc"] if p != "body"), "message": err["msg"]}
        for err in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content=_body("validation_error", "The request is invalid.", details),
    )


_LLM_STATUS = {
    "llm_rate_limited": 429,
    "llm_quota_exhausted": 503,
    "llm_timeout": 504,
    "llm_unavailable": 503,
    "llm_content_filtered": 422,
    "invalid_provider_selection": 422,
}


async def _llm_error_handler(_: Request, exc: Exception) -> JSONResponse:
    """Provider failures surface only as fixed, safe messages (never provider details)."""
    exc = cast(LLMError, exc)
    return JSONResponse(
        status_code=_LLM_STATUS.get(exc.code, 502), content=_body(exc.code, exc.message)
    )


# PostgreSQL SQLSTATEs for "busy, restarting or out of capacity" (not "bad query").
_RETRYABLE_CLASSES = frozenset({"08", "53"})  # connection exceptions; insufficient resources
_RETRYABLE_SQLSTATES = frozenset(
    {
        "57014",  # query_canceled (statement timeout)
        "57P01",  # admin_shutdown
        "57P02",  # crash_shutdown
        "57P03",  # cannot_connect_now
        "40001",  # serialization_failure
        "40P01",  # deadlock_detected
        "55P03",  # lock_not_available
        "25P03",  # idle_in_transaction_session_timeout
    }
)


# OS-level network failures: the peer or the route is gone (not a missing file or a permission).
_NETWORK_ERRNOS = frozenset(
    {
        errno.ECONNREFUSED,
        errno.ECONNRESET,
        errno.ECONNABORTED,
        errno.ENETUNREACH,
        errno.ENETDOWN,
        errno.EHOSTUNREACH,
        errno.ETIMEDOUT,
        errno.EPIPE,
    }
)


def _sqlstate(exc: sa_exc.DBAPIError) -> str | None:
    """The SQLSTATE of the underlying driver error, whichever attribute the driver exposes."""
    seen: BaseException | None = exc.orig
    for _ in range(3):  # the adapter may wrap the driver's own exception
        if seen is None:
            return None
        for attribute in ("sqlstate", "pgcode"):
            value = getattr(seen, attribute, None)
            if isinstance(value, str) and len(value) == 5:
                return value
        seen = seen.__cause__
    return None


def is_infrastructure_failure(exc: BaseException) -> bool:
    """A dependency (database, Redis, network) is unreachable, slow or exhausted.

    These are the user's "try again shortly", not a bug in the request, so they map to 503. A
    constraint violation, a missing file or a programming error is NOT here: those stay 500s.
    """
    if isinstance(exc, (sa_exc.OperationalError, sa_exc.InterfaceError, sa_exc.TimeoutError)):
        return True  # includes pool exhaustion (TimeoutError) and a statement timeout
    if isinstance(exc, sa_exc.DBAPIError):
        if exc.connection_invalidated:
            return True
        state = _sqlstate(exc)
        if state is not None and (state in _RETRYABLE_SQLSTATES or state[:2] in _RETRYABLE_CLASSES):
            return True
    if isinstance(exc, (redis_exc.ConnectionError, redis_exc.TimeoutError, ConnectionError)):
        return True
    if isinstance(exc, (socket.gaierror, socket.herror)):
        return True  # DNS failed: the database or Redis host no longer resolves (found in a drill)
    if isinstance(exc, OSError) and exc.errno in _NETWORK_ERRNOS:
        return True
    return isinstance(exc, TimeoutError)


def unhandled_error_response(exc: Exception) -> JSONResponse:
    """The last line of defence: a fixed JSON body, never a stack trace, SQL or a driver message.

    Only the exception class name is logged (SQL parameters in driver messages can hold user text).
    """
    infrastructure = is_infrastructure_failure(exc)
    logger.error(
        "unhandled error",
        extra={"error_type": type(exc).__name__, "infrastructure": infrastructure},
    )
    if infrastructure:
        return JSONResponse(
            status_code=503,
            content=_body(
                "service_unavailable", "The service is temporarily unavailable. Please try again."
            ),
            headers={"Retry-After": "5"},
        )
    return JSONResponse(
        status_code=500,
        content=_body("internal_error", "Something went wrong. Please try again later."),
    )


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(LLMError, _llm_error_handler)
    app.add_exception_handler(AppError, _app_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
