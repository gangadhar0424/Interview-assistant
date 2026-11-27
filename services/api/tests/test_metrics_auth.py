"""SEC-18: /metrics can be protected with a bearer token (open when no token is configured)."""

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from app.core.config import ConfigurationError, load_settings

TOKEN = "metrics-token-0123456789-abcdefXYZ"


def test_metrics_is_open_when_no_token_is_configured(
    make_client: Callable[..., TestClient],
) -> None:
    client = make_client()
    assert client.get("/metrics").status_code == 200


def test_metrics_refuses_requests_without_the_token(
    make_client: Callable[..., TestClient],
) -> None:
    client = make_client(METRICS_BEARER_TOKEN=TOKEN)
    refused = client.get("/metrics")
    assert refused.status_code == 401
    assert refused.headers["www-authenticate"] == "Bearer"
    assert "interview_" not in refused.text and refused.text == ""


@pytest.mark.parametrize(
    "header",
    ["", "Bearer", "Bearer wrong-token-0123456789-abcdefXYZ", TOKEN, "bearer " + TOKEN],
)
def test_metrics_refuses_a_wrong_or_malformed_token(
    make_client: Callable[..., TestClient], header: str
) -> None:
    client = make_client(METRICS_BEARER_TOKEN=TOKEN)
    assert client.get("/metrics", headers={"Authorization": header}).status_code == 401


def test_metrics_accepts_the_right_token(make_client: Callable[..., TestClient]) -> None:
    client = make_client(METRICS_BEARER_TOKEN=TOKEN)
    ok = client.get("/metrics", headers={"Authorization": f"Bearer {TOKEN}"})
    assert ok.status_code == 200
    assert "# HELP" in ok.text and "# TYPE" in ok.text


def test_a_short_token_is_refused_at_start_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("METRICS_BEARER_TOKEN", "short")
    with pytest.raises(ConfigurationError) as exc:
        load_settings()
    assert "METRICS_BEARER_TOKEN" in str(exc.value)
    assert "short" not in str(exc.value).replace("METRICS_BEARER_TOKEN", "")


def test_a_blank_token_counts_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("METRICS_BEARER_TOKEN", "  ")
    assert load_settings().metrics_bearer_token is None
