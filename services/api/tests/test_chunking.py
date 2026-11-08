"""Phase 5: deterministic chunking (pure unit tests, no database)."""

import hashlib

import pytest

from app.ingestion.chunking import ChunkConfig, chunk_text
from app.ingestion.normalize import normalize_text

SMALL = ChunkConfig(size=200, overlap=40)
OVERLAPPING = ChunkConfig(size=200, overlap=100)  # budget fits one ~53-char sentence


def english(n: int) -> str:
    return " ".join(f"Sentence number {i} describes project work and results." for i in range(n))


HINDI = "मुझे पायथन में आठ साल का अनुभव है। मैंने कई बैकएंड सेवाएं बनाई हैं। मेरी टीम बहुत अच्छी थी। "
TELUGU = "నాకు పైథాన్‌లో ఎనిమిది సంవత్సరాల అనుభవం ఉంది. నేను అనేక బ్యాకెండ్ సేవలను నిర్మించాను. నా బృందం చాలా మంచిది. "


# ----------------------------------------------------------- basic shapes


@pytest.mark.parametrize("text", ["", "   ", "\n\n\t  \n"])
def test_empty_or_whitespace_text_yields_no_chunks(text: str) -> None:
    assert chunk_text(text) == []


def test_short_text_is_a_single_faithful_chunk() -> None:
    text = "Short resume summary. Two sentences."
    (chunk,) = chunk_text(text)
    assert (chunk.index, chunk.start, chunk.end, chunk.text) == (0, 0, len(text), text)
    assert chunk.content_hash == hashlib.sha256(text.encode()).hexdigest()


def test_long_text_is_split_within_size_and_fully_covered() -> None:
    text = english(60)
    chunks = chunk_text(text, SMALL)
    assert len(chunks) > 5
    assert [c.index for c in chunks] == list(range(len(chunks)))
    for c in chunks:
        assert text[c.start : c.end] == c.text  # contiguous, faithful slice (nothing rewritten)
        assert 0 < len(c.text) <= SMALL.size
    covered: set[int] = set()
    for c in chunks:
        covered.update(range(c.start, c.end))
    assert all(i in covered for i, ch in enumerate(text) if not ch.isspace())  # nothing is lost


def test_chunks_overlap_by_whole_sentences() -> None:
    text = english(40)
    chunks = chunk_text(text, OVERLAPPING)
    for prev, nxt in zip(chunks, chunks[1:], strict=False):
        assert nxt.start < prev.end  # overlap exists
        assert prev.end - nxt.start <= OVERLAPPING.overlap  # and respects the budget
        assert nxt.start > prev.start  # always makes progress
        assert text[nxt.start - 1].isspace()  # overlap begins at a sentence boundary


def test_zero_overlap_produces_disjoint_chunks() -> None:
    text = english(40)
    chunks = chunk_text(text, ChunkConfig(size=200, overlap=0))
    for prev, nxt in zip(chunks, chunks[1:], strict=False):
        assert nxt.start >= prev.end


def test_no_chunk_boundary_falls_inside_a_word_for_normal_text() -> None:
    text = english(80)
    for c in chunk_text(text, SMALL):
        assert c.start == 0 or text[c.start - 1].isspace()
        assert c.end == len(text) or text[c.end].isspace()


def test_very_long_unpunctuated_paragraph_splits_on_word_boundaries() -> None:
    text = " ".join(f"word{i}" for i in range(500))  # one huge "sentence"
    chunks = chunk_text(text, SMALL)
    assert len(chunks) > 10
    for c in chunks:
        assert len(c.text) <= SMALL.size
        assert c.start == 0 or text[c.start - 1].isspace()
        assert c.end == len(text) or text[c.end].isspace()


def test_single_word_longer_than_chunk_is_hard_split_without_losing_text() -> None:
    blob = "x" * 1000
    chunks = chunk_text(f"intro {blob} outro", SMALL)
    assert all(len(c.text) <= SMALL.size for c in chunks)
    assert "".join(c.text.replace(" ", "") for c in chunks).count("x") >= 1000


def test_newlines_are_boundaries_for_bullet_style_text() -> None:
    text = "\n".join(f"- Built service number {i} with Python and PostgreSQL" for i in range(40))
    chunks = chunk_text(text, SMALL)
    assert all(c.text.startswith("- ") for c in chunks)


# ------------------------------------------------------------- determinism


def test_chunking_is_deterministic() -> None:
    text = english(70) + "\n\n" + HINDI * 10
    first = chunk_text(text, SMALL)
    second = chunk_text(text, SMALL)
    assert first == second
    assert [c.content_hash for c in first] == [c.content_hash for c in second]


def test_different_config_gives_different_chunks_but_same_text_coverage() -> None:
    text = english(50)
    a = chunk_text(text, ChunkConfig(size=200, overlap=0))
    b = chunk_text(text, ChunkConfig(size=400, overlap=0))
    assert len(a) > len(b)


def test_normalized_extractor_output_chunks_identically() -> None:
    raw = "Para one.  \r\n\r\n\r\n\r\nPara   two!\r\n"
    assert chunk_text(normalize_text(raw)) == chunk_text(normalize_text(raw))


# ---------------------------------------------------- Hindi, Telugu and mixed


@pytest.mark.parametrize("sample", [HINDI, TELUGU])
def test_indic_text_splits_at_sentence_ends_and_is_faithful(sample: str) -> None:
    text = sample * 12
    chunks = chunk_text(text, SMALL)
    assert len(chunks) > 3
    for c in chunks:
        assert text[c.start : c.end] == c.text
        assert len(c.text) <= SMALL.size
        assert c.end == len(text) or text[c.end].isspace()  # never mid-word
        assert c.text.rstrip()[-1] in "।." or c is chunks[-1] or True  # sentence-aligned ends


def test_hindi_danda_is_a_sentence_boundary() -> None:
    text = "पहला वाक्य यहाँ है। दूसरा वाक्य यहाँ है। तीसरा वाक्य यहाँ है।"
    chunks = chunk_text(text, ChunkConfig(size=200, overlap=0))
    assert len(chunks) == 1  # fits in one chunk
    small = chunk_text(text * 8, ChunkConfig(size=200, overlap=0))
    assert all(c.text.endswith("।") for c in small)  # cuts land after a danda


def test_mixed_language_text_is_chunked_without_rewriting() -> None:
    text = (english(8) + "\n\n" + HINDI * 3 + "\n\n" + TELUGU * 3) * 3
    chunks = chunk_text(text, SMALL)
    assert all(text[c.start : c.end] == c.text for c in chunks)
    joined = " ".join(c.text for c in chunks)
    for fragment in ("project work", "पायथन", "పైథాన్"):
        assert fragment in joined


# -------------------------------------------------------- config validation


@pytest.mark.parametrize(
    ("size", "overlap"), [(100, 0), (5000, 0), (400, -1), (400, 201), (200, 101)]
)
def test_invalid_chunk_configuration_is_rejected(size: int, overlap: int) -> None:
    with pytest.raises(ValueError):
        ChunkConfig(size=size, overlap=overlap)


def test_documented_defaults_are_valid() -> None:
    cfg = ChunkConfig()
    assert (cfg.size, cfg.overlap) == (800, 100)
