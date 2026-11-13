"""Native Anthropic (Claude) Messages adapter for the provider-neutral LLM port (D-57).

System messages become the Messages API `system` parameter; the rest keep their order. Vendor
errors are mapped to the fixed `LLMError` taxonomy so no provider text ever reaches a user or log.
"""

from collections.abc import AsyncGenerator, Sequence
from typing import Any

import httpx
from anthropic import (
    APIConnectionError,
    APIError,
    APIStatusError,
    APITimeoutError,
    AsyncAnthropic,
    AuthenticationError,
    RateLimitError,
)

from app.ai.base import LLMProvider
from app.ai.errors import (
    AuthError,
    ContentFiltered,
    InvalidSelection,
    LLMError,
    RateLimited,
    Unavailable,
)
from app.ai.errors import Timeout as ProviderTimeout
from app.ai.types import (
    Completion,
    GenerationConfig,
    Message,
    ModelSpec,
    ProviderSpec,
    Token,
    Usage,
)

ANTHROPIC_PROVIDER_ID = "anthropic"
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-5-5"


def anthropic_spec(model: str) -> ProviderSpec:
    return ProviderSpec(
        id=ANTHROPIC_PROVIDER_ID,
        models=(
            ModelSpec(
                id=model,
                label=f"Claude ({model})",
                capabilities=frozenset({"streaming", "completion"}),
                languages=("en", "hi", "te"),
            ),
        ),
    )


def _split(messages: Sequence[Message]) -> tuple[str, list[dict[str, str]]]:
    system = "\n\n".join(m.content for m in messages if m.role == "system")
    turns = [{"role": m.role, "content": m.content} for m in messages if m.role != "system"]
    return system, turns


def _usage(value: object) -> Usage | None:
    input_tokens = getattr(value, "input_tokens", None)
    output_tokens = getattr(value, "output_tokens", None)
    if type(input_tokens) is not int or type(output_tokens) is not int:
        return None
    return Usage(input_tokens=input_tokens, output_tokens=output_tokens)


def map_anthropic_error(error: APIError) -> LLMError:
    if isinstance(error, AuthenticationError) or (
        isinstance(error, APIStatusError) and error.status_code in (401, 403)
    ):
        return AuthError()
    if isinstance(error, RateLimitError) or (
        isinstance(error, APIStatusError) and error.status_code == 429
    ):
        return RateLimited()
    if isinstance(error, APITimeoutError) or (
        isinstance(error, APIStatusError) and error.status_code in (408, 504)
    ):
        return ProviderTimeout()
    if isinstance(error, APIConnectionError):
        return Unavailable()
    return Unavailable()  # 5xx (including 529 overloaded), 4xx we cannot act on, anything else


class AnthropicProvider(LLMProvider):
    """Asynchronous Claude adapter; only the configured model is selectable."""

    provider_id = ANTHROPIC_PROVIDER_ID

    def __init__(
        self,
        api_key: str,
        model: str,
        timeout: float,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._model = model
        self._client = AsyncAnthropic(
            api_key=api_key, timeout=timeout, max_retries=0, http_client=http_client
        )

    def _validate_model(self, model: str) -> None:
        if model != self._model:
            raise InvalidSelection()

    def _kwargs(
        self, messages: Sequence[Message], model: str, config: GenerationConfig
    ) -> dict[str, Any]:
        system, turns = _split(messages)
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": turns,
            "max_tokens": config.max_output_tokens,
            "temperature": config.temperature,
        }
        if system:
            kwargs["system"] = system
        return kwargs

    async def complete(
        self, messages: Sequence[Message], model: str, config: GenerationConfig
    ) -> Completion:
        self._validate_model(model)
        try:
            response = await self._client.messages.create(**self._kwargs(messages, model, config))
        except APIError as error:
            raise map_anthropic_error(error) from None
        try:
            if response.stop_reason == "refusal":
                raise ContentFiltered()
            text = "".join(b.text for b in response.content if getattr(b, "type", "") == "text")
            usage = _usage(response.usage)
        except (AttributeError, TypeError):
            raise Unavailable() from None
        return Completion(text=text, usage=usage, provider=self.provider_id, model=model)

    async def stream_chat(
        self, messages: Sequence[Message], model: str, config: GenerationConfig
    ) -> AsyncGenerator[Token | Usage, None]:
        self._validate_model(model)
        usage: Usage | None = None
        try:
            async with self._client.messages.stream(
                **self._kwargs(messages, model, config)
            ) as stream:
                async for text in stream.text_stream:
                    if not isinstance(text, str):
                        raise Unavailable()
                    if text:
                        yield Token(text)
                final = await stream.get_final_message()
                if final.stop_reason == "refusal":
                    raise ContentFiltered()
                usage = _usage(final.usage)
        except APIError as error:
            raise map_anthropic_error(error) from None
        except (AttributeError, TypeError):
            raise Unavailable() from None
        if usage is not None:
            yield usage

    async def aclose(self) -> None:
        await self._client.close()
