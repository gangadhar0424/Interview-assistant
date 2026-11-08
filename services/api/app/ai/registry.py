"""Provider registry and policy: which providers/models exist, are enabled and may be selected."""

from dataclasses import dataclass

from app.ai.base import LLMProvider
from app.ai.errors import InvalidSelection
from app.ai.fake import FakeLLMProvider, fake_spec
from app.ai.types import Capability, ModelSpec, ProviderSpec

# Provider ids that can be enabled by configuration. Phase 6 has no real adapter (OQ-4); a real
# adapter is added here, together with its module in app/ai/adapters/, once approved.
KNOWN_PROVIDER_IDS: frozenset[str] = frozenset({"fake"})


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


def build_registry(enabled: list[str]) -> ProviderRegistry:
    """Build the registry from configuration. Credentials (future adapters) come from env only."""
    entries: list[RegisteredProvider] = []
    for provider_id in enabled:
        if provider_id == "fake":
            entries.append(RegisteredProvider(FakeLLMProvider("fake"), fake_spec("fake")))
        else:  # config validation already rejects unknown ids; fail closed regardless
            raise ValueError("unknown provider id")
    return ProviderRegistry(entries)
