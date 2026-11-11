"""Simple, deterministic English question detection for transcribed utterances (Phase 12, D-53).

Pure and dependency free: the same input always gives the same answer. It is ADVISORY: the desktop
only shows a "possible question" flag and the user decides whether to ask it (auto-answer is an
opt-in, off by default). It is not a classifier and makes no claim about other languages.

Rules, in order: very short text is never a question; acknowledgements and fillers ("okay", "thank
you", "sounds good") are not; leading discourse markers ("so", "okay so", "and", "now") are skipped;
then a question word or request opener at the start, and a trailing question mark, add confidence.
"""

import re
from dataclasses import dataclass

from app.preferences.sanitize import clean_text

QUESTION_THRESHOLD = 0.6

_WORDS = re.compile(r"[a-z0-9']+")

# Words that may start an utterance without changing its meaning.
_LEADING_MARKERS = frozenset(
    {"so", "okay", "ok", "alright", "right", "and", "well", "now", "next", "then", "great",
     "good", "perfect", "thanks", "thank", "you", "um", "uh", "hmm", "yeah", "yes", "sure",
     "also", "but", "last", "final", "finally", "one", "more", "thing", "quick", "question"}
)  # fmt: skip

_WH = frozenset({"what", "why", "how", "when", "where", "who", "whom", "whose", "which"})
_AUX = frozenset(
    {"can", "could", "would", "will", "should", "do", "does", "did", "is", "are", "was", "were",
     "have", "has", "had", "am"}
)  # fmt: skip
_REQUEST_OPENERS = (
    ("tell", "me"),
    ("tell", "us"),
    ("describe",),
    ("explain",),
    ("walk", "me", "through"),
    ("walk", "us", "through"),
    ("talk", "me", "through"),
    ("talk", "about"),
    ("give", "me"),
    ("share",),
    ("elaborate",),
    ("i", "would", "like", "you", "to"),
    ("i'd", "like", "you", "to"),
    ("i", "want", "you", "to"),
)

_ACKNOWLEDGEMENTS = frozenset(
    {"okay", "ok", "right", "thanks", "thank", "you", "sure", "yes", "no", "yeah", "yep", "nope",
     "got", "it", "sounds", "good", "great", "nice", "alright", "i", "see", "mm", "mhm", "hmm",
     "uh", "um", "perfect", "cool", "wonderful", "awesome", "fine", "welcome", "bye", "goodbye",
     "hello", "hi", "hey", "morning", "afternoon", "evening", "please", "excuse", "me", "sorry",
     "pardon", "understood", "makes", "sense", "that", "s", "thats", "is", "all", "for", "now",
     "later"}
)  # fmt: skip


@dataclass(frozen=True, slots=True)
class Detection:
    is_question: bool
    confidence: float
    reason: str  # a fixed code, never text from the utterance


def _starts_with(words: list[str], opener: tuple[str, ...]) -> bool:
    return tuple(words[: len(opener)]) == opener


def classify(text: str) -> Detection:
    """Decide whether one utterance looks like a question put to the candidate."""
    cleaned = clean_text(text).strip()
    has_mark = cleaned.endswith("?")
    words = _WORDS.findall(cleaned.lower())
    if len(words) < 3:
        return Detection(False, 0.0, "too_short")
    if all(word in _ACKNOWLEDGEMENTS for word in words) and not has_mark:
        return Detection(False, 0.0, "acknowledgement")
    core = list(words)
    while len(core) > 3 and core[0] in _LEADING_MARKERS:
        core.pop(0)
    wh = core[0] in _WH
    aux = core[0] in _AUX and len(core) >= 4
    request = any(_starts_with(core, opener) for opener in _REQUEST_OPENERS)
    opener = wh or aux or request
    if opener and has_mark:
        return Detection(True, 0.95, "opener_and_mark")
    if wh and len(core) >= 4:
        return Detection(True, 0.8, "question_word")
    if request:
        return Detection(True, 0.8, "request")
    if aux and len(core) >= 5:
        return Detection(True, 0.7, "auxiliary_opener")
    if has_mark and len(core) >= 4:
        return Detection(True, 0.6, "question_mark")
    return Detection(False, 0.2 if opener else 0.0, "statement")
