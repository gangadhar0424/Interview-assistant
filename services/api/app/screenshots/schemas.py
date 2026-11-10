"""Response model for screenshot extraction (contract: docs/API.md, D-51).

Only the extracted text and routing facts: never the image, its bytes or any digest of it.
"""

from pydantic import BaseModel


class ScreenshotText(BaseModel):
    extracted_text: str  # cleaned, at most 2000 characters; empty when no question was found
    truncated: bool  # True when the text was shortened to the question limit
    provider: str
    model: str
    latency_ms: int
