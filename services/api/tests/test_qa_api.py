"""Phase 8B API: SSE endpoint, history endpoints, limits, safe failures, ownership, stale rows.

TestClient delivers a streamed response only after it completes, which is enough to check framing,
ordering and content. Real streaming behaviour (disconnects, concurrency) is in test_qa_live.py.
"""

import json
import logging
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from app.ai.errors import ContentFiltered, Unavailable
from app.core.config import get_settings
from app.core.errors import AppError
from app.embeddings.base import EmbeddingUnavailable
from app.embeddings.fake import FakeEmbeddingProvider
from app.qa.events import RetrievalDone
from app.qa.limits import QuestionLimits
from app.qa.service import QuestionAnswerer
from tests.conftest import RunSql
from tests.helpers import register
from tests.qa_fakes import RecordingProvider, answer_for_question
from tests.test_qa_service import make_router, quick_session
from tests.test_sessions import SESSIONS, new_file, safe

SSE = {"Accept": "text/event-stream"}
EVENT_NAMES = {
    "question.created",
    "retrieval.done",
    "answer.started",
    "answer.delta",
    "answer.done",
    "answer.error",
}


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def install(
    client: TestClient,
    providers: list[RecordingProvider],
    *,
    limits: QuestionLimits | None = None,
    embedder: Any = None,
    answerer_cls: type[QuestionAnswerer] = QuestionAnswerer,
) -> QuestionLimits:
    """Swap in an answerer with fake providers (production wiring never does this)."""
    app: Any = client.app
    app.state.question_answerer = answerer_cls(
        app.state.sessionmaker,
        get_settings(),
        make_router(providers),
        embedder or FakeEmbeddingProvider(),
    )
    app.state.question_limits = limits or QuestionLimits(1000, 1000)
    return app.state.question_limits  # type: ignore[no-any-return]


def ask(
    client: TestClient,
    headers: dict[str, str],
    session_id: str,
    text: str = "Tell me about yourself",
    *,
    accept: str | None = "text/event-stream",
    **body: Any,
) -> Response:
    request_headers = {**headers}
    if accept is not None:
        request_headers["Accept"] = accept
    response: Response = client.post(
        f"{SESSIONS}/{session_id}/questions",
        headers=request_headers,
        json={"text": text, **body},
    )
    return response


def frames(response: Response) -> list[tuple[str, dict[str, Any]]]:
    """Parse an SSE body strictly: every frame is `event: <name>\\ndata: <json>` + blank line."""
    body = response.text
    assert body.endswith("\n\n") and not body.endswith("\n\n\n")
    out: list[tuple[str, dict[str, Any]]] = []
    for raw in body[:-2].split("\n\n"):
        lines = raw.split("\n")
        assert len(lines) == 2, f"a frame must be exactly two lines: {raw!r}"
        assert lines[0].startswith("event: ") and lines[1].startswith("data: ")
        out.append((lines[0][len("event: ") :], json.loads(lines[1][len("data: ") :])))
    return out


def names(response: Response) -> list[str]:
    return [name for name, _ in frames(response)]


def count(run_sql: RunSql, table: str) -> int:
    return int(run_sql(f"SELECT count(*) AS n FROM {table}")[0]["n"])  # noqa: S608


@pytest.fixture
def alice(client: TestClient) -> dict[str, str]:
    return register(client, "alice@example.com")


@pytest.fixture
def bob(client: TestClient) -> dict[str, str]:
    return register(client, "bob@example.com")


@pytest.fixture
def setup(
    client: TestClient, run_sql: RunSql, alice: dict[str, str]
) -> tuple[str, str, RecordingProvider]:
    """Alice's ready session with a recording provider installed: (session, resume, provider)."""
    sid, resume, _ = quick_session(client, run_sql, alice)
    provider = RecordingProvider()
    install(client, [provider])
    return sid, resume, provider


# ------------------------------------------------------------ auth / 406


def test_every_question_endpoint_requires_authentication(client: TestClient) -> None:
    sid, aid = uuid.uuid4(), uuid.uuid4()
    assert (
        client.post(f"{SESSIONS}/{sid}/questions", json={"text": "x"}, headers=SSE).status_code
        == 401
    )
    assert (
        client.post(f"{SESSIONS}/{sid}/questions", json={"text": "x"}).status_code == 401
    )  # 401 before 406
    assert client.get(f"{SESSIONS}/{sid}/questions").status_code == 401
    assert client.get(f"{SESSIONS}/{sid}/answers/{aid}").status_code == 401


@pytest.mark.parametrize(
    "accept", [None, "", "application/json", "*/*", "text/html", "text/*", "text/event-streams"]
)
def test_requests_that_do_not_ask_for_sse_get_406_and_change_nothing(
    client: TestClient,
    run_sql: RunSql,
    alice: dict[str, str],
    setup: tuple[str, str, RecordingProvider],
    accept: str | None,
) -> None:
    sid, _, provider = setup
    response = ask(client, alice, sid, accept=accept)
    assert response.status_code == 406
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"]["code"] == "not_acceptable"
    assert count(run_sql, "detected_questions") == 0 and provider.recorded == []


def test_a_406_does_not_consume_the_rate_limit_or_a_slot(
    client: TestClient, alice: dict[str, str], setup: tuple[str, str, RecordingProvider]
) -> None:
    sid, _, _ = setup
    limits = install(client, [RecordingProvider()], limits=QuestionLimits(2, 2))
    for _ in range(5):
        assert ask(client, alice, sid, accept="application/json").status_code == 406
    assert ask(client, alice, sid).status_code == 200 and ask(client, alice, sid).status_code == 200
    assert limits.active(user_id_of(client, "alice@example.com")) == 0


