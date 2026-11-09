"""Deterministic OpenAI adapter tests using the official SDK and an in-memory HTTP transport."""

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest

from app.ai.adapters.openai import OpenAIProvider, openai_spec
from app.ai.errors import (
    AuthError,
    ContentFiltered,
    InvalidSelection,
    RateLimited,
    Timeout,
    Unavailable,
)
from app.ai.fake import FakeLLMProvider, fake_spec
from app.ai.registry import ProviderRegistry, RegisteredProvider, build_registry
from app.ai.router import LLMRouter, RouterConfig
from app.ai.types import Completion, GenerationConfig, Message, StreamMeta, Token, Usage
from app.ai.usage import MemoryUsageSink

MODEL = "gpt-4o-mini"
MESSAGES = [Message("system", "Be concise."), Message("user", "Say hello.")]
CONFIG = GenerationConfig(temperature=0.2, max_output_tokens=64)


def completion_response(
    *,
    content: str = "Hello!",
    usage: dict[str, int] | None = None,
    choices: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1,
        "model": MODEL,
        "choices": choices
        if choices is not None
        else [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content, "refusal": None},
                "finish_reason": "stop",
                "logprobs": None,
            }
        ],
        "usage": usage
        if usage is not None
        else {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
    }


def stream_body(*, include_usage: bool = True) -> bytes:
    chunks = [
        {
            "id": "chatcmpl-test",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": MODEL,
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": "Hello"},
                    "finish_reason": None,
                }
            ],
        },
        {
            "id": "chatcmpl-test",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": MODEL,
            "choices": [{"index": 0, "delta": {"content": " there!"}, "finish_reason": None}],
        },
        {
            "id": "chatcmpl-test",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": MODEL,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        },
    ]
    if include_usage:
        chunks.append(
            {
                "id": "chatcmpl-test",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": MODEL,
                "choices": [],
                "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
            }
        )
    return (
        b"".join(b"data: " + json.dumps(chunk).encode() + b"\n\n" for chunk in chunks)
        + b"data: [DONE]\n\n"
    )


def make_provider(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    model: str = MODEL,
    base_url: str = "https://api.openai.com/v1",
) -> OpenAIProvider:
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OpenAIProvider("test-key", model, 5, base_url=base_url, http_client=http_client)


def test_complete_maps_messages_and_generation_settings() -> None:
    captured: dict[str, Any] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers["authorization"]
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=completion_response())

    provider = make_provider(handle)
    try:
        result = asyncio.run(provider.complete(MESSAGES, MODEL, CONFIG))
    finally:
        asyncio.run(provider.aclose())

    assert result == Completion("Hello!", Usage(4, 2), "openai", MODEL)
    assert captured["authorization"] == "Bearer test-key"
    assert captured["body"]["messages"] == [
        {"role": "system", "content": "Be concise."},
        {"role": "user", "content": "Say hello."},
    ]
    assert captured["body"]["temperature"] == 0.2
    assert captured["body"]["max_completion_tokens"] == 64


def test_ollama_compatible_endpoint_uses_configured_model_and_path() -> None:
    captured: dict[str, Any] = {}
    model = "deepseek-v4.1-flash"

    def handle(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=completion_response())

    provider = make_provider(
        handle,
        model=model,
        base_url="https://ollama.com/v1",
    )
    try:
        result = asyncio.run(provider.complete(MESSAGES, model, CONFIG))
    finally:
        asyncio.run(provider.aclose())

    assert captured["url"] == "https://ollama.com/v1/chat/completions"
    assert captured["body"]["model"] == model
    assert result.model == model


def test_complete_leaves_missing_usage_unreported() -> None:
    response = completion_response()
    response["usage"] = None
    provider = make_provider(lambda _: httpx.Response(200, json=response))
    try:
        result = asyncio.run(provider.complete(MESSAGES, MODEL, CONFIG))
    finally:
        asyncio.run(provider.aclose())
    assert result.usage is None


def test_stream_yields_ordered_tokens_and_final_usage() -> None:
    provider = make_provider(
        lambda _: httpx.Response(
            200, content=stream_body(), headers={"content-type": "text/event-stream"}
        )
    )

    async def consume() -> list[Token | Usage]:
        return [event async for event in provider.stream_chat(MESSAGES, MODEL, CONFIG)]

    try:
        events = asyncio.run(consume())
    finally:
        asyncio.run(provider.aclose())

    assert events == [Token("Hello"), Token(" there!"), Usage(4, 2)]


def test_stream_does_not_invent_usage_when_not_returned() -> None:
    provider = make_provider(
        lambda _: httpx.Response(
            200,
            content=stream_body(include_usage=False),
            headers={"content-type": "text/event-stream"},
        )
    )

    async def consume() -> list[Token | Usage]:
        return [event async for event in provider.stream_chat(MESSAGES, MODEL, CONFIG)]

    try:
        events = asyncio.run(consume())
    finally:
        asyncio.run(provider.aclose())
    assert events == [Token("Hello"), Token(" there!")]


def test_stream_provider_error_is_propagated_before_any_token() -> None:
    provider = make_provider(
        lambda _: httpx.Response(503, json={"error": {"message": "unavailable"}})
    )

    async def consume() -> None:
        async for _ in provider.stream_chat(MESSAGES, MODEL, CONFIG):
            pytest.fail("the failed stream must not emit content")

    try:
        with pytest.raises(Unavailable):
            asyncio.run(consume())
    finally:
        asyncio.run(provider.aclose())


