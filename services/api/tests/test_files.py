"""Phase 3 targeted tests: upload validation, storage safety, ownership, lifecycle, failures."""

import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.storage.local import LocalFileStorage
from tests.conftest import RunSql
from tests.helpers import DOCX_MIME, make_docx, make_pdf, make_txt, register

KINDS = ["resumes", "documents"]
KEY_RE = re.compile(r"^[0-9a-f]{32}$")


def upload(
    client: TestClient,
    kind: str,
    headers: dict[str, str],
    name: str = "cv.pdf",
    data: bytes | None = None,
    mime: str = "application/pdf",
    **form: str,
) -> Response:
    payload = make_pdf() if data is None else data
    response: Response = client.post(
        f"/api/v1/{kind}", headers=headers, files={"file": (name, payload, mime)}, data=form
    )
    return response


def stored_files(storage_root: Path) -> list[Path]:
    uploads = storage_root / "uploads"
    return [p for p in uploads.rglob("*") if p.is_file()] if uploads.exists() else []


def temp_files(storage_root: Path) -> list[Path]:
    tmp = storage_root / "tmp"
    return list(tmp.iterdir()) if tmp.exists() else []


@pytest.fixture
def alice(client: TestClient) -> dict[str, str]:
    return register(client, "alice@example.com")


@pytest.fixture
def bob(client: TestClient) -> dict[str, str]:
    return register(client, "bob@example.com")


# --------------------------------------------------------- upload validation


def test_valid_pdf_resume(client: TestClient, alice: dict[str, str]) -> None:
    r = upload(client, "resumes", alice, "My CV.pdf")
    assert r.status_code == 201
    body = r.json()
    assert set(body) == {
        "id",
        "filename",
        "content_type",
        "size_bytes",
        "created_at",
        "ingestion",
        "indexing",
    }
    assert body["filename"] == "My CV.pdf"
    assert body["content_type"] == "application/pdf"
    assert body["size_bytes"] == len(make_pdf())


def test_valid_docx_resume(client: TestClient, alice: dict[str, str]) -> None:
    r = upload(client, "resumes", alice, "cv.docx", make_docx(), DOCX_MIME)
    assert r.status_code == 201
    assert r.json()["content_type"] == DOCX_MIME


def test_documents_accept_txt_but_resumes_do_not(
    client: TestClient, alice: dict[str, str], storage_root: Path
) -> None:
    assert upload(client, "documents", alice, "n.txt", make_txt(), "text/plain").status_code == 201
    r = upload(client, "resumes", alice, "n.txt", make_txt(), "text/plain")
    assert r.status_code == 415
    assert r.json()["error"]["code"] == "unsupported_file_type"
    assert len(stored_files(storage_root)) == 1  # only the document


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize(
    "name", ["malware.exe", "legacy.doc", "image.png", "archive.zip", "page.html", "noext", ".pdf"]
)
def test_unsupported_extensions_are_rejected(
    client: TestClient, alice: dict[str, str], storage_root: Path, kind: str, name: str
) -> None:
    r = upload(client, kind, alice, name, make_pdf())
    assert r.status_code == 415
    assert stored_files(storage_root) == []
    assert temp_files(storage_root) == []


def test_declared_type_that_contradicts_the_extension_is_rejected(
    client: TestClient, alice: dict[str, str]
) -> None:
    assert upload(client, "resumes", alice, "cv.pdf", mime="image/png").status_code == 415
    # Generic or missing declared types are fine; the content check decides.
    assert (
        upload(client, "resumes", alice, "cv.pdf", mime="application/octet-stream").status_code
        == 201
    )


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("cv.pdf", b"\x89PNG\r\n\x1a\n" + b"0" * 64),  # PNG renamed to .pdf
        ("cv.pdf", b"%PDF-1.4\nno end marker here" + b"x" * 2000),  # truncated PDF
        ("cv.docx", b"random bytes that are not a zip"),
        ("cv.docx", make_docx(with_document=False)),  # valid zip, not a DOCX
        ("cv.docx", make_docx({"word/vbaProject.bin": b"macro"})),  # macro-enabled
        ("cv.docx", make_docx({"../evil.txt": b"x"})),  # path-traversal member name
        ("cv.docx", make_pdf()),  # PDF renamed to .docx
    ],
)
def test_malformed_or_mismatched_content_is_rejected_and_not_kept(
    client: TestClient,
    alice: dict[str, str],
    storage_root: Path,
    kind: str,
    name: str,
    data: bytes,
) -> None:
    r = upload(client, kind, alice, name, data, mime="application/octet-stream")
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "invalid_file_content"
    assert stored_files(storage_root) == []
    assert temp_files(storage_root) == []  # temp file cleaned up on failure