def user_id_of(client: TestClient, email: str) -> uuid.UUID:
    return uuid.UUID(_user(client, email))


def _user(client: TestClient, email: str) -> str:
    login = client.post(
        "/api/v1/auth/login", json={"email": email, "password": "correct horse battery staple"}
    )
    return str(login.json()["user"]["id"])


@pytest.mark.parametrize(
    "accept",
    [
        "text/event-stream",
        "TEXT/EVENT-STREAM",
        "text/event-stream; charset=utf-8",
        "application/json, text/event-stream;q=0.9",
    ],
)
def test_sse_accept_header_variants_are_accepted(
    client: TestClient,
    alice: dict[str, str],
    setup: tuple[str, str, RecordingProvider],
    accept: str,
) -> None:
    assert ask(client, alice, setup[0], accept=accept).status_code == 200


def test_406_is_checked_before_the_body_is_validated(
    client: TestClient, alice: dict[str, str], setup: tuple[str, str, RecordingProvider]
) -> None:
    response = client.post(
        f"{SESSIONS}/{setup[0]}/questions",
        headers={**alice, "Accept": "application/json"},
        json={"nope": 1},
    )
    assert response.status_code == 406


# ---------------------------------------------------------------- framing


def test_the_stream_has_the_documented_headers(
    client: TestClient, alice: dict[str, str], setup: tuple[str, str, RecordingProvider]
) -> None:
    response = ask(client, alice, setup[0])
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache, no-transform"
    assert response.headers["x-accel-buffering"] == "no"


def test_events_come_in_the_documented_order_with_consistent_ids(
    client: TestClient,
    run_sql: RunSql,
    alice: dict[str, str],
    setup: tuple[str, str, RecordingProvider],
) -> None:
    response = ask(client, alice, setup[0])
    parsed = frames(response)
    sequence = [name for name, _ in parsed]
    assert sequence[:3] == ["question.created", "retrieval.done", "answer.started"]
    assert sequence[-1] == "answer.done" and set(sequence) <= EVENT_NAMES
    middle = sequence[3:-1]
    assert middle and set(middle) == {"answer.delta"}
    assert sequence.count("question.created") == sequence.count("answer.done") == 1

    created, retrieval, started = parsed[0][1], parsed[1][1], parsed[2][1]
    assert set(created) == {"question_id", "answer_id"}
    assert set(retrieval) == {"question_id", "chunk_count", "ms"}
    assert set(started) == {"answer_id", "question_id", "provider", "model"}
    deltas = [data for name, data in parsed if name == "answer.delta"]
    assert [d["index"] for d in deltas] == list(range(len(deltas)))
    assert all(set(d) == {"answer_id", "index", "text"} for d in deltas)
    done = parsed[-1][1]
    assert {created["answer_id"], started["answer_id"], done["answer_id"]} == {created["answer_id"]}
    assert {created["question_id"], retrieval["question_id"], done["question_id"]} == {
        created["question_id"]
    }
    assert all(d["answer_id"] == created["answer_id"] for d in deltas)
    assert (
        (started["provider"], started["model"])
        == (done["provider"], done["model"])
        == ("fake", "fake-echo")
    )


def test_answer_done_exposes_the_metrics_and_matches_what_is_stored(
    client: TestClient,
    run_sql: RunSql,
    alice: dict[str, str],
    setup: tuple[str, str, RecordingProvider],
) -> None:
    sid, _, _ = setup
    parsed = frames(ask(client, alice, sid))
    done = parsed[-1][1]
    assert set(done) == {
        "answer_id", "question_id", "provider", "model", "metrics", "fallback_used",
        "input_tokens", "output_tokens",
    }  # fmt: skip
    assert set(done["metrics"]) == {"retrieval_ms", "first_token_ms", "total_ms"}
    m = done["metrics"]
    assert 0 <= m["retrieval_ms"] <= m["first_token_ms"] <= m["total_ms"]
    assert done["fallback_used"] is False and done["input_tokens"] > 0 and done["output_tokens"] > 0
    row = run_sql("SELECT * FROM generated_answers")[0]
    assert str(row["id"]) == done["answer_id"] and row["status"] == "complete"
    assert (row["retrieval_ms"], row["first_token_ms"], row["total_ms"]) == (
        m["retrieval_ms"], m["first_token_ms"], m["total_ms"],
    )  # fmt: skip
    assert (row["provider"], row["model"], row["fallback_used"]) == ("fake", "fake-echo", False)
    text = "".join(d["text"] for n, d in parsed if n == "answer.delta")
    assert row["text"] == text
    detail = client.get(f"{SESSIONS}/{sid}/answers/{done['answer_id']}", headers=alice).json()
    assert detail["text"] == text and detail["total_ms"] == m["total_ms"]


def test_unicode_quotes_and_newlines_survive_framing_exactly(
    client: TestClient, alice: dict[str, str], setup: tuple[str, str, RecordingProvider]
) -> None:
    sid, _, _ = setup
    tricky = 'తెలుగు हिन्दी "quoted" back\\slash\n- bullet\r\nevent: forged\ndata: {"x":1}\n\n'
    install(client, [RecordingProvider(reply_fn=lambda m: tricky)])
    response = ask(client, alice, sid)
    parsed = frames(response)  # strict parse: a forged frame inside the text would break it
    assert [n for n, _ in parsed].count("answer.done") == 1 and "forged" not in [
        n for n, _ in parsed
    ]
    streamed = "".join(d["text"] for n, d in parsed if n == "answer.delta")
    assert streamed == tricky
    assert response.text.count("\n\n") == len(parsed)  # no raw blank line leaked from the text


