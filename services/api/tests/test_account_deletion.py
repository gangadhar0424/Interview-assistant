"""SEC-13 Decision 4 (option 4d with 4a, D-85): POST /api/v1/auth/account/delete.

Runs against the real test database as the runtime role (row-level security applies), with real
file storage under a per-test root."""

import asyncio
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.lifecycle.account import CONFIRMATION_PHRASE
from app.lifecycle.ledger import DeletionLedger
from app.lifecycle.orphans import sweep
from app.lifecycle.recorder import DeletionRecorder
from app.qa.cache import AnswerCache, cache_key
from app.storage.local import LocalFileStorage
from tests.conftest import RunSql
from tests.helpers import PASSWORD, run_maintenance_async
from tests.lifecycle_helpers import USER_TABLES, Seeded, counts, seed_user, user_files
from tests.test_answer_cache_purge import FakeRedis

URL = "/api/v1/auth/account/delete"
LEDGER_KEY = "L" * 40


def delete_account(
    client: TestClient,
    headers: dict[str, str],
    password: str = PASSWORD,
    confirmation: str = CONFIRMATION_PHRASE,
    **extra: Any,
) -> Any:
    return client.post(
        URL, headers=headers, json={"password": password, "confirmation": confirmation, **extra}
    )


def attach(client: TestClient, tmp_path: Path) -> tuple[FakeRedis, AnswerCache, DeletionLedger]:
    redis = FakeRedis()
    cache = AnswerCache(redis, 300)
    ledger = DeletionLedger(tmp_path / "ledger", LEDGER_KEY)
    client.app.state.deletion_recorder = DeletionRecorder(cache, ledger)  # type: ignore[attr-defined]
    return redis, cache, ledger


def cached_entry(cache: AnswerCache, s: Seeded) -> str:
    key = cache_key(s.user_id, s.session_id, "q", "p", "v", [s.resume_id])
    asyncio.run(
        cache.put(
            key,
            {"text": "cached"},
            user_id=s.user_id,
            session_id=s.session_id,
            source_ids=[s.resume_id],
        )
    )
    return key


@pytest.fixture
def two_users(client: TestClient, run_sql: RunSql) -> tuple[Seeded, Seeded]:
    return seed_user(client, run_sql, "alice@example.com"), seed_user(
        client, run_sql, "bob@example.com"
    )


def test_deletion_removes_every_row_file_cache_entry_and_sign_in_of_one_user_only(
    client: TestClient,
    run_sql: RunSql,
    storage_root: Path,
    tmp_path: Path,
    two_users: tuple[Seeded, Seeded],
) -> None:
    alice, bob = two_users
    redis, cache, ledger = attach(client, tmp_path)
    alice_key, bob_key = cached_entry(cache, alice), cached_entry(cache, bob)
    before_alice, before_bob = counts(run_sql, alice.user_id), counts(run_sql, bob.user_id)
    assert all(n > 0 for n in before_alice.values()), before_alice  # every table was seeded
    assert user_files(storage_root, alice.user_id) and user_files(storage_root, bob.user_id)
    bob_files = sorted(user_files(storage_root, bob.user_id))

    response = delete_account(client, alice.headers)

    assert response.status_code == 204, response.text
    assert response.headers.get("cache-control") == "no-store"
    assert "refresh_token=" in response.headers.get("set-cookie", "")  # cookie cleared
    assert counts(run_sql, alice.user_id) == dict.fromkeys(USER_TABLES, 0)
    assert user_files(storage_root, alice.user_id) == []
    assert not (storage_root / "uploads" / str(alice.user_id)).exists()
    assert alice_key not in redis.keys()
    # The other user is untouched: rows, files and cache.
    assert counts(run_sql, bob.user_id) == before_bob
    assert sorted(user_files(storage_root, bob.user_id)) == bob_files
    assert bob_key in redis.keys()
    # One ledger entry, ids only.
    (entry,) = ledger.read().entries
    assert (entry.kind, entry.subject_id, entry.owner_user_id) == (
        "account",
        alice.user_id,
        alice.user_id,
    )


def test_after_deletion_the_old_token_login_and_a_repeat_request_all_fail(
    client: TestClient, two_users: tuple[Seeded, Seeded]
) -> None:
    alice, bob = two_users
    assert delete_account(client, alice.headers).status_code == 204
    assert client.get("/api/v1/auth/me", headers=alice.headers).status_code == 401
    assert delete_account(client, alice.headers).status_code == 401  # nothing left to delete
    login = client.post("/api/v1/auth/login", json={"email": alice.email, "password": PASSWORD})
    assert login.status_code == 401
    assert client.get("/api/v1/auth/me", headers=bob.headers).status_code == 200


def test_the_email_can_register_again_as_a_new_empty_account(
    client: TestClient, run_sql: RunSql, two_users: tuple[Seeded, Seeded]
) -> None:
    alice, _ = two_users
    assert delete_account(client, alice.headers).status_code == 204
    signup = client.post("/api/v1/auth/signup", json={"email": alice.email, "password": PASSWORD})
    assert signup.status_code == 201
    new_id = run_sql("SELECT id FROM users WHERE email = :e", e=alice.email)[0]["id"]
    assert new_id != alice.user_id
    assert counts(run_sql, new_id)["sessions"] == 0


