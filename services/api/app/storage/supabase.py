"""Supabase Storage FileStorage (D-88).

Files live in ONE private bucket under exactly the keys LocalFileStorage generates:

    uploads/<user_id>/<namespace>/<uuid4-hex>

so database rows are valid for both backends and moving between them is a copy.

Safety properties (same contract as the local backend):
- Keys are generated here; a key is only accepted if it matches the exact generated shape, so no
  client-controlled string ever becomes part of a storage URL.
- The bucket is private and reached server-side with the project's secret key; nothing here is
  ever exposed to a browser, and no URL, key or file content is logged or put in an exception.
- An upload is spooled to a private temporary file, size-limited and hashed while it arrives,
  and only becomes visible at its key when `commit()` sends it (Storage creates an object
  atomically, and `x-upsert: false` refuses to overwrite).
- Missing content raises StoredFileMissing before any byte is yielded.

Implementations are synchronous (callers use a thread pool), matching the interface.
"""

import hashlib
import tempfile
import time
import uuid
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import datetime
from typing import Any, BinaryIO, cast
from uuid import UUID

import httpx

from app.storage.base import (
    FileStorage,
    FileTooLarge,
    InvalidStorageKey,
    PendingUpload,
    StorageError,
    StoredEntry,
    StoredFile,
    StoredFileMissing,
)
from app.storage.local import _KEY_RE, NAMESPACES

_LIST_PAGE = 1000
_DELETE_BATCH = 100
_TIMEOUT = httpx.Timeout(60.0, connect=10.0)


def _is_missing(response: httpx.Response) -> bool:
    """Storage reports a missing object as 404, or (older versions) 400 with a 404 body."""
    if response.status_code == 404:
        return True
    if response.status_code != 400:
        return False
    try:
        body = response.json()
    except ValueError:
        return False
    return isinstance(body, dict) and (
        str(body.get("statusCode")) == "404" or body.get("error") == "not_found"
    )


