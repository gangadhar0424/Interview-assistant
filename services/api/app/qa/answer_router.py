"""Answer router (D-66): decide, without any AI call, where an answer comes from.

    resume_extractive    the question matches the resume   -> the resume's own text, verbatim
    document_extractive  else it matches a selected document -> that document's own text, verbatim
    synthesis            related passages exist: an LLM answers from them, keeping their wording
    general              neither source covers it            -> general-knowledge answer (validated)
    uncertain            evidence is partial: ask the caller to rewrite the question and retry

The decision is a pure function of the search results, so it is cheap (microseconds), repeatable
and unit-testable. Confidence never uses an absolute similarity score (the dense scores of an
embedding model are low and close together, and reciprocal-rank-fusion scores are compressed). It
uses the question's content words found in a passage's heading or body. A passage whose OWN heading
names the topic is the strongest evidence and is answered verbatim; so is a body that contains
nearly all of a multi-word question. A heading that only an enclosing section matches is not
enough for a verbatim answer (the leaf is just part of the topic), so those go to synthesis.
Resume first: on equal strength the resume wins; a strong heading match in a document still beats
a weak body mention in the resume ("what is POSC" must open the POSC section, not the resume's
skills line).
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from app.core.config import Settings
from app.retrieval.service import ChunkHit
from app.retrieval.terms import content_terms, coverage

STRENGTH_TITLE = 3  # the passage's own (deepest) heading names the topic
STRENGTH_BODY = 2  # the body holds most of a multi-word question (two or more words needed)
STRENGTH_PATH = 1  # only an enclosing heading names the topic
PARTIAL_COVERAGE = 0.5  # some evidence, not enough to answer from it verbatim
PARTIAL_WINDOW = 5  # how many top hits of each corpus are checked for partial evidence
INTRO_SPAN_CHARS = 4000  # how much of the resume's opening an introduction question may use


class Mode(StrEnum):
    RESUME_EXTRACTIVE = "resume_extractive"
    DOCUMENT_EXTRACTIVE = "document_extractive"
    SYNTHESIS = "synthesis"
    GENERAL = "general"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class RouterSettings:
    heading_coverage: float = 0.6
    body_coverage: float = 0.75
    margin: float = 1.0  # 1.0 = a tie between equally good sections is resolved, never refused

    @classmethod
    def from_settings(cls, settings: Settings) -> "RouterSettings":
        return cls(
            heading_coverage=settings.qa_router_heading_coverage,
            body_coverage=settings.qa_router_body_coverage,
            margin=settings.qa_router_margin,
        )


@dataclass(frozen=True, slots=True)
class Decision:
    mode: Mode
    hit: ChunkHit | None = None  # the passage an extractive answer is taken from
    anchored_start: bool = False  # begin the answer at the start of the section
    reason: str = ""  # fixed vocabulary (logged): intro, title, body, path, partial, none
    span: tuple[int, int] | None = None  # explicit range in the source text (resume opening)


_INTRO = re.compile(
    r"\b(?:tell|talk)\s+(?:me\s+)?(?:a\s+(?:bit|little)\s+|more\s+)?about\s+your\s?self\b"
    r"|\bintroduce\s+your\s?self\b"
    r"|\bself[\s-]?introduction\b"
    r"|\babout\s+your\s?self\b"
    r"|\bwalk\s+me\s+through\s+(?:your\s+)?(?:resume|cv|background)\b"
    r"|\bgive\s+(?:me\s+)?(?:a\s+)?(?:brief\s+)?(?:introduction|intro)\b",
    re.IGNORECASE,
)


_BROAD = re.compile(
    r"^\W*(?:please\s+)?(?:can\s+you\s+|could\s+you\s+)?"
    r"(?:tell|talk)\s+(?:me\s+)?(?:a\s+(?:bit|little)\s+|more\s+)?about\b"
    r"|^\W*(?:please\s+)?(?:walk|take)\s+me\s+through\b"
    r"|^\W*(?:what(?:'s|\s+is)\s+)?your\s+experience\s+(?:with|in|on)\b"
    r"|^\W*(?:do\s+you\s+have\s+)?experience\s+(?:with|in|on)\b"
    r"|^\W*(?:an\s+)?overview\s+of\b",
    re.IGNORECASE,
)
BROAD_MAX_TERMS = 2  # an open-ended question names a topic, not a specific thing


def is_broad_question(question: str) -> bool:
    """An open-ended "tell me about <topic>" question (asks for the whole topic)."""
    return _BROAD.search(question) is not None


def is_intro_question(question: str) -> bool:
    """ "Tell me about yourself" and its common phrasings: answered from the resume's opening."""
    return _INTRO.search(question) is not None


def _title(hit: ChunkHit) -> str:
    return (hit.heading or "").rsplit(" > ", 1)[-1]