def test_a_fallback_is_visible_in_the_stream_and_the_stored_answer(
    client: TestClient,
    run_sql: RunSql,
    alice: dict[str, str],
    setup: tuple[str, str, RecordingProvider],
) -> None:
    sid, _, _ = setup
    install(client, [RecordingProvider("a", failures=[Unavailable()]), RecordingProvider("b")])
    parsed = frames(ask(client, alice, sid))
    started = next(d for n, d in parsed if n == "answer.started")
    done = parsed[-1][1]
    assert (started["provider"], done["provider"], done["fallback_used"]) == ("b", "b", True)
    assert run_sql("SELECT fallback_used, provider FROM generated_answers")[0] == {
        "fallback_used": True,
        "provider": "b",
    }


def test_the_question_type_override_is_applied_through_the_api(
    client: TestClient,
    run_sql: RunSql,
    alice: dict[str, str],
    setup: tuple[str, str, RecordingProvider],
) -> None:
    sid, _, provider = setup
    assert (
        ask(client, alice, sid, "Write a function", question_type_override="coding").status_code
        == 200
    )
    assert "Coding question" in provider.recorded[0].system
    assert run_sql("SELECT question_type FROM detected_questions")[0]["question_type"] == "coding"


# ----------------------------------------- refusals happen before the stream


def test_unknown_and_foreign_sessions_are_indistinguishable_json_404s(
    client: TestClient, run_sql: RunSql, alice: dict[str, str], bob: dict[str, str],
    setup: tuple[str, str, RecordingProvider],
) -> None:  # fmt: skip
    sid, _, provider = setup
    foreign = ask(client, bob, sid)
    unknown = ask(client, bob, str(uuid.uuid4()))
    assert foreign.status_code == unknown.status_code == 404
    assert foreign.headers["content-type"].startswith("application/json")
    assert safe(foreign) == safe(unknown)
    assert count(run_sql, "detected_questions") == 0 and provider.recorded == []


@pytest.mark.parametrize(
    ("kwargs", "status", "code"),
    [
        ({"text": ""}, 422, "validation_error"),
        ({"text": "   \n\t"}, 422, "validation_error"),
        ({"text": "q" * 2001}, 422, "validation_error"),
        ({"text": "ok", "question_type_override": "trivia"}, 422, "validation_error"),
        ({"text": "ok", "question_type_override": "CODING"}, 422, "validation_error"),
    ],
)
def test_invalid_questions_are_rejected_before_the_stream_starts(
    client: TestClient, run_sql: RunSql, alice: dict[str, str],
    setup: tuple[str, str, RecordingProvider], kwargs: dict[str, Any], status: int, code: str,
) -> None:  # fmt: skip
    sid, _, provider = setup
    limits = install(client, [provider], limits=QuestionLimits(100, 2))
    response = client.post(f"{SESSIONS}/{sid}/questions", headers={**alice, **SSE}, json=kwargs)
    assert (response.status_code, response.json()["error"]["code"]) == (status, code)
    assert response.headers["content-type"].startswith("application/json")
    assert count(run_sql, "detected_questions") == 0 and provider.recorded == []
    assert limits.active(user_id_of(client, "alice@example.com")) == 0  # no leaked slot


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"text": 5},
        {"text": "x", "user_id": "u"},
        {"text": "x", "session_id": "s"},
        {"text": "x", "status": "ready"},
    ],
)
def test_the_request_body_accepts_only_text_and_the_override(
    client: TestClient,
    alice: dict[str, str],
    setup: tuple[str, str, RecordingProvider],
    payload: dict[str, Any],
) -> None:
    response = client.post(
        f"{SESSIONS}/{setup[0]}/questions", headers={**alice, **SSE}, json=payload
    )
    assert response.status_code == 422


def test_a_session_that_is_not_ready_returns_409(
    client: TestClient,
    run_sql: RunSql,
    alice: dict[str, str],
    setup: tuple[str, str, RecordingProvider],
) -> None:
    sid, _, provider = setup
    run_sql("UPDATE sessions SET status = 'ended' WHERE id = :i", i=sid)
    response = ask(client, alice, sid)
    assert (response.status_code, response.json()["error"]["code"]) == (409, "session_not_ready")
    assert count(run_sql, "detected_questions") == 0 and provider.recorded == []


