"""D-57 adapters with mocked HTTP only: Groq (OpenAI-compatible), Claude, and the voice providers.

No test here touches a network or a real key. Secret hygiene is asserted: a sentinel API key and
sentinel request/response text never appear in raised errors.
"""

import asyncio
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from app.ai.adapters.anthropic import AnthropicProvider, anthropic_spec
from app.ai.adapters.openai import (
    GEMINI_BASE_URL,
    GEMINI_REQUEST_FIELDS,
    GROQ_BASE_URL,
    OpenAIProvider,
    openai_spec,
)
from app.ai.adapters.stt import GeminiSTT, OpenAICompatibleSTT, SarvamSTT
from app.ai.errors import (
    AuthError,
    ContentFiltered,
    InvalidSelection,
    RateLimited,
    Timeout,
    Unavailable,
)
from app.ai.registry import build_registry
from app.ai.router import LLMRouter, Selection
from app.ai.stt import AudioInput
from app.ai.types import GenerationConfig, Message, Token, Usage
from app.audio.validation import validate_wav
from tests.wav_helpers import make_wav

KEY = "sk-SENTINEL-KEY-9931"
TEXT = "SENTINEL-INTERVIEW-TEXT-5512"
MESSAGES = [Message("system", "Be concise."), Message("user", TEXT), Message("assistant", "ok")]
CONFIG = GenerationConfig(temperature=0.2, max_output_tokens=64)

Handler = Callable[[httpx.Request], httpx.Response]


def client(handler: Handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def audio() -> AudioInput:
    wav = validate_wav(make_wav(1.0), max_seconds=30)
    return AudioInput(wav.data, 16_000, wav.duration_ms)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


# ------------------------------------------------------------------- Groq (reasoning)


def chat_ok(content: str = "Hello") -> dict[str, Any]:
    return {
        "id": "x",
        "object": "chat.completion",
        "created": 1,
        "model": "openai/gpt-oss-120b",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content, "refusal": None},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
    }


def test_groq_uses_its_endpoint_model_and_low_reasoning_effort() -> None:
    seen: dict[str, Any] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=chat_ok())

    model = "openai/gpt-oss-120b"
    provider = OpenAIProvider(
        KEY,
        model,
        5,
        base_url=GROQ_BASE_URL,
        http_client=client(handle),
        provider_id="groq",
        extra_body={"reasoning_effort": "low"},
    )
    result = run(provider.complete(MESSAGES, model, CONFIG))
    assert result.provider == "groq" and result.text == "Hello" and result.model == model
    assert seen["url"] == "https://api.groq.com/openai/v1/chat/completions"
    assert seen["auth"] == f"Bearer {KEY}"
    assert seen["body"]["model"] == model and seen["body"]["reasoning_effort"] == "low"
    assert seen["body"]["max_completion_tokens"] == 64
    with pytest.raises(InvalidSelection):
        run(provider.complete(MESSAGES, "another-model", CONFIG))


def test_gemini_uses_its_openai_compatible_endpoint_and_model() -> None:
    seen: dict[str, Any] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=chat_ok())

    model = "gemini-3.1-flash-lite"
    provider = OpenAIProvider(
        KEY,
        model,
        5,
        base_url=GEMINI_BASE_URL,
        http_client=client(handle),
        provider_id="gemini",
        extra_body=dict(GEMINI_REQUEST_FIELDS),
    )
    result = run(provider.complete(MESSAGES, model, CONFIG))
    assert result.provider == "gemini" and result.text == "Hello"
    assert seen["url"] == (
        "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    )
    assert seen["auth"] == f"Bearer {KEY}"
    assert seen["body"]["model"] == model and seen["body"]["reasoning_effort"] == "low"
    with pytest.raises(InvalidSelection):
        run(provider.complete(MESSAGES, "another-model", CONFIG))


def test_build_registry_creates_gemini_only_with_its_key() -> None:
    with pytest.raises(ValueError):
        build_registry(["gemini"])
    registry = build_registry(["gemini"], gemini_api_key="g")
    assert registry.ids() == ["gemini"]
    assert registry.get("gemini").spec.models[0].id == "gemini-3.1-flash-lite"  # type: ignore[union-attr]
    run(registry.aclose())


def test_openai_adapter_sends_no_extra_fields_by_default() -> None:
    seen: dict[str, Any] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=chat_ok())

    provider = OpenAIProvider(KEY, "gpt-4o-mini", 5, http_client=client(handle))
    run(provider.complete(MESSAGES, "gpt-4o-mini", CONFIG))
    assert "reasoning_effort" not in seen["body"] and provider.provider_id == "openai"


