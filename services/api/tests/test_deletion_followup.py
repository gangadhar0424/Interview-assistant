"""SEC-13 (D-85): after a session, resume or document deletion commits, its cached answers are
purged and a ledger entry is written; neither step can fail the deletion; and the ledger replay
re-deletes everything after a restore, idempotently, without touching another user."""

import asyncio
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.lifecycle.ledger import DeletionLedger
from app.lifecycle.orphans import sweep
from app.lifecycle.recorder import DeletionRecorder
from app.lifecycle.replay import replay
from app.qa.cache import AnswerCache, cache_key
from app.storage.local import LocalFileStorage
from tests.conftest import RunSql
from tests.helpers import run_async, run_maintenance_async
from tests.lifecycle_helpers import USER_TABLES, Seeded, counts, seed_user, user_files
from tests.test_answer_cache_purge import FakeRedis
from tests.test_files import upload

LEDGER_KEY = "L" * 40


def attach(client: TestClient, tmp_path: Path) -> tuple[FakeRedis, AnswerCache, DeletionLedger]:
    redis = FakeRedis()
    cache = AnswerCache(redis, 300)
    ledger = DeletionLedger(tmp_path / "ledger", LEDGER_KEY)
    client.app.state.deletion_recorder = DeletionRecorder(cache, ledger)  # type: ignore[attr-defined]
    return redis, cache, ledger


def cached(cache: AnswerCache, s: Seeded, session: uuid.UUID, sources: list[uuid.UUID]) -> str:
    key = cache_key(s.user_id, session, str(uuid.uuid4()), "p", "v", sources)
    asyncio.run(
        cache.put(key, {"text": "x"}, user_id=s.user_id, session_id=session, source_ids=sources)
    )
    return key


def test_deleting_a_session_purges_its_cache_and_writes_one_ledger_entry(
    client: TestClient, run_sql: RunSql, tmp_path: Path
) -> None:
    alice = seed_user(client, run_sql, "alice@example.com")
    bob = seed_user(client, run_sql, "bob@example.com")
    redis, cache, ledger = attach(client, tmp_path)
    gone = cached(cache, alice, alice.session_id, [alice.resume_id])
    other = cached(cache, bob, bob.session_id, [bob.resume_id])
    r = client.delete(f"/api/v1/sessions/{alice.session_id}", headers=alice.headers)
    assert r.status_code == 204
    assert gone not in redis.keys() and other in redis.keys()
    (entry,) = ledger.read().entries
    assert (entry.kind, entry.subject_id, entry.owner_user_id) == (
        "session",
        alice.session_id,
        alice.user_id,
    )


def test_deleting_a_document_purges_answers_that_used_it(
    client: TestClient, run_sql: RunSql, tmp_path: Path
) -> None:
    alice = seed_user(client, run_sql, "alice@example.com")
    redis, cache, ledger = attach(client, tmp_path)
    used = cached(cache, alice, alice.session_id, [alice.resume_id, alice.document_id])
    unrelated = cached(cache, alice, uuid.uuid4(), [uuid.uuid4()])
    r = client.delete(f"/api/v1/documents/{alice.document_id}", headers=alice.headers)
    assert r.status_code == 204
    assert used not in redis.keys() and unrelated in redis.keys()
    (entry,) = ledger.read().entries
    assert (entry.kind, entry.subject_id) == ("document", alice.document_id)


def test_a_refused_deletion_writes_nothing(
    client: TestClient, run_sql: RunSql, tmp_path: Path
) -> None:
    alice = seed_user(client, run_sql, "alice@example.com")
    bob = seed_user(client, run_sql, "bob@example.com")
    redis, cache, ledger = attach(client, tmp_path)
    kept = cached(cache, alice, alice.session_id, [alice.resume_id])
    # Bob cannot delete Alice's session or resume: 404, no purge, no ledger entry.
    assert (
        client.delete(f"/api/v1/sessions/{alice.session_id}", headers=bob.headers).status_code
        == 404
    )
    assert (
        client.delete(f"/api/v1/resumes/{alice.resume_id}", headers=bob.headers).status_code == 404
    )
    assert kept in redis.keys()
    assert ledger.read().entries == []


