"""Deterministic fake provider for tests/local development. Has no real model behind it.

Behaviour is scripted: `failures` is consumed one entry per call (an `LLMError` instance is raised,
`None` means succeed), which is how tests inject outages, rate limits and timeouts.
"""

import asyncio
from collections.abc import AsyncIterator, Sequence

from app.ai.errors import LLMError
from app.ai.types import (
    Capability,
    Completion,
    GenerationConfig,
    Message,
    ModelSpec,
    ProviderSpec,
    StreamEvent,
    Token,
    Usage,
)

FAKE_MODEL_ID = "fake-echo"


def fake_spec(provider_id: str = "fake") -> ProviderSpec:
    caps: frozenset[Capability] = frozenset({"streaming", "completion"})
    return ProviderSpec(
        id=provider_id,
        models=(ModelSpec(FAKE_MODEL_ID, "Fake echo (test only)", caps, ("en", "hi", "te")),),
    )


class FakeLLMProvider:
    def __init__(
        self,
        provider_id: str = "fake",
        *,
        failures: Sequence[LLMError | None] = (),
        fail_after_tokens: int | None = None,
        first_token_delay: float = 0.0,
        reply: str = "echo: {last}",
    ) -> None:
        self.provider_id = provider_id
        self._failures = list(failures)
        self._fail_after_tokens = fail_after_tokens
        self._first_token_delay = first_token_delay
        self._reply = reply
        self.calls = 0
        self.models_seen: list[str] = []

    def _next_failure(self) -> LLMError | None:
        self.calls += 1
        return self._failures.pop(0) if self._failures else None

    def _text(self, messages: Sequence[Message]) -> str:
        last = messages[-1].content if messages else ""
        return self._reply.replace("{last}", last)

    @staticmethod
    def _usage(messages: Sequence[Message], text: str) -> Usage:
        return Usage(
            input_tokens=sum(len(m.content.split()) for m in messages),
            output_tokens=len(text.split()),
        )

    async def complete(
        self, messages: Sequence[Message], model: str, config: GenerationConfig
    ) -> Completion:
        self.models_seen.append(model)
        if self._first_token_delay:
            await asyncio.sleep(self._first_token_delay)
        if (failure := self._next_failure()) is not None:
            raise failure
        text = self._text(messages)
        return Completion(text, self._usage(messages, text), self.provider_id, model)

    async def stream_chat(
        self, messages: Sequence[Message], model: str, config: GenerationConfig
    ) -> AsyncIterator[StreamEvent]:
        self.models_seen.append(model)
        if self._first_token_delay:
            await asyncio.sleep(self._first_token_delay)
        failure = self._next_failure()
        if failure is not None and self._fail_after_tokens is None:
            raise failure
        text = self._text(messages)
        for i, word in enumerate(text.split(" ")):
            if failure is not None and i == self._fail_after_tokens:
                raise failure
            yield Token(word if i == 0 else " " + word)
        yield self._usage(messages, text)