def test_build_registry_creates_groq_and_claude_only_with_their_keys() -> None:
    for pid in ("groq", "anthropic"):
        with pytest.raises(ValueError):
            build_registry([pid])
    registry = build_registry(["groq", "anthropic"], groq_api_key="g", anthropic_api_key="a")
    assert registry.ids() == ["groq", "anthropic"]
    assert registry.get("groq").spec.models[0].id == "openai/gpt-oss-120b"  # type: ignore[union-attr]
    assert registry.get("anthropic").spec.models[0].id == "claude-sonnet-5-5"  # type: ignore[union-attr]
    run(registry.aclose())


# ------------------------------------------------------------------ Claude (reasoning)


def anthropic_ok(text: str = "Hello") -> dict[str, Any]:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-test",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 7, "output_tokens": 3},
    }


def claude(handler: Handler) -> AnthropicProvider:
    return AnthropicProvider(KEY, "claude-test", 5, http_client=client(handler))


def test_claude_maps_system_messages_and_generation_settings() -> None:
    seen: dict[str, Any] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["key"] = request.headers["x-api-key"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=anthropic_ok())

    provider = claude(handle)
    result = run(provider.complete(MESSAGES, "claude-test", CONFIG))
    assert (result.text, result.usage, result.provider) == ("Hello", Usage(7, 3), "anthropic")
    assert seen["url"].endswith("/v1/messages") and seen["key"] == KEY
    body = seen["body"]
    assert body["system"] == "Be concise."
    assert body["messages"] == [
        {"role": "user", "content": TEXT},
        {"role": "assistant", "content": "ok"},
    ]
    assert (body["max_tokens"], body["temperature"], body["model"]) == (64, 0.2, "claude-test")
    with pytest.raises(InvalidSelection):
        run(provider.complete(MESSAGES, "other", CONFIG))
    run(provider.aclose())


def sse(events: list[tuple[str, dict[str, Any]]]) -> bytes:
    return b"".join(
        f"event: {name}\ndata: {json.dumps(data)}\n\n".encode() for name, data in events
    )


def claude_stream_body(stop_reason: str = "end_turn") -> bytes:
    start = anthropic_ok("")
    start["content"] = []
    start["stop_reason"] = None
    start["usage"] = {"input_tokens": 7, "output_tokens": 1}
    events: list[tuple[str, dict[str, Any]]] = [
        ("message_start", {"type": "message_start", "message": start}),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ),
    ]
    for piece in ("Hel", "lo"):
        events.append(
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": piece},
                },
            )
        )
    events += [
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                "usage": {"output_tokens": 3},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ]
    return sse(events)


async def collect(provider: AnthropicProvider) -> list[Any]:
    return [e async for e in provider.stream_chat(MESSAGES, "claude-test", CONFIG)]


def test_claude_streams_ordered_tokens_then_usage() -> None:
    def handle(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=claude_stream_body(), headers={"content-type": "text/event-stream"}
        )

    events = run(collect(claude(handle)))
    assert events[:2] == [Token("Hel"), Token("lo")]
    assert isinstance(events[-1], Usage) and events[-1].output_tokens == 3


def test_claude_refusals_are_content_filtered_never_retried() -> None:
    def handle(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=claude_stream_body("refusal"),
            headers={"content-type": "text/event-stream"},
        )

    with pytest.raises(ContentFiltered):
        run(collect(claude(handle)))
    refusal = anthropic_ok()
    refusal["stop_reason"] = "refusal"
    with pytest.raises(ContentFiltered):
        run(
            claude(lambda _: httpx.Response(200, json=refusal)).complete(
                MESSAGES, "claude-test", CONFIG
            )
        )


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, AuthError),
        (403, AuthError),
        (429, RateLimited),
        (529, Unavailable),
        (500, Unavailable),
    ],
)
def test_claude_errors_map_to_the_fixed_taxonomy_without_leaking(
    status: int, error: type[Exception]
) -> None:
    body = {"type": "error", "error": {"type": "x", "message": f"{KEY} {TEXT}"}}
    provider = claude(lambda _: httpx.Response(status, json=body))
    with pytest.raises(error) as caught:
        run(provider.complete(MESSAGES, "claude-test", CONFIG))
    assert KEY not in repr(caught.value) and TEXT not in str(caught.value)


def test_claude_timeout_is_a_timeout() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(Timeout):
        run(claude(handle).complete(MESSAGES, "claude-test", CONFIG))


# --------------------------------------------- exclusivity across real adapters (router)


