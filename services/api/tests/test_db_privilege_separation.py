"""SEC-12: the application login and the privileged maintenance identity really are different.

These tests connect to the real test database as a least-privilege login (the shape production uses:
not a superuser, not the owner, may only SET ROLE interview_app_runtime) and try what an attacker
who controlled the API process could try. They check database permissions, not configuration text.
"""

import asyncio
import secrets
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml  # type: ignore[import-untyped]
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from app.db.session import create_engine, create_sessionmaker, set_tenant_context
from tests.conftest import RunSql
from tests.helpers import register

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def logins(run_sql: RunSql, test_db_url: str) -> Iterator[dict[str, str]]:
    """A runtime login (member of interview_app_runtime only) and a separate BYPASSRLS maintenance
    login, created for this test and dropped afterwards. Returns their connection URLs."""
    suffix = uuid.uuid4().hex[:8]
    runtime, maintenance = f"ia_t_runtime_{suffix}", f"ia_t_maint_{suffix}"
    runtime_pw, maintenance_pw = secrets.token_hex(16), secrets.token_hex(16)
    db = make_url(test_db_url).database
    run_sql(
        f"CREATE ROLE {runtime} LOGIN PASSWORD '{runtime_pw}' "
        "NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS"
    )
    run_sql(
        f"CREATE ROLE {maintenance} LOGIN PASSWORD '{maintenance_pw}' "
        "NOSUPERUSER NOCREATEDB NOCREATEROLE BYPASSRLS"
    )
    run_sql(f"GRANT interview_app_runtime TO {runtime}")
    run_sql(f"GRANT CONNECT ON DATABASE {db} TO {runtime}, {maintenance}")
    run_sql(f"GRANT USAGE ON SCHEMA public TO {maintenance}")
    run_sql(f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {maintenance}")
    base = make_url(test_db_url)
    try:
        yield {
            "runtime": base.set(username=runtime, password=runtime_pw).render_as_string(
                hide_password=False
            ),
            "maintenance": base.set(username=maintenance, password=maintenance_pw).render_as_string(
                hide_password=False
            ),
            "runtime_name": runtime,
            "maintenance_name": maintenance,
        }
    finally:
        for role in (runtime, maintenance):
            run_sql(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {role}")
            run_sql(f"REVOKE ALL ON SCHEMA public FROM {role}")
            run_sql(f"REVOKE ALL ON DATABASE {db} FROM {role}")
            run_sql(f"DROP ROLE IF EXISTS {role}")


def _attempt(url: str, statement: str) -> str | None:
    """Run one statement as the application would (runtime role, own transaction).
    Returns None if it succeeded, otherwise the database error class name."""

    async def go() -> str | None:
        engine = create_engine(url)
        try:
            sessions = create_sessionmaker(engine)
            async with sessions() as session:
                try:
                    await session.execute(text(statement))
                    await session.commit()
                except DBAPIError as exc:
                    return type(exc.orig).__name__ if exc.orig else type(exc).__name__
            return None
        finally:
            await engine.dispose()

    return asyncio.run(go())


FORBIDDEN = [
    "CREATE TABLE sec12_probe (id int)",
    "DROP TABLE users",
    "ALTER TABLE users ADD COLUMN sec12_probe int",
    "TRUNCATE users",
    "SELECT rolname, rolpassword FROM pg_authid",
    "CREATE ROLE sec12_evil LOGIN SUPERUSER",
    "CREATE EXTENSION IF NOT EXISTS dblink",
    "COPY (SELECT 1) TO PROGRAM 'echo pwned'",
    "ALTER ROLE interview_app_runtime BYPASSRLS",
    "DROP POLICY tenant_isolation ON users",
    "ALTER TABLE users DISABLE ROW LEVEL SECURITY",
    "ALTER SYSTEM SET log_statement = 'all'",
]


@pytest.mark.parametrize("statement", FORBIDDEN)
def test_runtime_login_cannot_perform_privileged_operations(
    logins: dict[str, str], statement: str
) -> None:
    assert _attempt(logins["runtime"], statement) is not None, statement


def test_runtime_login_cannot_become_the_maintenance_identity_or_another_owner(
    logins: dict[str, str],
) -> None:
    maintenance = logins["maintenance_name"]
    assert _attempt(logins["runtime"], f"SET ROLE {maintenance}") is not None
    assert _attempt(logins["runtime"], "SET ROLE postgres") is not None
    owner = make_url(logins["runtime"]).username
    assert _attempt(logins["runtime"], "RESET ROLE; SET ROLE pg_read_all_data") is not None
    assert owner is not None


def test_runtime_login_cannot_turn_row_security_off(
    client: TestClient, run_sql: RunSql, logins: dict[str, str]
) -> None:
    register(client, "sec12-victim@example.com")

    async def go() -> list[Any]:
        engine = create_engine(logins["runtime"])
        try:
            sessions = create_sessionmaker(engine)
            async with sessions() as session:
                try:
                    await session.execute(text("SET LOCAL row_security = off"))
                    return list((await session.execute(text("SELECT email FROM users"))).all())
                except DBAPIError:
                    return []  # refused outright: also fine
        finally:
            await engine.dispose()

    assert asyncio.run(go()) == []


def test_without_a_tenant_the_runtime_login_sees_nothing_but_maintenance_sees_all(
    client: TestClient, run_sql: RunSql, logins: dict[str, str]
) -> None:
    """The contrast that makes the split necessary: only the maintenance login bypasses RLS."""
    register(client, "sec12-a@example.com")
    register(client, "sec12-b@example.com")
    victim = run_sql("SELECT id FROM users WHERE email = 'sec12-a@example.com'")[0]["id"]

    async def runtime_view() -> tuple[int, int]:
        engine = create_engine(logins["runtime"])
        try:
            sessions = create_sessionmaker(engine)
            async with sessions() as session:
                no_tenant = len((await session.execute(text("SELECT id FROM users"))).all())
            async with sessions() as session:
                await set_tenant_context(session, victim)
                own = len((await session.execute(text("SELECT id FROM users"))).all())
            return no_tenant, own
        finally:
            await engine.dispose()

    async def maintenance_view() -> int:
        # A plain engine with no SET ROLE, exactly as the recovery command builds it.
        engine = create_engine(logins["maintenance"])
        try:
            async with engine.connect() as conn:
                return len((await conn.execute(text("SELECT id FROM users"))).all())
        finally:
            await engine.dispose()

    no_tenant, own = asyncio.run(runtime_view())
    assert (no_tenant, own) == (0, 1)
    assert asyncio.run(maintenance_view()) >= 2


# ---- deployment wiring: the credential exists only where it is needed ----

COMPOSE = yaml.safe_load((REPO_ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8"))
PROVIDER_KEYS = ("OPENAI_API_KEY", "GROQ_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY")


def _env(service: str) -> dict[str, Any]:
    env = COMPOSE["services"][service].get("environment", {})
    return dict(env) if isinstance(env, dict) else {e.split("=", 1)[0]: e for e in env}


@pytest.mark.parametrize("service", ["api", "worker", "web", "migrate", "postgres", "redis"])
def test_only_the_maintenance_service_holds_the_maintenance_credential(service: str) -> None:
    assert "MAINTENANCE_DATABASE_URL" not in _env(service), service


def test_the_maintenance_service_is_opt_in_only() -> None:
    service = COMPOSE["services"]["maintenance"]
    assert service.get("profiles") == ["ops"]
    assert "MAINTENANCE_DATABASE_URL" in _env("maintenance")
    assert service.get("restart") == "no"


def test_the_migration_job_holds_only_the_migration_identity() -> None:
    env = _env("migrate")
    assert set(env) <= {"APP_ENV", "DATABASE_URL"}, sorted(env)
    for key in (*PROVIDER_KEYS, "JWT_SECRET", "REDIS_URL"):
        assert key not in env


def test_the_runtime_and_migration_urls_are_different_variables() -> None:
    assert "MIGRATION_DATABASE_URL" in str(_env("migrate")["DATABASE_URL"])
    assert "MIGRATION_DATABASE_URL" not in str(_env("api")["DATABASE_URL"])


# ---- SEC-16: the data stores are hardened and have no route to the internet ----------------------


@pytest.mark.parametrize("service", ["postgres", "redis"])
def test_data_stores_are_unprivileged_read_only_and_capability_free(service: str) -> None:
    config = COMPOSE["services"][service]
    assert config.get("user") in {"postgres", "redis"}
    assert config.get("read_only") is True
    assert config.get("cap_drop") == ["ALL"]
    assert "no-new-privileges:true" in config.get("security_opt", [])
    assert config.get("pids_limit")
    assert config.get("networks") == ["data"]
    assert "ports" not in config  # still unpublished


def test_the_data_network_is_internal_and_only_app_services_can_reach_out() -> None:
    assert COMPOSE["networks"]["data"] == {"internal": True}
    services = COMPOSE["services"]
    on_edge = {name for name, svc in services.items() if "edge" in svc.get("networks", [])}
    assert on_edge == {"api", "worker", "web"}
    for name in ("postgres", "redis", "migrate", "maintenance"):
        assert COMPOSE["services"][name]["networks"] == ["data"], name
    assert COMPOSE["services"]["web"]["networks"] == ["edge"]  # never talks to the database


# ---- SEC-08: production refuses unbound handoff codes by default ---------------------------------


def test_production_requires_handoff_binding_by_default() -> None:
    value = str(_env("api")["HANDOFF_REQUIRE_CHALLENGE"])
    assert value == "${HANDOFF_REQUIRE_CHALLENGE:-true}"
    assert _env("worker")["HANDOFF_REQUIRE_CHALLENGE"] == _env("api")["HANDOFF_REQUIRE_CHALLENGE"]
    path = REPO_ROOT / "infra" / "deploy" / "production.env.example"
    example = path.read_text(encoding="utf-8")
    assert "\nHANDOFF_REQUIRE_CHALLENGE=true\n" in example
