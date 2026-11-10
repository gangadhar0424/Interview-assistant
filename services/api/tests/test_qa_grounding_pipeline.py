"""Citation-integrity check inside `QuestionAnswerer.ask` (Issue 2), without a database.

Preparation, retrieval and the final write are replaced by in-memory doubles; the real
PromptBuilder, router, fake provider and orchestration run. The check is record-and-observe: it
must never delay, alter, block or retract the streamed answer, and it must never fail an answer.
"""

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest

from app.ai.fake import FakeLLMProvider, fake_spec
from app.ai.registry import ProviderRegistry, RegisteredProvider
from app.ai.router import LLMRouter
from app.ai.types import GenerationConfig, Message, StreamEvent, Token, Usage
from app.ai.usage import MemoryUsageSink
from app.core.config import get_settings
from app.core.metrics import answer_grounding_checks
from app.embeddings.fake import FakeEmbeddingProvider
from app.preferences.compiler import PreferenceCompiler, PreferencesInput
from app.qa import repository
from app.qa import service as service_module
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
from app.qa.service import QuestionAnswerer, _Prepared
from app.retrieval.service import ChunkHit
from tests.qa_fakes import BLOCK

USER = uuid.uuid4()
SESSION = uuid.uuid4()
RESUME = uuid.uuid4()
SOURCE_1 = uuid.UUID("9db2f044-7514-4a2d-b748-9ce604418f3b")
SOURCE_2 = uuid.UUID("11111111-2222-4333-8444-555555555555")
CANARY = "PRIVATE-ANSWER-CANARY-7f3a"
EXCERPT = "Led the billing migration to PostgreSQL at Globex."
QUESTION_TEXT = "PRIVATE-QUESTION-CANARY-91bc tell me about your work"


def hit(source_type: str, source_id: uuid.UUID, index: int, text: str) -> ChunkHit:
    return ChunkHit(uuid.uuid4(), source_type, source_id, index, text, 0.5)


HITS = [
    hit("resume", SOURCE_1, 0, EXCERPT),
    hit("document", SOURCE_2, 5, "Reduced p99 latency from 900 ms to 240 ms."),
]


class ScriptedTokens(FakeLLMProvider):
    """Streams exactly the given tokens (so a tag can be split across deltas)."""

    def __init__(self, tokens: Sequence[str], *, fail_after: bool = False) -> None:
        super().__init__("scripted")
        self.tokens = list(tokens)
        self.fail_after = fail_after

    async def stream_chat(
        self, messages: Sequence[Message], model: str, config: GenerationConfig
    ) -> AsyncIterator[StreamEvent]:
        self.calls += 1
        self.last_messages = list(messages)
        for token in self.tokens:
            yield Token(token)
        if self.fail_after:
            raise RuntimeError("provider exploded")
        yield Usage(3, len(self.tokens))


class _Db:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *exc: object) -> bool:
        return False