def test_a_failing_selected_provider_is_not_replaced_by_another() -> None:
    called: list[str] = []

    def failing(_: httpx.Request) -> httpx.Response:
        called.append("anthropic")
        return httpx.Response(529, json={"type": "error", "error": {"type": "overloaded"}})

    def healthy(_: httpx.Request) -> httpx.Response:
        called.append("groq")
        return httpx.Response(200, json=chat_ok())

    from app.ai.registry import ProviderRegistry, RegisteredProvider

    registry = ProviderRegistry(
        [
            RegisteredProvider(claude(failing), anthropic_spec("claude-test")),
            RegisteredProvider(
                OpenAIProvider(KEY, "m", 5, http_client=client(healthy), provider_id="groq"),
                openai_spec("m", "groq"),
            ),
        ]
    )
    router = LLMRouter(registry, ["anthropic", "groq"])
    with pytest.raises(Unavailable):
        run(router.complete(MESSAGES, CONFIG, selection=Selection("anthropic", "claude-test")))
    assert called == ["anthropic"]  # groq was never called although it was healthy and in the chain
    # No selection (a legacy session) still uses the chain.
    result = run(router.complete(MESSAGES, CONFIG))
    assert result.provider == "groq"


# ----------------------------------------------------------------- voice adapters


def transcription_ok(text: str = "tell me about yourself") -> dict[str, Any]:
    return {"text": text}


def test_openai_and_groq_transcription_requests_and_language() -> None:
    seen: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(
            {
                "url": str(request.url),
                "auth": request.headers["authorization"],
                "ctype": request.headers["content-type"],
                "body": request.content,
            }
        )
        return httpx.Response(200, json=transcription_ok())

    cases = [
        ("openai", "https://api.openai.com/v1", "gpt-4o-mini-transcribe"),
        ("groq", "https://api.groq.com/openai/v1", "whisper-large-v3-turbo"),
    ]
    for pid, base, model in cases:
        stt = OpenAICompatibleSTT(pid, KEY, model, 5, base_url=base, http_client=client(handle))
        result = run(stt.transcribe(audio(), "te", model))
        assert result.text == "tell me about yourself"
        with pytest.raises(InvalidSelection):
            run(stt.transcribe(audio(), "te", "other-model"))
    assert [s["url"] for s in seen] == [
        "https://api.openai.com/v1/audio/transcriptions",
        "https://api.groq.com/openai/v1/audio/transcriptions",
    ]
    assert all(s["ctype"].startswith("multipart/form-data") for s in seen)
    assert all(s["auth"] == f"Bearer {KEY}" for s in seen)
    assert b'name="language"' in seen[0]["body"] and b"te" in seen[0]["body"]
    assert b"whisper-large-v3-turbo" in seen[1]["body"] and b"RIFF" in seen[1]["body"]


def test_gemini_transcription_sends_audio_as_a_chat_part_and_returns_the_reply() -> None:
    seen: dict[str, Any] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=chat_ok("  tell me about yourself  "))

    model = "gemini-3.1-flash-lite"
    stt = GeminiSTT("gemini", KEY, model, 5, http_client=client(handle))
    result = run(stt.transcribe(audio(), "hi", model))
    assert result.text == "tell me about yourself"
    assert seen["url"].endswith("/v1beta/openai/chat/completions")
    assert seen["auth"] == f"Bearer {KEY}"
    parts = seen["body"]["messages"][0]["content"]
    assert parts[0]["type"] == "text" and "Hindi" in parts[0]["text"]
    assert parts[1]["type"] == "input_audio" and parts[1]["input_audio"]["format"] == "wav"
    assert seen["body"]["temperature"] == 0
    with pytest.raises(InvalidSelection):
        run(stt.transcribe(audio(), "hi", "other-model"))


def test_gemini_transcription_errors_are_safe_and_mapped() -> None:
    for status, error in [(401, AuthError), (429, RateLimited), (503, Unavailable)]:

        def handle(_request: httpx.Request, code: int = status) -> httpx.Response:
            return httpx.Response(code, json={"error": TEXT})

        stt = GeminiSTT("gemini", KEY, "m", 5, http_client=client(handle))
        with pytest.raises(error) as info:
            run(stt.transcribe(audio(), "en", "m"))
        assert KEY not in str(info.value) and TEXT not in str(info.value)


@pytest.mark.parametrize(
    ("status", "error"),
    [(401, AuthError), (429, RateLimited), (503, Unavailable), (504, Timeout)],
)
def test_openai_compatible_transcription_errors_are_safe(
    status: int, error: type[Exception]
) -> None:
    body = {"error": {"message": f"{KEY} {TEXT}", "type": "x", "code": "y"}}
    stt = OpenAICompatibleSTT(
        "groq",
        KEY,
        "m",
        5,
        base_url=GROQ_BASE_URL,
        http_client=client(lambda _: httpx.Response(status, json=body)),
    )
    with pytest.raises(error) as caught:
        run(stt.transcribe(audio(), "en", "m"))
    assert KEY not in repr(caught.value) and TEXT not in str(caught.value)


