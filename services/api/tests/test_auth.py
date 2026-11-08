"""Phase 2 targeted tests: signup, login, access tokens, refresh rotation/reuse, logout."""

from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest
from fastapi.testclient import TestClient
from httpx import Response

from app.auth.security import JWT_ALGORITHM, JWT_ISSUER
from tests.conftest import TEST_JWT_SECRET, RunSql

EMAIL = "alice@example.com"
PASSWORD = "correct horse battery staple"  # noqa: S105
ORIGIN = {"Origin": "http://localhost:3000"}
COOKIE = "refresh_token"


def signup(client: TestClient, email: str = EMAIL, password: str = PASSWORD) -> Response:
    response: Response = client.post(
        "/api/v1/auth/signup", json={"email": email, "password": password}
    )
    return response


def login(client: TestClient, email: str = EMAIL, password: str = PASSWORD) -> Response:
    response: Response = client.post(
        "/api/v1/auth/login", json={"email": email, "password": password}
    )
    return response


def refresh_cookie(response: Response) -> str:
    value = response.cookies.get(COOKIE)
    assert value, "response did not set a refresh cookie"
    return value


def set_refresh_cookie(client: TestClient, token: str) -> None:
    client.cookies.clear()
    client.cookies.set(COOKIE, token, path="/api/v1/auth")


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def forge_token(*, exp_delta: timedelta, secret: str = TEST_JWT_SECRET, sub: str) -> str:
    now = datetime.now(UTC)
    claims = {"sub": sub, "iat": now, "exp": now + exp_delta, "iss": JWT_ISSUER}
    return jwt.encode(claims, secret, algorithm=JWT_ALGORITHM)


# ---------------------------------------------------------------- signup


def test_signup_success(client: TestClient) -> None:
    r = signup(client, email="  Alice@Example.COM ")
    assert r.status_code == 201
    user = r.json()["user"]
    assert user["email"] == EMAIL  # trimmed and lower-cased
    assert set(user) == {"id", "email", "created_at"}
    assert "password" not in r.text and "argon2" not in r.text


def test_signup_duplicate_email_rejected_case_insensitively(client: TestClient) -> None:
    assert signup(client).status_code == 201
    for email in (EMAIL, "ALICE@example.com"):
        r = signup(client, email=email)
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "signup_failed"
        assert "already" not in r.text.lower()  # message does not say which detail clashed


def test_password_is_stored_as_argon2id_hash(client: TestClient, run_sql: RunSql) -> None:
    signup(client)
    rows = run_sql("SELECT password_hash FROM users WHERE email = :e", e=EMAIL)
    stored = rows[0]["password_hash"]
    assert stored.startswith("$argon2id$")
    assert PASSWORD not in stored


@pytest.mark.parametrize(
    ("email", "password"),
    [("not-an-email", PASSWORD), (EMAIL, "short"), (EMAIL, " " * 12), (EMAIL, "x" * 129)],
)
def test_signup_validation_rejects_bad_input_without_echoing_it(
    client: TestClient, email: str, password: str
) -> None:
    r = signup(client, email=email, password=password)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"
    assert password.strip() == "" or password not in r.text  # input is never echoed back


# ----------------------------------------------------------------- login


def test_login_success_sets_httponly_refresh_cookie(client: TestClient) -> None:
    signup(client)
    r = login(client, email="ALICE@example.com")  # email case-insensitive
    assert r.status_code == 200
    body = r.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 15 * 60
    assert body["user"]["email"] == EMAIL
    assert COOKIE not in r.text  # refresh token is never in the JSON body
    set_cookie = r.headers["set-cookie"].lower()
    assert "httponly" in set_cookie
    assert "samesite=lax" in set_cookie
    assert "path=/api/v1/auth" in set_cookie
    assert r.headers["cache-control"] == "no-store"


def test_login_failures_are_generic_and_indistinguishable(client: TestClient) -> None:
    signup(client)
    wrong_pw = login(client, password="this is the wrong password")
    unknown = login(client, email="nobody@example.com")
    assert wrong_pw.status_code == unknown.status_code == 401
    assert (
        wrong_pw.json()["error"]["code"] == unknown.json()["error"]["code"] == "invalid_credentials"
    )
    assert wrong_pw.json()["error"]["message"] == unknown.json()["error"]["message"]
    assert COOKIE not in wrong_pw.headers.get("set-cookie", "")


