"""Conservative text normalisation.

Goals: stable, clean input for future chunking while staying faithful to the source. This layer
never summarises, reorders, spell-corrects or rewrites content.
"""

import re
import unicodedata

# Characters that carry no meaning for text processing: C0/C1 controls (except \n and \t, which
# are handled separately), DEL, and the zero-width no-break space / BOM.
_REMOVE = {chr(c) for c in range(0x00, 0x20)} | {chr(0x7F)} | {chr(c) for c in range(0x80, 0xA0)}
_REMOVE |= {"﻿"}
_KEEP = {"\n", "\t"}

_INLINE_SPACE = re.compile(r"[ \t  -   　]+")
_TRAILING_SPACE = re.compile(r" +\n")
_LEADING_SPACE = re.compile(r"\n +")
_BLANK_RUNS = re.compile(r"\n{3,}")


def normalize_text(raw: str) -> str:
    # 1. Line endings: CRLF / CR -> LF; form feed (PDF page break) -> paragraph-ish newline.
    text = raw.replace("\r\n", "\n").replace("\r", "\n").replace("\f", "\n")
    # 2. Drop null and other control characters that are not useful for text processing.
    text = "".join(ch for ch in text if ch in _KEEP or ch not in _REMOVE)
    # 3. Excessive horizontal whitespace -> a single space; trim line edges.
    text = _INLINE_SPACE.sub(" ", text)
    text = _TRAILING_SPACE.sub("\n", text)
    text = _LEADING_SPACE.sub("\n", text)
    # 4. Keep paragraph boundaries (one blank line) but collapse longer runs of blank lines.
    text = _BLANK_RUNS.sub("\n\n", text)
    return text.strip()


def is_effectively_empty(text: str) -> bool:
    """True when there is no letter or digit (e.g. only punctuation or whitespace)."""
    return not any(unicodedata.category(ch)[0] in ("L", "N") for ch in text)
