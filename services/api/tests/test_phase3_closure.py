"""Phase 3 closure checks: size boundary, public path boundary, commit-failure cleanup, config."""

import os
import tempfile
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import ConfigurationError, Settings, load_settings
from tests.helpers import make_pdf, register
from tests.test_files import stored_files, temp_files, upload

MIB = 1024 * 1024


def pdf_of_size(size: int) -> bytes:
    base = make_pdf()
    data = make_pdf(b"x" * (size - len(base)))
    assert len(data) == size
    return data


# ------------------------------------------------ 1. size-limit boundary


def test_size_limit_boundary_exactly_at_limit_passes_and_one_byte_over_fails(
    make_client: Callable[..., TestClient], storage_root: Path
) -> None:
    client = make_client(RESUME_MAX_UPLOAD_MB="1")
    headers = register(client, "alice@example.com")

    at_limit = upload(client, "resumes", headers, "at.pdf", pdf_of_size(MIB))
    assert at_limit.status_code == 201
    assert at_limit.json()["size_bytes"] == MIB
    assert len(stored_files(storage_root)) == 1

    over = upload(client, "resumes", headers, "over.pdf", pdf_of_size(MIB + 1))
    assert over.status_code == 413
    assert over.json()["error"]["code"] == "file_too_large"
    assert len(stored_files(storage_root)) == 1  # only the at-limit file; nothing from the reject
    assert temp_files(storage_root) == []


def test_understated_content_length_cannot_bypass_the_limit(
    make_client: Callable[..., TestClient], storage_root: Path
) -> None:
    client = make_client(RESUME_MAX_UPLOAD_MB="1")
    headers = register(client, "alice@example.com")
    boundary = "closure"
    body = (
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="a.pdf"\r\n'
            "Content-Type: application/pdf\r\n\r\n"
        ).encode()
        + pdf_of_size(3 * MIB)
        + f"\r\n--{boundary}--\r\n".encode()
    )
    r = client.post(
        "/api/v1/resumes",
        headers={
            **headers,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": "100",  # lies: the real body is ~3 MiB
        },
        content=body,
    )
    assert r.status_code == 413
    assert stored_files(storage_root) == []
    assert temp_files(storage_root) == []


# --------------------------------------- 2. public path/key/owner boundary


@pytest.mark.parametrize("kind", ["resumes", "documents"])
@pytest.mark.parametrize(
    "hostile_id",
    [
        "..%2F..%2F..%2Fetc%2Fpasswd",
        "%2Fetc%2Fpasswd",
        "..%5C..%5Cwindows%5Cwin.ini",
        "uploads%2F00000000-0000-4000-8000-000000000000%2Fresumes%2F" + "a" * 32,
        "../../etc/passwd",
        "C:%5CWindows%5Cwin.ini",
    ],
)
def test_path_like_resource_ids_never_reach_storage(
    client: TestClient, kind: str, hostile_id: str
) -> None:
    headers = register(client, "alice@example.com")
    for method, suffix in (("GET", "/file"), ("DELETE", "")):
        r = client.request(method, f"/api/v1/{kind}/{hostile_id}{suffix}", headers=headers)
        assert r.status_code in (404, 405, 422), (method, hostile_id, r.status_code)
        assert "root:" not in r.text and "[fonts]" not in r.text  # no system file content


def test_client_cannot_select_key_owner_or_root_through_any_request_field(
    client: TestClient, storage_root: Path, tmp_path: Path
) -> None:
    alice = register(client, "alice@example.com")
    bob = register(client, "bob@example.com")
    escape = tmp_path / "escaped"
    created = client.post(
        f"/api/v1/resumes?storage_key=../x&user_id=bob&root={escape}",
        headers={**alice, "X-Storage-Root": str(escape), "X-User-Id": "bob"},
        files={"file": ("C:\\Windows\\win.ini.pdf", make_pdf(), "application/pdf")},
        data={
            "storage_key": "uploads/bob/resumes/x",
            "user_id": "bob",
            "path": str(escape),
            "file_storage_root": str(escape),
        },
    )
    assert created.status_code == 201
    # The same query-string tricks on download are ignored: the server resolves its own key.
    file_id = created.json()["id"]
    r = client.get(
        f"/api/v1/resumes/{file_id}/file?storage_key=../../etc/passwd&path=/etc/passwd",
        headers=alice,
    )
    assert r.status_code == 200 and r.content == make_pdf()

    (path,) = stored_files(storage_root)
    assert path.relative_to(storage_root).parts[0] == "uploads"
    assert not escape.exists()  # nothing created where the client asked
    assert {p.name for p in tmp_path.iterdir()} == {"app-data"}  # nothing outside the root
    assert client.get("/api/v1/resumes", headers=bob).json()["items"] == []


