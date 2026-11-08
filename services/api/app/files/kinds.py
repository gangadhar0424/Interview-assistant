from dataclasses import dataclass

from app.core.config import Settings
from app.db.models import Document, Resume
from app.files.validation import DOCUMENT_EXTENSIONS, RESUME_EXTENSIONS


@dataclass(frozen=True)
class FileKind:
    """Everything that differs between resumes and documents."""

    name: str
    namespace: str  # storage namespace / URL segment
    model: type[Resume] | type[Document]
    allowed_extensions: frozenset[str]
    max_bytes: int


def resume_kind(settings: Settings) -> FileKind:
    return FileKind("resume", "resumes", Resume, RESUME_EXTENSIONS, settings.resume_max_bytes)


def document_kind(settings: Settings) -> FileKind:
    return FileKind(
        "document", "documents", Document, DOCUMENT_EXTENSIONS, settings.document_max_bytes
    )
