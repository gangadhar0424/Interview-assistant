"""Infrastructure dependency checks used by /ready. Results never contain secrets."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import redis.asyncio as aioredis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import Settings

logger = logging.getLogger(__name__)

CHECK_TIMEOUT_SECONDS = 2.0


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str | None = None  # safe, generic diagnostic (error class name only)


Check = Callable[[], Awaitable[CheckResult]]


async def check_postgres(settings: Settings) -> CheckResult:
    engine = create_async_engine(settings.database_url, connect_args={"timeout": 2})
    try:
        async with asyncio.timeout(CHECK_TIMEOUT_SECONDS), engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return CheckResult("postgres", True)
    except Exception as exc:
        logger.warning("postgres readiness check failed", extra={"error_type": type(exc).__name__})
        return CheckResult("postgres", False, type(exc).__name__)
    finally:
        await engine.dispose()


async def check_redis(settings: Settings) -> CheckResult:
    client = aioredis.from_url(  # type: ignore[no-untyped-call]
        settings.redis_url, socket_connect_timeout=2
    )
    try:
        async with asyncio.timeout(CHECK_TIMEOUT_SECONDS):
            await client.ping()
        return CheckResult("redis", True)
    except Exception as exc:
        logger.warning("redis readiness check failed", extra={"error_type": type(exc).__name__})
        return CheckResult("redis", False, type(exc).__name__)
    finally:
        await client.aclose()


def default_checks(settings: Settings) -> list[Check]:
    async def pg() -> CheckResult:
        return await check_postgres(settings)

    async def rd() -> CheckResult:
        return await check_redis(settings)

    return [pg, rd]