def test_disabled_user_cannot_log_in_or_use_existing_token(
    client: TestClient, run_sql: RunSql
) -> None:
    signup(client)
    token = login(client).json()["access_token"]
    run_sql("UPDATE users SET status = 'disabled'")
    assert login(client).status_code == 401
    assert client.get("/api/v1/auth/me", headers=bearer(token)).status_code == 401


# ------------------------------------------------------ protected endpoint


def test_me_requires_authentication(client: TestClient) -> None:
    r = client.get("/api/v1/auth/me")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "not_authenticated"
    assert r.headers["www-authenticate"] == "Bearer"


def test_me_with_valid_access_token(client: TestClient) -> None:
    signup(client)
    token = login(client).json()["access_token"]
    r = client.get("/api/v1/auth/me", headers=bearer(token))
    assert r.status_code == 200
    assert r.json()["email"] == EMAIL
    assert "password" not in r.text and "argon2" not in r.text


def test_access_token_has_only_minimal_claims(client: TestClient) -> None:
    signup(client)
    body = login(client).json()
    claims = jwt.decode(body["access_token"], TEST_JWT_SECRET, algorithms=[JWT_ALGORITHM])
    assert set(claims) == {"sub", "iat", "exp", "iss"}
    assert claims["sub"] == body["user"]["id"]
    assert claims["exp"] - claims["iat"] == 15 * 60


def test_expired_and_invalid_access_tokens_are_rejected(client: TestClient) -> None:
    signup(client)
    user_id = login(client).json()["user"]["id"]
    good_but_expired = forge_token(exp_delta=timedelta(seconds=-5), sub=user_id)
    wrong_secret = forge_token(
        exp_delta=timedelta(minutes=5), sub=user_id, secret="a-different-secret-of-32-characters!!"
    )
    unsigned = jwt.encode(
        {
            "sub": user_id,
            "iat": datetime.now(UTC),
            "exp": datetime.now(UTC) + timedelta(minutes=5),
            "iss": JWT_ISSUER,
        },
        key=None,
        algorithm="none",
    )
    unknown_user = forge_token(
        exp_delta=timedelta(minutes=5), sub="00000000-0000-0000-0000-000000000000"
    )
    for token in (good_but_expired, wrong_secret, unsigned, unknown_user, "garbage", ""):
        r = client.get("/api/v1/auth/me", headers=bearer(token))
        assert r.status_code == 401, token
        assert r.json()["error"]["code"] == "not_authenticated"


# ----------------------------------------------------------------- refresh


def test_refresh_issues_new_access_token_and_rotates_cookie(client: TestClient) -> None:
    signup(client)
    first = login(client)
    old_cookie = refresh_cookie(first)
    r = client.post("/api/v1/auth/refresh", headers=ORIGIN)
    assert r.status_code == 200
    new_cookie = refresh_cookie(r)
    assert new_cookie != old_cookie  # rotated
    token = r.json()["access_token"]
    assert client.get("/api/v1/auth/me", headers=bearer(token)).status_code == 200


def test_refresh_rotation_revokes_previous_token_and_chains_family(
    client: TestClient, run_sql: RunSql
) -> None:
    signup(client)
    login(client)
    assert client.post("/api/v1/auth/refresh", headers=ORIGIN).status_code == 200
    rows = run_sql(
        "SELECT revoked_at, replaced_by, family_id FROM refresh_tokens ORDER BY created_at"
    )
    assert len(rows) == 2
    assert rows[0]["revoked_at"] is not None
    assert rows[0]["replaced_by"] is not None
    assert rows[1]["revoked_at"] is None
    assert rows[0]["family_id"] == rows[1]["family_id"]


def test_refresh_tokens_are_stored_hashed(client: TestClient, run_sql: RunSql) -> None:
    signup(client)
    raw = refresh_cookie(login(client))
    stored = run_sql("SELECT token_hash FROM refresh_tokens")[0]["token_hash"]
    assert stored != raw
    assert raw not in stored
    assert len(stored) == 64


