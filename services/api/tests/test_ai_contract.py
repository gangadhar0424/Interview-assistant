import asyncio

import pytest

from app.ai.base import LLMProvider
from app.ai.errors import AuthError, ContentFiltered, LLMError, RateLimited, Timeout, Unavailable
from app.ai.fake import FAKE_MODEL_ID, FakeLLMProvider
from app.ai.types import GenerationConfig
from tests.ai_contract import MESSAGES, assert_failures_are_taxonomy_errors, run_contract

# Every enabled adapter belongs here (Phase 6: fake only; real adapters pending OQ-4).
ADAPTERS: list[tuple[str, LLMProvider, str]] = [("fake", FakeLLMProvider(), FAKE_MODEL_ID)]


@pytest.mark.parametrize(("name", "provider", "model"), ADAPTERS, ids=[a[0] for a in ADAPTERS])
def test_adapter_passes_contract(name: str, provider: LLMProvider, model: str) -> None:
    asyncio.run(run_contract(provider, model))
    asyncio.run(assert_failures_are_taxonomy_errors(provider, model))


@pytest.mark.parametrize("error", [RateLimited, Timeout, Unavailable, AuthError, ContentFiltered])
def test_fake_injects_each_taxonomy_error(error: type[LLMError]) -> None:
    provider = FakeLLMProvider(failures=[error()])
    with pytest.raises(error):
        asyncio.run(provider.complete(MESSAGES, FAKE_MODEL_ID, GenerationConfig()))


def test_generation_config_is_validated() -> None:
    with pytest.raises(ValueError):
        GenerationConfig(temperature=5)
    with pytest.raises(ValueError):
        GenerationConfig(max_output_tokens=0)