@pytest.mark.parametrize(
    ("password", "confirmation", "status", "code"),
    [
        ("wrong password!", CONFIRMATION_PHRASE, 403, "password_incorrect"),
        (PASSWORD, "delete my account", 422, "confirmation_mismatch"),  # exact text only
        (PASSWORD, "", 422, "confirmation_mismatch"),
        (PASSWORD, CONFIRMATION_PHRASE + " ", 422, "confirmation_mismatch"),
    ],
)
def test_a_wrong_password_or_phrase_deletes_nothing(
    client: TestClient,
    run_sql: RunSql,
    storage_root: Path,
    two_users: tuple[Seeded, Seeded],
    password: str,
    confirmation: str,
    status: int,
    code: str,
) -> None:
    alice, _ = two_users
    before = counts(run_sql, alice.user_id)
    files = user_files(storage_root, alice.user_id)
    response = delete_account(client, alice.headers, password, confirmation)
    assert response.status_code == status and response.json()["error"]["code"] == code
    assert counts(run_sql, alice.user_id) == before
    assert user_files(storage_root, alice.user_id) == files
    assert password not in response.text


def test_malformed_requests_are_refused_without_effect(
    client: TestClient, run_sql: RunSql, two_users: tuple[Seeded, Seeded]
) -> None:
    alice, _ = two_users
    before = counts(run_sql, alice.user_id)
    assert client.post(URL, headers=alice.headers, json={"password": PASSWORD}).status_code == 422
    assert delete_account(client, alice.headers, user_id=str(uuid.uuid4())).status_code == 422
    assert (
        client.post(
            URL, json={"password": PASSWORD, "confirmation": CONFIRMATION_PHRASE}
        ).status_code
        == 401
    )
    assert delete_account(client, {"Authorization": "Bearer not-a-token"}).status_code == 401
    assert counts(run_sql, alice.user_id) == before


def test_a_user_can_only_ever_delete_their_own_account(
    client: TestClient, run_sql: RunSql, two_users: tuple[Seeded, Seeded]
) -> None:
    """There is no target parameter: Bob's token with Bob's password deletes Bob, never Alice, and
    Bob's token with Alice's password (identical here) still deletes only Bob."""
    alice, bob = two_users
    before_alice = counts(run_sql, alice.user_id)
    assert delete_account(client, bob.headers).status_code == 204
    assert counts(run_sql, alice.user_id) == before_alice
    assert counts(run_sql, bob.user_id)["users"] == 0


def test_repeated_wrong_passwords_are_throttled(
    make_client: Callable[..., TestClient], run_sql: RunSql
) -> None:
    client = make_client(LOGIN_FAILURES_PER_EMAIL="3")
    alice = seed_user(client, run_sql, "alice@example.com")
    for _ in range(3):
        assert delete_account(client, alice.headers, "wrong password!").status_code == 403
    throttled = delete_account(client, alice.headers)  # even the right password now waits
    assert throttled.status_code == 429 and "retry-after" in throttled.headers
    assert counts(run_sql, alice.user_id)["users"] == 1


def test_a_file_removal_failure_still_deletes_the_account_and_the_sweep_finishes_it(
    client: TestClient,
    run_sql: RunSql,
    storage_root: Path,
    tmp_path: Path,
    test_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
    two_users: tuple[Seeded, Seeded],
) -> None:
    alice, bob = two_users
    attach(client, tmp_path)

    def broken(self: LocalFileStorage, user_id: uuid.UUID) -> int:
        raise OSError("disk unavailable")

    monkeypatch.setattr(LocalFileStorage, "delete_user_tree", broken)
    assert delete_account(client, alice.headers).status_code == 204
    assert counts(run_sql, alice.user_id) == dict.fromkeys(USER_TABLES, 0)
    leftover = user_files(storage_root, alice.user_id)
    assert leftover  # the failure left the files behind
    monkeypatch.undo()

    storage = LocalFileStorage(storage_root)
    report = run_maintenance_async(
        test_db_url, lambda sessions: sweep(sessions, storage, min_age_seconds=0, apply=True)
    )
    assert report.orphan_files_removed == len(leftover)
    assert report.orphan_owner_ids == [str(alice.user_id)]
    assert user_files(storage_root, alice.user_id) == []
    assert user_files(storage_root, bob.user_id)  # never another user's files


def test_a_ledger_failure_does_not_undo_or_fail_the_deletion(
    client: TestClient, run_sql: RunSql, tmp_path: Path, two_users: tuple[Seeded, Seeded]
) -> None:
    alice, _ = two_users
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x")
    client.app.state.deletion_recorder = DeletionRecorder(  # type: ignore[attr-defined]
        None, DeletionLedger(blocker, LEDGER_KEY)
    )
    assert delete_account(client, alice.headers).status_code == 204
    assert counts(run_sql, alice.user_id)["users"] == 0


def test_every_user_owned_table_cascades_from_users(run_sql: RunSql) -> None:
    """A new table with a `user_id` column must cascade from `users`, or account deletion would
    leave its rows behind. `session_preferences` has no `user_id` and cascades via sessions."""
    tables = {
        r["table_name"]
        for r in run_sql(
            "SELECT table_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND column_name = 'user_id'"
        )
    }
    cascading = {
        r["table"]
        for r in run_sql(
            "SELECT c.conrelid::regclass::text AS table FROM pg_constraint c "
            "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey) "
            "WHERE c.contype = 'f' AND c.confrelid = 'users'::regclass "
            "AND c.confdeltype = 'c' AND a.attname = 'user_id'"
        )
    }
    assert tables == cascading, f"tables with user_id but no cascade: {tables - cascading}"
    assert set(USER_TABLES) - {"users", "session_preferences"} == tables
