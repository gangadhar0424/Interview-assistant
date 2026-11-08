"""Test-only doubles for the Phase 8A pipeline (no real provider, no credentials).

`RecordingProvider` records every call it receives (so tests can inspect exactly what reached the
provider) and builds a reply that follows the formatting guidance in the system prompt (format,
length band, language), so format/length conformance checks exercise the whole pipeline. This is a
plumbing check: it proves preferences reach the provider call, not that a model obeys them.
"""

import asyncio
import re
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field

from app.ai.fake import FakeLLMProvider
from app.ai.types import GenerationConfig, Message, StreamEvent, Token, Usage

BLOCK = re.compile(r"^<<<BEGIN UNTRUSTED:(\w+)>>>\n(.*?)\n<<<END UNTRUSTED:\1>>>$", re.S | re.M)
LENGTH = re.compile(r"Aim for (\d+) to (\d+) words")


@dataclass
class RecordedCall:
    messages: list[Message]
    model: str
    config: GenerationConfig

    @property
    def system(self) -> str:
        return self.messages[0].content

    @property
    def user(self) -> str:
        return self.messages[-1].content

    def block(self, label: str) -> str:
        return str(dict(BLOCK.findall(self.user))[label])


@dataclass
class OpenSessionTracker:
    """Counts database sessions currently open (wraps a sessionmaker)."""

    open: int = 0
    max_open: int = 0
    seen_open_at_provider_call: list[int] = field(default_factory=list)


class TrackedSessionMaker:
    """Drop-in for `async_sessionmaker` that tracks how many sessions are open at once."""

    def __init__(self, inner: Callable[[], object], tracker: OpenSessionTracker) -> None:
        self._inner = inner
        self._tracker = tracker

    def __call__(self) -> "_TrackedSession":
        return _TrackedSession(self._inner(), self._tracker)


class _TrackedSession:
    def __init__(self, session: object, tracker: OpenSessionTracker) -> None:
        self._session = session
        self._tracker = tracker

    async def __aenter__(self) -> object:
        self._tracker.open += 1
        self._tracker.max_open = max(self._tracker.max_open, self._tracker.open)
        return await self._session.__aenter__()  # type: ignore[attr-defined]

    async def __aexit__(self, *exc: object) -> object:
        try:
            return await self._session.__aexit__(*exc)  # type: ignore[attr-defined]
        finally:
            self._tracker.open -= 1


class RecordingProvider(FakeLLMProvider):
    def __init__(
        self,
        provider_id: str = "fake",
        *,
        tracker: OpenSessionTracker | None = None,
        reply_fn: Callable[[Sequence[Message]], str] | None = None,
        **kw: object,
    ) -> None:
        super().__init__(provider_id, **kw)  # type: ignore[arg-type]
        self.recorded: list[RecordedCall] = []
        self._tracker = tracker
        self._reply_fn = reply_fn

    def _text(self, messages: Sequence[Message]) -> str:
        if self._reply_fn is not None:
            return self._reply_fn(messages)
        return build_reply(messages[0].content)

    async def stream_chat(
        self, messages: Sequence[Message], model: str, config: GenerationConfig
    ) -> AsyncIterator[StreamEvent]:
        self.recorded.append(RecordedCall(list(messages), model, config))
        if self._tracker is not None:
            self._tracker.seen_open_at_provider_call.append(self._tracker.open)
        async for event in super().stream_chat(messages, model, config):
            yield event


def build_reply(system: str) -> str:
    """A reply shaped by the system prompt: format, length band midpoint and language."""
    band = LENGTH.search(system)
    words = (int(band.group(1)) + int(band.group(2))) // 2 if band else 20
    if "short bullet points of key phrases" in system:
        per = 4
        lines = [" ".join(f"point{i}w{j}" for j in range(per)) for i in range(max(1, words // per))]
        return "\n".join(f"- {line}" for line in lines)
    if "every bullet is a complete first-person sentence" in system:
        per = 8
        lines = [
            "I " + " ".join(f"did{i}w{j}" for j in range(per - 2)) + "."
            for i in range(max(1, words // per))
        ]
        return "\n".join(f"- {line}" for line in lines)
    # full spoken script: connected sentences, no bullets, no headings
    sentences = [" ".join(f"s{i}w{j}" for j in range(10)) + "." for i in range(max(1, words // 10))]
    return " ".join(sentences)


def word_count(text: str) -> int:
    return len([w for w in re.split(r"\s+", text.replace("- ", " ")) if w])


def is_bullet_list(text: str) -> bool:
    lines = [ln for ln in text.split("\n") if ln.strip()]
    return bool(lines) and all(ln.startswith("- ") for ln in lines)


def answer_for_question(messages: Sequence[Message]) -> str:
    """A reply that names the question it answers (lets history tests tell answers apart)."""
    question = str(dict(BLOCK.findall(messages[-1].content))["question"])
    return "ANSWER-FOR-" + question.replace(" ", "_")


class SlowTokensProvider(FakeLLMProvider):
    """Emits one token every `token_delay` seconds and records whether its stream was closed.

    `emitted` counts tokens produced; `closed` becomes True when the stream is closed or finishes
    (proving that a client disconnect reaches the provider stream). Plain attributes, so a test
    thread can poll them while the server runs in another thread.
    """

    def __init__(self, provider_id: str = "slow", *, token_delay: float = 0.1, tokens: int = 60):
        super().__init__(provider_id)
        self.token_delay = token_delay
        self.tokens = tokens
        self.emitted = 0
        self.closed = False
        self.finished = False

    async def stream_chat(
        self, messages: Sequence[Message], model: str, config: GenerationConfig
    ) -> AsyncIterator[StreamEvent]:
        self.calls += 1
        try:
            for i in range(self.tokens):
                await asyncio.sleep(self.token_delay)
                self.emitted += 1
                yield Token(f"word{i} ")
            self.finished = True
            yield Usage(1, self.tokens)
        finally:
            self.closed = True
