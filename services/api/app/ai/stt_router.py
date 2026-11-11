"""Speech-to-text routing: allow-listed providers, fallback chain, timeout, breaker, metering.

The same rules as `LLMRouter`/`VisionRouter` (D-33): fall back on timeout/unavailable/rate-limit,
never on content filtering, skip a provider whose breaker is open, and treat cancellation as no
verdict on provider health. Usage records carry identifiers, sizes and latency only: never audio
or transcript text.
"""

import asyncio
import logging
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from app.ai.breaker import CircuitBreaker
from app.ai.errors import LLMError, Timeout, Unavailable
from app.ai.fake_stt import FakeSTTProvider, fake_stt_spec
from app.ai.stt import AudioInput, STTProvider, SttSpec
from app.ai.types import Usage
from app.ai.usage import LoggingUsageSink, UsageRecord, UsageSink
from app.core.metrics import llm_request_duration, llm_requests

logger = logging.getLogger("app.ai.stt")

# Only the deterministic fake exists. A real adapter needs a separate approval (D-53, OQ-5).
KNOWN_STT_PROVIDER_IDS: frozenset[str] = frozenset({"fake"})


@dataclass(frozen=True, slots=True)
class SttRouterConfig:
    request_timeout_seconds: float = 20.0
    breaker_failure_threshold: int = 3
    breaker_cooldown_seconds: float = 30.0


@dataclass(frozen=True, slots=True)
class RegisteredStt:
    provider: STTProvider
    spec: SttSpec


@dataclass(frozen=True, slots=True)
class Transcription:
    text: str = field(repr=False)  # a transcript: never log, never a label
    provider: str = ""
    model: str = ""
    usage: Usage | None = None
    fallback_used: bool = False


class SttRouter:
    def __init__(
        self,
        entries: Sequence[RegisteredStt],
        chain: Sequence[str] | None = None,
        config: SttRouterConfig | None = None,
        usage_sink: UsageSink | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        ids = [e.spec.provider_id for e in entries]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate stt provider id")
        for e in entries:
            if e.provider.provider_id != e.spec.provider_id:
                raise ValueError("stt provider id does not match its spec")
        self._entries = {e.spec.provider_id: e for e in entries}
        self._chain = [p for p in (chain or ids) if p in self._entries]
        self._cfg = config or SttRouterConfig()
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
        entry: RegisteredStt,
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
        # Provider id and exception class only: the exception text could echo request content.
        logger.warning(
            "stt provider raised unexpected error",
            extra={"provider": provider_id, "error_type": type(exc).__name__},
        )
        return Unavailable()

    async def transcribe(
        self, audio: AudioInput, language: str | None, *, user_id: uuid.UUID | None = None
    ) -> Transcription:
        last_error: LLMError | None = None
        for position, pid in enumerate(self._chain):
            entry = self._entries[pid]
            breaker = self._breakers[pid]
            if not breaker.allow():
                continue
            started = self._clock()
            try:
                async with asyncio.timeout(self._cfg.request_timeout_seconds):
                    result = await entry.provider.transcribe(audio, language, entry.spec.model_id)
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
            return Transcription(
                result.text, pid, entry.spec.model_id, result.usage, fallback_used=position > 0
            )
        raise last_error or Unavailable()


def build_stt_router(
    enabled: Sequence[str],
    chain: Sequence[str] | None = None,
    config: SttRouterConfig | None = None,
) -> SttRouter:
    """Build the speech-to-text allow-list from configuration (empty = audio input disabled)."""
    entries: list[RegisteredStt] = []
    for provider_id in enabled:
        if provider_id == "fake":
            entries.append(RegisteredStt(FakeSTTProvider("fake"), fake_stt_spec("fake")))
        else:  # config validation already rejects unknown ids; fail closed regardless
            raise ValueError("unknown stt provider id")
    return SttRouter(entries, chain, config)
