"""Phase 13A Batch 2 (no database): limiter additions, client key, auth limits, invite gate."""

import pytest

from app.auth.client_ip import client_key
from app.auth.limits import THROTTLED_MESSAGE, AuthLimits, invite_allows
from app.core.config import ConfigurationError, Settings, load_settings
from app.core.errors import AppError
from app.jobs_import.limiter import SlidingWindowLimiter


@pytest.fixture(autouse=True)
def _no_env_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(Settings.model_config, "env_file", None)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


# ------------------------------------------------------- limiter additions


def test_blocked_does_not_consume_and_record_does() -> None:
    limiter = SlidingWindowLimiter(2, 60.0, Clock())
    assert limiter.blocked("a") is False
    assert limiter.blocked("a") is False  # peeking never records
    limiter.record("a")
    assert limiter.blocked("a") is False
    limiter.record("a")
    assert limiter.blocked("a") is True


def test_blocked_clears_after_the_window() -> None:
    clock = Clock()
    limiter = SlidingWindowLimiter(1, 60.0, clock)
    limiter.record("a")
    assert limiter.blocked("a") is True
    assert 1 <= limiter.retry_after("a") <= 61
    clock.now += 61
    assert limiter.blocked("a") is False


def test_reset_clears_one_key_only() -> None:
    limiter = SlidingWindowLimiter(1, 60.0, Clock())
    limiter.record("a")
    limiter.record("b")
    limiter.reset("a")
    assert limiter.blocked("a") is False
    assert limiter.blocked("b") is True
    limiter.reset("never-seen")  # no error


def test_existing_allow_behaviour_is_unchanged() -> None:
    limiter = SlidingWindowLimiter(2, 60.0, Clock())
    assert limiter.allow("k") and limiter.allow("k")
    assert limiter.allow("k") is False


def test_many_keys_stay_bounded() -> None:
    clock = Clock()
    limiter = SlidingWindowLimiter(1, 60.0, clock)
    for i in range(10_000):
        limiter.record(f"k{i}")
    clock.now += 61  # every old key has expired
    for i in range(10_000, 10_010):
        limiter.record(f"k{i}")
    assert len(limiter._events) < 10_100


# ------------------------------------------------------------ client key

PUBLIC = "8.8.8.8"


@pytest.mark.parametrize(
    ("peer", "forwarded", "expected"),
    [
        (PUBLIC, None, PUBLIC),
        (PUBLIC, "1.2.3.4", PUBLIC),  # header ignored when the proxy is not trusted
        ("127.0.0.1", "1.2.3.4", None),  # loopback = a local proxy: no per-client key
        ("::1", None, None),
        ("10.1.2.3", None, None),
        ("192.168.0.5", None, None),
        ("169.254.1.1", None, None),
        ("0.0.0.0", None, None),  # noqa: S104
        ("testclient", None, None),  # not an IP address
        (None, None, None),
        ("", None, None),
        ("::ffff:8.8.8.8", None, PUBLIC),  # IPv4-mapped IPv6 is normalized
        ("2606:4700:4700::1111", None, "2606:4700:4700::1111"),
        ("203.0.113.9", None, None),  # documentation range: not a real client
        ("100.64.0.1", None, None),  # carrier-grade NAT: shared by many clients
        ("224.0.0.1", None, None),  # multicast
        ("2001:db8::1", None, None),
    ],
)
def test_client_key_without_a_trusted_proxy(
    peer: str | None, forwarded: str | None, expected: str | None
) -> None:
    assert client_key(peer, forwarded, trusted_proxy=False, hops=1) == expected