def test_closing_stream_closes_provider_response() -> None:
    class TrackedStream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield (
                b"data: "
                + json.dumps(
                    {
                        "id": "chatcmpl-test",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": MODEL,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"role": "assistant", "content": "first"},
                                "finish_reason": None,
                            }
                        ],
                    }
                ).encode()
                + b"\n\n"
            )

        async def aclose(self) -> None:
            self.closed = True

    response_stream = TrackedStream()
    provider = make_provider(
        lambda _: httpx.Response(
            200,
            stream=response_stream,
            headers={"content-type": "text/event-stream"},
        )
    )

    async def consume_one() -> None:
        stream = provider.stream_chat(MESSAGES, MODEL, CONFIG)
        assert await anext(stream) == Token("first")
        await stream.aclose()

    try:
        asyncio.run(consume_one())
    finally:
        asyncio.run(provider.aclose())
    assert response_stream.closed


@pytest.mark.parametrize(
    ("status_code", "body", "error"),
    [
        (
            401,
            {"error": {"message": "unauthorized", "type": "authentication_error", "code": None}},
            AuthError,
        ),
        (
            429,
            {
                "error": {
                    "message": "rate limited",
                    "type": "requests",
                    "code": "rate_limit_exceeded",
                }
            },
            RateLimited,
        ),
        (
            503,
            {"error": {"message": "unavailable", "type": "server_error", "code": None}},
            Unavailable,
        ),
        (
            400,
            {
                "error": {
                    "message": "filtered",
                    "type": "invalid_request_error",
                    "code": "content_filter",
                }
            },
            ContentFiltered,
        ),
    ],
)
def test_provider_api_errors_map_to_safe_taxonomy(
    status_code: int, body: dict[str, Any], error: type[Exception]
) -> None:
    provider = make_provider(lambda _: httpx.Response(status_code, json=body))
    try:
        with pytest.raises(error) as raised:
            asyncio.run(provider.complete(MESSAGES, MODEL, CONFIG))
    finally:
        asyncio.run(provider.aclose())
    assert "test-key" not in str(raised.value)
    assert "Say hello" not in str(raised.value)


def test_provider_timeout_maps_to_timeout_error() -> None:
    def handle(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("private transport detail")

    provider = make_provider(handle)
    try:
        with pytest.raises(Timeout):
            asyncio.run(provider.complete(MESSAGES, MODEL, CONFIG))
    finally:
        asyncio.run(provider.aclose())


def test_malformed_completion_maps_to_unavailable() -> None:
    provider = make_provider(lambda _: httpx.Response(200, json=completion_response(choices=[])))
    try:
        with pytest.raises(Unavailable):
            asyncio.run(provider.complete(MESSAGES, MODEL, CONFIG))
    finally:
        asyncio.run(provider.aclose())


def test_unexpected_disconnect_maps_to_unavailable() -> None:
    def handle(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("private connection detail")

    provider = make_provider(handle)
    try:
        with pytest.raises(Unavailable):
            asyncio.run(provider.complete(MESSAGES, MODEL, CONFIG))
    finally:
        asyncio.run(provider.aclose())


def test_provider_rejects_models_outside_the_configured_allowlist() -> None:
    provider = make_provider(lambda _: pytest.fail("request must not be sent"))
    try:
        with pytest.raises(InvalidSelection):
            asyncio.run(provider.complete(MESSAGES, "unconfigured-model", CONFIG))
    finally:
        asyncio.run(provider.aclose())


def test_registry_registers_only_the_configured_openai_model() -> None:
    registry = build_registry(
        ["openai"],
        openai_api_key="test-key",
        openai_model=MODEL,
        openai_timeout=5,
        openai_base_url="https://ollama.com/v1",
    )
    try:
        assert registry.ids() == ["openai"]
        entry = registry.get("openai")
        assert entry is not None
        assert [model.id for model in entry.spec.models] == [MODEL]
        assert entry.spec.models[0].capabilities == frozenset({"completion", "streaming"})
    finally:
        asyncio.run(registry.aclose())


def test_router_records_unknown_usage_without_substituting_counts() -> None:
    response = completion_response()
    response["usage"] = None
    provider = make_provider(lambda _: httpx.Response(200, json=response))
    registry = ProviderRegistry([RegisteredProvider(provider, openai_spec(MODEL))])
    sink = MemoryUsageSink()
    router = LLMRouter(registry, ["openai"], usage_sink=sink)
    try:
        result = asyncio.run(router.complete(MESSAGES, CONFIG))
    finally:
        asyncio.run(registry.aclose())

    assert result.usage is None
    assert len(sink.records) == 1
    assert sink.records[0].input_tokens is None
    assert sink.records[0].output_tokens is None
    assert sink.records[0].model == MODEL
    assert sink.records[0].ok is True


def test_router_records_missing_usage_as_unknown_and_uses_breaker_fallback() -> None:
    attempts = 0

    def handle(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503, json={"error": {"message": "unavailable"}})

    provider = make_provider(handle)
    fallback = FakeLLMProvider("fake")
    registry = ProviderRegistry(
        [
            RegisteredProvider(provider, openai_spec(MODEL)),
            RegisteredProvider(fallback, fake_spec()),
        ]
    )
    sink = MemoryUsageSink()
    router = LLMRouter(
        registry,
        ["openai", "fake"],
        RouterConfig(breaker_failure_threshold=1),
        usage_sink=sink,
    )

    async def answer() -> list[Any]:
        return [event async for event in router.stream_chat(MESSAGES, CONFIG)]

    try:
        asyncio.run(answer())
        events = asyncio.run(answer())
    finally:
        asyncio.run(registry.aclose())

    assert sum(isinstance(event, Token) for event in events) > 0
    assert any(isinstance(event, StreamMeta) and event.fallback_used for event in events)
    assert attempts == 1
    assert sink.records[0].ok is False and sink.records[0].error_code == "llm_unavailable"
    assert sink.records[1].ok is True
