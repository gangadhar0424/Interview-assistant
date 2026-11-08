"""Shared test fixtures.

Two kinds of tests exist:
- Infrastructure-free tests (health, config): settings point at closed ports and fake secrets.
- Database tests (auth): run against a dedicated `<db>_test` database on the compose Postgres,
  dropped and re-created from the Alembic migrations once per test session. The development
  database is never touched. Requires `docker compose up -d --wait` (or a CI Postgres service).
"""

import asyncio
import os
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from pathlib import Path
from typing import Any

import pytest
from alembic.config import Config
from dotenv import dotenv_values
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command
from app.core.config import REPO_ROOT, get_settings
from app.main import create_app
from tests.helpers import RecordingDispatcher

API_DIR = Path(__file__).resolve().parents[1]
TEST_JWT_SECRET = "test-only-jwt-secret-0123456789abcdef0123456789"  # noqa: S105

# Captured at import time, before any fake values are applied below.
_REAL_DB_URL = os.environ.get("DATABASE_URL") or dotenv_values(REPO_ROOT / ".env").get(
    "DATABASE_URL"
)


@pytest.fixture(autouse=True)
def _base_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deterministic fake settings; never read a developer's real .env values."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@127.0.0.1:1/db")
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("JWT_SECRET", TEST_JWT_SECRET)
    monkeypatch.setenv("ACCESS_TOKEN_TTL_MINUTES", "15")
    monkeypatch.setenv("REFRESH_TOKEN_TTL_DAYS", "14")
    monkeypatch.setenv("CORS_ORIGINS", "http://localhost:3000")
    get_settings.cache_clear()


def _test_db_url() -> str:
    if not _REAL_DB_URL:
        pytest.fail("DATABASE_URL is not set (and no .env found); cannot run database tests")
    url = make_url(_REAL_DB_URL)
    return url.set(database=f"{url.database}_test").render_as_string(hide_password=False)


async def _recreate_database(admin_url: str, db_name: str) -> None:
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)'))
            await conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def test_db_url() -> Iterator[str]:
    """Create a clean test database and migrate it from scratch (proves clean-DB migrations)."""
    url = _test_db_url()
    parsed = make_url(url)
    assert parsed.database is not None and parsed.database.endswith("_test")
    admin_url = parsed.set(database="postgres").render_as_string(hide_password=False)
    asyncio.run(_recreate_database(admin_url, parsed.database))

    saved = {k: os.environ.get(k) for k in ("DATABASE_URL", "REDIS_URL", "JWT_SECRET")}
    os.environ.update(
        DATABASE_URL=url, REDIS_URL="redis://127.0.0.1:1/0", JWT_SECRET=TEST_JWT_SECRET
    )
    get_settings.cache_clear()
    try:
        cfg = Config(str(API_DIR / "alembic.ini"))
        cfg.set_main_option("script_location", str(API_DIR / "alembic"))
        command.upgrade(cfg, "head")
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        get_settings.cache_clear()
    yield url


RunSql = Callable[..., list[dict[str, Any]]]


@pytest.fixture
def run_sql(test_db_url: str) -> RunSql:
    """Execute SQL against the test database; returns rows as dicts for SELECTs."""

    def run(sql: str, **params: Any) -> list[dict[str, Any]]:
        async def go() -> list[dict[str, Any]]:
            engine = create_async_engine(test_db_url)
            try:
                async with engine.begin() as conn:
                    result = await conn.execute(text(sql), params)
                    return [dict(r._mapping) for r in result] if result.returns_rows else []
            finally:
                await engine.dispose()

        return asyncio.run(go())

    return run


@pytest.fixture
def storage_root(tmp_path: Path) -> Path:
    return tmp_path / "app-data"


@pytest.fixture
def make_client(
    test_db_url: str, run_sql: RunSql, monkeypatch: pytest.MonkeyPatch, storage_root: Path
) -> Iterator[Callable[..., TestClient]]:
    """Factory for apps wired to the clean test DB and a per-test temp storage root.

    Keyword arguments are applied as environment overrides (e.g. RESUME_MAX_UPLOAD_MB="1").
    """
    monkeypatch.setenv("DATABASE_URL", test_db_url)
    monkeypatch.setenv("FILE_STORAGE_ROOT", str(storage_root))
    run_sql("TRUNCATE users CASCADE")  # cascades to resumes/documents
    stack = ExitStack()

    def build(**env: str) -> TestClient:
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        get_settings.cache_clear()
        built = stack.enter_context(TestClient(create_app()))
        # Never touch Redis in tests: use a recording dispatcher (tests may swap or inspect it).
        built.app.state.dispatcher = RecordingDispatcher()  # type: ignore[attr-defined]
        return built

    yield build
    stack.close()


@pytest.fixture
def client(make_client: Callable[..., TestClient]) -> TestClient:
    """App wired to the clean test database with empty tables."""
    return make_client()