def test_selected_materials_that_are_not_ready_or_deleted_return_409(
    client: TestClient, run_sql: RunSql, alice: dict[str, str]
) -> None:
    resume = new_file(client, run_sql, alice, "resumes")
    doc = new_file(client, run_sql, alice, "documents")
    created = client.post(
        SESSIONS,
        headers=alice,
        json={
            "company_name": "Acme", "job_description": "JD", "resume_id": resume,
            "document_ids": [doc],
            "preferences": {
                "language": "en", "question_type": "auto", "answer_format": "full_script",
                "answer_length": "short", "answer_tone": "simple",
            },
        },
    )  # fmt: skip
    assert created.status_code == 201, created.text
    sid = created.json()["id"]
    provider = RecordingProvider()
    install(client, [provider])

    def refused() -> tuple[int, str]:
        response = ask(client, alice, sid)
        return response.status_code, response.json()["error"]["code"]

    assert ask(client, alice, sid).status_code == 200  # everything ready: fine
    run_sql(
        "UPDATE ingestion_jobs SET status = 'pending' WHERE source_id = :i AND stage = 'embed'",
        i=doc,
    )
    assert refused() == (409, "materials_not_ready")  # a selected document is no longer ready
    run_sql(
        "UPDATE ingestion_jobs SET status = 'completed' WHERE source_id = :i AND stage = 'embed'",
        i=doc,
    )
    assert ask(client, alice, sid).status_code == 200
    assert client.delete(f"/api/v1/documents/{doc}", headers=alice).status_code == 204
    assert refused() == (409, "materials_not_ready")  # a selected document was deleted: refuse
    run_sql(
        "UPDATE ingestion_jobs SET status = 'failed' WHERE source_id = :i AND stage = 'extract'",
        i=resume,
    )
    assert refused() == (409, "materials_not_ready")  # the resume is no longer ready
    assert len(provider.recorded) == 2  # only the two successful asks reached the provider


# ------------------------------------------------------------ safe failures


def error_frame(response: Response) -> dict[str, Any]:
    parsed = frames(response)
    assert response.status_code == 200  # the stream had started; the failure is an event
    assert [n for n, _ in parsed][:2] == ["question.created", "retrieval.done"]
    assert "answer.done" not in [n for n, _ in parsed]
    name, data = parsed[-1]
    assert name == "answer.error"
    assert set(data) == {"answer_id", "question_id", "code", "message", "retryable"}
    return data


def test_when_every_provider_fails_the_stream_ends_with_a_safe_answer_error(
    client: TestClient,
    run_sql: RunSql,
    alice: dict[str, str],
    setup: tuple[str, str, RecordingProvider],
) -> None:
    sid, _, _ = setup
    install(client, [RecordingProvider("a", failures=[Unavailable()])])
    data = error_frame(ask(client, alice, sid))
    assert (data["code"], data["message"], data["retryable"]) == (
        "llm_unavailable", "The AI service is unavailable.", True,
    )  # fmt: skip
    row = run_sql("SELECT status, error_code, text FROM generated_answers")[0]
    assert (row["status"], row["error_code"], row["text"]) == ("error", "llm_unavailable", "")


def test_content_filtering_is_a_safe_non_retryable_error(
    client: TestClient, alice: dict[str, str], setup: tuple[str, str, RecordingProvider]
) -> None:
    sid, _, _ = setup
    install(client, [RecordingProvider("a", failures=[ContentFiltered()]), RecordingProvider("b")])
    data = error_frame(ask(client, alice, sid))
    assert (data["code"], data["retryable"]) == ("llm_content_filtered", False)


def test_provider_internals_never_reach_the_client_the_database_or_the_logs(
    client: TestClient, run_sql: RunSql, alice: dict[str, str],
    setup: tuple[str, str, RecordingProvider], caplog: pytest.LogCaptureFixture,
) -> None:  # fmt: skip
    sid, _, _ = setup

    class Exploding(RecordingProvider):
        async def stream_chat(self, messages: Any, model: str, config: Any) -> Any:
            raise RuntimeError("sk-LEAKED-key at /srv/app/secrets Traceback")
            yield  # pragma: no cover

    install(client, [Exploding("a")])
    with caplog.at_level(logging.DEBUG):
        response = ask(client, alice, sid)
    assert error_frame(response)["code"] == "llm_unavailable"
    dumped = response.text + repr(run_sql("SELECT * FROM generated_answers")) + caplog.text
    for leaked in ("LEAKED", "sk-", "/srv/app", "Traceback"):
        assert leaked not in dumped


def test_a_retrieval_failure_is_a_safe_answer_error(
    client: TestClient, alice: dict[str, str], setup: tuple[str, str, RecordingProvider]
) -> None:
    sid, _, _ = setup
    provider = RecordingProvider()
    install(client, [provider], embedder=FakeEmbeddingProvider(error=EmbeddingUnavailable()))
    parsed = frames(ask(client, alice, sid))
    assert parsed[-1][0] == "answer.error" and parsed[-1][1]["code"] == "retrieval_failed"
    assert provider.recorded == []


def test_an_unexpected_failure_inside_the_stream_becomes_a_generic_answer_error(
    client: TestClient, alice: dict[str, str], setup: tuple[str, str, RecordingProvider]
) -> None:
    sid, _, _ = setup

    class Boom(QuestionAnswerer):
        async def ask(
            self,
            user_id: uuid.UUID,
            session_id: uuid.UUID,
            text: str,
            question_type_override: str | None = None,
        ) -> Any:
            async for event in super().ask(user_id, session_id, text, question_type_override):
                yield event
                if isinstance(event, RetrievalDone):
                    raise RuntimeError("secret-internal-detail /etc/passwd")

    limits = install(client, [RecordingProvider()], answerer_cls=Boom)
    response = ask(client, alice, sid)
    parsed = frames(response)
    assert parsed[-1][0] == "answer.error" and parsed[-1][1]["code"] == "internal_error"
    assert "secret-internal-detail" not in response.text and "/etc/passwd" not in response.text
    assert limits.active(user_id_of(client, "alice@example.com")) == 0


