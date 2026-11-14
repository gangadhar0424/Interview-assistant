"""Provider registry and policy: which providers/models exist, are enabled and may be selected."""

from dataclasses import dataclass

from app.ai.adapters.anthropic import AnthropicProvider, anthropic_spec
from app.ai.adapters.openai import (
    DEFAULT_GEMINI_MODEL,
    DEFAULT_GROQ_MODEL,
    GEMINI_BASE_URL,
    GEMINI_PROVIDER_ID,
    GEMINI_REQUEST_FIELDS,
    GROQ_BASE_URL,
    GROQ_PROVIDER_ID,
    GROQ_REQUEST_FIELDS,
    OpenAIProvider,
    openai_spec,
)
from app.ai.base import LLMProvider
from app.ai.errors import InvalidSelection
from app.ai.fake import FakeLLMProvider, fake_spec
from app.ai.types import Capability, ModelSpec, ProviderSpec

KNOWN_PROVIDER_IDS: frozenset[str] = frozenset({"fake", "openai", "groq", "anthropic", "gemini"})


@dataclass(frozen=True, slots=True)
class RegisteredProvider:
    provider: LLMProvider
    spec: ProviderSpec


class ProviderRegistry:
    """Ordered, enabled providers. The enabled set is the server-side allowlist."""

    def __init__(self, entries: list[RegisteredProvider]) -> None:
        ids = [e.spec.id for e in entries]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate provider id in registry")
        for e in entries:
            if e.provider.provider_id != e.spec.id:
                raise ValueError("provider id does not match its spec")
        self._entries = {e.spec.id: e for e in entries}

    def ids(self) -> list[str]:
        return list(self._entries)

    def get(self, provider_id: str) -> RegisteredProvider | None:
        return self._entries.get(provider_id)

    def entries(self) -> list[RegisteredProvider]:
        return list(self._entries.values())

    def resolve(
        self, provider_id: str, model_id: str, capability: Capability
    ) -> tuple[RegisteredProvider, ModelSpec]:
        """Validate a user selection against the allowlist and the requested capability."""
        entry = self._entries.get(provider_id)
        model = entry.spec.model(model_id) if entry else None
        if entry is None or model is None or capability not in model.capabilities:
            raise InvalidSelection()
        return entry, model

    def default_model(self, provider_id: str, capability: Capability) -> ModelSpec | None:
        entry = self._entries.get(provider_id)
        if entry is None:
            return None
        return next((m for m in entry.spec.models if capability in m.capabilities), None)

    def languages(self) -> list[str]:
        seen: dict[str, None] = {}
        for e in self._entries.values():
            for m in e.spec.models:
                for lang in m.languages:
                    seen[lang] = None
        return list(seen)

    async def aclose(self) -> None:
        """Close provider-owned resources when the application lifespan ends."""
        for entry in self._entries.values():
            if isinstance(entry.provider, OpenAIProvider | AnthropicProvider):
                await entry.provider.aclose()


def build_registry(
    enabled: list[str],
    *,
    openai_api_key: str | None = None,
    openai_model: str = "gpt-4o-mini",
    openai_timeout: float = 30.0,
    openai_base_url: str = "https://api.openai.com/v1",
    groq_api_key: str | None = None,
    groq_model: str = DEFAULT_GROQ_MODEL,
    anthropic_api_key: str | None = None,
    anthropic_model: str = "claude-sonnet-5-5",
    gemini_api_key: str | None = None,
    gemini_model: str = DEFAULT_GEMINI_MODEL,
) -> ProviderRegistry:
    """Build the reasoning-provider allowlist; credentials remain server-side (D-57)."""
    entries: list[RegisteredProvider] = []
    for provider_id in enabled:
        if provider_id == "fake":
            entries.append(RegisteredProvider(FakeLLMProvider("fake"), fake_spec("fake")))
        elif provider_id == "openai":
            if not openai_api_key:
                raise ValueError("OPENAI_API_KEY is required when the OpenAI provider is enabled")
            provider = OpenAIProvider(
                api_key=openai_api_key,
                model=openai_model,
                timeout=openai_timeout,
                base_url=openai_base_url,
            )
            entries.append(RegisteredProvider(provider, openai_spec(openai_model)))
        elif provider_id == "groq":
            if not groq_api_key:
                raise ValueError("GROQ_API_KEY is required when the Groq provider is enabled")
            entries.append(
                RegisteredProvider(
                    OpenAIProvider(
                        api_key=groq_api_key,
                        model=groq_model,
                        timeout=openai_timeout,
                        base_url=GROQ_BASE_URL,
                        provider_id=GROQ_PROVIDER_ID,
                        extra_body=dict(GROQ_REQUEST_FIELDS),
                    ),
                    openai_spec(groq_model, GROQ_PROVIDER_ID, f"Groq ({groq_model})"),
                )
            )
        elif provider_id == GEMINI_PROVIDER_ID:
            if not gemini_api_key:
                raise ValueError("GEMINI_API_KEY is required when the Gemini provider is enabled")
            entries.append(
                RegisteredProvider(
                    OpenAIProvider(
                        api_key=gemini_api_key,
                        model=gemini_model,
                        timeout=openai_timeout,
                        base_url=GEMINI_BASE_URL,
                        provider_id=GEMINI_PROVIDER_ID,
                        extra_body=dict(GEMINI_REQUEST_FIELDS),
                    ),
                    openai_spec(gemini_model, GEMINI_PROVIDER_ID, f"Gemini ({gemini_model})"),
                )
            )
        elif provider_id == "anthropic":
            if not anthropic_api_key:
                raise ValueError(
                    "ANTHROPIC_API_KEY is required when the Anthropic provider is enabled"
                )
            entries.append(
                RegisteredProvider(
                    AnthropicProvider(
                        api_key=anthropic_api_key, model=anthropic_model, timeout=openai_timeout
                    ),
                    anthropic_spec(anthropic_model),
                )
            )
        else:  # config validation already rejects unknown ids; fail closed regardless
            raise ValueError("unknown provider id")
    return ProviderRegistry(entries)
