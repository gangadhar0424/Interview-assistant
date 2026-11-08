"""LocalFileStorage unit tests (no database): key safety, path confinement, temp handling."""

import os
import re
import time
import uuid
from pathlib import Path

import pytest

from app.storage.base import FileTooLarge, InvalidStorageKey, StoredFileMissing
from app.storage.local import LocalFileStorage

USER = uuid.uuid4()
KEY_RE = re.compile(r"^uploads/[0-9a-f-]{36}/(resumes|documents)/[0-9a-f]{32}$")


@pytest.fixture
def storage(tmp_path: Path) -> LocalFileStorage:
    return LocalFileStorage(tmp_path / "root")


def store(storage: LocalFileStorage, data: bytes = b"hello", ns: str = "resumes") -> str:
    pending = storage.begin(USER, ns, max_bytes=max(1024, len(data)))
    pending.write(data)
    return pending.commit().key


def test_constructing_storage_does_no_io(tmp_path: Path) -> None:
    LocalFileStorage(tmp_path / "never-created")
    assert not (tmp_path / "never-created").exists()


def test_keys_are_server_generated_unique_and_namespaced(storage: LocalFileStorage) -> None:
    k1, k2 = store(storage), store(storage)
    assert k1 != k2
    for key in (k1, k2):
        assert KEY_RE.fullmatch(key)
        assert key.startswith(f"uploads/{USER}/resumes/")


def test_commit_is_atomic_and_reports_size_and_hash(
    storage: LocalFileStorage, tmp_path: Path
) -> None:
    pending = storage.begin(USER, "documents", max_bytes=1024)
    pending.write(b"abc")
    # Nothing exists at any permanent location before commit.
    assert not (tmp_path / "root" / "uploads").exists()
    stored = pending.commit()
    assert stored.size == 3
    assert stored.sha256 == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    assert (tmp_path / "root" / stored.key).read_bytes() == b"abc"
    assert list((tmp_path / "root" / "tmp").iterdir()) == []  # temp file consumed by the move


def test_abort_removes_temp_file_and_is_idempotent(
    storage: LocalFileStorage, tmp_path: Path
) -> None:
    pending = storage.begin(USER, "resumes", max_bytes=1024)
    pending.write(b"partial")
    assert len(list((tmp_path / "root" / "tmp").iterdir())) == 1
    pending.abort()
    pending.abort()
    assert list((tmp_path / "root" / "tmp").iterdir()) == []
    assert not (tmp_path / "root" / "uploads").exists()


def test_abort_after_commit_keeps_the_stored_file(
    storage: LocalFileStorage, tmp_path: Path
) -> None:
    pending = storage.begin(USER, "resumes", max_bytes=1024)
    pending.write(b"keep me")
    stored = pending.commit()
    pending.abort()
    assert (tmp_path / "root" / stored.key).read_bytes() == b"keep me"


def test_size_limit_is_enforced_while_writing(storage: LocalFileStorage, tmp_path: Path) -> None:
    pending = storage.begin(USER, "resumes", max_bytes=10)
    pending.write(b"0123456789")
    with pytest.raises(FileTooLarge):
        pending.write(b"x")
    pending.abort()
    assert list((tmp_path / "root" / "tmp").iterdir()) == []


@pytest.mark.parametrize(
    "bad_key",
    [
        "../../etc/passwd",
        "/etc/passwd",
        "C:\\Windows\\win.ini",
        f"uploads/{USER}/resumes/../../../outside",
        f"uploads/{USER}/resumes/" + "a" * 31,  # wrong name length
        f"uploads/{USER}/other/" + "a" * 32,  # unknown namespace
        f"uploads/{str(USER).upper()}/resumes/" + "a" * 32,  # non-canonical user id
        f"uploads/{USER}/resumes/" + "a" * 32 + "/extra",
        f"uploads/{USER}/resumes/" + "a" * 32 + "\x00",
        "",
    ],
)
def test_malformed_or_escaping_keys_are_rejected_everywhere(
    storage: LocalFileStorage, bad_key: str
) -> None:
    with pytest.raises(InvalidStorageKey):
        storage.open_stream(bad_key)
    with pytest.raises(InvalidStorageKey):
        storage.delete(bad_key)
    with pytest.raises(InvalidStorageKey):
        storage.exists(bad_key)


def test_unknown_namespace_cannot_begin(storage: LocalFileStorage) -> None:
    with pytest.raises(InvalidStorageKey):
        storage.begin(USER, "../escape", max_bytes=10)


def test_missing_file_raises_before_streaming_and_delete_is_idempotent(
    storage: LocalFileStorage,
) -> None:
    key = f"uploads/{USER}/resumes/" + "a" * 32
    with pytest.raises(StoredFileMissing):
        storage.open_stream(key)
    assert storage.delete(key) is False
    assert storage.exists(key) is False


def test_stream_and_delete_roundtrip(storage: LocalFileStorage) -> None:
    key = store(storage, b"x" * 200_000)
    assert b"".join(storage.open_stream(key, chunk_size=4096)) == b"x" * 200_000
    assert storage.delete(key) is True
    assert storage.exists(key) is False


def test_symlink_escape_is_blocked(storage: LocalFileStorage, tmp_path: Path) -> None:
    key = store(storage)
    user_dir = tmp_path / "root" / "uploads" / str(USER)
    outside = tmp_path / "outside"
    (outside / "resumes").mkdir(parents=True)
    victim = outside / "resumes" / key.rsplit("/", 1)[1]
    victim.write_bytes(b"secret")
    # Replace the user's directory with a symlink that points outside the root.
    for child in list(user_dir.rglob("*"))[::-1]:
        child.unlink() if child.is_file() else child.rmdir()
    user_dir.rmdir()
    try:
        user_dir.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted on this platform/user")
    with pytest.raises(InvalidStorageKey):
        storage.open_stream(key)
    with pytest.raises(InvalidStorageKey):
        storage.delete(key)
    assert victim.read_bytes() == b"secret"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are not meaningful on Windows")
def test_files_and_directories_are_private_on_posix(
    storage: LocalFileStorage, tmp_path: Path
) -> None:
    key = store(storage)
    root = tmp_path / "root"
    assert (root / key).stat().st_mode & 0o777 == 0o600
    assert (root / "uploads").stat().st_mode & 0o777 == 0o700


def test_purge_stale_temp_removes_only_old_part_files(
    storage: LocalFileStorage, tmp_path: Path
) -> None:
    fresh = storage.begin(USER, "resumes", max_bytes=10)
    fresh.write(b"a")
    stale = tmp_path / "root" / "tmp" / "stale.part"
    stale.write_bytes(b"old")
    old = time.time() - 7200
    os.utime(stale, (old, old))
    assert storage.purge_stale_temp(max_age_seconds=3600) == 1
    assert not stale.exists()
    assert len(list((tmp_path / "root" / "tmp").iterdir())) == 1  # the in-progress upload survives
    fresh.abort()
