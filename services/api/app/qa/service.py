"""Typed question answering orchestration (Phase 8A, D-38).

    ask(user, session, text, override) ->
      validate -> [tx 1: session by (user, id), status ready, materials still ready, compile
      preferences, history, INSERT question + streaming answer] -> question.created
      -> [tx 2: retrieval from the session's selected sources] -> retrieval.done
      -> PromptBuilder -> LLMRouter.stream_chat (NO database session open) -> answer.started,
         answer.delta ...
      -> [tx 3: UPDATE answer + question status] -> answer.done

Pre-stream problems (unknown session, not ready, materials not ready, invalid input) raise
`AppError` before anything is persisted, so the 8B endpoint can still answer with a proper HTTP
status. Failures after that become an `AnswerFailed` event with a fixed, safe message (provider
and exception details never leave this module) and the answer row is finished as `error`. A
consumer that stops early (client disconnect) leaves the answer `cancelled` with its partial text.
Question and answer text are never logged.
"""

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncGenerator, Callable, Sequence
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.errors import InvalidSelection, LLMError, Timeout
from app.ai.router import LLMRouter, Selection
from app.ai.types import StreamMeta, Token, Usage
from app.core.config import Settings
from app.core.errors import AppError
from app.core.metrics import answer_grounding_checks
from app.db.models import SessionPreferences
from app.db.session import set_tenant_context
from app.embeddings.base import EmbeddingProvider
from app.ingestion import repository as ingestion_repository
from app.preferences.answer_config import CompiledPreferences
from app.preferences.compiler import PreferenceCompiler, PreferencesInput
from app.preferences.enums import QuestionType
from app.preferences.errors import InvalidPreference
from app.preferences.prompt_builder import (
    PROMPT_VERSION,
    EvidencePassage,
    EvidenceRef,
    PromptBuilder,
    PromptInputs,
)
from app.preferences.sanitize import clean_text
from app.qa import repository
from app.qa.context import ContextConfig, retrieve_context, truncate
from app.qa.events import (
    AnswerDelta,
    AnswerDone,
    AnswerFailed,
    AnswerStarted,
    QaEvent,
    QuestionCreated,
    RetrievalDone,
)
from app.qa.grounding import GroundingCheck, check_citations
from app.retrieval.service import MAX_QUERY_CHARS
from app.sessions import repository as sessions_repository
from app.sessions.materials import is_ready
from app.sessions.service import session_not_found

logger = logging.getLogger(__name__)

RETRIEVAL_FAILED = "retrieval_failed"
EMPTY_ANSWER = "empty_answer"
INTERNAL_ERROR = "internal_error"
SAFE_MESSAGES = {
    RETRIEVAL_FAILED: "Your materials could not be searched. Please try again.",
    EMPTY_ANSWER: "The AI service returned an empty answer. Please try again.",
    INTERNAL_ERROR: "Something went wrong. Please try again.",
}


class _Failed(Exception):
    """Internal: a controlled failure carrying a safe code/message (never raw exception text)."""

    def __init__(self, code: str, message: str, retryable: bool) -> None:
        super().__init__(code)
        self.code = code
        self.message = message
        self.retryable = retryable


@dataclass(frozen=True)
class _Prepared:
    session_id: uuid.UUID
    question_id: uuid.UUID
    answer_id: uuid.UUID
    company_name: str
    job_description: str
    resume_id: uuid.UUID
    document_ids: list[uuid.UUID]
    compiled: CompiledPreferences
    history: list[tuple[str, str]]


@dataclass
class _Run:
    """Progress of one generation, so a failure or cancellation can record what happened."""

    retrieval_ms: int | None = None
    chunk_ids: list[uuid.UUID] = field(default_factory=list)
    parts: list[str] = field(default_factory=list)
    meta: StreamMeta | None = None
    first_token_ms: int | None = None
    usage: Usage | None = None
    finished: bool = False  # the outcome is already recorded (never downgrade it to cancelled)