def test_a_cache_or_ledger_failure_never_fails_the_deletion(
    client: TestClient, run_sql: RunSql, tmp_path: Path
) -> None:
    alice = seed_user(client, run_sql, "alice@example.com")
    redis = FakeRedis()
    redis.fail = True
    blocker = tmp_path / "file-not-dir"
    blocker.write_text("x")
    client.app.state.deletion_recorder = DeletionRecorder(  # type: ignore[attr-defined]
        AnswerCache(redis, 300), DeletionLedger(blocker, LEDGER_KEY)
    )
    assert (
        client.delete(f"/api/v1/sessions/{alice.session_id}", headers=alice.headers).status_code
        == 204
    )
    assert counts(run_sql, alice.user_id)["sessions"] == 0


# --------------------------------------------------------------------- replay after a restore


def test_replay_deletes_again_everything_the_ledger_names_and_is_idempotent(
    client: TestClient, run_sql: RunSql, storage_root: Path, tmp_path: Path, test_db_url: str
) -> None:
    """Simulates a restore: the objects exist again (as the backup had them) while the ledger,
    kept outside the database, says they were deleted afterwards."""
    alice = seed_user(client, run_sql, "alice@example.com")
    carol = seed_user(client, run_sql, "carol@example.com")
    bob = seed_user(client, run_sql, "bob@example.com")
    before_bob = counts(run_sql, bob.user_id)
    bob_files = sorted(user_files(storage_root, bob.user_id))
    ledger = DeletionLedger(tmp_path / "ledger", LEDGER_KEY)
    ledger.append("session", alice.session_id, alice.user_id)
    ledger.append("document", alice.document_id, alice.user_id)
    ledger.append("account", carol.user_id, carol.user_id)
    storage = LocalFileStorage(storage_root)
    settings = get_settings()

    dry = run_async(
        test_db_url,
        lambda s: replay(s, storage, settings, ledger.read().entries, apply=False),
    )
    assert dry.outcomes == {"would_delete": 3}
    assert counts(run_sql, carol.user_id)["users"] == 1  # a dry run changes nothing

    report = run_async(
        test_db_url,
        lambda s: replay(s, storage, settings, ledger.read().entries, apply=True),
    )
    assert report.ok and report.outcomes == {"deleted": 3}
    assert counts(run_sql, alice.user_id)["sessions"] == 0
    assert counts(run_sql, alice.user_id)["documents"] == 0
    assert counts(run_sql, alice.user_id)["resumes"] == 1  # not named in the ledger
    assert counts(run_sql, carol.user_id) == dict.fromkeys(USER_TABLES, 0)
    assert user_files(storage_root, carol.user_id) == []
    assert counts(run_sql, bob.user_id) == before_bob
    assert sorted(user_files(storage_root, bob.user_id)) == bob_files

    again = run_async(
        test_db_url,
        lambda s: replay(s, storage, settings, ledger.read().entries, apply=True),
    )
    assert again.ok and again.outcomes == {"absent": 3}


def test_replay_never_applies_an_altered_entry_and_reports_it(
    client: TestClient, run_sql: RunSql, storage_root: Path, tmp_path: Path, test_db_url: str
) -> None:
    alice = seed_user(client, run_sql, "alice@example.com")
    bob = seed_user(client, run_sql, "bob@example.com")
    ledger = DeletionLedger(tmp_path / "ledger", LEDGER_KEY)
    ledger.append("session", alice.session_id, alice.user_id)
    path = next((tmp_path / "ledger").glob("*.jsonl"))
    # An attacker rewrites the entry to point at Bob's account.
    path.write_text(
        path.read_text(encoding="utf-8")
        .replace(str(alice.session_id), str(bob.user_id))
        .replace('"session"', '"account"')
        .replace(str(alice.user_id), str(bob.user_id)),
        encoding="utf-8",
    )
    read = ledger.read()
    report = run_async(
        test_db_url,
        lambda s: replay(
            s,
            LocalFileStorage(storage_root),
            get_settings(),
            read.entries,
            apply=True,
            invalid_lines=read.invalid_lines,
        ),
    )
    assert not report.ok and report.invalid_lines == 1 and report.outcomes == {}
    assert counts(run_sql, bob.user_id)["users"] == 1


def test_replay_cannot_cross_tenants_even_with_a_valid_entry(
    client: TestClient, run_sql: RunSql, storage_root: Path, tmp_path: Path, test_db_url: str
) -> None:
    """A correctly signed entry that names Bob's session under Alice's id deletes nothing: the
    replay runs under the entry owner's tenant context and matches on owner and id."""
    alice = seed_user(client, run_sql, "alice@example.com")
    bob = seed_user(client, run_sql, "bob@example.com")
    ledger = DeletionLedger(tmp_path / "ledger", LEDGER_KEY)
    ledger.append("session", bob.session_id, alice.user_id)
    ledger.append("resume", bob.resume_id, alice.user_id)
    report = run_async(
        test_db_url,
        lambda s: replay(
            s, LocalFileStorage(storage_root), get_settings(), ledger.read().entries, apply=True
        ),
    )
    assert report.outcomes == {"absent": 2}
    assert counts(run_sql, bob.user_id)["sessions"] == 1
    assert counts(run_sql, bob.user_id)["resumes"] == 1


