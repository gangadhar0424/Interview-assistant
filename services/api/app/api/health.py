"""Operational endpoints (unversioned, no auth): liveness and readiness."""

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, Response, status

from app.core.config import Settings, get_settings
from app.core.readiness import Check, default_checks

router = APIRouter(tags=["operational"])


def get_checks(settings: Settings = Depends(get_settings)) -> list[Check]:  # noqa: B008
    return default_checks(settings)


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness only. Deliberately does not touch any dependency."""
    return {"status": "ok"}


@router.get("/ready")
async def ready(
    response: Response,
    checks: list[Check] = Depends(get_checks),  # noqa: B008
) -> dict[str, Any]:
    results = await asyncio.gather(*(check() for check in checks))
    all_ok = all(r.ok for r in results)
    if not all_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {
        "status": "ready" if all_ok else "not_ready",
        "checks": {
            r.name: {
                "status": "ok" if r.ok else "unavailable",
                **({"error": r.detail} if r.detail else {}),
            }
            for r in results
        },
    }
