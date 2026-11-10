"""Local-filesystem FileStorage.

Layout under the configured root (which must be a persistent volume in deployment):

    <root>/uploads/<user_id>/<namespace>/<uuid4-hex>     permanent files (server-generated names)
    <root>/tmp/<uuid4-hex>.part                          uploads in progress

Safety properties:
- Keys are generated here; a key is only accepted if it matches the exact generated shape.
- Every path is resolved and verified to stay inside the resolved root (blocks traversal and
  symlink escapes).
- Client file names never reach this module.
- Uploads are written to a private temp file, fsynced, then moved with an atomic os.replace
  (the temp dir is inside the root, so it is on the same filesystem).
- Directories are created 0700 and files 0600 where the platform supports it.
"""

import hashlib
import os
import re
import time
import uuid
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import BinaryIO
from uuid import UUID

from app.storage.base import (
    FileStorage,
    FileTooLarge,
    InvalidStorageKey,
    PendingUpload,
    StoredFile,
    StoredFileMissing,
)

NAMESPACES = frozenset({"resumes", "documents"})
_KEY_RE = re.compile(
    r"^uploads/(?P<user>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
    r"/(?P<ns>resumes|documents)/(?P<name>[0-9a-f]{32})$"
)
_DIR_MODE = 0o700
_FILE_MODE = 0o600
_O_BINARY = getattr(os, "O_BINARY", 0)  # Windows


class LocalFileStorage(FileStorage):
    def __init__(self, root: Path) -> None:
        # No I/O here: the root is created lazily so merely building the app never touches disk.
        self._root = Path(root).expanduser().absolute()

    # ------------------------------------------------------------------ helpers
    def _ensure_root(self) -> Path:
        self._root.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
        return self._root.resolve()

    def _resolve(self, key: str) -> Path:
        """Map a generated key to a path, proving it stays inside the root."""
        if not _KEY_RE.fullmatch(key):
            raise InvalidStorageKey("malformed storage key")
        root = self._ensure_root()
        path = (root / key).resolve()
        if not path.is_relative_to(root):
            raise InvalidStorageKey("storage key escapes the storage root")
        return path

    def _make_private_dirs(self, directory: Path) -> None:
        """Create `directory` and every missing ancestor below the root with mode 0700.

        `Path.mkdir(parents=True, mode=...)` applies the mode to the last directory only; the
        intermediate ones (here `uploads/` and `uploads/<user>/`) would get the default mode
        (typically 0755, listable by other local users). Each level is therefore created
        explicitly and chmod-ed, which is also independent of the process umask.
        """
        root = self._ensure_root()
        missing: list[Path] = []
        current = directory
        while current != root and not current.exists() and current != current.parent:
            missing.append(current)
            current = current.parent
        for path in reversed(missing):
            path.mkdir(mode=_DIR_MODE, exist_ok=True)
            os.chmod(path, _DIR_MODE)

    def _tmp_dir(self) -> Path:
        tmp = self._ensure_root() / "tmp"
        tmp.mkdir(exist_ok=True, mode=_DIR_MODE)
        return tmp

    # --------------------------------------------------------------- interface
    def begin(self, user_id: UUID, namespace: str, max_bytes: int) -> PendingUpload:
        if namespace not in NAMESPACES:
            raise InvalidStorageKey("unknown storage namespace")
        key = f"uploads/{user_id}/{namespace}/{uuid.uuid4().hex}"
        if not _KEY_RE.fullmatch(key):  # defends against a non-canonical user_id string
            raise InvalidStorageKey("malformed storage key")
        return _LocalPendingUpload(self, key, max_bytes)

    def open_stream(self, key: str, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        path = self._resolve(key)
        try:
            handle = path.open("rb")
        except FileNotFoundError:
            raise StoredFileMissing("stored file not found") from None

        def chunks() -> Iterator[bytes]:
            try:
                while chunk := handle.read(chunk_size):
                    yield chunk
            finally:
                handle.close()

        return chunks()

    def delete(self, key: str) -> bool:
        path = self._resolve(key)
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        return True

    def exists(self, key: str) -> bool:
        return self._resolve(key).is_file()

    def purge_stale_temp(self, max_age_seconds: float) -> int:
        """Remove abandoned `.part` files (e.g. left by a crash). Returns how many were removed."""
        removed = 0
        cutoff = time.time() - max_age_seconds
        for part in self._tmp_dir().glob("*.part"):
            try:
                if part.stat().st_mtime < cutoff:
                    part.unlink()
                    removed += 1
            except FileNotFoundError:
                continue
        return removed


class _LocalPendingUpload(PendingUpload):
    def __init__(self, storage: LocalFileStorage, key: str, max_bytes: int) -> None:
        self._storage = storage
        self._key = key
        self._max_bytes = max_bytes
        self._size = 0
        self._hash = hashlib.sha256()
        self._committed = False
        self._tmp_path = storage._tmp_dir() / f"{uuid.uuid4().hex}.part"
        fd = os.open(
            self._tmp_path,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | _O_BINARY,
            _FILE_MODE,
        )
        self._writer: BinaryIO | None = os.fdopen(fd, "wb")

    @property
    def size(self) -> int:
        return self._size

    def write(self, chunk: bytes) -> None:
        if self._writer is None:
            raise ValueError("upload is no longer writable")
        self._size += len(chunk)
        if self._size > self._max_bytes:
            raise FileTooLarge("upload exceeds the maximum size")
        self._hash.update(chunk)
        self._writer.write(chunk)

    def _finish_writing(self) -> None:
        if self._writer is not None:
            self._writer.flush()
            os.fsync(self._writer.fileno())
            self._writer.close()
            self._writer = None

    def reader(self) -> AbstractContextManager[BinaryIO]:
        self._finish_writing()

        @contextmanager
        def open_reader() -> Iterator[BinaryIO]:
            with self._tmp_path.open("rb") as handle:
                yield handle

        return open_reader()

    def commit(self) -> StoredFile:
        self._finish_writing()
        target = self._storage._resolve(self._key)
        self._storage._make_private_dirs(target.parent)
        os.replace(self._tmp_path, target)  # atomic: readers never see a partial file
        self._committed = True
        return StoredFile(key=self._key, size=self._size, sha256=self._hash.hexdigest())

    def abort(self) -> None:
        if self._committed:
            return
        if self._writer is not None:
            self._writer.close()
            self._writer = None
        self._tmp_path.unlink(missing_ok=True)
