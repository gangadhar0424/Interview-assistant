"""Provider registry and policy: which providers/models exist, are enabled and may be selected."""

from dataclasses import dataclass

from app.ai.adapters.openai import OpenAIProvider, openai_spec
from app.ai.base import LLMProvider
from app.ai.errors import InvalidSelection
from app.ai.fake import FakeLLMProvider, fake_spec
from app.ai.types import Capability, ModelSpec, ProviderSpec

KNOWN_PROVIDER_IDS: frozenset[str] = frozenset({"fake", "openai"})


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
            if isinstance(entry.provider, OpenAIProvider):
                await entry.provider.aclose()


def build_registry(
    enabled: list[str],
    *,
    openai_api_key: str | None = None,
    openai_model: str = "gpt-4o-mini",
    openai_timeout: float = 30.0,
    openai_base_url: str = "https://api.openai.com/v1",
) -> ProviderRegistry:
    """Build the provider allowlist from configuration; credentials remain server-side."""
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
        else:  # config validation already rejects unknown ids; fail closed regardless
            raise ValueError("unknown provider id")
    return ProviderRegistry(entries)
