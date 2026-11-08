"""Text extraction behind a small abstraction.

Adding a file type = adding an `Extractor` and registering its stored content type in
EXTRACTORS; the ingestion workflow does not change. Every failure is an `ExtractionError` with a
fixed, user-safe code/message (never parser exception text, content or paths).

Security notes: PyMuPDF only parses and renders text; it does not execute JavaScript or launch
embedded content. python-docx only reads the XML parts; macros are never executed (and macro-
enabled files are already refused at upload). Inputs are size-limited at upload (10/20 MB), PDFs
are page-limited here, and extracted text is size-limited after normalisation.
"""

import io
from typing import Protocol

import docx
import pymupdf
from docx.document import Document as DocxDocument
from docx.table import Table
from docx.text.paragraph import Paragraph

MAX_PDF_PAGES = 500
MAX_TEXT_CHARS = 5_000_000


class ExtractionError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code)
        self.code = code
        self.message = message


class Extractor(Protocol):
    name: str

    def extract(self, data: bytes) -> str:
        """Return raw (un-normalised) text or raise ExtractionError."""
        ...


class PdfExtractor:
    name = "pymupdf"

    def extract(self, data: bytes) -> str:
        try:
            document = pymupdf.open(stream=data, filetype="pdf")  # type: ignore[no-untyped-call]
        except Exception:
            raise ExtractionError("unreadable_file", "The PDF could not be read.") from None
        try:
            if document.needs_pass or document.is_encrypted:
                raise ExtractionError(
                    "encrypted_file", "Password-protected PDFs are not supported."
                )
            if document.page_count > MAX_PDF_PAGES:
                raise ExtractionError(
                    "too_many_pages", f"The PDF has more than {MAX_PDF_PAGES} pages."
                )
            try:
                pages = [
                    document.load_page(i).get_text("text")  # type: ignore[no-untyped-call]
                    for i in range(document.page_count)
                ]
            except Exception:
                raise ExtractionError("unreadable_file", "The PDF could not be read.") from None
        finally:
            document.close()  # type: ignore[no-untyped-call]
        return "\n\n".join(pages)


class DocxExtractor:
    name = "python-docx"

    def extract(self, data: bytes) -> str:
        try:
            document = docx.Document(io.BytesIO(data))
            return "\n\n".join(_docx_blocks(document))
        except ExtractionError:
            raise
        except Exception:
            raise ExtractionError("unreadable_file", "The DOCX file could not be read.") from None


def _docx_blocks(document: DocxDocument) -> list[str]:
    blocks: list[str] = []
    for item in document.iter_inner_content():  # paragraphs and tables, in document order
        if isinstance(item, Paragraph):
            if item.text.strip():
                blocks.append(item.text)
        elif isinstance(item, Table):
            blocks.extend(_table_rows(item))
    return blocks


def _table_rows(table: Table) -> list[str]:
    rows: list[str] = []
    for row in table.rows:
        seen: set[int] = set()
        cells: list[str] = []
        for cell in row.cells:
            if id(cell._tc) in seen:  # merged cells repeat the same underlying element
                continue
            seen.add(id(cell._tc))
            if cell.text.strip():
                cells.append(cell.text.strip())
        if cells:
            rows.append(" | ".join(cells))
    return rows


class TextExtractor:
    name = "text"

    def extract(self, data: bytes) -> str:
        try:
            return data.decode("utf-8-sig")  # strict: undecodable bytes fail predictably
        except UnicodeDecodeError:
            raise ExtractionError("invalid_encoding", "The text file is not valid UTF-8.") from None


# Keyed by the canonical content type stored at upload (see app/files/validation.py).
EXTRACTORS: dict[str, Extractor] = {
    "application/pdf": PdfExtractor(),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": DocxExtractor(),
    "text/plain": TextExtractor(),
}


def extractor_for(content_type: str) -> Extractor:
    try:
        return EXTRACTORS[content_type]
    except KeyError:
        raise ExtractionError(
            "unsupported_file_type", "This file type cannot be processed."
        ) from None
