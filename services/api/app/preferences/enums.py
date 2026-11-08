"""Allowed preference values: the single source of truth (API options, compiler, templates, DB).

The string values are stable identifiers stored in the database (docs/DATABASE.md
`session_preferences`) and sent over the API; never rename them without a migration.
"""

from enum import StrEnum


class Language(StrEnum):
    """Answer language (BCP-47). The web UI itself is English only for now (owner decision)."""

    EN = "en"
    HI = "hi"
    TE = "te"


class QuestionType(StrEnum):
    AUTO = "auto"
    BEHAVIORAL = "behavioral"
    CODING = "coding"
    EXPERIENCE_CHECK = "experience_check"
    HOW_DO_YOU = "how_do_you"
    SITUATIONAL = "situational"
    SYSTEM_DESIGN = "system_design"
    TECHNICAL_KNOWLEDGE = "technical_knowledge"
    TELL_ME_ABOUT_YOURSELF = "tell_me_about_yourself"


class AnswerFormat(StrEnum):
    FULL_SCRIPT = "full_script"
    SCRIPT_BULLETS = "script_bullets"
    BULLETS = "bullets"


class AnswerLength(StrEnum):
    SHORT = "short"
    BALANCED = "balanced"
    LONG = "long"


class AnswerTone(StrEnum):
    SIMPLE = "simple"
    FORMAL = "formal"
