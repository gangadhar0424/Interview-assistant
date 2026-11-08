"""DEV ONLY: the real API with a slow, canned fake LLM, for manual browser testing of Phase 8.

    uv run uvicorn tests.manual_qa_server:app --port 8000

Why this exists: the built-in `fake` provider answers instantly and echoes the whole prompt, so
streaming, Stop and "leave the page mid-answer" cannot be seen in a browser. This module wires the
normal application exactly as `app.main` does, then replaces ONLY the LLM router (and the answerer
that uses it) with one slow fake provider, the same way the automated tests do. Nothing under
`app/` is changed, no real model is involved and no credentials are used. It refuses to start
unless APP_ENV=local.

The answer is a fixed paragraph streamed one word at a time (MANUAL_QA_TOKEN_DELAY seconds apart,
default 0.15). Put one of these markers anywhere in your question to trigger a failure:

    [fail]     the provider is unavailable before any text (-> "The AI service is unavailable")
    [partial]  the provider fails after 8 words (-> partial text kept + the failure message)
"""

import asyncio
import os
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.ai.errors import Unavailable
from app.ai.fake import FakeLLMProvider, fake_spec
from app.ai.registry import ProviderRegistry, RegisteredProvider
from app.ai.router import LLMRouter, RouterConfig
from app.ai.types import GenerationConfig, Message, StreamEvent, Token, Usage
from app.core.config import get_settings
from app.embeddings.factory import build_embedding_provider
from app.main import create_app
from app.qa.service import QuestionAnswerer

ANSWER = (
    "This is a canned answer from the manual test provider. It streams one word at a time so you "
    "can watch the text arrive, press Stop part way through, or leave the page while it is still "
    "being written. In a real session this text would be built from your resume, the documents "
    "you selected and the job description, and it would follow the format, length and tone chosen "
    "for the session. Nothing here comes from a real model."
)


class ManualProvider(FakeLLMProvider):
    def __init__(self, token_delay: float) -> None:
        super().__init__("fake")
        self._token_delay = token_delay

    async def stream_chat(
        self, messages: Sequence[Message], model: str, config: GenerationConfig
    ) -> AsyncIterator[StreamEvent]:
        self.calls += 1
        self.models_seen.append(model)
        prompt = "\n".join(m.content for m in messages)
        if "[fail]" in prompt:
            raise Unavailable()
        words = ANSWER.split(" ")
        for i, word in enumerate(words):
            if "[partial]" in prompt and i == 8:
                raise Unavailable()
            await asyncio.sleep(self._token_delay)
            yield Token(word if i == 0 else " " + word)
        yield Usage(len(prompt.split()), len(words))


def build() -> FastAPI:
    settings = get_settings()
    if settings.app_env != "local":
        raise SystemExit("manual_qa_server is for APP_ENV=local only.")
    delay = float(os.environ.get("MANUAL_QA_TOKEN_DELAY", "0.15"))
    app = create_app()
    normal_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        async with normal_lifespan(application):
            provider = ManualProvider(delay)
            registry = ProviderRegistry([RegisteredProvider(provider, fake_spec("fake"))])
            router = LLMRouter(
                registry,
                registry.ids(),
                RouterConfig(
                    request_timeout_seconds=settings.llm_request_timeout_seconds,
                    first_token_timeout_seconds=settings.llm_first_token_timeout_seconds,
                    stream_idle_timeout_seconds=settings.llm_stream_idle_timeout_seconds,
                    breaker_failure_threshold=settings.llm_breaker_failure_threshold,
                    breaker_cooldown_seconds=settings.llm_breaker_cooldown_seconds,
                ),
            )
            application.state.llm_registry = registry
            application.state.llm_router = router
            application.state.question_answerer = QuestionAnswerer(
                application.state.sessionmaker,
                settings,
                router,
                build_embedding_provider(settings),
            )
            yield

    app.router.lifespan_context = lifespan
    return app


app = build()
