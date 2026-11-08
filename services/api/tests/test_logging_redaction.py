"""Ticket redaction in logs (Phase 9A, D-41). No database needed."""

import io
import logging

import pytest

from app.core.logging import JsonFormatter, TicketRedactionFilter, configure_logging, redact_tickets

TICKET = "k3J9-aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789_A"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (f"/api/v1/ws/sessions/x?ticket={TICKET}", "/api/v1/ws/sessions/x?ticket=[redacted]"),
        (f"/p?a=1&ticket={TICKET}&b=2", "/p?a=1&ticket=[redacted]&b=2"),
        (f'"GET /p?TICKET={TICKET} HTTP/1.1"', '"GET /p?TICKET=[redacted] HTTP/1.1"'),
        (f"url='/p?ticket={TICKET}'", "url='/p?ticket=[redacted]'"),
        ("no secrets here", "no secrets here"),
    ],
)
def test_redact_tickets(text: str, expected: str) -> None:
    assert redact_tickets(text) == expected


def record(msg: str, args: object) -> logging.LogRecord:
    return logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, msg, args, None)  # type: ignore[arg-type]


def test_the_filter_redacts_message_and_arguments_the_way_uvicorn_logs_them() -> None:
    f = TicketRedactionFilter()
    access = record(
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:5000", "GET", f"/api/v1/ws/sessions/s?ticket={TICKET}", "1.1", 403),
    )
    assert f.filter(access) is True
    assert TICKET not in access.getMessage() and "ticket=[redacted]" in access.getMessage()

    accepted = record('%s - "WebSocket %s" [accepted]', ("1.2.3.4", f"/ws?ticket={TICKET}"))
    f.filter(accepted)
    assert TICKET not in accepted.getMessage()

    literal = record(f"GET /ws?ticket={TICKET}", None)
    f.filter(literal)
    assert TICKET not in literal.getMessage()

    named = record("%(path)s", None)
    named.args = {"path": f"/ws?ticket={TICKET}"}
    f.filter(named)
    assert TICKET not in named.getMessage()


def test_configure_logging_protects_the_application_handler_and_uvicorn_loggers() -> None:
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    try:
        configure_logging("INFO")
        (handler,) = root.handlers
        stream = io.StringIO()
        handler.setStream(stream)  # type: ignore[attr-defined]
        logging.getLogger("app.test").info("connect /ws?ticket=%s", TICKET)
        assert TICKET not in stream.getvalue() and "ticket=[redacted]" in stream.getvalue()
        for name in ("uvicorn.access", "uvicorn.error", "websockets"):
            assert any(
                isinstance(f, TicketRedactionFilter) for f in logging.getLogger(name).filters
            ), name
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


def test_the_json_formatter_output_is_also_clean() -> None:
    rec = record("%s", (f"/ws?ticket={TICKET}",))
    TicketRedactionFilter().filter(rec)
    assert TICKET not in JsonFormatter().format(rec)
