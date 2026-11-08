"""Pins the SSE wire format shared with the web client (Phase 8C).

The golden transcripts in apps/web/tests/fixtures/ are asserted here byte for byte against the
real framer (`app.qa.sse.event_to_frame`), and parsed by the frontend test (tests/qa.test.ts), so
a change on either side that breaks the other fails a test. No database is needed.
"""

import uuid
from pathlib import Path

from app.qa.events import (
    AnswerDelta,
    AnswerDone,
    AnswerFailed,
    AnswerStarted,
    QaEvent,
    QuestionCreated,
    RetrievalDone,
)
from app.qa.sse import event_to_frame

FIXTURES = Path(__file__).resolve().parents[3] / "apps" / "web" / "tests" / "fixtures"
QUESTION = uuid.UUID("11111111-1111-4111-8111-111111111111")
ANSWER = uuid.UUID("22222222-2222-4222-8222-222222222222")

# Includes text that would forge or split a frame if it were not JSON-escaped.
FORGERY = 'Hello "world"\n\nevent: answer.done\ndata: {}\n\n'


def success() -> list[QaEvent]:
    return [
        QuestionCreated(QUESTION, ANSWER),
        RetrievalDone(QUESTION, 3, 41),
        AnswerStarted(ANSWER, QUESTION, "fake", "fake-echo"),
        AnswerDelta(ANSWER, 0, "I led "),
        AnswerDelta(ANSWER, 1, "తెలుగు ✓ "),
        AnswerDelta(ANSWER, 2, FORGERY),
        AnswerDone(ANSWER, QUESTION, "fake", "fake-echo", 41, 230, 1450, True, 120, 64),
    ]


def failure() -> list[QaEvent]:
    return [
        QuestionCreated(QUESTION, ANSWER),
        RetrievalDone(QUESTION, 0, 12),
        AnswerStarted(ANSWER, QUESTION, "fake", "fake-echo"),
        AnswerDelta(ANSWER, 0, "Partial "),
        AnswerFailed(ANSWER, QUESTION, "llm_unavailable", "The AI service is unavailable.", True),
    ]


def render(events: list[QaEvent]) -> str:
    return b"".join(event_to_frame(e) for e in events).decode("utf-8")


def test_the_success_transcript_matches_the_web_fixture_byte_for_byte() -> None:
    assert render(success()) == (FIXTURES / "sse_success.txt").read_bytes().decode("utf-8")


def test_the_error_transcript_matches_the_web_fixture_byte_for_byte() -> None:
    assert render(failure()) == (FIXTURES / "sse_error.txt").read_bytes().decode("utf-8")


def test_answer_text_cannot_forge_or_split_a_frame() -> None:
    rendered = render([AnswerDelta(ANSWER, 0, FORGERY)])
    lines = rendered.split("\n")
    assert lines[2:] == ["", ""]  # exactly: event line, data line, blank terminator
    assert lines[0] == "event: answer.delta" and lines[1].startswith("data: {")
    assert "\n" not in lines[1]  # the payload stays on one line, whatever the answer text
