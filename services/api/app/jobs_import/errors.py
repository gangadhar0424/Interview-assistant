"""Internal failure reasons. They are logged, never shown: users only ever see fixed messages."""

INVALID_URL = "invalid_url"
BLOCKED = "blocked_target"
DNS = "dns_failure"
CONNECT = "connect_failed"
TLS = "tls_failure"
TIMEOUT = "timeout"
REDIRECTS = "too_many_redirects"
HTTP = "http_error"
CONTENT = "unsupported_content"
TOO_LARGE = "too_large"
NO_INFO = "no_job_info"
INTERNAL = "internal_error"


class ImportFailure(Exception):
    """A controlled failure of the fetch/extract pipeline. `reason` is one of the constants."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason
