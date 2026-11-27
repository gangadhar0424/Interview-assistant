"""SEC-33: the ASCII fast path of `clean_text` gives exactly the results of the original rules.

`reference` below is the implementation as it was before the fast path (a character-by-character
loop), copied verbatim. The tests compare the live function with it on every ASCII character, on
seeded random strings dense in control characters and line endings, and on non-ASCII text that
exercises each rule (bidi overrides, zero-width and tag characters, Hindi and Telugu joiners, line
separators, private use, surrogates). Nothing here depends on timing.
"""

import random
import unicodedata

import pytest

from app.preferences import sanitize
from app.preferences.sanitize import clean_text

_KEEP_FORMAT_CHARS = frozenset({"\N{ZERO WIDTH NON-JOINER}", "\N{ZERO WIDTH JOINER}"})
_LINE_BREAKS = frozenset({"\N{LINE SEPARATOR}", "\N{PARAGRAPH SEPARATOR}", "\u0085"})


def reference(text: str) -> str:
    import re

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
    cleaned = re.sub(r"[ \t]+\n", "\n", "".join(out))
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def test_every_single_ascii_character_matches_the_reference() -> None:
    for code in range(128):
        ch = chr(code)
        for sample in (ch, f"a{ch}b", f"  {ch}  ", f"a{ch}{ch}{ch}b", f"x\n{ch}\ny"):
            assert clean_text(sample) == reference(sample), repr(sample)


def test_seeded_random_ascii_strings_match_the_reference() -> None:
    rng = random.Random(20261123)  # noqa: S311 (seeded, for reproducible test data)
    alphabet = [chr(c) for c in range(128)]
    alphabet += ["\r\n"] * 20 + ["\n"] * 40 + [" "] * 40 + ["\t"] * 20 + ["a"] * 60
    for _ in range(4000):
        sample = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 240)))
        assert clean_text(sample) == reference(sample), repr(sample)


NON_ASCII = [
    "café naïve",  # accents (NFC)
    "é",  # decomposed e + combining acute: NFC composes it
    "नमस्ते दुनिया",  # Hindi
    "నమస్కారం",  # Telugu
    "क्‍ष క్‌ష",  # Hindi and Telugu with ZWJ and ZWNJ (kept)
    "abc‮def",  # right-to-left override (format: dropped)
    "a​b⁠c﻿d",  # zero-width space, word joiner, BOM (format: dropped)
    "tag\U000e0041\U000e0042end",  # Unicode tag characters (format: dropped)
    "line sep para\u0085nel",  # separators become newlines
    "private",  # private use (dropped)
    "x\ud800y",  # a lone surrogate (dropped)
    "mix \u0001 of \t all\r\nthe\rthings     here  \n\n\n\n end ‍",
    " nbsp  and 　ideographic　space",
]


@pytest.mark.parametrize("sample", NON_ASCII)
def test_non_ascii_text_matches_the_reference(sample: str) -> None:
    assert clean_text(sample) == reference(sample)


def test_cleaning_is_idempotent_on_both_paths() -> None:
    rng = random.Random(7)  # noqa: S311 (a seeded generator for reproducible test data)
    pool = [chr(c) for c in range(128)] + list("éन‍‮ x")
    for _ in range(1500):
        once = clean_text("".join(rng.choice(pool) for _ in range(rng.randint(0, 120))))
        assert clean_text(once) == once


def test_ascii_text_takes_the_fast_path_and_never_the_character_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(_: str) -> str:
        raise AssertionError("the character loop ran for pure-ASCII text")

    monkeypatch.setattr(sanitize, "_clean_characters", forbidden)
    assert clean_text("Hello\tworld\x00!\r\n\r\n\r\n\r\nbye") == "Hello world!\n\nbye"
    big = "y" * 2_000_000  # a very large answer costs one pass at C speed
    assert clean_text(big) == big


def test_text_with_any_non_ascii_character_still_uses_the_reference_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []
    original = sanitize._clean_characters

    def spy(text: str) -> str:
        calls.append(len(text))
        return original(text)

    monkeypatch.setattr(sanitize, "_clean_characters", spy)
    assert clean_text("abc‮def") == "abcdef"  # the bidi override is still removed
    assert calls == [7]


def test_known_outputs_did_not_change() -> None:
    assert clean_text("  a\t\tb  \n\n\n\nc  ") == "a  b\n\nc"
    assert clean_text("x​y") == "xy"
    assert clean_text("क्‍ष") == "क्‍ष"  # ZWJ kept
    assert clean_text("") == ""
