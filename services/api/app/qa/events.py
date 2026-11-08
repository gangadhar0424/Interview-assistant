"""Events yielded by `QuestionAnswerer.ask` (names mirror docs/API.md: question.created,
retrieval.done, answer.started, answer.delta, answer.done, answer.error)."""

import uuid
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class QuestionCreated:
    question_id: uuid.UUID
    answer_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class RetrievalDone:
    question_id: uuid.UUID
    chunk_count: int
    ms: int


@dataclass(frozen=True, slots=True)
class AnswerStarted:
    answer_id: uuid.UUID
    question_id: uuid.UUID
    provider: str
    model: str


@dataclass(frozen=True, slots=True)
class AnswerDelta:
    answer_id: uuid.UUID
    index: int
    text: str


@dataclass(frozen=True, slots=True)
class AnswerDone:
    answer_id: uuid.UUID
    question_id: uuid.UUID
    provider: str
    model: str
    retrieval_ms: int
    first_token_ms: int
    total_ms: int
    fallback_used: bool
    input_tokens: int | None
    output_tokens: int | None


@dataclass(frozen=True, slots=True)
class AnswerFailed:
    """A safe, user-facing failure (fixed code/message; never provider or exception details)."""

    answer_id: uuid.UUID
    question_id: uuid.UUID
    code: str
    message: str
    retryable: bool


QaEvent = QuestionCreated | RetrievalDone | AnswerStarted | AnswerDelta | AnswerDone | AnswerFailed