class Harness:
    """One answerer with the database-facing steps replaced by in-memory doubles."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, provider: FakeLLMProvider) -> None:
        self.provider = provider
        self.finished: list[repository.AnswerResult] = []
        self.events: list[QaEvent] = []
        settings = get_settings()
        registry = ProviderRegistry([RegisteredProvider(provider, fake_spec(provider.provider_id))])
        router = LLMRouter(registry, registry.ids(), None, usage_sink=MemoryUsageSink())
        self.answerer = QuestionAnswerer(
            lambda: _Db(),  # type: ignore[arg-type]
            settings,
            router,
            FakeEmbeddingProvider(),
        )
        compiled = PreferenceCompiler.from_settings(settings).compile(
            PreferencesInput("en", "behavioral", "full_script", "balanced", "simple")
        )
        prepared = _Prepared(
            session_id=SESSION,
            question_id=uuid.uuid4(),
            answer_id=uuid.uuid4(),
            company_name="Acme",
            job_description="Backend engineer",
            resume_id=RESUME,
            document_ids=[],
            compiled=compiled,
            history=[],
        )
        self.prepared = prepared

        async def prepare(*args: object) -> _Prepared:
            return prepared

        async def retrieve(*args: object, **kwargs: object) -> list[ChunkHit]:
            return list(HITS)

        async def no_tenant(*args: object) -> None:
            return None

        async def finish(
            user_id: uuid.UUID,
            prep: _Prepared,
            result: repository.AnswerResult,
            *,
            answered: bool = False,
        ) -> None:
            self.finished.append(result)

        monkeypatch.setattr(self.answerer, "_prepare", prepare)
        monkeypatch.setattr(self.answerer, "_finish", finish)
        monkeypatch.setattr(service_module, "retrieve_context", retrieve)
        monkeypatch.setattr(service_module, "set_tenant_context", no_tenant)

    def ask(self, *, stop_after: Any = None) -> list[QaEvent]:
        async def go() -> list[QaEvent]:
            agen = self.answerer.ask(USER, SESSION, QUESTION_TEXT)
            try:
                async for event in agen:
                    self.events.append(event)
                    if stop_after is not None and stop_after(event):
                        break
            finally:
                await agen.aclose()
            return self.events

        return asyncio.run(go())

    @property
    def deltas(self) -> list[str]:
        return [e.text for e in self.events if isinstance(e, AnswerDelta)]

    @property
    def result(self) -> repository.AnswerResult:
        assert len(self.finished) == 1
        return self.finished[0]


def harness(monkeypatch: pytest.MonkeyPatch, *tokens: str, **kw: bool) -> Harness:
    return Harness(monkeypatch, ScriptedTokens(tokens, **kw))


def status_count(status: str) -> float:
    return float(answer_grounding_checks.labels(status=status)._value.get())


TOKENS_VALID = ("I led the ", "billing migration. ", "Evidence (not spoken): ", "[E", "1", "]")
TOKENS_FORGED = ("I founded a company. ", "Evidence (not spoken): ", "[E", "9", "]")
TOKENS_MALFORMED = ("I led it. ", "Evidence (not spoken): [E1, E2]")
TOKENS_UNCITED = ("I founded a company and led forty engineers there.",)


# ---------------------------------------------------------------- recorded outcomes


def test_a_valid_citation_is_recorded_as_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    h = harness(monkeypatch, *TOKENS_VALID)
    before = status_count("ok")
    events = h.ask()
    assert isinstance(events[-1], AnswerDone)
    assert h.result.status == "complete"
    assert h.result.text == "".join(TOKENS_VALID)
    assert h.result.grounding_check == {
        "v": 1,
        "status": "ok",
        "shown_count": 2,
        "valid_ids": [1],
        "unknown_ids": [],
        "malformed_count": 0,
    }
    assert status_count("ok") == before + 1


def test_a_forged_id_is_recorded_but_the_answer_still_completes_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = harness(monkeypatch, *TOKENS_FORGED)
    before = status_count("invalid_citations")
    events = h.ask()
    assert isinstance(events[-1], AnswerDone) and not any(
        isinstance(e, AnswerFailed) for e in events
    )
    assert h.result.status == "complete"
    assert h.result.text == "".join(TOKENS_FORGED)  # not rewritten, not redacted
    check = h.result.grounding_check
    assert check is not None
    assert (check["status"], check["valid_ids"], check["unknown_ids"]) == (
        "invalid_citations",
        [],
        [9],
    )
    assert status_count("invalid_citations") == before + 1


def test_a_malformed_tag_is_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    h = harness(monkeypatch, *TOKENS_MALFORMED)
    h.ask()
    assert h.result.status == "complete"
    assert h.result.grounding_check is not None
    assert h.result.grounding_check["status"] == "invalid_citations"
    assert h.result.grounding_check["malformed_count"] == 1


def test_an_uncited_answer_is_informational_and_not_judged(monkeypatch: pytest.MonkeyPatch) -> None:
    # The check cannot tell that this claim is unsupported: it only reports that no tag exists.
    h = harness(monkeypatch, *TOKENS_UNCITED)
    h.ask()
    assert h.result.status == "complete"
    assert h.result.grounding_check is not None
    assert h.result.grounding_check["status"] == "no_citations"


def test_the_check_receives_exactly_the_evidence_the_prompt_rendered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = harness(monkeypatch, *TOKENS_VALID)
    seen: list[Any] = []
    real = check_citations

    def spy(text: str, evidence: Sequence[Any]) -> GroundingCheck:
        seen.append((text, tuple(evidence)))
        return real(text, evidence)

    monkeypatch.setattr(service_module, "check_citations", spy)
    h.ask()
    assert len(seen) == 1
    text, evidence = seen[0]
    assert text == "".join(TOKENS_VALID)  # the complete accumulated text, not a fragment
    material = dict(BLOCK.findall(h.provider.last_messages[-1].content))["candidate_material"]  # type: ignore[attr-defined]
    for ref in evidence:
        assert (
            f"[E{ref.tag} | {ref.source_type} | source {ref.source_id} | chunk {ref.chunk_index}]"
            in material
        )
    assert [ref.tag for ref in evidence] == [1, 2]


# -------------------------------------------------------- streaming is not touched


def test_streamed_deltas_and_event_order_are_identical_whatever_the_check_finds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs: dict[str, Harness] = {}
    for name, tokens in (
        ("valid", TOKENS_VALID),
        ("forged", TOKENS_FORGED),
        ("malformed", TOKENS_MALFORMED),
        ("uncited", TOKENS_UNCITED),
    ):
        runs[name] = harness(monkeypatch, *tokens)
        runs[name].ask()
        assert runs[name].deltas == list(tokens)  # one delta per token, in order, unmodified
        kinds = [type(e) for e in runs[name].events]
        assert kinds[:4] == [QuestionCreated, RetrievalDone, AnswerStarted, AnswerDelta]
        assert kinds[-1] is AnswerDone
        assert [e.index for e in runs[name].events if isinstance(e, AnswerDelta)] == list(
            range(len(tokens))
        )


def test_the_check_runs_only_after_every_delta_has_been_delivered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = harness(monkeypatch, *TOKENS_VALID)
    delivered_when_checked: list[int] = []
    real = check_citations

    def spy(text: str, evidence: Sequence[Any]) -> GroundingCheck:
        delivered_when_checked.append(len(h.deltas))  # what the consumer already has
        return real(text, evidence)

    monkeypatch.setattr(service_module, "check_citations", spy)
    h.ask()
    assert delivered_when_checked == [len(TOKENS_VALID)]  # all of it, and nothing was held back


def test_events_are_the_same_with_the_check_working_or_broken(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    working = harness(monkeypatch, *TOKENS_FORGED)
    working.ask()
    broken = harness(monkeypatch, *TOKENS_FORGED)

    def explode(text: str, evidence: Sequence[Any]) -> GroundingCheck:
        raise RuntimeError("boom")

    monkeypatch.setattr(service_module, "check_citations", explode)
    broken.ask()

    def shape(h: Harness) -> list[tuple[str, object]]:
        return [
            (type(e).__name__, e.text if isinstance(e, AnswerDelta) else None) for e in h.events
        ]

    assert shape(working) == shape(broken)
    assert working.result.text == broken.result.text


# ------------------------------------------------------------ failure isolation


def test_a_failing_check_never_fails_the_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    h = harness(monkeypatch, *TOKENS_VALID)
    before = status_count("check_error")

    def explode(text: str, evidence: Sequence[Any]) -> GroundingCheck:
        raise ValueError(f"validator bug while reading {text}")

    monkeypatch.setattr(service_module, "check_citations", explode)
    events = h.ask()
    assert isinstance(events[-1], AnswerDone) and not any(
        isinstance(e, AnswerFailed) for e in events
    )
    assert h.result.status == "complete"
    assert h.result.text == "".join(TOKENS_VALID)
    assert h.result.grounding_check == {
        "v": 1,
        "status": "check_error",
        "shown_count": 2,
        "valid_ids": [],
        "unknown_ids": [],
        "malformed_count": 0,
    }
    assert status_count("check_error") == before + 1


def test_a_failing_metric_or_log_still_leaves_a_complete_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = harness(monkeypatch, *TOKENS_VALID)

    class BrokenCounter:
        def labels(self, **kw: object) -> "BrokenCounter":
            raise RuntimeError("metrics backend down")

    monkeypatch.setattr(service_module, "answer_grounding_checks", BrokenCounter())
    events = h.ask()
    assert isinstance(events[-1], AnswerDone)
    assert h.result.status == "complete"
    assert h.result.grounding_check is not None
    assert h.result.grounding_check["status"] == "check_error"


# ------------------------------------------------- answers that are not checked


def test_a_cancelled_answer_is_not_checked_and_keeps_its_partial_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = harness(monkeypatch, *TOKENS_VALID)
    called: list[bool] = []
    monkeypatch.setattr(
        service_module,
        "check_citations",
        lambda *a: called.append(True),
    )
    h.ask(stop_after=lambda e: isinstance(e, AnswerDelta) and e.index == 1)
    assert not called
    assert h.result.status == "cancelled" and h.result.grounding_check is None
    assert h.result.text == "".join(TOKENS_VALID[:2])


def test_a_failed_answer_is_not_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    h = harness(monkeypatch, *TOKENS_VALID, fail_after=True)
    called: list[bool] = []
    monkeypatch.setattr(
        service_module,
        "check_citations",
        lambda *a: called.append(True),
    )
    events = h.ask()
    assert isinstance(events[-1], AnswerFailed)
    assert not called
    assert h.result.status == "error" and h.result.grounding_check is None


def test_an_empty_answer_is_an_error_and_is_not_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    h = harness(monkeypatch, "   ")
    events = h.ask()
    assert isinstance(events[-1], AnswerFailed)
    assert h.result.status == "error" and h.result.grounding_check is None


# ------------------------------------------------------------------- privacy


def test_logs_and_metrics_carry_counts_only_never_answer_or_question_text(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    tokens = (f"{CANARY} {EXCERPT} ", "Evidence (not spoken): [E1], [E9], [E2, E3]")
    h = harness(monkeypatch, *tokens)
    with caplog.at_level(logging.DEBUG):
        h.ask()
    assert any(r.getMessage() == "citation integrity checked" for r in caplog.records)
    everything = caplog.text + "".join(repr(r.__dict__) for r in caplog.records)
    for secret in (CANARY, EXCERPT, QUESTION_TEXT, "PRIVATE-QUESTION-CANARY", str(SOURCE_1)):
        assert secret not in everything
    record = next(r for r in caplog.records if r.getMessage() == "citation integrity checked")
    assert (record.grounding_status, record.valid_count, record.unknown_count) == (  # type: ignore[attr-defined]
        "invalid_citations",
        1,
        2,  # E9, and E3 inside the malformed `[E2, E3]` (only E1 and E2 were rendered)
    )
    stored = h.result.grounding_check
    assert stored is not None and CANARY not in repr(stored) and EXCERPT not in repr(stored)


def test_a_check_failure_logs_only_the_error_type(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    h = harness(monkeypatch, f"{CANARY} ", "[E1]")

    def explode(text: str, evidence: Sequence[Any]) -> GroundingCheck:
        raise ValueError(f"cannot parse {text}")

    monkeypatch.setattr(service_module, "check_citations", explode)
    with caplog.at_level(logging.DEBUG):
        h.ask()
    everything = caplog.text + "".join(repr(r.__dict__) for r in caplog.records)
    assert CANARY not in everything and "cannot parse" not in everything
    failure = next(r for r in caplog.records if r.getMessage() == "citation integrity check failed")
    assert failure.error_type == "ValueError"  # type: ignore[attr-defined]


def test_the_check_is_not_part_of_any_client_facing_schema() -> None:
    from app.qa.schemas import AnswerOut
    from app.realtime import schemas as proto

    assert "grounding_check" not in AnswerOut.model_fields
    for model in (proto.AnswerDonePayload, proto.AnswerDeltaPayload):
        assert not any("ground" in name for name in model.model_fields)
