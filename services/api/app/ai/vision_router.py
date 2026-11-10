"""Vision routing: allow-listed providers, fallback chain, timeout, circuit breaker, metering.

A thin counterpart of `LLMRouter` for one bounded, non-streaming call. Same rules (D-33): fall back
on timeout/unavailable/rate-limit, never on content filtering; a provider with an open breaker is
skipped; cancellation gives no verdict on provider health. Usage records carry identifiers, sizes
and latency only, never the image or the extracted text.
"""

import asyncio
import logging
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from app.ai.breaker import CircuitBreaker
from app.ai.errors import LLMError, Timeout, Unavailable
from app.ai.fake_vision import FakeVisionProvider, fake_vision_spec
from app.ai.types import Usage
from app.ai.usage import LoggingUsageSink, UsageRecord, UsageSink
from app.ai.vision import ImageInput, VisionProvider, VisionSpec
from app.core.metrics import llm_request_duration, llm_requests

logger = logging.getLogger("app.ai.vision")

# Only the deterministic fake exists. A real adapter needs a separate approval (D-51).
KNOWN_VISION_PROVIDER_IDS: frozenset[str] = frozenset({"fake"})


@dataclass(frozen=True, slots=True)
class VisionRouterConfig:
    request_timeout_seconds: float = 20.0
    breaker_failure_threshold: int = 3
    breaker_cooldown_seconds: float = 30.0


@dataclass(frozen=True, slots=True)
class RegisteredVision:
    provider: VisionProvider
    spec: VisionSpec


@dataclass(frozen=True, slots=True)
class VisionExtraction:
    text: str = field(repr=False)  # extracted content: never log, never a label
    provider: str = ""
    model: str = ""
    usage: Usage | None = None
    fallback_used: bool = False


class VisionRouter:
    def __init__(
        self,
        entries: Sequence[RegisteredVision],
        chain: Sequence[str] | None = None,
        config: VisionRouterConfig | None = None,
        usage_sink: UsageSink | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        ids = [e.spec.provider_id for e in entries]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate vision provider id")
        for e in entries:
            if e.provider.provider_id != e.spec.provider_id:
                raise ValueError("vision provider id does not match its spec")
        self._entries = {e.spec.provider_id: e for e in entries}
        self._chain = [p for p in (chain or ids) if p in self._entries]
        self._cfg = config or VisionRouterConfig()
        self._sink: UsageSink = usage_sink or LoggingUsageSink()
        self._clock = clock
        self._breakers = {
            pid: CircuitBreaker(
                self._cfg.breaker_failure_threshold, self._cfg.breaker_cooldown_seconds, clock
            )
            for pid in self._entries
        }

    @property
    def available(self) -> bool:
        return bool(self._chain)

    def ids(self) -> list[str]:
        return list(self._entries)

    def breaker_state(self, provider_id: str) -> str:
        return self._breakers[provider_id].state

    def _record(
        self,
        entry: RegisteredVision,
        started: float,
        user_id: uuid.UUID | None,
        usage: Usage | None,
        error: LLMError | None,
    ) -> None:
        pid = entry.spec.provider_id
        llm_requests.labels(pid, error.code if error else "success").inc()
        llm_request_duration.labels(pid).observe(self._clock() - started)
        self._sink.record(
            UsageRecord(
                provider=pid,
                model=entry.spec.model_id,
                input_tokens=usage.input_tokens if usage else None,
                output_tokens=usage.output_tokens if usage else None,
                latency_ms=int((self._clock() - started) * 1000),
                ok=error is None,
                error_code=error.code if error else None,
                user_id=user_id,
            )
        )

    @staticmethod
    def _as_llm_error(exc: BaseException, provider_id: str) -> LLMError:
        if isinstance(exc, LLMError):
            return exc
        if isinstance(exc, TimeoutError):
            return Timeout()
        # Only the provider id and the exception class: never the exception text (it could echo
        # request content).
        logger.warning(
            "vision provider raised unexpected error",
            extra={"provider": provider_id, "error_type": type(exc).__name__},
        )
        return Unavailable()

    async def extract(
        self, instructions: str, image: ImageInput, *, user_id: uuid.UUID | None = None
    ) -> VisionExtraction:
        last_error: LLMError | None = None
        for position, pid in enumerate(self._chain):
            entry = self._entries[pid]
            breaker = self._breakers[pid]
            if not breaker.allow():
                continue
            started = self._clock()
            try:
                async with asyncio.timeout(self._cfg.request_timeout_seconds):
                    result = await entry.provider.extract_text(
                        instructions, image, entry.spec.model_id
                    )
            except Exception as exc:
                error = self._as_llm_error(exc, pid)
                if error.counts_against_provider:
                    breaker.record_failure()
                else:
                    breaker.release_probe()
                self._record(entry, started, user_id, None, error)
                if not error.fallback_eligible:
                    raise error from None
                last_error = error
                continue
            except BaseException:  # cancellation: no verdict on provider health
                breaker.release_probe()
                raise
            breaker.record_success()
            self._record(entry, started, user_id, result.usage, None)
            return VisionExtraction(
                result.text, pid, entry.spec.model_id, result.usage, fallback_used=position > 0
            )
        raise last_error or Unavailable()


def build_vision_router(
    enabled: Sequence[str],
    chain: Sequence[str] | None = None,
    config: VisionRouterConfig | None = None,
) -> VisionRouter:
    """Build the vision allow-list from configuration (empty = screenshots disabled)."""
    entries: list[RegisteredVision] = []
    for provider_id in enabled:
        if provider_id == "fake":
            entries.append(RegisteredVision(FakeVisionProvider("fake"), fake_vision_spec("fake")))
        else:  # config validation already rejects unknown ids; fail closed regardless
            raise ValueError("unknown vision provider id")
    return VisionRouter(entries, chain, config)
