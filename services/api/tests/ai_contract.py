"""Provider contract suite: every enabled LLMProvider adapter must pass the same checks.

Run it against an adapter with `await run_contract(provider, model)` (see test_ai_contract.py).
Phase 6 has only the fake adapter; real adapters add themselves to the parametrisation with
recorded fixtures once approved (OQ-4).
"""

from app.ai.base import LLMProvider
from app.ai.errors import LLMError
from app.ai.types import Completion, GenerationConfig, Message, Token, Usage

TELUGU = "తెలుగు"
MESSAGES = [
    Message("system", "You are concise."),
    Message("user", f"Say hello in English, हिन्दी and {TELUGU}."),
]
CONFIG = GenerationConfig(temperature=0.2, max_output_tokens=64)


async def run_contract(provider: LLMProvider, model: str) -> None:
    assert provider.provider_id

    # complete(): non-empty text and a usage record with sane token counts.
    done = await provider.complete(MESSAGES, model, CONFIG)
    assert isinstance(done, Completion)
    assert done.text.strip()
    assert done.usage.input_tokens >= 0 and done.usage.output_tokens >= 0

    # stream_chat(): tokens (in order), then at most one trailing Usage; text is non-empty.
    events = [e async for e in provider.stream_chat(MESSAGES, model, CONFIG)]
    tokens = [e for e in events if isinstance(e, Token)]
    usages = [e for e in events if isinstance(e, Usage)]
    assert tokens and "".join(t.text for t in tokens).strip()
    assert len(usages) <= 1
    if usages:
        assert events[-1] is usages[0]
    # Multilingual text must survive a round trip unharmed.
    assert TELUGU in "".join(t.text for t in tokens)


async def assert_failures_are_taxonomy_errors(provider: LLMProvider, model: str) -> None:
    """Whatever goes wrong, only `LLMError` subclasses may escape an adapter."""
    try:
        await provider.complete(MESSAGES, model, CONFIG)
    except LLMError:
        pass
