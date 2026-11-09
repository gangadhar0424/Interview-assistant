"""Router: selection policy, fallback on injected failures, circuit breaker, streaming, metering."""

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest

from app.ai.errors import (
    AuthError,
    ContentFiltered,
    InvalidSelection,
    LLMError,
    RateLimited,
    Timeout,
    Unavailable,
)
from app.ai.fake import FAKE_MODEL_ID, FakeLLMProvider, fake_spec
from app.ai.registry import ProviderRegistry, RegisteredProvider
from app.ai.router import LLMRouter, RouterConfig, Selection
from app.ai.types import (
    Completion,
    GenerationConfig,
    Message,
    RouterEvent,
    StreamEvent,
    StreamMeta,
    Token,
    Usage,
)
from app.ai.usage import LoggingUsageSink, MemoryUsageSink

CFG = GenerationConfig()
MSGS = [Message("user", "secret resume text about Kubernetes")]


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def build(
    providers: list[FakeLLMProvider],
    chain: list[str] | None = None,
    config: RouterConfig | None = None,
    clock: Clock | None = None,
) -> tuple[LLMRouter, MemoryUsageSink]:
    registry = ProviderRegistry(
        [RegisteredProvider(p, fake_spec(p.provider_id)) for p in providers]
    )
    sink = MemoryUsageSink()
    kwargs: dict[str, Any] = {"clock": clock} if clock else {}
    router = LLMRouter(
        registry, chain or registry.ids(), config or RouterConfig(), usage_sink=sink, **kwargs
    )
    return router, sink


def complete(router: LLMRouter, **kw: Any) -> Completion:
    return asyncio.run(router.complete(MSGS, CFG, **kw))


def stream(router: LLMRouter, **kw: Any) -> list[RouterEvent]:
    async def go() -> list[RouterEvent]:
        return [e async for e in router.stream_chat(MSGS, CFG, **kw)]

    return asyncio.run(go())


# ------------------------------------------------------------ selection / policy


def test_user_selection_is_tried_first_then_chain() -> None:
    a, b = FakeLLMProvider("a"), FakeLLMProvider("b")
    router, _ = build([a, b])
    result = complete(router, selection=Selection("b", FAKE_MODEL_ID))
    assert result.provider == "b" and (a.calls, b.calls) == (0, 1)


def test_default_order_is_chain_order() -> None:
    a, b = FakeLLMProvider("a"), FakeLLMProvider("b")
    router, _ = build([a, b], chain=["b", "a"])
    assert complete(router).provider == "b"


@pytest.mark.parametrize(
    "selection", [Selection("nope", FAKE_MODEL_ID), Selection("a", "not-a-model")]
)
def test_selection_outside_allowlist_is_rejected_without_calling_any_provider(
    selection: Selection,
) -> None:
    a = FakeLLMProvider("a")
    router, sink = build([a])
    with pytest.raises(InvalidSelection):
        complete(router, selection=selection)
    assert a.calls == 0 and sink.records == []


def test_no_providers_enabled_is_a_safe_unavailable_error() -> None:
    router, _ = build([])
    with pytest.raises(Unavailable):
        complete(router)


# ------------------------------------------------------------------- fallback


@pytest.mark.parametrize("error", [Timeout, Unavailable, RateLimited, AuthError])
def test_falls_back_on_injected_failures(error: type[LLMError]) -> None:
    a, b = FakeLLMProvider("a", failures=[error()]), FakeLLMProvider("b")
    router, sink = build([a, b])
    assert complete(router).provider == "b"
    assert [(r.provider, r.ok) for r in sink.records] == [("a", False), ("b", True)]
    assert sink.records[0].error_code == error.code


def test_content_filtered_never_falls_back() -> None:
    a, b = FakeLLMProvider("a", failures=[ContentFiltered()]), FakeLLMProvider("b")
    router, _ = build([a, b])
    with pytest.raises(ContentFiltered):
        complete(router)
    assert b.calls == 0


def test_all_providers_failing_raises_the_last_taxonomy_error() -> None:
    a = FakeLLMProvider("a", failures=[Unavailable()])
    b = FakeLLMProvider("b", failures=[RateLimited()])
    router, _ = build([a, b])
    with pytest.raises(RateLimited):
        complete(router)


def test_unexpected_provider_exception_is_hidden_behind_a_safe_error() -> None:
    class Broken(FakeLLMProvider):
        async def complete(
            self, messages: Sequence[Message], model: str, config: GenerationConfig
        ) -> Completion:
            raise RuntimeError("sk-LEAKED-KEY and prompt text")

    router, _ = build([Broken("a")])
    with pytest.raises(Unavailable) as exc:
        complete(router)
    assert "LEAKED" not in str(exc.value) and "LEAKED" not in exc.value.message


