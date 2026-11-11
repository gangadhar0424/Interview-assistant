"""Phase 13A Batch 2: login/signup throttling and the signup gate through the real app."""

from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from app.auth import security, service
from app.core.config import Settings, get_settings
from app.core.security_headers import BASELINE_HEADERS

PASSWORD = "correct horse battery staple"  # noqa: S105
WRONG = "definitely the wrong password"  # noqa: S105
INVITE = "an-invite-code-for-tests-only"
PUBLIC_A = "8.8.8.8"
PUBLIC_B = "1.1.1.1"
Make = Callable[..., TestClient]


def set_peer(client: TestClient, address: str) -> None:
    """Make the test client connect from `address` (the TCP peer the API sees)."""
    transport: Any = client._transport
    transport.client = (address, 40000)


def signup(client: TestClient, email: str, code: str | None = None, **headers: str) -> Response:
    body: dict[str, Any] = {"email": email, "password": PASSWORD}
    if code is not None:
        body["invite_code"] = code
    out: Response = client.post("/api/v1/auth/signup", json=body, headers=headers)
    return out


def login(client: TestClient, email: str, password: str = PASSWORD, **headers: str) -> Response:
    out: Response = client.post(
        "/api/v1/auth/login", json={"email": email, "password": password}, headers=headers
    )
    return out


def error_of(response: Response) -> tuple[str, str]:
    body = response.json()["error"]
    return body["code"], body["message"]


# ------------------------------------------------------------------ login


def test_login_is_blocked_after_the_configured_failures(make_client: Make) -> None:
    client = make_client(LOGIN_FAILURES_PER_EMAIL="3")
    assert signup(client, "a@example.com").status_code == 201
    for _ in range(3):
        assert login(client, "a@example.com", WRONG).status_code == 401
    blocked = login(client, "a@example.com", WRONG)
    assert blocked.status_code == 429
    assert error_of(blocked)[0] == "rate_limited"
    assert int(blocked.headers["Retry-After"]) >= 1
    # Even the CORRECT password is refused while blocked, and that is not a signal of validity.
    assert login(client, "a@example.com", PASSWORD).status_code == 429


def test_a_blocked_response_carries_the_security_headers(make_client: Make) -> None:
    client = make_client(LOGIN_FAILURES_PER_EMAIL="1")
    login(client, "a@example.com", WRONG)
    blocked = login(client, "a@example.com", WRONG)
    assert blocked.status_code == 429
    for name, value in BASELINE_HEADERS.items():
        assert blocked.headers[name] == value
    assert blocked.headers["Cache-Control"] == "no-store"
    assert "Retry-After" in blocked.headers


def test_known_and_unknown_emails_are_indistinguishable(make_client: Make) -> None:
    client = make_client(LOGIN_FAILURES_PER_EMAIL="2")
    signup(client, "real@example.com")
    outcomes = []
    for email in ("real@example.com", "ghost@example.com"):
        statuses = [login(client, email, WRONG).status_code for _ in range(2)]
        blocked = login(client, email, WRONG)
        outcomes.append((statuses, blocked.status_code, error_of(blocked)))
    assert outcomes[0] == outcomes[1]
    assert outcomes[0] == (
        [401, 401],
        429,
        ("rate_limited", "Too many attempts. Please try again later."),
    )


def test_one_email_being_blocked_does_not_affect_others(make_client: Make) -> None:
    client = make_client(LOGIN_FAILURES_PER_EMAIL="2")
    signup(client, "a@example.com")
    signup(client, "b@example.com")
    for _ in range(2):
        login(client, "a@example.com", WRONG)
    assert login(client, "a@example.com", PASSWORD).status_code == 429
    assert login(client, "b@example.com", PASSWORD).status_code == 200


def test_a_successful_login_clears_the_failure_count(make_client: Make) -> None:
    client = make_client(LOGIN_FAILURES_PER_EMAIL="3")
    signup(client, "a@example.com")
    for _ in range(2):
        login(client, "a@example.com", WRONG)
    assert login(client, "a@example.com", PASSWORD).status_code == 200
    for _ in range(2):  # would have been the 4th and 5th failure without the reset
        assert login(client, "a@example.com", WRONG).status_code == 401
    assert login(client, "a@example.com", PASSWORD).status_code == 200


def test_the_email_is_normalized_before_counting(make_client: Make) -> None:
    client = make_client(LOGIN_FAILURES_PER_EMAIL="2")
    login(client, "Mixed@Example.com", WRONG)
    login(client, "  mixed@example.COM", WRONG)
    assert login(client, "mixed@example.com", WRONG).status_code == 429


