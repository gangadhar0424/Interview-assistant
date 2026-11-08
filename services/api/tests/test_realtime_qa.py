"""Phase 9B: typed Q&A over the WebSocket (D-42): flow, cancel, refusals, isolation, cleanup.

The real application runs against the clean test database with a fake LLM provider behind the
production `QuestionAnswerer`; Redis is the usual `FakeRedis` behind the production `TicketStore`.
Backpressure and real disconnects are in test_realtime_channel.py / test_realtime_qa_live.py.

Every receive goes through `recv`, which fails the test after a timeout instead of hanging it.
"""

import json
import threading
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from starlette.websockets import WebSocketDisconnect

from app.ai.errors import Unavailable
from app.core.config import get_settings
from app.embeddings.fake import FakeEmbeddingProvider
from app.qa import events as qa
from app.qa import sse
from app.qa.limits import QuestionLimits
from app.qa.service import QuestionAnswerer
from app.realtime import adapter
from app.realtime import schemas as proto
from tests.qa_fakes import RecordingProvider, SlowTokensProvider, answer_for_question
from tests.test_qa_service import answers, make_router, questions
from tests.test_realtime_gateway import (  # noqa: F401  (fixtures are reused)
    LogCapture,
    World,
    build_world,
    expect_close,
    ping,
    say_hello,
    wait_for,
)
from tests.test_realtime_gateway import logs as logs  # noqa: F401
from tests.test_sessions import SESSIONS

TERMINAL = ("answer.done", "answer.error", "answer.cancelled")


@pytest.fixture
def worlds(request: pytest.FixtureRequest) -> Callable[..., World]:
    """The 9A `build_world` factory (registered by the import above)."""
    factory: Callable[..., World] = request.getfixturevalue("build_world")
    return factory


# ----------------------------------------------------------------------------- helpers
def install(
    world: World, providers: list[Any], *, limits: QuestionLimits | None = None
) -> QuestionLimits:
    """Swap in an answerer with fake providers (production wiring never does this)."""
    app: Any = world.client.app
    app.state.question_answerer = QuestionAnswerer(
        app.state.sessionmaker,
        get_settings(),
        make_router(providers),
        FakeEmbeddingProvider(),
    )
    app.state.question_limits = limits or QuestionLimits(1000, 1000)
    return app.state.question_limits  # type: ignore[no-any-return]


def recv(ws: Any, timeout: float = 15.0) -> dict[str, Any]:
    """`receive_json` that fails the test instead of hanging when nothing arrives."""
    box: list[tuple[str, Any]] = []

    def run() -> None:
        try:
            box.append(("ok", ws.receive_json()))
        except BaseException as exc:  # noqa: BLE001 - handed back to the test thread
            box.append(("err", exc))

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout)
    if not box:
        raise AssertionError(f"no message within {timeout} s")
    kind, value = box[0]
    if kind == "err":
        raise value
    assert isinstance(value, dict)
    return value


def until(ws: Any, *types: str) -> list[dict[str, Any]]:
    """Messages up to and including the first one whose type is in `types`."""
    out: list[dict[str, Any]] = []
    while True:
        message = recv(ws)
        out.append(message)
        if message["type"] in types:
            return out


def submit(ws: Any, text: str = "Tell me about yourself", **payload: Any) -> None:
    ws.send_json({"type": "question.submit", "payload": {"text": text, **payload}})


def cancel(ws: Any, question_id: str) -> None:
    ws.send_json({"type": "answer.cancel", "payload": {"question_id": question_id}})


def kinds(messages: list[dict[str, Any]]) -> list[str]:
    return [m["type"] for m in messages]


def streamed_text(messages: list[dict[str, Any]]) -> str:
    return "".join(m["payload"]["text"] for m in messages if m["type"] == "answer.delta")


def assert_pong(ws: Any) -> None:
    ws.send_json(ping())
    assert recv(ws)["type"] == "pong"


def expect_disconnect(ws: Any) -> WebSocketDisconnect:
    """Read until the server closes the socket (queued events may arrive first)."""
    while True:
        try:
            recv(ws)
        except WebSocketDisconnect as closed:
            return closed


