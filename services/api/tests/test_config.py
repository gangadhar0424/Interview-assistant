import tempfile
from pathlib import Path

import pytest

from app.core.config import ConfigurationError, Settings, load_settings


@pytest.fixture(autouse=True)
def _no_env_file(monkeypatch: pytest.MonkeyPatch) -> None:
    # Isolate from any developer .env at the repo root.
    monkeypatch.setitem(Settings.model_config, "env_file", None)


def test_loads_valid_settings() -> None:
    s = load_settings()
    assert s.app_env == "local"
    assert s.log_level == "INFO"
    assert s.redis_url.startswith("redis://")
    assert s.access_token_ttl_minutes == 15
    assert s.cors_origin_list == ["http://localhost:3000"]
    assert s.cookie_secure is False


def test_missing_required_settings_fail_with_useful_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABASE_URL")
    monkeypatch.delenv("REDIS_URL")
    monkeypatch.delenv("JWT_SECRET")
    with pytest.raises(ConfigurationError) as exc:
        load_settings()
    msg = str(exc.value)
    assert "DATABASE_URL" in msg
    assert "REDIS_URL" in msg
    assert "JWT_SECRET" in msg
    assert ".env.example" in msg


def test_invalid_value_error_does_not_leak_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "mysql://user:SuperSecretPw@host/db")
    with pytest.raises(ConfigurationError) as exc:
        load_settings()
    assert "SuperSecretPw" not in str(exc.value)
    assert "DATABASE_URL" in str(exc.value)


def test_invalid_log_level_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "LOUD")
    with pytest.raises(ConfigurationError):
        load_settings()


def test_short_jwt_secret_rejected_without_leaking_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JWT_SECRET", "too-short-secret")
    with pytest.raises(ConfigurationError) as exc:
        load_settings()
    assert "JWT_SECRET" in str(exc.value)
    assert "too-short-secret" not in str(exc.value)


def test_placeholder_jwt_secret_rejected_outside_local(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JWT_SECRET", "change-me-" + "x" * 40)
    load_settings()  # allowed in local
    monkeypatch.setenv("APP_ENV", "production")
    with pytest.raises(ConfigurationError):
        load_settings()


@pytest.mark.parametrize("origins", ["*", "localhost:3000", "http://ok.example,*"])
def test_cors_origins_must_be_explicit(monkeypatch: pytest.MonkeyPatch, origins: str) -> None:
    monkeypatch.setenv("CORS_ORIGINS", origins)
    with pytest.raises(ConfigurationError):
        load_settings()


def test_storage_settings_defaults_and_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    s = load_settings()
    assert s.storage_root.name == "app-data"
    assert (s.resume_max_bytes, s.document_max_bytes) == (10 * 1024 * 1024, 20 * 1024 * 1024)
    monkeypatch.setenv("FILE_STORAGE_ROOT", "/data/uploads")
    monkeypatch.setenv("RESUME_MAX_UPLOAD_MB", "5")
    s = load_settings()
    assert s.storage_root.as_posix().endswith("/data/uploads")
    assert s.resume_max_bytes == 5 * 1024 * 1024


def test_storage_root_is_required_outside_local(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "fastembed")  # required outside local (Phase 5)
    monkeypatch.setenv("JWT_SECRET", "a-real-looking-secret-" + "x" * 30)
    with pytest.raises(ConfigurationError) as exc:
        load_settings()
    assert "FILE_STORAGE_ROOT" in str(exc.value)
    monkeypatch.setenv("FILE_STORAGE_ROOT", str(Path(tempfile.gettempdir()) / "persistent-volume"))
    load_settings()
