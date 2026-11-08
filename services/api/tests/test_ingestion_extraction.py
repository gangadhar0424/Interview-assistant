"""Phase 4: normalisation and extractors (pure unit tests, no database)."""

import pytest

from app.ingestion.extractors import (
    MAX_PDF_PAGES,
    DocxExtractor,
    ExtractionError,
    PdfExtractor,
    TextExtractor,
    extractor_for,
)
from app.ingestion.normalize import is_effectively_empty, normalize_text
from tests.helpers import encrypted_pdf, make_docx, real_docx, real_pdf

# ------------------------------------------------------------------ normalisation


def test_normalize_line_endings_and_blank_runs() -> None:
    assert normalize_text("a\r\nb\rc\n\n\n\n\nd") == "a\nb\nc\n\nd"


def test_normalize_collapses_inline_whitespace_but_keeps_paragraphs() -> None:
    raw = "  Hello   \t world   again  \n\n\n  Second   paragraph  \n"
    assert normalize_text(raw) == "Hello world again\n\nSecond paragraph"


def test_normalize_removes_null_and_control_characters_but_keeps_text() -> None:
    raw = "na\x00me\x07 is\x1b ok﻿\x7f\x85 done"
    assert normalize_text(raw) == "name is ok done"


def test_normalize_turns_form_feed_into_a_break_and_is_faithful_to_unicode() -> None:
    assert normalize_text("page1\fpage2") == "page1\npage2"
    hindi = "नमस्ते दुनिया — తెలుగు"
    assert normalize_text(hindi) == hindi  # not rewritten, not NFC/NFKC-transformed


def test_normalize_is_idempotent() -> None:
    once = normalize_text("x \r\n\r\n\r\n y\x00  ")
    assert normalize_text(once) == once


@pytest.mark.parametrize(
    ("text", "empty"), [("", True), ("  \n\t", True), ("...---", True), ("a", False), ("७", False)]
)
def test_effectively_empty(text: str, empty: bool) -> None:
    assert is_effectively_empty(text) is empty


# ------------------------------------------------------------------------- PDF


def test_pdf_text_is_extracted_from_all_pages() -> None:
    text = PdfExtractor().extract(real_pdf("First page text", "Second page text"))
    assert "First page text" in text and "Second page text" in text
    assert text.index("First") < text.index("Second")


def test_pdf_without_text_yields_no_text() -> None:
    assert is_effectively_empty(normalize_text(PdfExtractor().extract(real_pdf(""))))


def test_encrypted_pdf_is_a_safe_failure() -> None:
    with pytest.raises(ExtractionError) as exc:
        PdfExtractor().extract(encrypted_pdf())
    assert exc.value.code == "encrypted_file"
    assert "secret" not in exc.value.message


@pytest.mark.parametrize(
    "data",
    [b"%PDF-1.4\nthis is garbage\n%%EOF\n", b"%PDF-", b"", b"\x00" * 100, b"not a pdf"],
)
def test_malformed_pdf_is_a_safe_failure(data: bytes) -> None:
    with pytest.raises(ExtractionError) as exc:
        PdfExtractor().extract(data)
    assert exc.value.code == "unreadable_file"


def test_pdf_page_limit() -> None:
    with pytest.raises(ExtractionError) as exc:
        PdfExtractor().extract(real_pdf(*["x"] * (MAX_PDF_PAGES + 1)))
    assert exc.value.code == "too_many_pages"


# ------------------------------------------------------------------------ DOCX


def test_docx_paragraphs_and_tables_are_extracted_in_order() -> None:
    data = real_docx(
        ["Jane Doe", "Experienced engineer."], table=[["Skill", "Years"], ["Python", "8"]]
    )
    text = DocxExtractor().extract(data)
    assert (
        text.index("Jane Doe") < text.index("Experienced engineer.") < text.index("Skill | Years")
    )
    assert "Python | 8" in text


@pytest.mark.parametrize(
    "data",
    [
        make_docx(),  # passes Phase 3 signature checks but is not a real Word document
        b"PK\x03\x04 truncated zip",
        b"plain bytes",
        b"",
    ],
)
def test_malformed_docx_is_a_safe_failure(data: bytes) -> None:
    with pytest.raises(ExtractionError) as exc:
        DocxExtractor().extract(data)
    assert exc.value.code == "unreadable_file"


# ------------------------------------------------------------------------- TXT


def test_text_decoding_utf8_with_bom_and_crlf() -> None:
    raw = TextExtractor().extract("﻿Line one\r\nLine two\r\n".encode())
    assert normalize_text(raw) == "Line one\nLine two"


@pytest.mark.parametrize("data", [b"\xff\xfe\xfa invalid", b"caf\xe9 latin-1"])
def test_text_decoding_failure_is_predictable(data: bytes) -> None:
    with pytest.raises(ExtractionError) as exc:
        TextExtractor().extract(data)
    assert exc.value.code == "invalid_encoding"


def test_unknown_content_type_has_no_extractor() -> None:
    with pytest.raises(ExtractionError) as exc:
        extractor_for("image/png")
    assert exc.value.code == "unsupported_file_type"
    assert extractor_for("application/pdf").name == "pymupdf"
    assert extractor_for("text/plain").name == "text"
