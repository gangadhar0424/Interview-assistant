"""Phase 4: ingestion job lifecycle, failure handling, dispatch/recovery and user isolation."""

import asyncio
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import get_settings
from app.db.models import IngestionJob
from app.embeddings.fake import FakeEmbeddingProvider
from app.ingestion import repository as ingestion_repository
from app.ingestion.service import find_stuck_jobs, process_job, recover_stale_jobs
from app.storage.local import LocalFileStorage
from tests.conftest import RunSql
from tests.helpers import (
    RecordingDispatcher,
    make_docx,
    make_pdf,
    real_docx,
    real_pdf,
    register,
    run_async,
    run_worker,
)
from tests.test_files import stored_files, upload

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@pytest.fixture
def alice(client: TestClient) -> dict[str, str]:
    return register(client, "alice@example.com")


@pytest.fixture
def bob(client: TestClient) -> dict[str, str]:
    return register(client, "bob@example.com")


def dispatcher_of(client: TestClient) -> RecordingDispatcher:
    dispatcher: RecordingDispatcher = client.app.state.dispatcher  # type: ignore[attr-defined]
    return dispatcher


def job_row(run_sql: RunSql, file_id: str, stage: str = "extract") -> dict[str, Any]:
    return run_sql(
        "SELECT * FROM ingestion_jobs WHERE source_id = :i AND stage = :s",
        i=uuid.UUID(file_id),
        s=stage,
    )[0]


def texts(run_sql: RunSql) -> list[dict[str, Any]]:
    return run_sql("SELECT * FROM extracted_texts")


def status_of(
    client: TestClient, kind: str, headers: dict[str, str], file_id: str
) -> dict[str, Any]:
    body: dict[str, Any] = client.get(f"/api/v1/{kind}/{file_id}", headers=headers).json()
    return body


class Env:
    """Bundle for running the worker against the test database/storage."""

    def __init__(self, url: str, root: Path) -> None:
        self.url, self.root = url, root

    def work(self, job_id: Any, **kwargs: Any) -> str:
        return run_worker(self.url, self.root, uuid.UUID(str(job_id)), **kwargs)

    def work_file(self, file_id: str) -> None:
        """Run the whole pipeline for one file: extract stage, then its embed stage."""

        async def job_ids(sessions: Any) -> dict[str, uuid.UUID]:
            async with sessions() as session:
                rows = (
                    await session.execute(
                        select(IngestionJob.stage, IngestionJob.id).where(
                            IngestionJob.source_id == uuid.UUID(file_id)
                        )
                    )
                ).all()
            return {r.stage: r.id for r in rows}

        self.work(run_async(self.url, job_ids)["extract"])
        self.work(run_async(self.url, job_ids)["embed"])


@pytest.fixture
def env(test_db_url: str, storage_root: Path) -> Env:
    return Env(test_db_url, storage_root)


# ------------------------------------------------------- upload creates state


@pytest.mark.parametrize("kind", ["resumes", "documents"])
def test_upload_creates_pending_job_and_dispatches_after_commit(
    client: TestClient, alice: dict[str, str], run_sql: RunSql, kind: str
) -> None:
    created = upload(client, kind, alice, "cv.pdf", real_pdf("Hello")).json()
    assert created["ingestion"]["status"] == "pending"
    assert created["ingestion"]["error_code"] is None

    job = job_row(run_sql, created["id"])
    assert job["status"] == "pending" and job["attempt"] == 0
    assert job["source_type"] == kind[:-1]
    assert job["user_id"] == run_sql("SELECT id FROM users")[0]["id"]
    assert dispatcher_of(client).dispatched == [job["id"]]
    assert texts(run_sql) == []  # nothing is extracted inside the request


def test_extraction_does_not_run_in_the_request_path(
    client: TestClient,
    alice: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*_a: object, **_k: object) -> str:
        raise AssertionError("extraction must not run during upload")

    monkeypatch.setattr("app.ingestion.extractors.PdfExtractor.extract", boom)
    assert upload(client, "resumes", alice, "cv.pdf", real_pdf("x")).status_code == 201