# ------------------------------------------------------------------------- orphan sweep


def test_the_sweep_finds_both_kinds_and_removes_only_old_unreferenced_files(
    client: TestClient, run_sql: RunSql, storage_root: Path, test_db_url: str
) -> None:
    alice = seed_user(client, run_sql, "alice@example.com")
    bob = seed_user(client, run_sql, "bob@example.com")
    storage = LocalFileStorage(storage_root)
    # Orphan file: a stored file of Alice's that no row names.
    orphan = storage_root / "uploads" / str(alice.user_id) / "documents" / ("a" * 32)
    orphan.write_bytes(b"left behind")
    # Missing file: Bob's resume row whose file is gone.
    bob_resume_key = run_sql("SELECT storage_key FROM resumes WHERE id = :i", i=bob.resume_id)[0][
        "storage_key"
    ]
    (storage_root / bob_resume_key).unlink()
    # Malformed: a name this storage never writes (reported, never removed).
    odd = storage_root / "uploads" / str(alice.user_id) / "documents" / "notes.txt"
    odd.write_bytes(b"?")
    valid = sorted(p for p in user_files(storage_root, alice.user_id) if p not in (orphan, odd))

    dry = run_maintenance_async(
        test_db_url, lambda s: sweep(s, storage, min_age_seconds=0, apply=False)
    )
    assert dry.orphan_files == 1 and dry.orphan_files_removed == 0
    assert dry.orphan_owner_ids == [str(alice.user_id)]
    assert dry.missing_files == [f"resume:{bob.resume_id}"]
    assert dry.malformed_entries == 1
    assert orphan.exists()  # a dry run removes nothing

    young = run_maintenance_async(
        test_db_url, lambda s: sweep(s, storage, min_age_seconds=3600, apply=True)
    )
    assert young.orphan_files == 0 and young.orphan_files_kept_young == 1 and orphan.exists()

    done = run_maintenance_async(
        test_db_url, lambda s: sweep(s, storage, min_age_seconds=0, apply=True)
    )
    assert done.orphan_files_removed == 1 and not orphan.exists()
    assert odd.exists()
    assert sorted(p for p in user_files(storage_root, alice.user_id) if p != odd) == valid
    assert counts(run_sql, bob.user_id)["resumes"] == 1  # rows are never changed

    again = run_maintenance_async(
        test_db_url, lambda s: sweep(s, storage, min_age_seconds=0, apply=True)
    )
    assert again.orphan_files == 0 and again.errors == 0  # retry-safe


def test_a_file_whose_row_appears_during_the_sweep_is_kept(
    client: TestClient,
    run_sql: RunSql,
    storage_root: Path,
    test_db_url: str,
    monkeypatch: object,
) -> None:
    """Race: the sweep lists keys, an upload then commits its row, and the sweep reaches the file.
    The per-file re-check right before removal must keep it."""
    import pytest

    from app.lifecycle import orphans

    assert isinstance(monkeypatch, pytest.MonkeyPatch)
    alice = seed_user(client, run_sql, "alice@example.com")
    storage = LocalFileStorage(storage_root)
    real_all_keys = orphans._all_keys

    async def stale_listing(session: object) -> set[str]:
        keys = await real_all_keys(session)  # type: ignore[arg-type]
        # Pretend the newest upload's row had not been committed when the listing was taken.
        response = upload(
            client, "documents", alice.headers, "late.txt", b"late upload", "text/plain"
        )
        assert response.status_code == 201
        return keys

    monkeypatch.setattr(orphans, "_all_keys", stale_listing)
    report = run_maintenance_async(
        test_db_url, lambda s: sweep(s, storage, min_age_seconds=0, apply=True)
    )
    assert report.orphan_files == 1 and report.orphan_files_rescued == 1
    assert report.orphan_files_removed == 0
    late_key = run_sql(
        "SELECT storage_key FROM documents WHERE user_id = :u ORDER BY created_at DESC LIMIT 1",
        u=alice.user_id,
    )[0]["storage_key"]
    assert (storage_root / late_key).exists()
