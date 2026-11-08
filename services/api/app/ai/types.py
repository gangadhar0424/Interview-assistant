"""Provider-neutral data types shared by the port, router and API."""

from dataclasses import dataclass
from typing import Literal

Role = Literal["system", "user", "assistant"]
Capability = Literal["streaming", "completion"]


@dataclass(frozen=True, slots=True)
class Message:
    role: Role
    content: str


@dataclass(frozen=True, slots=True)
class GenerationConfig:
    temperature: float = 0.3
    max_output_tokens: int = 1024

    def __post_init__(self) -> None:
        if not 0.0 <= self.temperature <= 2.0:
            raise ValueError("temperature must be between 0 and 2")
        if not 1 <= self.max_output_tokens <= 32768:
            raise ValueError("max_output_tokens must be between 1 and 32768")


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True, slots=True)
class Completion:
    text: str
    usage: Usage
    provider: str = ""
    model: str = ""


@dataclass(frozen=True, slots=True)
class Token:
    text: str


StreamEvent = Token | Usage


@dataclass(frozen=True, slots=True)
class StreamMeta:
    """Which provider/model actually answers a routed stream (emitted by the router, never by a
    provider). `fallback_used` is True when the answering attempt was not the first planned one."""

    provider: str
    model: str
    fallback_used: bool


# What `LLMRouter.stream_chat` yields: the provider events plus one `StreamMeta` (Phase 8A).
RouterEvent = Token | Usage | StreamMeta


@dataclass(frozen=True, slots=True)
class ModelSpec:
    id: str
    label: str
    capabilities: frozenset[Capability]
    languages: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    id: str
    models: tuple[ModelSpec, ...]

    def model(self, model_id: str) -> ModelSpec | None:
        return next((m for m in self.models if m.id == model_id), None)
