"""SupabaseFileStorage unit tests (D-88), against an in-memory fake of the Storage REST API.

No network: every request goes through httpx.MockTransport. The fake mirrors the endpoints the
adapter uses (upload, authenticated download, list, batch delete, bucket get/create) and the way
Storage reports a missing object.
"""

import hashlib
import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.core.config import ConfigurationError, Settings, load_settings
from app.storage.base import FileTooLarge, InvalidStorageKey, StorageError, StoredFileMissing
from app.storage.factory import build_storage
from app.storage.local import LocalFileStorage
from app.storage.migrate_to_supabase import Report, migrate_row
from app.storage.supabase import SupabaseFileStorage

SECRET = "fake-service-key-for-tests"  # noqa: S105 - not a real key
BUCKET = "uploads"
USER = uuid.uuid4()
OTHER = uuid.uuid4()
KEY_RE = re.compile(r"^uploads/[0-9a-f-]{36}/(resumes|documents)/[0-9a-f]{32}$")


class FakeStorage:
    """Just enough of Supabase Storage. `objects` maps path -> (bytes, created_at)."""

    def __init__(self, *, missing_style: int = 400, bucket_public: bool | None = False) -> None:
        self.objects: dict[str, tuple[bytes, datetime]] = {}
        self.calls: list[tuple[str, str]] = []
        self.missing_style = missing_style
        self.bucket_public = bucket_public  # None: bucket does not exist
        self.fail_next: int | None = None

    def _missing(self) -> httpx.Response:
        if self.missing_style == 404:
            return httpx.Response(404, json={"message": "Object not found"})
        return httpx.Response(
            400, json={"statusCode": "404", "error": "not_found", "message": "Object not found"}
        )

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        assert request.headers["apikey"] == SECRET
        assert request.headers["authorization"] == f"Bearer {SECRET}"
        if self.fail_next is not None:
            status, self.fail_next = self.fail_next, None
            return httpx.Response(status, json={"message": "boom"})
        prefix = "/storage/v1"
        assert path.startswith(prefix)
        path = path[len(prefix) :]

        if request.method == "GET" and path == f"/bucket/{BUCKET}":
            if self.bucket_public is None:
                return httpx.Response(400, json={"statusCode": "404", "error": "Bucket not found"})
            return httpx.Response(200, json={"id": BUCKET, "public": self.bucket_public})
        if request.method == "POST" and path == "/bucket":
            body = json.loads(request.content)
            assert body == {"id": BUCKET, "name": BUCKET, "public": False}
            self.bucket_public = False
            return httpx.Response(200, json={"name": BUCKET})

        if request.method == "POST" and path.startswith(f"/object/list/{BUCKET}"):
            body = json.loads(request.content)
            return httpx.Response(200, json=self._list(body))
        if request.method == "DELETE" and path == f"/object/{BUCKET}":
            deleted = []
            for name in json.loads(request.content)["prefixes"]:
                if self.objects.pop(name, None) is not None:
                    deleted.append({"name": name})
            return httpx.Response(200, json=deleted)
        if request.method == "POST" and path.startswith(f"/object/{BUCKET}/"):
            key = path[len(f"/object/{BUCKET}/") :]
            assert request.headers["x-upsert"] == "false"
            if key in self.objects:
                return httpx.Response(400, json={"statusCode": "409", "error": "Duplicate"})
            self.objects[key] = (request.content, datetime.now(UTC))
            return httpx.Response(200, json={"Key": f"{BUCKET}/{key}"})
        if request.method == "GET" and path.startswith(f"/object/authenticated/{BUCKET}/"):
            key = path[len(f"/object/authenticated/{BUCKET}/") :]
            if key not in self.objects:
                return self._missing()
            return httpx.Response(200, content=self.objects[key][0])
        return httpx.Response(400, json={"message": f"unexpected {request.method} {path}"})

    def _list(self, body: dict[str, Any]) -> list[dict[str, Any]]:
        prefix = body["prefix"].rstrip("/") + "/"
        entries: dict[str, dict[str, Any]] = {}
        for key, (data, created) in self.objects.items():
            if not key.startswith(prefix):
                continue
            head, _, rest = key[len(prefix) :].partition("/")
            if rest:
                entries.setdefault(head, {"name": head, "id": None, "metadata": None})
            else:
                entries[head] = {
                    "name": head,
                    "id": str(uuid.uuid4()),
                    "created_at": created.isoformat().replace("+00:00", "Z"),
                    "metadata": {"size": len(data)},
                }
        items = sorted(entries.values(), key=lambda e: e["name"])
        if body.get("search"):
            items = [e for e in items if e["name"].startswith(body["search"])]
        return items[body["offset"] : body["offset"] + body["limit"]]