def test_request_timeout_falls_back() -> None:
    a, b = FakeLLMProvider("a", first_token_delay=0.5), FakeLLMProvider("b")
    router, _ = build([a, b], config=RouterConfig(request_timeout_seconds=0.05))
    assert complete(router).provider == "b"


# -------------------------------------------------------------------- streaming


def test_stream_yields_tokens_then_usage_and_records_usage() -> None:
    router, sink = build([FakeLLMProvider("a")])
    uid = uuid.uuid4()
    events = stream(router, user_id=uid)
    assert isinstance(events[-1], Usage)
    assert "".join(e.text for e in events if isinstance(e, Token)) == "echo: " + MSGS[0].content
    assert len(sink.records) == 1 and sink.records[0].user_id == uid and sink.records[0].ok
    output_tokens = sink.records[0].output_tokens
    assert output_tokens is not None and output_tokens > 0  # the fake provider reports usage


def test_first_token_timeout_triggers_fallback() -> None:
    a, b = FakeLLMProvider("a", first_token_delay=0.5), FakeLLMProvider("b")
    router, sink = build([a, b], config=RouterConfig(first_token_timeout_seconds=0.05))
    events = stream(router)
    assert any(isinstance(e, Token) for e in events)
    assert [(r.provider, r.error_code) for r in sink.records] == [("a", "llm_timeout"), ("b", None)]


def test_stream_failure_before_first_token_falls_back() -> None:
    a, b = FakeLLMProvider("a", failures=[Unavailable()]), FakeLLMProvider("b")
    router, _ = build([a, b])
    text = "".join(e.text for e in stream(router) if isinstance(e, Token))
    assert text.startswith("echo:") and b.calls == 1


def test_stream_failure_after_tokens_is_raised_not_replayed_elsewhere() -> None:
    a = FakeLLMProvider("a", failures=[Unavailable()], fail_after_tokens=1)
    b = FakeLLMProvider("b")
    router, _ = build([a, b])
    seen: list[RouterEvent] = []

    async def go() -> None:
        async for e in router.stream_chat(MSGS, CFG):
            seen.append(e)

    with pytest.raises(Unavailable):
        asyncio.run(go())
    assert seen and b.calls == 0  # partial output was delivered; no second answer is spliced in


def test_stream_idle_timeout_after_first_token_is_an_error() -> None:
    class Stalls(FakeLLMProvider):
        async def stream_chat(
            self, messages: Sequence[Message], model: str, config: GenerationConfig
        ) -> AsyncIterator[StreamEvent]:
            yield Token("hi")
            await asyncio.sleep(1)
            yield Token("never")

    router, _ = build(
        [Stalls("a"), FakeLLMProvider("b")], config=RouterConfig(stream_idle_timeout_seconds=0.05)
    )
    with pytest.raises(Timeout):
        stream(router)


# ---------------------------------------------------------------- circuit breaker


def _breaker_router(clock: Clock, failures: int) -> tuple[LLMRouter, FakeLLMProvider]:
    a = FakeLLMProvider("a", failures=[Unavailable()] * failures)
    cfg = RouterConfig(breaker_failure_threshold=2, breaker_cooldown_seconds=30)
    router, _ = build([a, FakeLLMProvider("b")], config=cfg, clock=clock)
    return router, a


def test_breaker_opens_after_threshold_and_skips_the_provider() -> None:
    clock = Clock()
    router, a = _breaker_router(clock, failures=10)
    complete(router)
    complete(router)  # second consecutive failure opens the breaker
    assert router.breaker_state("a") == "open"
    calls_before = a.calls
    assert complete(router).provider == "b"
    assert a.calls == calls_before  # not even attempted while open


def test_breaker_half_open_probe_recovers_the_provider() -> None:
    clock = Clock()
    router, _ = _breaker_router(clock, failures=2)  # fails twice, then healthy
    complete(router)
    complete(router)
    assert router.breaker_state("a") == "open"
    clock.now += 31
    assert router.breaker_state("a") == "half_open"
    assert complete(router).provider == "a"  # the probe succeeds
    assert router.breaker_state("a") == "closed"


def test_breaker_half_open_failure_reopens() -> None:
    clock = Clock()
    router, _ = _breaker_router(clock, failures=3)
    complete(router)
    complete(router)
    clock.now += 31
    assert complete(router).provider == "b"  # probe to "a" fails, falls back
    assert router.breaker_state("a") == "open"