def title_coverage(terms: Sequence[str], hit: ChunkHit) -> float:
    return coverage(list(terms), _title(hit)) if hit.heading else 0.0


def strength(terms: Sequence[str], hit: ChunkHit, cfg: RouterSettings) -> int:
    """How strongly `hit` is anchored to the question's content words (0 = not at all)."""
    words = list(terms)
    if not words:
        return 0
    if hit.heading:
        if coverage(words, _title(hit)) >= cfg.heading_coverage:
            return STRENGTH_TITLE
        if coverage(words, hit.heading) >= cfg.heading_coverage:
            return STRENGTH_PATH
    if len(words) >= 2 and coverage(words, hit.text) >= cfg.body_coverage:
        return STRENGTH_BODY
    return 0


def _section_key(hit: ChunkHit) -> tuple[object, ...]:
    if hit.section_start is not None:
        return (hit.source_id, hit.section_start)
    return (hit.source_id, "chunk", hit.chunk_index)


def _best_anchored(
    terms: Sequence[str], hits: Sequence[ChunkHit], cfg: RouterSettings
) -> tuple[int, ChunkHit] | None:
    """The best anchored passage of one corpus: strongest anchor, then the heading that covers
    most of the question, then the fused search rank. With `margin` above 1.0, two different
    sections that tie on all three (within the margin) are not decisive and give None."""
    scored = [(strength(terms, h, cfg), title_coverage(terms, h), h) for h in hits]
    scored = [item for item in scored if item[0] > 0]
    if not scored:
        return None
    scored.sort(key=lambda item: (-item[0], -item[1], -item[2].fused_score, item[2].chunk_index))
    top_strength, top_cover, top = scored[0]
    if cfg.margin > 1.0:
        for other_strength, other_cover, other in scored[1:]:
            if _section_key(other) == _section_key(top):
                continue
            if (
                other_strength == top_strength
                and abs(other_cover - top_cover) < 1e-9
                and top.fused_score < other.fused_score * cfg.margin
            ):
                return None
            break
    return top_strength, top


def _partial_evidence(
    terms: Sequence[str], resume_hits: Sequence[ChunkHit], document_hits: Sequence[ChunkHit]
) -> bool:
    words = list(terms)
    window = [*resume_hits[:PARTIAL_WINDOW], *document_hits[:PARTIAL_WINDOW]]
    return any(coverage(words, f"{h.heading or ''} {h.text}") >= PARTIAL_COVERAGE for h in window)


def decide(
    question: str,
    resume_hits: Sequence[ChunkHit],
    document_hits: Sequence[ChunkHit],
    cfg: RouterSettings,
    *,
    resume_opening: ChunkHit | None = None,
    terms: Sequence[str] | None = None,
) -> Decision:
    """Pick the answer source. `terms` overrides the question's own content words (the rewritten
    question on the second pass)."""
    words = list(terms) if terms is not None else content_terms(question)

    if resume_opening is not None and is_intro_question(question):
        return Decision(
            Mode.RESUME_EXTRACTIVE,
            resume_opening,
            anchored_start=True,
            reason="intro",
            span=(0, INTRO_SPAN_CHARS),
        )

    if is_broad_question(question) and 0 < len(words) <= BROAD_MAX_TERMS:
        titled = {
            _section_key(h)
            for h in (*resume_hits, *document_hits)
            if strength(words, h, cfg) == STRENGTH_TITLE
        }
        if len(titled) >= 2:
            # Several different sections are about this topic ("tell me about enhancements"):
            # one of them verbatim would be an arbitrary pick, so the model answers from all of
            # them (keeping their wording, and the role guard keeps the candidate's real role).
            return Decision(Mode.SYNTHESIS, reason="broad")

    resume = _best_anchored(words, resume_hits, cfg)
    document = _best_anchored(words, document_hits, cfg)
    names = {STRENGTH_TITLE: "title", STRENGTH_PATH: "path", STRENGTH_BODY: "body"}
    choice: tuple[int, ChunkHit, Mode] | None = None
    if resume and (document is None or resume[0] >= document[0]):
        choice = (resume[0], resume[1], Mode.RESUME_EXTRACTIVE)
    elif document:
        choice = (document[0], document[1], Mode.DOCUMENT_EXTRACTIVE)
    if choice is not None:
        strength_, hit, mode = choice
        if strength_ == STRENGTH_PATH:
            # Only an enclosing section matches: the leaf is part of a bigger topic, so let the
            # model answer from the related passages (keeping their wording).
            return Decision(Mode.SYNTHESIS, hit, reason="path")
        return Decision(
            mode, hit, anchored_start=strength_ == STRENGTH_TITLE, reason=names[strength_]
        )

    if _partial_evidence(words, resume_hits, document_hits):
        return Decision(Mode.UNCERTAIN, reason="partial")
    return Decision(Mode.GENERAL, reason="none")
