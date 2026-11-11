"""The `STTProvider` port (Phase 12, D-53): one utterance of audio in, plain text out.

Separate from `LLMProvider` and `VisionProvider`: the text and vision ports are unchanged. Speech
to text is utterance-at-a-time (no streaming partials), which keeps the contract small and fully
deterministic to test.

Privacy: an `AudioInput` holds raw speech and an `SttResult` holds a transcript. Neither may ever
be logged, used as a metric label or put in an error message, so their `repr` hides the content and
adapters must raise only `LLMError` subclasses (fixed safe messages).
"""

import hashlib
from dataclasses import dataclass, field
from typing import Protocol

from app.ai.types import Usage


@dataclass(frozen=True, slots=True)
class AudioInput:
    """A validated utterance (see `app.audio.validation`): canonical 16 kHz mono PCM16 WAV bytes."""

    data: bytes = field(repr=False)
    sample_rate: int
    duration_ms: int
    mime: str = "audio/wav"

    @property
    def size_bytes(self) -> int:
        return len(self.data)

    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


@dataclass(frozen=True, slots=True)
class SttResult:
    text: str = field(repr=False)
    usage: Usage | None = None


class STTProvider(Protocol):
    provider_id: str

    async def transcribe(self, audio: AudioInput, language: str | None, model: str) -> SttResult:
        """Return what was said. Must raise only `LLMError` subclasses."""
        ...


@dataclass(frozen=True, slots=True)
class SttSpec:
    provider_id: str
    model_id: str
    label: str
