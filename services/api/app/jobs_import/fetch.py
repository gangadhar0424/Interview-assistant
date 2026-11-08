"""Safe page fetcher (standard library only; synchronous, run in a worker thread).

Properties enforced here:
- every hop (the first request AND each redirect) is validated and resolved by app.jobs_import.ssrf;
- the socket connects to the validated IP, never to the hostname (no second DNS lookup, so DNS
  rebinding cannot redirect the connection); TLS still verifies the certificate for the hostname;
- redirects are followed manually (limited), nothing else is followed;
- no cookies, no credentials, no compression (identity only), GET only;
- hard caps on total time and on body size, and only HTML content types are accepted.
"""

import http.client
import socket
import ssl
import time
from dataclasses import dataclass
from urllib.parse import urljoin

from app.jobs_import import errors
from app.jobs_import.ssrf import (
    DEFAULT_POLICY,
    Resolver,
    Target,
    UrlPolicy,
    parse_target,
    resolve_and_check,
    system_resolver,
)

USER_AGENT = "InterviewAssistant-JobImport/1.0 (user-initiated fetch of one public page)"
ALLOWED_MEDIA_TYPES = frozenset({"text/html", "application/xhtml+xml"})
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
MAX_LOCATION_LENGTH = 2048
READ_CHUNK = 65536


@dataclass(frozen=True)
class FetchedPage:
    body: bytes
    charset: str | None


@dataclass(frozen=True)
class _Redirect:
    location: str


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """Plain HTTP that connects to a pre-validated IP while keeping the Host header."""

    def __init__(self, host: str, port: int, ip: str, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout)
        self._pinned_ip = ip

    def connect(self) -> None:
        self.sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS to a pre-validated IP; the certificate is verified against the hostname (SNI)."""

    def __init__(self, host: str, port: int, ip: str, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout)
        self._pinned_ip = ip
        self._tls = ssl.create_default_context()  # verifies chain and hostname by default

    def connect(self) -> None:
        raw = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        self.sock = self._tls.wrap_socket(raw, server_hostname=self.host)


class SafeFetcher:
    def __init__(
        self,
        *,
        timeout: float = 8.0,
        max_bytes: int = 1_048_576,
        max_redirects: int = 3,
        resolver: Resolver = system_resolver,
        policy: UrlPolicy = DEFAULT_POLICY,
        user_agent: str = USER_AGENT,
    ) -> None:
        self.policy = policy
        self._timeout = timeout
        self._max_bytes = max_bytes
        self._max_redirects = max_redirects
        self._resolver = resolver
        self._headers = {
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1",
            "Accept-Encoding": "identity",
            "Connection": "close",
        }

    def fetch(self, url: str) -> FetchedPage:
        deadline = time.monotonic() + self._timeout
        current = url
        for _ in range(self._max_redirects + 1):
            target = parse_target(current, self.policy)
            ip = resolve_and_check(target.host, target.port, self._resolver, self.policy)
            result = self._request(target, ip, deadline)
            if isinstance(result, _Redirect):
                current = urljoin(target.url, result.location)
                continue
            return result
        raise errors.ImportFailure(errors.REDIRECTS)

    def _connect(self, target: Target, ip: str, timeout: float) -> http.client.HTTPConnection:
        if target.scheme == "https":
            return _PinnedHTTPSConnection(target.host, target.port, ip, timeout)
        return _PinnedHTTPConnection(target.host, target.port, ip, timeout)

    def _request(self, target: Target, ip: str, deadline: float) -> FetchedPage | _Redirect:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise errors.ImportFailure(errors.TIMEOUT)
        conn = self._connect(target, ip, remaining)
        try:
            try:
                conn.request("GET", target.path, headers=self._headers)
                response = conn.getresponse()
            except TimeoutError:
                raise errors.ImportFailure(errors.TIMEOUT) from None
            except ssl.SSLError:
                raise errors.ImportFailure(errors.TLS) from None
            except (OSError, http.client.HTTPException):
                raise errors.ImportFailure(errors.CONNECT) from None

            if response.status in REDIRECT_STATUSES:
                location = response.getheader("Location")
                if not location or len(location) > MAX_LOCATION_LENGTH:
                    raise errors.ImportFailure(errors.HTTP)
                return _Redirect(location)
            if response.status != 200:
                raise errors.ImportFailure(errors.HTTP)

            media_type = (response.getheader("Content-Type") or "").split(";")[0].strip().lower()
            if media_type not in ALLOWED_MEDIA_TYPES:
                raise errors.ImportFailure(errors.CONTENT)
            if (response.getheader("Content-Encoding") or "identity").strip().lower() != "identity":
                raise errors.ImportFailure(errors.CONTENT)
            declared = response.getheader("Content-Length")
            if declared is not None and declared.isdigit() and int(declared) > self._max_bytes:
                raise errors.ImportFailure(errors.TOO_LARGE)
            body = self._read(response, conn, deadline)
            return FetchedPage(body=body, charset=response.headers.get_content_charset())
        finally:
            conn.close()

    def _read(
        self, response: http.client.HTTPResponse, conn: http.client.HTTPConnection, deadline: float
    ) -> bytes:
        chunks: list[bytes] = []
        total = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise errors.ImportFailure(errors.TIMEOUT)
            if conn.sock is not None:
                conn.sock.settimeout(remaining)  # bounds each read by the TOTAL time left
            try:
                chunk = response.read1(READ_CHUNK)
            except TimeoutError:
                raise errors.ImportFailure(errors.TIMEOUT) from None
            except (OSError, http.client.HTTPException):
                raise errors.ImportFailure(errors.CONNECT) from None
            if not chunk:
                return b"".join(chunks)
            total += len(chunk)
            if total > self._max_bytes:
                raise errors.ImportFailure(errors.TOO_LARGE)
            chunks.append(chunk)
