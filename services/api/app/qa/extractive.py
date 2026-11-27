"""Exact-text answers (D-66): return the user's own wording, not a model's rewrite of it.

An extractive answer is a contiguous run of whole sentences/lines of a stored source text.
Nothing is generated, translated or paraphrased, so no provider is called, nothing can be
invented, and the answer is fast. The only transformation is `tidy`, which turns the bullet
glyphs PDF extraction leaves behind (private-use and replacement characters) into a plain
bullet and collapses blank-line runs. The result is verified to be a substring of the tidied
source before it is used.

Length: if the section is longer than the session's length setting allows, a window of whole
sentences is chosen (from the start when the heading itself matched the question, otherwise the
window that contains most of the question's words). It is never shortened by rewriting.
"""

import re
import uuid
from collections.abc import Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.session import set_tenant_context
from app.ingestion.chunking import _sentence_spans
from app.qa.answer_router import Decision
from app.retrieval.terms import token_set

# Bullet glyphs that PDF extraction emits as private-use or replacement characters.
_BULLET_GLYPHS = re.compile("[-�•●▪□‣]")
_BULLET_THEN_BREAK = re.compile(r"•[ \t]*\n+[ \t]*")
_BLANK_RUNS = re.compile(r"\n{3,}")
_SPACE_RUNS = re.compile(r"[ \t]{2,}")

LENGTH_SLACK = 1.2  # a section up to this much over the word budget is returned whole
MIN_WINDOW_SENTENCES = 1


def tidy(text: str) -> str:
    """Make extracted text readable without changing any word of it."""
    out = _BULLET_GLYPHS.sub("•", text)
    out = _BULLET_THEN_BREAK.sub("• ", out)
    out = _SPACE_RUNS.sub(" ", out)
    out = _BLANK_RUNS.sub("\n\n", out)
    return out.strip()


# Notes the author of a prep document wrote TO the candidate ("Confirm before using."). They are
# instructions, not words to say, so they never reach the spoken answer.
_COACH_LINE = re.compile(
    r"^\s*(?:confirm before using|keep the bracketed lines|do not invent|do not claim"
    r"|if your .{0,80}(?:say so|say that|limited|only done|ewm-only)"
    r"|check the actual\b|confirm exact\b|only go into more detail"
    r"|know your answer|this is the question the interviewer)[^\n]*$",
    re.IGNORECASE | re.MULTILINE,
)
_CONFIRM_TAG = re.compile(r"\s*\[confirm[^\]]*\]", re.IGNORECASE)
_SAY_HOW = re.compile(r"\s*\bSay (?:how|whether)\b[^\n]*", re.IGNORECASE)


def strip_coaching(text: str) -> str:
    """Remove authoring notes (whole instruction lines, `[confirm]` tags, "Say how: ...")."""
    out = _COACH_LINE.sub("", text)
    out = _CONFIRM_TAG.sub("", out)
    out = _SAY_HOW.sub("", out)
    return tidy(out)


def word_count(text: str) -> int:
    return len(text.split())


def fit_to_words(
    text: str, max_words: int, terms: Sequence[str] = (), *, anchored_start: bool = False
) -> str:
    """`text` if it fits the word budget, else a window of whole sentences/lines of it.

    The window starts at the beginning when `anchored_start` (the heading matched the question, or
    an introduction was asked); otherwise it is the window with the most of the question's words.
    """
    text = text.strip()
    if max_words <= 0 or word_count(text) <= max_words * LENGTH_SLACK:
        return text
    spans = _sentence_spans(text)
    if not spans:
        return " ".join(text.split()[:max_words])
    counts = [word_count(text[s:e]) for s, e in spans]
    wanted = set(terms)

    def window_end(start: int) -> int:
        total, end = 0, start
        while end < len(spans) and (total + counts[end] <= max_words or end == start):
            total += counts[end]
            end += 1
        return end  # exclusive

    if anchored_start or not wanted:
        first = 0
    else:
        sentence_hits = [len(wanted & token_set(text[s:e])) for s, e in spans]
        best, first = -1, 0
        for start in range(len(spans)):
            score = sum(sentence_hits[start : window_end(start)])
            if score > best:
                best, first = score, start
    last = window_end(first)
    return text[spans[first][0] : spans[last - 1][1]].strip()


def is_verbatim(answer: str, source: str) -> bool:
    """True when `answer` is an exact contiguous piece of the (tidied) `source`."""
    return bool(answer) and answer in tidy(source)


def split_for_streaming(text: str) -> list[str]:
    """Paragraph-sized pieces whose concatenation is exactly `text` (the UI fills in smoothly)."""
    pieces = [piece for piece in re.split(r"(?<=\n\n)", text) if piece]
    return pieces or [text]


_SLICE_SQL = text(
    "SELECT substring(text FROM CAST(:start AS integer) FOR CAST(:length AS integer)) AS piece "
    "FROM extracted_texts "
    "WHERE user_id = :uid AND source_type = :st AND source_id = :sid"
)


async def load_slice(
    session: AsyncSession,
    user_id: uuid.UUID,
    source_type: str,
    source_id: uuid.UUID,
    start: int,
    end: int,
) -> str:
    """The characters [start, end) of a stored source text (the user's own file only)."""
    row = (
        await session.execute(
            _SLICE_SQL,
            {
                "uid": user_id,
                "st": source_type,
                "sid": source_id,
                "start": start + 1,  # SQL substring is 1-based
                "length": max(0, end - start),
            },
        )
    ).first()
    return row.piece if row is not None and row.piece else ""


async def section_source(
    sessionmaker: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
    decision: Decision,
) -> str | None:
    """The tidied, complete text of the section an extractive decision points at (the user's own
    file only), or None when there is none."""
    hit = decision.hit
    if hit is None:
        return None
    span = decision.span
    if span is None and hit.section_start is not None and hit.section_end is not None:
        span = (hit.section_start, hit.section_end)
    if span is not None:
        async with sessionmaker() as db:
            await set_tenant_context(db, user_id)
            source = await load_slice(db, user_id, hit.source_type, hit.source_id, *span)
    else:
        source = hit.text  # a fixed-size chunk is already an exact slice of the source
    source = strip_coaching(tidy(source))
    return source or None


def cut_at_sentence(text: str, max_chars: int) -> str:
    """`text` shortened to at most `max_chars`, ending at a sentence or line boundary."""
    if len(text) <= max_chars:
        return text
    kept = text[:max_chars]
    boundary = max(kept.rfind(". "), kept.rfind(chr(10)))
    return (kept[: boundary + 1] if boundary > max_chars // 2 else kept).rstrip()


async def extractive_text(
    sessionmaker: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
    decision: Decision,
    terms: Sequence[str],
    max_words: int,
) -> str | None:
    """The exact text for an extractive decision, fitted to `max_words`; None when it cannot be
    produced and verified (the caller then falls back to the synthesis path)."""
    source = await section_source(sessionmaker, user_id, decision)
    if source is None:
        return None
    answer = fit_to_words(source, max_words, terms, anchored_start=decision.anchored_start)
    return answer if is_verbatim(answer, source) else None