# ------------------------------------------------------------- happy paths


def test_pdf_end_to_end_pending_processing_completed(
    client: TestClient,
    alice: dict[str, str],
    run_sql: RunSql,
    env: Env,
    storage_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = real_pdf("Jane Doe   Senior\tEngineer", "Second page")
    created = upload(client, "resumes", alice, "cv.pdf", content).json()
    job_id = job_row(run_sql, created["id"])["id"]
    (stored,) = stored_files(storage_root)
    original = stored.read_bytes()

    seen: dict[str, Any] = {}
    from app.ingestion import extractors

    real = extractors.PdfExtractor.extract

    def spy(self: extractors.PdfExtractor, data: bytes) -> str:
        seen["row"] = job_row(run_sql, created["id"])  # state while the worker is extracting
        return real(self, data)

    monkeypatch.setattr(extractors.PdfExtractor, "extract", spy)
    assert env.work(job_id) == "completed"

    assert seen["row"]["status"] == "processing"
    assert seen["row"]["attempt"] == 1 and seen["row"]["locked_at"] is not None
    done = job_row(run_sql, created["id"])
    assert done["status"] == "completed"
    assert done["locked_at"] is None and done["finished_at"] is not None
    assert done["error_code"] is None

    (text,) = texts(run_sql)
    assert text["text"].startswith("Jane Doe Senior Engineer")  # whitespace normalised
    assert "Second page" in text["text"] and text["char_count"] == len(text["text"])
    assert text["extractor"] == "pymupdf" and text["user_id"] == done["user_id"]
    assert stored.read_bytes() == original  # original upload is untouched
    assert status_of(client, "resumes", alice, created["id"])["ingestion"]["status"] == "completed"


def test_docx_and_txt_end_to_end(
    client: TestClient, alice: dict[str, str], run_sql: RunSql, env: Env
) -> None:
    docx_file = upload(
        client,
        "documents",
        alice,
        "notes.docx",
        real_docx(["Project Alpha", "Led a team."], table=[["Role", "Lead"]]),
        DOCX_MIME,
    ).json()
    txt_file = upload(
        client,
        "documents",
        alice,
        "n.txt",
        b"Line1\r\n\r\n\r\n\r\nLine2  end\r\n",
        "text/plain",
    ).json()
    for created in (docx_file, txt_file):
        assert env.work(job_row(run_sql, created["id"])["id"]) == "completed"
    by_source = {r["source_id"]: r for r in texts(run_sql)}
    docx_text = by_source[uuid.UUID(docx_file["id"])]
    txt_text = by_source[uuid.UUID(txt_file["id"])]
    assert docx_text["extractor"] == "python-docx"
    assert "Project Alpha" in docx_text["text"] and "Role | Lead" in docx_text["text"]
    assert txt_text["text"] == "Line1\n\nLine2 end" and txt_text["extractor"] == "text"


# ------------------------------------------------------------------ failures


@pytest.mark.parametrize(
    ("name", "data", "mime", "code"),
    [
        ("scan.pdf", real_pdf(""), "application/pdf", "no_text"),
        ("blank.txt", b"  \n\t \n", "text/plain", "no_text"),
        ("bad.pdf", b"%PDF-1.4\ngarbage garbage\n%%EOF\n", "application/pdf", "unreadable_file"),
        ("bad.docx", make_docx(), DOCX_MIME, "unreadable_file"),
    ],
)
def test_unextractable_files_fail_safely(
    client: TestClient,
    alice: dict[str, str],
    run_sql: RunSql,
    env: Env,
    name: str,
    data: bytes,
    mime: str,
    code: str,
) -> None:
    created = upload(client, "documents", alice, name, data, mime)
    assert created.status_code == 201, created.text
    file_id = created.json()["id"]
    assert env.work(job_row(run_sql, file_id)["id"]) == "failed"

    job = job_row(run_sql, file_id)
    assert job["status"] == "failed" and job["error_code"] == code
    assert job["locked_at"] is None and job["finished_at"] is not None
    assert texts(run_sql) == []
    shown = status_of(client, "documents", alice, file_id)["ingestion"]
    assert shown["status"] == "failed" and shown["error_code"] == code and shown["error_message"]


def test_encrypted_pdf_fails_safely(
    client: TestClient, alice: dict[str, str], run_sql: RunSql, env: Env
) -> None:
    from tests.helpers import encrypted_pdf

    created = upload(client, "resumes", alice, "locked.pdf", encrypted_pdf()).json()
    assert env.work(job_row(run_sql, created["id"])["id"]) == "failed"
    assert job_row(run_sql, created["id"])["error_code"] == "encrypted_file"


def test_text_decoding_failure_after_upload_is_recorded(
    client: TestClient, alice: dict[str, str], run_sql: RunSql, env: Env, storage_root: Path
) -> None:
    created = upload(client, "documents", alice, "n.txt", b"fine text", "text/plain").json()
    (stored,) = stored_files(storage_root)
    stored.write_bytes(b"\xff\xfe\xfa not utf-8")  # corrupted after upload validation
    assert env.work(job_row(run_sql, created["id"])["id"]) == "failed"
    assert job_row(run_sql, created["id"])["error_code"] == "invalid_encoding"


def test_missing_stored_file_fails_the_job(
    client: TestClient, alice: dict[str, str], run_sql: RunSql, env: Env, storage_root: Path
) -> None:
    created = upload(client, "resumes", alice, "cv.pdf", real_pdf("x")).json()
    (stored,) = stored_files(storage_root)
    stored.unlink()
    assert env.work(job_row(run_sql, created["id"])["id"]) == "failed"
    job = job_row(run_sql, created["id"])
    assert job["error_code"] == "file_missing"
    assert str(storage_root) not in str(job["error_message"])


def test_unexpected_worker_exception_is_recorded_without_leaking_details(
    client: TestClient,
    alice: dict[str, str],
    run_sql: RunSql,
    env: Env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode(*_a: object, **_k: object) -> str:
        raise RuntimeError("SECRET-DETAIL at C:\\internal\\path with document text")

    monkeypatch.setattr("app.ingestion.extractors.PdfExtractor.extract", explode)
    created = upload(client, "resumes", alice, "cv.pdf", real_pdf("x")).json()
    assert env.work(job_row(run_sql, created["id"])["id"]) == "failed"

    job = job_row(run_sql, created["id"])
    assert job["status"] == "failed" and job["error_code"] == "internal_error"
    stored_text = f"{job['error_code']} {job['error_message']}"
    assert (
        "SECRET-DETAIL" not in stored_text and "internal" not in str(job["error_message"]).lower()
    )
    api = client.get(f"/api/v1/resumes/{created['id']}", headers=alice).text
    assert "SECRET-DETAIL" not in api and "C:\\" not in api
    assert job["status"] != "processing"  # never left stuck


# -------------------------------------------------- idempotency and duplicates


def test_duplicate_processing_creates_no_duplicate_output(
    client: TestClient, alice: dict[str, str], run_sql: RunSql, env: Env
) -> None:
    created = upload(client, "resumes", alice, "cv.pdf", real_pdf("Only once")).json()
    job_id = job_row(run_sql, created["id"])["id"]
    assert env.work(job_id) == "completed"
    assert env.work(job_id) == "skipped"  # already completed: not processed again
    assert len(texts(run_sql)) == 1
    assert job_row(run_sql, created["id"])["attempt"] == 1


def test_concurrent_workers_claim_a_job_only_once(
    client: TestClient,
    alice: dict[str, str],
    run_sql: RunSql,
    test_db_url: str,
    storage_root: Path,
) -> None:
    created = upload(client, "resumes", alice, "cv.pdf", real_pdf("Race")).json()
    job_id = job_row(run_sql, created["id"])["id"]
    storage = LocalFileStorage(storage_root)

    async def both(sessions: Any) -> list[str]:
        return list(
            await asyncio.gather(
                process_job(sessions, storage, job_id, embedder=FakeEmbeddingProvider()),
                process_job(sessions, storage, job_id, embedder=FakeEmbeddingProvider()),
            )
        )

    outcomes = run_async(test_db_url, both)
    assert sorted(outcomes) == ["completed", "skipped"]
    assert len(texts(run_sql)) == 1
    assert job_row(run_sql, created["id"])["attempt"] == 1


def test_reprocessing_replaces_text_instead_of_duplicating_it(
    client: TestClient, alice: dict[str, str], run_sql: RunSql, env: Env
) -> None:
    created = upload(client, "documents", alice, "n.txt", b"first version", "text/plain").json()
    job_id = job_row(run_sql, created["id"])["id"]
    env.work(job_id)
    run_sql("UPDATE ingestion_jobs SET status = 'pending'")  # e.g. a reprocess after recovery
    run_sql("UPDATE extracted_texts SET text = 'stale'")
    assert env.work(job_id) == "completed"
    (row,) = texts(run_sql)
    assert row["text"] == "first version"


def test_job_deleted_while_processing_stores_nothing(
    client: TestClient,
    alice: dict[str, str],
    run_sql: RunSql,
    env: Env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = upload(client, "resumes", alice, "cv.pdf", real_pdf("Doomed")).json()
    from app.ingestion import extractors

    real = extractors.PdfExtractor.extract

    def delete_midway(self: extractors.PdfExtractor, data: bytes) -> str:
        run_sql("DELETE FROM ingestion_jobs")  # the user deleted the file while we work
        return real(self, data)

    monkeypatch.setattr(extractors.PdfExtractor, "extract", delete_midway)
    assert env.work(job_row(run_sql, created["id"])["id"]) == "discarded"
    assert texts(run_sql) == []


def test_deleting_a_file_removes_its_job_and_text_and_skips_the_worker(
    client: TestClient, alice: dict[str, str], run_sql: RunSql, env: Env
) -> None:
    done = upload(client, "resumes", alice, "a.pdf", real_pdf("Done")).json()
    env.work(job_row(run_sql, done["id"])["id"])
    pending = upload(client, "resumes", alice, "b.pdf", real_pdf("Pending")).json()
    pending_job = job_row(run_sql, pending["id"])["id"]
    assert len(texts(run_sql)) == 1

    assert client.delete(f"/api/v1/resumes/{done['id']}", headers=alice).status_code == 204
    assert client.delete(f"/api/v1/resumes/{pending['id']}", headers=alice).status_code == 204
    assert run_sql("SELECT count(*) AS n FROM ingestion_jobs")[0]["n"] == 0
    assert texts(run_sql) == []
    assert env.work(pending_job) == "skipped"  # queued message for a deleted file is harmless


# ----------------------------------------------------------------------- retry


def test_retry_failed_job_is_safe_and_not_duplicated(
    client: TestClient, alice: dict[str, str], run_sql: RunSql, env: Env, storage_root: Path
) -> None:
    created = upload(client, "documents", alice, "n.txt", b"ok text", "text/plain").json()
    (stored,) = stored_files(storage_root)
    stored.write_bytes(b"\xff\xfe broken")
    job_id = job_row(run_sql, created["id"])["id"]
    assert env.work(job_id) == "failed"
    dispatcher_of(client).dispatched.clear()

    stored.write_bytes(b"repaired text")
    r = client.post(f"/api/v1/documents/{created['id']}/retry", headers=alice)
    assert r.status_code == 202 and r.json()["ingestion"]["status"] == "pending"
    assert r.json()["ingestion"]["error_code"] is None
    assert dispatcher_of(client).dispatched == [job_id]
    assert job_row(run_sql, created["id"])["attempt"] == 0

    again = client.post(f"/api/v1/documents/{created['id']}/retry", headers=alice)
    assert again.status_code == 409 and again.json()["error"]["code"] == "not_retryable"
    assert dispatcher_of(client).dispatched == [job_id]  # no second dispatch

    assert env.work(job_id) == "completed"
    assert texts(run_sql)[0]["text"] == "repaired text"
    done_retry = client.post(f"/api/v1/documents/{created['id']}/retry", headers=alice)
    assert done_retry.status_code == 409  # completed files are not retryable


# -------------------------------------------------------- dispatch and recovery


def test_queue_outage_at_upload_leaves_a_recoverable_pending_job(
    client: TestClient, alice: dict[str, str], run_sql: RunSql, env: Env, test_db_url: str
) -> None:
    dispatcher = dispatcher_of(client)
    dispatcher.fail = True
    created = upload(client, "resumes", alice, "cv.pdf", real_pdf("Survives outage"))
    assert created.status_code == 201  # the upload itself succeeds
    file_id = created.json()["id"]
    job = job_row(run_sql, file_id)
    assert job["status"] == "pending" and dispatcher.dispatched == []

    settings = get_settings()

    async def sweep(sessions: Any) -> Any:
        return await recover_stale_jobs(
            sessions,
            dispatcher,
            max_attempts=settings.ingestion_max_attempts,
            processing_timeout_seconds=900,
            pending_redispatch_seconds=120,
        )

    # Fresh pending job: not old enough to be considered lost.
    dispatcher.fail = False
    assert run_async(test_db_url, sweep).dispatched == []

    run_sql("UPDATE ingestion_jobs SET updated_at = now() - interval '10 minutes'")
    report = run_async(test_db_url, sweep)
    assert report.stale_pending == [job["id"]] and report.dispatched == [job["id"]]
    assert dispatcher.dispatched == [job["id"]]
    assert env.work(job["id"]) == "completed"
    assert run_async(test_db_url, sweep).dispatched == []  # idempotent: nothing left to do


def test_sweep_with_queue_still_down_keeps_the_job_pending(
    client: TestClient, alice: dict[str, str], run_sql: RunSql, test_db_url: str
) -> None:
    dispatcher = dispatcher_of(client)
    upload(client, "resumes", alice, "cv.pdf", real_pdf("x"))
    run_sql("UPDATE ingestion_jobs SET updated_at = now() - interval '10 minutes'")
    dispatcher.fail = True

    async def sweep(sessions: Any) -> Any:
        return await recover_stale_jobs(
            sessions,
            dispatcher,
            max_attempts=3,
            processing_timeout_seconds=900,
            pending_redispatch_seconds=120,
        )

    report = run_async(test_db_url, sweep)
    assert report.dispatched == [] and len(report.dispatch_failed) == 1
    assert run_sql("SELECT status FROM ingestion_jobs")[0]["status"] == "pending"


def test_stuck_processing_jobs_are_identifiable_and_recovered_deterministically(
    client: TestClient, alice: dict[str, str], run_sql: RunSql, test_db_url: str
) -> None:
    dispatcher = dispatcher_of(client)
    ids = [
        upload(client, "documents", alice, f"d{i}.pdf", real_pdf(f"d{i}")).json() for i in range(4)
    ]
    jobs = [job_row(run_sql, i["id"])["id"] for i in ids]
    dispatcher.dispatched.clear()
    # 0: stuck, attempts left; 1: stuck, attempts exhausted; 2: processing but recent; 3: pending.
    run_sql(
        "UPDATE ingestion_jobs SET status='processing', attempt=1, "
        "locked_at = now() - interval '2 hours' WHERE id = :i",
        i=jobs[0],
    )
    run_sql(
        "UPDATE ingestion_jobs SET status='processing', attempt=3, "
        "locked_at = now() - interval '3 hours' WHERE id = :i",
        i=jobs[1],
    )
    run_sql(
        "UPDATE ingestion_jobs SET status='processing', attempt=1, locked_at = now() WHERE id = :i",
        i=jobs[2],
    )

    async def identify(sessions: Any) -> list[Any]:
        async with sessions() as session:
            found = await find_stuck_jobs(
                session, processing_timeout_seconds=900, pending_redispatch_seconds=120
            )
        return [j.id for j in found]

    assert run_async(test_db_url, identify) == [jobs[0], jobs[1]]  # oldest first; others excluded

    async def sweep(sessions: Any) -> Any:
        return await recover_stale_jobs(
            sessions,
            dispatcher,
            max_attempts=3,
            processing_timeout_seconds=900,
            pending_redispatch_seconds=120,
        )

    report = run_async(test_db_url, sweep)
    assert report.failed == [jobs[1]] and report.requeued == [jobs[0]]
    assert dispatcher.dispatched == [jobs[0]]
    states = {r["id"]: r for r in run_sql("SELECT * FROM ingestion_jobs")}
    assert states[jobs[0]]["status"] == "pending" and states[jobs[0]]["locked_at"] is None
    assert states[jobs[1]]["status"] == "failed" and states[jobs[1]]["error_code"] == "timeout"
    assert states[jobs[2]]["status"] == "processing"  # recent: untouched
    assert states[jobs[3]]["status"] == "pending"  # fresh pending: untouched

    again = run_async(test_db_url, sweep)  # deterministic and idempotent
    assert (again.failed, again.requeued, again.dispatched) == ([], [], [])


def test_worker_task_uses_settings_for_database_and_storage(
    client: TestClient,
    alice: dict[str, str],
    run_sql: RunSql,
) -> None:
    from app.workers.tasks import ingest_job

    created = upload(client, "resumes", alice, "cv.pdf", real_pdf("Via actor")).json()
    job = job_row(run_sql, created["id"])
    ingest_job.fn(str(job["id"]))  # what the Dramatiq worker runs, minus the broker
    assert job_row(run_sql, created["id"])["status"] == "completed"
    assert "Via actor" in texts(run_sql)[0]["text"]


# ------------------------------------------------------------------ isolation


def test_users_cannot_see_or_retry_each_others_ingestion(
    client: TestClient,
    alice: dict[str, str],
    bob: dict[str, str],
    run_sql: RunSql,
    env: Env,
    test_db_url: str,
) -> None:
    secret = "ALICE-UNIQUE-CONTENT-MARKER"
    created = upload(client, "resumes", alice, "cv.pdf", real_pdf(secret)).json()
    job_id = job_row(run_sql, created["id"])["id"]
    env.work(job_id)
    failed = upload(client, "resumes", alice, "bad.pdf", b"%PDF-1.4\njunk\n%%EOF\n").json()
    env.work(job_row(run_sql, failed["id"])["id"])
    assert job_row(run_sql, failed["id"])["status"] == "failed"

    # Bob cannot read status, retry, or list Alice's files.
    assert client.get(f"/api/v1/resumes/{created['id']}", headers=bob).status_code == 404
    r = client.post(f"/api/v1/resumes/{failed['id']}/retry", headers=bob)
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
    assert job_row(run_sql, failed["id"])["status"] == "failed"  # untouched by Bob's attempt
    assert client.get("/api/v1/resumes", headers=bob).json()["items"] == []

    # Extracted text is never returned by any API, for anyone.
    for response in (
        client.get("/api/v1/resumes", headers=alice),
        client.get(f"/api/v1/resumes/{created['id']}", headers=alice),
        client.get(f"/api/v1/resumes/{created['id']}/file", headers=bob),
        client.get(f"/api/v1/resumes/{created['id']}", headers=bob),
    ):
        assert secret not in response.text or response.content.startswith(b"%PDF")
    assert secret not in client.get(f"/api/v1/resumes/{created['id']}", headers=alice).text

    # Internal accessor is user-scoped: Bob's identity yields nothing, Alice's yields the text.
    alice_id = run_sql("SELECT id FROM users WHERE email='alice@example.com'")[0]["id"]
    bob_id = run_sql("SELECT id FROM users WHERE email='bob@example.com'")[0]["id"]

    async def fetch(sessions: Any) -> tuple[Any, Any]:
        async with sessions() as session:
            src = uuid.UUID(created["id"])
            mine = await ingestion_repository.get_extracted_text(session, alice_id, "resume", src)
            theirs = await ingestion_repository.get_extracted_text(session, bob_id, "resume", src)
            return mine, theirs

    mine, theirs = run_async(test_db_url, fetch)
    assert theirs is None and mine is not None and secret in mine.text


def test_ingestion_endpoints_require_authentication(client: TestClient) -> None:
    some = "00000000-0000-0000-0000-000000000000"
    for kind in ("resumes", "documents"):
        assert client.get(f"/api/v1/{kind}/{some}").status_code == 401
        assert client.post(f"/api/v1/{kind}/{some}/retry").status_code == 401


def test_unknown_file_status_and_retry_are_404(client: TestClient, alice: dict[str, str]) -> None:
    some = "00000000-0000-4000-8000-000000000000"
    assert client.get(f"/api/v1/resumes/{some}", headers=alice).status_code == 404
    assert client.post(f"/api/v1/resumes/{some}/retry", headers=alice).status_code == 404


def test_phase3_pdf_helper_still_uploads_and_is_pending(
    client: TestClient, alice: dict[str, str]
) -> None:
    # Files with a valid signature but fake content are accepted at upload (extraction decides).
    r = upload(client, "resumes", alice, "cv.pdf", make_pdf())
    assert r.status_code == 201 and r.json()["ingestion"]["status"] == "pending"


def test_slow_worker_cannot_overwrite_a_job_that_recovery_already_failed(
    client: TestClient,
    alice: dict[str, str],
    run_sql: RunSql,
    env: Env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = upload(client, "resumes", alice, "cv.pdf", real_pdf("Slow worker")).json()
    from app.ingestion import extractors

    real = extractors.PdfExtractor.extract

    def recovered_meanwhile(self: extractors.PdfExtractor, data: bytes) -> str:
        # While this (slow) worker runs, the sweep gives up on the job.
        run_sql("UPDATE ingestion_jobs SET status='failed', error_code='timeout', locked_at=NULL")
        return real(self, data)

    monkeypatch.setattr(extractors.PdfExtractor, "extract", recovered_meanwhile)
    assert env.work(job_row(run_sql, created["id"])["id"]) == "discarded"
    job = job_row(run_sql, created["id"])
    assert job["status"] == "failed" and job["error_code"] == "timeout"  # not overwritten
    assert texts(run_sql) == []


def test_ingestion_repository_reads_are_scoped_to_the_user(
    client: TestClient,
    alice: dict[str, str],
    bob: dict[str, str],
    run_sql: RunSql,
    test_db_url: str,
) -> None:
    created = upload(client, "resumes", alice, "cv.pdf", real_pdf("x")).json()
    source = uuid.UUID(created["id"])
    alice_id = run_sql("SELECT id FROM users WHERE email='alice@example.com'")[0]["id"]
    bob_id = run_sql("SELECT id FROM users WHERE email='bob@example.com'")[0]["id"]

    async def look(sessions: Any) -> tuple[Any, Any, Any, Any]:
        async with sessions() as session:
            return (
                await ingestion_repository.get_job_for_source(session, alice_id, "resume", source),
                await ingestion_repository.get_job_for_source(session, bob_id, "resume", source),
                await ingestion_repository.jobs_for_sources(session, alice_id, "resume", [source]),
                await ingestion_repository.jobs_for_sources(session, bob_id, "resume", [source]),
            )

    mine, theirs, mine_many, theirs_many = run_async(test_db_url, look)
    assert mine is not None and theirs is None
    assert list(mine_many) == [source] and theirs_many == {}