def test_txt_with_binary_content_or_bad_encoding_is_rejected(
    client: TestClient, alice: dict[str, str]
) -> None:
    for data in (b"text\x00with nul", b"\xff\xfe\xfa invalid utf-8"):
        r = upload(client, "documents", alice, "n.txt", data, "text/plain")
        assert r.status_code == 422


def test_empty_upload_and_missing_file_field(client: TestClient, alice: dict[str, str]) -> None:
    assert upload(client, "resumes", alice, "cv.pdf", b"").status_code == 422
    r = client.post("/api/v1/resumes", headers=alice, data={"other": "x"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "file_required"


def test_per_kind_size_limits_are_enforced_by_streaming_check(
    make_client: Callable[..., TestClient], storage_root: Path
) -> None:
    client = make_client(RESUME_MAX_UPLOAD_MB="1", DOCUMENT_MAX_UPLOAD_MB="2")
    headers = register(client, "alice@example.com")
    limit = 1024 * 1024
    # Within the body allowance (limit + 1 MiB framing) but over the file limit: stream check.
    over = make_pdf(b"x" * limit)
    r = upload(client, "resumes", headers, "big.pdf", over)
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "file_too_large"
    assert "1 MB" in r.json()["error"]["message"]
    assert stored_files(storage_root) == [] and temp_files(storage_root) == []
    # The same size is fine for documents (2 MB limit) and a file just under the limit passes.
    assert upload(client, "documents", headers, "big.pdf", over).status_code == 201
    just_under = make_pdf(b"x" * (limit - 200))
    assert len(just_under) < limit
    assert upload(client, "resumes", headers, "ok.pdf", just_under).status_code == 201


def test_default_limits_are_10mb_for_resumes_and_20mb_for_documents(client: TestClient) -> None:
    from app.core.config import get_settings

    settings = get_settings()
    assert settings.resume_max_bytes == 10 * 1024 * 1024
    assert settings.document_max_bytes == 20 * 1024 * 1024


def test_oversized_body_is_rejected_early_by_declared_length(
    make_client: Callable[..., TestClient], storage_root: Path
) -> None:
    client = make_client(RESUME_MAX_UPLOAD_MB="1")
    headers = register(client, "alice@example.com")
    r = upload(client, "resumes", headers, "huge.pdf", make_pdf(b"x" * (3 * 1024 * 1024)))
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "file_too_large"
    assert stored_files(storage_root) == [] and temp_files(storage_root) == []


def test_oversized_chunked_body_without_content_length_is_rejected(
    make_client: Callable[..., TestClient], storage_root: Path
) -> None:
    client = make_client(RESUME_MAX_UPLOAD_MB="1")
    headers = register(client, "alice@example.com")

    def body() -> Iterator[bytes]:
        yield b"--b\r\nContent-Disposition: form-data; name=file; filename=a.pdf\r\n\r\n"
        for _ in range(3):
            yield b"x" * (1024 * 1024)
        yield b"\r\n--b--\r\n"

    r = client.post(
        "/api/v1/resumes",
        headers={**headers, "Content-Type": "multipart/form-data; boundary=b"},
        content=body(),
    )
    assert r.status_code == 413
    assert stored_files(storage_root) == [] and temp_files(storage_root) == []


# ---------------------------------------------- file names and storage safety


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize(
    ("hostile", "expected_display"),
    [
        ("../../../etc/passwd.pdf", "passwd.pdf"),
        ("..\\..\\windows\\evil.pdf", "evil.pdf"),
        ("a/b/c.pdf", "c.pdf"),
        ("/abs/path/root.pdf", "root.pdf"),
        # httpx percent-encodes control characters and quotes in multipart filenames, so the
        # server sees them literally; they must still be harmless. (Raw ones: unit test below.)
        ("name\x00.pdf", "name%00.pdf"),
        ('we"ird<>|?*.pdf', "we%22ird.pdf"),
        ("  spaced name .pdf", "spaced name .pdf"),
    ],
)
def test_hostile_filenames_never_reach_the_filesystem(
    client: TestClient,
    alice: dict[str, str],
    storage_root: Path,
    run_sql: RunSql,
    kind: str,
    hostile: str,
    expected_display: str,
) -> None:
    r = upload(client, kind, alice, hostile)
    assert r.status_code == 201, r.text
    assert r.json()["filename"] == expected_display

    user_id = run_sql("SELECT id FROM users")[0]["id"]
    files = stored_files(storage_root)
    assert len(files) == 1
    path = files[0]
    # On disk: <root>/uploads/<user-id>/<kind>/<32 hex> -- server-generated, no client text.
    assert path.relative_to(storage_root).parts[:3] == ("uploads", str(user_id), kind)
    assert KEY_RE.fullmatch(path.name)
    # Nothing was written anywhere outside the storage root.
    assert not any(storage_root.parent.glob("passwd*")) and not any(
        storage_root.parent.glob("evil*")
    )
    key = run_sql(f"SELECT storage_key FROM {kind}")[0]["storage_key"]  # noqa: S608
    assert key == path.relative_to(storage_root).as_posix()
    assert expected_display not in key


def test_client_cannot_choose_storage_location_or_owner(
    client: TestClient, alice: dict[str, str], bob: dict[str, str], run_sql: RunSql
) -> None:
    bob_id = run_sql("SELECT id FROM users WHERE email = 'bob@example.com'")[0]["id"]
    r = upload(
        client,
        "resumes",
        alice,
        user_id=str(bob_id),
        storage_key="uploads/x/resumes/y",
        path="../../x",
    )
    assert r.status_code == 201
    owner = run_sql("SELECT u.email, r.storage_key FROM resumes r JOIN users u ON u.id = r.user_id")
    assert owner[0]["email"] == "alice@example.com"
    assert owner[0]["storage_key"] != "uploads/x/resumes/y"
    assert upload_count(client, bob) == 0


def upload_count(client: TestClient, headers: dict[str, str]) -> int:
    return len(client.get("/api/v1/resumes", headers=headers).json()["items"])


def test_download_header_is_safe_for_unusual_filenames(
    client: TestClient, alice: dict[str, str]
) -> None:
    created = upload(client, "resumes", alice, "résumé; final.pdf").json()
    r = client.get(f"/api/v1/resumes/{created['id']}/file", headers=alice)
    disposition = r.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert "filename*=UTF-8''r%C3%A9sum%C3%A9" in disposition
    assert re.search(r'filename="[\x20-\x7e]*"', disposition)
    assert "\r" not in disposition and "\n" not in disposition


# ------------------------------------------------------------- authentication


@pytest.mark.parametrize("kind", KINDS)
def test_all_endpoints_require_authentication(client: TestClient, kind: str) -> None:
    some_id = "00000000-0000-0000-0000-000000000000"
    calls: list[Response] = [
        client.post(f"/api/v1/{kind}", files={"file": ("a.pdf", make_pdf(), "application/pdf")}),
        client.get(f"/api/v1/{kind}"),
        client.get(f"/api/v1/{kind}/{some_id}/file"),
        client.delete(f"/api/v1/{kind}/{some_id}"),
    ]
    for r in calls:
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "not_authenticated"


# ------------------------------------------------------------------ ownership


@pytest.mark.parametrize("kind", KINDS)
def test_users_only_see_their_own_files(
    client: TestClient, alice: dict[str, str], bob: dict[str, str], kind: str
) -> None:
    a1 = upload(client, kind, alice, "alice1.pdf").json()
    a2 = upload(client, kind, alice, "alice2.pdf").json()
    b1 = upload(client, kind, bob, "bob1.pdf").json()
    alice_ids = {i["id"] for i in client.get(f"/api/v1/{kind}", headers=alice).json()["items"]}
    bob_ids = {i["id"] for i in client.get(f"/api/v1/{kind}", headers=bob).json()["items"]}
    assert alice_ids == {a1["id"], a2["id"]}
    assert bob_ids == {b1["id"]}


@pytest.mark.parametrize("kind", KINDS)
def test_user_cannot_download_or_delete_another_users_file(
    client: TestClient,
    alice: dict[str, str],
    bob: dict[str, str],
    storage_root: Path,
    kind: str,
) -> None:
    secret = make_pdf(b"alice-private-content")
    created = upload(client, kind, alice, "private.pdf", secret).json()
    url = f"/api/v1/{kind}/{created['id']}"

    r = client.get(f"{url}/file", headers=bob)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"
    assert b"alice-private-content" not in r.content

    r = client.delete(url, headers=bob)
    assert r.status_code == 404
    assert len(stored_files(storage_root)) == 1  # Alice's file untouched

    # 404 for another user's file is indistinguishable from a file that never existed.
    missing = client.get(f"/api/v1/{kind}/00000000-0000-4000-8000-000000000000/file", headers=bob)
    assert missing.status_code == 404
    assert (
        missing.json()["error"]["message"]
        == client.get(f"{url}/file", headers=bob).json()["error"]["message"]
    )

    # Alice still has full access.
    assert client.get(f"{url}/file", headers=alice).content == secret
    assert client.delete(url, headers=alice).status_code == 204


def test_resume_id_is_not_valid_in_the_documents_namespace(
    client: TestClient, alice: dict[str, str]
) -> None:
    resume = upload(client, "resumes", alice).json()
    assert client.get(f"/api/v1/documents/{resume['id']}/file", headers=alice).status_code == 404
    assert client.delete(f"/api/v1/documents/{resume['id']}", headers=alice).status_code == 404
    assert client.get(f"/api/v1/resumes/{resume['id']}/file", headers=alice).status_code == 200


# ------------------------------------------------------------------ lifecycle


@pytest.mark.parametrize("kind", KINDS)
def test_upload_list_retrieve_delete_lifecycle(
    client: TestClient, alice: dict[str, str], storage_root: Path, kind: str
) -> None:
    content = make_pdf(b"lifecycle-bytes" * 5000)
    created = upload(client, kind, alice, "life.pdf", content).json()
    assert len(stored_files(storage_root)) == 1

    items = client.get(f"/api/v1/{kind}", headers=alice).json()["items"]
    assert [i["id"] for i in items] == [created["id"]]

    r = client.get(f"/api/v1/{kind}/{created['id']}/file", headers=alice)
    assert r.status_code == 200
    assert r.content == content
    assert r.headers["content-type"] == "application/pdf"
    assert r.headers["content-length"] == str(len(content))
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "private, no-store"
    assert r.headers["content-disposition"].startswith("attachment;")

    assert client.delete(f"/api/v1/{kind}/{created['id']}", headers=alice).status_code == 204
    assert client.get(f"/api/v1/{kind}", headers=alice).json()["items"] == []
    assert client.get(f"/api/v1/{kind}/{created['id']}/file", headers=alice).status_code == 404
    assert stored_files(storage_root) == []  # physical file removed as well
    assert client.delete(f"/api/v1/{kind}/{created['id']}", headers=alice).status_code == 404


def test_list_is_newest_first_and_limited(client: TestClient, alice: dict[str, str]) -> None:
    ids = [upload(client, "documents", alice, f"d{i}.pdf").json()["id"] for i in range(3)]
    items = client.get("/api/v1/documents", headers=alice).json()["items"]
    assert [i["id"] for i in items] == ids[::-1]
    assert len(client.get("/api/v1/documents?limit=2", headers=alice).json()["items"]) == 2
    assert client.get("/api/v1/documents?limit=0", headers=alice).status_code == 422


def test_responses_never_expose_paths_keys_or_hashes(
    client: TestClient, alice: dict[str, str], storage_root: Path
) -> None:
    created = upload(client, "resumes", alice).json()
    listing = client.get("/api/v1/resumes", headers=alice).text
    for text in (str(created), listing):
        assert "uploads/" not in text and str(storage_root) not in text
        assert "storage_key" not in text and "sha256" not in text


# ------------------------------------------------------------ failure handling


def test_missing_physical_file_returns_safe_410_and_can_still_be_deleted(
    client: TestClient, alice: dict[str, str], storage_root: Path
) -> None:
    created = upload(client, "resumes", alice).json()
    (path,) = stored_files(storage_root)
    path.unlink()  # simulate lost/corrupted storage

    r = client.get(f"/api/v1/resumes/{created['id']}/file", headers=alice)
    assert r.status_code == 410
    assert r.json()["error"]["code"] == "file_missing"
    assert str(storage_root) not in r.text and "uploads" not in r.text  # no filesystem details
    assert len(client.get("/api/v1/resumes", headers=alice).json()["items"]) == 1  # still listed

    assert client.delete(f"/api/v1/resumes/{created['id']}", headers=alice).status_code == 204
    assert client.get("/api/v1/resumes", headers=alice).json()["items"] == []


def test_database_failure_after_storage_write_leaves_no_orphan_file(
    client: TestClient,
    alice: dict[str, str],
    storage_root: Path,
    run_sql: RunSql,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def failing_commit(self: AsyncSession) -> None:
        raise OperationalError("INSERT", {}, Exception("simulated database outage"))

    monkeypatch.setattr(AsyncSession, "commit", failing_commit)
    r = upload(client, "resumes", alice)
    monkeypatch.undo()

    assert r.status_code == 500
    assert r.json()["error"]["code"] == "save_failed"
    assert "simulated" not in r.text
    assert stored_files(storage_root) == []  # the just-stored file was cleaned up
    assert temp_files(storage_root) == []
    assert run_sql("SELECT count(*) AS n FROM resumes")[0]["n"] == 0


def test_database_delete_failure_after_file_removal_is_recoverable(
    client: TestClient,
    alice: dict[str, str],
    storage_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = upload(client, "resumes", alice).json()
    url = f"/api/v1/resumes/{created['id']}"

    real_delete: Any = AsyncSession.delete

    async def failing_delete(self: AsyncSession, instance: object) -> None:
        raise OperationalError("DELETE", {}, Exception("simulated database outage"))

    monkeypatch.setattr(AsyncSession, "delete", failing_delete)
    r = client.delete(url, headers=alice)
    monkeypatch.setattr(AsyncSession, "delete", real_delete)

    assert r.status_code == 500
    assert r.json()["error"]["code"] == "delete_failed"
    assert "simulated" not in r.text
    # Inconsistent but safe: record remains, content is gone, downloads say so explicitly.
    assert stored_files(storage_root) == []
    assert len(client.get("/api/v1/resumes", headers=alice).json()["items"]) == 1
    assert client.get(f"{url}/file", headers=alice).status_code == 410
    # Retrying completes the deletion.
    assert client.delete(url, headers=alice).status_code == 204
    assert client.get("/api/v1/resumes", headers=alice).json()["items"] == []


def test_storage_delete_failure_keeps_the_record_so_the_user_can_retry(
    client: TestClient,
    alice: dict[str, str],
    storage_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = upload(client, "resumes", alice).json()
    url = f"/api/v1/resumes/{created['id']}"

    def failing_delete(self: LocalFileStorage, key: str) -> bool:
        raise PermissionError("simulated permission problem")

    monkeypatch.setattr(LocalFileStorage, "delete", failing_delete)
    r = client.delete(url, headers=alice)
    monkeypatch.undo()

    assert r.status_code == 500
    assert r.json()["error"]["code"] == "delete_failed"
    assert str(storage_root) not in r.text and "simulated" not in r.text
    assert len(stored_files(storage_root)) == 1  # nothing changed
    assert client.get(f"{url}/file", headers=alice).status_code == 200
    assert client.delete(url, headers=alice).status_code == 204


def test_no_file_names_or_paths_are_written_to_logs(
    client: TestClient,
    alice: dict[str, str],
    storage_root: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("DEBUG")
    created = upload(client, "resumes", alice, "very-secret-name.pdf").json()
    client.get(f"/api/v1/resumes/{created['id']}/file", headers=alice)
    client.delete(f"/api/v1/resumes/{created['id']}", headers=alice)
    logged = "\n".join(r.getMessage() + str(r.__dict__) for r in caplog.records)
    assert "very-secret-name" not in logged
    assert str(storage_root) not in logged


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("../../etc/passwd", "passwd"),
        (r"..\..\x\y.pdf", "y.pdf"),
        ("name\x00.pdf", "name.pdf"),
        ("tab\tand\nnewline.pdf", "tabandnewline.pdf"),
        ('q"uo<te>s|?*.pdf', "quotes.pdf"),
        ("   ...   ", "upload"),
        ("", "upload"),
        (None, "upload"),
    ],
)
def test_sanitize_filename_unit(raw: str | None, expected: str) -> None:
    from app.files.validation import sanitize_filename

    assert sanitize_filename(raw) == expected


def test_sanitize_filename_bounds_length_and_never_contains_separators() -> None:
    from app.files.validation import sanitize_filename

    result = sanitize_filename("a" * 400 + ".pdf")
    assert len(result) <= 255
    assert "/" not in result and "\\" not in result and "\x00" not in result
