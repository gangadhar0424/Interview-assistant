"""Copy uploaded files from local storage to Supabase Storage (D-88). One-off operator command.

    python -m app.storage.migrate_to_supabase             # dry run: report only
    python -m app.storage.migrate_to_supabase --apply     # copy

Driven by the database, not the disk: every resume and document row names its storage key and
the SHA-256 of its content. For each row the file is looked up under the local roots, checked
against that hash, uploaded to the PRIVATE bucket under the SAME key (never overwriting), then
downloaded again and re-checked. Files no row references are not copied (the orphan sweep owns
them). Output is counts and row ids only: no file names, contents, URLs or keys.

Needs SUPABASE_URL and SUPABASE_SERVICE_KEY; works whatever FILE_STORAGE_BACKEND is, so it can
run before the switch. Idempotent: rows already present in the bucket are verified, not re-sent.
"""

import argparse
import asyncio
import hashlib
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select

from app.core.config import REPO_ROOT, Settings, get_settings
from app.db.models import Document, Resume
from app.db.session import create_engine, create_maintenance_sessionmaker
from app.storage.base import StorageError, StoredFileMissing
from app.storage.local import LocalFileStorage
from app.storage.supabase import SupabaseFileStorage

API_DIR = Path(__file__).resolve().parents[2]


@dataclass
class Report:
    rows: int = 0
    copied: int = 0
    already_present: int = 0
    verified: int = 0
    missing_locally: list[str] = field(default_factory=list)
    hash_mismatch: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


def local_roots(settings: Settings, extra: Sequence[Path]) -> list[LocalFileStorage]:
    """The configured root plus the two places a relative FILE_STORAGE_ROOT has resolved to."""
    seen: list[Path] = []
    for root in [settings.storage_root, REPO_ROOT / "app-data", API_DIR / "app-data", *extra]:
        resolved = Path(root).expanduser().absolute()
        if resolved not in seen and (resolved / "uploads").is_dir():
            seen.append(resolved)
    return [LocalFileStorage(root) for root in seen]


def _read(storage: LocalFileStorage | SupabaseFileStorage, key: str) -> bytes | None:
    try:
        return b"".join(storage.open_stream(key))
    except StoredFileMissing:
        return None


def migrate_row(
    label: str,
    key: str,
    sha256: str,
    roots: Sequence[LocalFileStorage],
    target: SupabaseFileStorage,
    apply: bool,
    report: Report,
) -> None:
    report.rows += 1
    if target.exists(key):
        report.already_present += 1
        remote = _read(target, key)
        if remote is not None and hashlib.sha256(remote).hexdigest() == sha256:
            report.verified += 1
        else:
            report.hash_mismatch.append(label)
        return
    data = next((d for d in (_read(root, key) for root in roots) if d is not None), None)
    if data is None:
        report.missing_locally.append(label)
        return
    if hashlib.sha256(data).hexdigest() != sha256:
        report.hash_mismatch.append(label)
        return
    if not apply:
        return
    target.put_existing(key, data)
    report.copied += 1
    remote = _read(target, key)
    if remote is not None and hashlib.sha256(remote).hexdigest() == sha256:
        report.verified += 1
    else:
        report.failed.append(label)


async def _rows(settings: Settings) -> list[tuple[str, str, str]]:
    engine = create_engine(settings.require_maintenance_database_url(), settings)
    try:
        sessions = create_maintenance_sessionmaker(engine)
        rows: list[tuple[str, str, str]] = []
        async with sessions() as session:
            for kind, model in (("resume", Resume), ("document", Document)):
                result = await session.execute(select(model.id, model.storage_key, model.sha256))
                rows += [(f"{kind}:{row_id}", key, sha) for row_id, key, sha in result]
        return rows
    finally:
        await engine.dispose()


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description="Copy uploaded files to Supabase Storage (D-88)")
    parser.add_argument("--apply", action="store_true", help="copy (default: dry run)")
    parser.add_argument(
        "--local-root", action="append", default=[], type=Path, help="extra local storage root"
    )
    args = parser.parse_args(argv)
    settings = get_settings()
    if settings.supabase_url is None or settings.supabase_service_key is None:
        print("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set.")  # noqa: T201
        return 2
    target = SupabaseFileStorage(
        settings.supabase_url,
        settings.supabase_service_key.get_secret_value(),
        settings.supabase_storage_bucket,
    )
    report = Report()
    try:
        if args.apply:
            created = target.ensure_private_bucket()
            print(f"bucket={settings.supabase_storage_bucket} created={created} private=True")  # noqa: T201
        roots = local_roots(settings, args.local_root)
        print(f"local roots with uploads: {len(roots)}")  # noqa: T201
        for label, key, sha256 in asyncio.run(_rows(settings)):
            try:
                migrate_row(label, key, sha256, roots, target, args.apply, report)
            except StorageError as exc:
                report.failed.append(f"{label} ({exc})")
    finally:
        target.close()
    print(  # noqa: T201
        f"apply={args.apply} rows={report.rows} copied={report.copied} "
        f"already_present={report.already_present} verified={report.verified} "
        f"missing_locally={len(report.missing_locally)} hash_mismatch={len(report.hash_mismatch)} "
        f"failed={len(report.failed)}"
    )
    for label in report.missing_locally:
        print(f"missing locally {label}")  # noqa: T201
    for label in report.hash_mismatch:
        print(f"hash mismatch {label}")  # noqa: T201
    for label in report.failed:
        print(f"failed {label}")  # noqa: T201
    problems = report.missing_locally or report.hash_mismatch or report.failed
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
