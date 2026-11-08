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


def clean_text(text: str) -> str:
    """NFC-normalise, drop unsafe control/format characters, tidy whitespace (pure, idempotent)."""
    out: list[str] = []
    for ch in unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n")):
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
    cleaned = _TRAILING_SPACE.sub("\n", "".join(out))
    return _BLANK_RUNS.sub("\n\n", cleaned).strip()