def test_reuse_of_rotated_refresh_token_is_rejected_and_revokes_family(
    client: TestClient, run_sql: RunSql
) -> None:
    signup(client)
    old = refresh_cookie(login(client))
    rotated = client.post("/api/v1/auth/refresh", headers=ORIGIN)
    newest = refresh_cookie(rotated)

    # Attacker (or buggy client) replays the already-rotated token.
    set_refresh_cookie(client, old)
    replay = client.post("/api/v1/auth/refresh", headers=ORIGIN)
    assert replay.status_code == 401
    assert replay.json()["error"]["code"] == "invalid_refresh_token"

    # The whole family is now dead, including the legitimately newest token.
    set_refresh_cookie(client, newest)
    assert client.post("/api/v1/auth/refresh", headers=ORIGIN).status_code == 401
    assert run_sql("SELECT count(*) AS n FROM refresh_tokens WHERE revoked_at IS NULL")[0]["n"] == 0


def test_expired_refresh_token_returns_session_expired(client: TestClient, run_sql: RunSql) -> None:
    signup(client)
    login(client)
    run_sql("UPDATE refresh_tokens SET expires_at = now() - interval '1 minute'")
    r = client.post("/api/v1/auth/refresh", headers=ORIGIN)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "session_expired"


def test_refresh_without_or_with_unknown_cookie_is_rejected(client: TestClient) -> None:
    assert client.post("/api/v1/auth/refresh", headers=ORIGIN).status_code == 401
    set_refresh_cookie(client, "not-a-real-token")
    assert client.post("/api/v1/auth/refresh", headers=ORIGIN).status_code == 401


def test_refresh_and_logout_reject_untrusted_origin(client: TestClient) -> None:
    signup(client)
    login(client)
    evil = {"Origin": "https://evil.example"}
    assert client.post("/api/v1/auth/refresh", headers=evil).status_code == 403
    assert client.post("/api/v1/auth/logout", headers=evil).status_code == 403
    # The refresh session is untouched by the rejected requests.
    assert client.post("/api/v1/auth/refresh", headers=ORIGIN).status_code == 200


# ------------------------------------------------------------------ logout


def test_logout_revokes_refresh_session(client: TestClient, run_sql: RunSql) -> None:
    signup(client)
    cookie = refresh_cookie(login(client))
    r = client.post("/api/v1/auth/logout", headers=ORIGIN)
    assert r.status_code == 204
    assert COOKIE in r.headers["set-cookie"] and "max-age=0" in r.headers["set-cookie"].lower()

    # The old refresh token can no longer obtain access tokens.
    set_refresh_cookie(client, cookie)
    assert client.post("/api/v1/auth/refresh", headers=ORIGIN).status_code == 401
    assert run_sql("SELECT count(*) AS n FROM refresh_tokens WHERE revoked_at IS NULL")[0]["n"] == 0


def test_logout_is_idempotent_without_a_session(client: TestClient) -> None:
    assert client.post("/api/v1/auth/logout", headers=ORIGIN).status_code == 204


def test_logout_only_affects_its_own_session(client: TestClient) -> None:
    signup(client)
    first = refresh_cookie(login(client))
    second = refresh_cookie(login(client))  # second device/login: separate family
    set_refresh_cookie(client, first)
    assert client.post("/api/v1/auth/logout", headers=ORIGIN).status_code == 204
    set_refresh_cookie(client, second)
    assert client.post("/api/v1/auth/refresh", headers=ORIGIN).status_code == 200


# --------------------------------------------------------------- misc / CORS


def test_cors_allows_only_configured_origin(client: TestClient) -> None:
    ok = client.options(
        "/api/v1/auth/login",
        headers={"Origin": "http://localhost:3000", "Access-Control-Request-Method": "POST"},
    )
    assert ok.headers.get("access-control-allow-origin") == "http://localhost:3000"
    assert ok.headers.get("access-control-allow-credentials") == "true"
    bad = client.options(
        "/api/v1/auth/login",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"},
    )
    assert "access-control-allow-origin" not in bad.headers


def test_no_password_or_token_is_written_to_logs(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG")
    signup(client)
    body: dict[str, Any] = login(client).json()
    client.post("/api/v1/auth/logout", headers=ORIGIN)
    logged = "\n".join(r.getMessage() + str(r.__dict__) for r in caplog.records)
    assert PASSWORD not in logged
    assert body["access_token"] not in logged
    assert "$argon2" not in logged
