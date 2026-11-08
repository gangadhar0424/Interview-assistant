"""Maps the transport-neutral `QaEvent`s of `QuestionAnswerer.ask` to WebSocket envelopes (9B).

The payloads are field-for-field the same as the SSE frames of Phase 8B (`app.qa.sse`); a test
compares the two so the transports cannot drift. Nothing here reads or formats user text.
"""

import uuid

from pydantic import BaseModel

from app.qa.events import (
    AnswerDelta,
    AnswerDone,
    AnswerFailed,
    AnswerStarted,
    QaEvent,
    QuestionCreated,
    RetrievalDone,
)
from app.realtime import schemas as proto


def _envelope(seq: int) -> dict[str, object]:
    return {"id": proto.new_message_id(), "seq": seq, "ts": proto.now_ms()}


def to_message(event: QaEvent, seq: int) -> BaseModel:
    match event:
        case QuestionCreated():
            return proto.QuestionCreated(
                **_envelope(seq),
                payload=proto.QuestionCreatedPayload(
                    question_id=event.question_id, answer_id=event.answer_id
                ),
            )
        case RetrievalDone():
            return proto.RetrievalDone(
                **_envelope(seq),
                payload=proto.RetrievalDonePayload(
                    question_id=event.question_id, chunk_count=event.chunk_count, ms=event.ms
                ),
            )
        case AnswerStarted():
            return proto.AnswerStarted(
                **_envelope(seq),
                payload=proto.AnswerStartedPayload(
                    answer_id=event.answer_id,
                    question_id=event.question_id,
                    provider=event.provider,
                    model=event.model,
                ),
            )
        case AnswerDelta():
            return proto.AnswerDelta(
                **_envelope(seq),
                payload=proto.AnswerDeltaPayload(
                    answer_id=event.answer_id, index=event.index, text=event.text
                ),
            )
        case AnswerDone():
            return proto.AnswerDone(
                **_envelope(seq),
                payload=proto.AnswerDonePayload(
                    answer_id=event.answer_id,
                    question_id=event.question_id,
                    provider=event.provider,
                    model=event.model,
                    metrics=proto.AnswerMetrics(
                        retrieval_ms=event.retrieval_ms,
                        first_token_ms=event.first_token_ms,
                        total_ms=event.total_ms,
                    ),
                    fallback_used=event.fallback_used,
                    input_tokens=event.input_tokens,
                    output_tokens=event.output_tokens,
                ),
            )
        case AnswerFailed():
            return proto.AnswerError(
                **_envelope(seq),
                payload=proto.AnswerErrorPayload(
                    answer_id=event.answer_id,
                    question_id=event.question_id,
                    code=event.code,
                    message=event.message,
                    retryable=event.retryable,
                ),
            )


def cancelled(answer_id: uuid.UUID, question_id: uuid.UUID, seq: int) -> BaseModel:
    return proto.AnswerCancelled(
        **_envelope(seq),
        payload=proto.AnswerCancelledPayload(answer_id=answer_id, question_id=question_id),
    )


def internal_answer_error(answer_id: uuid.UUID, question_id: uuid.UUID, seq: int) -> BaseModel:
    """The safety net: an unexpected failure after the stream started (no detail ever sent)."""
    return proto.AnswerError(
        **_envelope(seq),
        payload=proto.AnswerErrorPayload(
            answer_id=answer_id,
            question_id=question_id,
            code=proto.ERR_INTERNAL,
            message=proto.ERROR_MESSAGES[proto.ERR_INTERNAL],
            retryable=True,
        ),
    )
