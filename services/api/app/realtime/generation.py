"""One typed question being answered over the realtime channel (Phases 9B/9C, D-42/D-43).

A `Generation` drives `QuestionAnswerer.ask` (the single Q&A core, shared with SSE) as its own
task. It belongs to the (user, session) `SessionChannel`, NOT to a socket: every event goes to the
channel's retained buffer (numbered there), and nothing here ever waits for a socket. When a socket
drops, the generation keeps running until it ends or the channel's grace period expires.

Lifecycle guarantees, whatever way the generation ends (done, failed, refused, cancelled, grace
expired, server shutdown):
- the user's concurrency slot is released exactly once, and only AFTER the answer generator has
  been closed (which closes the provider stream and stores a `cancelled` answer with its partial
  text; a completed answer is never downgraded: `finish_answer` refuses to rewrite it);
- no exception text, question text or answer text is logged or sent.

The database is the source of truth for the terminal state. A user cancel interrupts the task, but
the terminal event is derived from the stored answer row, so a cancel that lost the race with the
final commit reports the completed answer (`answer.done`), never a false `answer.cancelled`.

Exactly one terminal event is emitted for a generation that started: `answer.done`, `answer.error`
or `answer.cancelled`. A refusal before the stream starts is a single control `error` instead.
"""

import asyncio
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import AppError
from app.db.models import User
from app.qa import repository
from app.qa.events import AnswerDone, AnswerFailed, QaEvent, QuestionCreated
from app.qa.limits import QuestionLimits, Slot
from app.qa.service import QuestionAnswerer
from app.realtime import adapter
from app.realtime import schemas as proto
from app.realtime.session_channel import SessionChannel

logger = logging.getLogger("app.realtime")


@dataclass(frozen=True, slots=True)
class _StoredAnswer:
    status: str
    provider: str | None
    model: str | None
    fallback_used: bool
    input_tokens: int | None
    output_tokens: int | None
    retrieval_ms: int | None
    first_token_ms: int | None
    total_ms: int | None
    error_code: str | None