def test_a_blocked_attempt_does_no_password_hashing(
    make_client: Make, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_client(LOGIN_FAILURES_PER_EMAIL="1")
    signup(client, "a@example.com")
    login(client, "a@example.com", WRONG)
    calls: list[str] = []
    real_verify, real_burn = security.verify_password, security.burn_password_check

    def counting_verify(*args: Any, **kwargs: Any) -> Any:
        calls.append("verify")
        return real_verify(*args, **kwargs)

    def counting_burn(*args: Any, **kwargs: Any) -> Any:
        calls.append("burn")
        return real_burn(*args, **kwargs)

    monkeypatch.setattr(security, "verify_password", counting_verify)
    monkeypatch.setattr(security, "burn_password_check", counting_burn)
    assert login(client, "a@example.com", WRONG).status_code == 429
    assert calls == []  # blocked before Argon2: a throttled attacker costs almost nothing


def test_throttling_does_not_log_emails_passwords_or_addresses(
    make_client: Make, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG")
    client = make_client(LOGIN_FAILURES_PER_EMAIL="1")
    set_peer(client, PUBLIC_A)
    login(client, "private.person@example.com", WRONG)
    login(client, "private.person@example.com", WRONG)
    logged = "\n".join(r.getMessage() + str(r.__dict__) for r in caplog.records)
    for secret in ("private.person", WRONG, PUBLIC_A):
        assert secret not in logged


def test_a_public_client_is_limited_across_emails(make_client: Make) -> None:
    client = make_client(LOGIN_FAILURES_PER_CLIENT="3", LOGIN_FAILURES_PER_EMAIL="100")
    set_peer(client, PUBLIC_A)
    for i in range(3):
        assert login(client, f"user{i}@example.com", WRONG).status_code == 401
    assert login(client, "someone.else@example.com", WRONG).status_code == 429
    set_peer(client, PUBLIC_B)  # a different client is unaffected
    assert login(client, "someone.else@example.com", WRONG).status_code == 401


def test_a_shared_local_peer_never_gets_a_shared_bucket(make_client: Make) -> None:
    """Behind a local proxy every user has the same (loopback) peer: no per-client limit."""
    client = make_client(LOGIN_FAILURES_PER_CLIENT="2", LOGIN_FAILURES_PER_EMAIL="100")
    set_peer(client, "127.0.0.1")
    for i in range(10):
        assert login(client, f"user{i}@example.com", WRONG).status_code == 401


def test_forwarded_for_is_ignored_unless_the_proxy_is_trusted(make_client: Make) -> None:
    client = make_client(LOGIN_FAILURES_PER_CLIENT="2", LOGIN_FAILURES_PER_EMAIL="100")
    set_peer(client, PUBLIC_A)
    for i in range(2):
        login(client, f"user{i}@example.com", WRONG, **{"X-Forwarded-For": f"9.9.9.{i}"})
    # Rotating the header does not evade the limit: the real peer is what counts.
    assert (
        login(client, "x@example.com", WRONG, **{"X-Forwarded-For": "9.9.9.99"}).status_code == 429
    )
    # And a forged header cannot push the blame onto someone else's address either.
    set_peer(client, PUBLIC_B)
    assert login(client, "y@example.com", WRONG, **{"X-Forwarded-For": PUBLIC_A}).status_code == 401


def test_with_a_trusted_proxy_the_forwarded_client_is_limited(make_client: Make) -> None:
    client = make_client(
        LOGIN_FAILURES_PER_CLIENT="2",
        LOGIN_FAILURES_PER_EMAIL="100",
        TRUSTED_PROXY_ENABLED="true",
        TRUSTED_PROXY_HOPS="1",
    )
    set_peer(client, "127.0.0.1")  # the proxy
    for i in range(2):
        login(client, f"user{i}@example.com", WRONG, **{"X-Forwarded-For": PUBLIC_A})
    assert login(client, "z@example.com", WRONG, **{"X-Forwarded-For": PUBLIC_A}).status_code == 429
    # A forged extra entry on the left does not change which client is counted.
    forged = {"X-Forwarded-For": f"4.4.4.4, {PUBLIC_A}"}
    assert login(client, "z@example.com", WRONG, **forged).status_code == 429
    # A different client behind the same proxy is independent.
    assert login(client, "z@example.com", WRONG, **{"X-Forwarded-For": PUBLIC_B}).status_code == 401
    # A missing or malformed header means no client key (and no shared bucket).
    assert login(client, "z@example.com", WRONG).status_code == 401
    assert login(client, "z@example.com", WRONG, **{"X-Forwarded-For": "junk"}).status_code == 401


# ----------------------------------------------------------------- signup


def test_local_signup_is_open_without_an_invite_code(make_client: Make) -> None:
    client = make_client()
    assert signup(client, "open@example.com").status_code == 201


def test_a_configured_code_is_required_and_the_failure_is_generic(make_client: Make) -> None:
    client = make_client(SIGNUP_INVITE_CODE=INVITE)
    assert signup(client, "first@example.com", INVITE).status_code == 201
    duplicate = signup(client, "first@example.com", INVITE)  # a real duplicate-email failure
    missing = signup(client, "new1@example.com")
    wrong = signup(client, "new2@example.com", "not-the-code")
    empty = signup(client, "new3@example.com", "")
    for refused in (duplicate, missing, wrong, empty):
        assert refused.status_code == 409
        assert error_of(refused) == (
            "signup_failed",
            "An account could not be created with these details.",
        )
    # Same code and message; only the per-request id differs.
    assert {error_of(r) for r in (missing, wrong, duplicate, empty)} == {error_of(duplicate)}
    # Nothing was created by the refused attempts.
    assert login(client, "new1@example.com").status_code == 401


def test_a_refused_signup_spends_the_same_hashing_work(
    make_client: Make, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_client(SIGNUP_INVITE_CODE=INVITE)
    calls: list[str] = []
    real = service.burn_signup_work

    def counting(password: str) -> None:
        calls.append("b")
        real(password)

    monkeypatch.setattr(service, "burn_signup_work", counting)
    assert signup(client, "x@example.com", "wrong").status_code == 409
    assert calls == ["b"]


def test_the_gate_is_closed_outside_local_without_a_code(make_client: Make) -> None:
    client = make_client()
    base: Settings = get_settings()

    def deployed() -> Settings:
        return base.model_copy(update={"app_env": "production"})

    client.app.dependency_overrides[get_settings] = deployed  # type: ignore[attr-defined]
    for code in (None, "", "guess", INVITE):
        refused = signup(client, "closed@example.com", code)
        assert refused.status_code == 409 and error_of(refused)[0] == "signup_failed"


def test_signup_attempts_are_limited_per_public_client(make_client: Make) -> None:
    client = make_client(SIGNUP_ATTEMPTS_PER_CLIENT_PER_HOUR="2")
    set_peer(client, PUBLIC_A)
    assert signup(client, "a1@example.com").status_code == 201
    assert signup(client, "a2@example.com").status_code == 201
    limited = signup(client, "a3@example.com")
    assert limited.status_code == 429 and int(limited.headers["Retry-After"]) >= 1
    set_peer(client, PUBLIC_B)
    assert signup(client, "b1@example.com").status_code == 201


def test_failed_gate_checks_have_a_global_ceiling(make_client: Make) -> None:
    client = make_client(SIGNUP_INVITE_CODE=INVITE, SIGNUP_FAILURES_GLOBAL_PER_HOUR="3")
    for i in range(3):
        assert signup(client, f"g{i}@example.com", "guess").status_code == 409
    over = signup(client, "g9@example.com", "another-guess")
    assert over.status_code == 429 and error_of(over)[0] == "rate_limited"
    # Documented trade-off: while the ceiling holds, even the right code waits (signup only).
    assert signup(client, "g10@example.com", INVITE).status_code == 429
    assert login(client, "g10@example.com").status_code == 401  # login is not affected


def test_the_invite_code_is_never_echoed_or_logged(
    make_client: Make, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG")
    client = make_client(SIGNUP_INVITE_CODE=INVITE)
    invalid = client.post(
        "/api/v1/auth/signup",
        json={"email": "not-an-email", "password": PASSWORD, "invite_code": "leak-canary-code-1"},
    )
    assert invalid.status_code == 422
    signup(client, "ok@example.com", "wrong-code-canary-2")
    assert "leak-canary-code-1" not in invalid.text
    logged = "\n".join(r.getMessage() + str(r.__dict__) for r in caplog.records)
    for secret in ("leak-canary-code-1", "wrong-code-canary-2", INVITE):
        assert secret not in logged


def test_an_oversized_invite_code_is_rejected_by_validation(make_client: Make) -> None:
    client = make_client(SIGNUP_INVITE_CODE=INVITE)
    response = signup(client, "big@example.com", "x" * 129)
    assert response.status_code == 422
    assert "x" * 129 not in response.text


def test_throttling_does_not_change_the_successful_flows(make_client: Make) -> None:
    client = make_client(SIGNUP_INVITE_CODE=INVITE)
    assert signup(client, "flow@example.com", INVITE).status_code == 201
    ok = login(client, "flow@example.com")
    assert ok.status_code == 200 and ok.headers["Cache-Control"] == "no-store"
    assert "refresh_token" in ok.headers["set-cookie"]
    token = ok.json()["access_token"]
    me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
