"""Voice-listening (speech-to-text) registry and router (D-53, D-57).

A separate allow-list from the reasoning providers. The caller names ONE provider (the session's
selection) and that provider is exclusive: a failure is raised as a clear error and never retried
on another provider. A provider whose breaker is open is refused, and cancellation is no verdict on
provider health. Usage records carry identifiers, sizes and latency only: never audio or text.
"""

import asyncio
import logging
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from app.ai.adapters.stt import (
    DEFAULT_GROQ_STT_MODEL,
    DEFAULT_OPENAI_STT_MODEL,
    DEFAULT_SARVAM_STT_MODEL,
    GROQ_STT_BASE_URL,
    GROQ_STT_PROVIDER_ID,
    OPENAI_BASE_URL,
    OPENAI_STT_PROVIDER_ID,
    SARVAM_STT_PROVIDER_ID,
    OpenAICompatibleSTT,
    SarvamSTT,
    stt_spec,
)
from app.ai.breaker import CircuitBreaker
from app.ai.errors import InvalidSelection, LLMError, Timeout, Unavailable
from app.ai.fake_stt import FakeSTTProvider, fake_stt_spec
from app.ai.stt import AudioInput, STTProvider, SttSpec
from app.ai.types import Usage
from app.ai.usage import LoggingUsageSink, UsageRecord, UsageSink
from app.core.metrics import llm_request_duration, llm_requests

logger = logging.getLogger("app.ai.stt")

KNOWN_STT_PROVIDER_IDS: frozenset[str] = frozenset({"fake", "openai", "groq", "sarvam"})


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


class SttRouter:
    def __init__(
        self,
        entries: Sequence[RegisteredStt],
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
        return bool(self._entries)

    def ids(self) -> list[str]:
        return list(self._entries)

    def entries(self) -> list[RegisteredStt]:
        return list(self._entries.values())

    def resolve(self, provider_id: str, model_id: str | None = None) -> RegisteredStt:
        """Validate a selection against the allow-list. `model_id` None = the configured model."""
        entry = self._entries.get(provider_id)
        if entry is None or (model_id is not None and model_id != entry.spec.model_id):
            raise InvalidSelection()
        return entry

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
        self,
        audio: AudioInput,
        language: str | None,
        *,
        provider: str,
        model: str | None = None,
        user_id: uuid.UUID | None = None,
    ) -> Transcription:
        entry = self.resolve(provider, model)  # raises InvalidSelection (never falls back)
        breaker = self._breakers[provider]
        if not breaker.allow():
            raise Unavailable()
        started = self._clock()
        try:
            async with asyncio.timeout(self._cfg.request_timeout_seconds):
                result = await entry.provider.transcribe(audio, language, entry.spec.model_id)
        except Exception as exc:
            error = self._as_llm_error(exc, provider)
            if error.counts_against_provider:
                breaker.record_failure()
            else:
                breaker.release_probe()
            self._record(entry, started, user_id, None, error)
            raise error from None
        except BaseException:  # cancellation: no verdict on provider health
            breaker.release_probe()
            raise
        breaker.record_success()
        self._record(entry, started, user_id, result.usage, None)
        return Transcription(result.text, provider, entry.spec.model_id, result.usage)

    async def aclose(self) -> None:
        for entry in self._entries.values():
            closer = getattr(entry.provider, "aclose", None)
            if closer is not None:
                await closer()


def build_stt_router(
    enabled: Sequence[str],
    config: SttRouterConfig | None = None,
    *,
    openai_api_key: str | None = None,
    openai_model: str = DEFAULT_OPENAI_STT_MODEL,
    groq_api_key: str | None = None,
    groq_model: str = DEFAULT_GROQ_STT_MODEL,
    sarvam_api_key: str | None = None,
    sarvam_model: str = DEFAULT_SARVAM_STT_MODEL,
) -> SttRouter:
    """Build the voice-listening allow-list from configuration (empty = audio input disabled)."""
    timeout = (config or SttRouterConfig()).request_timeout_seconds
    entries: list[RegisteredStt] = []
    for provider_id in enabled:
        if provider_id == "fake":
            entries.append(RegisteredStt(FakeSTTProvider("fake"), fake_stt_spec("fake")))
        elif provider_id == OPENAI_STT_PROVIDER_ID:
            if not openai_api_key:
                raise ValueError("OPENAI_API_KEY is required when OpenAI voice is enabled")
            entries.append(
                RegisteredStt(
                    OpenAICompatibleSTT(
                        provider_id, openai_api_key, openai_model, timeout, base_url=OPENAI_BASE_URL
                    ),
                    stt_spec(provider_id, openai_model, f"OpenAI transcription ({openai_model})"),
                )
            )
        elif provider_id == GROQ_STT_PROVIDER_ID:
            if not groq_api_key:
                raise ValueError("GROQ_API_KEY is required when Groq voice is enabled")
            entries.append(
                RegisteredStt(
                    OpenAICompatibleSTT(
                        provider_id, groq_api_key, groq_model, timeout, base_url=GROQ_STT_BASE_URL
                    ),
                    stt_spec(provider_id, groq_model, f"Groq Whisper ({groq_model})"),
                )
            )
        elif provider_id == SARVAM_STT_PROVIDER_ID:
            if not sarvam_api_key:
                raise ValueError("SARVAM_API_KEY is required when Sarvam voice is enabled")
            entries.append(
                RegisteredStt(
                    SarvamSTT(sarvam_api_key, sarvam_model, timeout),
                    stt_spec(provider_id, sarvam_model, f"Sarvam ({sarvam_model})"),
                )
            )
        else:  # config validation already rejects unknown ids; fail closed regardless
            raise ValueError("unknown stt provider id")
    return SttRouter(entries, config)
