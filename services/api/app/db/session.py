import logging
from collections.abc import AsyncIterator
from uuid import UUID

from fastapi import Request
from sqlalchemy import event, text
from sqlalchemy.engine import Connection
from sqlalchemy.engine.interfaces import ExceptionContext
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session

from app.core.metrics import database_errors

RUNTIME_DATABASE_ROLE = "interview_app_runtime"
logger = logging.getLogger("app.db")


class _RuntimeSession(Session):
    pass


@event.listens_for(_RuntimeSession, "after_begin")
def _apply_runtime_role(session: Session, transaction: object, connection: Connection) -> None:
    del transaction
    connection.exec_driver_sql(f"SET LOCAL ROLE {RUNTIME_DATABASE_ROLE}")
    user_id = session.info.get("app.user_id")
    if user_id is not None:
        connection.execute(
            text("SELECT set_config('app.user_id', :user_id, true)"),
            {"user_id": str(user_id)},
        )


def create_engine(database_url: str) -> AsyncEngine:
    # Lazy: no connection is opened until first use, so startup never depends on Postgres.
    engine = create_async_engine(database_url, pool_pre_ping=True)

    @event.listens_for(engine.sync_engine, "handle_error")
    def _record_database_error(exception_context: ExceptionContext) -> None:
        database_errors.inc()
        logger.warning(
            "database operation failed",
            extra={"error_type": type(exception_context.original_exception).__name__},
        )

    return engine


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, sync_session_class=_RuntimeSession)


def create_maintenance_sessionmaker(
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    """Create sessions for explicitly configured maintenance credentials only."""
    return async_sessionmaker(engine, expire_on_commit=False)


async def set_tenant_context(session: AsyncSession, user_id: UUID) -> None:
    """Set the RLS identity for this session and its current transaction.

    The value must come from authenticated identity or a database-backed job row, never request
    input. A session cannot switch tenants after it has been bound.
    """
    if not isinstance(user_id, UUID):
        raise TypeError("tenant identity must be a UUID")
    bound = session.info.get("app.user_id")
    if bound is not None and bound != user_id:
        raise RuntimeError("database session is already bound to another tenant")
    session.info["app.user_id"] = user_id
    await session.execute(
        text("SELECT set_config('app.user_id', :user_id, true)"),
        {"user_id": str(user_id)},
    )


async def set_login_email(session: AsyncSession, email: str) -> None:
    """Limit the unauthenticated login lookup to the submitted account."""
    await session.execute(
        text("SELECT set_config('app.login_email', :email, true)"),
        {"email": email},
    )


async def set_refresh_token_context(session: AsyncSession, token_hash: str) -> None:
    """Allow refresh-token lookup by the presented opaque token only."""
    await session.execute(
        text("SELECT set_config('app.refresh_token_hash', :token_hash, true)"),
        {"token_hash": token_hash},
    )


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Request-scoped DB session. Services commit explicitly."""
    sessionmaker: async_sessionmaker[AsyncSession] = request.app.state.sessionmaker
    async with sessionmaker() as session:
        yield session
