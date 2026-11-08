"""Needs `docker compose up -d --wait` and a real .env. Run: uv run pytest -m integration"""

import pytest
from fastapi.testclient import TestClient

from app.core.config import REPO_ROOT, Settings, get_settings
from app.main import create_app

pytestmark = pytest.mark.integration


@pytest.fixture
def real_env(monkeypatch: pytest.MonkeyPatch) -> None:
    if not (REPO_ROOT / ".env").exists():
        pytest.skip("no .env configured")
    # Drop the conftest fakes so the real .env is used.
    monkeypatch.delenv("DATABASE_URL")
    monkeypatch.delenv("REDIS_URL")
    get_settings.cache_clear()
    assert Settings.model_config["env_file"] == REPO_ROOT / ".env"


def test_ready_against_real_infrastructure(real_env: None) -> None:
    with TestClient(create_app()) as client:
        r = client.get("/ready")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ready"