@pytest.fixture
def fake() -> FakeStorage:
    return FakeStorage()


@pytest.fixture
def storage(fake: FakeStorage) -> SupabaseFileStorage:
    return SupabaseFileStorage(
        "https://example.supabase.co", SECRET, BUCKET, transport=httpx.MockTransport(fake)
    )


def store(
    storage: SupabaseFileStorage,
    data: bytes = b"hello",
    ns: str = "resumes",
    user: uuid.UUID = USER,
) -> str:
    pending = storage.begin(user, ns, max_bytes=max(1024, len(data)))
    pending.write(data)
    return pending.commit().key


def test_constructing_storage_does_no_io(fake: FakeStorage) -> None:
    transport = httpx.MockTransport(fake)
    SupabaseFileStorage("https://x.supabase.co", SECRET, BUCKET, transport=transport)
    assert fake.calls == []


def test_keys_are_server_generated_unique_and_namespaced(storage: SupabaseFileStorage) -> None:
    k1, k2 = store(storage), store(storage, ns="documents")
    assert k1 != k2
    assert KEY_RE.fullmatch(k1) and k1.startswith(f"uploads/{USER}/resumes/")
    assert KEY_RE.fullmatch(k2) and k2.startswith(f"uploads/{USER}/documents/")


def test_unknown_namespace_is_rejected(storage: SupabaseFileStorage) -> None:
    with pytest.raises(InvalidStorageKey):
        storage.begin(USER, "avatars", max_bytes=10)


def test_round_trip_reports_size_and_sha256(storage: SupabaseFileStorage) -> None:
    data = b"resume bytes " * 10_000
    pending = storage.begin(USER, "resumes", max_bytes=len(data))
    for start in range(0, len(data), 4096):
        pending.write(data[start : start + 4096])
    with pending.reader() as handle:  # validation reads the received bytes
        assert handle.read() == data
    stored = pending.commit()
    assert stored.size == len(data)
    assert stored.sha256 == hashlib.sha256(data).hexdigest()
    assert b"".join(storage.open_stream(stored.key, chunk_size=1000)) == data


def test_nothing_is_visible_before_commit_and_abort_leaves_nothing(
    storage: SupabaseFileStorage, fake: FakeStorage
) -> None:
    pending = storage.begin(USER, "resumes", max_bytes=100)
    pending.write(b"partial")
    assert fake.objects == {}
    pending.abort()
    pending.abort()  # idempotent
    assert fake.objects == {}
    assert not any(method == "POST" and "/object/uploads/" in p for method, p in fake.calls)


def test_size_limit_is_enforced_while_receiving(storage: SupabaseFileStorage) -> None:
    pending = storage.begin(USER, "resumes", max_bytes=5)
    pending.write(b"12345")
    with pytest.raises(FileTooLarge):
        pending.write(b"6")
    pending.abort()


@pytest.mark.parametrize("style", [400, 404])
def test_missing_file_raises_before_streaming(style: int) -> None:
    fake = FakeStorage(missing_style=style)
    storage = SupabaseFileStorage(
        "https://example.supabase.co", SECRET, BUCKET, transport=httpx.MockTransport(fake)
    )
    key = f"uploads/{USER}/resumes/{uuid.uuid4().hex}"
    with pytest.raises(StoredFileMissing):
        storage.open_stream(key)


