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
class EvidenceRef:
    """One evidence entry exactly as rendered into the prompt (prompt v2): the tag number the model
    may cite, plus the metadata printed in a long-form tag. `source_id` and `chunk_index` are None
    for a plain-text passage, which is tagged `[E<n>]` without metadata."""

    tag: int
    source_type: str | None
    source_id: UUID | None
    chunk_index: int | None


@dataclass(frozen=True, slots=True)
class BuiltPrompt:
    messages: list[Message]
    prompt_version: str
    # The tags actually rendered, in prompt order. Empty for v1 (no tags). This is the only place
    # tag numbers are decided, so a post-generation check never re-derives the numbering.
    evidence: tuple[EvidenceRef, ...] = ()


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


# An evidence tag is a trust signal: only this builder creates one. Any look-alike inside text that
# a user, a job posting or a document supplied is rewritten so it cannot be read, or cited, as
# candidate evidence. Only the head of the tag is matched (`[E1`, any case, spaces or a line break
# after the bracket, full-width and corner-bracket look-alikes), so `[E1]`, `[E1 | resume | ...]`
# and an unclosed tag are all covered, and `[E1]` still becomes exactly `[quoted evidence tag]`.
_EVIDENCE_TAG_HEAD = re.compile(
    r"[\[\uFF3B\u3010\u3014\u27E6]\s*[Ee\uFF25\uFF45]\s*[0-9\uFF10-\uFF19]+"
)
_QUOTED_TAG_HEAD = "[quoted evidence tag"
EVIDENCE_TAG_HEAD = _EVIDENCE_TAG_HEAD  # shared with the post-generation citation check


def neutralize_evidence_tags(text: str) -> str:
    """Rewrite anything that looks like the head of an evidence tag (pure, idempotent). Call it
    after `clean_text`, so invisible characters cannot be used to hide a tag from the match."""
    return _EVIDENCE_TAG_HEAD.sub(_QUOTED_TAG_HEAD, text)


def wrap_untrusted(label: str, text: str, *, neutralize_tags: bool = False) -> str:
    """Delimit untrusted text as data. `neutralize_tags` is set for every channel except the
    candidate material, whose genuine evidence tags are created by `PromptBuilder.build` itself."""
    body = clean_text(text)
    if neutralize_tags:
        body = neutralize_evidence_tags(body)
    body = neutralize_delimiters(body) or NONE_PROVIDED
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
        evidence: list[EvidenceRef] = []
        for index, passage in enumerate(inputs.context_passages, 1):
            if isinstance(passage, EvidencePassage):
                if passage.source_type not in ("resume", "document") or passage.chunk_index < 0:
                    raise PromptError("invalid evidence metadata")
                if not isinstance(passage.source_id, UUID):
                    raise PromptError("invalid evidence metadata")
                text = clean_text(passage.text)
                if text and self.version == "v2":
                    text = neutralize_evidence_tags(text)
                    material_entries.append(
                        f"[E{index} | {passage.source_type} | source {passage.source_id} "
                        f"| chunk {passage.chunk_index}] {text}"
                    )
                    evidence.append(
                        EvidenceRef(
                            index, passage.source_type, passage.source_id, passage.chunk_index
                        )
                    )
                elif text:
                    material_entries.append(text)
            else:
                text = clean_text(passage)
                if text and self.version == "v2":
                    text = neutralize_evidence_tags(text)
                if text:
                    material_entries.append(f"[E{index}] {text}" if self.version == "v2" else text)
                    if self.version == "v2":
                        evidence.append(EvidenceRef(index, None, None, None))
        material = "\n\n".join(material_entries)
        exchanges = "\n\n".join(
            f"Q: {clean_text(q)}\nA: {clean_text(a)}" for q, a in inputs.history
        )
        # v1 has no evidence tags: its output stays byte-for-byte unchanged
        tags = self.version == "v2"
        user = _render(
            self._t("user.md"),
            {
                "company_block": wrap_untrusted(
                    "company_name", inputs.company_name, neutralize_tags=tags
                ),
                "job_block": wrap_untrusted(
                    "job_description", inputs.job_description, neutralize_tags=tags
                ),
                "material_block": wrap_untrusted("candidate_material", material),
                "history_block": wrap_untrusted(
                    "previous_exchanges", exchanges, neutralize_tags=tags
                ),
                "style_block": wrap_untrusted(
                    "style_preferences", config.extra_instructions, neutralize_tags=tags
                ),
                "question_block": wrap_untrusted("question", inputs.question, neutralize_tags=tags),
            },
        )
        return BuiltPrompt(
            messages=[Message("system", self._system(config)), Message("user", user)],
            prompt_version=self.version,
            evidence=tuple(evidence),
        )
