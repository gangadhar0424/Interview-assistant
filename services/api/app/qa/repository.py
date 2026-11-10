"""Data access for questions and answers.

Same rule as app/sessions/repository.py: every function takes `user_id` and filters on it in SQL;
there is no lookup or update by id alone. Each call is meant to run in its own short transaction
(the orchestrator never keeps a session open while tokens stream).
"""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DetectedQuestion, GeneratedAnswer


@dataclass
class AnswerResult:
    """Everything recorded when an answer finishes (complete, error or cancelled)."""

    status: str
    text: str = ""
    error_code: str | None = None
    provider: str | None = None
    model: str | None = None
    fallback_used: bool = False
    input_tokens: int | None = None
    output_tokens: int | None = None
    retrieval_ms: int | None = None
    first_token_ms: int | None = None
    total_ms: int | None = None
    retrieved_chunk_ids: list[uuid.UUID] = field(default_factory=list)
    # Content-free citation-integrity summary (`app.qa.grounding`); None when not checked.
    grounding_check: dict[str, Any] | None = None


async def create_question_with_answer(
    session: AsyncSession,
    user_id: uuid.UUID,
    session_id: uuid.UUID,
    *,
    text: str,
    question_type: str,
    generation_config: dict[str, Any],
    prompt_version: str,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Insert the question and its `streaming` answer; `user_id` is the authenticated user."""
    question = DetectedQuestion(
        session_id=session_id,
        user_id=user_id,
        source="typed",
        kind="explicit",
        raw_text=text,
        question_type=question_type,
        status="detected",
    )
    session.add(question)
    await session.flush()
    answer = GeneratedAnswer(
        question_id=question.id,
        session_id=session_id,
        user_id=user_id,
        generation_config=generation_config,
        prompt_version=prompt_version,
        status="streaming",
    )
    session.add(answer)
    await session.flush()
    return question.id, answer.id


async def recent_history(
    session: AsyncSession, user_id: uuid.UUID, session_id: uuid.UUID, limit: int
) -> list[tuple[str, str]]:
    """The last `limit` answered (question, answer) pairs of this session, oldest first."""
    if limit <= 0:
        return []
    stmt = (
        select(DetectedQuestion.raw_text, GeneratedAnswer.text)
        .join(GeneratedAnswer, GeneratedAnswer.question_id == DetectedQuestion.id)
        .where(
            DetectedQuestion.user_id == user_id,
            DetectedQuestion.session_id == session_id,
            GeneratedAnswer.user_id == user_id,
            GeneratedAnswer.status == "complete",
        )
        .order_by(DetectedQuestion.detected_at.desc(), DetectedQuestion.id.desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()
    return [(question, answer) for question, answer in reversed(rows)]


async def finish_answer(
    session: AsyncSession, user_id: uuid.UUID, answer_id: uuid.UUID, result: AnswerResult
) -> bool:
    """Record the outcome of an answer. Returns False if no such answer exists for this user, or if
    it is already `complete`: a completed answer is terminal and is never rewritten (so a late
    cancellation or error can never downgrade it)."""
    updated = await session.execute(
        update(GeneratedAnswer)
        .where(
            GeneratedAnswer.id == answer_id,
            GeneratedAnswer.user_id == user_id,
            GeneratedAnswer.status != "complete",
        )
        .values(
            status=result.status,
            text=result.text,
            error_code=result.error_code,
            provider=result.provider,
            model=result.model,
            fallback_used=result.fallback_used,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            retrieval_ms=result.retrieval_ms,
            first_token_ms=result.first_token_ms,
            total_ms=result.total_ms,
            retrieved_chunk_ids=result.retrieved_chunk_ids,
            grounding_check=result.grounding_check,
        )
        .returning(GeneratedAnswer.id)
    )
    return updated.first() is not None


async def mark_question_answered(
    session: AsyncSession, user_id: uuid.UUID, question_id: uuid.UUID
) -> None:
    await session.execute(
        update(DetectedQuestion)
        .where(DetectedQuestion.id == question_id, DetectedQuestion.user_id == user_id)
        .values(status="answered")
    )


STALE_ERROR_CODE = "generation_interrupted"


async def expire_stale_streaming(
    session: AsyncSession, user_id: uuid.UUID, session_id: uuid.UUID, older_than_seconds: float
) -> int:
    """A `streaming` answer older than the request timeout can only be an orphan (the server
    enforces that timeout): read it as `error`. Scoped to one user's one session; no scheduler."""
    cutoff = datetime.now(UTC) - timedelta(seconds=older_than_seconds)
    result = await session.execute(
        update(GeneratedAnswer)
        .where(
            GeneratedAnswer.user_id == user_id,
            GeneratedAnswer.session_id == session_id,
            GeneratedAnswer.status == "streaming",
            GeneratedAnswer.created_at < cutoff,
        )
        .values(status="error", error_code=STALE_ERROR_CODE)
        .returning(GeneratedAnswer.id)
    )
    return len(result.all())


async def list_questions(
    session: AsyncSession,
    user_id: uuid.UUID,
    session_id: uuid.UUID,
    *,
    limit: int,
    after: tuple[datetime, uuid.UUID] | None = None,
) -> list[DetectedQuestion]:
    """Oldest first (a transcript), keyset-paginated on (detected_at, id)."""
    stmt = select(DetectedQuestion).where(
        DetectedQuestion.user_id == user_id, DetectedQuestion.session_id == session_id
    )
    if after is not None:
        detected_at, row_id = after
        stmt = stmt.where(
            or_(
                DetectedQuestion.detected_at > detected_at,
                and_(DetectedQuestion.detected_at == detected_at, DetectedQuestion.id > row_id),
            )
        )
    stmt = stmt.order_by(DetectedQuestion.detected_at.asc(), DetectedQuestion.id.asc()).limit(limit)
    return list((await session.execute(stmt)).scalars())


async def answers_for_questions(
    session: AsyncSession,
    user_id: uuid.UUID,
    session_id: uuid.UUID,
    question_ids: list[uuid.UUID],
) -> list[GeneratedAnswer]:
    if not question_ids:
        return []
    stmt = (
        select(GeneratedAnswer)
        .where(
            GeneratedAnswer.user_id == user_id,
            GeneratedAnswer.session_id == session_id,
            GeneratedAnswer.question_id.in_(question_ids),
        )
        .order_by(GeneratedAnswer.created_at.asc(), GeneratedAnswer.id.asc())
    )
    return list((await session.execute(stmt)).scalars())


async def get_answer(
    session: AsyncSession, user_id: uuid.UUID, session_id: uuid.UUID, answer_id: uuid.UUID
) -> GeneratedAnswer | None:
    stmt = select(GeneratedAnswer).where(
        GeneratedAnswer.id == answer_id,
        GeneratedAnswer.session_id == session_id,
        GeneratedAnswer.user_id == user_id,
    )
    return (await session.execute(stmt)).scalar_one_or_none()