class Generation:
    def __init__(
        self,
        *,
        user_id: uuid.UUID,
        session_id: uuid.UUID,
        session: SessionChannel,
        answerer: QuestionAnswerer,
        limits: QuestionLimits,
        sessionmaker: async_sessionmaker[AsyncSession],
        text: str,
        question_type_override: str | None,
        on_account_inactive: Callable[[], None],
    ) -> None:
        self._user_id = user_id
        self._session_id = session_id
        self._session = session
        self._answerer = answerer
        self._limits = limits
        self._sessionmaker = sessionmaker
        self._text = text
        self._override = question_type_override
        self._on_account_inactive = on_account_inactive
        self._task: asyncio.Task[None] | None = None
        self._cancel_requested = False
        self.question_id: uuid.UUID | None = None
        self.answer_id: uuid.UUID | None = None
        self.terminal_sent = False  # a terminal event is already emitted

    # ------------------------------------------------------------------ control
    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    @property
    def active(self) -> bool:
        return self._task is not None and not self._task.done()

    def cancel(self, question_id: uuid.UUID) -> bool:
        """Cancel if `question_id` is THIS running generation. Anything else returns False and the
        caller sends the one indistinguishable `nothing_to_cancel` (no ownership oracle)."""
        if (
            not self.active
            or self._task is None
            or self.question_id is None
            or question_id != self.question_id
            or self.terminal_sent
            or self._cancel_requested
        ):
            return False
        self._cancel_requested = True
        self._task.cancel()
        return True

    async def aclose(self) -> None:
        """Stop the generation (grace expired or server shutdown) and wait for its cleanup."""
        task = self._task
        if task is None or task.done():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    # ------------------------------------------------------------------ the run
    def _emit(self, event: QaEvent) -> None:
        self._session.emit(partial(adapter.to_message, event))

    def _send_error(self, code: str, *, fatal: bool = False) -> None:
        self._session.send_control(proto.error_event(code, fatal=fatal))

    async def _user_is_active(self) -> bool:
        async with self._sessionmaker() as db:
            user = await db.get(User, self._user_id)
        return user is not None and user.status == "active"

    async def _stored_answer(self) -> _StoredAnswer | None:
        """The answer row as the database holds it now (None if it cannot be read)."""
        if self.answer_id is None:
            return None
        try:
            async with self._sessionmaker() as db:
                row = await repository.get_answer(
                    db, self._user_id, self._session_id, self.answer_id
                )
                if row is None:
                    return None
                return _StoredAnswer(
                    status=row.status,
                    provider=row.provider,
                    model=row.model,
                    fallback_used=row.fallback_used,
                    input_tokens=row.input_tokens,
                    output_tokens=row.output_tokens,
                    retrieval_ms=row.retrieval_ms,
                    first_token_ms=row.first_token_ms,
                    total_ms=row.total_ms,
                    error_code=row.error_code,
                )
        except Exception as exc:  # noqa: BLE001 - fall back to `cancelled`; never leak the reason
            logger.warning("ws stored answer unreadable", extra={"error_type": type(exc).__name__})
            return None

    async def _announce_after_cancel(self) -> None:
        """A user cancel interrupted the task: report what the DATABASE says happened."""
        if self.answer_id is None or self.question_id is None:
            return
        answer_id, question_id = self.answer_id, self.question_id
        self.terminal_sent = True
        stored = await self._stored_answer()
        if stored is not None and stored.status == "complete":
            # The cancel lost the race with the final commit: the completed answer wins.
            total = stored.total_ms or 0
            self._emit(
                AnswerDone(
                    answer_id=answer_id,
                    question_id=question_id,
                    provider=stored.provider or "",
                    model=stored.model or "",
                    retrieval_ms=stored.retrieval_ms or 0,
                    first_token_ms=stored.first_token_ms
                    if stored.first_token_ms is not None
                    else total,
                    total_ms=total,
                    fallback_used=stored.fallback_used,
                    input_tokens=stored.input_tokens,
                    output_tokens=stored.output_tokens,
                )
            )
        elif stored is not None and stored.status == "error":
            code = stored.error_code or proto.ERR_INTERNAL
            self._emit(
                AnswerFailed(
                    answer_id=answer_id,
                    question_id=question_id,
                    code=code,
                    message=proto.ERROR_MESSAGES.get(
                        code, proto.ERROR_MESSAGES[proto.ERR_INTERNAL]
                    ),
                    retryable=True,
                )
            )
        else:  # `cancelled`, or a row that is still `streaming` because its write failed
            self._session.emit(partial(adapter.cancelled, answer_id, question_id))

    async def _run(self) -> None:
        slot: Slot | None = None
        stream = None
        try:
            # R-44: the socket outlives the handshake, so the account is re-checked per question.
            if not await self._user_is_active():
                self._send_error(proto.ERR_ACCOUNT_INACTIVE, fatal=True)
                self._on_account_inactive()  # the gateway flushes the error and closes (1008)
                return
            slot = self._limits.admit(self._user_id)
            stream = self._answerer.ask(self._user_id, self._session_id, self._text, self._override)
            async for event in stream:
                if isinstance(event, QuestionCreated):
                    self.question_id, self.answer_id = event.question_id, event.answer_id
                if isinstance(event, AnswerDone | AnswerFailed):
                    self.terminal_sent = True
                self._emit(event)
        except AppError as refusal:
            # Only raised before the stream starts (`ask` turns later failures into events).
            code = refusal.code if refusal.code in proto.PRE_STREAM_ERRORS else proto.ERR_INTERNAL
            self._send_error(code)
        except asyncio.CancelledError:
            if not self._cancel_requested:
                raise  # grace expired / shutdown: nobody is listening
            # `answer.cancel`: `ask` has already stored its outcome on its way out.
            await self._announce_after_cancel()
        except Exception as exc:  # noqa: BLE001 - a safety net: nothing internal may reach the peer
            logger.warning("ws generation failed", extra={"error_type": type(exc).__name__})
            if (
                self.answer_id is not None
                and self.question_id is not None
                and not self.terminal_sent
            ):
                self.terminal_sent = True
                self._session.emit(
                    partial(adapter.internal_answer_error, self.answer_id, self.question_id)
                )
            elif not self.terminal_sent:
                self._send_error(proto.ERR_INTERNAL)
        finally:
            try:
                if stream is not None:
                    await asyncio.shield(stream.aclose())
            finally:
                if slot is not None:
                    slot.release()