def idle(limits: QuestionLimits, user_id: uuid.UUID) -> bool:
    return wait_for(lambda: limits.active(user_id) == 0, timeout=8.0)


# ----------------------------------------------------------------------------- the happy path
def test_a_question_streams_in_order_with_consecutive_sequence_numbers(
    worlds: Callable[..., World],
) -> None:
    world = worlds()
    limits = install(world, [RecordingProvider()])
    with world.connect() as ws:
        say_hello(ws)
        submit(ws)
        events = until(ws, *TERMINAL)

    names = kinds(events)
    assert names[:3] == ["question.created", "retrieval.done", "answer.started"]
    assert names[-1] == "answer.done"
    assert set(names[3:-1]) == {"answer.delta"} and len(names) > 4
    assert [m["seq"] for m in events] == list(range(1, len(events) + 1))
    assert len({m["id"] for m in events}) == len(events)
    for message in events:
        proto.server_adapter.validate_python(message)  # the committed contract
    assert [m["payload"]["index"] for m in events if m["type"] == "answer.delta"] == list(
        range(names.count("answer.delta"))
    )

    created, done = events[0]["payload"], events[-1]["payload"]
    (question,) = questions(world.run_sql)
    (answer,) = answers(world.run_sql)
    assert str(question["id"]) == created["question_id"] and question["status"] == "answered"
    assert str(question["user_id"]) == str(world.alice_id)
    assert str(question["session_id"]) == world.alice_session
    assert str(answer["id"]) == created["answer_id"] == done["answer_id"]
    assert answer["status"] == "complete" and answer["text"] == streamed_text(events)
    assert set(done) == {
        "answer_id", "question_id", "provider", "model", "metrics", "fallback_used",
        "input_tokens", "output_tokens",
    }  # fmt: skip
    assert idle(limits, world.alice_id)


def test_a_second_question_on_the_same_socket_continues_the_sequence(
    worlds: Callable[..., World],
) -> None:
    world = worlds()
    install(world, [RecordingProvider()])
    with world.connect() as ws:
        say_hello(ws)
        submit(ws, "First question")
        first = until(ws, *TERMINAL)
        submit(ws, "Second question")
        second = until(ws, *TERMINAL)
    assert kinds(second)[-1] == "answer.done"
    assert second[0]["seq"] == first[-1]["seq"] + 1
    assert [m["seq"] for m in second] == list(range(second[0]["seq"], second[-1]["seq"] + 1))
    assert len(questions(world.run_sql)) == 2


def test_control_events_are_never_numbered(worlds: Callable[..., World]) -> None:
    world = worlds()
    install(world, [SlowTokensProvider(token_delay=0.05, tokens=6)])
    with world.connect() as ws:
        ready = recv(ws)
        ws.send_json({"type": "client.hello", "payload": {"protocol_version": 1}})
        submit(ws)
        until(ws, "question.created")
        ws.send_json(ping())
        messages = until(ws, "pong")
        until(ws, *TERMINAL)
    assert ready["seq"] == 0 and messages[-1]["seq"] == 0


# ----------------------------------------------------------------------------- SSE parity
def _sse_data(event: qa.QaEvent) -> tuple[str, dict[str, Any]]:
    head, data = sse.event_to_frame(event).decode().rstrip("\n").split("\n")
    return head.removeprefix("event: "), json.loads(data.removeprefix("data: "))


def test_every_event_payload_is_identical_to_its_sse_frame() -> None:
    q, a = uuid.uuid4(), uuid.uuid4()
    samples: list[qa.QaEvent] = [
        qa.QuestionCreated(q, a),
        qa.RetrievalDone(q, 3, 12),
        qa.AnswerStarted(a, q, "fake", "fake-echo"),
        qa.AnswerDelta(a, 4, 'héllo\n"quoted"   ✓'),
        qa.AnswerDone(a, q, "fake", "m", 5, 6, 7, True, 11, 22),
        qa.AnswerDone(a, q, "fake", "m", 5, 6, 7, False, None, None),
        qa.AnswerFailed(a, q, "llm_unavailable", "The AI service is unavailable.", True),
    ]
    for event in samples:
        name, data = _sse_data(event)
        message = adapter.to_message(event, 9)
        assert message.model_dump(mode="json")["type"] == name
        assert json.loads(message.model_dump_json())["payload"] == data
        assert message.seq == 9  # type: ignore[attr-defined]