def test_content_filter_does_not_open_the_breaker() -> None:
    a = FakeLLMProvider("a", failures=[ContentFiltered()] * 5)
    router, _ = build([a], config=RouterConfig(breaker_failure_threshold=2))
    for _ in range(4):
        with pytest.raises(ContentFiltered):
            complete(router)
    assert router.breaker_state("a") == "closed"


def test_abandoned_half_open_stream_does_not_wedge_the_breaker() -> None:
    clock = Clock()
    router, _ = _breaker_router(clock, failures=2)
    complete(router)
    complete(router)
    clock.now += 31

    async def take_one() -> None:
        agen = router.stream_chat(MSGS, CFG)
        await anext(agen)
        await agen.aclose()  # consumer walks away mid-stream

    asyncio.run(take_one())
    assert complete(router).provider == "a"  # a new probe is still allowed


# ---------------------------------------------------------------------- privacy


def test_usage_logs_contain_no_prompt_text(caplog: pytest.LogCaptureFixture) -> None:
    registry = ProviderRegistry([RegisteredProvider(FakeLLMProvider("a"), fake_spec("a"))])
    router = LLMRouter(registry, ["a"], usage_sink=LoggingUsageSink())
    with caplog.at_level(logging.DEBUG):
        complete(router)
    assert "llm_usage" in caplog.text
    assert "Kubernetes" not in caplog.text and "secret resume" not in caplog.text


# ------------------------------------------------- stream metadata (Phase 8A)


def metas(events: list[RouterEvent]) -> list[StreamMeta]:
    return [e for e in events if isinstance(e, StreamMeta)]


def test_stream_reports_the_answering_provider_before_the_first_token() -> None:
    router, _ = build([FakeLLMProvider("a"), FakeLLMProvider("b")])
    events = stream(router)
    assert metas(events) == [StreamMeta("a", FAKE_MODEL_ID, fallback_used=False)]
    first_token = next(i for i, e in enumerate(events) if isinstance(e, Token))
    assert isinstance(events[first_token - 1], StreamMeta)  # immediately before token one
    assert isinstance(events[-1], Usage)  # existing contract: usage still comes last


def test_fallback_is_reported_when_a_later_provider_answers() -> None:
    a, b = FakeLLMProvider("a", failures=[Unavailable()]), FakeLLMProvider("b")
    router, _ = build([a, b])
    assert metas(stream(router)) == [StreamMeta("b", FAKE_MODEL_ID, fallback_used=True)]


def test_a_user_selected_provider_that_answers_is_not_a_fallback() -> None:
    router, _ = build([FakeLLMProvider("a"), FakeLLMProvider("b")])
    events = stream(router, selection=Selection("b", FAKE_MODEL_ID))
    assert metas(events) == [StreamMeta("b", FAKE_MODEL_ID, fallback_used=False)]


def test_a_provider_skipped_by_an_open_breaker_counts_as_a_fallback() -> None:
    clock = Clock()
    a = FakeLLMProvider("a", failures=[Unavailable()] * 10)
    cfg = RouterConfig(breaker_failure_threshold=2, breaker_cooldown_seconds=30)
    router, _ = build([a, FakeLLMProvider("b")], config=cfg, clock=clock)
    stream(router)
    stream(router)  # second failure opens a's breaker
    assert router.breaker_state("a") == "open"
    assert metas(stream(router)) == [StreamMeta("b", FAKE_MODEL_ID, fallback_used=True)]


def test_stream_meta_is_sent_exactly_once_per_stream() -> None:
    router, _ = build([FakeLLMProvider("a")])
    assert len(metas(stream(router))) == 1


def test_a_stream_without_tokens_still_reports_the_provider() -> None:
    class Silent(FakeLLMProvider):
        async def stream_chat(
            self, messages: Sequence[Message], model: str, config: GenerationConfig
        ) -> AsyncIterator[StreamEvent]:
            yield Usage(1, 0)

    events = stream(build([Silent("a")])[0])
    assert metas(events) == [StreamMeta("a", FAKE_MODEL_ID, fallback_used=False)]
    assert isinstance(events[-1], Usage)


def test_no_meta_is_emitted_when_every_provider_fails() -> None:
    router, _ = build([FakeLLMProvider("a", failures=[Unavailable()])])
    seen: list[RouterEvent] = []

    async def go() -> None:
        async for e in router.stream_chat(MSGS, CFG):
            seen.append(e)

    with pytest.raises(Unavailable):
        asyncio.run(go())
    assert seen == []


def test_the_router_exposes_its_registry_read_only() -> None:
    router, _ = build([FakeLLMProvider("a")])
    assert router.registry.ids() == ["a"]
