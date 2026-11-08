"""PreferenceCompiler: the only place mapping preference values to answer/sampling configuration.

Pure and deterministic: no database, no provider calls, no registry. Allowlist validation of the
optional provider/model happens where a session is created (Phase 7B), not here.
"""

import math
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from app.ai.types import GenerationConfig
from app.preferences.answer_config import AnswerConfig, CompiledPreferences
from app.preferences.enums import AnswerFormat, AnswerLength, AnswerTone, Language, QuestionType
from app.preferences.errors import InvalidPreference
from app.preferences.sanitize import clean_text

if TYPE_CHECKING:
    from app.core.config import Settings

# Provider/model identifiers are opaque ids from config; only their shape is checked here.
_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}$")
_INDIC = frozenset({Language.HI, Language.TE})
_MAX_OUTPUT_TOKENS_CAP = 32768  # the Phase 6 GenerationConfig ceiling

# Sampling temperature per question type: factual/technical answers are sampled more tightly.
# Tunable starting values, unverified against any real model (no adapter exists yet, OQ-4).
_TEMPERATURE: dict[QuestionType, float] = {
    QuestionType.AUTO: 0.3,
    QuestionType.BEHAVIORAL: 0.5,
    QuestionType.CODING: 0.2,
    QuestionType.EXPERIENCE_CHECK: 0.3,
    QuestionType.HOW_DO_YOU: 0.4,
    QuestionType.SITUATIONAL: 0.5,
    QuestionType.SYSTEM_DESIGN: 0.2,
    QuestionType.TECHNICAL_KNOWLEDGE: 0.2,
    QuestionType.TELL_ME_ABOUT_YOURSELF: 0.5,
}


@dataclass(frozen=True, slots=True)
class LengthTargets:
    """(min_words, max_words) per answer length, plus the token-budget factors."""

    bands: dict[AnswerLength, tuple[int, int]]
    tokens_per_word: float = 2.0
    indic_token_multiplier: float = 2.5


@dataclass(frozen=True, slots=True)
class PreferencesInput:
    """Raw preference values as received (strings). Validated by `PreferenceCompiler.compile`."""

    language: str
    question_type: str
    answer_format: str
    answer_length: str
    answer_tone: str
    extra_instructions: str | None = None
    provider: str | None = None
    model: str | None = None


def _parse_enum[E: StrEnum](enum_type: type[E], value: object, field: str) -> E:
    """Exact (case-sensitive) match on the stable identifier; unknown values are rejected."""
    if not isinstance(value, str):
        raise InvalidPreference(field)
    try:
        return enum_type(value)
    except ValueError:
        raise InvalidPreference(field) from None


def _parse_optional_id(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _ID_PATTERN.fullmatch(value):
        raise InvalidPreference(field)
    return value


class PreferenceCompiler:
    def __init__(self, targets: LengthTargets, extra_instructions_max_chars: int = 1000) -> None:
        missing = set(AnswerLength) - set(targets.bands)
        if missing:
            raise ValueError("length targets must define every answer length")
        self._targets = targets
        self._max_extra = extra_instructions_max_chars

    @property
    def max_extra_instructions(self) -> int:
        return self._max_extra

    @classmethod
    def from_settings(cls, settings: "Settings") -> "PreferenceCompiler":
        s = settings
        return cls(
            LengthTargets(
                bands={
                    AnswerLength.SHORT: (s.answer_short_min_words, s.answer_short_max_words),
                    AnswerLength.BALANCED: (
                        s.answer_balanced_min_words,
                        s.answer_balanced_max_words,
                    ),
                    AnswerLength.LONG: (s.answer_long_min_words, s.answer_long_max_words),
                },
                tokens_per_word=s.answer_tokens_per_word,
                indic_token_multiplier=s.answer_indic_token_multiplier,
            ),
            s.extra_instructions_max_chars,
        )

    def _clean_extra(self, value: str | None) -> str:
        if value is None:
            return ""
        if not isinstance(value, str):
            raise InvalidPreference("extra_instructions")
        cleaned = clean_text(value)
        if len(cleaned) > self._max_extra:  # rejected, never silently truncated
            raise InvalidPreference("extra_instructions")
        return cleaned

    def _max_output_tokens(self, language: Language, max_words: int) -> int:
        factor = self._targets.tokens_per_word
        if language in _INDIC:
            factor *= self._targets.indic_token_multiplier
        return min(_MAX_OUTPUT_TOKENS_CAP, max(1, math.ceil(max_words * factor)))

    def compile(self, prefs: PreferencesInput) -> CompiledPreferences:
        language = _parse_enum(Language, prefs.language, "language")
        question_type = _parse_enum(QuestionType, prefs.question_type, "question_type")
        answer_format = _parse_enum(AnswerFormat, prefs.answer_format, "answer_format")
        answer_length = _parse_enum(AnswerLength, prefs.answer_length, "answer_length")
        answer_tone = _parse_enum(AnswerTone, prefs.answer_tone, "answer_tone")
        extra = self._clean_extra(prefs.extra_instructions)
        provider = _parse_optional_id(prefs.provider, "provider")
        model = _parse_optional_id(prefs.model, "model")
        if model is not None and provider is None:
            raise InvalidPreference("model")  # a model id is meaningless without its provider

        min_words, max_words = self._targets.bands[answer_length]
        answer = AnswerConfig(
            language=language,
            question_type=question_type,
            answer_format=answer_format,
            answer_length=answer_length,
            answer_tone=answer_tone,
            extra_instructions=extra,
            target_min_words=min_words,
            target_max_words=max_words,
            provider=provider,
            model=model,
        )
        generation = GenerationConfig(
            temperature=_TEMPERATURE[question_type],
            max_output_tokens=self._max_output_tokens(language, max_words),
        )
        return CompiledPreferences(answer=answer, generation=generation)