def test_a_generation_that_exceeds_the_time_budget_fails_with_its_partial_text(
    make_client: Callable[..., TestClient], run_sql: RunSql
) -> None:
    from tests.qa_fakes import SlowTokensProvider

    client = make_client(QA_REQUEST_TIMEOUT_SECONDS="1")
    alice = register(client, "alice@example.com")
    sid, _, _ = quick_session(client, run_sql, alice)
    slow = SlowTokensProvider(token_delay=0.4, tokens=30)
    app: Any = client.app
    app.state.question_answerer = QuestionAnswerer(
        app.state.sessionmaker,
        get_settings(),
        make_router([slow]),
        FakeEmbeddingProvider(),
    )
    app.state.question_limits = QuestionLimits(100, 100)
    parsed = frames(ask(client, alice, sid))
    assert parsed[-1][0] == "answer.error" and parsed[-1][1]["code"] == "llm_timeout"
    deltas = [d["text"] for n, d in parsed if n == "answer.delta"]
    assert 1 <= len(deltas) < 30 and slow.closed  # stopped early and closed the provider stream
    row = run_sql("SELECT status, error_code, text FROM generated_answers")[0]
    assert (row["status"], row["error_code"], row["text"]) == (
        "error",
        "llm_timeout",
        "".join(deltas),
    )


# ------------------------------------------------------------------- limits


def test_the_question_rate_limit_is_per_user_and_counts_every_attempt(
    client: TestClient, run_sql: RunSql, alice: dict[str, str], bob: dict[str, str],
    setup: tuple[str, str, RecordingProvider],
) -> None:  # fmt: skip
    sid, _, provider = setup
    clock = Clock()
    install(client, [provider], limits=QuestionLimits(3, 2, clock=clock))
    assert ask(client, alice, sid).status_code == 200
    assert ask(client, alice, str(uuid.uuid4())).status_code == 404  # refused asks count too
    assert ask(client, alice, sid, "x" * 2001).status_code == 422
    limited = ask(client, alice, sid)
    assert limited.status_code == 429 and limited.json()["error"]["code"] == "rate_limited"
    assert int(limited.headers["Retry-After"]) >= 1
    assert len(provider.recorded) == 1 and count(run_sql, "detected_questions") == 1  # no work done
    bob_session, _, _ = quick_session(client, run_sql, bob)  # Bob has his own budget
    assert ask(client, bob, bob_session).status_code == 200
    clock.now += 61  # the window slides
    assert ask(client, alice, sid).status_code == 200


def test_limits_release_their_slots_on_every_path() -> None:
    limits = QuestionLimits(100, 2)
    user = uuid.uuid4()
    first, second = limits.admit(user), limits.admit(user)
    with pytest.raises(AppError) as exc:
        limits.admit(user)
    assert exc.value.code == "too_many_generations"
    first.release()
    first.release()  # idempotent: must not free a second slot
    assert limits.active(user) == 1
    third = limits.admit(user)
    with pytest.raises(AppError):
        limits.admit(user)
    second.release()
    third.release()
    assert limits.active(user) == 0


def test_the_concurrency_cap_is_per_user_and_independent_of_the_rate_limit() -> None:
    limits = QuestionLimits(100, 1)
    a, b = uuid.uuid4(), uuid.uuid4()
    slot_a = limits.admit(a)
    limits.admit(b).release()  # another user is unaffected
    with pytest.raises(AppError) as exc:
        limits.admit(a)
    assert exc.value.status_code == 429 and exc.value.headers["Retry-After"]
    slot_a.release()
    limits.admit(a).release()


def test_slots_are_released_after_success_failure_and_refusal(
    client: TestClient, alice: dict[str, str], setup: tuple[str, str, RecordingProvider]
) -> None:
    sid, _, _ = setup
    limits = install(
        client,
        [RecordingProvider("a", failures=[None, Unavailable()])],
        limits=QuestionLimits(100, 1),
    )
    uid = user_id_of(client, "alice@example.com")
    assert ask(client, alice, sid).status_code == 200 and limits.active(uid) == 0  # success
    assert "answer.error" in names(ask(client, alice, sid)) and limits.active(uid) == 0  # failure
    assert (
        ask(client, alice, str(uuid.uuid4())).status_code == 404 and limits.active(uid) == 0
    )  # refusal
    assert ask(client, alice, sid, "x" * 2001).status_code == 422 and limits.active(uid) == 0
    assert ask(client, alice, sid).status_code == 200  # a cap of one never got stuck


# ------------------------------------------------------------------ history


def make_history(
    client: TestClient, alice: dict[str, str], sid: str, texts: list[str]
) -> list[dict[str, Any]]:
    install(client, [RecordingProvider(reply_fn=answer_for_question)])
    done: list[dict[str, Any]] = []
    for text in texts:
        done.append(frames(ask(client, alice, sid, text))[-1][1])
    return done


def test_history_lists_questions_oldest_first_with_their_answers(
    client: TestClient, alice: dict[str, str], setup: tuple[str, str, RecordingProvider]
) -> None:
    sid, _, _ = setup
    done = make_history(client, alice, sid, ["first", "second", "third"])
    body = client.get(f"{SESSIONS}/{sid}/questions", headers=alice).json()
    assert set(body) == {"items", "next_cursor"} and body["next_cursor"] is None
    assert [q["text"] for q in body["items"]] == ["first", "second", "third"]
    item = body["items"][0]
    assert set(item) == {
        "id",
        "text",
        "source",
        "kind",
        "type",
        "confidence",
        "status",
        "created_at",
        "answers",
    }
    assert (item["source"], item["kind"], item["type"], item["confidence"], item["status"]) == (
        "typed", "explicit", "behavioral", None, "answered",
    )  # fmt: skip
    (answer,) = item["answers"]
    assert answer["id"] == done[0]["answer_id"] and answer["text"] == "ANSWER-FOR-first"
    assert set(answer) == {
        "id", "question_id", "status", "text", "provider", "model", "retrieval_ms",
        "first_token_ms", "total_ms", "fallback_used", "error_code", "input_tokens",
        "output_tokens", "created_at",
    }  # fmt: skip


