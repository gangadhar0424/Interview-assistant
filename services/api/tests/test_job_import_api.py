"""POST /sessions/job-import: contract, safety, rate limiting, never-blocking manual fallback."""

import asyncio
import json
import logging
import socket
import threading
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from app.core.errors import AppError
from app.jobs_import import errors
from app.jobs_import.fetch import FetchedPage, SafeFetcher
from app.jobs_import.limiter import SlidingWindowLimiter
from app.jobs_import.service import MESSAGES, JobImporter
from tests.conftest import RunSql
from tests.helpers import register
from tests.job_server import LocalServer, local_server, page, slow
from tests.test_sessions import body as session_body
from tests.test_sessions import create as create_session
from tests.test_sessions import new_file

IMPORT = "/api/v1/sessions/job-import"
POSTING = {
    "@context": "https://schema.org",
    "@type": "JobPosting",
    "title": "Platform Engineer",
    "hiringOrganization": {"name": "Initech"},
    "description": "<p>Run the platform.</p>",
}
JSON_LD_PAGE = (
    "<html><head><script type='application/ld+json'>" + json.dumps(POSTING) + "</script></head>"
    "<body>x</body></html>"
)
UNREACHABLE = MESSAGES["unreachable"]


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def server() -> Iterator[LocalServer]:
    with local_server() as s:
        yield s


@pytest.fixture
def alice(client: TestClient) -> dict[str, str]:
    return register(client, "alice@example.com")


@pytest.fixture
def bob(client: TestClient) -> dict[str, str]:
    return register(client, "bob@example.com")


def install(
    client: TestClient,
    server: LocalServer,
    *,
    rate: int = 100,
    clock: Clock | None = None,
    timeout: float = 5.0,
    max_bytes: int = 1_048_576,
) -> None:
    """Swap in an importer that can reach the local test server (production never does this)."""
    fetcher = SafeFetcher(
        timeout=timeout,
        max_bytes=max_bytes,
        resolver=server.resolver(),
        policy=server.policy(),
    )
    limiter = SlidingWindowLimiter(rate, 60.0, clock=clock or Clock())
    client.app.state.job_importer = JobImporter(  # type: ignore[attr-defined]
        fetcher, limiter, max_description_chars=20000, max_concurrent=4, overall_timeout=timeout
    )


def do_import(client: TestClient, headers: dict[str, str], url: str) -> Response:
    response: Response = client.post(IMPORT, headers=headers, json={"url": url})
    return response


# ------------------------------------------------------------ auth / routing


def test_import_requires_authentication(client: TestClient) -> None:
    assert client.post(IMPORT, json={"url": "https://example.com/job"}).status_code == 401


def test_job_import_route_is_registered_before_the_session_id_route(client: TestClient) -> None:
    # FastAPI keeps included routers lazily, so read the registration order from the OpenAPI
    # paths, which are emitted in route order.
    paths = list(client.app.openapi()["paths"])  # type: ignore[attr-defined]
    assert paths.index("/api/v1/sessions/job-import") < paths.index("/api/v1/sessions/{session_id}")


def test_the_uuid_route_never_swallows_job_import(
    client: TestClient, alice: dict[str, str], server: LocalServer
) -> None:
    server.routes["/job"] = page(JSON_LD_PAGE)
    install(client, server)
    assert do_import(client, alice, server.url("/job")).status_code == 200
    # Other methods on that path are not handled as a session id either.
    assert client.get(IMPORT, headers=alice).status_code in (405, 422)
    assert client.delete(IMPORT, headers=alice).status_code in (405, 422)


