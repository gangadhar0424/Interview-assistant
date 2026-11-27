import tempfile
from pathlib import Path

import pytest

from app.core.config import ConfigurationError, Settings, find_repo_root, load_settings


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
    monkeypatch.setenv("JWT_SECRET", "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0U1v2W3x4")
    monkeypatch.setenv("REDIS_URL", "rediss://redis.example.invalid:6379/0")
    monkeypatch.setenv(
        "MAINTENANCE_DATABASE_URL",
        "postgresql+asyncpg://maintenance:placeholder@db.example.invalid/app",
    )
    with pytest.raises(ConfigurationError) as exc:
        load_settings()
    assert "FILE_STORAGE_ROOT" in str(exc.value)
    monkeypatch.setenv("FILE_STORAGE_ROOT", str(Path(tempfile.gettempdir()) / "persistent-volume"))
    load_settings()


def _set_valid_deployment_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "fastembed")
    monkeypatch.setenv("JWT_SECRET", "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0U1v2W3x4")
    monkeypatch.setenv("REDIS_URL", "rediss://redis.example.invalid:6379/0")
    monkeypatch.setenv(
        "MAINTENANCE_DATABASE_URL",
        "postgresql+asyncpg://maintenance:placeholder@db.example.invalid/app",
    )
    monkeypatch.setenv("FILE_STORAGE_ROOT", str(Path(tempfile.gettempdir()) / "persistent-volume"))


def test_plaintext_redis_is_rejected_outside_local(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_valid_deployment_settings(monkeypatch)
    monkeypatch.setenv("REDIS_URL", "redis://redis.example.invalid:6379/0")

    with pytest.raises(ConfigurationError) as exc:
        load_settings()

    assert "REDIS_URL" in str(exc.value)


def test_ordinary_processes_do_not_need_the_maintenance_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # SEC-12: the API and worker must start WITHOUT the privileged identity.
    _set_valid_deployment_settings(monkeypatch)
    monkeypatch.delenv("MAINTENANCE_DATABASE_URL")

    settings = load_settings()

    assert settings.maintenance_database_url is None


def test_maintenance_commands_refuse_to_run_without_the_url_outside_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_valid_deployment_settings(monkeypatch)
    monkeypatch.delenv("MAINTENANCE_DATABASE_URL")
    settings = load_settings()

    with pytest.raises(ConfigurationError) as exc:
        settings.require_maintenance_database_url()

    assert "MAINTENANCE_DATABASE_URL" in str(exc.value)


def test_maintenance_url_is_returned_when_set_and_local_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_valid_deployment_settings(monkeypatch)
    assert "maintenance" in load_settings().require_maintenance_database_url()
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.delenv("MAINTENANCE_DATABASE_URL")
    monkeypatch.delenv("REDIS_URL")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    local = load_settings()
    assert local.require_maintenance_database_url() == local.database_url


def test_blank_maintenance_url_counts_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    # `.env.example` ships `MAINTENANCE_DATABASE_URL=` (blank).
    monkeypatch.setenv("MAINTENANCE_DATABASE_URL", "  ")
    assert load_settings().maintenance_database_url is None


# ---- SEC-07: APP_ENV defaults to local, which must not start against remote services ----


def test_local_default_is_refused_when_the_database_is_remote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("APP_ENV", raising=False)  # the forgotten-variable case
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@db.internal.example:5432/app")

    with pytest.raises(ConfigurationError) as exc:
        load_settings()

    message = str(exc.value)
    assert "APP_ENV" in message and "DATABASE_URL" in message
    assert "u:p@" not in message  # never echoes the URL


def test_local_default_is_refused_when_redis_or_cors_is_remote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.setenv("REDIS_URL", "redis://cache.internal.example:6379/0")
    with pytest.raises(ConfigurationError) as exc:
        load_settings()
    assert "REDIS_URL" in str(exc.value)

    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("CORS_ORIGINS", "https://app.example.com")
    with pytest.raises(ConfigurationError) as exc:
        load_settings()
    assert "CORS_ORIGINS" in str(exc.value)


def test_local_with_loopback_services_still_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APP_ENV", raising=False)
    for url in ("localhost", "127.0.0.1", "[::1]"):
        monkeypatch.setenv("DATABASE_URL", f"postgresql+asyncpg://u:p@{url}:5432/app")
        monkeypatch.setenv("REDIS_URL", f"redis://{url}:6379/0")
        assert load_settings().app_env == "local"


def test_local_with_remote_services_needs_an_explicit_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@db.internal.example:5432/app")
    monkeypatch.setenv("LOCAL_ALLOW_REMOTE_SERVICES", "true")

    assert load_settings().app_env == "local"


def test_deployed_environments_are_unaffected_by_the_local_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_valid_deployment_settings(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@db.internal.example:5432/app")
    monkeypatch.setenv("CORS_ORIGINS", "https://app.example.com")

    assert load_settings().app_env == "staging"


def test_production_safeguards_stay_on_when_app_env_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_valid_deployment_settings(monkeypatch)
    monkeypatch.setenv("APP_ENV", "production")
    settings = load_settings()
    assert settings.cookie_secure is True and settings.docs_enabled is False
    monkeypatch.setenv("JWT_SECRET", "x" * 43)  # weak
    with pytest.raises(ConfigurationError):
        load_settings()


def test_repo_root_in_a_checkout_is_the_repository(tmp_path: Path) -> None:
    config = tmp_path / "repo" / "services" / "api" / "app" / "core" / "config.py"
    config.parent.mkdir(parents=True)
    config.write_text("")
    assert find_repo_root(config) == tmp_path / "repo"


def test_repo_root_in_a_container_layout_does_not_crash() -> None:
    """/app/app/core/config.py has only four ancestors; this used to raise IndexError on import."""
    root = find_repo_root(Path("/app/app/core/config.py"))
    assert root.as_posix().endswith("app") or root.as_posix() == "/app"


def test_repo_root_with_a_deep_path_that_is_not_the_repository(tmp_path: Path) -> None:
    config = tmp_path / "a" / "b" / "c" / "d" / "e" / "config.py"
    config.parent.mkdir(parents=True)
    config.write_text("")
    assert (
        find_repo_root(config) == config.resolve().parents[2]
    )  # no services/api ancestor: app dir
