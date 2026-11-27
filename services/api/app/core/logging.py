"""Structured (JSON) logging. Never log secrets or user content; log IDs only."""

import json
import logging
import re
import sys
import traceback
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

request_id_ctx: ContextVar[str | None] = ContextVar("request_id", default=None)

_RESERVED = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = request_id_ctx.get()
        if request_id:
            payload["request_id"] = request_id
        for key, value in record.__dict__.items():
            if key not in _RESERVED:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = format_exception_without_messages(record.exc_info)
        return json.dumps(payload, default=str)


_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")


def accepted_request_id(supplied: str | None) -> str:
    """A client-supplied X-Request-ID only if it is short and plain; otherwise a fresh one.

    Stops a caller from putting an unbounded or odd string into every log line and response header.
    """
    if supplied is not None and _REQUEST_ID.fullmatch(supplied):
        return supplied
    return uuid.uuid4().hex


def format_exception_without_messages(exc_info: Any) -> str:
    """Exception class names (the whole cause chain) and stack frames, NEVER an exception message.

    Messages from the database driver, parsers or providers can carry parameter values, document or
    transcript text. A frame line shows only source code, so the traceback stays useful.
    """
    exc = exc_info[1] if isinstance(exc_info, tuple) else None
    if not isinstance(exc, BaseException):
        return "unknown exception"
    parts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen and len(parts) < 8:
        seen.add(id(current))
        frames = "".join(traceback.format_tb(current.__traceback__))
        parts.append(f"{frames}{type(current).__module__}.{type(current).__qualname__}")
        implicit = None if current.__suppress_context__ else current.__context__
        current = current.__cause__ or implicit
    return "\n-- caused by --\n".join(reversed(parts))


_TICKET_PARAM = re.compile(r"""(ticket=)[^\s&"']+""", re.IGNORECASE)
# Loggers that print the WebSocket request target (path + query string), which carries the ticket.
_TICKET_LOGGERS = ("uvicorn", "uvicorn.access", "uvicorn.error", "websockets", "wsproto")


def redact_tickets(text: str) -> str:
    return _TICKET_PARAM.sub(r"\1[redacted]", text)


class TicketRedactionFilter(logging.Filter):
    """Removes a WebSocket ticket (`?ticket=...`) from a log record before any handler sees it.

    The ticket is single-use and short-lived, but it must still never reach a log (D-41).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # Format first, then redact the finished text: redacting the template alone could swallow
        # a `%s` placeholder and break the line, and the ticket may arrive as an argument.
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a malformed log call must not be made worse here
            return True
        redacted = redact_tickets(message)
        if redacted != message:
            record.msg = redacted
            record.args = None
        return True


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(TicketRedactionFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # uvicorn configures its own handlers (propagate=False) before the app is created, so the
    # filter is attached to those loggers (idempotent) and to their handlers as well.
    for name in _TICKET_LOGGERS:
        logger = logging.getLogger(name)
        if not any(isinstance(f, TicketRedactionFilter) for f in logger.filters):
            logger.addFilter(TicketRedactionFilter())
        for h in logger.handlers:
            if not any(isinstance(f, TicketRedactionFilter) for f in h.filters):
                h.addFilter(TicketRedactionFilter())