# -------------------------------------------------------------- production wiring


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/", "http://127.0.0.1:80/admin", "http://[::1]/", "http://10.0.0.1/",
        "http://192.168.1.1/", "http://172.16.0.1/", "http://169.254.169.254/latest/meta-data/",
        "http://[::ffff:127.0.0.1]/", "http://0.0.0.0/", "http://localhost/",
        "http://127.0.0.1:6379/", "http://127.0.0.1:8000/api/v1/health",
    ],
)  # fmt: skip
def test_the_real_app_wiring_refuses_internal_targets_without_any_network_call(
    client: TestClient, alice: dict[str, str], monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    attempts: list[Any] = []

    def refuse(address: Any, *args: Any, **kwargs: Any) -> None:
        attempts.append(address)
        raise AssertionError("network used for a blocked target")

    monkeypatch.setattr(socket, "create_connection", refuse)
    response = do_import(client, alice, url)
    assert response.status_code == 200
    assert response.json() == {
        "status": "failed",
        "company": None,
        "title": None,
        "description": None,
        "message": UNREACHABLE,
    }
    assert attempts == []
    # The answer never echoes the URL or explains why (no oracle for probing internal hosts).
    for leak in ("127.0.0.1", "10.0.0.1", "169.254", "blocked", "private", "internal", "refused"):
        assert leak not in response.text


def test_blocked_and_unreachable_targets_are_indistinguishable(
    client: TestClient, alice: dict[str, str], server: LocalServer
) -> None:
    install(client, server)
    server.routes["/down"] = page("", status=503)
    blocked = do_import(client, alice, "http://10.1.2.3/").json()
    http_error = do_import(client, alice, server.url("/down")).json()
    dns = do_import(client, alice, "http://unknown-host.test/").json()  # fake resolver: no DNS
    assert blocked == http_error == dns


# ------------------------------------------------------------------- outcomes


def test_structured_job_page_succeeds_with_the_documented_contract(
    client: TestClient, alice: dict[str, str], server: LocalServer
) -> None:
    server.routes["/job"] = page(JSON_LD_PAGE)
    install(client, server)
    response = do_import(client, alice, server.url("/job"))
    assert response.status_code == 200
    assert response.json() == {
        "status": "succeeded",
        "company": "Initech",
        "title": "Platform Engineer",
        "description": "Run the platform.",
        "message": MESSAGES["succeeded"],
    }


def test_a_generic_page_is_at_most_partial(
    client: TestClient, alice: dict[str, str], server: LocalServer
) -> None:
    html = "<title>Dev</title><meta property='og:site_name' content='Hooli'><main>Write code</main>"
    server.routes["/job"] = page(html)
    install(client, server)
    data = do_import(client, alice, server.url("/job")).json()
    assert data["status"] == "partial" and data["company"] == "Hooli" and data["title"] == "Dev"
    assert data["message"] == MESSAGES["partial"]


def test_a_page_without_job_information_fails_gracefully(
    client: TestClient, alice: dict[str, str], server: LocalServer
) -> None:
    server.routes["/empty"] = page("<html><body><nav>menu</nav></body></html>")
    install(client, server)
    data = do_import(client, alice, server.url("/empty")).json()
    assert data["status"] == "failed" and data["message"] == MESSAGES["no_job_info"]
    assert data["company"] is None and data["title"] is None and data["description"] is None


def test_unsupported_oversized_and_error_pages_fail_without_blocking(
    client: TestClient, alice: dict[str, str], server: LocalServer
) -> None:
    server.routes["/pdf"] = page(b"%PDF", content_type="application/pdf")
    server.routes["/big"] = page(b"a" * 5000)
    server.routes["/missing"] = page("", status=404)
    install(client, server, max_bytes=2000)
    cases = {
        "/pdf": MESSAGES[errors.CONTENT],
        "/big": MESSAGES[errors.TOO_LARGE],
        "/missing": UNREACHABLE,
    }
    for path, message in cases.items():
        response = do_import(client, alice, server.url(path))
        assert response.status_code == 200, path
        assert response.json()["status"] == "failed" and response.json()["message"] == message


def test_a_slow_site_times_out_into_a_normal_failed_result(
    client: TestClient, alice: dict[str, str], server: LocalServer
) -> None:
    server.routes["/slow"] = slow(3.0, page(JSON_LD_PAGE))
    install(client, server, timeout=0.5)
    response = do_import(client, alice, server.url("/slow"))
    assert response.status_code == 200 and response.json()["message"] == UNREACHABLE


INVALID_LINKS = [
    "ftp://example.com/x",
    "not a url",
    "http://user:pw@example.com/",
    "javascript:alert(1)",
    "//example.com",
    "http://exa mple.com",
]


@pytest.mark.parametrize("url", INVALID_LINKS)
def test_invalid_links_give_a_failed_result_not_an_error(
    client: TestClient, alice: dict[str, str], server: LocalServer, url: str
) -> None:
    install(client, server)
    response = do_import(client, alice, url)
    assert response.status_code == 200
    assert response.json()["status"] == "failed"
    assert response.json()["message"] == MESSAGES[errors.INVALID_URL]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"url": ""},
        {"url": "x" * 2049},
        {"url": 5},
        {"url": "https://example.com", "user_id": str(uuid.uuid4())},
        {"url": "https://example.com", "timeout": 600},
        {"url": "https://example.com", "storage_key": "../../etc/passwd"},
    ],
)
def test_the_request_accepts_only_a_url(
    client: TestClient, alice: dict[str, str], payload: dict[str, Any]
) -> None:
    assert client.post(IMPORT, headers=alice, json=payload).status_code == 422


