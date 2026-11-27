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
from dataclasses import dataclass, field, replace
from typing import Any

from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.errors import InvalidSelection, LLMError, Timeout
from app.ai.router import LLMRouter, Selection
from app.ai.types import StreamMeta, Token, Usage
from app.core.config import Settings
from app.core.errors import AppError
from app.core.metrics import (
    answer_grounding_checks,
    answer_routes,
    answer_stage_seconds,
    record_answer_timing,
)
from app.db.models import SessionPreferences
from app.db.session import set_tenant_context
from app.embeddings.base import EmbeddingProvider
from app.ingestion import repository as ingestion_repository
from app.preferences.answer_config import CompiledPreferences
from app.preferences.compiler import PreferenceCompiler, PreferencesInput
from app.preferences.enums import AnswerLength, QuestionType
from app.preferences.errors import InvalidPreference
from app.preferences.prompt_builder import (
    PROMPT_VERSION,
    BuiltPrompt,
    EvidencePassage,
    EvidenceRef,
    PromptBuilder,
    PromptInputs,
    retry_question,
)
from app.preferences.sanitize import clean_text
from app.qa import repository
from app.qa.answer_router import Decision, Mode, RouterSettings, decide
from app.qa.cache import AnswerCache, cache_key, digest
from app.qa.caveats import find_caveats
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
from app.qa.extractive import (
    LENGTH_SLACK,
    cut_at_sentence,
    fit_to_words,
    is_verbatim,
    section_source,
    split_for_streaming,
    word_count,
)
from app.qa.grounding import GroundingCheck, check_citations
from app.qa.ranking import Ranked, context_from_ranked, retrieve_ranked
from app.qa.rewrite import rewrite_question
from app.qa.source import source_payload, source_ref
from app.qa.validation import check_deterministic, judge_answer, language_matches
from app.retrieval.service import MAX_QUERY_CHARS, ChunkHit
from app.retrieval.terms import content_terms
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
    parts: list[str] = field(default_factory=list)  # text the user has been shown (and is stored)
    draft: list[str] = field(default_factory=list)  # a held-back general answer, not yet validated
    meta: StreamMeta | None = None
    first_token_ms: int | None = None
    usage: Usage | None = None
    finished: bool = False  # the outcome is already recorded (never downgrade it to cancelled)
    source_kind: str | None = None  # D-66: resume | document | synthesis | general
    source_refs: list[dict[str, Any]] | None = None


@dataclass
class _Plan:
    """What retrieval and routing decided for one question (D-66)."""

    hits: list[ChunkHit]  # passages for the prompt (synthesis / legacy path)
    mode: Mode | None = None  # None: the legacy path (router off or shadow)
    exact: str | None = None  # the verified exact text of an extractive answer
    selection_terms: list[str] = field(default_factory=list)
    # A larger word budget for an answer composed from a whole section (None: the session's own).
    prepared: "_Prepared | None" = None


