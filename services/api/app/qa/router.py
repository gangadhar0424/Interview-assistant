"""Typed Q&A endpoints (Phase 8B, D-39). Ownership comes only from `CurrentUser`.

POST /sessions/{id}/questions   streams the answer as SSE (`Accept: text/event-stream`)
GET  /sessions/{id}/questions   question history with answers (oldest first, cursor paginated)
GET  /sessions/{id}/answers/{answer_id}   one answer with its metrics

Deferred (not in Phase 8): regenerate, cancel, screenshots, export, /me/history, aggregated metrics.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import CurrentUser
from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.db.session import get_session
from app.qa import history
from app.qa.limits import QuestionLimits
from app.qa.schemas import AnswerOut, QuestionCreate, QuestionList
from app.qa.service import QuestionAnswerer
from app.qa.sse import SseResponse, frames

router = APIRouter(prefix="/sessions", tags=["questions"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def get_answerer(request: Request) -> QuestionAnswerer:
    answerer: QuestionAnswerer = request.app.state.question_answerer
    return answerer


def get_limits(request: Request) -> QuestionLimits:
    limits: QuestionLimits = request.app.state.question_limits
    return limits


AnswererDep = Annotated[QuestionAnswerer, Depends(get_answerer)]
LimitsDep = Annotated[QuestionLimits, Depends(get_limits)]


def require_event_stream(request: Request) -> None:
    """The answer can only be delivered as SSE, so the client must ask for it explicitly."""
    accepted = {
        part.split(";")[0].strip().lower() for part in request.headers.get("accept", "").split(",")
    }
    if "text/event-stream" not in accepted:
        raise AppError(
            406,
            "not_acceptable",
            "This endpoint streams server-sent events. Send 'Accept: text/event-stream'.",
        )


EventStreamDep = Annotated[None, Depends(require_event_stream)]


@router.post(
    "/{session_id}/questions",
    responses={200: {"content": {"text/event-stream": {}}, "description": "SSE answer stream"}},
)
async def ask_question(
    session_id: uuid.UUID,
    body: QuestionCreate,
    current: CurrentUser,  # authentication first (401), then the Accept check (406)
    _accept: EventStreamDep,
    answerer: AnswererDep,
    limits: LimitsDep,
) -> Response:
    slot = limits.admit(current.id)  # 429 before any work; the slot is released exactly once
    stream = answerer.ask(
        current.id,
        session_id,
        body.text,
        body.question_type_override,
        input_source=body.input_source,
    )
    try:
        # Runs validation and the first short transaction. A refusal (404, 409, 422) raises here,
        # before the response starts, so the client gets a normal JSON error with that status.
        first = await anext(stream)
    except BaseException:
        slot.release()
        await stream.aclose()
        raise

    async def close() -> None:
        try:
            await stream.aclose()  # records a cancelled answer if the client went away mid-stream
        finally:
            slot.release()

    return SseResponse(frames(first, stream), on_close=close)


@router.get("/{session_id}/questions", response_model=QuestionList)
async def list_questions(
    session_id: uuid.UUID,
    current: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query(max_length=300)] = None,
) -> QuestionList:
    return await history.list_questions(
        session,
        current.id,
        session_id,
        limit=limit,
        cursor=cursor,
        timeout_seconds=settings.qa_request_timeout_seconds,
    )


@router.get("/{session_id}/answers/{answer_id}", response_model=AnswerOut)
async def get_answer(
    session_id: uuid.UUID,
    answer_id: uuid.UUID,
    current: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
) -> AnswerOut:
    return await history.get_answer(
        session,
        current.id,
        session_id,
        answer_id,
        timeout_seconds=settings.qa_request_timeout_seconds,
    )
