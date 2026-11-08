"""/health and /ready behaviour. Infrastructure is faked (real services: test_integration.py)."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.api.health import get_checks
from app.core.readiness import Check, CheckResult
from app.main import create_app


def _check(name: str, ok: bool, detail: str | None = None) -> Check:
    async def run() -> CheckResult:
        return CheckResult(name, ok, detail)

    return run


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(create_app()) as c:
        yield c


def test_health_ok_without_any_infrastructure(client: TestClient) -> None:
    # Settings point at closed ports (see conftest); /health must not care.
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_ready_ok_when_dependencies_available(client: TestClient) -> None:
    client.app.dependency_overrides[get_checks] = lambda: [  # type: ignore[attr-defined]
        _check("postgres", True),
        _check("redis", True),
    ]
    r = client.get("/ready")
    assert r.status_code == 200
    assert r.json() == {
        "status": "ready",
        "checks": {"postgres": {"status": "ok"}, "redis": {"status": "ok"}},
    }


@pytest.mark.parametrize("down", ["postgres", "redis"])
def test_ready_503_when_a_dependency_is_down(client: TestClient, down: str) -> None:
    client.app.dependency_overrides[get_checks] = lambda: [  # type: ignore[attr-defined]
        _check("postgres", down != "postgres", "OSError" if down == "postgres" else None),
        _check("redis", down != "redis", "ConnectionError" if down == "redis" else None),
    ]
    r = client.get("/ready")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "not_ready"
    assert body["checks"][down]["status"] == "unavailable"


def test_ready_with_real_unreachable_dependencies_is_safe(client: TestClient) -> None:
    # Real checks against closed ports (conftest): must return 503 and leak nothing.
    r = client.get("/ready")
    assert r.status_code == 503
    assert "postgresql" not in r.text
    assert "redis://" not in r.text
    assert "127.0.0.1" not in r.text
    body = r.json()
    assert body["checks"]["postgres"]["status"] == "unavailable"
    assert body["checks"]["redis"]["status"] == "unavailable"


def test_request_id_header_is_returned(client: TestClient) -> None:
    r = client.get("/health", headers={"x-request-id": "abc123"})
    assert r.headers["x-request-id"] == "abc123"
