"""Server-Sent Events framing and the streaming response for the question endpoint.

Wire format: each event is `event: <name>\\ndata: <one-line JSON>\\n\\n` (UTF-8). JSON escapes every
newline, so a frame can never be split or forged by answer text. Event names follow docs/API.md:
question.created, retrieval.done, answer.started, answer.delta, answer.done, answer.error.
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from app.qa.events import (
    AnswerDelta,
    AnswerDone,
    AnswerFailed,
    AnswerStarted,
    QaEvent,
    QuestionCreated,
    RetrievalDone,
)

logger = logging.getLogger(__name__)

GENERIC_ERROR_MESSAGE = "Something went wrong. Please try again."


def sse_frame(event: str, data: dict[str, Any]) -> bytes:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {payload}\n\n".encode()


def event_to_frame(event: QaEvent) -> bytes:
    match event:
        case QuestionCreated():
            return sse_frame(
                "question.created",
                {"question_id": str(event.question_id), "answer_id": str(event.answer_id)},
            )
        case RetrievalDone():
            return sse_frame(
                "retrieval.done",
                {
                    "question_id": str(event.question_id),
                    "chunk_count": event.chunk_count,
                    "ms": event.ms,
                },
            )
        case AnswerStarted():
            return sse_frame(
                "answer.started",
                {
                    "answer_id": str(event.answer_id),
                    "question_id": str(event.question_id),
                    "provider": event.provider,
                    "model": event.model,
                },
            )
        case AnswerDelta():
            return sse_frame(
                "answer.delta",
                {"answer_id": str(event.answer_id), "index": event.index, "text": event.text},
            )
        case AnswerDone():
            return sse_frame(
                "answer.done",
                {
                    "answer_id": str(event.answer_id),
                    "question_id": str(event.question_id),
                    "provider": event.provider,
                    "model": event.model,
                    "metrics": {
                        "retrieval_ms": event.retrieval_ms,
                        "first_token_ms": event.first_token_ms,
                        "total_ms": event.total_ms,
                    },
                    "fallback_used": event.fallback_used,
                    "input_tokens": event.input_tokens,
                    "output_tokens": event.output_tokens,
                },
            )
        case AnswerFailed():
            return sse_frame(
                "answer.error",
                {
                    "answer_id": str(event.answer_id),
                    "question_id": str(event.question_id),
                    "code": event.code,
                    "message": event.message,
                    "retryable": event.retryable,
                },
            )


async def frames(first: QaEvent, rest: AsyncIterator[QaEvent]) -> AsyncIterator[bytes]:
    """The SSE byte stream. An unexpected failure becomes a generic `answer.error` frame: no
    exception text, stack trace or internal detail is ever sent to the client."""
    yield event_to_frame(first)
    try:
        async for event in rest:
            yield event_to_frame(event)
    except Exception as exc:  # noqa: BLE001 - nothing internal may reach the client
        logger.warning("answer stream failed", extra={"error_type": type(exc).__name__})
        answer_id = getattr(first, "answer_id", None)
        question_id = getattr(first, "question_id", None)
        yield sse_frame(
            "answer.error",
            {
                "answer_id": str(answer_id) if answer_id else None,
                "question_id": str(question_id) if question_id else None,
                "code": "internal_error",
                "message": GENERIC_ERROR_MESSAGE,
                "retryable": True,
            },
        )


class SseResponse(StreamingResponse):
    """A StreamingResponse that ALWAYS runs `on_close` when the response ends, however it ends
    (finished, error, or client disconnect cancelling the response). `on_close` closes the answer
    generator (which records a `cancelled` answer with its partial text and closes the provider
    stream) and releases the user's concurrency slot; it runs outside the cancelled scope."""

    def __init__(
        self, content: AsyncIterator[bytes], on_close: Callable[[], Awaitable[None]]
    ) -> None:
        super().__init__(
            content,
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",  # no proxy may buffer or compress
                "X-Accel-Buffering": "no",
            },
        )
        self._on_close = on_close

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await asyncio.shield(self._on_close())
