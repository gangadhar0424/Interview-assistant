"""Citation-integrity check for a finished answer (Issue 2; prompt v2 evidence tags).

WHAT THIS IS: a structural check that every evidence tag in the answer text (`[E3]`, or the long
form `[E3 | resume | source <uuid> | chunk 0]`) is well formed and refers to an entry that was
actually rendered into THIS request's prompt. WHAT IT IS NOT: proof that the answer is grounded. A
valid citation does not show that the cited excerpt supports the sentence, and an answer with no
citation, or with a fabricated claim that carries no tag, passes unnoticed. Uncited-claim detection
and semantic claim verification are deliberately out of scope.

Pure and deterministic: no I/O, no clock, no logging, and the result holds only tag numbers and
counts, never answer text or excerpts. The caller decides what to do with the result (the Q&A
service records it and never lets it change the answer).

Grammar (strict, ASCII): `[E<n>]` with `n` in 1..999999 and no leading zero, or the long form
above with lowercase `resume|document`, a UUID, and a non-negative chunk index without leading
zeros. Anything else that starts like a tag (`[E]`, `[E0]`, `[E01]`, `[e1]`, `[E1, E2]`, full-width
or corner-bracket look-alikes, an unclosed tag) is malformed. Adjacent tags such as `[E1][E2]` are
two separate well-formed tags. Bare mentions without brackets (`E1`, `(E1)`) are not tags.
"""

import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from app.preferences.prompt_builder import EVIDENCE_TAG_HEAD, EvidenceRef
from app.preferences.sanitize import clean_text

GROUNDING_CHECK_VERSION = 1

STATUS_OK = "ok"  # at least one tag, all well formed and resolving to rendered evidence
STATUS_INVALID = "invalid_citations"  # an unknown/forged tag or a malformed tag is present
STATUS_NO_CITATIONS = "no_citations"  # informational only: rule 3 allows omitting the line
STATUS_CHECK_ERROR = "check_error"  # the check itself failed; the answer is unaffected
STATUSES = (STATUS_OK, STATUS_INVALID, STATUS_NO_CITATIONS, STATUS_CHECK_ERROR)

MAX_RECORDED_IDS = 50  # bounds the stored/logged lists whatever the model writes
_MAX_TAG_CHARS = 200  # a tag longer than this on one line is treated as unclosed
_MAX_NUMBER_DIGITS = 6

_CLOSERS = "]］】〕⟧"
_NUMBER = rf"[1-9][0-9]{{0,{_MAX_NUMBER_DIGITS - 1}}}"
_BARE = re.compile(rf"\[E({_NUMBER})\]", re.ASCII)
_LONG = re.compile(
    rf"\[E({_NUMBER}) \| (resume|document) \| source ([0-9a-fA-F]{{8}}-[0-9a-fA-F]{{4}}-"
    rf"[0-9a-fA-F]{{4}}-[0-9a-fA-F]{{4}}-[0-9a-fA-F]{{12}}) \| chunk (0|[1-9][0-9]{{0,8}})\]",
    re.ASCII,
)
# Numbers mentioned inside a malformed tag (still checked against the rendered set). Longer digit
# runs are ignored, never converted, so a huge number cannot be turned into an int.
_DIGITS = "[0-9０-９]"
_MENTIONED_NUMBER = re.compile(rf"[EeＥｅ]\s*({_DIGITS}{{1,{_MAX_NUMBER_DIGITS}}})(?!{_DIGITS})")
# `[E]` has no number, so it is not an evidence-tag head; it is counted as malformed. Only the
# exact empty form is matched, so ordinary bracketed words such as `[Edit]` are left alone.
_EMPTY_TAG = re.compile(r"[\[［【〔⟦]\s*[EeＥｅ]\s*[\]］】〕⟧]")


@dataclass(frozen=True, slots=True)
class GroundingCheck:
    """Content-free outcome of the citation-integrity check (the stored `grounding_check` JSON)."""

    status: str
    shown_count: int
    valid_ids: tuple[int, ...] = ()
    unknown_ids: tuple[int, ...] = ()
    malformed_count: int = 0

    def to_json(self) -> dict[str, object]:
        return {
            "v": GROUNDING_CHECK_VERSION,
            "status": self.status,
            "shown_count": self.shown_count,
            "valid_ids": list(self.valid_ids),
            "unknown_ids": list(self.unknown_ids),
            "malformed_count": self.malformed_count,
        }

    @classmethod
    def errored(cls, shown_count: int) -> "GroundingCheck":
        return cls(STATUS_CHECK_ERROR, shown_count)


def _tag_extent(text: str, head_end: int, next_head_start: int) -> int | None:
    """Index just past the closing bracket of the tag whose head ends at `head_end`, or None.

    The search never crosses a line break, never reaches the next tag head (so an unclosed tag
    cannot swallow a later well-formed one) and never goes past `_MAX_TAG_CHARS`.
    """
    line_end = text.find("\n", head_end)
    limit = min(
        len(text) if line_end == -1 else line_end,
        next_head_start,
        head_end + _MAX_TAG_CHARS,
    )
    for position in range(head_end, limit):
        if text[position] in _CLOSERS:
            return position + 1
    return None


def _mentioned_numbers(fragment: str) -> list[int]:
    return [int(match.group(1)) for match in _MENTIONED_NUMBER.finditer(fragment)]


def check_citations(text: str, evidence: Sequence[EvidenceRef]) -> GroundingCheck:
    """Check every evidence tag in `text` against the entries rendered into this request's prompt.

    `evidence` is `BuiltPrompt.evidence`. Tag numbers are never derived here.
    """
    rendered: Mapping[int, EvidenceRef] = {ref.tag: ref for ref in evidence}
    cleaned = clean_text(text)  # same cleaning as the prompt side: invisible characters removed
    heads = list(EVIDENCE_TAG_HEAD.finditer(cleaned))

    valid: set[int] = set()
    unknown: set[int] = set()
    malformed = len(_EMPTY_TAG.findall(cleaned))
    for position, head in enumerate(heads):
        next_head = heads[position + 1].start() if position + 1 < len(heads) else len(cleaned)
        end = _tag_extent(cleaned, head.end(), next_head)
        if end is None:  # unclosed: only the head can be read
            malformed += 1
            unknown.update(n for n in _mentioned_numbers(head.group(0)) if n not in rendered)
            continue
        tag = cleaned[head.start() : end]
        bare = _BARE.fullmatch(tag)
        if bare is not None:
            number = int(bare.group(1))
            (valid if number in rendered else unknown).add(number)
            continue
        long_form = _LONG.fullmatch(tag)
        if long_form is not None:
            number = int(long_form.group(1))
            ref = rendered.get(number)
            if (
                ref is not None
                and ref.source_id is not None
                and ref.source_type == long_form.group(2)
                and ref.source_id == uuid.UUID(long_form.group(3))
                and ref.chunk_index == int(long_form.group(4))
            ):
                valid.add(number)
            else:  # a number that was not rendered, or rendered with different metadata
                unknown.add(number)
            continue
        malformed += 1  # starts like a tag but is not in the strict grammar
        unknown.update(n for n in _mentioned_numbers(tag) if n not in rendered)

    if unknown or malformed:
        status = STATUS_INVALID
    elif valid:
        status = STATUS_OK
    else:
        status = STATUS_NO_CITATIONS
    return GroundingCheck(
        status=status,
        shown_count=len(rendered),
        valid_ids=tuple(sorted(valid))[:MAX_RECORDED_IDS],
        unknown_ids=tuple(sorted(unknown))[:MAX_RECORDED_IDS],
        malformed_count=malformed,
    )