# ----------------------------------------------------------- rate limiting


def test_attempts_are_rate_limited_per_user(
    client: TestClient, alice: dict[str, str], bob: dict[str, str], server: LocalServer
) -> None:
    server.routes["/job"] = page(JSON_LD_PAGE)
    clock = Clock()
    install(client, server, rate=3, clock=clock)
    for _ in range(3):
        assert do_import(client, alice, server.url("/job")).status_code == 200
    limited = do_import(client, alice, server.url("/job"))
    assert limited.status_code == 429 and limited.json()["error"]["code"] == "rate_limited"
    assert int(limited.headers["Retry-After"]) >= 1
    # Another user has their own budget.
    assert do_import(client, bob, server.url("/job")).status_code == 200
    # The window slides: after a minute Alice can import again.
    clock.now += 61
    assert do_import(client, alice, server.url("/job")).status_code == 200


def test_blocked_and_invalid_attempts_count_against_the_limit(
    client: TestClient, alice: dict[str, str], server: LocalServer
) -> None:
    install(client, server, rate=2)
    assert do_import(client, alice, "http://10.0.0.1/").status_code == 200
    assert do_import(client, alice, "not a url").status_code == 200
    assert do_import(client, alice, "http://10.0.0.1/").status_code == 429  # probing is throttled


def test_limiter_window_boundaries_and_isolation() -> None:
    clock = Clock()
    limiter = SlidingWindowLimiter(2, 60.0, clock=clock)
    assert limiter.allow("a") and limiter.allow("a") and not limiter.allow("a")
    assert limiter.allow("b")  # separate key
    clock.now += 59.9
    assert not limiter.allow("a")  # still inside the window
    clock.now += 0.2
    assert limiter.allow("a")  # the oldest events expired
    assert limiter.retry_after("never-seen") == 1


def test_rejected_attempts_do_not_extend_the_window() -> None:
    clock = Clock()
    limiter = SlidingWindowLimiter(1, 10.0, clock=clock)
    assert limiter.allow("a")
    for _ in range(50):
        clock.now += 0.1
        assert not limiter.allow("a")
    clock.now += 5.1  # 10.1 s after the only accepted event
    assert limiter.allow("a")


# ------------------------------------------------- never blocks / never leaks


class ExplodingFetcher:
    def fetch(self, url: str) -> FetchedPage:
        raise RuntimeError("secret-internal-detail /srv/app/token=abc123")


def importer_with(fetcher: Any, **kw: Any) -> JobImporter:
    return JobImporter(
        fetcher,
        SlidingWindowLimiter(100, 60.0),
        max_description_chars=20000,
        max_concurrent=kw.pop("max_concurrent", 4),
        overall_timeout=kw.pop("overall_timeout", 5.0),
    )