class QuestionAnswerer:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
        router: LLMRouter,
        embedder: EmbeddingProvider,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._settings = settings
        self._router = router
        self._embedder = embedder
        self._clock = clock
        self._context = ContextConfig.from_settings(settings)
        self._generation_timeout = settings.qa_request_timeout_seconds

    # ---------------------------------------------------------------- validation

    @staticmethod
    def _clean_question(text: str) -> str:
        cleaned = clean_text(text)
        if not cleaned:
            raise AppError(422, "validation_error", "The question must not be empty.")
        if len(cleaned) > MAX_QUERY_CHARS:
            raise AppError(
                422,
                "validation_error",
                f"The question must be at most {MAX_QUERY_CHARS} characters.",
            )
        return cleaned

    @staticmethod
    def _parse_override(value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return QuestionType(value).value  # exact match against the existing enum
        except ValueError:
            raise AppError(
                422, "validation_error", "question_type_override is not a valid question type."
            ) from None

    def _compile(self, prefs: SessionPreferences, override: str | None) -> CompiledPreferences:
        try:
            return PreferenceCompiler.from_settings(self._settings).compile(
                PreferencesInput(
                    language=prefs.language,
                    question_type=override or prefs.question_type,
                    answer_format=prefs.answer_format,
                    answer_length=prefs.answer_length,
                    answer_tone=prefs.answer_tone,
                    extra_instructions=prefs.extra_instructions,
                    provider=prefs.provider,
                    model=prefs.model,
                )
            )
        except InvalidPreference:  # e.g. an operator lowered a limit after the session was saved
            raise AppError(
                409,
                "session_preferences_invalid",
                "This session's preferences need to be updated before asking questions.",
            ) from None

    @staticmethod
    async def _require_ready_materials(
        session: AsyncSession,
        user_id: uuid.UUID,
        resume_id: uuid.UUID | None,
        document_ids: Sequence[uuid.UUID],
        selected_snapshot: Sequence[dict[str, object]],
    ) -> uuid.UUID:
        """R-27: EVERY material the session selected must still exist and be fully indexed NOW.

        Owner rule (D-39): a selected material that was deleted or is no longer Ready refuses the
        question with 409; the context is never silently reduced. The resume is mandatory, and the
        selection is the session's `materials_snapshot` (what the user chose), so a selected
        document whose link vanished because the document was deleted counts as missing. The user
        changes the selection explicitly by editing the session.
        """
        unavailable = AppError(
            409,
            "materials_not_ready",
            "A selected resume or document is no longer ready. Check your files and try again.",
        )
        if resume_id is None:
            raise unavailable
        selected = {uuid.UUID(str(item["id"])) for item in selected_snapshot}
        if selected - set(document_ids):  # a selected document was deleted
            raise unavailable
        resume_jobs = await ingestion_repository.jobs_for_sources(
            session, user_id, "resume", [resume_id]
        )
        if not is_ready(resume_jobs.get(resume_id, {})):
            raise unavailable
        doc_jobs = await ingestion_repository.jobs_for_sources(
            session, user_id, "document", list(document_ids)
        )
        if any(not is_ready(doc_jobs.get(doc_id, {})) for doc_id in document_ids):
            raise unavailable
        return resume_id

    # ------------------------------------------------------------------ steps

    async def _prepare(
        self, user_id: uuid.UUID, session_id: uuid.UUID, question: str, override: str | None
    ) -> _Prepared:
        """Transaction 1: validate everything, then create the question and streaming answer."""
        async with self._sessionmaker() as db:
            await set_tenant_context(db, user_id)
            row = await sessions_repository.get_for_user(db, user_id, session_id)
            if row is None:
                raise session_not_found()  # another user's session looks exactly like a missing one
            if row.status != "ready":
                raise AppError(
                    409, "session_not_ready", "Questions can only be asked in a ready session."
                )
            prefs = await sessions_repository.preferences_for_user(db, user_id, session_id)
            if prefs is None:
                raise session_not_found()
            document_ids = sorted(
                await sessions_repository.document_ids_for_user(db, user_id, session_id), key=str
            )
            resume_id = await self._require_ready_materials(
                db, user_id, row.resume_id, document_ids, row.materials_snapshot
            )
            compiled = self._compile(prefs, override)
            history = [
                (
                    truncate(q, self._context.history_item_max_chars),
                    truncate(a, self._context.history_item_max_chars),
                )
                for q, a in await repository.recent_history(
                    db, user_id, session_id, self._context.history_pairs
                )
            ]
            snapshot: dict[str, Any] = {
                **compiled.to_dict(),
                "question_type_override": override,
                "context": {
                    "top_k": self._context.top_k,
                    "resume_quota": self._context.resume_quota,
                    "max_chars": self._context.max_chars,
                    "history_pairs": self._context.history_pairs,
                },
            }
            question_id, answer_id = await repository.create_question_with_answer(
                db,
                user_id,
                session_id,
                text=question,
                question_type=compiled.answer.question_type.value,
                generation_config=snapshot,
                prompt_version=PROMPT_VERSION,
            )
            await db.commit()
        return _Prepared(
            session_id=session_id,
            question_id=question_id,
            answer_id=answer_id,
            company_name=row.company_name,
            job_description=row.job_description,
            resume_id=resume_id,
            document_ids=document_ids,
            compiled=compiled,
            history=history,
        )

    def _ms(self, since: float) -> int:
        return max(0, int((self._clock() - since) * 1000))

    def _selection(self, compiled: CompiledPreferences) -> Selection | None:
        provider, model = compiled.answer.provider, compiled.answer.model
        if provider is None:
            return None  # server default chain
        if model is None:
            default = self._router.registry.default_model(provider, "streaming")
            if default is None:
                raise InvalidSelection()
            model = default.id
        return Selection(provider, model)

    async def _finish(
        self,
        user_id: uuid.UUID,
        prepared: _Prepared,
        result: repository.AnswerResult,
        *,
        answered: bool = False,
    ) -> None:
        """Transaction 3: record the outcome (a fresh short session, never held while streaming)."""
        async with self._sessionmaker() as db:
            await set_tenant_context(db, user_id)
            await repository.finish_answer(db, user_id, prepared.answer_id, result)
            if answered:
                await repository.mark_question_answered(db, user_id, prepared.question_id)
            await db.commit()

    @staticmethod
    def _result(
        status: str, run: _Run, started_ms: int | None, **extra: Any
    ) -> repository.AnswerResult:
        return repository.AnswerResult(
            status=status,
            text="".join(run.parts),
            provider=run.meta.provider if run.meta else None,
            model=run.meta.model if run.meta else None,
            fallback_used=run.meta.fallback_used if run.meta else False,
            input_tokens=run.usage.input_tokens if run.usage else None,
            output_tokens=run.usage.output_tokens if run.usage else None,
            retrieval_ms=run.retrieval_ms,
            first_token_ms=run.first_token_ms,
            total_ms=started_ms,
            retrieved_chunk_ids=list(run.chunk_ids),
            **extra,
        )

    @staticmethod
    def _citation_check(
        evidence: Sequence[EvidenceRef], answer_text: str, prepared: _Prepared
    ) -> dict[str, Any]:
        """Citation-integrity check of the finished answer; it can never fail or change it.

        Record-and-observe only: a structural check of evidence tags against what this request's
        prompt rendered. It is NOT proof that the answer is grounded. Any error inside the check
        (or its telemetry) is swallowed and recorded as `check_error`. Only counts and tag numbers
        are logged, never answer text.
        """
        try:
            check = check_citations(answer_text, evidence)
            answer_grounding_checks.labels(status=check.status).inc()
            logger.info(
                "citation integrity checked",
                extra={
                    "session_id": str(prepared.session_id),
                    "question_id": str(prepared.question_id),
                    "answer_id": str(prepared.answer_id),
                    "grounding_status": check.status,
                    "shown_count": check.shown_count,
                    "valid_count": len(check.valid_ids),
                    "unknown_count": len(check.unknown_ids),
                    "malformed_count": check.malformed_count,
                },
            )
            return check.to_json()
        except Exception as exc:  # noqa: BLE001 - the check must never affect the answer
            try:
                logger.warning(
                    "citation integrity check failed", extra={"error_type": type(exc).__name__}
                )
                answer_grounding_checks.labels(status="check_error").inc()
            except Exception:  # noqa: BLE001, S110 - telemetry is best effort
                pass
            return GroundingCheck.errored(len(evidence)).to_json()

    # -------------------------------------------------------------------- main

    async def ask(
        self,
        user_id: uuid.UUID,
        session_id: uuid.UUID,
        text: str,
        question_type_override: str | None = None,
    ) -> AsyncGenerator[QaEvent, None]:
        started = self._clock()
        question = self._clean_question(text)
        override = self._parse_override(question_type_override)
        prepared = await self._prepare(user_id, session_id, question, override)
        run = _Run()
        try:
            yield QuestionCreated(prepared.question_id, prepared.answer_id)

            # --- retrieval (own short transaction; sources come from the session, not the client)
            retrieval_started = self._clock()
            try:
                async with self._sessionmaker() as db:
                    await set_tenant_context(db, user_id)
                    hits = await retrieve_context(
                        db,
                        self._embedder,
                        user_id,
                        question,
                        resume_id=prepared.resume_id,
                        document_ids=prepared.document_ids,
                        config=self._context,
                    )
            except Exception as exc:  # noqa: BLE001 - embedding/DB errors must not leak
                logger.warning("retrieval failed", extra={"error_type": type(exc).__name__})
                raise _Failed(RETRIEVAL_FAILED, SAFE_MESSAGES[RETRIEVAL_FAILED], True) from None
            run.retrieval_ms = self._ms(retrieval_started)
            run.chunk_ids = [hit.chunk_id for hit in hits]
            yield RetrievalDone(prepared.question_id, len(hits), run.retrieval_ms)

            # --- prompt + generation: no database session is open from here until the finish
            prompt = PromptBuilder().build(
                prepared.compiled.answer,
                PromptInputs(
                    question=question,
                    company_name=prepared.company_name,
                    job_description=prepared.job_description,
                    context_passages=[
                        EvidencePassage(
                            source_type=hit.source_type,
                            source_id=hit.source_id,
                            chunk_index=hit.chunk_index,
                            text=hit.text,
                        )
                        for hit in hits
                    ],
                    history=prepared.history,
                ),
            )
            try:
                selection = self._selection(prepared.compiled)
                # `aclosing` guarantees the router (and so the provider stream) is closed when we
                # stop for any reason: failure, deadline, or the consumer disconnecting.
                async with aclosing(
                    self._router.stream_chat(
                        prompt.messages,
                        prepared.compiled.generation,
                        selection=selection,
                        user_id=user_id,
                    )
                ) as stream:
                    async for event in stream:
                        if isinstance(event, StreamMeta):
                            run.meta = event
                            yield AnswerStarted(
                                prepared.answer_id,
                                prepared.question_id,
                                event.provider,
                                event.model,
                            )
                        elif isinstance(event, Token):
                            if run.first_token_ms is None:
                                run.first_token_ms = self._ms(started)  # request -> first token
                            run.parts.append(event.text)
                            yield AnswerDelta(prepared.answer_id, len(run.parts) - 1, event.text)
                        elif isinstance(event, Usage):
                            run.usage = event
                        if self._clock() - started > self._generation_timeout:
                            raise _Failed(Timeout.code, Timeout.message, True)  # total budget
            except LLMError as err:
                raise _Failed(err.code, err.message, err.fallback_eligible) from None

            if not "".join(run.parts).strip() or run.meta is None:
                raise _Failed(EMPTY_ANSWER, SAFE_MESSAGES[EMPTY_ANSWER], True)
            total_ms = self._ms(started)
            # Streaming is over: every delta is already out and nothing below changes the text.
            grounding_check = self._citation_check(prompt.evidence, "".join(run.parts), prepared)
            await self._finish(
                user_id,
                prepared,
                self._result("complete", run, total_ms, grounding_check=grounding_check),
                answered=True,
            )
            run.finished = True
            logger.info(
                "question answered",
                extra={
                    "user_id": str(user_id),
                    "session_id": str(prepared.session_id),
                    "question_id": str(prepared.question_id),
                    "answer_id": str(prepared.answer_id),
                    "total_ms": total_ms,
                    "fallback_used": run.meta.fallback_used,
                },
            )
            yield AnswerDone(
                answer_id=prepared.answer_id,
                question_id=prepared.question_id,
                provider=run.meta.provider,
                model=run.meta.model,
                retrieval_ms=run.retrieval_ms,
                first_token_ms=run.first_token_ms if run.first_token_ms is not None else total_ms,
                total_ms=total_ms,
                fallback_used=run.meta.fallback_used,
                input_tokens=run.usage.input_tokens if run.usage else None,
                output_tokens=run.usage.output_tokens if run.usage else None,
            )
        except _Failed as failure:
            yield await self._failed(user_id, prepared, run, started, failure)
        except Exception as exc:  # noqa: BLE001 - never leak internals to the caller
            logger.warning("answer crashed", extra={"error_type": type(exc).__name__})
            crash = _Failed(INTERNAL_ERROR, SAFE_MESSAGES[INTERNAL_ERROR], True)
            yield await self._failed(user_id, prepared, run, started, crash)
        except (asyncio.CancelledError, GeneratorExit):
            # The consumer stopped (client disconnect): keep the partial text, mark cancelled.
            if run.finished:
                raise
            try:
                await asyncio.shield(
                    self._finish(
                        user_id, prepared, self._result("cancelled", run, self._ms(started))
                    )
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "could not record a cancelled answer", extra={"error_type": type(exc).__name__}
                )
            raise

    async def _failed(
        self, user_id: uuid.UUID, prepared: _Prepared, run: _Run, started: float, failure: _Failed
    ) -> AnswerFailed:
        result = self._result("error", run, self._ms(started), error_code=failure.code)
        await self._finish(user_id, prepared, result)
        logger.info(
            "question failed",
            extra={
                "user_id": str(user_id),
                "session_id": str(prepared.session_id),
                "question_id": str(prepared.question_id),
                "error_code": failure.code,
            },
        )
        return AnswerFailed(
            prepared.answer_id,
            prepared.question_id,
            failure.code,
            failure.message,
            failure.retryable,
        )
