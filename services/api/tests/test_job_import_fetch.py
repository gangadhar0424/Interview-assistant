"""SafeFetcher against a local server: redirects, pinning, caps, content rules, timeouts, TLS."""

import socket
import ssl
import time
from collections.abc import Iterator
from typing import Any

import pytest

from app.jobs_import import errors
from app.jobs_import.errors import ImportFailure
from app.jobs_import.fetch import (
    USER_AGENT,
    SafeFetcher,
    _PinnedHTTPSConnection,
)
from tests.job_server import LocalServer, drip, local_server, page, redirect, slow

OK_HTML = "<html><head><title>Hello</title></head><body>ok</body></html>"


@pytest.fixture
def server() -> Iterator[LocalServer]:
    with local_server() as s:
        yield s


def fetcher(server: LocalServer, **kw: Any) -> SafeFetcher:
    kw.setdefault("timeout", 5.0)
    kw.setdefault("resolver", server.resolver({"internal.test": "10.0.0.5"}))
    return SafeFetcher(policy=server.policy(), **kw)


def reason(f: SafeFetcher, url: str) -> str:
    with pytest.raises(ImportFailure) as exc:
        f.fetch(url)
    return exc.value.reason


@pytest.fixture
def connections(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Records every address a socket is opened to (and still performs the connection)."""
    seen: list[Any] = []
    real = socket.create_connection

    def recording(address: Any, *args: Any, **kwargs: Any) -> Any:
        seen.append(address)
        return real(address, *args, **kwargs)

    monkeypatch.setattr(socket, "create_connection", recording)
    return seen


# ------------------------------------------------------------------- basics


def test_fetches_html_and_reports_charset(server: LocalServer) -> None:
    server.routes["/job"] = page(OK_HTML, content_type="text/html; charset=ISO-8859-1")
    result = fetcher(server).fetch(server.url("/job"))
    assert b"Hello" in result.body and result.charset == "iso-8859-1"


@pytest.mark.parametrize(
    "ctype", ["text/html", "TEXT/HTML; charset=utf-8", "application/xhtml+xml"]
)
def test_html_content_types_are_accepted(server: LocalServer, ctype: str) -> None:
    server.routes["/job"] = page(OK_HTML, content_type=ctype)
    assert fetcher(server).fetch(server.url("/job")).body


@pytest.mark.parametrize(
    "ctype",
    ["application/pdf", "image/png", "application/json", "text/plain", "application/octet-stream",
     "application/zip", "video/mp4", "", None],
)  # fmt: skip
def test_other_content_types_are_rejected(server: LocalServer, ctype: str | None) -> None:
    server.routes["/f"] = page(b"%PDF-1.4 binary", content_type=ctype)
    assert reason(fetcher(server), server.url("/f")) == errors.CONTENT


def test_compressed_responses_are_rejected(server: LocalServer) -> None:
    server.routes["/z"] = page(OK_HTML, headers={"Content-Encoding": "gzip"})
    assert reason(fetcher(server), server.url("/z")) == errors.CONTENT


@pytest.mark.parametrize("status", [204, 400, 401, 403, 404, 410, 429, 500, 502, 503])
def test_non_200_responses_are_failures(server: LocalServer, status: int) -> None:
    server.routes["/s"] = page(OK_HTML, status=status)
    assert reason(fetcher(server), server.url("/s")) == errors.HTTP


def test_a_redirect_without_a_location_is_a_failure(server: LocalServer) -> None:
    server.routes["/r"] = page("", status=302)
    assert reason(fetcher(server), server.url("/r")) == errors.HTTP


# ------------------------------------------------------- request hygiene


def test_requests_are_anonymous_identity_only_and_identify_themselves(server: LocalServer) -> None:
    server.routes["/job"] = page(OK_HTML)
    fetcher(server).fetch(server.url("/job"))
    headers = {k.lower(): v for k, v in server.requests[0][1].items()}
    assert headers["user-agent"] == USER_AGENT and "InterviewAssistant" in headers["user-agent"]
    assert headers["accept-encoding"] == "identity"
    assert headers["host"] == f"jobs.test:{server.port}"  # the hostname, not the pinned IP
    assert headers["connection"] == "close"
    for forbidden in ("cookie", "authorization", "proxy-authorization", "referer", "origin"):
        assert forbidden not in headers


def test_cookies_set_by_the_server_are_never_sent_back(server: LocalServer) -> None:
    server.routes["/a"] = redirect("/b", headers={"Set-Cookie": "session=secret; Path=/"})
    server.routes["/b"] = page(OK_HTML)
    fetcher(server).fetch(server.url("/a"))
    assert len(server.requests) == 2
    assert all("cookie" not in {k.lower() for k in headers} for _, headers in server.requests)


def test_the_fragment_is_never_sent(server: LocalServer) -> None:
    server.routes["/job"] = page(OK_HTML)
    fetcher(server).fetch(server.url("/job?id=7#section"))
    assert server.requests[0][0] == "/job?id=7"


# ---------------------------------------------------------------- redirects


def test_relative_and_absolute_redirects_are_followed(server: LocalServer) -> None:
    server.routes["/a"] = redirect("/b")
    server.routes["/b"] = redirect(server.url("/c"), status=301)
    server.routes["/c"] = page(OK_HTML)
    assert fetcher(server).fetch(server.url("/a")).body
    assert [p for p, _ in server.requests] == ["/a", "/b", "/c"]


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_all_standard_redirect_statuses_are_followed(server: LocalServer, status: int) -> None:
    server.routes["/a"] = redirect("/b", status=status)
    server.routes["/b"] = page(OK_HTML)
    assert fetcher(server).fetch(server.url("/a")).body


def test_redirect_limit_allows_exactly_max_redirects(server: LocalServer) -> None:
    for src, dst in (("/a", "/b"), ("/b", "/c"), ("/c", "/d")):
        server.routes[src] = redirect(dst)
    server.routes["/d"] = page(OK_HTML)
    assert fetcher(server, max_redirects=3).fetch(server.url("/a")).body  # 3 redirects: fine
    server.routes["/d"] = redirect("/e")
    server.routes["/e"] = page(OK_HTML)
    assert reason(fetcher(server, max_redirects=3), server.url("/a")) == errors.REDIRECTS


def test_redirect_loops_terminate(server: LocalServer) -> None:
    server.routes["/loop"] = redirect("/loop")
    assert reason(fetcher(server), server.url("/loop")) == errors.REDIRECTS
    assert len(server.requests) == 4  # initial request + 3 redirects, then stop


def test_redirect_to_an_internal_host_is_blocked_before_any_connection(
    server: LocalServer, connections: list[Any]
) -> None:
    server.routes["/r"] = redirect(server.url("/secret", host="internal.test"))
    assert reason(fetcher(server), server.url("/r")) == errors.BLOCKED
    assert [p for p, _ in server.requests] == ["/r"]
    assert all(addr[0] != "10.0.0.5" for addr in connections)  # never even attempted


@pytest.mark.parametrize(
    "location",
    [
        "file:///etc/passwd", "gopher://jobs.test/x", "javascript:alert(1)", "ftp://jobs.test/x",
        "data:text/html,boom", "http://user:pw@jobs.test:{port}/x", "http://jobs.test:22/x",
        "http://jobs.test:6379/x", "http://169.254.169.254/latest/meta-data/",
        "http://metadata.google.internal/", "//internal.test:{port}/x",
        "http://[::1]:{port}/x", "http://127.0.0.1:{port}/admin",
    ],
)  # fmt: skip
def test_hostile_redirect_targets_are_never_followed(server: LocalServer, location: str) -> None:
    server.routes["/r"] = redirect(location.format(port=server.port))
    server.routes["/admin"] = page("INTERNAL ADMIN")
    server.routes["/x"] = page("SHOULD NOT BE FETCHED")
    with pytest.raises(ImportFailure):
        fetcher(server).fetch(server.url("/r"))
    assert [p for p, _ in server.requests] == ["/r"]


def test_a_very_long_location_header_is_rejected(server: LocalServer) -> None:
    server.routes["/r"] = redirect("/" + "a" * 3000)
    assert reason(fetcher(server), server.url("/r")) == errors.HTTP


# ---------------------------------------------- pinning (DNS rebinding defence)


def test_the_connection_uses_the_validated_ip_and_resolves_once_per_hop(
    server: LocalServer, connections: list[Any]
) -> None:
    calls: list[str] = []

    def rebinding(host: str, port: int) -> list[str]:
        calls.append(host)
        # First answer: the (allowed) test server; a second lookup would point inside the network.
        return ["127.0.0.1"] if len(calls) == 1 else ["10.0.0.9"]

    server.routes["/job"] = page(OK_HTML)
    f = SafeFetcher(timeout=5.0, resolver=rebinding, policy=server.policy())
    assert f.fetch(server.url("/job")).body  # would fail if the hostname were looked up again
    assert calls == ["jobs.test"]
    assert connections == [("127.0.0.1", server.port)]


def test_each_redirect_hop_is_resolved_and_validated_again(server: LocalServer) -> None:
    calls: list[str] = []
    base = server.resolver({"other.test": "127.0.0.1"})

    def counting(host: str, port: int) -> list[str]:
        calls.append(host)
        return base(host, port)

    server.routes["/a"] = redirect(server.url("/b", host="other.test"))
    server.routes["/b"] = page(OK_HTML)
    SafeFetcher(resolver=counting, policy=server.policy()).fetch(server.url("/a"))
    assert calls == ["jobs.test", "other.test"]


def test_a_host_that_resolves_to_a_mix_of_public_and_private_is_blocked(
    server: LocalServer, connections: list[Any]
) -> None:
    f = SafeFetcher(resolver=lambda h, p: ["127.0.0.1", "10.0.0.1"], policy=server.policy())
    assert reason(f, server.url("/job")) == errors.BLOCKED
    assert connections == [] and server.requests == []


# ---------------------------------------------------------------- size caps


def test_body_size_cap_is_exact(server: LocalServer) -> None:
    server.routes["/exact"] = page(b"a" * 2000)
    server.routes["/over"] = page(b"a" * 2001)
    f = fetcher(server, max_bytes=2000)
    assert len(f.fetch(server.url("/exact")).body) == 2000
    assert reason(f, server.url("/over")) == errors.TOO_LARGE


def test_an_oversized_body_without_content_length_is_still_capped(server: LocalServer) -> None:
    server.routes["/stream"] = page(b"a" * 50_000, declare_length=False)
    assert reason(fetcher(server, max_bytes=10_000), server.url("/stream")) == errors.TOO_LARGE


def test_a_lying_content_length_cannot_exceed_the_cap(server: LocalServer) -> None:
    server.routes["/lie"] = page(b"a" * 50_000, headers={"X-Note": "no content-length sent"},
                                 declare_length=False)  # fmt: skip
    assert reason(fetcher(server, max_bytes=1000), server.url("/lie")) == errors.TOO_LARGE


# ------------------------------------------------------------------ timeouts


def test_a_server_that_never_answers_times_out_within_the_budget(server: LocalServer) -> None:
    server.routes["/slow"] = slow(3.0, page(OK_HTML))
    started = time.monotonic()
    assert reason(fetcher(server, timeout=0.5), server.url("/slow")) == errors.TIMEOUT
    assert time.monotonic() - started < 2.0


def test_a_slow_drip_response_is_cut_off_by_the_total_deadline(server: LocalServer) -> None:
    server.routes["/drip"] = drip(0.15)
    started = time.monotonic()
    assert reason(fetcher(server, timeout=1.0), server.url("/drip")) == errors.TIMEOUT
    assert time.monotonic() - started < 3.0  # one byte per 0.15 s must not hold the worker


def test_connection_refused_is_a_failure(server: LocalServer) -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        closed_port = s.getsockname()[1]
    from app.jobs_import.ssrf import UrlPolicy, is_public_ip

    policy = UrlPolicy(
        allowed_ports=frozenset({closed_port}),
        ip_allowed=lambda ip: str(ip) == "127.0.0.1" or is_public_ip(ip),
    )
    f = SafeFetcher(timeout=2.0, resolver=server.resolver(), policy=policy)
    # Windows retries a refused connect until the deadline, so it may surface as a timeout.
    assert reason(f, f"http://jobs.test:{closed_port}/") in {errors.CONNECT, errors.TIMEOUT}


# ----------------------------------------------------------------------- TLS


def test_https_connects_to_the_pinned_ip_and_verifies_the_hostname(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[Any] = []
    wrapped: list[dict[str, Any]] = []

    def fake_connect(addr: Any, timeout: Any = None) -> object:
        opened.append(addr)
        return object()

    monkeypatch.setattr(socket, "create_connection", fake_connect)

    class FakeContext:
        def wrap_socket(self, sock: Any, server_hostname: str | None = None) -> Any:
            wrapped.append({"server_hostname": server_hostname})
            return sock

    conn = _PinnedHTTPSConnection("jobs.example.com", 443, "93.184.216.34", 5.0)
    conn._tls = FakeContext()  # type: ignore[assignment]
    conn.connect()
    assert opened == [("93.184.216.34", 443)]  # the validated IP, not the hostname
    assert wrapped == [{"server_hostname": "jobs.example.com"}]  # SNI + certificate hostname


def test_https_uses_a_verifying_tls_context() -> None:
    conn = _PinnedHTTPSConnection("jobs.example.com", 443, "93.184.216.34", 5.0)
    assert conn._tls.verify_mode == ssl.CERT_REQUIRED and conn._tls.check_hostname is True


def test_https_never_falls_back_to_plain_http(server: LocalServer) -> None:
    server.routes["/job"] = page(OK_HTML)
    url = f"https://jobs.test:{server.port}/job"  # a plain-HTTP server speaking to a TLS client
    assert reason(fetcher(server), url) in {errors.TLS, errors.CONNECT}
