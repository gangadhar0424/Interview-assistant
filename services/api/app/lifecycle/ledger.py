"""Deletion ledger (SEC-13 Decision 3-R2, D-85).

An append-only record of deletions, kept OUTSIDE the database and the uploads volume, so that after
a restore from an older backup the deleted objects can be deleted again (`app.lifecycle.replay`).

What one entry holds, and nothing else (enforced by `ALLOWED_FIELDS` and a test):
    v, entry_id (random), kind (account|session|resume|document), subject_id (the deleted object's
    id), owner_user_id, deleted_at (UTC, ISO 8601), mac.
No email, name, file name, path, company, job text, question, answer, IP address or user agent,
and no hash of any of them. Every value is a random identifier or a timestamp.

Storage: one JSON line per entry in `ledger-YYYY-MM.jsonl` under DELETION_LEDGER_PATH (a
directory). Lines are appended with a single `write` on a file opened in append mode, so several
API processes on one host can append safely (POSIX append semantics on a local filesystem; a
network filesystem is not supported). Monthly files make retention a whole-file deletion, so no
file is ever rewritten while another process appends to it.

Integrity: each line carries an HMAC-SHA256 over its canonical content with DELETION_LEDGER_KEY.
A changed or forged line fails verification and is never replayed. Limits (documented in
`docs/DATA_LIFECYCLE.md`): a whole line or file removed by someone with write access is not
detected by the MAC (the off-host copy is the protection); the MAC does not hide the content, which
is ids only.
"""

import hashlib
import hmac
import json
import os
import re
import sys
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

Kind = Literal["account", "session", "resume", "document"]
KINDS: frozenset[str] = frozenset({"account", "session", "resume", "document"})
VERSION = 1
ALLOWED_FIELDS = frozenset({"v", "entry_id", "kind", "subject_id", "owner_user_id", "deleted_at"})
_FILE_RE = re.compile(r"^ledger-(\d{4})-(\d{2})\.jsonl$")
_DIR_MODE = 0o700
_WIN_LOCK_OFFSET = 2**40
_FILE_MODE = 0o600


class LedgerError(Exception):
    """A ledger problem. Messages never contain entry content beyond ids."""


@dataclass(frozen=True)
class LedgerEntry:
    entry_id: uuid.UUID
    kind: Kind
    subject_id: uuid.UUID
    owner_user_id: uuid.UUID
    deleted_at: datetime

    def body(self) -> dict[str, object]:
        return {
            "v": VERSION,
            "entry_id": str(self.entry_id),
            "kind": self.kind,
            "subject_id": str(self.subject_id),
            "owner_user_id": str(self.owner_user_id),
            "deleted_at": self.deleted_at.astimezone(UTC).isoformat(),
        }


@dataclass
class ReadReport:
    entries: list[LedgerEntry]
    invalid_lines: int  # failed MAC, malformed, unknown field or kind: never replayed
    files: int


def _canonical(body: dict[str, object]) -> bytes:
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


