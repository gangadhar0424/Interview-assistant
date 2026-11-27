"""Failure injection for M-12 (D-79): the backend must fail safely and recover.

Real Postgres (the throwaway `<db>_test` database) is used wherever the failure is a database
behaviour (pool exhaustion, timeouts, rollback, interrupted migration). The default test environment
points DATABASE_URL and REDIS_URL at a closed port, which is the "dependency down" case.
"""

import asyncio
import errno
import socket
import time
from pathlib import Path

import pytest
import redis.exceptions as redis_exc
import sqlalchemy.exc as sa_exc
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from alembic import command
from app.core.config import get_settings
from app.core.errors import is_infrastructure_failure
from app.db.session import create_engine, engine_options
from app.main import create_app
from tests.conftest import API_DIR, TEST_JWT_SECRET, _recreate_database

# ---------------------------------------------------------------------------- classification


class _FakeOrig(Exception):
    def __init__(self, sqlstate: str | None = None) -> None:
        super().__init__("driver detail that must never reach a client")
        self.sqlstate = sqlstate


def _dbapi(
    cls: type[sa_exc.DBAPIError], sqlstate: str | None = None, invalidated: bool = False
) -> Exception:
    error = cls(
        "SELECT secret FROM users WHERE email = 'a@b.c'", {"p": "user text"}, _FakeOrig(sqlstate)
    )
    error.connection_invalidated = invalidated
    return error


@pytest.mark.parametrize(
    "exc",
    [
        _dbapi(sa_exc.OperationalError),
        _dbapi(sa_exc.InterfaceError),
        _dbapi(sa_exc.DBAPIError, invalidated=True),
        _dbapi(sa_exc.DBAPIError, sqlstate="57014"),  # query cancelled: statement timeout
        _dbapi(sa_exc.DBAPIError, sqlstate="53300"),  # too many connections
        _dbapi(sa_exc.DBAPIError, sqlstate="57P01"),  # server shutting down
        _dbapi(sa_exc.DBAPIError, sqlstate="40P01"),  # deadlock detected
        sa_exc.TimeoutError("QueuePool limit of size 1 overflow 0 reached"),
        redis_exc.ConnectionError("redis down"),
        redis_exc.TimeoutError("redis slow"),
        ConnectionRefusedError("refused"),
        socket.gaierror(-3, "Temporary failure in name resolution"),
        socket.herror(1, "Unknown host"),
        OSError(errno.ENETUNREACH, "Network is unreachable"),
        OSError(errno.EHOSTUNREACH, "No route to host"),
        OSError(errno.ETIMEDOUT, "Connection timed out"),
        TimeoutError("slow"),
        TimeoutError(),
    ],
)
def test_dependency_failures_are_classified_as_unavailable(exc: Exception) -> None:
    assert is_infrastructure_failure(exc) is True


@pytest.mark.parametrize(
    "exc",
    [
        _dbapi(
            sa_exc.IntegrityError, sqlstate="23505"
        ),  # a unique violation is a bug or a conflict
        _dbapi(sa_exc.DBAPIError, sqlstate="42P01"),  # undefined table
        ValueError("bad"),
        KeyError("x"),
        FileNotFoundError("missing upload"),
        PermissionError("no access to the storage volume"),
        OSError(errno.ENOSPC, "No space left on device"),
        RuntimeError("bug"),
        redis_exc.ResponseError("WRONGTYPE"),
    ],
)
def test_real_bugs_are_not_hidden_as_outages(exc: Exception) -> None:
    assert is_infrastructure_failure(exc) is False


# ---------------------------------------------------------------------------- HTTP behaviour


def _client() -> TestClient:
    get_settings.cache_clear()
    return TestClient(create_app(), raise_server_exceptions=False)


