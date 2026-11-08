"""The `LLMProvider` port. Adapters map vendor APIs and failures to `LLMError`."""

from collections.abc import AsyncIterator, Sequence
from typing import Protocol

from app.ai.types import Completion, GenerationConfig, Message, StreamEvent


class LLMProvider(Protocol):
    provider_id: str

    async def complete(
        self, messages: Sequence[Message], model: str, config: GenerationConfig
    ) -> Completion:
        """One full response. Must raise only `LLMError` subclasses."""
        ...

    def stream_chat(
        self, messages: Sequence[Message], model: str, config: GenerationConfig
    ) -> AsyncIterator[StreamEvent]:
        """Yield `Token`s then (optionally) a final `Usage`. Must raise only `LLMError`s."""
        ...
