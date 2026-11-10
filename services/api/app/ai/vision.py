"""The `VisionProvider` port (Phase 11A, D-51): image in, plain text out.

Deliberately separate from `LLMProvider`: the text port, `Message` and the text router are
unchanged, so text-only providers need no image support and screenshots can never reach one.

Privacy: an `ImageInput` holds raw screenshot bytes and a `VisionResult` holds extracted text.
Neither may ever be logged, used as a metric label or put in an error message, so their `repr`
hides the content and adapters must raise only `LLMError` subclasses (fixed safe messages).
"""

import hashlib
from dataclasses import dataclass, field
from typing import Protocol

from app.ai.types import Usage


@dataclass(frozen=True, slots=True)
class ImageInput:
    """A validated PNG (see `app.screenshots.validation`). `data` is never shown by `repr`."""

    data: bytes = field(repr=False)
    width: int
    height: int
    mime: str = "image/png"

    @property
    def size_bytes(self) -> int:
        return len(self.data)

    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


@dataclass(frozen=True, slots=True)
class VisionResult:
    text: str = field(repr=False)
    usage: Usage | None = None


class VisionProvider(Protocol):
    provider_id: str

    async def extract_text(self, instructions: str, image: ImageInput, model: str) -> VisionResult:
        """Return the question/problem visible in `image`. Must raise only `LLMError` subclasses."""
        ...


@dataclass(frozen=True, slots=True)
class VisionSpec:
    provider_id: str
    model_id: str
    label: str