@pytest.mark.parametrize(
    "key",
    [
        "uploads/../secrets",
        f"uploads/{USER}/resumes/../../x",
        f"uploads/{USER}/avatars/{uuid.uuid4().hex}",
        f"uploads/{USER}/resumes/{uuid.uuid4().hex}?download",
        f"/uploads/{USER}/resumes/{uuid.uuid4().hex}",
        "",
    ],
)
def test_malformed_keys_never_reach_storage(
    storage: SupabaseFileStorage, fake: FakeStorage, key: str
) -> None:
    for call in (storage.open_stream, storage.delete, storage.exists):
        with pytest.raises(InvalidStorageKey):
            call(key)
    assert fake.calls == []


def test_delete_is_idempotent_and_exists_follows(storage: SupabaseFileStorage) -> None:
    key = store(storage)
    assert storage.exists(key)
    assert storage.delete(key) is True
    assert storage.delete(key) is False
    assert not storage.exists(key)


def test_delete_user_tree_removes_only_that_user(
    storage: SupabaseFileStorage, fake: FakeStorage
) -> None:
    mine = [store(storage), store(storage, ns="documents"), store(storage)]
    theirs = store(storage, user=OTHER)
    assert storage.delete_user_tree(USER) == 3
    assert storage.delete_user_tree(USER) == 0  # idempotent
    assert set(fake.objects) == {theirs}
    assert not any(storage.exists(k) for k in mine)


def test_delete_user_tree_requires_a_uuid(storage: SupabaseFileStorage) -> None:
    with pytest.raises(InvalidStorageKey):
        storage.delete_user_tree("../other")  # type: ignore[arg-type]


def test_iter_stored_reports_keys_ages_and_malformed_entries(
    storage: SupabaseFileStorage, fake: FakeStorage
) -> None:
    key = store(storage)
    fake.objects[key] = (fake.objects[key][0], datetime.now(UTC) - timedelta(hours=2))
    fake.objects[f"uploads/{USER}/resumes/not-a-generated-name"] = (b"x", datetime.now(UTC))
    entries = {e.relative_path: e for e in storage.iter_stored()}
    assert entries[key].key == key
    assert 7000 < entries[key].age_seconds < 7400
    assert entries[f"uploads/{USER}/resumes/not-a-generated-name"].key is None


def test_server_errors_are_storage_errors_without_secrets_or_keys(
    storage: SupabaseFileStorage, fake: FakeStorage
) -> None:
    key = f"uploads/{USER}/resumes/{uuid.uuid4().hex}"
    fake.fail_next = 500
    with pytest.raises(StorageError) as caught:
        storage.open_stream(key)
    assert not isinstance(caught.value, StoredFileMissing)
    for text in (str(caught.value), repr(caught.value)):
        assert SECRET not in text and key not in text and "supabase.co" not in text


def test_failed_commit_raises_and_abort_still_works(
    storage: SupabaseFileStorage, fake: FakeStorage
) -> None:
    pending = storage.begin(USER, "resumes", max_bytes=100)
    pending.write(b"data")
    fake.fail_next = 503
    with pytest.raises(StorageError):
        pending.commit()
    pending.abort()
    assert fake.objects == {}


def test_network_failure_is_a_storage_error() -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    storage = SupabaseFileStorage(
        "https://example.supabase.co", SECRET, BUCKET, transport=httpx.MockTransport(broken)
    )
    with pytest.raises(StorageError) as caught:
        storage.exists(f"uploads/{USER}/resumes/{uuid.uuid4().hex}")
    assert "no route" not in str(caught.value)


