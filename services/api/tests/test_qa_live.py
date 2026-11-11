"""Phase 8B over a REAL socket: a uvicorn server in a thread, a real HTTP client, real disconnects.

TestClient cannot disconnect mid-stream, so these tests start the actual application (including
its request-logging middleware) on 127.0.0.1 and talk to it with httpx. What they verify:
- a client that goes away mid-stream leaves a `cancelled` answer with the partial text, the
  provider stream is closed and the user's concurrency slot is released;
- a client that goes away right after `answer.done` never downgrades the completed answer;
- the per-user concurrency cap works across real simultaneous connections;
- tokens are delivered incrementally (the app does not buffer the stream).
NOT verified here: the Next.js proxy in front of the API (Phase 8C).
"""

import json
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.embeddings.fake import FakeEmbeddingProvider
from app.main import create_app
from app.qa.limits import QuestionLimits
from app.qa.service import QuestionAnswerer
from tests.conftest import RunSql
from tests.helpers import register
from tests.qa_fakes import SlowTokensProvider
from tests.test_qa_service import make_router, quick_session
from tests.test_sessions import SESSIONS


@dataclass
class Live:
    app: Any
    port: int

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def install(self, provider: SlowTokensProvider, limits: QuestionLimits) -> None:
        self.app.state.question_answerer = QuestionAnswerer(
            self.app.state.sessionmaker,
            get_settings(),
            make_router([provider]),
            FakeEmbeddingProvider(),
        )
        self.app.state.question_limits = limits


@pytest.fixture
def live(client: TestClient) -> Iterator[Live]:  # `client` prepares the clean database + env
    app = create_app()
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=0,
            log_level="warning",
            lifespan="on",
            timeout_graceful_shutdown=2,
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started, "the live server did not start"
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield Live(app=app, port=port)
    finally:
        server.should_exit = True
        thread.join(timeout=15)


