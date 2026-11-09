"""Builders for small, valid and deliberately invalid upload fixtures (synthetic data only)."""

import io
import zipfile

from fastapi.testclient import TestClient

PASSWORD = "correct horse battery staple"  # noqa: S105
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def make_pdf(extra: bytes = b"") -> bytes:
    return b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n" + extra + b"\ntrailer\n%%EOF\n"


def make_docx(extra_files: dict[str, bytes] | None = None, *, with_document: bool = True) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        if with_document:
            archive.writestr("word/document.xml", "<w:document/>")
        for name, data in (extra_files or {}).items():
            archive.writestr(name, data)
    return buffer.getvalue()


def make_txt(text: str = "Synthetic notes.\n") -> bytes:
    return text.encode("utf-8")


def register(client: TestClient, email: str) -> dict[str, str]:
    """Sign up + log in; return Authorization headers for that user."""
    assert (
        client.post("/api/v1/auth/signup", json={"email": email, "password": PASSWORD}).status_code
        == 201
    )
    login = client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert login.status_code == 200
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


# ---------------------------------------------------------------- Phase 4 helpers
import asyncio  # noqa: E402
import uuid  # noqa: E402
from collections.abc import Awaitable, Callable  # noqa: E402
from pathlib import Path  # noqa: E402

import docx  # noqa: E402
import pymupdf  # noqa: E402
from sqlalchemy import text as sql_text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker  # noqa: E402

from app.db.session import (  # noqa: E402
    create_engine,
    create_maintenance_sessionmaker,
    create_sessionmaker,
)
from app.embeddings.base import EmbeddingProvider  # noqa: E402
from app.embeddings.fake import FakeEmbeddingProvider  # noqa: E402
from app.storage.local import LocalFileStorage  # noqa: E402


class RecordingDispatcher:
    """Stands in for the Redis queue: records dispatched job ids; can simulate an outage."""

    def __init__(self) -> None:
        self.dispatched: list[uuid.UUID] = []
        self.fail = False

    def dispatch(self, job_id: uuid.UUID, user_id: uuid.UUID) -> None:
        if self.fail:
            raise ConnectionError("simulated queue outage")
        self.dispatched.append(job_id)


def real_pdf(*pages: str) -> bytes:
    document = pymupdf.open()
    for text in pages or ("",):
        page = document.new_page()
        if text:
            page.insert_text((72, 72), text)
    data: bytes = document.tobytes()
    return data


def encrypted_pdf(text: str = "secret") -> bytes:
    document = pymupdf.open()
    document.new_page().insert_text((72, 72), text)
    data: bytes = document.tobytes(
        encryption=pymupdf.PDF_ENCRYPT_AES_256,  # type: ignore[attr-defined]
        user_pw="user-pw",
        owner_pw="owner-pw",
    )
    return data


def real_docx(paragraphs: list[str], table: list[list[str]] | None = None) -> bytes:
    document = docx.Document()
    for text in paragraphs:
        document.add_paragraph(text)
    if table:
        grid = document.add_table(rows=len(table), cols=len(table[0]))
        for r, row in enumerate(table):
            for c, value in enumerate(row):
                grid.cell(r, c).text = value
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def run_async[T](
    test_db_url: str, fn: Callable[[async_sessionmaker[AsyncSession]], Awaitable[T]]
) -> T:
    """Run an async function with a fresh engine on its own event loop (like a separate worker)."""

    async def go() -> T:
        engine = create_engine(test_db_url)
        try:
            return await fn(create_sessionmaker(engine))
        finally:
            await engine.dispose()

    return asyncio.run(go())


def run_maintenance_async[T](
    test_db_url: str, fn: Callable[[async_sessionmaker[AsyncSession]], Awaitable[T]]
) -> T:
    """Run an explicit maintenance operation using the test database admin connection."""

    async def go() -> T:
        engine = create_engine(test_db_url)
        try:
            return await fn(create_maintenance_sessionmaker(engine))
        finally:
            await engine.dispose()

    return asyncio.run(go())


def job_owner_id(test_db_url: str, job_id: uuid.UUID) -> uuid.UUID:
    async def go() -> uuid.UUID:
        engine = create_engine(test_db_url)
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    sql_text("SELECT user_id FROM ingestion_jobs WHERE id = :job_id"),
                    {"job_id": job_id},
                )
                owner_id: uuid.UUID | None = result.scalar_one_or_none()
                if owner_id is None:
                    raise ValueError(f"test ingestion job {job_id} does not exist")
                return owner_id
        finally:
            await engine.dispose()

    return asyncio.run(go())


def run_worker(
    test_db_url: str,
    storage_root: Path,
    job_id: uuid.UUID,
    *,
    embedder: EmbeddingProvider | None = None,
    dispatcher: RecordingDispatcher | None = None,
    max_attempts: int = 3,
    user_id: uuid.UUID | None = None,
) -> str:
    """Run one job (either stage) like a worker would, with the deterministic fake embedder."""
    from app.ingestion.service import process_job

    storage = LocalFileStorage(storage_root)
    provider = embedder or FakeEmbeddingProvider()
    # A real queue message carries the owner; tests may pass it when the job row is already gone.
    owner_id = user_id or job_owner_id(test_db_url, job_id)
    outcome: str = run_async(
        test_db_url,
        lambda sessions: process_job(
            sessions,
            storage,
            job_id,
            user_id=owner_id,
            embedder=provider,
            dispatcher=dispatcher,
            max_attempts=max_attempts,
        ),
    )
    return outcome