# ----------------------------------------------------------------------------- one at a time
def test_a_second_question_during_a_generation_is_refused_without_cancelling_it(
    worlds: Callable[..., World],
) -> None:
    world = worlds()
    provider = SlowTokensProvider(token_delay=0.05, tokens=10)
    limits = install(world, [provider])
    with world.connect() as ws:
        say_hello(ws)
        submit(ws, "The first question")
        until(ws, "question.created")
        submit(ws, "The second question")
        events = until(ws, *TERMINAL)
        refusal = next(m for m in events if m["type"] == "error")
        assert refusal["seq"] == 0
        assert refusal["payload"] == {
            "code": "generation_in_progress",
            "message": proto.ERROR_MESSAGES["generation_in_progress"],
            "fatal": False,
        }
        assert events[-1]["type"] == "answer.done"  # the running answer was not cancelled
        assert_pong(ws)
    assert len(questions(world.run_sql)) == 1 and provider.calls == 1
    assert idle(limits, world.alice_id)


# ----------------------------------------------------------------------------- cancel
def test_cancel_mid_stream_stores_the_partial_answer_and_ends_with_answer_cancelled(
    worlds: Callable[..., World],
) -> None:
    world = worlds()
    provider = SlowTokensProvider(token_delay=0.05, tokens=400)
    limits = install(world, [provider])
    with world.connect() as ws:
        say_hello(ws)
        submit(ws)
        head = until(ws, "answer.delta")
        question_id = head[0]["payload"]["question_id"]
        cancel(ws, question_id)
        tail = until(ws, "answer.cancelled")
        events = head + tail

        assert [m["type"] for m in events if m["type"] in TERMINAL] == ["answer.cancelled"]
        assert events[-1]["payload"] == {
            "answer_id": head[0]["payload"]["answer_id"],
            "question_id": question_id,
        }
        assert [m["seq"] for m in events] == list(range(1, len(events) + 1))
        assert wait_for(lambda: provider.closed)  # the provider stream was closed
        (answer,) = answers(world.run_sql)
        assert answer["status"] == "cancelled"
        assert answer["text"] == streamed_text(events) != ""  # the partial text, as delivered
        assert idle(limits, world.alice_id)

        provider.tokens = 3  # the socket stays usable after a cancel
        submit(ws, "Another question")
        assert until(ws, *TERMINAL)[-1]["type"] == "answer.done"
    assert [a["status"] for a in answers(world.run_sql)] == ["cancelled", "complete"]


def test_cancel_before_the_first_token(worlds: Callable[..., World]) -> None:
    world = worlds()
    provider = SlowTokensProvider(token_delay=2.0, tokens=3)
    limits = install(world, [provider])
    with world.connect() as ws:
        say_hello(ws)
        submit(ws)
        created = until(ws, "question.created")[0]
        cancel(ws, created["payload"]["question_id"])
        events = until(ws, *TERMINAL)
    assert events[-1]["type"] == "answer.cancelled"
    assert "answer.delta" not in kinds(events)
    (answer,) = answers(world.run_sql)
    assert (answer["status"], answer["text"]) == ("cancelled", "")
    assert idle(limits, world.alice_id)


def test_cancel_after_the_answer_is_done_cannot_downgrade_it(
    worlds: Callable[..., World],
) -> None:
    world = worlds()
    install(world, [RecordingProvider()])
    with world.connect() as ws:
        say_hello(ws)
        submit(ws)
        events = until(ws, *TERMINAL)
        cancel(ws, events[0]["payload"]["question_id"])
        error = recv(ws)
    assert error["payload"]["code"] == "nothing_to_cancel" and error["payload"]["fatal"] is False
    (answer,) = answers(world.run_sql)
    assert answer["status"] == "complete" and answer["text"] == streamed_text(events)


