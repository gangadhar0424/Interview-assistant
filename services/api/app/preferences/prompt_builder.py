"""PromptBuilder: the only place prompts are built, from versioned template files (D-11, D-34).

Templates live in `templates/<version>/`. A released version directory is immutable: change a
template by creating a new version directory and bumping `PROMPT_VERSION`. A version can override
only changed files and inherits other files from v1; fingerprints cover the effective template set.
All prompt prose lives in those files; this module only assembles them.

Untrusted text (job description, candidate material, prior exchanges, the question and the user's
style preferences) is never placed in the system message. It goes into delimited data blocks in the
user message, and any text inside it that could forge a delimiter is neutralised first.
"""

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from uuid import UUID

from app.ai.types import Message
from app.preferences.answer_config import AnswerConfig
from app.preferences.sanitize import clean_text

PROMPT_VERSION = "v2"
TEMPLATES_ROOT = Path(__file__).resolve().parent / "templates"
_BASE_TEMPLATE_VERSION = "v1"

NONE_PROVIDED = "(none provided)"
_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")


class PromptError(ValueError):
    """Bad builder input or a broken template set (never contains user text)."""


@dataclass(frozen=True, slots=True)
class EvidencePassage:
    source_type: str
    source_id: UUID
    chunk_index: int
    text: str


@dataclass(frozen=True, slots=True)
class PromptInputs:
    """The runtime material for one question. Assembling it from retrieval is Phase 8's job."""

    question: str
    company_name: str = ""
    job_description: str = ""
    context_passages: Sequence[str | EvidencePassage] = ()
    history: Sequence[tuple[str, str]] = ()  # earlier (question, answer) pairs


@dataclass(frozen=True, slots=True)
class BuiltPrompt:
    messages: list[Message]
    prompt_version: str


@lru_cache(maxsize=512)
def _read(version: str, relative: str) -> str:
    path = TEMPLATES_ROOT / version / relative
    if not path.is_file() and version != _BASE_TEMPLATE_VERSION:
        path = TEMPLATES_ROOT / _BASE_TEMPLATE_VERSION / relative
    try:
        raw = path.read_bytes()
    except OSError:
        raise PromptError("template not found") from None
    return raw.decode("utf-8").replace("\r\n", "\n").rstrip("\n")


def _render(template: str, values: Mapping[str, str]) -> str:
    """Single-pass `{{name}}` substitution: substituted values are never re-scanned."""

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            raise PromptError("template placeholder has no value")
        return values[key]

    return _PLACEHOLDER.sub(replace, template)


def template_fingerprint(version: str) -> str:
    """SHA-256 over a version's effective templates (names + normalised content)."""
    root = TEMPLATES_ROOT / version
    if not root.is_dir():
        raise PromptError("unknown prompt version")
    base = TEMPLATES_ROOT / _BASE_TEMPLATE_VERSION
    paths = {path.relative_to(root).as_posix(): path for path in root.rglob("*") if path.is_file()}
    if version != _BASE_TEMPLATE_VERSION:
        for path in base.rglob("*"):
            if path.is_file():
                paths.setdefault(path.relative_to(base).as_posix(), path)
    digest = hashlib.sha256()
    for relative, path in sorted(paths.items()):
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
        digest.update(b"\0")
    return digest.hexdigest()


_OPEN_LOOKALIKE = "\N{SINGLE LEFT-POINTING ANGLE QUOTATION MARK}" * 3
_CLOSE_LOOKALIKE = "\N{SINGLE RIGHT-POINTING ANGLE QUOTATION MARK}" * 3


def neutralize_delimiters(text: str) -> str:
    """Make `<<<` / `>>>` unusable inside untrusted text so it cannot forge a block boundary."""
    return text.replace("<<<", _OPEN_LOOKALIKE).replace(">>>", _CLOSE_LOOKALIKE)


def wrap_untrusted(label: str, text: str) -> str:
    body = neutralize_delimiters(clean_text(text)) or NONE_PROVIDED
    return f"<<<BEGIN UNTRUSTED:{label}>>>\n{body}\n<<<END UNTRUSTED:{label}>>>"


class PromptBuilder:
    def __init__(self, version: str = PROMPT_VERSION) -> None:
        if not (TEMPLATES_ROOT / version).is_dir():
            raise PromptError("unknown prompt version")
        self.version = version

    def _t(self, relative: str) -> str:
        return _read(self.version, relative)

    def _system(self, config: AnswerConfig) -> str:
        words = {
            "min_words": str(config.target_min_words),
            "max_words": str(config.target_max_words),
        }
        return _render(
            self._t("system.md"),
            {
                "question_type_guidance": self._t(f"question_type/{config.question_type.value}.md"),
                "format_guidance": self._t(f"format/{config.answer_format.value}.md"),
                "length_guidance": _render(
                    self._t(f"length/{config.answer_length.value}.md"), words
                ),
                "tone_guidance": self._t(f"tone/{config.answer_tone.value}.md"),
                "language_guidance": self._t(f"language/{config.language.value}.md"),
            },
        )

    def build(self, config: AnswerConfig, inputs: PromptInputs) -> BuiltPrompt:
        if not clean_text(inputs.question):
            raise PromptError("question must not be empty")
        material_entries: list[str] = []
        for index, passage in enumerate(inputs.context_passages, 1):
            if isinstance(passage, EvidencePassage):
                if passage.source_type not in ("resume", "document") or passage.chunk_index < 0:
                    raise PromptError("invalid evidence metadata")
                if not isinstance(passage.source_id, UUID):
                    raise PromptError("invalid evidence metadata")
                text = clean_text(passage.text)
                if text and self.version == "v2":
                    text = re.sub(r"\[E\d+\]", "[quoted evidence tag]", text, flags=re.IGNORECASE)
                    material_entries.append(
                        f"[E{index} | {passage.source_type} | source {passage.source_id} "
                        f"| chunk {passage.chunk_index}] {text}"
                    )
                elif text:
                    material_entries.append(text)
            else:
                text = clean_text(passage)
                if text:
                    material_entries.append(f"[E{index}] {text}" if self.version == "v2" else text)
        material = "\n\n".join(material_entries)
        exchanges = "\n\n".join(
            f"Q: {clean_text(q)}\nA: {clean_text(a)}" for q, a in inputs.history
        )
        user = _render(
            self._t("user.md"),
            {
                "company_block": wrap_untrusted("company_name", inputs.company_name),
                "job_block": wrap_untrusted("job_description", inputs.job_description),
                "material_block": wrap_untrusted("candidate_material", material),
                "history_block": wrap_untrusted("previous_exchanges", exchanges),
                "style_block": wrap_untrusted("style_preferences", config.extra_instructions),
                "question_block": wrap_untrusted("question", inputs.question),
            },
        )
        return BuiltPrompt(
            messages=[Message("system", self._system(config)), Message("user", user)],
            prompt_version=self.version,
        )
