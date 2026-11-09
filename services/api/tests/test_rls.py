import asyncio
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db.session import create_engine, create_sessionmaker, set_tenant_context
from tests.conftest import RunSql
from tests.helpers import register


def test_runtime_role_enforces_tenant_isolation(
    client: TestClient, run_sql: RunSql, test_db_url: str
) -> None:
    alice = register(client, "rls-alice@example.com")
    bob = register(client, "rls-bob@example.com")
    alice_id: uuid.UUID = run_sql("SELECT id FROM users WHERE email = 'rls-alice@example.com'")[0][
        "id"
    ]
    bob_id: uuid.UUID = run_sql("SELECT id FROM users WHERE email = 'rls-bob@example.com'")[0]["id"]
    assert alice["Authorization"] and bob["Authorization"]

    async def verify() -> None:
        engine = create_engine(test_db_url)
        try:
            sessions = create_sessionmaker(engine)
            async with sessions() as session:
                role = (
                    await session.execute(
                        text(
                            "SELECT current_user, rolbypassrls FROM pg_roles "
                            "WHERE rolname = current_user"
                        )
                    )
                ).one()
                assert role.current_user == "interview_app_runtime"
                assert role.rolbypassrls is False
                assert (
                    await session.execute(text("SELECT email FROM users ORDER BY email"))
                ).scalars().all() == []

            async with sessions() as session:
                await set_tenant_context(session, alice_id)
                emails = (
                    (await session.execute(text("SELECT email FROM users ORDER BY email")))
                    .scalars()
                    .all()
                )
                assert emails == ["rls-alice@example.com"]
                assert (
                    await session.execute(
                        text("SELECT email FROM users WHERE id = :user_id"),
                        {"user_id": bob_id},
                    )
                ).scalar_one_or_none() is None
        finally:
            await engine.dispose()

    asyncio.run(verify())
