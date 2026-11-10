"""Deterministic fake vision provider for tests/local development. No model, no network.

`reply` may contain `{width}`, `{height}`, `{size}` and `{digest}` (first 12 hex characters of the
image's SHA-256), so a test can prove which image reached the provider without the provider ever
storing or printing it. `failures` is consumed one entry per call (an `LLMError` is raised, `None`
means succeed), exactly like `FakeLLMProvider`; `delay` makes the call slow (timeouts, cancel).
"""

import asyncio
from collections.abc import Sequence

from app.ai.errors import LLMError
from app.ai.types import Usage
from app.ai.vision import ImageInput, VisionResult, VisionSpec

FAKE_VISION_MODEL_ID = "fake-vision"


def fake_vision_spec(provider_id: str = "fake") -> VisionSpec:
    return VisionSpec(provider_id, FAKE_VISION_MODEL_ID, "Fake vision (test only)")


class FakeVisionProvider:
    def __init__(
        self,
        provider_id: str = "fake",
        *,
        reply: str = "Fake extraction: {width}x{height} image {digest}",
        failures: Sequence[LLMError | None] = (),
        delay: float = 0.0,
    ) -> None:
        self.provider_id = provider_id
        self._reply = reply
        self._failures = list(failures)
        self._delay = delay
        self.calls = 0
        self.cancelled = 0
        # Test introspection only: digests and instructions, never the image bytes.
        self.digests_seen: list[str] = []
        self.instructions_seen: list[str] = []

    async def extract_text(self, instructions: str, image: ImageInput, model: str) -> VisionResult:
        self.calls += 1
        self.digests_seen.append(image.sha256())
        self.instructions_seen.append(instructions)
        try:
            if self._delay:
                await asyncio.sleep(self._delay)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        failure = self._failures.pop(0) if self._failures else None
        if failure is not None:
            raise failure
        text = (
            self._reply.replace("{width}", str(image.width))
            .replace("{height}", str(image.height))
            .replace("{size}", str(image.size_bytes))
            .replace("{digest}", image.sha256()[:12])
        )
        usage = Usage(input_tokens=image.size_bytes // 750, output_tokens=len(text.split()))
        return VisionResult(text, usage)