# ------------------------------------- 3. commit-failure and cleanup


def test_failure_while_committing_the_file_leaves_no_stored_or_temp_file(
    client: TestClient, storage_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = register(client, "alice@example.com")

    def failing_replace(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated disk failure")

    monkeypatch.setattr("app.storage.local.os.replace", failing_replace)
    r = upload(client, "resumes", headers)
    monkeypatch.undo()

    assert r.status_code == 500
    assert r.json()["error"]["code"] == "save_failed"
    assert "simulated" not in r.text and str(storage_root) not in r.text
    assert stored_files(storage_root) == []
    assert temp_files(storage_root) == []
    # The system is healthy afterwards.
    assert upload(client, "resumes", headers).status_code == 201


# --------------------------------------------- 4. configuration safety


def _settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    for key in ("FILE_STORAGE_ROOT", "APP_ENV"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("JWT_SECRET", "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0U1v2W3x4")
    # Deployed environments also require TLS Redis and a maintenance DB URL (D-45).
    monkeypatch.setenv("REDIS_URL", "rediss://redis.example.invalid:6379/0")
    monkeypatch.setenv(
        "MAINTENANCE_DATABASE_URL",
        "postgresql+asyncpg://maintenance:placeholder@db.example.invalid/app",
    )
    # Deployed environments also require a real embedding provider (Phase 5); keep this helper
    # focused on the storage-root rules.
    monkeypatch.setenv("EMBEDDING_PROVIDER", "fastembed")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return load_settings()


def test_local_default_root_is_usable_and_blank_value_means_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    default = _settings(monkeypatch, APP_ENV="local").storage_root
    assert default.is_absolute() and default.name == "app-data"
    assert _settings(monkeypatch, APP_ENV="local", FILE_STORAGE_ROOT="  ").storage_root == default


@pytest.mark.parametrize("env", ["staging", "production"])
@pytest.mark.parametrize("value", [None, "", "   ", ".", "uploads", "./uploads", "../up"])
def test_deployed_environments_reject_missing_blank_or_relative_roots(
    monkeypatch: pytest.MonkeyPatch, env: str, value: str | None
) -> None:
    extra = {} if value is None else {"FILE_STORAGE_ROOT": value}
    with pytest.raises(ConfigurationError) as exc:
        _settings(monkeypatch, APP_ENV=env, **extra)
    assert "FILE_STORAGE_ROOT" in str(exc.value)
    assert "persistent" in str(exc.value) or "absolute" in str(exc.value)


def test_deployed_environments_accept_an_explicit_absolute_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = str(Path(tempfile.gettempdir()) / "persistent-volume")
    s = _settings(monkeypatch, APP_ENV="production", FILE_STORAGE_ROOT=root)
    assert s.storage_root == Path(root)


def test_root_is_created_lazily_by_the_server_never_at_startup_or_from_requests(
    client: TestClient, storage_root: Path
) -> None:
    assert not storage_root.exists()  # app startup performs no storage I/O
    headers = register(client, "alice@example.com")
    assert client.get("/api/v1/resumes", headers=headers).status_code == 200
    assert not storage_root.exists()  # reads do not create it either
    assert upload(client, "resumes", headers).status_code == 201
    assert {p.name for p in storage_root.iterdir()} == {"uploads", "tmp"}
    assert os.path.isabs(storage_root)