@pytest.mark.parametrize(
    ("forwarded", "hops", "expected"),
    [
        ("9.9.9.9", 1, "9.9.9.9"),
        ("1.1.1.1, 9.9.9.9", 1, "9.9.9.9"),  # a forged leftmost entry is ignored
        ("1.1.1.1, 9.9.9.9, 10.0.0.2", 2, "9.9.9.9"),
        ("9.9.9.9", 2, None),  # fewer entries than trusted hops
        ("", 1, None),
        (None, 1, None),
        ("not-an-ip", 1, None),
        ("9.9.9.9, garbage", 1, None),
        (", ,", 1, None),
        ("2606:4700:4700::1111", 1, "2606:4700:4700::1111"),
        (", ".join(["1.1.1.1"] * 25), 1, None),  # absurdly long header
    ],
)
def test_client_key_with_a_trusted_proxy(
    forwarded: str | None, hops: int, expected: str | None
) -> None:
    assert client_key("127.0.0.1", forwarded, trusted_proxy=True, hops=hops) == expected


def test_trusted_proxy_never_falls_back_to_the_proxy_address() -> None:
    assert client_key("127.0.0.1", None, trusted_proxy=True, hops=1) is None
    assert client_key(PUBLIC, "garbage", trusted_proxy=True, hops=1) is None


# ---------------------------------------------------------- AuthLimits


def _limits(monkeypatch: pytest.MonkeyPatch, **env: str) -> AuthLimits:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return AuthLimits(load_settings())


def _blocked(call: object) -> AppError:
    with pytest.raises(AppError) as caught:
        call()  # type: ignore[operator]
    return caught.value


def test_login_is_blocked_after_the_configured_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    limits = _limits(monkeypatch, LOGIN_FAILURES_PER_EMAIL="3")
    for _ in range(3):
        limits.check_login("a@example.com", None)
        limits.record_login_failure("a@example.com", None)
    error = _blocked(lambda: limits.check_login("a@example.com", None))
    assert (error.status_code, error.code, error.message) == (
        429,
        "rate_limited",
        THROTTLED_MESSAGE,
    )
    assert int(error.headers["Retry-After"]) >= 1
    limits.check_login("b@example.com", None)  # other emails are independent


def test_success_clears_the_email_counter(monkeypatch: pytest.MonkeyPatch) -> None:
    limits = _limits(monkeypatch, LOGIN_FAILURES_PER_EMAIL="2")
    limits.record_login_failure("a@example.com", None)
    limits.record_login_failure("a@example.com", None)
    assert _blocked(lambda: limits.check_login("a@example.com", None)).status_code == 429
    limits.record_login_success("a@example.com")
    limits.check_login("a@example.com", None)


def test_client_limit_applies_only_with_a_client_key(monkeypatch: pytest.MonkeyPatch) -> None:
    limits = _limits(monkeypatch, LOGIN_FAILURES_PER_CLIENT="2", LOGIN_FAILURES_PER_EMAIL="100")
    for i in range(5):  # different emails, no client key: never blocked
        limits.record_login_failure(f"u{i}@example.com", None)
        limits.check_login(f"u{i}@example.com", None)
    for i in range(2):
        limits.record_login_failure(f"v{i}@example.com", PUBLIC)
    assert _blocked(lambda: limits.check_login("fresh@example.com", PUBLIC)).code == "rate_limited"
    limits.check_login("fresh@example.com", "1.1.1.1")  # another client is fine
    limits.check_login("fresh@example.com", None)


