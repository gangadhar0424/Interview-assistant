"""Brute-force and abuse limits for login and signup (Phase 13A Batch 2, D-54).

In-process only (same trade-off as the other limiters, R-30): with several API workers the limits
are per worker. Keys are salted HMAC digests held in memory only: an email address, an IP address
or an invite code is never stored, returned or logged in clear.

Login counts FAILED attempts per normalized email (always) and per client address (only when a
trustworthy address exists, see `client_ip`). The check runs before any password hashing, so a
blocked request is cheap, and it applies to the submitted email whether or not the account exists,
so a response never reveals whether an email is registered. A successful login clears that email's
counter. Signup counts attempts per client and failed gate checks globally, so the invite code
cannot be guessed even when no client address is available.
"""

import hashlib
import hmac
import secrets

from app.core.config import Settings
from app.core.errors import AppError
from app.jobs_import.limiter import SlidingWindowLimiter

THROTTLED_MESSAGE = "Too many attempts. Please try again later."
_HOUR = 3600.0


def throttled(retry_after: int) -> AppError:
    return AppError(
        429,
        "rate_limited",
        THROTTLED_MESSAGE,
        headers={"Retry-After": str(max(1, retry_after))},
    )


class AuthLimits:
    def __init__(self, settings: Settings) -> None:
        window = float(settings.login_window_seconds)
        self._salt = secrets.token_bytes(32)  # per process; keys cannot be reversed from memory
        self._login_email = SlidingWindowLimiter(settings.login_failures_per_email, window)
        self._login_client = SlidingWindowLimiter(settings.login_failures_per_client, window)
        self._signup_client = SlidingWindowLimiter(
            settings.signup_attempts_per_client_per_hour, _HOUR
        )
        self._signup_failures = SlidingWindowLimiter(
            settings.signup_failures_global_per_hour, _HOUR
        )

    def _key(self, kind: str, value: str) -> str:
        return hmac.new(self._salt, f"{kind}:{value}".encode(), hashlib.sha256).hexdigest()

    # ------------------------------------------------------------------ login

    def check_login(self, email: str, client: str | None) -> None:
        """Raise 429 if this email (or client address) has too many recent failures."""
        waits: list[int] = []
        email_key = self._key("login-email", email)
        if self._login_email.blocked(email_key):
            waits.append(self._login_email.retry_after(email_key))
        if client is not None:
            client_key = self._key("login-client", client)
            if self._login_client.blocked(client_key):
                waits.append(self._login_client.retry_after(client_key))
        if waits:
            raise throttled(max(waits))

    def record_login_failure(self, email: str, client: str | None) -> None:
        self._login_email.record(self._key("login-email", email))
        if client is not None:
            self._login_client.record(self._key("login-client", client))

    def record_login_success(self, email: str) -> None:
        self._login_email.reset(self._key("login-email", email))

    # ----------------------------------------------------------------- signup

    def check_signup(self, client: str | None) -> None:
        """Count this attempt against the client (if known) and refuse when over a limit."""
        if self._signup_failures.blocked("global"):
            raise throttled(self._signup_failures.retry_after("global"))
        if client is not None:
            key = self._key("signup-client", client)
            if not self._signup_client.allow(key):
                raise throttled(self._signup_client.retry_after(key))

    def record_signup_failure(self) -> None:
        self._signup_failures.record("global")


def invite_allows(settings: Settings, supplied: str | None) -> bool:
    """Signup access: closed unless an invite code is configured (open only in local development
    when no code is configured). The comparison is constant time."""
    code = settings.signup_invite_code
    if code is None:
        return settings.app_env == "local"
    if supplied is None:
        return False
    return hmac.compare_digest(supplied.encode(), code.get_secret_value().encode())