def test_a_malformed_transcription_response_is_unavailable() -> None:
    stt = OpenAICompatibleSTT(
        "openai",
        KEY,
        "m",
        5,
        base_url="https://api.openai.com/v1",
        http_client=client(lambda _: httpx.Response(200, json={"unexpected": 1})),
    )
    with pytest.raises(Unavailable):
        run(stt.transcribe(audio(), "en", "m"))


def test_sarvam_request_shape_and_response() -> None:
    seen: dict[str, Any] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["key"] = request.headers.get("api-subscription-key")
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = request.content
        return httpx.Response(200, json={"transcript": "namaste", "language_code": "hi-IN"})

    stt = SarvamSTT(KEY, "saarika:v2.5", 5, http_client=client(handle))
    result = run(stt.transcribe(audio(), "hi", "saarika:v2.5"))
    assert result.text == "namaste"
    assert seen["url"] == "https://api.sarvam.ai/speech-to-text"
    assert seen["key"] == KEY and seen["auth"] is None
    assert b"saarika:v2.5" in seen["body"] and b"hi-IN" in seen["body"] and b"RIFF" in seen["body"]
    with pytest.raises(InvalidSelection):
        run(stt.transcribe(audio(), "hi", "other"))


@pytest.mark.parametrize(
    ("status", "error"),
    [(403, AuthError), (429, RateLimited), (500, Unavailable), (408, Timeout)],
)
def test_sarvam_errors_are_safe(status: int, error: type[Exception]) -> None:
    body = {"error": {"message": f"{KEY} {TEXT}"}}
    stt = SarvamSTT(KEY, "m", 5, http_client=client(lambda _: httpx.Response(status, json=body)))
    with pytest.raises(error) as caught:
        run(stt.transcribe(audio(), "en", "m"))
    assert KEY not in repr(caught.value) and TEXT not in str(caught.value)


def test_sarvam_network_failures_are_mapped() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("t", request=request)

    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("c", request=request)

    with pytest.raises(Timeout):
        run(SarvamSTT(KEY, "m", 5, http_client=client(timeout)).transcribe(audio(), "en", "m"))
    with pytest.raises(Unavailable):
        run(SarvamSTT(KEY, "m", 5, http_client=client(broken)).transcribe(audio(), "en", "m"))
    garbage = SarvamSTT(KEY, "m", 5, http_client=client(lambda _: httpx.Response(200, text="nope")))
    with pytest.raises(Unavailable):
        run(garbage.transcribe(audio(), "en", "m"))


# ------------------------------------------------------------------------- config


@pytest.fixture
def base_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@127.0.0.1:1/db")
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    monkeypatch.setenv("JWT_SECRET", "test-only-jwt-secret-0123456789abcdef0123456789")
    monkeypatch.setenv("APP_ENV", "local")
    for name in ("LLM_ENABLED_PROVIDERS", "STT_ENABLED_PROVIDERS"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def settings() -> Any:
    from app.core.config import Settings

    return Settings(_env_file=None)


def test_documented_model_defaults_and_empty_allow_lists(base_env: pytest.MonkeyPatch) -> None:
    s = settings()
    assert s.groq_model == "openai/gpt-oss-120b"
    assert s.anthropic_model == "claude-sonnet-5-5"
    assert (s.openai_stt_model, s.groq_stt_model, s.sarvam_stt_model) == (
        "gpt-4o-mini-transcribe",
        "whisper-large-v3-turbo",
        "saarika:v2.5",
    )
    assert s.llm_enabled_provider_list == [] and s.stt_enabled_provider_list == []


@pytest.mark.parametrize(
    ("list_name", "provider", "key_name"),
    [
        ("LLM_ENABLED_PROVIDERS", "groq", "GROQ_API_KEY"),
        ("LLM_ENABLED_PROVIDERS", "anthropic", "ANTHROPIC_API_KEY"),
        ("STT_ENABLED_PROVIDERS", "openai", "OPENAI_API_KEY"),
        ("STT_ENABLED_PROVIDERS", "groq", "GROQ_API_KEY"),
        ("STT_ENABLED_PROVIDERS", "sarvam", "SARVAM_API_KEY"),
    ],
)
def test_an_enabled_provider_needs_its_key_and_the_error_never_shows_it(
    base_env: pytest.MonkeyPatch, list_name: str, provider: str, key_name: str
) -> None:
    base_env.setenv(list_name, provider)
    with pytest.raises(ValueError, match=key_name):
        settings()
    base_env.setenv(key_name, KEY)
    s = settings()
    assert KEY not in repr(s)  # secrets are SecretStr: never in a repr
    assert provider in (s.llm_enabled_provider_list + s.stt_enabled_provider_list)


def test_a_blank_model_setting_is_refused(base_env: pytest.MonkeyPatch) -> None:
    base_env.setenv("GROQ_MODEL", "   ")
    with pytest.raises(ValueError):
        settings()