def events(lines: Iterator[str]) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield SSE events as they arrive."""
    name = ""
    for line in lines:
        if line.startswith("event: "):
            name = line[len("event: ") :]
        elif line.startswith("data: "):
            yield name, json.loads(line[len("data: ") :])


def wait_for(condition: Callable[[], bool], timeout: float = 10.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(interval)
    return condition()


def answer_row(run_sql: RunSql) -> dict[str, Any]:
    return run_sql("SELECT * FROM generated_answers")[0]


@dataclass
class Setup:
    live: Live
    headers: dict[str, str]
    session_id: str
    user_id: uuid.UUID
    run_sql: RunSql

    def stream(self, text: str = "Tell me about yourself") -> httpx.Client:
        return httpx.Client(timeout=15)

    def open(self, client: httpx.Client, text: str = "Tell me about yourself") -> Any:
        return client.stream(
            "POST",
            self.live.url(f"{SESSIONS}/{self.session_id}/questions"),
            headers={**self.headers, "Accept": "text/event-stream"},
            json={"text": text},
        )


@pytest.fixture
def setup(client: TestClient, live: Live, run_sql: RunSql) -> Setup:
    headers = register(client, "alice@example.com")
    sid, _, uid = quick_session(client, run_sql, headers)
    return Setup(live, headers, sid, uid, run_sql)


# ------------------------------------------------------------- disconnects


def test_a_client_that_disconnects_mid_stream_leaves_a_cancelled_answer(setup: Setup) -> None:
    provider = SlowTokensProvider(token_delay=0.15, tokens=100)
    limits = QuestionLimits(100, 2)
    setup.live.install(provider, limits)
    seen: list[str] = []
    with httpx.Client(timeout=15) as client, setup.open(client) as response:
        assert response.status_code == 200
        for name, _ in events(response.iter_lines()):
            seen.append(name)
            if seen.count("answer.delta") >= 3:
                break
        assert limits.active(setup.user_id) == 1  # the slot is held while streaming
    # leaving the `with` closes the connection abruptly (the client "went away")

    assert wait_for(lambda: answer_row(setup.run_sql)["status"] != "streaming")
    row = answer_row(setup.run_sql)
    assert row["status"] == "cancelled"
    assert row["text"].startswith("word0 word1 ") and 0 < len(row["text"]) < len("word0 " * 100)
    assert row["total_ms"] is not None and row["first_token_ms"] is not None
    assert setup.run_sql("SELECT status FROM detected_questions")[0]["status"] == "detected"
    assert wait_for(lambda: provider.closed), "the provider stream was not closed"
    emitted = provider.emitted
    time.sleep(0.6)
    assert provider.emitted == emitted and emitted < 100  # generation really stopped
    assert wait_for(lambda: limits.active(setup.user_id) == 0), "the slot leaked"


def test_a_disconnect_before_the_first_token_is_cancelled_with_no_text(setup: Setup) -> None:
    provider = SlowTokensProvider(token_delay=2.0, tokens=5)
    limits = QuestionLimits(100, 2)
    setup.live.install(provider, limits)
    with httpx.Client(timeout=15) as client, setup.open(client) as response:
        for name, _ in events(response.iter_lines()):
            if name == "retrieval.done":
                break
        # The provider is now generating (waiting two seconds for its first token): disconnect.
        assert wait_for(lambda: provider.calls >= 1)
    assert wait_for(lambda: answer_row(setup.run_sql)["status"] != "streaming")
    row = answer_row(setup.run_sql)
    assert (row["status"], row["text"]) == ("cancelled", "")
    assert wait_for(lambda: provider.closed), "the provider stream was not closed"
    assert provider.emitted == 0
    assert wait_for(lambda: limits.active(setup.user_id) == 0)


def test_a_disconnect_straight_after_question_created_still_ends_cleanly(setup: Setup) -> None:
    provider = SlowTokensProvider(token_delay=0.5, tokens=5)
    limits = QuestionLimits(100, 2)
    setup.live.install(provider, limits)
    with httpx.Client(timeout=15) as client, setup.open(client) as response:
        assert next(events(response.iter_lines()))[0] == "question.created"
    assert wait_for(lambda: answer_row(setup.run_sql)["status"] != "streaming")
    assert answer_row(setup.run_sql)["status"] == "cancelled"  # never left as `streaming`
    assert wait_for(lambda: limits.active(setup.user_id) == 0)


def test_a_disconnect_right_after_answer_done_never_downgrades_the_answer(setup: Setup) -> None:
    provider = SlowTokensProvider(token_delay=0.02, tokens=4)
    limits = QuestionLimits(100, 2)
    setup.live.install(provider, limits)
    with httpx.Client(timeout=15) as client, setup.open(client) as response:
        for name, _ in events(response.iter_lines()):
            if name == "answer.done":
                break  # close the connection at the very end of the stream
    assert wait_for(lambda: limits.active(setup.user_id) == 0)
    time.sleep(0.5)  # give any (wrong) late cancellation time to overwrite the row
    row = answer_row(setup.run_sql)
    assert row["status"] == "complete" and row["text"] == "word0 word1 word2 word3 "
    assert setup.run_sql("SELECT status FROM detected_questions")[0]["status"] == "answered"


def test_the_service_keeps_working_after_disconnects(setup: Setup) -> None:
    provider = SlowTokensProvider(token_delay=0.05, tokens=40)
    limits = QuestionLimits(100, 1)  # a single slot: a leak would block the next request
    setup.live.install(provider, limits)
    for _ in range(3):
        with httpx.Client(timeout=15) as client, setup.open(client) as response:
            assert response.status_code == 200
            next(events(response.iter_lines()))
        assert wait_for(lambda: limits.active(setup.user_id) == 0)

    def all_cancelled() -> bool:
        rows = setup.run_sql("SELECT status FROM generated_answers")
        return len(rows) == 3 and all(r["status"] == "cancelled" for r in rows)

    # The limiter slot is released before the shielded finish write commits, so wait for the row.
    assert wait_for(all_cancelled)
    statuses = [r["status"] for r in setup.run_sql("SELECT status FROM generated_answers")]
    assert statuses == ["cancelled"] * 3


# ---------------------------------------------------------- concurrency cap


def test_the_concurrency_cap_holds_across_real_simultaneous_streams(setup: Setup) -> None:
    provider = SlowTokensProvider(token_delay=0.2, tokens=100)
    limits = QuestionLimits(100, 2)
    setup.live.install(provider, limits)
    with (
        httpx.Client(timeout=15) as c1,
        httpx.Client(timeout=15) as c2,
        httpx.Client(timeout=15) as c3,
    ):
        with setup.open(c1) as r1, setup.open(c2) as r2:
            assert r1.status_code == r2.status_code == 200
            # Keep the iterators alive: a garbage-collected iterator closes its connection.
            stream1, stream2 = events(r1.iter_lines()), events(r2.iter_lines())
            next(stream1)
            next(stream2)
            assert limits.active(setup.user_id) == 2

            third = c3.post(
                setup.live.url(f"{SESSIONS}/{setup.session_id}/questions"),
                headers={**setup.headers, "Accept": "text/event-stream"},
                json={"text": "one too many"},
            )
            assert third.status_code == 429
            assert third.json()["error"]["code"] == "too_many_generations"
            assert third.headers["content-type"].startswith("application/json")
            assert int(third.headers["Retry-After"]) >= 1
            assert len(setup.run_sql("SELECT id FROM detected_questions")) == 2  # no third row
        # both streams were abandoned: the slots free up and a new question is accepted
        assert wait_for(lambda: limits.active(setup.user_id) == 0)
        with setup.open(c3) as again:
            assert again.status_code == 200
            assert next(events(again.iter_lines()))[0] == "question.created"
    assert wait_for(lambda: limits.active(setup.user_id) == 0)


# ------------------------------------------------------------- incremental


def test_tokens_arrive_incrementally_not_buffered_until_the_end(setup: Setup) -> None:
    provider = SlowTokensProvider(token_delay=0.3, tokens=6)
    setup.live.install(provider, QuestionLimits(100, 2))
    started = time.monotonic()
    arrivals: list[float] = []
    with httpx.Client(timeout=15) as client, setup.open(client) as response:
        for name, _ in events(response.iter_lines()):
            if name == "answer.delta":
                arrivals.append(time.monotonic() - started)
    assert len(arrivals) == 6
    # Relative, not absolute, so machine load (setup + retrieval time) cannot make it flaky: the
    # first token arrives well before the last (5 gaps x 0.3 s) instead of everything at the end.
    assert arrivals[-1] - arrivals[0] > 1.0  # the tokens trickle in over time, not in one burst
    assert all(b >= a for a, b in zip(arrivals, arrivals[1:], strict=False))
    assert answer_row(setup.run_sql)["status"] == "complete"