def test_cancel_for_an_unknown_foreign_or_missing_question_is_one_indistinguishable_error(
    worlds: Callable[..., World],
) -> None:
    world = worlds()
    install(world, [SlowTokensProvider(token_delay=0.05, tokens=12)])
    with world.connect("alice") as alice, world.connect("bob") as bob:
        say_hello(alice)
        say_hello(bob)
        submit(bob, "Bob's private question")
        bobs = until(bob, "question.created")[0]["payload"]["question_id"]

        replies = []
        for target in (bobs, str(uuid.uuid4())):  # a real foreign id, and an id that exists nowhere
            cancel(alice, target)
            replies.append(recv(alice)["payload"])
        assert (
            replies[0]
            == replies[1]
            == {
                "code": "nothing_to_cancel",
                "message": proto.ERROR_MESSAGES["nothing_to_cancel"],
                "fatal": False,
            }
        )
        assert until(bob, *TERMINAL)[-1]["type"] == "answer.done"  # Bob's answer was untouched
        assert_pong(alice)
    assert [a["status"] for a in answers(world.run_sql)] == ["complete"]


def test_cancel_with_the_wrong_id_never_touches_the_running_generation(
    worlds: Callable[..., World],
) -> None:
    world = worlds()
    limits = install(world, [SlowTokensProvider(token_delay=0.05, tokens=10)])
    with world.connect() as ws:
        say_hello(ws)
        submit(ws)
        until(ws, "question.created")
        cancel(ws, str(uuid.uuid4()))  # not the running question
        events = until(ws, *TERMINAL)
        refusal = next(m for m in events if m["type"] == "error")
        assert refusal["payload"]["code"] == "nothing_to_cancel"
        assert events[-1]["type"] == "answer.done"  # the running answer was not cancelled
    assert [a["status"] for a in answers(world.run_sql)] == ["complete"]
    assert idle(limits, world.alice_id)


def test_cancel_with_a_malformed_question_id_is_an_invalid_message(
    worlds: Callable[..., World],
) -> None:
    world = worlds()
    with world.connect() as ws:
        say_hello(ws)
        ws.send_json({"type": "answer.cancel", "payload": {"question_id": "not-a-uuid"}})
        assert recv(ws)["payload"]["code"] == "invalid_message"
        assert_pong(ws)


# ----------------------------------------------------------------------------- refusals
def _refusal(ws: Any) -> dict[str, Any]:
    message = recv(ws)
    assert message["type"] == "error" and message["seq"] == 0
    assert message["payload"]["fatal"] is False
    assert message["payload"]["message"] == proto.ERROR_MESSAGES[message["payload"]["code"]]
    return dict(message["payload"])


def _expect_refused_then_alive(world: World, ws: Any, code: str, limits: QuestionLimits) -> None:
    assert _refusal(ws)["code"] == code
    assert_pong(ws)  # non-fatal: the socket is still usable, and no sequenced event was sent
    assert questions(world.run_sql) == [] and answers(world.run_sql) == []
    assert idle(limits, world.alice_id)  # the slot taken for the attempt was released


@pytest.mark.parametrize(
    "text, extra",
    [
        ("   ", {}),
        ("x" * 3000, {}),
        ("A valid question", {"question_type_override": "not-a-real-type"}),
    ],
)
def test_invalid_questions_are_refused_with_validation_error(
    worlds: Callable[..., World], text: str, extra: dict[str, Any]
) -> None:
    world = worlds()
    limits = install(world, [RecordingProvider()])
    with world.connect() as ws:
        say_hello(ws)
        submit(ws, text, **extra)
        _expect_refused_then_alive(world, ws, "validation_error", limits)


def test_a_session_deleted_after_the_handshake_is_refused_as_not_found(
    worlds: Callable[..., World],
) -> None:
    world = worlds()
    limits = install(world, [RecordingProvider()])
    with world.connect() as ws:
        say_hello(ws)
        deleted = world.client.delete(f"{SESSIONS}/{world.alice_session}", headers=world.alice)
        assert deleted.status_code == 204, deleted.text
        submit(ws)
        _expect_refused_then_alive(world, ws, "session_not_found", limits)


