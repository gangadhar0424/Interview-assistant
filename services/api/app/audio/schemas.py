"""Response model for utterance transcription (contract: docs/API.md, D-53).

Only the transcript, the advisory question flag and routing facts: never the audio, its bytes or any
digest of it.
"""

from pydantic import BaseModel


class TranscriptionOut(BaseModel):
    text: str  # cleaned, at most 2000 characters; empty when nothing intelligible was said
    truncated: bool  # True when the text was shortened to the question limit
    is_question: bool  # advisory only (simple English heuristic)
    confidence: float  # 0..1
    provider: str
    model: str
    latency_ms: int
