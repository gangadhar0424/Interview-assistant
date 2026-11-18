"""Question terms for lexical search and for the router's confidence check (D-66).

Plain, dependency-free and script-neutral: words are runs of Unicode letters/digits. Stop words are
English function words and interview filler only; a word in another script is always a content
word. A light plural strip lets "enhancements" meet "enhancement" without a language-specific
stemmer, and the lexical query uses prefix matching on the stripped form.

A word followed by a number ("enhancement 1", "Project #2", "step 3") is ONE term ("enhancement1"):
the number is what tells the sections apart, so it must never be dropped, and it must match exactly
("enhancement1" is not "enhancement10"). Text is read the same way, so "Enhancement 1" and
"Enhancement1:" are the same token.
"""

import re

_WORD = re.compile(r"\w+", re.UNICODE)
_FUSED = re.compile(r"^([^\W\d_]+)(\d+)$", re.UNICODE)  # letters then digits: "enhancement1"
MAX_TERMS = 24
MIN_TERM_CHARS = 2
PREFIX_MIN_CHARS = 5  # shorter terms must match a whole word ("cap" must not match "capacity")

STOPWORDS = frozenset(
    """a about after all also an and any are as at be been being but by can could did do does
    done for from give had has have he her his how i if in into is it its just me more my no not
    of on one or our out please provide really she should so some such tell than that the their
    them then there these they this those to up us very was we were what when where which who whom
    why will with would you your yours yourself myself explain describe example examples
    work worked working done use used using difference differences differ between versus vs
    compare comparison meaning mean means concept definition define advantage advantages
    disadvantage disadvantages benefit benefits
    """.split()
)


def stem(word: str) -> str:
    """Drop a plural ending (enhancements -> enhancement, queries -> query)."""
    w = word.lower()
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 4 and w.endswith("es") and w[-3] in "sxz":
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def is_numbered(term: str) -> bool:
    """True for a fused word+number term such as "enhancement1"."""
    return _FUSED.match(term) is not None


def content_terms(text: str) -> list[str]:
    """Distinct content words of `text` (lowercased, stop words removed), in order. A word followed
    by a number becomes one numbered term; a bare number on its own is dropped."""
    seen: dict[str, None] = {}
    previous: str | None = None  # the last kept alphabetic term, eligible to take a number
    for match in _WORD.finditer(text):
        word = match.group(0).lower()
        if word.isdigit():
            if previous is not None:
                seen.pop(previous, None)  # "enhancement" + "1" -> "enhancement1" only
                fused = previous + word
                seen.setdefault(fused, None)
            previous = None
            continue
        if len(word) < MIN_TERM_CHARS or word in STOPWORDS:
            previous = None
            continue
        term = stem(word) if not is_numbered(word) else word
        seen.setdefault(term, None)
        previous = term if not is_numbered(term) else None
        if len(seen) >= MAX_TERMS:
            break
    return list(seen)


def tsquery_or(terms: list[str]) -> str:
    """A `to_tsquery('simple', ...)` string: an OR of the terms. Plain terms match by prefix; a
    numbered term matches exactly, either as one token ("Enhancement1") or as a word followed by the
    number ("Enhancement 1"). Terms are word characters only, so nothing can inject an operator."""
    parts: list[str] = []
    for term in terms:
        if not _WORD.fullmatch(term):
            continue
        fused = _FUSED.match(term)
        if fused:
            parts.append(f"({term} | {fused.group(1)} <-> {fused.group(2)})")
        else:
            parts.append(f"{term}:*")
    return " | ".join(parts)


def token_set(text: str) -> frozenset[str]:
    """Stemmed lowercase words of `text`, plus the fused word+number form of every word that is
    followed by a number, for coverage checks."""
    tokens: set[str] = set()
    previous: str | None = None
    for match in _WORD.finditer(text):
        word = match.group(0).lower()
        if word.isdigit():
            if previous is not None:
                tokens.add(previous + word)
            tokens.add(word)
            previous = None
            continue
        token = word if is_numbered(word) else stem(word)
        tokens.add(token)
        previous = None if is_numbered(word) else token
    return frozenset(tokens)


def coverage(terms: list[str], text: str) -> float:
    """Share of `terms` found in `text`: a numbered term must match exactly, a long word may match
    by prefix, a short word must match a whole word; 0.0 for no terms."""
    if not terms:
        return 0.0
    tokens = token_set(text)
    hit = 0
    for term in terms:
        if term in tokens:
            hit += 1
        elif is_numbered(term):
            continue  # "enhancement1" must not match "enhancement10"
        elif len(term) >= PREFIX_MIN_CHARS and any(t.startswith(term) for t in tokens):
            hit += 1
    return hit / len(terms)