def test_a_session_that_is_not_ready_is_refused(worlds: Callable[..., World]) -> None:
    world = worlds()
    limits = install(world, [RecordingProvider()])
    with world.connect() as ws:
        say_hello(ws)
        world.run_sql("UPDATE sessions SET status = 'ended' WHERE id = :i", i=world.alice_session)
        submit(ws)
        _expect_refused_then_alive(world, ws, "session_not_ready", limits)


def test_materials_that_are_no_longer_ready_are_refused(
    worlds: Callable[..., World],
) -> None:
    world = worlds()
    limits = install(world, [RecordingProvider()])
    with world.connect() as ws:
        say_hello(ws)
        world.run_sql(
            "UPDATE ingestion_jobs SET status = 'failed' "
            "WHERE stage = 'extract' "
            "AND source_id = (SELECT resume_id FROM sessions WHERE id = :i)",
            i=world.alice_session,
        )
        submit(ws)
        _expect_refused_then_alive(world, ws, "materials_not_ready", limits)


def test_the_per_user_question_rate_limit_applies_over_the_socket(
    worlds: Callable[..., World],
) -> None:
    world = worlds()
    limits = install(world, [RecordingProvider()], limits=QuestionLimits(1, 10))
    with world.connect() as ws:
        say_hello(ws)
        submit(ws, "The first")
        assert until(ws, *TERMINAL)[-1]["type"] == "answer.done"
        submit(ws, "The second")
        assert _refusal(ws)["code"] == "rate_limited"
        assert_pong(ws)
    assert len(questions(world.run_sql)) == 1
    assert idle(limits, world.alice_id)


def test_the_concurrent_generation_cap_is_shared_by_all_of_a_users_sockets(
    worlds: Callable[..., World],
) -> None:
    world = worlds()
    provider = SlowTokensProvider(token_delay=0.05, tokens=12)
    limits = install(world, [provider], limits=QuestionLimits(100, 1))
    with world.connect("alice", world.alice_session) as first:
        say_hello(first)
        submit(first)
        until(first, "question.created")
        assert limits.active(world.alice_id) == 1  # the socket's generation holds the shared slot
        with world.connect("alice", world.alice_session_2) as second:
            say_hello(second)
            submit(second)
            assert _refusal(second)["code"] == "too_many_generations"
            assert_pong(second)
        assert until(first, *TERMINAL)[-1]["type"] == "answer.done"
    assert len(questions(world.run_sql)) == 1
    assert idle(limits, world.alice_id)


# ----------------------------------------------------------------------------- provider failure
def test_a_provider_failure_is_a_safe_answer_error_and_the_socket_stays_usable(
    worlds: Callable[..., World],
) -> None:
    world = worlds()
    limits = install(world, [RecordingProvider("a", failures=[Unavailable(), None])])
    with world.connect() as ws:
        say_hello(ws)
        submit(ws)
        events = until(ws, *TERMINAL)
        assert events[-1]["type"] == "answer.error"
        assert events[-1]["payload"]["code"] == "llm_unavailable"
        assert events[-1]["payload"]["retryable"] is True
        assert events[-1]["payload"]["message"] == "The AI service is unavailable."
        assert [m["seq"] for m in events] == list(range(1, len(events) + 1))
        assert idle(limits, world.alice_id)
        row = world.run_sql("SELECT status, error_code, text FROM generated_answers")[0]
        assert (row["status"], row["error_code"], row["text"]) == ("error", "llm_unavailable", "")
        submit(ws, "Try again")
        assert until(ws, *TERMINAL)[-1]["type"] == "answer.done"


def test_an_unexpected_server_failure_never_leaks_its_detail(
    worlds: Callable[..., World], monkeypatch: pytest.MonkeyPatch
) -> None:
    world = worlds()
    limits = install(world, [RecordingProvider()])
    real = adapter.to_message

    def broken(event: qa.QaEvent, seq: int) -> Any:
        if isinstance(event, qa.AnswerDelta):
            raise RuntimeError("SECRET-INTERNAL-DETAIL /srv/app/internal.py")
        return real(event, seq)

    monkeypatch.setattr(adapter, "to_message", broken)
    with world.connect() as ws:
        say_hello(ws)
        submit(ws)
        events = until(ws, *TERMINAL)
        assert events[-1]["type"] == "answer.error"
        assert events[-1]["payload"]["code"] == "internal_error"
        assert events[-1]["payload"]["message"] == proto.ERROR_MESSAGES["internal_error"]
        assert "SECRET" not in json.dumps(events)
        assert idle(limits, world.alice_id)
        assert answers(world.run_sql)[0]["status"] == "cancelled"  # the stream was closed cleanly
        monkeypatch.setattr(adapter, "to_message", real)
        submit(ws, "Works again")
        assert until(ws, *TERMINAL)[-1]["type"] == "answer.done"