ANSWER_NOT_VALIDATED = "answer_not_validated"
SAFE_MESSAGES_EXTRA = {
    ANSWER_NOT_VALIDATED: "I could not produce a reliable answer. Please rephrase and try again."
}


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
        self._router_cfg = RouterSettings.from_settings(settings)
        self._cache: AnswerCache | None = None

    def set_cache(self, cache: AnswerCache | None) -> None:
        """Attach the per-session answer cache (Redis is created after the answerer)."""
        self._cache = cache

    async def warm_up(self) -> None:
        """Load the embedding model and touch the database pool, so the first question is not slow
        (D-66, Phase 4). Idempotent and cheap once warm; every failure is ignored."""
        try:
            await asyncio.to_thread(self._embedder.embed_query, "warm up")
            async with self._sessionmaker() as db:
                await db.execute(sql_text("SELECT 1"))
        except Exception as exc:  # noqa: BLE001 - warming is an optimisation, never a requirement
            logger.warning("warm-up skipped", extra={"error_type": type(exc).__name__})

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
        self,
        user_id: uuid.UUID,
        session_id: uuid.UUID,
        question: str,
        override: str | None,
        input_source: str = "typed",
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
                source=input_source,
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
        record_answer_timing(
            status,
            run.meta.provider if run.meta else None,
            run.first_token_ms,
            started_ms,
        )
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
            source_kind=run.source_kind,
            source_refs=run.source_refs,
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

    # ------------------------------------------------------- routing and answers (D-66)

    def _safe_selection(self, compiled: CompiledPreferences) -> Selection | None:
        try:
            return self._selection(compiled)
        except LLMError:
            return None

    @staticmethod
    def _prompt(prepared: _Prepared, question: str, hits: Sequence[ChunkHit]) -> BuiltPrompt:
        return PromptBuilder().build(
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

    @staticmethod
    def _heading_of(hit: ChunkHit) -> str | None:
        return hit.heading.rsplit(" > ", 1)[-1] if hit.heading else None

    def _refs_for(self, hits: Sequence[ChunkHit], ranked: Ranked | None) -> list[dict[str, Any]]:
        """One reference per source of the prompt passages, in relevance order."""
        refs: list[dict[str, Any]] = []
        seen: set[uuid.UUID] = set()
        for hit in hits:
            if hit.source_id in seen:
                continue
            seen.add(hit.source_id)
            name = ranked.names.get(hit.source_id) if ranked is not None else None
            refs.append(source_ref(hit.source_type, hit.source_id, name, self._heading_of(hit)))
        return refs

    async def _plan(
        self,
        user_id: uuid.UUID,
        prepared: _Prepared,
        question: str,
        run: _Run,
        detail: str = "full",
    ) -> _Plan:
        """Retrieval and routing. With the router off this is exactly the previous behaviour."""
        settings = self._settings
        router_mode = settings.qa_router_mode
        started = self._clock()
        ranked: Ranked | None = None
        try:
            if router_mode == "off" and settings.qa_retrieval_mode == "dense":
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
            else:
                ranked = await retrieve_ranked(
                    self._sessionmaker,
                    self._embedder,
                    user_id,
                    question,
                    resume_id=prepared.resume_id,
                    document_ids=prepared.document_ids,
                    config=self._context,
                )
                hits = context_from_ranked(ranked, self._context)
        except Exception as exc:  # noqa: BLE001 - embedding/DB errors must not leak
            logger.warning("retrieval failed", extra={"error_type": type(exc).__name__})
            raise _Failed(RETRIEVAL_FAILED, SAFE_MESSAGES[RETRIEVAL_FAILED], True) from None
        run.retrieval_ms = self._ms(started)
        answer_stage_seconds.labels(stage="retrieval").observe(run.retrieval_ms / 1000)
        run.chunk_ids = [hit.chunk_id for hit in hits]
        if ranked is None or router_mode == "off":
            return _Plan(hits)

        compiled = prepared.compiled
        if detail == "summary":
            # A spoken half-minute: every model-written answer uses the short band, and an exact
            # window is cut to it as well.
            compiled = PreferenceCompiler.from_settings(settings).with_word_budget(
                compiled,
                min_words=settings.answer_short_min_words,
                max_words=settings.answer_short_max_words,
                length=AnswerLength.SHORT,
            )
            prepared = replace(prepared, compiled=compiled)
        terms = content_terms(question)
        decision = decide(
            question,
            ranked.resume,
            ranked.documents,
            self._router_cfg,
            resume_opening=ranked.opening,
        )
        if decision.mode is Mode.UNCERTAIN and router_mode == "on":
            # The router is unsure: one short, time-capped rewrite and a second search.
            rewrite_started = self._clock()
            new_terms = await rewrite_question(
                self._router,
                self._safe_selection(compiled),
                question,
                timeout_seconds=settings.qa_rewrite_timeout_seconds,
                user_id=user_id,
            )
            answer_stage_seconds.labels(stage="rewrite").observe(self._clock() - rewrite_started)
            if new_terms:
                try:
                    second = await retrieve_ranked(
                        self._sessionmaker,
                        self._embedder,
                        user_id,
                        " ".join(new_terms),
                        resume_id=prepared.resume_id,
                        document_ids=prepared.document_ids,
                        config=self._context,
                    )
                except Exception as exc:  # noqa: BLE001 - keep the first pass
                    logger.warning(
                        "rewrite search failed", extra={"error_type": type(exc).__name__}
                    )
                else:
                    ranked, terms = second, new_terms
                    hits = context_from_ranked(second, self._context)
                    run.chunk_ids = [hit.chunk_id for hit in hits]
                    decision = decide(
                        question,
                        second.resume,
                        second.documents,
                        self._router_cfg,
                        resume_opening=second.opening,
                        terms=new_terms,
                    )
            if decision.mode is Mode.UNCERTAIN:
                decision = Decision(Mode.SYNTHESIS, reason="partial")

        extractive = (Mode.RESUME_EXTRACTIVE, Mode.DOCUMENT_EXTRACTIVE)
        if (
            decision.mode in extractive
            and decision.hit is not None
            and not language_matches(decision.hit.text, compiled.answer.language)
        ):
            # Exact text in another script than the session's language: translate, keep wording.
            decision = Decision(Mode.SYNTHESIS, decision.hit, reason="language")

        answer_routes.labels(mode=decision.mode.value, reason=decision.reason).inc()
        logger.info(
            "answer routed",
            extra={
                "question_id": str(prepared.question_id),
                "answer_id": str(prepared.answer_id),
                "router_mode": router_mode,
                "mode": decision.mode.value,
                "reason": decision.reason,
            },
        )
        if router_mode == "shadow":  # decision recorded only: the answer follows the old path
            return _Plan(hits)

        if decision.mode in extractive and decision.hit is not None:
            hit = decision.hit
            source = await section_source(self._sessionmaker, user_id, decision)
            if source is not None:
                budget = compiled.answer.target_max_words
                too_long = word_count(source) > budget * LENGTH_SLACK
                if decision.reason == "title" and too_long:
                    # The user named a section that is longer than their length setting: answer
                    # from the WHOLE section (complete, in the requested format, keeping its
                    # wording) instead of cutting the raw text off.
                    section_hit = replace(
                        hit, text=cut_at_sentence(source, settings.qa_section_context_max_chars)
                    )
                    wide = (
                        compiled  # a summary keeps the short band set above
                        if detail == "summary"
                        else PreferenceCompiler.from_settings(settings).with_word_budget(
                            compiled,
                            min_words=settings.answer_long_min_words,
                            max_words=max(budget, settings.qa_section_answer_max_words),
                        )
                    )
                    guards = await self._role_guard(user_id, prepared, question, [hit])
                    run.chunk_ids = [hit.chunk_id, *(g.chunk_id for g in guards)]
                    run.source_kind = "synthesis"
                    run.source_refs = self._refs_for([hit], ranked)
                    return _Plan(
                        [section_hit, *guards],
                        Mode.SYNTHESIS,
                        prepared=replace(prepared, compiled=wide),
                    )
                exact = fit_to_words(source, budget, terms, anchored_start=decision.anchored_start)
                if is_verbatim(exact, source):
                    run.chunk_ids = [hit.chunk_id]
                    run.source_kind = (
                        "resume" if decision.mode is Mode.RESUME_EXTRACTIVE else "document"
                    )
                    run.source_refs = self._refs_for([hit], ranked)
                    return _Plan([hit], decision.mode, exact)
            decision = Decision(Mode.SYNTHESIS, decision.hit, reason="unverified")
        if decision.mode is Mode.GENERAL:
            run.chunk_ids = []
            run.source_kind, run.source_refs = "general", []
            return _Plan([], Mode.GENERAL, prepared=prepared if detail == "summary" else None)
        run.source_kind, run.source_refs = "synthesis", self._refs_for(hits, ranked)
        guards = await self._role_guard(user_id, prepared, question, hits)
        run.chunk_ids = [*run.chunk_ids, *(g.chunk_id for g in guards)]
        return _Plan(
            [*hits, *guards],
            Mode.SYNTHESIS,
            prepared=prepared if detail == "summary" else None,
        )

    async def _role_guard(
        self,
        user_id: uuid.UUID,
        prepared: _Prepared,
        question: str,
        main: Sequence[ChunkHit],
    ) -> list[ChunkHit]:
        """Passages of the user's selected files that LIMIT the candidate's role on this topic
        ("I have not written the code myself"), sent along with a model-composed answer so it
        never claims what the candidate disowns elsewhere (D-66). Best effort: never blocks."""
        started = self._clock()
        topic = content_terms(" ".join([question, *(h.heading or "" for h in main[:2])]))
        try:
            guards = await find_caveats(
                self._sessionmaker,
                user_id,
                [prepared.resume_id, *prepared.document_ids],
                topic,
                exclude=[h.chunk_id for h in main],
            )
        except Exception as exc:  # noqa: BLE001 - an optional guard must never break an answer
            logger.warning("role guard skipped", extra={"error_type": type(exc).__name__})
            return []
        answer_stage_seconds.labels(stage="role_guard").observe(self._clock() - started)
        return guards

    async def _stream(
        self,
        user_id: uuid.UUID,
        prepared: _Prepared,
        prompt: BuiltPrompt,
        run: _Run,
        started: float,
        *,
        emit: bool,
    ) -> AsyncGenerator[QaEvent, None]:
        """Stream the model's answer into `run`. With `emit` False nothing is yielded: the text is
        only collected (the general path validates it before showing it)."""
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
                        if emit:
                            yield AnswerStarted(
                                prepared.answer_id,
                                prepared.question_id,
                                event.provider,
                                event.model,
                            )
                    elif isinstance(event, Token):
                        if emit:
                            if run.first_token_ms is None:
                                run.first_token_ms = self._ms(started)  # request -> first token
                            run.parts.append(event.text)
                            yield AnswerDelta(prepared.answer_id, len(run.parts) - 1, event.text)
                        else:  # held back: nothing is stored or shown until it is validated
                            run.draft.append(event.text)
                    elif isinstance(event, Usage):
                        run.usage = event
                    if self._clock() - started > self._generation_timeout:
                        raise _Failed(Timeout.code, Timeout.message, True)  # total budget
        except LLMError as err:
            raise _Failed(err.code, err.message, err.fallback_eligible) from None

    def _done(self, prepared: _Prepared, run: _Run, total_ms: int) -> AnswerDone:
        meta = run.meta
        if meta is None:  # callers guarantee a provider/answer source before completing
            raise _Failed(EMPTY_ANSWER, SAFE_MESSAGES[EMPTY_ANSWER], True)
        return AnswerDone(
            answer_id=prepared.answer_id,
            question_id=prepared.question_id,
            provider=meta.provider,
            model=meta.model,
            retrieval_ms=run.retrieval_ms or 0,
            first_token_ms=run.first_token_ms if run.first_token_ms is not None else total_ms,
            total_ms=total_ms,
            fallback_used=meta.fallback_used,
            input_tokens=run.usage.input_tokens if run.usage else None,
            output_tokens=run.usage.output_tokens if run.usage else None,
            source=source_payload(run.source_kind, run.source_refs),
        )

    async def _complete(
        self,
        user_id: uuid.UUID,
        prepared: _Prepared,
        run: _Run,
        started: float,
        evidence: Sequence[EvidenceRef] | None,
    ) -> AnswerDone:
        """Record a finished answer and build `answer.done`. Everything is already delivered, so
        nothing here changes the text."""
        if not "".join(run.parts).strip() or run.meta is None:
            raise _Failed(EMPTY_ANSWER, SAFE_MESSAGES[EMPTY_ANSWER], True)
        total_ms = self._ms(started)
        # The check scans the whole answer in pure Python (about a microsecond per character), so
        # it runs in a worker thread: on the event loop it would delay every other user's
        # requests for as long as it takes (SEC-33). It reads only its arguments.
        answer_text = "".join(run.parts)
        grounding_check = (
            await asyncio.to_thread(self._citation_check, evidence, answer_text, prepared)
            if evidence is not None
            else None
        )
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
                "source_kind": run.source_kind,
            },
        )
        return self._done(prepared, run, total_ms)

    async def _answer_exact(
        self, user_id: uuid.UUID, prepared: _Prepared, exact: str, run: _Run, started: float
    ) -> AsyncGenerator[QaEvent, None]:
        """The user's own wording, verbatim: no provider is called."""
        kind = run.source_kind or "document"
        run.meta = StreamMeta("extractive", kind, False)
        yield AnswerStarted(prepared.answer_id, prepared.question_id, "extractive", kind)
        run.first_token_ms = self._ms(started)
        for piece in split_for_streaming(exact):
            run.parts.append(piece)
            yield AnswerDelta(prepared.answer_id, len(run.parts) - 1, piece)
        answer_stage_seconds.labels(stage="extractive").observe(self._clock() - started)
        yield await self._complete(user_id, prepared, run, started, None)

    async def _emit_buffered(
        self, prepared: _Prepared, run: _Run, started: float, text: str
    ) -> AsyncGenerator[QaEvent, None]:
        """Show an answer that was held back for validation."""
        meta = run.meta
        if meta is None:
            raise _Failed(EMPTY_ANSWER, SAFE_MESSAGES[EMPTY_ANSWER], True)
        run.parts = []
        yield AnswerStarted(prepared.answer_id, prepared.question_id, meta.provider, meta.model)
        run.first_token_ms = self._ms(started)  # the user sees text from here, not before
        for piece in split_for_streaming(text):
            run.parts.append(piece)
            yield AnswerDelta(prepared.answer_id, len(run.parts) - 1, piece)

    async def _answer_general(
        self,
        user_id: uuid.UUID,
        prepared: _Prepared,
        question: str,
        run: _Run,
        started: float,
    ) -> AsyncGenerator[QaEvent, None]:
        """General-knowledge answer for a question neither the resume nor a document covers. With
        `QA_VALIDATE_GENERAL=on` it is checked before it is shown: deterministic rules, then a fast
        judge model, one regeneration on failure, else a safe error."""
        settings = self._settings
        compiled = prepared.compiled
        validate = settings.qa_validate_general == "on"
        selection = self._safe_selection(compiled)
        key: str | None = None
        if validate and self._cache is not None and self._cache.enabled:
            key = cache_key(
                user_id,
                prepared.session_id,
                question,
                digest(compiled.to_dict()),
                PROMPT_VERSION,
                [prepared.resume_id, *prepared.document_ids],
            )
            cached = await self._cache.get(key)
            if cached is not None:
                run.meta = StreamMeta(
                    str(cached.get("provider") or "cache"), str(cached.get("model") or ""), False
                )
                run.source_refs = [{"validated": bool(cached.get("validated"))}]
                async for event in self._emit_buffered(prepared, run, started, cached["text"]):
                    yield event
                yield await self._complete(user_id, prepared, run, started, None)
                return

        attempt_question = question
        for _attempt in range(2 if validate else 1):
            prompt = self._prompt(prepared, attempt_question, [])
            run.parts, run.draft = [], []
            run.meta, run.first_token_ms, run.usage = None, None, None
            if not validate:
                async for event in self._stream(user_id, prepared, prompt, run, started, emit=True):
                    yield event
                break
            generation_started = self._clock()
            async for _ in self._stream(user_id, prepared, prompt, run, started, emit=False):
                pass
            answer_stage_seconds.labels(stage="general").observe(self._clock() - generation_started)
            text = "".join(run.draft)
            validation_started = self._clock()
            verdict = check_deterministic(
                text,
                language=compiled.answer.language,
                max_words=compiled.answer.target_max_words,
            )
            if verdict.ok:
                verdict = await judge_answer(
                    self._router,
                    selection,
                    question,
                    text,
                    timeout_seconds=settings.qa_judge_timeout_seconds,
                    user_id=user_id,
                )
            answer_stage_seconds.labels(stage="validation").observe(
                self._clock() - validation_started
            )
            if verdict.ok:
                run.source_refs = [{"validated": verdict.judged}]
                async for event in self._emit_buffered(prepared, run, started, text):
                    yield event
                if key is not None and self._cache is not None and run.meta is not None:
                    await self._cache.put(
                        key,
                        {
                            "text": text,
                            "provider": run.meta.provider,
                            "model": run.meta.model,
                            "validated": verdict.judged,
                        },
                        user_id=user_id,
                        session_id=prepared.session_id,
                        source_ids=[prepared.resume_id, *prepared.document_ids],
                    )
                break
            attempt_question = retry_question(question, verdict.reason)
        else:
            run.draft = []  # a rejected draft is discarded, never stored or shown
            raise _Failed(ANSWER_NOT_VALIDATED, SAFE_MESSAGES_EXTRA[ANSWER_NOT_VALIDATED], True)
        yield await self._complete(user_id, prepared, run, started, None)

    # -------------------------------------------------------------------- main

    async def ask(
        self,
        user_id: uuid.UUID,
        session_id: uuid.UUID,
        text: str,
        question_type_override: str | None = None,
        input_source: str = "typed",
        detail: str | None = None,
    ) -> AsyncGenerator[QaEvent, None]:
        started = self._clock()
        question = self._clean_question(text)
        override = self._parse_override(question_type_override)
        if input_source not in (
            "typed",
            "screenshot",
            "audio",
        ):  # never trust a caller-supplied value
            raise AppError(422, "validation_error", "input_source is not valid.")
        if detail not in (None, "full", "summary"):
            raise AppError(422, "validation_error", "detail is not valid.")
        prepared = await self._prepare(user_id, session_id, question, override, input_source)
        run = _Run()
        try:
            yield QuestionCreated(prepared.question_id, prepared.answer_id)

            # --- retrieval + routing (own short transactions; sources come from the session)
            plan = await self._plan(
                user_id, prepared, question, run, detail or self._settings.qa_default_detail
            )
            yield RetrievalDone(prepared.question_id, len(plan.hits), run.retrieval_ms or 0)

            # --- answer: no database session is open from here until the finish
            if plan.exact is not None:  # exact text of the resume / a document: no model call
                async for event in self._answer_exact(user_id, prepared, plan.exact, run, started):
                    yield event
            elif plan.mode is Mode.GENERAL:  # neither source covers it: validated general answer
                writer = plan.prepared or prepared  # a summary uses the short length band
                async for event in self._answer_general(user_id, writer, question, run, started):
                    yield event
            else:  # related passages (synthesis) or the previous pipeline
                writer = plan.prepared or prepared  # a whole-section answer has a larger budget
                prompt = self._prompt(writer, question, plan.hits)
                async for event in self._stream(user_id, writer, prompt, run, started, emit=True):
                    yield event
                yield await self._complete(user_id, prepared, run, started, prompt.evidence)
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
