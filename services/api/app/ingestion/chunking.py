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
    # D-66, section-aware chunks only: the heading path ("40. Enhancement in EWM > Enhancement1")
    # and the character range of the section this chunk belongs to (offsets into the source text).
    heading: str | None = None
    section_start: int | None = None
    section_end: int | None = None

    @property
    def embed_text(self) -> str:
        """What is embedded: the heading path gives the passage its context (the stored and
        returned `text` stays the unchanged source slice)."""
        return f"{self.heading}" + chr(10) + self.text if self.heading else self.text


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


# ----------------------------------------------------------------- section-aware chunking (D-66)

MIN_HEADINGS = 2  # fewer headings than this: the text has no usable structure, use fixed chunks
MIN_SEGMENT_CHARS = 0  # 0: every heading keeps its own chunk, so its title can be matched (D-66)
MIN_SECTION_CHARS = 300  # a section shorter than this is widened to its parent section
MAX_HEADING_CHARS = 100
MAX_PATH_TITLE_CHARS = 120
MAX_NUMBER_GAP = 3  # largest jump in heading numbers that still counts as the next heading

_NUMBERED_HEADING = re.compile(r"^(\d{1,3})[.)]\s+\S")
# "35 Transportation Management": a number without punctuation, accepted only as the very next
# heading number (see `_find_headings`), so "7 years of experience" style lines stay body text.
_BARE_NUMBERED_HEADING = re.compile(r"^(\d{1,3})\s+[A-Z]")
# A numbered line that opens like an instruction is a list step, not a heading.
_LIST_STEP = re.compile(
    r"^\d{1,3}[.)]\s+(?:Check|Implement|Return|Review|Confirm|Define|Use|Read|Keep|Test|"
    r"Identify|Inspect|Debug|Ensure|Validate|Document|Determine|Configure|Set|Add|Update|"
    r"Maintain|Map|Assess|Trace|Clarify|Pinpoint|Preserve|Apply|Verify|Make|Run|Take|Copy|"
    r"Remove|Transport|Train|Prepare|Understand|Start|Allow|Handle|Avoid|Insert|Select|Enter|"
    r"Provide|Find|Look|Ask|If|When|Where|Do|Don't|Can|Should|Then|And|Also)\b"
)
_LEVEL1_WORD = re.compile(r"^(Part|Chapter|Section|Project)\s*#?\s*\d+\b", re.IGNORECASE)
_LEVEL2_WORD = re.compile(r"^(Enhancement|Step|Phase)\s*#?\s*\d+\b", re.IGNORECASE)
_MARKDOWN_HEADING = re.compile(r"^(#{1,3})\s+\S")
_COLON_HEADING = re.compile(r"^[A-Z][^:\n]{1,58}:$")


@dataclass(frozen=True)
class _Heading:
    start: int  # offsets of the heading line in the source text
    end: int
    level: int  # 1 = top-level section ... 3 = sub-heading
    title: str


def _heading_level(line: str) -> int | None:
    """Level of a heading-like line, or None. Conservative: a numbered list item that reads like a
    sentence (ends with . : ;) is not a heading."""
    if not line:
        return None
    markdown = _MARKDOWN_HEADING.match(line)
    if markdown:
        return len(markdown.group(1))
    if len(line) > MAX_HEADING_CHARS:
        return None
    if _NUMBERED_HEADING.match(line) and line[-1] not in ".:;":
        if ";" in line or _LIST_STEP.match(line):
            return None
        return 1
    if _LEVEL1_WORD.match(line) and not line.endswith("."):
        return 1
    if _LEVEL2_WORD.match(line) and not line.endswith("."):
        return 2
    if _COLON_HEADING.match(line) and len(line.split()) <= 6:
        return 3
    return None


