"""Router: user selection -> fallback chain, with timeouts, circuit breakers and usage metering.

Fallback rules (docs/ARCHITECTURE.md section 6, D-33):
- Fall back on `Timeout`, `Unavailable`, `RateLimited` and server-side `AuthError`.
- `ContentFiltered` and invalid selections never fall back.
- A stream may fall back only BEFORE its first token is emitted; once tokens were sent to the
  caller a later failure is raised (the caller cannot un-send partial output).
- A provider whose breaker is open is skipped without being called.
"""

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncGenerator, Callable, Sequence
from dataclasses import dataclass

from app.ai.breaker import CircuitBreaker
from app.ai.errors import LLMError, Timeout, Unavailable
from app.ai.registry import ProviderRegistry, RegisteredProvider
from app.ai.types import (
    Capability,
    Completion,
    GenerationConfig,
    Message,
    RouterEvent,
    StreamMeta,
    Token,
    Usage,
)
from app.ai.usage import LoggingUsageSink, UsageRecord, UsageSink
from app.core.metrics import llm_request_duration, llm_requests

logger = logging.getLogger("app.ai.router")


@dataclass(frozen=True, slots=True)
class Selection:
    provider: str
    model: str


@dataclass(frozen=True, slots=True)
class RouterConfig:
    request_timeout_seconds: float = 30.0
    first_token_timeout_seconds: float = 4.0
    stream_idle_timeout_seconds: float = 15.0
    breaker_failure_threshold: int = 3
    breaker_cooldown_seconds: float = 30.0


class LLMRouter:
    def __init__(
        self,
        registry: ProviderRegistry,
        fallback_chain: Sequence[str],
        config: RouterConfig | None = None,
        usage_sink: UsageSink | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._registry = registry
        self._chain = [p for p in fallback_chain if registry.get(p) is not None]
        self._cfg = config or RouterConfig()
        self._sink: UsageSink = usage_sink or LoggingUsageSink()
        self._clock = clock
        self._breakers = {
            pid: CircuitBreaker(
                self._cfg.breaker_failure_threshold, self._cfg.breaker_cooldown_seconds, clock
            )
            for pid in registry.ids()
        }

    @property
    def registry(self) -> ProviderRegistry:
        return self._registry

    # ---------------------------------------------------------------- planning

    def breaker_state(self, provider_id: str) -> str:
        return self._breakers[provider_id].state

    def _plan(
        self, selection: Selection | None, capability: Capability
    ) -> list[tuple[RegisteredProvider, str]]:
        """Ordered (provider, model) attempts: validated selection first, then the chain."""
        plan: list[tuple[RegisteredProvider, str]] = []
        if selection is not None:
            entry, model = self._registry.resolve(selection.provider, selection.model, capability)
            plan.append((entry, model.id))
        for pid in self._chain:
            if any(e.spec.id == pid for e, _ in plan):
                continue
            entry_ = self._registry.get(pid)
            default = self._registry.default_model(pid, capability)
            if entry_ is not None and default is not None:
                plan.append((entry_, default.id))
        return plan

    def _record(
        self,
        entry: RegisteredProvider,
        model: str,
        started: float,
        user_id: uuid.UUID | None,
        usage: Usage | None,
        error: LLMError | None,
    ) -> None:
        llm_requests.labels(entry.spec.id, error.code if error else "success").inc()
        llm_request_duration.labels(entry.spec.id).observe(self._clock() - started)
        self._sink.record(
            UsageRecord(
                provider=entry.spec.id,
                model=model,
                input_tokens=usage.input_tokens if usage else None,
                output_tokens=usage.output_tokens if usage else None,
                latency_ms=int((self._clock() - started) * 1000),
                ok=error is None,
                error_code=error.code if error else None,
                user_id=user_id,
            )
        )

    def _settle(self, provider_id: str, error: LLMError | None) -> None:
        breaker = self._breakers[provider_id]
        if error is None:
            breaker.record_success()
        elif error.counts_against_provider:
            breaker.record_failure()
        else:
            breaker.release_probe()

    @staticmethod
    def _as_llm_error(exc: BaseException, provider_id: str) -> LLMError:
        """Adapters must raise LLMError; anything else is hidden behind a generic error."""
        if isinstance(exc, LLMError):
            return exc
        if isinstance(exc, TimeoutError):
            return Timeout()
        logger.warning("provider raised unexpected error", extra={"provider": provider_id})
        return Unavailable()

    # ------------------------------------------------------------------ calls

    async def complete(
        self,
        messages: Sequence[Message],
        config: GenerationConfig,
        *,
        selection: Selection | None = None,
        user_id: uuid.UUID | None = None,
    ) -> Completion:
        last_error: LLMError | None = None
        for entry, model in self._plan(selection, "completion"):
            breaker = self._breakers[entry.spec.id]
            if not breaker.allow():
                continue
            started = self._clock()
            try:
                async with asyncio.timeout(self._cfg.request_timeout_seconds):
                    result = await entry.provider.complete(messages, model, config)
            except Exception as exc:
                error = self._as_llm_error(exc, entry.spec.id)
                self._settle(entry.spec.id, error)
                self._record(entry, model, started, user_id, None, error)
                if not error.fallback_eligible:
                    raise error from None
                last_error = error
                continue
            except BaseException:  # cancellation: no verdict on provider health
                breaker.release_probe()
                raise
            self._settle(entry.spec.id, None)
            self._record(entry, model, started, user_id, result.usage, None)
            return Completion(result.text, result.usage, entry.spec.id, model)
        raise last_error or Unavailable()

    async def stream_chat(
        self,
        messages: Sequence[Message],
        config: GenerationConfig,
        *,
        selection: Selection | None = None,
        user_id: uuid.UUID | None = None,
    ) -> AsyncGenerator[RouterEvent, None]:
        last_error: LLMError | None = None
        for position, (entry, model) in enumerate(self._plan(selection, "streaming")):
            breaker = self._breakers[entry.spec.id]
            if not breaker.allow():
                continue
            started = self._clock()
            stream = entry.provider.stream_chat(messages, model, config)
            emitted = False
            usage: Usage | None = None
            # Fallback = the answering attempt is not the first planned one (a provider skipped
            # by an open breaker, or one that failed before its first token, counts too).
            meta = StreamMeta(entry.spec.id, model, fallback_used=position > 0)
            meta_sent = False
            try:
                timeout = self._cfg.first_token_timeout_seconds
                while True:
                    try:
                        async with asyncio.timeout(timeout):
                            event = await anext(stream)
                    except StopAsyncIteration:
                        break
                    timeout = self._cfg.stream_idle_timeout_seconds
                    if isinstance(event, Usage):
                        usage = event
                        continue
                    if isinstance(event, Token) and not meta_sent:
                        meta_sent = True
                        yield meta  # before the first token, so consumers know who is answering
                    emitted = emitted or isinstance(event, Token)
                    yield event
            except Exception as exc:
                error = self._as_llm_error(exc, entry.spec.id)
                self._settle(entry.spec.id, error)
                self._record(entry, model, started, user_id, None, error)
                if emitted or not error.fallback_eligible:
                    raise error from None
                last_error = error
                continue
            except BaseException:  # consumer closed the stream / cancelled: no health verdict
                breaker.release_probe()
                raise
            finally:
                aclose = getattr(stream, "aclose", None)
                if aclose is not None:
                    await aclose()
            self._settle(entry.spec.id, None)
            self._record(entry, model, started, user_id, usage, None)
            if not meta_sent:  # a stream without tokens still reports who answered
                yield meta
            if usage is not None:
                yield usage
            return
        raise last_error or Unavailable()