def test_history_pagination_is_complete_stable_and_validated(
    client: TestClient, alice: dict[str, str], setup: tuple[str, str, RecordingProvider]
) -> None:
    sid, _, _ = setup
    make_history(client, alice, sid, [f"q{i}" for i in range(5)])
    seen: list[str] = []
    cursor: str | None = None
    for _ in range(5):
        url = f"{SESSIONS}/{sid}/questions?limit=2" + (f"&cursor={cursor}" if cursor else "")
        page = client.get(url, headers=alice).json()
        seen += [q["text"] for q in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert seen == [f"q{i}" for i in range(5)]
    for bad in ("limit=0", "limit=101", "cursor=not-a-cursor"):
        assert client.get(f"{SESSIONS}/{sid}/questions?{bad}", headers=alice).status_code == 422, (
            bad
        )


def test_history_shows_failed_answers_and_only_this_sessions_questions(
    client: TestClient,
    run_sql: RunSql,
    alice: dict[str, str],
    setup: tuple[str, str, RecordingProvider],
) -> None:
    sid, resume, _ = setup
    install(client, [RecordingProvider("a", failures=[None, Unavailable()])])
    ask(client, alice, sid, "answered one")
    ask(client, alice, sid, "failed one")
    other = client.post(SESSIONS, headers=alice, json={
        "company_name": "Other", "job_description": "JD", "resume_id": resume, "document_ids": [],
        "preferences": {"language": "en", "question_type": "auto", "answer_format": "bullets",
                        "answer_length": "short", "answer_tone": "simple"},
    }).json()["id"]  # fmt: skip
    ask(client, alice, other, "other session question")
    items = client.get(f"{SESSIONS}/{sid}/questions", headers=alice).json()["items"]
    assert [q["text"] for q in items] == ["answered one", "failed one"]
    assert [q["status"] for q in items] == ["answered", "detected"]
    failed = items[1]["answers"][0]
    assert (failed["status"], failed["error_code"]) == ("error", "llm_unavailable")


def test_history_and_answers_are_owner_scoped_and_indistinguishable(
    client: TestClient, alice: dict[str, str], bob: dict[str, str], run_sql: RunSql,
    setup: tuple[str, str, RecordingProvider],
) -> None:  # fmt: skip
    sid, _, _ = setup
    done = make_history(client, alice, sid, ["secret question"])
    aid = done[0]["answer_id"]
    bob_sid, _, _ = quick_session(client, run_sql, bob)
    unknown = str(uuid.uuid4())

    foreign_list = client.get(f"{SESSIONS}/{sid}/questions", headers=bob)
    unknown_list = client.get(f"{SESSIONS}/{unknown}/questions", headers=bob)
    assert foreign_list.status_code == 404 and safe(foreign_list) == safe(unknown_list)
    assert "secret question" not in foreign_list.text

    foreign_answer = client.get(f"{SESSIONS}/{sid}/answers/{aid}", headers=bob)
    unknown_session_answer = client.get(f"{SESSIONS}/{unknown}/answers/{aid}", headers=bob)
    assert foreign_answer.status_code == 404 and safe(foreign_answer) == safe(
        unknown_session_answer
    )

    # Bob's own session + Alice's answer id looks exactly like an unknown answer id.
    cross = client.get(f"{SESSIONS}/{bob_sid}/answers/{aid}", headers=bob)
    missing = client.get(f"{SESSIONS}/{bob_sid}/answers/{unknown}", headers=bob)
    assert cross.status_code == 404 and safe(cross) == safe(missing)
    assert "ANSWER-FOR" not in cross.text
    assert client.get(f"{SESSIONS}/{sid}/answers/{aid}", headers=alice).status_code == 200


def test_an_answer_is_only_reachable_through_its_own_session(
    client: TestClient,
    run_sql: RunSql,
    alice: dict[str, str],
    setup: tuple[str, str, RecordingProvider],
) -> None:
    sid, resume, _ = setup
    done = make_history(client, alice, sid, ["question"])
    second = quick_session(client, run_sql, alice)[0]
    wrong = client.get(f"{SESSIONS}/{second}/answers/{done[0]['answer_id']}", headers=alice)
    assert wrong.status_code == 404 and wrong.json()["error"]["code"] == "answer_not_found"


def test_the_answer_endpoint_exposes_only_user_facing_fields(
    client: TestClient,
    run_sql: RunSql,
    alice: dict[str, str],
    setup: tuple[str, str, RecordingProvider],
) -> None:
    sid, _, _ = setup
    done = make_history(client, alice, sid, ["question"])
    body = client.get(f"{SESSIONS}/{sid}/answers/{done[0]['answer_id']}", headers=alice).json()
    for hidden in (
        "generation_config",
        "prompt_version",
        "retrieved_chunk_ids",
        "user_id",
        "session_id",
    ):
        assert hidden not in body
    listing = client.get(f"{SESSIONS}/{sid}/questions", headers=alice).text
    for hidden in ("generation_config", "retrieved_chunk_ids", "user_id", "prompt"):
        assert hidden not in listing


# --------------------------------------------------------- stale streaming rows


def stale_answer(
    run_sql: RunSql, session_id: str, owner: Any, age: float, status: str = "streaming"
) -> str:
    qid = run_sql(
        "INSERT INTO detected_questions (session_id, user_id, raw_text) "
        "VALUES (:s, :u, 'stale?') RETURNING id",
        s=session_id, u=owner,
    )[0]["id"]  # fmt: skip
    return str(run_sql(
        "INSERT INTO generated_answers (question_id, session_id, user_id, status, "
        "generation_config, prompt_version, created_at) VALUES "
        "(:q, :s, :u, :st, '{}'::jsonb, 'v1', "
        "now() - make_interval(secs => :age)) RETURNING id",
        q=qid, s=session_id, u=owner, st=status, age=age,
    )[0]["id"])  # fmt: skip


def test_old_streaming_answers_are_read_as_errors_and_persisted(
    client: TestClient,
    run_sql: RunSql,
    alice: dict[str, str],
    setup: tuple[str, str, RecordingProvider],
) -> None:
    sid, _, _ = setup
    owner = run_sql("SELECT id FROM users")[0]["id"]
    old = stale_answer(run_sql, sid, owner, 300)
    fresh = stale_answer(run_sql, sid, owner, 5)
    done = stale_answer(run_sql, sid, owner, 3600, status="complete")

    detail = client.get(f"{SESSIONS}/{sid}/answers/{old}", headers=alice).json()
    assert (detail["status"], detail["error_code"]) == ("error", "generation_interrupted")
    assert (
        client.get(f"{SESSIONS}/{sid}/answers/{fresh}", headers=alice).json()["status"]
        == "streaming"
    )
    assert (
        client.get(f"{SESSIONS}/{sid}/answers/{done}", headers=alice).json()["status"] == "complete"
    )
    stored = {
        str(r["id"]): r["status"] for r in run_sql("SELECT id, status FROM generated_answers")
    }
    assert stored == {old: "error", fresh: "streaming", done: "complete"}  # persisted, no scheduler


def test_the_history_list_also_expires_stale_rows(
    client: TestClient,
    run_sql: RunSql,
    alice: dict[str, str],
    setup: tuple[str, str, RecordingProvider],
) -> None:
    sid, _, _ = setup
    stale_answer(run_sql, sid, run_sql("SELECT id FROM users")[0]["id"], 600)
    items = client.get(f"{SESSIONS}/{sid}/questions", headers=alice).json()["items"]
    assert items[0]["answers"][0]["status"] == "error"


def test_the_stale_threshold_is_the_configured_request_timeout(
    make_client: Callable[..., TestClient], run_sql: RunSql
) -> None:
    client = make_client(QA_REQUEST_TIMEOUT_SECONDS="1")
    alice = register(client, "alice@example.com")
    sid, _, _ = quick_session(client, run_sql, alice)
    owner = run_sql("SELECT id FROM users")[0]["id"]
    just_over = stale_answer(run_sql, sid, owner, 3)
    assert (
        client.get(f"{SESSIONS}/{sid}/answers/{just_over}", headers=alice).json()["status"]
        == "error"
    )


def test_reading_never_expires_another_users_streaming_rows(
    client: TestClient, run_sql: RunSql, alice: dict[str, str], bob: dict[str, str],
    setup: tuple[str, str, RecordingProvider],
) -> None:  # fmt: skip
    sid, _, _ = setup
    alice_id = run_sql("SELECT id FROM users WHERE email = 'alice@example.com'")[0]["id"]
    old = stale_answer(run_sql, sid, alice_id, 300)
    bob_sid, _, _ = quick_session(client, run_sql, bob)
    assert client.get(f"{SESSIONS}/{bob_sid}/questions", headers=bob).status_code == 200
    assert client.get(f"{SESSIONS}/{sid}/questions", headers=bob).status_code == 404
    assert (
        run_sql("SELECT status FROM generated_answers WHERE id = :i", i=old)[0]["status"]
        == "streaming"
    )


def test_a_completed_answer_is_never_rewritten_by_reading(
    client: TestClient,
    run_sql: RunSql,
    alice: dict[str, str],
    setup: tuple[str, str, RecordingProvider],
) -> None:
    sid, _, _ = setup
    done = make_history(client, alice, sid, ["question"])
    run_sql("UPDATE generated_answers SET created_at = now() - interval '1 day'")
    body = client.get(f"{SESSIONS}/{sid}/answers/{done[0]['answer_id']}", headers=alice).json()
    assert body["status"] == "complete" and body["error_code"] is None


# ------------------------------------------------------------- privacy


def test_question_and_answer_text_are_never_logged(
    client: TestClient, alice: dict[str, str], setup: tuple[str, str, RecordingProvider],
    caplog: pytest.LogCaptureFixture,
) -> None:  # fmt: skip
    sid, _, _ = setup
    install(client, [RecordingProvider(reply_fn=lambda m: "PRIVATE-ANSWER-TEXT")])
    with caplog.at_level(logging.DEBUG):
        ask(client, alice, sid, "PRIVATE-QUESTION-TEXT salary")
        client.get(f"{SESSIONS}/{sid}/questions", headers=alice)
    for secret in ("PRIVATE-QUESTION-TEXT", "PRIVATE-ANSWER-TEXT"):
        assert secret not in caplog.text


# --------------------------- each scoping filter matters on its own (mixed-owner rows)


def insert_row(
    run_sql: RunSql,
    *,
    q_session: str,
    q_user: Any,
    a_session: str,
    a_user: Any,
    text: str,
    status: str = "complete",
    age: float = 0,
) -> tuple[str, str]:
    """Insert a question and an answer whose owners/sessions may deliberately DISAGREE (an
    integrity breach), so that a missing user or session filter becomes observable."""
    qid = str(
        run_sql(
            "INSERT INTO detected_questions (session_id, user_id, raw_text, status) "
            "VALUES (:s, :u, :t, 'answered') RETURNING id",
            s=q_session,
            u=q_user,
            t=f"{text}-QUESTION",
        )[0]["id"]  # fmt: skip
    )
    aid = str(
        run_sql(
            "INSERT INTO generated_answers (question_id, session_id, user_id, text, status, "
            "generation_config, prompt_version, created_at) VALUES (:q, :s, :u, :t, :st, "
            "'{}'::jsonb, 'v1', now() - make_interval(secs => :age)) RETURNING id",
            q=qid,
            s=a_session,
            u=a_user,
            t=f"{text}-ANSWER",
            st=status,
            age=age,
        )[0]["id"]  # fmt: skip
    )
    return qid, aid


def test_history_and_answer_reads_filter_on_both_user_and_session(
    client: TestClient, run_sql: RunSql, alice: dict[str, str], bob: dict[str, str]
) -> None:
    sa, _, alice_id = quick_session(client, run_sql, alice)
    sb, _, _ = quick_session(client, run_sql, bob)
    bob_id = run_sql("SELECT id FROM users WHERE email = 'bob@example.com'")[0]["id"]
    # (question session/user, answer session/user)
    _, x_answer = insert_row(
        run_sql,
        q_session=sb,
        q_user=alice_id,
        a_session=sb,
        a_user=alice_id,
        text="ALICE-IN-BOBS-SESSION",
    )  # noqa: E501
    _, y_answer = insert_row(
        run_sql,
        q_session=sa,
        q_user=bob_id,
        a_session=sa,
        a_user=bob_id,
        text="BOB-IN-ALICES-SESSION",
    )  # noqa: E501
    own_q, own_a = insert_row(
        run_sql, q_session=sa, q_user=alice_id, a_session=sa, a_user=alice_id, text="OWN"
    )  # noqa: E501
    # Foreign answers attached to Alice's legitimate question (wrong user / wrong session).
    run_sql(
        "INSERT INTO generated_answers (question_id, session_id, user_id, text, status, "
        "generation_config, prompt_version) VALUES (:q, :s, :u, 'WRONG-USER-ANSWER', 'complete', "
        "'{}'::jsonb, 'v1'), (:q, :s2, :a, 'WRONG-SESSION-ANSWER', 'complete', '{}'::jsonb, 'v1')",
        q=own_q, s=sa, u=bob_id, s2=sb, a=alice_id,
    )  # fmt: skip

    alice_list = client.get(f"{SESSIONS}/{sa}/questions", headers=alice).text
    assert "OWN-QUESTION" in alice_list and "OWN-ANSWER" in alice_list
    for leaked in (
        "BOB-IN-ALICES-SESSION",
        "ALICE-IN-BOBS-SESSION",
        "WRONG-USER-ANSWER",
        "WRONG-SESSION-ANSWER",
    ):
        assert leaked not in alice_list, leaked
    bob_list = client.get(f"{SESSIONS}/{sb}/questions", headers=bob).text
    assert "ALICE-IN-BOBS-SESSION" not in bob_list and "WRONG-SESSION-ANSWER" not in bob_list

    assert client.get(f"{SESSIONS}/{sa}/answers/{own_a}", headers=alice).status_code == 200
    for session, headers, answer in (
        (sb, bob, x_answer),
        (sa, alice, x_answer),
        (sa, alice, y_answer),
    ):
        response = client.get(f"{SESSIONS}/{session}/answers/{answer}", headers=headers)
        assert (
            response.status_code == 404 and response.json()["error"]["code"] == "answer_not_found"
        )


def test_stale_expiry_filters_on_both_user_and_session(
    client: TestClient, run_sql: RunSql, alice: dict[str, str], bob: dict[str, str]
) -> None:
    sa, _, alice_id = quick_session(client, run_sql, alice)
    sb, _, _ = quick_session(client, run_sql, bob)
    bob_id = run_sql("SELECT id FROM users WHERE email = 'bob@example.com'")[0]["id"]
    # Bob reads his own session SB. Neither row below may be expired by that read:
    wrong_user = insert_row(
        run_sql,
        q_session=sb,
        q_user=alice_id,
        a_session=sb,
        a_user=alice_id,
        text="W1",
        status="streaming",
        age=600,
    )[1]  # noqa: E501
    wrong_session = insert_row(
        run_sql,
        q_session=sa,
        q_user=bob_id,
        a_session=sa,
        a_user=bob_id,
        text="W2",
        status="streaming",
        age=600,
    )[1]  # noqa: E501
    own = insert_row(
        run_sql,
        q_session=sb,
        q_user=bob_id,
        a_session=sb,
        a_user=bob_id,
        text="OWN",
        status="streaming",
        age=600,
    )[1]  # noqa: E501
    assert client.get(f"{SESSIONS}/{sb}/questions", headers=bob).status_code == 200
    states = {
        str(r["id"]): r["status"] for r in run_sql("SELECT id, status FROM generated_answers")
    }
    assert states[own] == "error"  # Bob's own stale row in his own session is expired
    assert states[wrong_user] == "streaming" and states[wrong_session] == "streaming"
