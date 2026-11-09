"""GET /ai/options, the safe error envelope for LLM failures, and LLM configuration validation."""

from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.ai.errors import AuthError, ContentFiltered, LLMError, RateLimited, Timeout, Unavailable
from app.core.config import ConfigurationError, Settings, load_settings
from app.preferences.enums import AnswerFormat, AnswerLength, AnswerTone, Language, QuestionType
from tests.helpers import register


def test_options_requires_authentication(client: TestClient) -> None:
    assert client.get("/api/v1/ai/options").status_code == 401


def test_options_lists_only_enabled_providers(make_client: Callable[..., TestClient]) -> None:
    client = make_client(LLM_ENABLED_PROVIDERS="fake")
    headers = register(client, "alice@example.com")
    body = client.get("/api/v1/ai/options", headers=headers).json()
    assert [p["id"] for p in body["providers"]] == ["fake"]
    model = body["providers"][0]["models"][0]
    assert set(model) == {"id", "label", "capabilities", "languages"}
    assert body["languages"] == ["en", "hi", "te"]


def test_options_is_empty_when_no_provider_is_enabled(client: TestClient) -> None:
    headers = register(client, "alice@example.com")
    body = client.get("/api/v1/ai/options", headers=headers).json()
    assert body["providers"] == []  # no provider enabled, yet preference values are still served
    assert body["languages"] == ["en", "hi", "te"]


def test_options_serve_the_preference_enums_as_the_single_source_of_truth(
    client: TestClient,
) -> None:
    headers = register(client, "alice@example.com")
    body = client.get("/api/v1/ai/options", headers=headers).json()
    assert set(body) == {
        "providers",
        "languages",
        "question_types",
        "formats",
        "lengths",
        "tones",
    }
    assert body["languages"] == [v.value for v in Language]
    assert body["question_types"] == [v.value for v in QuestionType]
    assert body["formats"] == [v.value for v in AnswerFormat]
    assert body["lengths"] == [v.value for v in AnswerLength]
    assert body["tones"] == [v.value for v in AnswerTone]
    assert "auto" in body["question_types"] and len(body["question_types"]) == 9
    # Stable order across calls.
    assert client.get("/api/v1/ai/options", headers=headers).json() == body


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (RateLimited, 429, "llm_rate_limited"),
        (Timeout, 504, "llm_timeout"),
        (Unavailable, 503, "llm_unavailable"),
        (AuthError, 503, "llm_unavailable"),
        (ContentFiltered, 422, "llm_content_filtered"),
    ],
)
def test_llm_errors_become_safe_envelopes(
    client: TestClient, error: type[LLMError], status: int, code: str
) -> None:
    async def boom() -> None:
        raise error()

    client.app.add_api_route("/_boom", boom)  # type: ignore[attr-defined]
    response = client.get("/_boom")
    assert response.status_code == status
    body = response.json()["error"]
    assert body["code"] == code and set(body) == {"code", "message", "request_id"}


# ------------------------------------------------------------------ configuration


@pytest.fixture
def no_env_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(Settings.model_config, "env_file", None)


@pytest.mark.usefixtures("no_env_file")
def test_llm_defaults_enable_nothing() -> None:
    s = load_settings()
    assert s.llm_enabled_provider_list == [] and s.llm_fallback_chain_list == []


@pytest.mark.usefixtures("no_env_file")
def test_llm_settings_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_ENABLED_PROVIDERS", " fake ")
    monkeypatch.setenv("LLM_FALLBACK_CHAIN", "fake")
    s = load_settings()
    assert s.llm_enabled_provider_list == ["fake"] and s.llm_fallback_chain_list == ["fake"]


@pytest.mark.usefixtures("no_env_file")
def test_openai_settings_require_server_key_and_accept_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_ENABLED_PROVIDERS", "openai")
    with pytest.raises(ConfigurationError) as exc:
        load_settings()
    assert "OPENAI_API_KEY" in str(exc.value)

    monkeypatch.setenv("OPENAI_API_KEY", "server-only-test-key")
    monkeypatch.setenv("OPENAI_MODEL", "  gpt-test-model  ")
    monkeypatch.setenv("OPENAI_TIMEOUT", "12")
    settings = load_settings()
    assert settings.openai_model == "gpt-test-model"
    assert settings.openai_timeout == 12


@pytest.mark.usefixtures("no_env_file")
def test_ollama_cloud_settings_require_ollama_key_and_accept_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_ENABLED_PROVIDERS", "openai")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://ollama.com/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "not-an-ollama-key")
    with pytest.raises(ConfigurationError) as exc:
        load_settings()
    assert "OLLAMA_API_KEY" in str(exc.value)

    monkeypatch.setenv("OLLAMA_API_KEY", "server-only-ollama-test-key")
    monkeypatch.setenv("OPENAI_MODEL", "deepseek-v4.1-flash")
    settings = load_settings()
    assert settings.openai_base_url.host == "ollama.com"
    assert settings.openai_model == "deepseek-v4.1-flash"
    assert settings.ollama_api_key is not None
    assert "server-only-ollama-test-key" not in repr(settings)


@pytest.mark.usefixtures("no_env_file")
@pytest.mark.parametrize(
    ("name", "value", "field"),
    [
        ("OPENAI_MODEL", "   ", "OPENAI_MODEL"),
        ("OPENAI_TIMEOUT", "0", "OPENAI_TIMEOUT"),
    ],
)
def test_invalid_openai_settings_fail_clearly(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str, field: str
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigurationError) as exc:
        load_settings()
    assert field in str(exc.value)


@pytest.mark.usefixtures("no_env_file")
@pytest.mark.parametrize(
    ("name", "value", "field"),
    [
        ("LLM_ENABLED_PROVIDERS", "unknown", "LLM_ENABLED_PROVIDERS"),
        ("LLM_ENABLED_PROVIDERS", "fake,fake", "LLM_ENABLED_PROVIDERS"),
        ("LLM_FALLBACK_CHAIN", "fake", "LLM_FALLBACK_CHAIN"),  # listed but not enabled
        ("LLM_REQUEST_TIMEOUT_SECONDS", "0", "LLM_REQUEST_TIMEOUT_SECONDS"),
        ("LLM_FIRST_TOKEN_TIMEOUT_SECONDS", "-1", "LLM_FIRST_TOKEN_TIMEOUT_SECONDS"),
        ("LLM_BREAKER_FAILURE_THRESHOLD", "0", "LLM_BREAKER_FAILURE_THRESHOLD"),
    ],
)
def test_invalid_llm_settings_fail_clearly(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str, field: str
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigurationError) as exc:
        load_settings()
    assert field in str(exc.value)


@pytest.mark.usefixtures("no_env_file")
def test_fake_llm_provider_is_rejected_outside_local(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.setenv("JWT_SECRET", "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0U1v2W3x4")
    monkeypatch.setenv("REDIS_URL", "rediss://redis.example.invalid:6379/0")
    monkeypatch.setenv(
        "MAINTENANCE_DATABASE_URL",
        "postgresql+asyncpg://maintenance:placeholder@db.example.invalid/app",
    )
    monkeypatch.setenv("FILE_STORAGE_ROOT", str(tmp_path))
    monkeypatch.setenv("EMBEDDING_PROVIDER", "fastembed")
    monkeypatch.setenv("LLM_ENABLED_PROVIDERS", "fake")
    with pytest.raises(ConfigurationError) as exc:
        load_settings()
    assert "fake LLM provider" in str(exc.value)