class DeletionLedger:
    def __init__(self, directory: Path, key: str) -> None:
        if len(key) < 32:
            raise LedgerError("ledger key must be at least 32 characters")
        self._dir = Path(directory)
        self._key = key.encode("utf-8")
        self._lock = threading.Lock()

    def _mac(self, body: dict[str, object]) -> str:
        return hmac.new(self._key, _canonical(body), hashlib.sha256).hexdigest()

    def _file_for(self, when: datetime) -> Path:
        when = when.astimezone(UTC)
        return self._dir / f"ledger-{when.year:04d}-{when.month:02d}.jsonl"

    def _ensure_dir(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
        try:
            os.chmod(self._dir, _DIR_MODE)
        except OSError:  # pragma: no cover - not supported on every platform
            pass

    def append(
        self,
        kind: Kind,
        subject_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        *,
        now: datetime | None = None,
    ) -> LedgerEntry:
        """Write one entry durably (flush and fsync) and return it. Raises on any I/O failure."""
        if kind not in KINDS:
            raise LedgerError("unknown ledger kind")
        if not isinstance(subject_id, uuid.UUID) or not isinstance(owner_user_id, uuid.UUID):
            raise LedgerError("ledger ids must be UUIDs")
        entry = LedgerEntry(uuid.uuid4(), kind, subject_id, owner_user_id, now or datetime.now(UTC))
        body = entry.body()
        line = json.dumps({**body, "mac": self._mac(body)}, separators=(",", ":")) + "\n"
        data = line.encode("utf-8")
        with self._lock:
            self._ensure_dir()
            path = self._file_for(entry.deleted_at)
            flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0)
            fd = os.open(path, flags, _FILE_MODE)
            try:
                # Exclusive OS lock across processes: Windows emulates O_APPEND as seek-then-write,
                # which lost lines between two handles in a test, so append alone is not enough.
                with _exclusive(fd):
                    os.lseek(fd, 0, os.SEEK_END)
                    written = os.write(fd, data)
                    if written != len(data):
                        raise LedgerError("short ledger write")
                    os.fsync(fd)
            finally:
                os.close(fd)
        return entry

    def _files(self) -> list[Path]:
        if not self._dir.is_dir():
            return []
        return sorted(p for p in self._dir.iterdir() if p.is_file() and _FILE_RE.match(p.name))

    def _parse(self, line: str) -> LedgerEntry | None:
        try:
            raw = json.loads(line)
        except ValueError:
            return None
        if not isinstance(raw, dict) or set(raw) != ALLOWED_FIELDS | {"mac"}:
            return None
        mac = raw.pop("mac")
        if not isinstance(mac, str) or not hmac.compare_digest(mac, self._mac(raw)):
            return None
        try:
            if raw["v"] != VERSION or raw["kind"] not in KINDS:
                return None
            deleted_at = datetime.fromisoformat(str(raw["deleted_at"]))
            if deleted_at.tzinfo is None:
                return None
            return LedgerEntry(
                uuid.UUID(str(raw["entry_id"])),
                raw["kind"],
                uuid.UUID(str(raw["subject_id"])),
                uuid.UUID(str(raw["owner_user_id"])),
                deleted_at,
            )
        except (KeyError, ValueError, TypeError):
            return None

    def read(self) -> ReadReport:
        """Every verified entry, oldest file first. Bad lines are counted, never returned."""
        entries: list[LedgerEntry] = []
        invalid = 0
        files = self._files()
        for path in files:
            for line in _lines(path):
                if not line.strip():
                    continue
                entry = self._parse(line)
                if entry is None:
                    invalid += 1
                else:
                    entries.append(entry)
        entries.sort(key=lambda e: (e.deleted_at, str(e.entry_id)))
        return ReadReport(entries, invalid, len(files))

    def prune(
        self, retention_days: int, *, now: datetime | None = None, apply: bool = False
    ) -> list[str]:
        """Remove monthly files whose every entry is older than `retention_days`.

        A file for month M can hold entries up to the end of M, so it is removed only when the
        first instant of the next month is older than the cutoff. Entries therefore live at least
        `retention_days` and at most about a month longer. Returns the file names (dry run unless
        `apply`)."""
        if retention_days < 1:
            raise LedgerError("retention must be at least one day")
        current = (now or datetime.now(UTC)).astimezone(UTC)
        removable: list[str] = []
        for path in self._files():
            match = _FILE_RE.match(path.name)
            if match is None:  # _files() only returns matching names
                continue
            year, month = int(match.group(1)), int(match.group(2))
            month_end = datetime(year + month // 12, month % 12 + 1, 1, tzinfo=UTC)
            if (current - month_end).total_seconds() > retention_days * 86400:
                removable.append(path.name)
                if apply:
                    path.unlink(missing_ok=True)
        return removable


@contextmanager
def _exclusive(fd: int) -> Iterator[None]:
    """Hold an exclusive lock on the open ledger file for one append (blocks until granted)."""
    if sys.platform == "win32":
        import msvcrt

        # Lock one byte far beyond any real file size, so the lock never covers written data.
        os.lseek(fd, _WIN_LOCK_OFFSET, os.SEEK_SET)
        while True:
            try:
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)  # retries for about 10 s, then raises
                break
            except OSError:
                continue
        try:
            yield
        finally:
            os.lseek(fd, _WIN_LOCK_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)


def _lines(path: Path) -> Iterator[str]:
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        yield from handle
