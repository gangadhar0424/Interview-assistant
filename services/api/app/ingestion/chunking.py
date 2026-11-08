"""Deterministic text chunking.

Chunks are contiguous slices of the (already normalised) extracted text, so they are faithful to
the source: nothing is rewritten or translated. Boundaries prefer, in order: line breaks and
sentence ends (including the Devanagari danda `।`/`॥`), then word boundaries inside an over-long
sentence, and only for a single "word" longer than the chunk size a hard cut. Consecutive chunks
overlap by whole sentences/lines up to `overlap` characters.

Sizes are in characters (no tokenizer dependency; works for every script). Defaults suit the
embedding model's 512-token window even for Indic scripts, which use more tokens per character.
"""

import hashlib
import re
from dataclasses import dataclass

DEFAULT_CHUNK_SIZE = 800
DEFAULT_CHUNK_OVERLAP = 100
MIN_CHUNK_SIZE = 200
MAX_CHUNK_SIZE = 4000

_LINE = re.compile(r"\S[^\n]*")
# Sentence end: . ! ? … । ॥ (optionally followed by closing quotes/brackets) before whitespace.
_SENTENCE_END = re.compile(r"[.!?…।॥]+[\"')\]”’]*(?=\s)")


@dataclass(frozen=True)
class ChunkConfig:
    size: int = DEFAULT_CHUNK_SIZE
    overlap: int = DEFAULT_CHUNK_OVERLAP

    def __post_init__(self) -> None:
        if not MIN_CHUNK_SIZE <= self.size <= MAX_CHUNK_SIZE:
            raise ValueError(f"chunk size must be between {MIN_CHUNK_SIZE} and {MAX_CHUNK_SIZE}")
        if self.overlap < 0 or self.overlap * 2 > self.size:
            raise ValueError("chunk overlap must be >= 0 and at most half the chunk size")


@dataclass(frozen=True)
class TextChunk:
    index: int
    start: int  # character offsets into the source text
    end: int
    text: str
    content_hash: str  # sha256 of `text`


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for line in _LINE.finditer(text):
        line_end = line.end()
        position = line.start()
        for end_match in _SENTENCE_END.finditer(text, line.start(), line_end):
            spans.append((position, end_match.end()))
            position = end_match.end()
            while position < line_end and text[position].isspace():
                position += 1
        if position < line_end:
            spans.append((position, line_end))
    return [(s, e) for s, e in spans if e > s]


def _split_long(text: str, start: int, end: int, size: int) -> list[tuple[int, int]]:
    """Split an over-long unit at word boundaries (hard cut only inside a single huge word)."""
    pieces: list[tuple[int, int]] = []
    while end - start > size:
        cut = max(
            text.rfind(" ", start, start + size + 1), text.rfind("\t", start, start + size + 1)
        )
        if cut <= start:
            cut = start + size
        pieces.append((start, cut))
        start = cut
        while start < end and text[start].isspace():
            start += 1
    if end > start:
        pieces.append((start, end))
    return pieces


def chunk_text(text: str, config: ChunkConfig | None = None) -> list[TextChunk]:
    cfg = config or ChunkConfig()
    units: list[tuple[int, int]] = []
    for start, end in _sentence_spans(text):
        if end - start > cfg.size:
            units.extend(_split_long(text, start, end, cfg.size))
        else:
            units.append((start, end))
    if not units:
        return []

    chunks: list[TextChunk] = []
    i = 0
    while i < len(units):
        start = units[i][0]
        j = i
        while j + 1 < len(units) and units[j + 1][1] - start <= cfg.size:
            j += 1
        end = units[j][1]
        body = text[start:end]
        chunks.append(
            TextChunk(
                index=len(chunks),
                start=start,
                end=end,
                text=body,
                content_hash=hashlib.sha256(body.encode("utf-8")).hexdigest(),
            )
        )
        if j + 1 >= len(units):
            break
        k = j + 1  # next chunk begins with as many trailing units as fit in the overlap budget
        while k - 1 > i and end - units[k - 1][0] <= cfg.overlap:
            k -= 1
        i = k
    return chunks
