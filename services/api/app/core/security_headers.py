"""Baseline security response headers for every HTTP response of the API (Phase 13A).

Existing headers are never overwritten (`setdefault`): the file download route keeps its own
`Cache-Control`/`X-Content-Type-Options`, the SSE stream keeps `no-cache, no-transform`, and the
login/refresh responses keep `no-store`. HSTS is deliberately not set here: it belongs to the TLS
terminating proxy (Phase 14A), because the API itself is served over plain HTTP behind it.

The API returns JSON, event streams and file downloads, never HTML, so a restrictive CSP is safe,
except for the interactive docs pages (`/docs`, `/redoc`), which load their own scripts.
"""

from typing import Protocol


class SupportsSetDefault(Protocol):
    """A dict or Starlette `MutableHeaders`: only `setdefault` is needed."""

    def setdefault(self, key: str, value: str) -> str: ...


AUTH_PATH_PREFIX = "/api/v1/auth/"
_DOCS_PATHS = frozenset({"/docs", "/redoc", "/docs/oauth2-redirect"})

BASELINE_HEADERS: dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}
_API_CSP = "default-src 'none'; frame-ancestors 'none'"


def apply_security_headers(path: str, headers: SupportsSetDefault) -> None:
    for name, value in BASELINE_HEADERS.items():
        headers.setdefault(name, value)
    if path not in _DOCS_PATHS:
        headers.setdefault("Content-Security-Policy", _API_CSP)
    if path.startswith(AUTH_PATH_PREFIX):
        # Tokens and session state must never be stored by a browser or an intermediary.
        headers.setdefault("Cache-Control", "no-store")
