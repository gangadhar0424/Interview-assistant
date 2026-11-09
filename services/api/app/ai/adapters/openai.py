"""OpenAI-compatible Chat Completions adapter for the provider-neutral LLM port."""

from collections.abc import AsyncGenerator, Sequence
from typing import cast

import httpx
from openai import (
    APIConnectionError,
    APIError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    AuthenticationError,
    RateLimitError,
)
from openai.types.chat import ChatCompletionMessageParam

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

OPENAI_PROVIDER_ID = "openai"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"


def openai_spec(model: str) -> ProviderSpec:
    return ProviderSpec(
        id=OPENAI_PROVIDER_ID,
        models=(
            ModelSpec(
                id=model,
                label=f"OpenAI-compatible ({model})",
                capabilities=frozenset({"streaming", "completion"}),
                languages=("en", "hi", "te"),
            ),
        ),
    )


def _messages(messages: Sequence[Message]) -> list[ChatCompletionMessageParam]:
    return [
        cast(ChatCompletionMessageParam, {"role": message.role, "content": message.content})
        for message in messages
    ]


def _usage(value: object) -> Usage | None:
    if value is None:
        return None
    input_tokens = getattr(value, "prompt_tokens", None)
    output_tokens = getattr(value, "completion_tokens", None)
    if type(input_tokens) is not int or type(output_tokens) is not int:
        return None
    return Usage(input_tokens=input_tokens, output_tokens=output_tokens)


def _map_error(error: APIError) -> LLMError:
    if isinstance(error, (AuthenticationError,)) or (
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
    if isinstance(error, APIConnectionError) or (
        isinstance(error, APIStatusError) and error.status_code >= 500
    ):
        return Unavailable()
    if isinstance(error, APIStatusError) and _is_content_filter(error.body):
        return ContentFiltered()
    return Unavailable()


def _is_content_filter(body: object) -> bool:
    if not isinstance(body, dict):
        return False
    detail = body.get("error", body)
    if not isinstance(detail, dict):
        return False
    return detail.get("code") in {"content_filter", "content_policy_violation"}


class OpenAIProvider(LLMProvider):
    """Asynchronous OpenAI adapter; only the configured model is selectable."""

    provider_id = OPENAI_PROVIDER_ID

    def __init__(
        self,
        api_key: str,
        model: str,
        timeout: float,
        *,
        base_url: str = "https://api.openai.com/v1",
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._model = model
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=0,
            http_client=http_client,
        )

    def _validate_model(self, model: str) -> None:
        if model != self._model:
            raise InvalidSelection()

    async def complete(
        self, messages: Sequence[Message], model: str, config: GenerationConfig
    ) -> Completion:
        self._validate_model(model)
        try:
            response = await self._client.chat.completions.create(
                model=model,
                messages=_messages(messages),
                temperature=config.temperature,
                max_completion_tokens=config.max_output_tokens,
            )
        except APIError as error:
            raise _map_error(error) from None

        try:
            choice = response.choices[0]
            if choice.finish_reason == "content_filter" or choice.message.refusal:
                raise ContentFiltered()
            content = choice.message.content
            if not isinstance(content, str):
                raise Unavailable()
            usage = _usage(response.usage)
        except (AttributeError, IndexError, TypeError):
            raise Unavailable() from None
        return Completion(text=content, usage=usage, provider=self.provider_id, model=model)

    async def stream_chat(
        self, messages: Sequence[Message], model: str, config: GenerationConfig
    ) -> AsyncGenerator[Token | Usage, None]:
        self._validate_model(model)
        stream = None
        usage: Usage | None = None
        try:
            stream = await self._client.chat.completions.create(
                model=model,
                messages=_messages(messages),
                temperature=config.temperature,
                max_completion_tokens=config.max_output_tokens,
                stream=True,
                stream_options={"include_usage": True},
            )
            async for chunk in stream:
                if chunk.usage is not None:
                    usage = _usage(chunk.usage)
                for choice in chunk.choices:
                    if choice.finish_reason == "content_filter":
                        raise ContentFiltered()
                    delta = choice.delta
                    if delta.refusal:
                        raise ContentFiltered()
                    if delta.content is not None:
                        if not isinstance(delta.content, str):
                            raise Unavailable()
                        if delta.content:
                            yield Token(delta.content)
        except APIError as error:
            raise _map_error(error) from None
        except (AttributeError, TypeError):
            raise Unavailable() from None
        finally:
            if stream is not None:
                await stream.close()
        if usage is not None:
            yield usage

    async def aclose(self) -> None:
        await self._client.close()