def test_database_down_returns_a_safe_503_not_a_stack_trace(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with _client() as client:
        response = client.post(
            "/api/v1/auth/login", json={"email": "a@example.com", "password": "x" * 12}
        )
    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"
    assert response.headers["x-request-id"]
    body = response.json()
    assert body["error"]["code"] == "service_unavailable"
    raw = response.text
    for leaked in (
        "asyncpg",
        "sqlalchemy",
        "127.0.0.1",
        "Traceback",
        "SELECT",
        "postgresql",
        "password",
    ):
        assert leaked not in raw, leaked
    # the log keeps the class only, never the driver message (which can carry SQL parameters)
    captured = capsys.readouterr()
    logged = captured.out + captured.err
    assert "unhandled error" in logged and "ConnectionRefusedError" in logged
    assert "a@example.com" not in logged and "SELECT" not in logged


def test_unexpected_errors_return_a_fixed_500_and_log_only_the_class(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with _client() as client:

        @client.app.get("/__boom")  # type: ignore[attr-defined,untyped-decorator]
        async def boom() -> None:
            raise RuntimeError("secret resume text: Jane Doe, 12 Main St")

        response = client.get("/__boom")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert (
        "Jane Doe" not in response.text
        and "resume" not in response.text
        and "Traceback" not in response.text
    )
    assert response.headers["x-request-id"]
    assert "x-content-type-options" in response.headers  # security headers still applied
    captured = capsys.readouterr()
    logged = captured.out + captured.err
    assert "RuntimeError" in logged and "Jane Doe" not in logged and "12 Main St" not in logged


def test_health_stays_up_and_ready_reports_unavailable_without_dependencies() -> None:
    with _client() as client:
        assert client.get("/health").status_code == 200
        ready = client.get("/ready")
    assert ready.status_code == 503
    assert "127.0.0.1" not in ready.text and "asyncpg" not in ready.text


def test_api_documentation_is_served_locally_and_can_be_switched_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _client() as client:
        assert client.get("/openapi.json").status_code == 200
        assert client.get("/docs").status_code == 200
    monkeypatch.setenv("API_DOCS_ENABLED", "false")
    with _client() as client:
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert client.get(path).status_code == 404, path


# ------------------------------------------------------------- real database behaviour


def _engine(monkeypatch: pytest.MonkeyPatch, test_db_url: str, **env: str) -> AsyncEngine:
    monkeypatch.setenv("DATABASE_URL", test_db_url)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    settings = get_settings()
    return create_engine(settings.database_url, settings)


def test_engine_options_come_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_POOL_SIZE", "3")
    monkeypatch.setenv("DB_MAX_OVERFLOW", "1")
    monkeypatch.setenv("DB_STATEMENT_TIMEOUT_MS", "15000")
    get_settings.cache_clear()
    options = engine_options(get_settings())
    assert (
        options["pool_size"] == 3
        and options["max_overflow"] == 1
        and options["pool_pre_ping"] is True
    )
    server = options["connect_args"]["server_settings"]  # type: ignore[index]
    assert server["statement_timeout"] == "15000"
    assert server["idle_in_transaction_session_timeout"] == "300000"
    assert server["application_name"] == "interview-assistant"


@pytest.mark.parametrize(
    "env",
    [
        {"DB_POOL_SIZE": "0"},
        {"DB_POOL_SIZE": "1000"},
        {"DB_MAX_OVERFLOW": "-1"},
        {"DB_POOL_TIMEOUT_SECONDS": "0"},
        {"DB_CONNECT_TIMEOUT_SECONDS": "0"},
        {"DB_STATEMENT_TIMEOUT_MS": "10"},
        {"DB_STATEMENT_TIMEOUT_MS": "99999999"},
        {"DB_IDLE_IN_TRANSACTION_TIMEOUT_MS": "10"},
        {"DB_POOL_RECYCLE_SECONDS": "1"},
        {"DB_CONNECT_TIMEOUT_SECONDS": "30", "DB_POOL_TIMEOUT_SECONDS": "1"},
        {"DB_STATEMENT_TIMEOUT_MS": "600000", "DB_IDLE_IN_TRANSACTION_TIMEOUT_MS": "1000"},
    ],
)
def test_unreasonable_database_limits_are_rejected_at_startup(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str]
) -> None:
    from app.core.config import ConfigurationError, load_settings

    monkeypatch.setitem(
        __import__("app.core.config", fromlist=["Settings"]).Settings.model_config, "env_file", None
    )
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(ConfigurationError):
        load_settings()


def test_pool_exhaustion_fails_fast_and_recovers(
    monkeypatch: pytest.MonkeyPatch, test_db_url: str
) -> None:
    async def go() -> None:
        engine = _engine(
            monkeypatch,
            test_db_url,
            DB_POOL_SIZE="1",
            DB_MAX_OVERFLOW="0",
            DB_POOL_TIMEOUT_SECONDS="0.5",
            DB_CONNECT_TIMEOUT_SECONDS="2",
        )
        try:
            async with engine.connect() as held:
                await held.execute(text("SELECT 1"))
                started = time.perf_counter()
                with pytest.raises(sa_exc.TimeoutError) as raised:
                    async with engine.connect():
                        pass
                waited = time.perf_counter() - started
                assert 0.4 <= waited < 3.0, waited  # bounded wait, not a hang
                assert is_infrastructure_failure(raised.value)
            async with (
                engine.connect() as again
            ):  # the pool recovered once the connection came back
                assert (await again.execute(text("SELECT 1"))).scalar_one() == 1
        finally:
            await engine.dispose()

    asyncio.run(go())


def test_statement_timeout_cancels_a_runaway_query_and_the_connection_stays_usable(
    monkeypatch: pytest.MonkeyPatch, test_db_url: str
) -> None:
    async def go() -> None:
        engine = _engine(monkeypatch, test_db_url, DB_STATEMENT_TIMEOUT_MS="1000")
        try:
            started = time.perf_counter()
            async with engine.connect() as conn:
                with pytest.raises(sa_exc.DBAPIError) as raised:
                    await conn.execute(text("SELECT pg_sleep(5)"))
                assert time.perf_counter() - started < 4.0
                assert is_infrastructure_failure(raised.value)
                await conn.rollback()
                assert (await conn.execute(text("SELECT 1"))).scalar_one() == 1
        finally:
            await engine.dispose()

    asyncio.run(go())


def test_idle_in_transaction_connections_are_terminated_by_the_server(
    monkeypatch: pytest.MonkeyPatch, test_db_url: str
) -> None:
    async def go() -> None:
        engine = _engine(
            monkeypatch,
            test_db_url,
            DB_IDLE_IN_TRANSACTION_TIMEOUT_MS="1000",
            DB_STATEMENT_TIMEOUT_MS="1000",
        )
        try:
            conn = await engine.connect()
            await conn.execute(text("SELECT 1"))  # opens a transaction and leaves it open
            await asyncio.sleep(2.0)
            with pytest.raises(sa_exc.DBAPIError) as raised:
                await conn.execute(text("SELECT 1"))
            assert is_infrastructure_failure(raised.value)
        finally:
            await engine.dispose()
        # a fresh connection from the same engine settings works
        engine2 = _engine(monkeypatch, test_db_url)
        try:
            async with engine2.connect() as conn2:
                assert (await conn2.execute(text("SELECT 1"))).scalar_one() == 1
        finally:
            await engine2.dispose()

    asyncio.run(go())


def test_a_failed_transaction_leaves_no_partial_changes(
    monkeypatch: pytest.MonkeyPatch, test_db_url: str
) -> None:
    async def go() -> None:
        engine = _engine(monkeypatch, test_db_url)
        try:
            with pytest.raises(RuntimeError):
                async with engine.begin() as conn:
                    await conn.execute(text("CREATE TABLE m12_tx_probe (id int)"))
                    await conn.execute(text("INSERT INTO m12_tx_probe VALUES (1)"))
                    raise RuntimeError("fail after writing")
            async with engine.connect() as conn:
                exists = (
                    await conn.execute(text("SELECT to_regclass('public.m12_tx_probe')"))
                ).scalar_one()
                assert exists is None  # even the DDL was rolled back
        finally:
            await engine.dispose()

    asyncio.run(go())


# ---------------------------------------------------------------------------- migrations

_PROBE_ENV = """
import asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from alembic import context

URL = context.config.get_main_option("sqlalchemy.url")

def _do_run(connection):
    # The same shape as alembic/env.py: ONE transaction around the whole upgrade.
    context.configure(connection=connection, version_table="alembic_version_probe")
    with context.begin_transaction():
        context.run_migrations()

async def main():
    engine = create_async_engine(URL)
    async with engine.connect() as connection:
        await connection.run_sync(_do_run)
    await engine.dispose()

asyncio.run(main())
"""


def _write_probe_scripts(directory: Path) -> None:
    versions = directory / "versions"
    versions.mkdir(parents=True)
    (directory / "env.py").write_text(_PROBE_ENV, encoding="utf-8")
    (directory / "script.py.mako").write_text(
        '"""${message}"""\nrevision = ${repr(up_revision)}\n'
        "down_revision = ${repr(down_revision)}\n"
        "branch_labels = None\ndepends_on = None\n\n"
        "def upgrade():\n    pass\n\ndef downgrade():\n    pass\n",
        encoding="utf-8",
    )
    (versions / "p001_first.py").write_text(
        'revision = "p001"\ndown_revision = None\nbranch_labels = None\ndepends_on = None\n'
        "from alembic import op\n\n"
        'def upgrade():\n    op.execute("CREATE TABLE m12_probe_one (id int)")\n\n'
        'def downgrade():\n    op.execute("DROP TABLE m12_probe_one")\n',
        encoding="utf-8",
    )
    (versions / "p002_breaks.py").write_text(
        'revision = "p002"\ndown_revision = "p001"\nbranch_labels = None\ndepends_on = None\n'
        "from alembic import op\n\n"
        'def upgrade():\n    op.execute("CREATE TABLE m12_probe_two (id int)")\n'
        '    raise RuntimeError("interrupted mid-migration")\n\n'
        "def downgrade():\n    pass\n",
        encoding="utf-8",
    )


def test_an_interrupted_migration_leaves_the_database_unchanged(
    test_db_url: str, tmp_path: Path
) -> None:
    """The whole upgrade is one transaction (as in alembic/env.py): a failure rolls back every step
    of that run, including steps that had already succeeded, and the version table does not move."""
    scripts = tmp_path / "probe-migrations"
    _write_probe_scripts(scripts)
    cfg = Config()
    cfg.set_main_option("script_location", str(scripts))
    cfg.set_main_option("sqlalchemy.url", test_db_url)

    async def state() -> tuple[object, object, list[str]]:
        engine = create_async_engine(test_db_url)
        try:
            async with engine.connect() as conn:
                one = (
                    await conn.execute(text("SELECT to_regclass('public.m12_probe_one')"))
                ).scalar_one()
                two = (
                    await conn.execute(text("SELECT to_regclass('public.m12_probe_two')"))
                ).scalar_one()
                table = (
                    await conn.execute(text("SELECT to_regclass('public.alembic_version_probe')"))
                ).scalar_one()
                versions: list[str] = []
                if table is not None:
                    rows = await conn.execute(text("SELECT version_num FROM alembic_version_probe"))
                    versions = [r[0] for r in rows]
                return one, two, versions
        finally:
            await engine.dispose()

    with pytest.raises(RuntimeError, match="interrupted mid-migration"):
        command.upgrade(cfg, "head")
    one, two, versions = asyncio.run(state())
    assert one is None and two is None and versions == []  # nothing from the failed run survived

    # fixing the broken step and re-running applies cleanly (the failure was safely retryable)
    (scripts / "versions" / "p002_breaks.py").write_text(
        'revision = "p002"\ndown_revision = "p001"\nbranch_labels = None\ndepends_on = None\n'
        "from alembic import op\n\n"
        'def upgrade():\n    op.execute("CREATE TABLE m12_probe_two (id int)")\n\n'
        'def downgrade():\n    op.execute("DROP TABLE m12_probe_two")\n',
        encoding="utf-8",
    )
    command.upgrade(cfg, "head")
    one, two, versions = asyncio.run(state())
    assert one is not None and two is not None and versions == ["p002"]
    command.downgrade(cfg, "base")  # leave the shared test database as found
    assert asyncio.run(state())[:2] == (None, None)


def test_the_real_migration_chain_upgrades_downgrades_and_upgrades_again(
    monkeypatch: pytest.MonkeyPatch, test_db_url: str
) -> None:
    """head -> base -> head on a scratch database: reversible and re-appliable."""
    parsed = make_url(test_db_url)
    scratch_name = f"{parsed.database}_migrations"
    admin_url = parsed.set(database="postgres").render_as_string(hide_password=False)
    scratch_url = parsed.set(database=scratch_name).render_as_string(hide_password=False)
    asyncio.run(_recreate_database(admin_url, scratch_name))
    monkeypatch.setenv("DATABASE_URL", scratch_url)
    monkeypatch.setenv("JWT_SECRET", TEST_JWT_SECRET)
    get_settings.cache_clear()
    cfg = Config(str(API_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(API_DIR / "alembic"))

    async def tables() -> set[str]:
        engine = create_async_engine(scratch_url)
        try:
            async with engine.connect() as conn:
                rows = await conn.execute(
                    text(
                        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                        "AND tablename <> 'alembic_version'"
                    )
                )
                return {r[0] for r in rows}
        finally:
            await engine.dispose()

    try:
        command.upgrade(cfg, "head")
        first = asyncio.run(tables())
        assert {"users", "sessions", "chunks"} <= first
        command.downgrade(cfg, "base")
        assert asyncio.run(tables()) == set()  # a full downgrade removes everything it created
        command.upgrade(cfg, "head")
        assert asyncio.run(tables()) == first  # and re-applying gives the same schema
    finally:
        get_settings.cache_clear()
        asyncio.run(_drop(admin_url, scratch_name))


async def _drop(admin_url: str, name: str) -> None:
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    finally:
        await engine.dispose()
