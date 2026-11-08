"""`AnswerConfig`: the compiled, typed answer preferences of a session (docs/DECISIONS.md D-34).

Separate from the Phase 6 `app.ai.types.GenerationConfig`, which only carries sampling parameters
for a provider call. `CompiledPreferences` bundles both.
"""

from dataclasses import dataclass
from typing import Any

from app.ai.types import GenerationConfig
from app.preferences.enums import AnswerFormat, AnswerLength, AnswerTone, Language, QuestionType


@dataclass(frozen=True, slots=True)
class AnswerConfig:
    language: Language
    question_type: QuestionType
    answer_format: AnswerFormat
    answer_length: AnswerLength
    answer_tone: AnswerTone
    extra_instructions: str  # already cleaned and length-checked; "" when none
    target_min_words: int
    target_max_words: int
    provider: str | None = None  # None = server default chain (allowlist check happens at use)
    model: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "language": self.language.value,
            "question_type": self.question_type.value,
            "answer_format": self.answer_format.value,
            "answer_length": self.answer_length.value,
            "answer_tone": self.answer_tone.value,
            "extra_instructions": self.extra_instructions,
            "target_min_words": self.target_min_words,
            "target_max_words": self.target_max_words,
            "provider": self.provider,
            "model": self.model,
        }


@dataclass(frozen=True, slots=True)
class CompiledPreferences:
    answer: AnswerConfig
    generation: GenerationConfig

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable snapshot (stored as `session_preferences.generation_config` in 7B)."""
        return {
            "answer": self.answer.to_dict(),
            "generation": {
                "temperature": self.generation.temperature,
                "max_output_tokens": self.generation.max_output_tokens,
            },
        }