def _find_headings(text: str) -> list[_Heading]:
    """Headings of `text`. A numbered line only counts as a heading when it continues the running
    numbering (the next number, allowing a small gap): list steps that restart at 1 under a heading
    are body text. A Part/Chapter/Section/Project heading restarts the numbering."""
    headings: list[_Heading] = []
    last_number = 0
    for line in _LINE.finditer(text):
        stripped = line.group(0).strip()
        level = _heading_level(stripped)
        if level is None and last_number > 0 and len(stripped) <= MAX_HEADING_CHARS:
            bare = _BARE_NUMBERED_HEADING.match(stripped)
            if bare and int(bare.group(1)) == last_number + 1 and stripped[-1] not in ".:;":
                last_number = int(bare.group(1))
                level = 1
        elif level == 1:
            numbered = _NUMBERED_HEADING.match(stripped)
            if numbered:
                number = int(numbered.group(1))
                if last_number == 0 or last_number < number <= last_number + MAX_NUMBER_GAP:
                    last_number = number
                else:
                    level = None
            elif _LEVEL1_WORD.match(stripped):
                last_number = 0
        if level is not None:
            title = stripped.lstrip("#").strip() if stripped.startswith("#") else stripped
            headings.append(_Heading(line.start(), line.end(), level, title))
    return headings


def chunk_sections(text: str, config: ChunkConfig | None = None) -> list[TextChunk]:
    """Chunk along the document's headings so a passage keeps its section context.

    Every chunk is still a contiguous slice of `text` (nothing rewritten). A chunk carries its
    heading path and the range of its section, so an exact-text answer can return the whole
    section. A text with fewer than `MIN_HEADINGS` headings is chunked exactly as before.
    """
    cfg = config or ChunkConfig()
    heads = _find_headings(text)
    if len(heads) < MIN_HEADINGS:
        return chunk_text(text, cfg)

    count = len(heads)
    # Section span of each heading: up to the next heading of the same or a higher level.
    span_end = [len(text)] * count
    for i in range(count):
        for j in range(i + 1, count):
            if heads[j].level <= heads[i].level:
                span_end[i] = heads[j].start
                break
    # Parent of each heading (nearest earlier heading of a higher level) and its heading path.
    parent: list[int | None] = [None] * count
    paths: list[str] = [""] * count
    stack: list[int] = []
    for i, head in enumerate(heads):
        while stack and heads[stack[-1]].level >= head.level:
            stack.pop()
        parent[i] = stack[-1] if stack else None
        stack.append(i)
        paths[i] = " > ".join(heads[k].title[:MAX_PATH_TITLE_CHARS] for k in stack)

    def section_of(i: int) -> tuple[int, int]:
        k = i
        while span_end[k] - heads[k].start < MIN_SECTION_CHARS:
            up = parent[k]
            if up is None:
                break
            k = up
        return heads[k].start, span_end[k]

    # Segments: the text between consecutive headings (any level); tiny heading-only segments are
    # merged into the segment below so a bare title does not become a chunk of its own.
    segments: list[tuple[int, int, int | None]] = []  # (start, end, first heading index)
    if text[: heads[0].start].strip():
        segments.append((0, heads[0].start, None))
    i = 0
    while i < count:
        seg_start = heads[i].start
        seg_end = heads[i + 1].start if i + 1 < count else len(text)
        last = i
        while len(text[heads[i].end : seg_end].strip()) < MIN_SEGMENT_CHARS and last + 1 < count:
            last += 1
            seg_end = heads[last + 1].start if last + 1 < count else len(text)
        segments.append((seg_start, seg_end, i))
        i = last + 1

    chunks: list[TextChunk] = []
    for seg_start, seg_end, head_index in segments:
        section = section_of(head_index) if head_index is not None else (None, None)
        for piece in chunk_text(text[seg_start:seg_end], cfg):
            chunks.append(
                TextChunk(
                    index=len(chunks),
                    start=seg_start + piece.start,
                    end=seg_start + piece.end,
                    text=piece.text,
                    content_hash=piece.content_hash,
                    heading=paths[head_index] if head_index is not None else None,
                    section_start=section[0],
                    section_end=section[1],
                )
            )
    return chunks