def _age_seconds(item: dict[str, Any], now: float) -> float:
    stamp = item.get("created_at") or item.get("updated_at")
    if not isinstance(stamp, str):
        return 0.0
    try:
        return max(0.0, now - datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0.0


class SupabaseFileStorage(FileStorage):
    def __init__(
        self,
        base_url: str,
        service_key: str,
        bucket: str,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        # No network I/O here: building the app (or a worker) must not contact Storage.
        self._bucket = bucket
        self._client = httpx.Client(
            base_url=base_url.rstrip("/") + "/storage/v1",
            headers={"apikey": service_key, "Authorization": f"Bearer {service_key}"},
            timeout=_TIMEOUT,
            transport=transport,
            follow_redirects=False,
        )

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _check_key(key: str) -> str:
        if not _KEY_RE.fullmatch(key):
            raise InvalidStorageKey("malformed storage key")
        return key

    def _object_path(self, key: str) -> str:
        return f"/object/{self._bucket}/{self._check_key(key)}"

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            return self._client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise StorageError(f"file storage request failed ({type(exc).__name__})") from None

    def _list(self, prefix: str, search: str | None = None) -> Iterator[dict[str, Any]]:
        """One folder level (Storage lists are not recursive). Folders have `id: null`."""
        offset = 0
        while True:
            body: dict[str, Any] = {
                "prefix": prefix,
                "limit": _LIST_PAGE,
                "offset": offset,
                "sortBy": {"column": "name", "order": "asc"},
            }
            if search is not None:
                body["search"] = search
            response = self._request("POST", f"/object/list/{self._bucket}", json=body)
            if response.status_code != 200:
                raise StorageError(f"file storage list failed (HTTP {response.status_code})")
            items = response.json()
            if not isinstance(items, list):
                raise StorageError("file storage list returned an unexpected body")
            yield from (item for item in items if isinstance(item, dict))
            if len(items) < _LIST_PAGE:
                return
            offset += _LIST_PAGE

    def _walk(self, prefix: str) -> Iterator[tuple[str, dict[str, Any]]]:
        """Every object below `prefix` as (full path, list item), depth first."""
        for item in self._list(prefix):
            name = item.get("name")
            if not isinstance(name, str) or not name or "/" in name or name in (".", ".."):
                continue
            path = f"{prefix}/{name}"
            if item.get("id") is None:  # a folder
                yield from self._walk(path)
            else:
                yield path, item

    def _delete_paths(self, paths: list[str]) -> int:
        removed = 0
        for start in range(0, len(paths), _DELETE_BATCH):
            batch = paths[start : start + _DELETE_BATCH]
            response = self._request("DELETE", f"/object/{self._bucket}", json={"prefixes": batch})
            if response.status_code != 200:
                raise StorageError(f"file storage delete failed (HTTP {response.status_code})")
            deleted = response.json()
            removed += len(deleted) if isinstance(deleted, list) else 0
        return removed

    # --------------------------------------------------------------- interface
    def begin(self, user_id: UUID, namespace: str, max_bytes: int) -> PendingUpload:
        if namespace not in NAMESPACES:
            raise InvalidStorageKey("unknown storage namespace")
        key = f"uploads/{user_id}/{namespace}/{uuid.uuid4().hex}"
        self._check_key(key)  # defends against a non-canonical user_id string
        return _SupabasePendingUpload(self, key, max_bytes)

    def open_stream(self, key: str, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        request = self._client.build_request(
            "GET", f"/object/authenticated/{self._bucket}/{self._check_key(key)}"
        )
        try:
            response = self._client.send(request, stream=True)
        except httpx.HTTPError as exc:
            raise StorageError(f"file storage request failed ({type(exc).__name__})") from None
        if response.status_code != 200:
            response.read()
            response.close()
            if _is_missing(response):
                raise StoredFileMissing("stored file not found")
            raise StorageError(f"file storage download failed (HTTP {response.status_code})")

        def chunks() -> Iterator[bytes]:
            try:
                yield from response.iter_bytes(chunk_size)
            except httpx.HTTPError as exc:
                raise StorageError(f"file storage stream failed ({type(exc).__name__})") from None
            finally:
                response.close()

        return chunks()

    def delete(self, key: str) -> bool:
        return self._delete_paths([self._check_key(key)]) > 0

    def exists(self, key: str) -> bool:
        folder, _, name = self._check_key(key).rpartition("/")
        return any(
            item.get("name") == name and item.get("id") is not None
            for item in self._list(folder, search=name)
        )

    def delete_user_tree(self, user_id: UUID) -> int:
        if not isinstance(user_id, UUID):
            raise InvalidStorageKey("user id must be a UUID")
        paths = [path for path, _ in self._walk(f"uploads/{user_id}")]
        return self._delete_paths(paths) if paths else 0

    def iter_stored(self) -> Iterator[StoredEntry]:
        now = time.time()
        for path, item in self._walk("uploads"):
            yield StoredEntry(
                path if _KEY_RE.fullmatch(path) else None, _age_seconds(item, now), path
            )

    def ensure_private_bucket(self) -> bool:
        """Create the bucket as PRIVATE if missing (setup only). Returns True if it was created.
        Refuses to continue if an existing bucket of that name is public."""
        response = self._request("GET", f"/bucket/{self._bucket}")
        if response.status_code == 200:
            if response.json().get("public") is not False:
                raise StorageError("the storage bucket exists but is PUBLIC; make it private")
            return False
        response = self._request(
            "POST", "/bucket", json={"id": self._bucket, "name": self._bucket, "public": False}
        )
        if response.status_code not in (200, 201):
            raise StorageError(f"could not create the storage bucket (HTTP {response.status_code})")
        return True

    def put_existing(self, key: str, data: bytes) -> bool:
        """Copy already-stored content to `key` (migration only; never overwrites).
        Returns False if an object already exists at that key."""
        response = self._request(
            "POST",
            self._object_path(key),
            content=data,
            headers={"Content-Type": "application/octet-stream", "x-upsert": "false"},
        )
        if response.status_code in (200, 201):
            return True
        if response.status_code in (400, 409) and "uplicate" in response.text:
            return False
        raise StorageError(f"file storage upload failed (HTTP {response.status_code})")

    def close(self) -> None:
        self._client.close()


class _SupabasePendingUpload(PendingUpload):
    def __init__(self, storage: SupabaseFileStorage, key: str, max_bytes: int) -> None:
        self._storage = storage
        self._key = key
        self._max_bytes = max_bytes
        self._size = 0
        self._hash = hashlib.sha256()
        self._committed = False
        # Anonymous temporary file: private to this process and removed on close.
        self._file: BinaryIO | None = cast(BinaryIO, tempfile.TemporaryFile())  # noqa: SIM115

    @property
    def size(self) -> int:
        return self._size

    def _open_file(self) -> BinaryIO:
        if self._file is None:
            raise ValueError("upload is no longer open")
        return self._file

    def write(self, chunk: bytes) -> None:
        handle = self._open_file()
        if self._committed:
            raise ValueError("upload is no longer writable")
        self._size += len(chunk)
        if self._size > self._max_bytes:
            raise FileTooLarge("upload exceeds the maximum size")
        self._hash.update(chunk)
        handle.write(chunk)

    def reader(self) -> AbstractContextManager[BinaryIO]:
        handle = self._open_file()
        handle.flush()

        @contextmanager
        def open_reader() -> Iterator[BinaryIO]:
            handle.seek(0)
            yield handle  # not closed here: commit() still needs the bytes
            handle.seek(0, 2)

        return open_reader()

    def commit(self) -> StoredFile:
        handle = self._open_file()
        handle.flush()
        handle.seek(0)
        data = handle.read()
        response = self._storage._request(
            "POST",
            self._storage._object_path(self._key),
            content=data,
            headers={"Content-Type": "application/octet-stream", "x-upsert": "false"},
        )
        if response.status_code not in (200, 201):
            raise StorageError(f"file storage upload failed (HTTP {response.status_code})")
        self._committed = True
        self._close()
        return StoredFile(key=self._key, size=self._size, sha256=self._hash.hexdigest())

    def _close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None

    def abort(self) -> None:
        self._close()
