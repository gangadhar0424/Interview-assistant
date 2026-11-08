"""Phase 4 closure checks: completion atomicity, delete-vs-worker race, recovery safety."""

import asyncio
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.ingestion import extractors
from app.ingestion.service import recover_stale_jobs
from app.workers import recovery
from tests.conftest import RunSql
from tests.helpers import RecordingDispatcher, real_pdf, run_async
from tests.test_files import stored_files, upload
from tests.test_ingestion_jobs import (  # noqa: F401  (fixtures + helpers reused as-is)
    Env,
    alice,
    bob,
    dispatcher_of,
    env,
    job_row,
    texts,
)

JOBS_SNAPSHOT = (
    "SELECT id, status, attempt, locked_at, updated_at, finished_at, error_code "
    "FROM ingestion_jobs ORDER BY id"
)


async def _sweep(sessions: Any, dispatcher: RecordingDispatcher) -> Any:
    return await recover_stale_jobs(
        sessions,
        dispatcher,
        max_attempts=3,
        processing_timeout_seconds=900,
        pending_redispatch_seconds=120,
    )


# ----------------------------------------------------- 1. atomic completion


def test_job_cannot_become_completed_if_persisting_the_text_fails(
    client: TestClient,
    alice: dict[str, str],  # noqa: F811
    run_sql: RunSql,
    env: Env,  # noqa: F811
    test_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = upload(client, "resumes", alice, "cv.pdf", real_pdf("Atomic")).json()
    job_id = job_row(run_sql, created["id"])["id"]

    def failing_insert(*_a: object, **_k: object) -> None:
        # Raised AFTER the job UPDATE ran inside the completion transaction.
        raise OperationalError("INSERT", {}, Exception("simulated persistence failure"))

    async def no_fail_handler(*_a: object, **_k: object) -> None:
        return None  # prevent the normal failure path so we can observe the raw rollback

    monkeypatch.setattr("app.ingestion.service.insert", failing_insert)
    monkeypatch.setattr("app.ingestion.service._fail", no_fail_handler)
    env.work(job_id)  # the failure handler is stubbed, so only the rollback is observable
    monkeypatch.undo()

    job = job_row(run_sql, created["id"])
    assert job["status"] == "processing"  # the 'completed' UPDATE was rolled back with the text
    assert job["finished_at"] is None
    assert texts(run_sql) == []

    # With the normal failure path the job ends up failed (never completed) and is recoverable.
    monkeypatch.setattr("app.ingestion.service.insert", failing_insert)
    run_sql("UPDATE ingestion_jobs SET status='pending'")
    assert env.work(job_id) == "failed"
    monkeypatch.undo()
    assert job_row(run_sql, created["id"])["status"] == "failed"
    assert job_row(run_sql, created["id"])["error_code"] == "internal_error"
    assert texts(run_sql) == []
    retry = client.post(f"/api/v1/resumes/{created['id']}/retry", headers=alice)
    assert retry.status_code == 202
    assert env.work(job_id) == "completed" and len(texts(run_sql)) == 1


def test_extracted_text_exists_exactly_for_completed_jobs_across_all_paths(
    client: TestClient,
    alice: dict[str, str],  # noqa: F811
    run_sql: RunSql,
    env: Env,  # noqa: F811
    test_db_url: str,
    storage_root: Any,
) -> None:
    def new(name: str, data: bytes) -> str:
        return str(upload(client, "documents", alice, name, data).json()["id"])

    completed = new("ok.pdf", real_pdf("Completed"))
    failed = new("bad.pdf", b"%PDF-1.4\ngarbage\n%%EOF\n")
    pending = new("pending.pdf", real_pdf("Pending"))
    timed_out = new("slow.pdf", real_pdf("Slow"))
    retried = new("retry.pdf", real_pdf("Retried"))
    deleted = new("gone.pdf", real_pdf("Gone"))

    env.work(job_row(run_sql, completed)["id"])
    env.work(job_row(run_sql, failed)["id"])
    env.work(job_row(run_sql, deleted)["id"])  # completed, then deleted below
    # A stuck worker whose attempts are exhausted is failed by recovery.
    run_sql(
        "UPDATE ingestion_jobs SET status='processing', attempt=3, "
        "locked_at = now() - interval '2 hours' WHERE source_id = :i",
        i=uuid.UUID(timed_out),
    )
    run_async(test_db_url, lambda s: _sweep(s, RecordingDispatcher()))
    # A failed job is retried and then completes.
    run_sql("UPDATE ingestion_jobs SET status='failed' WHERE source_id = :i", i=uuid.UUID(retried))
    assert client.post(f"/api/v1/documents/{retried}/retry", headers=alice).status_code == 202
    env.work(job_row(run_sql, retried)["id"])
    assert client.delete(f"/api/v1/documents/{deleted}", headers=alice).status_code == 204

    statuses = {
        str(r["source_id"]): r["status"]
        for r in run_sql("SELECT * FROM ingestion_jobs WHERE stage = 'extract'")
    }
    assert statuses == {
        completed: "completed",
        failed: "failed",
        pending: "pending",
        timed_out: "failed",
        retried: "completed",
    }
    with_text = {str(r["source_id"]) for r in texts(run_sql)}
    assert with_text == {sid for sid, st in statuses.items() if st == "completed"}


# ------------------------------------------------ 2. delete during ingestion


def test_api_delete_while_a_worker_is_extracting_leaves_nothing_behind(
    client: TestClient,
    alice: dict[str, str],  # noqa: F811
    run_sql: RunSql,
    env: Env,  # noqa: F811
    test_db_url: str,
    storage_root: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = upload(client, "resumes", alice, "cv.pdf", real_pdf("Racing delete")).json()
    job_id = job_row(run_sql, created["id"])["id"]
    real = extractors.PdfExtractor.extract
    seen: dict[str, Any] = {}

    def delete_via_api_midway(self: extractors.PdfExtractor, data: bytes) -> str:
        seen["status_during"] = job_row(run_sql, created["id"])["status"]
        seen["delete"] = client.delete(f"/api/v1/resumes/{created['id']}", headers=alice)
        return real(self, data)

    monkeypatch.setattr(extractors.PdfExtractor, "extract", delete_via_api_midway)
    assert env.work(job_id) == "discarded"
    monkeypatch.undo()

    assert seen["status_during"] == "processing"
    assert seen["delete"].status_code == 204
    # Final state is fully consistent: no job, no text, no file record, no stored file.
    assert run_sql("SELECT count(*) AS n FROM ingestion_jobs")[0]["n"] == 0
    assert run_sql("SELECT count(*) AS n FROM resumes")[0]["n"] == 0
    assert texts(run_sql) == [] and stored_files(storage_root) == []

    # Neither a duplicate delivery nor the recovery sweep resurrects the deleted work.
    assert env.work(job_id) == "skipped"
    dispatcher = RecordingDispatcher()
    report = run_async(test_db_url, lambda s: _sweep(s, dispatcher))
    assert (report.failed, report.requeued, report.dispatched) == ([], [], [])
    assert dispatcher.dispatched == [] and texts(run_sql) == []


# ------------------------------------------------------ 3. recovery safety


def test_recovery_dry_run_changes_nothing_and_completed_jobs_are_never_reprocessed(
    client: TestClient,
    alice: dict[str, str],  # noqa: F811
    run_sql: RunSql,
    env: Env,  # noqa: F811
    test_db_url: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    done = upload(client, "resumes", alice, "done.pdf", real_pdf("Done")).json()
    lost = upload(client, "resumes", alice, "lost.pdf", real_pdf("Lost")).json()
    stuck = upload(client, "resumes", alice, "stuck.pdf", real_pdf("Stuck")).json()
    env.work_file(done["id"])  # fully completed: extract and embed stages
    run_sql(
        "UPDATE ingestion_jobs SET updated_at = now() - interval '1 day' "
        "WHERE status IN ('completed', 'pending')"
    )
    run_sql(
        "UPDATE ingestion_jobs SET status='processing', attempt=1, "
        "locked_at = now() - interval '2 hours' WHERE source_id = :i",
        i=uuid.UUID(stuck["id"]),
    )
    before = run_sql(JOBS_SNAPSHOT)
    text_before = texts(run_sql)

    capsys.readouterr()
    assert asyncio.run(recovery.main(dry_run=True)) == 0
    output = capsys.readouterr().out
    assert "2 stuck job(s)" in output  # the lost pending job and the stuck processing job
    assert str(job_row(run_sql, done["id"])["id"]) not in output  # completed jobs never listed
    assert run_sql(JOBS_SNAPSHOT) == before and texts(run_sql) == text_before  # no mutation

    dispatcher = RecordingDispatcher()
    report = run_async(test_db_url, lambda s: _sweep(s, dispatcher))
    done_id = job_row(run_sql, done["id"])["id"]
    assert done_id not in dispatcher.dispatched and done_id not in report.requeued
    assert sorted(dispatcher.dispatched) == sorted(
        [job_row(run_sql, lost["id"])["id"], job_row(run_sql, stuck["id"])["id"]]
    )
    assert job_row(run_sql, done["id"])["status"] == "completed"
    assert len(texts(run_sql)) == 1  # the completed file's text is untouched

    again = run_async(test_db_url, lambda s: _sweep(s, dispatcher))
    assert (again.failed, again.requeued, again.dispatched) == ([], [], [])  # idempotent
