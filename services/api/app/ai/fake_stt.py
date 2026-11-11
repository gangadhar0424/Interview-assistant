"""Deterministic fake speech-to-text provider for tests/local development. No model, no network.

`script` is consumed one entry per call (a scripted conversation); when it is empty `reply` is used.
`reply` may contain `{duration_ms}`, `{size}` and `{digest}` (first 12 hex characters of the audio's
SHA-256), so a test can prove which audio reached the provider without the provider storing or
printing it. `failures` is consumed one entry per call (an `LLMError` is raised, `None` means
succeed); `delay` makes the call slow (timeouts, cancellation).
"""

import asyncio
from collections.abc import Sequence

from app.ai.errors import LLMError
from app.ai.stt import AudioInput, SttResult, SttSpec
from app.ai.types import Usage

FAKE_STT_MODEL_ID = "fake-stt"


def fake_stt_spec(provider_id: str = "fake") -> SttSpec:
    return SttSpec(provider_id, FAKE_STT_MODEL_ID, "Fake speech-to-text (test only)")


class FakeSTTProvider:
    def __init__(
        self,
        provider_id: str = "fake",
        *,
        reply: str = "Fake transcript: {duration_ms} ms {digest}",
        script: Sequence[str] = (),
        failures: Sequence[LLMError | None] = (),
        delay: float = 0.0,
    ) -> None:
        self.provider_id = provider_id
        self._reply = reply
        self._script = list(script)
        self._failures = list(failures)
        self._delay = delay
        self.calls = 0
        self.cancelled = 0
        # Test introspection only: digests and languages, never the audio bytes.
        self.digests_seen: list[str] = []
        self.languages_seen: list[str | None] = []

    async def transcribe(self, audio: AudioInput, language: str | None, model: str) -> SttResult:
        self.calls += 1
        self.digests_seen.append(audio.sha256())
        self.languages_seen.append(language)
        try:
            if self._delay:
                await asyncio.sleep(self._delay)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        failure = self._failures.pop(0) if self._failures else None
        if failure is not None:
            raise failure
        template = self._script.pop(0) if self._script else self._reply
        text = (
            template.replace("{duration_ms}", str(audio.duration_ms))
            .replace("{size}", str(audio.size_bytes))
            .replace("{digest}", audio.sha256()[:12])
        )
        usage = Usage(input_tokens=audio.duration_ms // 100, output_tokens=len(text.split()))
        return SttResult(text, usage)
