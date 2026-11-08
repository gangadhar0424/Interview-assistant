"""Upload validation: file names, declared types and file-signature checks.

Validation never trusts the client: the extension selects the expected format, the declared MIME
type must not contradict it, and the actual bytes must look like that format. All checks read the
temp file in bounded chunks (nothing large is loaded into memory).
"""

import codecs
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import BinaryIO

CHUNK_SIZE = 64 * 1024
MAX_DISPLAY_FILENAME_LENGTH = 255

# DOCX is a ZIP container; bound it so a "zip bomb" cannot be uploaded and later extracted.
DOCX_MAX_ENTRIES = 5_000
DOCX_MAX_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
_DOCX_REQUIRED = ("[Content_Types].xml", "word/document.xml")
_DOCX_FORBIDDEN = ("word/vbaProject.bin",)  # macro-enabled documents


class InvalidContent(Exception):
    """The bytes do not match the expected format (message is safe to show to users)."""


def sanitize_filename(raw: str | None) -> str:
    """Return a safe *display* name: no directories, control characters or reserved symbols.

    The result is stored as metadata only; it is never used to build a filesystem path.
    """
    name = (raw or "").replace("\\", "/").split("/")[-1]
    name = "".join(ch for ch in name if ch.isprintable() and ch not in '<>:"|?*')
    name = name.strip(" .")[:MAX_DISPLAY_FILENAME_LENGTH].strip(" .")
    return name or "upload"


def extension_of(filename: str) -> str:
    return PurePosixPath(filename).suffix.lower()


def _validate_pdf(handle: BinaryIO, size: int) -> None:
    handle.seek(0)
    if handle.read(5) != b"%PDF-":
        raise InvalidContent("The file is not a valid PDF.")
    handle.seek(max(0, size - 1024))
    if b"%%EOF" not in handle.read():
        raise InvalidContent("The PDF file appears to be truncated or corrupt.")


def _validate_docx(handle: BinaryIO, size: int) -> None:
    del size
    handle.seek(0)
    try:
        with zipfile.ZipFile(handle) as archive:
            infos = archive.infolist()
            names = {info.filename for info in infos}
            if len(infos) > DOCX_MAX_ENTRIES:
                raise InvalidContent("The DOCX file is not acceptable.")
            if sum(info.file_size for info in infos) > DOCX_MAX_UNCOMPRESSED_BYTES:
                raise InvalidContent("The DOCX file is not acceptable.")
            if any(n.startswith("/") or ".." in n.split("/") for n in names):
                raise InvalidContent("The DOCX file is not acceptable.")
            if not all(required in names for required in _DOCX_REQUIRED):
                raise InvalidContent("The file is not a valid DOCX document.")
            if any(forbidden in names for forbidden in _DOCX_FORBIDDEN):
                raise InvalidContent("Macro-enabled documents are not accepted.")
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, EOFError, ValueError):
        raise InvalidContent("The file is not a valid DOCX document.") from None


def _validate_txt(handle: BinaryIO, size: int) -> None:
    del size
    handle.seek(0)
    decoder = codecs.getincrementaldecoder("utf-8-sig")()
    try:
        while chunk := handle.read(CHUNK_SIZE):
            if b"\x00" in chunk:
                raise InvalidContent("The file is not a plain-text (UTF-8) file.")
            decoder.decode(chunk)
        decoder.decode(b"", final=True)
    except UnicodeDecodeError:
        raise InvalidContent("The file is not a plain-text (UTF-8) file.") from None


@dataclass(frozen=True)
class FileFormat:
    name: str
    mime: str  # canonical type stored and served
    accepted_declared: frozenset[str]  # client-declared types that do not contradict the format
    validate: Callable[[BinaryIO, int], None]


_OCTET = "application/octet-stream"

FORMATS: dict[str, FileFormat] = {
    ".pdf": FileFormat(
        "pdf", "application/pdf", frozenset({"application/pdf", "application/x-pdf"}), _validate_pdf
    ),
    ".docx": FileFormat(
        "docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        frozenset(
            {
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "application/zip",
            }
        ),
        _validate_docx,
    ),
    ".txt": FileFormat("txt", "text/plain", frozenset({"text/plain"}), _validate_txt),
}

# Per-kind allow-lists (single place to change which types a kind accepts).
RESUME_EXTENSIONS = frozenset({".pdf", ".docx"})
DOCUMENT_EXTENSIONS = frozenset({".pdf", ".docx", ".txt"})


def declared_type_acceptable(fmt: FileFormat, declared: str | None) -> bool:
    """A declared type may be missing or generic, but must not contradict the extension."""
    value = (declared or "").split(";")[0].strip().lower()
    return value in ("", _OCTET) or value in fmt.accepted_declared
