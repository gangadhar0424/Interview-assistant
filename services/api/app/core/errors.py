"""Uniform error envelope: {"error": {"code", "message", "request_id", "details"?}}."""

from typing import Any, cast

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.ai.errors import LLMError
from app.core.config import get_settings
from app.core.logging import request_id_ctx

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


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(LLMError, _llm_error_handler)
    app.add_exception_handler(AppError, _app_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
