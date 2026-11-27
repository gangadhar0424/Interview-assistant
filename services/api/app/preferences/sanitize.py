"""Cleaning of user-supplied free text before it is stored or placed in a prompt.

This removes characters that are invisible or can disguise content (control characters, bidi
overrides, zero-width characters, Unicode "tag" characters). It does NOT make text safe to obey:
untrusted text is still only ever placed inside delimited data blocks by the PromptBuilder.
"""

import re
import unicodedata

# ZWNJ/ZWJ are format characters (category Cf) but are required to spell many Hindi and Telugu
# words correctly (conjuncts), so they must survive.
_KEEP_FORMAT_CHARS = frozenset({"\N{ZERO WIDTH NON-JOINER}", "\N{ZERO WIDTH JOINER}"})
_LINE_BREAKS = frozenset({"\N{LINE SEPARATOR}", "\N{PARAGRAPH SEPARATOR}", "\u0085"})
_BLANK_RUNS = re.compile(r"\n{3,}")
_TRAILING_SPACE = re.compile(r"[ \t]+\n")


# ASCII fast path (SEC-33). For pure-ASCII text the per-character loop below reduces to a table: a
# tab becomes a space, every other control character (category Cc: U+0000 to U+001F and U+007F) is
# dropped, and the newline is kept. The line-break, format, surrogate and private-use rules cannot
# apply to ASCII. `str.translate` does this in C, about a hundred times faster than the loop, which
# matters only for extreme inputs (a 25 MiB answer took about 14 s in the loop);
# tests/test_sanitize_fast_path.py proves both paths agree on every ASCII character.
_ASCII_TABLE: dict[int, str | None] = {
    code: None for code in range(0x20) if code not in (0x09, 0x0A)
}
_ASCII_TABLE[0x09] = " "
_ASCII_TABLE[0x7F] = None


def _clean_characters(normalized: str) -> str:
    """The reference rules, character by character (used for any text that is not pure ASCII)."""
    out: list[str] = []
    for ch in normalized:
        if ch == "\n" or ch in _LINE_BREAKS:
            out.append("\n")
        elif ch == "\t":
            out.append(" ")
        else:
            category = unicodedata.category(ch)
            if category in ("Cc", "Cs", "Co") or (
                category == "Cf" and ch not in _KEEP_FORMAT_CHARS
            ):
                continue
            out.append(ch)
    return "".join(out)


def clean_text(text: str) -> str:
    """NFC-normalise, drop unsafe control/format characters, tidy whitespace (pure, idempotent)."""
    unified = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = unicodedata.normalize("NFC", unified)
    if normalized.isascii():
        joined = normalized.translate(_ASCII_TABLE)
    else:
        joined = _clean_characters(normalized)
    cleaned = _TRAILING_SPACE.sub("\n", joined)
    return _BLANK_RUNS.sub("\n\n", cleaned).strip()
