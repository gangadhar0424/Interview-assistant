"""SEC-15: per-user upload quotas, enforced server-side, race-free, with no orphaned files."""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.helpers import make_pdf, register
from tests.test_files import stored_files, temp_files, upload

MIB = 1024 * 1024


@pytest.fixture
def widen_race_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """Delay each quota read so every racing request has read usage before any has committed.

    Without the per-user lock this makes the race certain instead of a matter of timing; with the
    lock the delay happens inside the critical section and the requests simply queue.
    """
    import asyncio

    from app.files import repository

    real = repository.usage_for_user

    async def slow(*args: object, **kwargs: object) -> tuple[int, int]:
        result = await real(*args, **kwargs)  # type: ignore[arg-type]
        await asyncio.sleep(0.25)
        return result

    monkeypatch.setattr(repository, "usage_for_user", slow)


def _files(storage_root: Path) -> int:
    return len(stored_files(storage_root))


def test_defaults_are_documented_values() -> None:
    from app.core.config import Settings

    fields = Settings.model_fields
    assert fields["user_max_resumes"].default == 20
    assert fields["user_max_documents"].default == 100
    assert fields["user_max_storage_mb"].default == 500


def test_count_quota_boundary_exactly_at_limit(
    make_client: Callable[..., TestClient], storage_root: Path
) -> None:
    client = make_client(USER_MAX_RESUMES="2")
    headers = register(client, "alice@example.com")
    assert upload(client, "resumes", headers, "a.pdf", make_pdf(b"1")).status_code == 201
    assert (
        upload(client, "resumes", headers, "b.pdf", make_pdf(b"2")).status_code == 201
    )  # at limit
    r = upload(client, "resumes", headers, "c.pdf", make_pdf(b"3"))  # one over
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "file_quota_exceeded"
    assert _files(storage_root) == 2 and temp_files(storage_root) == []


def test_count_quota_is_per_kind_and_freed_by_delete(
    make_client: Callable[..., TestClient], storage_root: Path
) -> None:
    client = make_client(USER_MAX_RESUMES="1", USER_MAX_DOCUMENTS="1")
    headers = register(client, "alice@example.com")
    first = upload(client, "resumes", headers, "a.pdf", make_pdf(b"1"))
    assert first.status_code == 201
    assert upload(client, "documents", headers, "d.pdf", make_pdf(b"d")).status_code == 201
    assert upload(client, "resumes", headers, "b.pdf", make_pdf(b"2")).status_code == 409
    assert (
        client.delete(f"/api/v1/resumes/{first.json()['id']}", headers=headers).status_code == 204
    )
    assert upload(client, "resumes", headers, "b.pdf", make_pdf(b"2")).status_code == 201


def test_quota_is_per_user(make_client: Callable[..., TestClient]) -> None:
    client = make_client(USER_MAX_RESUMES="1")
    alice = register(client, "alice@example.com")
    bob = register(client, "bob@example.com")
    assert upload(client, "resumes", alice, "a.pdf", make_pdf(b"1")).status_code == 201
    assert upload(client, "resumes", alice, "a2.pdf", make_pdf(b"2")).status_code == 409
    assert upload(client, "resumes", bob, "b.pdf", make_pdf(b"3")).status_code == 201


def test_storage_quota_counts_resumes_and_documents_together(
    make_client: Callable[..., TestClient], storage_root: Path
) -> None:
    client = make_client(USER_MAX_STORAGE_MB="1", DOCUMENT_MAX_UPLOAD_MB="2")
    headers = register(client, "alice@example.com")
    big = make_pdf(b"x" * (600 * 1024))
    assert upload(client, "resumes", headers, "a.pdf", big).status_code == 201
    r = upload(client, "documents", headers, "d.pdf", big)  # 1.2 MB total > 1 MB
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "storage_quota_exceeded"
    assert _files(storage_root) == 1 and temp_files(storage_root) == []  # rejected file removed
    small = make_pdf(b"y" * 1000)
    assert upload(client, "documents", headers, "s.pdf", small).status_code == 201


def test_storage_quota_exact_boundary(
    make_client: Callable[..., TestClient], storage_root: Path
) -> None:
    client = make_client(USER_MAX_STORAGE_MB="1")
    headers = register(client, "alice@example.com")
    exact = make_pdf(b"x" * (MIB - len(make_pdf())))
    assert len(exact) == MIB
    # Use a document (20 MB per-file default) so the storage quota alone decides.
    r = upload(client, "documents", headers, "exact.pdf", exact)
    assert r.status_code == 201  # used + incoming == limit is allowed
    r2 = upload(client, "documents", headers, "one.pdf", make_pdf())
    assert r2.status_code == 409 and r2.json()["error"]["code"] == "storage_quota_exceeded"


def test_concurrent_uploads_cannot_pass_the_count_quota(
    make_client: Callable[..., TestClient], storage_root: Path, widen_race_window: None
) -> None:
    client = make_client(USER_MAX_RESUMES="2")
    headers = register(client, "alice@example.com")

    def one(i: int) -> int:
        return upload(
            client, "resumes", headers, f"c{i}.pdf", make_pdf(str(i).encode())
        ).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(one, range(8)))
    assert codes.count(201) == 2, codes
    assert codes.count(409) == 6, codes
    assert _files(storage_root) == 2 and temp_files(storage_root) == []
    listed = client.get("/api/v1/resumes", headers=headers).json()["items"]
    assert len(listed) == 2


