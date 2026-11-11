"""Phase 13A: baseline security headers on every API response, without overwriting route headers."""

from typing import Any

from fastapi.testclient import TestClient

from app.core.security_headers import BASELINE_HEADERS, apply_security_headers

BASELINE = dict(BASELINE_HEADERS)


def assert_baseline(headers: Any) -> None:
    for name, value in BASELINE.items():
        assert headers.get(name) == value, name


# ---------------------------------------------------------------- pure function


def test_apply_sets_the_baseline_and_a_restrictive_api_csp() -> None:
    headers: dict[str, str] = {}
    apply_security_headers("/api/v1/sessions", headers)
    assert_baseline(headers)
    assert headers["Content-Security-Policy"] == "default-src 'none'; frame-ancestors 'none'"
    assert "Cache-Control" not in headers


def test_apply_never_overwrites_existing_headers() -> None:
    headers = {
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "same-origin",
        "Content-Security-Policy": "default-src 'self'",
    }
    apply_security_headers("/api/v1/auth/login", headers)
    assert headers["Cache-Control"] == "private, no-store"
    assert headers["Referrer-Policy"] == "same-origin"
    assert headers["Content-Security-Policy"] == "default-src 'self'"


def test_auth_paths_are_never_cached() -> None:
    for path in ("/api/v1/auth/login", "/api/v1/auth/refresh", "/api/v1/auth/me"):
        headers: dict[str, str] = {}
        apply_security_headers(path, headers)
        assert headers["Cache-Control"] == "no-store", path
    headers = {}
    apply_security_headers("/api/v1/authors", headers)  # a prefix lookalike is not an auth path
    assert "Cache-Control" not in headers


def test_interactive_docs_keep_working_without_the_api_csp() -> None:
    for path in ("/docs", "/redoc"):
        headers: dict[str, str] = {}
        apply_security_headers(path, headers)
        assert "Content-Security-Policy" not in headers, path
        assert_baseline(headers)


def test_hsts_is_left_to_the_tls_proxy() -> None:
    headers: dict[str, str] = {}
    apply_security_headers("/health", headers)
    assert "Strict-Transport-Security" not in headers


# ------------------------------------------------------- through the real app


def test_every_response_class_carries_the_baseline(client: TestClient) -> None:
    ok = client.get("/health")
    assert ok.status_code == 200
    assert_baseline(ok.headers)

    missing = client.get("/api/v1/does-not-exist")  # 404 from the framework
    assert missing.status_code == 404
    assert_baseline(missing.headers)

    unauthorized = client.get("/api/v1/auth/me")  # 401 from the auth dependency
    assert unauthorized.status_code == 401
    assert_baseline(unauthorized.headers)
    assert unauthorized.headers["Cache-Control"] == "no-store"

    invalid = client.post("/api/v1/auth/login", json={"email": "not-an-email"})  # 422
    assert invalid.status_code == 422
    assert_baseline(invalid.headers)

    assert_baseline(client.get("/metrics").headers)


def test_login_and_refresh_responses_are_no_store_and_keep_their_cookie(
    client: TestClient,
) -> None:
    creds = {"email": "headers@example.com", "password": "correct-horse-battery-1"}
    assert client.post("/api/v1/auth/signup", json=creds).status_code == 201
    login = client.post("/api/v1/auth/login", json=creds)
    assert login.status_code == 200
    assert_baseline(login.headers)
    assert login.headers["Cache-Control"] == "no-store"
    assert "refresh_token" in login.headers.get("set-cookie", "")


def test_docs_page_is_still_served_without_a_blocking_csp(client: TestClient) -> None:
    response = client.get("/docs")
    assert response.status_code == 200
    assert "Content-Security-Policy" not in response.headers
    assert_baseline(response.headers)


def test_request_id_header_is_still_returned(client: TestClient) -> None:
    response = client.get("/health", headers={"X-Request-ID": "abc123"})
    assert response.headers["x-request-id"] == "abc123"
    assert_baseline(response.headers)