# ----------------------------------------------------------------------------- isolation
@pytest.mark.parametrize(
    "extra",
    [
        {"user_id": "00000000-0000-0000-0000-000000000001"},
        {"session_id": "00000000-0000-0000-0000-000000000002"},
        {"document_ids": ["00000000-0000-0000-0000-000000000003"]},
        {"provider": "fake"},
    ],
)
def test_a_question_can_never_carry_an_identity_or_a_source(
    worlds: Callable[..., World], extra: dict[str, Any]
) -> None:
    world = worlds()
    limits = install(world, [RecordingProvider()])
    with world.connect() as ws:
        say_hello(ws)
        submit(ws, "A question", **extra)
        assert _refusal(ws)["code"] == "invalid_message"
        assert_pong(ws)
    assert questions(world.run_sql) == [] and limits.active(world.alice_id) == 0


def test_two_users_asking_at_once_each_get_only_their_own_answer(
    worlds: Callable[..., World],
) -> None:
    world = worlds()
    install(world, [RecordingProvider(reply_fn=answer_for_question)])
    with world.connect("alice") as alice, world.connect("bob") as bob:
        say_hello(alice)
        say_hello(bob)
        submit(alice, "ALICE-CANARY-QUESTION")
        submit(bob, "BOB-CANARY-QUESTION")
        a_events = until(alice, *TERMINAL)
        b_events = until(bob, *TERMINAL)
    assert a_events[-1]["type"] == b_events[-1]["type"] == "answer.done"
    assert "BOB-CANARY" not in json.dumps(a_events)
    assert "ALICE-CANARY" not in json.dumps(b_events)
    rows = {
        str(q["id"]): (str(q["user_id"]), str(q["session_id"]), q["raw_text"])
        for q in questions(world.run_sql)
    }
    assert rows[a_events[0]["payload"]["question_id"]] == (
        str(world.alice_id), world.alice_session, "ALICE-CANARY-QUESTION",
    )  # fmt: skip
    assert rows[b_events[0]["payload"]["question_id"]] == (
        str(world.bob_id), world.bob_session, "BOB-CANARY-QUESTION",
    )  # fmt: skip


def test_a_user_disabled_after_the_handshake_cannot_ask(
    worlds: Callable[..., World],
) -> None:
    world = worlds()
    limits = install(world, [RecordingProvider()])
    with world.connect() as ws:
        say_hello(ws)
        assert_pong(ws)  # an idle socket is allowed to live (R-44) ...
        world.run_sql("UPDATE users SET status = 'disabled' WHERE id = :i", i=world.alice_id)
        submit(ws)  # ... but the next ACTION re-validates the account
        error = recv(ws)
        assert error["type"] == "error"
        assert error["payload"] == {
            "code": "account_inactive",
            "message": proto.ERROR_MESSAGES["account_inactive"],
            "fatal": True,
        }
        assert expect_disconnect(ws).code == proto.CLOSE_POLICY
    assert questions(world.run_sql) == [] and answers(world.run_sql) == []
    assert limits.active(world.alice_id) == 0