def test_concurrent_uploads_cannot_pass_the_storage_quota(
    make_client: Callable[..., TestClient], storage_root: Path, widen_race_window: None
) -> None:
    client = make_client(USER_MAX_STORAGE_MB="1", DOCUMENT_MAX_UPLOAD_MB="2")
    headers = register(client, "alice@example.com")
    body = make_pdf(b"x" * (400 * 1024))  # two fit in 1 MiB, three do not

    def one(i: int) -> int:
        return upload(client, "documents", headers, f"c{i}.pdf", body + str(i).encode()).status_code

    with ThreadPoolExecutor(max_workers=6) as pool:
        codes = list(pool.map(one, range(6)))
    assert codes.count(201) == 2, codes
    assert _files(storage_root) == 2 and temp_files(storage_root) == []


def test_unauthenticated_upload_is_refused_before_any_quota_or_storage_work(
    client: TestClient, storage_root: Path
) -> None:
    r = client.post("/api/v1/resumes", files={"file": ("a.pdf", make_pdf(), "application/pdf")})
    assert r.status_code == 401
    assert _files(storage_root) == 0 and temp_files(storage_root) == []


def test_failed_commit_after_quota_check_leaves_no_orphan(
    make_client: Callable[..., TestClient],
    storage_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_client(USER_MAX_RESUMES="5")
    headers = register(client, "alice@example.com")
    real = AsyncSession.commit

    async def failing(self: AsyncSession) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(AsyncSession, "commit", failing)
    r = upload(client, "resumes", headers, "a.pdf", make_pdf(b"1"))
    monkeypatch.setattr(AsyncSession, "commit", real)
    assert r.status_code == 500
    assert _files(storage_root) == 0 and temp_files(storage_root) == []
    assert upload(client, "resumes", headers, "a.pdf", make_pdf(b"1")).status_code == 201


def test_quota_message_does_not_leak_other_users_or_paths(
    make_client: Callable[..., TestClient],
) -> None:
    client = make_client(USER_MAX_RESUMES="1")
    headers = register(client, "alice@example.com")
    upload(client, "resumes", headers, "a.pdf", make_pdf(b"1"))
    r = upload(client, "resumes", headers, "b.pdf", make_pdf(b"2"))
    text = r.text
    assert (
        "alice" not in text
        and "app-data" not in text
        and "uploads" not in text.replace("resumes", "")
    )


def test_concurrent_first_uploads_by_one_user_all_succeed_in_storage(tmp_path: Path) -> None:
    """Regression (found by SEC-15): racing first writes used to fail path resolution on Windows."""
    import threading
    import uuid

    from app.storage.local import LocalFileStorage

    for trial in range(15):
        storage = LocalFileStorage(tmp_path / f"root{trial}")
        user = uuid.uuid4()
        errors: list[str] = []

        def one(
            storage: LocalFileStorage = storage,
            user: uuid.UUID = user,
            errors: list[str] = errors,
        ) -> None:
            try:
                pending = storage.begin(user, "documents", 1_000_000)
                pending.write(b"x" * 100)
                pending.commit()
            except Exception as exc:  # noqa: BLE001  (the test reports any failure)
                errors.append(f"{type(exc).__name__}: {exc}")

        threads = [threading.Thread(target=one) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == [], errors


def test_no_database_connection_is_held_while_the_upload_body_is_read(
    make_client: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    """SEC-35: a slow client must not pin a pooled connection while its body trickles in.

    Found by the Phase F slow-client probe: 25 stalled uploads from one account (more than
    `pool_size + max_overflow`) made every other user's request wait for the pool and fail 503.
    """
    from starlette.requests import Request

    client = make_client()
    headers = register(client, "alice@example.com")
    engine = client.app.state.engine  # type: ignore[attr-defined]
    seen: list[int] = []
    real_form = Request.form

    def spy(self: Request, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        if self.url.path.endswith("/resumes"):
            seen.append(engine.sync_engine.pool.checkedout())
        return real_form(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Request, "form", spy)
    assert upload(client, "resumes", headers, "a.pdf", make_pdf(b"1")).status_code == 201
    assert seen == [0], f"connections checked out while reading the body: {seen}"


def test_authentication_leaves_no_database_connection_checked_out(
    make_client: Callable[..., TestClient],
) -> None:
    """SEC-35 (root cause): a handler must not inherit a pooled connection from authentication.

    `get_current_user` reads the user row, which starts a transaction. If it stays open, every
    authenticated request pins one connection for its whole duration (slow request bodies, long
    answer streams, slow provider calls), so `pool_size + max_overflow` such requests starve
    everyone else. This route runs after authentication and reports what is checked out.
    """
    from app.auth.deps import CurrentUser

    client = make_client()
    headers = register(client, "alice@example.com")
    engine = client.app.state.engine  # type: ignore[attr-defined]

    async def probe(current: CurrentUser) -> dict[str, int]:
        return {"checked_out": engine.sync_engine.pool.checkedout()}

    client.app.add_api_route("/api/v1/_probe_auth_pool", probe, methods=["GET"])  # type: ignore[attr-defined]
    r = client.get("/api/v1/_probe_auth_pool", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json() == {"checked_out": 0}