def test_unexpected_errors_become_a_safe_failed_result(
    client: TestClient, alice: dict[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    client.app.state.job_importer = importer_with(ExplodingFetcher())  # type: ignore[attr-defined]
    with caplog.at_level(logging.DEBUG):
        response = do_import(client, alice, "https://example.com/job")
    assert response.status_code == 200 and response.json()["message"] == UNREACHABLE
    assert (
        "secret-internal-detail" not in response.text
        and "secret-internal-detail" not in caplog.text
    )
    assert "abc123" not in caplog.text


def test_urls_and_page_content_are_never_logged(
    client: TestClient, alice: dict[str, str], server: LocalServer, caplog: pytest.LogCaptureFixture
) -> None:
    server.routes["/secret-path-token-777"] = page(
        JSON_LD_PAGE.replace("Run the platform.", "CONFIDENTIAL-PAGE-TEXT")
    )
    install(client, server)
    with caplog.at_level(logging.DEBUG):
        response = do_import(client, alice, server.url("/secret-path-token-777?key=sk-abc"))
    assert response.json()["status"] == "succeeded"
    assert "secret-path-token-777" not in caplog.text and "sk-abc" not in caplog.text
    assert "CONFIDENTIAL-PAGE-TEXT" not in caplog.text and "jobs.test" not in caplog.text


def test_an_overall_timeout_guard_returns_a_failed_result() -> None:
    class Hangs:
        def fetch(self, url: str) -> FetchedPage:
            threading.Event().wait(3.0)
            return FetchedPage(body=b"", charset=None)

    importer = importer_with(Hangs(), overall_timeout=0.1)  # guard fires at 0.1 + 2 s
    result = asyncio.run(importer.import_job(uuid.uuid4(), "https://example.com"))
    assert result.status == "failed" and result.message == UNREACHABLE


def test_concurrent_imports_are_capped() -> None:
    gate = threading.Event()

    class Blocking:
        def fetch(self, url: str) -> FetchedPage:
            gate.wait(5.0)
            return FetchedPage(body=b"<html></html>", charset=None)

    importer = importer_with(Blocking(), max_concurrent=1)

    async def scenario() -> None:
        first = asyncio.create_task(importer.import_job(uuid.uuid4(), "https://example.com/a"))
        await asyncio.sleep(0.1)
        with pytest.raises(AppError) as exc:
            await importer.import_job(uuid.uuid4(), "https://example.com/b")
        assert exc.value.status_code == 429 and exc.value.code == "import_busy"
        gate.set()
        assert (await first).status == "failed"  # an empty page: nothing to import
        # The slot is released afterwards.
        assert (await importer.import_job(uuid.uuid4(), "https://example.com/c")).status == "failed"

    asyncio.run(scenario())


# ------------------------------------------- manual entry always still works


def test_nothing_is_stored_by_an_import(
    client: TestClient, alice: dict[str, str], server: LocalServer, run_sql: RunSql
) -> None:
    server.routes["/job"] = page(JSON_LD_PAGE)
    install(client, server)
    tables = ("sessions", "resumes", "documents", "ingestion_jobs", "chunks")
    before = [run_sql(f"SELECT count(*) AS n FROM {t}")[0]["n"] for t in tables]  # noqa: S608
    do_import(client, alice, server.url("/job"))
    do_import(client, alice, "http://10.0.0.1/")
    after = [run_sql(f"SELECT count(*) AS n FROM {t}")[0]["n"] for t in tables]  # noqa: S608
    assert before == after


def test_after_a_failed_import_the_session_can_still_be_created_manually(
    client: TestClient, alice: dict[str, str], server: LocalServer, run_sql: RunSql
) -> None:
    install(client, server)
    link = "http://10.0.0.1/jobs/42"  # an import of this link fails (blocked)...
    assert do_import(client, alice, link).json()["status"] == "failed"
    resume = new_file(client, run_sql, alice, "resumes")
    created = create_session(
        client,
        alice,
        session_body(resume, company_name="Initech", job_url=link, job_description="Typed by hand"),
    )
    assert created.status_code == 201  # ...and manual entry is unaffected
    assert created.json()["company_name"] == "Initech"
    assert created.json()["job_url"] == link


def test_the_session_api_does_not_fetch_the_job_url(
    client: TestClient, alice: dict[str, str], run_sql: RunSql, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts: list[Any] = []
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: attempts.append(a))
    resume = new_file(client, run_sql, alice, "resumes")
    created = create_session(client, alice, session_body(resume, job_url="https://example.com/x"))
    assert created.status_code == 201 and attempts == []


# ------------------------------------------------------------- configuration


@pytest.fixture
def no_env_file(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import Settings

    monkeypatch.setitem(Settings.model_config, "env_file", None)


@pytest.mark.usefixtures("no_env_file")
def test_job_import_defaults_are_conservative() -> None:
    from app.core.config import load_settings

    s = load_settings()
    assert s.job_import_timeout_seconds == 8.0 and s.job_import_max_bytes == 1_048_576
    assert s.job_import_max_redirects == 3 and s.job_import_rate_limit_per_minute == 10
    assert s.job_import_max_concurrent == 4


@pytest.mark.usefixtures("no_env_file")
@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("JOB_IMPORT_TIMEOUT_SECONDS", "0"),
        ("JOB_IMPORT_TIMEOUT_SECONDS", "31"),
        ("JOB_IMPORT_MAX_BYTES", "100"),
        ("JOB_IMPORT_MAX_BYTES", "99999999"),
        ("JOB_IMPORT_MAX_REDIRECTS", "-1"),
        ("JOB_IMPORT_MAX_REDIRECTS", "6"),
        ("JOB_IMPORT_RATE_LIMIT_PER_MINUTE", "0"),
        ("JOB_IMPORT_MAX_CONCURRENT", "0"),
    ],
)
def test_invalid_job_import_settings_fail_clearly(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    from app.core.config import ConfigurationError, load_settings

    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigurationError) as exc:
        load_settings()
    assert name in str(exc.value)


def test_the_app_uses_the_strict_policy_and_configured_limits(client: TestClient) -> None:
    from app.jobs_import.ssrf import DEFAULT_POLICY

    importer = client.app.state.job_importer  # type: ignore[attr-defined]
    assert importer._fetcher.policy is DEFAULT_POLICY  # production never uses a looser policy
