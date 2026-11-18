"""Word files keep their heading structure through extraction (D-66)."""

import io

import docx

from app.ingestion.chunking import chunk_sections
from app.ingestion.extractors import DocxExtractor
from app.ingestion.normalize import normalize_text

BODY = "This paragraph explains the topic in enough detail to be a real section of text. " * 4


def build_docx() -> bytes:
    document = docx.Document()
    document.add_heading("Interview Notes", level=0)
    document.add_heading("Receiving Excess Quantity", level=1)
    document.add_paragraph(BODY + "The operator scans the article.")
    document.add_heading("Validations", level=2)
    document.add_paragraph(BODY + "The bin must exist.")
    document.add_heading("Putaway by Cart", level=1)
    document.add_paragraph(BODY + "Totes are assigned to a cart.")
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def test_word_heading_styles_become_markdown_headings() -> None:
    text = DocxExtractor().extract(build_docx())
    lines = text.splitlines()
    assert "# Interview Notes" in lines
    assert "# Receiving Excess Quantity" in lines
    assert "## Validations" in lines
    assert "# Putaway by Cart" in lines
    assert BODY.strip()[:40] in text  # ordinary paragraphs are unchanged, without a marker


def test_a_word_document_is_chunked_by_its_real_sections() -> None:
    text = normalize_text(DocxExtractor().extract(build_docx()))
    chunks = chunk_sections(text)
    headings = {c.heading for c in chunks}
    assert any(h and h.endswith("Receiving Excess Quantity") for h in headings)
    assert any(h and h.endswith("Validations") for h in headings)
    parent = next(
        c for c in chunks if c.heading and c.heading.endswith("Receiving Excess Quantity")
    )
    section = text[parent.section_start : parent.section_end]
    assert "Validations" in section and "Putaway by Cart" not in section


def test_a_document_without_heading_styles_extracts_exactly_as_before() -> None:
    document = docx.Document()
    document.add_paragraph("Plain first paragraph.")
    document.add_paragraph("Plain second paragraph.")
    buffer = io.BytesIO()
    document.save(buffer)
    assert (
        DocxExtractor().extract(buffer.getvalue())
        == "Plain first paragraph.\n\nPlain second paragraph."
    )
