"""Read side of typed Q&A: question history and single answers (owner-scoped).

Every query is scoped by the authenticated user AND the session; the session itself must belong to
the user first. An unknown session, another user's session and (for answers) an unknown or foreign
answer id are indistinguishable. A `streaming` answer older than the request timeout is read as
`error` (and persisted as such) so an orphaned row never looks alive forever.
"""

import base64
import binascii
import json
import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.db.models import GeneratedAnswer
from app.qa import repository
from app.qa.schemas import AnswerOut, QuestionList, QuestionOut
from app.sessions import repository as sessions_repository
from app.sessions.service import session_not_found


def answer_not_found() -> AppError:
    return AppError(404, "answer_not_found", "Answer not found.")


def to_answer_out(answer: GeneratedAnswer) -> AnswerOut:
    return AnswerOut(
        id=answer.id,
        question_id=answer.question_id,
        status=answer.status,
        text=answer.text,
        provider=answer.provider,
        model=answer.model,
        retrieval_ms=answer.retrieval_ms,
        first_token_ms=answer.first_token_ms,
        total_ms=answer.total_ms,
        fallback_used=answer.fallback_used,
        error_code=answer.error_code,
        input_tokens=answer.input_tokens,
        output_tokens=answer.output_tokens,
        created_at=answer.created_at,
    )


def _encode_cursor(detected_at: datetime, row_id: uuid.UUID) -> str:
    raw = json.dumps({"c": detected_at.isoformat(), "i": str(row_id)}).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode()))
        return datetime.fromisoformat(data["c"]), uuid.UUID(data["i"])
    except (ValueError, KeyError, TypeError, binascii.Error):
        raise AppError(422, "invalid_cursor", "The pagination cursor is invalid.") from None


async def _owned_session(db: AsyncSession, user_id: uuid.UUID, session_id: uuid.UUID) -> None:
    if await sessions_repository.get_for_user(db, user_id, session_id) is None:
        raise session_not_found()


async def _expire_stale(
    db: AsyncSession, user_id: uuid.UUID, session_id: uuid.UUID, timeout_seconds: float
) -> None:
    if await repository.expire_stale_streaming(db, user_id, session_id, timeout_seconds):
        await db.commit()


async def list_questions(
    db: AsyncSession,
    user_id: uuid.UUID,
    session_id: uuid.UUID,
    *,
    limit: int,
    cursor: str | None,
    timeout_seconds: float,
) -> QuestionList:
    await _owned_session(db, user_id, session_id)
    await _expire_stale(db, user_id, session_id, timeout_seconds)
    after = _decode_cursor(cursor) if cursor else None
    rows = await repository.list_questions(db, user_id, session_id, limit=limit + 1, after=after)
    page, more = rows[:limit], len(rows) > limit
    answers = await repository.answers_for_questions(db, user_id, session_id, [q.id for q in page])
    by_question: dict[uuid.UUID, list[AnswerOut]] = {}
    for answer in answers:
        by_question.setdefault(answer.question_id, []).append(to_answer_out(answer))
    items = [
        QuestionOut(
            id=q.id,
            text=q.raw_text,
            source=q.source,
            kind=q.kind,
            type=q.question_type,
            confidence=None,
            status=q.status,
            created_at=q.detected_at,
            answers=by_question.get(q.id, []),
        )
        for q in page
    ]
    last = page[-1] if page else None
    return QuestionList(
        items=items,
        next_cursor=_encode_cursor(last.detected_at, last.id) if more and last else None,
    )


async def get_answer(
    db: AsyncSession,
    user_id: uuid.UUID,
    session_id: uuid.UUID,
    answer_id: uuid.UUID,
    *,
    timeout_seconds: float,
) -> AnswerOut:
    await _owned_session(db, user_id, session_id)
    await _expire_stale(db, user_id, session_id, timeout_seconds)
    answer = await repository.get_answer(db, user_id, session_id, answer_id)
    if answer is None:
        raise answer_not_found()
    return to_answer_out(answer)