def test_signup_attempts_are_limited_per_client_only_when_known(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limits = _limits(monkeypatch, SIGNUP_ATTEMPTS_PER_CLIENT_PER_HOUR="2")
    for _ in range(50):
        limits.check_signup(None)  # unknown client: no per-client limit
    limits.check_signup(PUBLIC)
    limits.check_signup(PUBLIC)
    error = _blocked(lambda: limits.check_signup(PUBLIC))
    assert error.status_code == 429 and "Retry-After" in error.headers
    limits.check_signup("1.1.1.1")


def test_failed_signups_have_a_global_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    limits = _limits(monkeypatch, SIGNUP_FAILURES_GLOBAL_PER_HOUR="3")
    for _ in range(3):
        limits.check_signup(None)
        limits.record_signup_failure()
    assert _blocked(lambda: limits.check_signup(None)).code == "rate_limited"


def test_limiter_keys_are_digests_not_values(monkeypatch: pytest.MonkeyPatch) -> None:
    limits = _limits(monkeypatch)
    limits.record_login_failure("secret.person@example.com", PUBLIC)
    stored = " ".join(list(limits._login_email._events) + list(limits._login_client._events))
    assert "secret.person" not in stored and PUBLIC not in stored
    assert AuthLimits(load_settings())._key("x", "v") != limits._key("x", "v")  # per-process salt


def test_defaults_match_the_approved_limits() -> None:
    s = load_settings()
    assert (s.login_failures_per_email, s.login_failures_per_client, s.login_window_seconds) == (
        5,
        20,
        900,
    )
    assert (s.signup_attempts_per_client_per_hour, s.signup_failures_global_per_hour) == (10, 30)
    assert s.trusted_proxy_enabled is False and s.trusted_proxy_hops == 1
    assert s.signup_invite_code is None


# ----------------------------------------------------------- invite gate


def test_local_without_a_code_is_open(monkeypatch: pytest.MonkeyPatch) -> None:
    s = load_settings()
    assert invite_allows(s, None) is True and invite_allows(s, "anything") is True


def test_a_configured_code_is_always_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIGNUP_INVITE_CODE", "local-dev-invite-code")
    s = load_settings()
    assert invite_allows(s, "local-dev-invite-code") is True
    assert invite_allows(s, None) is False
    assert invite_allows(s, "") is False
    assert invite_allows(s, "local-dev-invite-cod") is False
    assert invite_allows(s, "LOCAL-DEV-INVITE-CODE") is False


def test_blank_code_means_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIGNUP_INVITE_CODE", "   ")
    assert load_settings().signup_invite_code is None


def _deployment(monkeypatch: pytest.MonkeyPatch) -> None:
    import tempfile
    from pathlib import Path

    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "fastembed")
    monkeypatch.setenv("JWT_SECRET", "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0U1v2W3x4")
    monkeypatch.setenv("REDIS_URL", "rediss://redis.example.invalid:6379/0")
    monkeypatch.setenv(
        "MAINTENANCE_DATABASE_URL",
        "postgresql+asyncpg://maintenance:placeholder@db.example.invalid/app",
    )
    monkeypatch.setenv("FILE_STORAGE_ROOT", str(Path(tempfile.gettempdir()) / "persistent-volume"))


def test_signup_is_closed_by_default_outside_local(monkeypatch: pytest.MonkeyPatch) -> None:
    _deployment(monkeypatch)
    s = load_settings()  # valid without a code: signup is simply closed
    assert s.signup_invite_code is None
    assert invite_allows(s, None) is False
    assert invite_allows(s, "any-guess-at-all-1234") is False


def test_a_short_code_is_rejected_outside_local(monkeypatch: pytest.MonkeyPatch) -> None:
    _deployment(monkeypatch)
    monkeypatch.setenv("SIGNUP_INVITE_CODE", "short-code-15chr")  # 16: allowed
    load_settings()
    monkeypatch.setenv("SIGNUP_INVITE_CODE", "short-code-15ch")  # 15
    with pytest.raises(ConfigurationError) as caught:
        load_settings()
    assert "SIGNUP_INVITE_CODE" in str(caught.value)
    assert "short-code-15ch" not in str(caught.value)  # the value is never echoed


def test_the_right_code_opens_signup_outside_local(monkeypatch: pytest.MonkeyPatch) -> None:
    _deployment(monkeypatch)
    monkeypatch.setenv("SIGNUP_INVITE_CODE", "a-long-enough-invite-code")
    s = load_settings()
    assert invite_allows(s, "a-long-enough-invite-code") is True
    assert invite_allows(s, "another-long-enough-code") is False


def test_the_comparison_is_constant_time(monkeypatch: pytest.MonkeyPatch) -> None:
    import inspect

    import app.auth.limits as module

    assert "compare_digest" in inspect.getsource(module.invite_allows)