# ----------------------------------------------------------------------------- cleanup
def test_closing_the_socket_mid_stream_detaches_and_the_grace_expiry_cleans_everything_up(
    worlds: Callable[..., World],
) -> None:
    """9C: a closing socket no longer cancels at once; the grace expiry does (1 s here)."""
    world = worlds(WS_RESUME_GRACE_SECONDS="1")
    provider = SlowTokensProvider(token_delay=0.05, tokens=400)
    limits = install(world, [provider])
    with world.connect() as ws:
        say_hello(ws)
        submit(ws)
        until(ws, "answer.delta")
    assert limits.active(world.alice_id) == 1  # detached, still generating, slot still held
    assert idle(limits, world.alice_id)  # released once the grace is over
    assert wait_for(lambda: provider.closed and not provider.finished)
    assert wait_for(lambda: world.registry_size() == 0)
    assert wait_for(lambda: answers(world.run_sql)[0]["status"] == "cancelled")
    assert answers(world.run_sql)[0]["text"] != ""  # the partial text is kept


def test_the_answer_is_stored_before_the_slot_is_released(
    worlds: Callable[..., World], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stream must be closed (which stores the `cancelled` answer) BEFORE the slot is freed:
    deterministic cleanup, not whenever the garbage collector gets to the abandoned generator."""
    world = worlds()
    seen: list[str] = []

    class Probe(QuestionLimits):
        def _release(self, user_id: uuid.UUID) -> None:
            def peek() -> None:  # `run_sql` needs its own thread: it starts an event loop
                seen.append(str(world.run_sql("SELECT status FROM generated_answers")[0]["status"]))

            reader = threading.Thread(target=peek)
            reader.start()
            reader.join(10)
            super()._release(user_id)

    install(world, [RecordingProvider()], limits=Probe(1000, 1000))
    real = adapter.to_message

    def broken(event: qa.QaEvent, seq: int) -> Any:
        if isinstance(event, qa.AnswerDelta):
            raise RuntimeError("boom")  # the generation stops with its stream still open
        return real(event, seq)

    monkeypatch.setattr(adapter, "to_message", broken)
    with world.connect() as ws:
        say_hello(ws)
        submit(ws)
        assert until(ws, *TERMINAL)[-1]["type"] == "answer.error"
        assert wait_for(lambda: len(seen) == 1)
    assert seen == ["cancelled"]


def test_the_grace_expiry_stops_a_generation_that_is_waiting_for_the_provider(
    worlds: Callable[..., World],
) -> None:
    """Not only a streaming answer: a generation stalled before its first token must also be
    stopped promptly when the grace expires, not when the provider eventually speaks."""
    world = worlds(WS_RESUME_GRACE_SECONDS="1")
    provider = SlowTokensProvider(token_delay=30.0, tokens=3)
    limits = install(world, [provider])
    with world.connect() as ws:
        say_hello(ws)
        submit(ws)
        until(ws, "retrieval.done")
        assert wait_for(lambda: provider.calls == 1)  # waiting for the provider's first token
    assert wait_for(lambda: limits.active(world.alice_id) == 0, timeout=6.0)
    assert wait_for(lambda: provider.closed, timeout=6.0) and provider.emitted == 0
    assert wait_for(lambda: answers(world.run_sql)[0]["status"] == "cancelled", timeout=6.0)


def test_a_resume_point_on_a_new_channel_is_unavailable(worlds: Callable[..., World]) -> None:
    world = worlds()
    with world.connect() as ws:
        recv(ws)
        ws.send_json(
            {"type": "client.hello", "payload": {"protocol_version": 1, "resume_from_seq": 5}}
        )
        assert recv(ws)["payload"]["code"] == "resume_unavailable"
        assert_pong(ws)


# ----------------------------------------------------------------------------- logging
def test_no_question_answer_or_ticket_text_is_ever_logged(
    worlds: Callable[..., World], logs: LogCapture
) -> None:
    world = worlds()
    install(
        world,
        [RecordingProvider(reply_fn=lambda m: "CANARY-ANSWER-7712 " + "words " * 20)],
    )
    ticket = world.ticket()
    path = f"/api/v1/ws/sessions/{world.alice_session}?ticket={ticket}"
    with world.client.websocket_connect(path) as ws:
        say_hello(ws)
        submit(ws, "CANARY-QUESTION-8841 please")
        events = until(ws, *TERMINAL)
        assert events[-1]["type"] == "answer.done"
        cancel(ws, events[0]["payload"]["question_id"])
        recv(ws)
    text = "\n".join(logs.lines)
    assert "CANARY" not in text and ticket not in text