def test_put_existing_never_overwrites(storage: SupabaseFileStorage, fake: FakeStorage) -> None:
    key = f"uploads/{USER}/documents/{uuid.uuid4().hex}"
    assert storage.put_existing(key, b"first") is True
    assert storage.put_existing(key, b"second") is False
    assert fake.objects[key][0] == b"first"


def test_bucket_setup_creates_private_and_refuses_public() -> None:
    fake = FakeStorage(bucket_public=None)
    storage = SupabaseFileStorage(
        "https://example.supabase.co", SECRET, BUCKET, transport=httpx.MockTransport(fake)
    )
    assert storage.ensure_private_bucket() is True
    assert storage.ensure_private_bucket() is False
    fake.bucket_public = True
    with pytest.raises(StorageError, match="PUBLIC"):
        storage.ensure_private_bucket()


# ---- configuration and factory -------------------------------------------------------------


def test_backend_defaults_to_local_and_factory_builds_it(monkeypatch: pytest.MonkeyPatch) -> None:
    # The default comes from the field, whatever a developer's .env says.
    assert Settings.model_fields["file_storage_backend"].default == "local"
    monkeypatch.setenv("FILE_STORAGE_BACKEND", "local")
    assert isinstance(build_storage(load_settings()), LocalFileStorage)


def test_factory_builds_supabase_without_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FILE_STORAGE_BACKEND", "supabase")
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", SECRET)
    assert isinstance(build_storage(load_settings()), SupabaseFileStorage)


@pytest.mark.parametrize(
    ("url", "key"),
    [
        ("", SECRET),
        ("http://example.supabase.co", SECRET),
        ("https://example.supabase.co/storage/v1", SECRET),
        ("https://user:pw@example.supabase.co", SECRET),
        ("https://example.supabase.co", ""),
    ],
)
def test_supabase_backend_needs_https_url_and_key(
    monkeypatch: pytest.MonkeyPatch, url: str, key: str
) -> None:
    monkeypatch.setenv("FILE_STORAGE_BACKEND", "supabase")
    monkeypatch.setenv("SUPABASE_URL", url)
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", key)
    with pytest.raises(ConfigurationError) as caught:
        load_settings()
    assert SECRET not in str(caught.value)


# ---- one-off migration (DB-driven copy; the DB part is covered by running the command) ------


def test_migration_copies_verifies_and_is_idempotent(
    tmp_path: Path, storage: SupabaseFileStorage, fake: FakeStorage
) -> None:
    local = LocalFileStorage(tmp_path / "root")
    data = b"%PDF resume"
    pending = local.begin(USER, "resumes", max_bytes=1024)
    pending.write(data)
    stored = pending.commit()

    dry = Report()
    migrate_row("resume:1", stored.key, stored.sha256, [local], storage, False, dry)
    assert fake.objects == {} and dry.copied == 0 and not dry.missing_locally

    first = Report()
    migrate_row("resume:1", stored.key, stored.sha256, [local], storage, True, first)
    assert (first.copied, first.verified) == (1, 1)
    assert fake.objects[stored.key][0] == data

    again = Report()
    migrate_row("resume:1", stored.key, stored.sha256, [local], storage, True, again)
    assert (again.copied, again.already_present, again.verified) == (0, 1, 1)


def test_migration_refuses_a_changed_file_and_reports_a_missing_one(
    tmp_path: Path, storage: SupabaseFileStorage, fake: FakeStorage
) -> None:
    local = LocalFileStorage(tmp_path / "root")
    pending = local.begin(USER, "documents", max_bytes=1024)
    pending.write(b"original")
    stored = pending.commit()
    report = Report()
    migrate_row("document:1", stored.key, "0" * 64, [local], storage, True, report)
    missing_key = f"uploads/{USER}/documents/{uuid.uuid4().hex}"
    migrate_row("document:2", missing_key, "0" * 64, [local], storage, True, report)
    assert report.hash_mismatch == ["document:1"]
    assert report.missing_locally == ["document:2"]
    assert fake.objects == {}
