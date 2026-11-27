"""Regression: the JSON log formatter never writes an exception message (user content risk)."""

import json
import logging

from app.core.logging import JsonFormatter

SECRET = "resume text: Jane Q. Candidate, 12 Example Street"


def _format_failure() -> dict[str, str]:
    try:
        try:
            raise ValueError(SECRET)
        except ValueError as inner:
            raise RuntimeError(f"insert failed for {SECRET}") from inner
    except RuntimeError:
        logger = logging.getLogger("test")
        try:
            raise
        except RuntimeError:
            import sys

            record = logger.makeRecord(
                "test", logging.ERROR, __file__, 1, "ingestion crashed", (), sys.exc_info()
            )
    parsed: dict[str, str] = json.loads(JsonFormatter().format(record))
    return parsed


def test_exception_message_and_cause_message_are_not_logged() -> None:
    payload = _format_failure()
    assert SECRET not in json.dumps(payload)
    assert "Candidate" not in payload["exception"]


def test_class_names_and_frames_are_still_logged() -> None:
    exception = _format_failure()["exception"]
    assert "RuntimeError" in exception and "ValueError" in exception
    assert "_format_failure" in exception


def test_request_id_is_accepted_only_when_short_and_plain() -> None:
    from app.core.logging import accepted_request_id

    assert accepted_request_id("abc-123_X.y") == "abc-123_X.y"
    for bad in ("a" * 65, "has space", "line\r\nbreak", "semi;colon", "", None):
        got = accepted_request_id(bad)
        assert got != bad and len(got) == 32
