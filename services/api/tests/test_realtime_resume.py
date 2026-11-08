"""Phase 9C: detached generation, grace period, `resume_from_seq` replay, isolation and the
cancel-versus-final-commit race (D-43).

Runs the real application against the clean test database with a fake LLM provider behind the
production `QuestionAnswerer`. Real-socket reconnects and the load smoke are in
test_realtime_resume_live.py.
"""

import asyncio
import json
import threading
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from starlette.websockets import WebSocketDisconnect

from app.main import create_app
from app.qa import repository
from app.qa.events import AnswerDelta
from app.qa.service import QuestionAnswerer
from app.realtime import adapter
from app.realtime import schemas as proto
from tests.qa_fakes import RecordingProvider, SlowTokensProvider
from tests.test_qa_service import answers, questions
from tests.test_realtime_gateway import World, build_world, logs, wait_for  # noqa: F401
from tests.test_realtime_qa import (
    TERMINAL,
    assert_pong,
    cancel,
    idle,
    install,
    kinds,
    recv,
    streamed_text,
    submit,
    until,
)
from tests.test_realtime_session_channel import FakeGeneration


@pytest.fixture
def make_world(request: pytest.FixtureRequest) -> Callable[..., World]:
    """The 9A `build_world` factory (registered by the import above)."""
    factory: Callable[..., World] = request.getfixturevalue("build_world")
    return factory


def reconnect(world: World, who: str = "alice", session: str | None = None) -> Any:
    return world.connect(who, session)


def hello_with(ws: Any, resume_from_seq: int | None = None) -> dict[str, Any]:
    """Read `session.ready`, send `client.hello` (optionally with a resume point); return ready."""
    ready = recv(ws)
    assert ready["type"] == "session.ready"
    payload: dict[str, Any] = {"protocol_version": 1}
    if resume_from_seq is not None:
        payload["resume_from_seq"] = resume_from_seq
    ws.send_json({"type": "client.hello", "payload": payload})
    return ready


def read_deltas(ws: Any, count: int) -> list[dict[str, Any]]:
    """Messages up to and including the `count`-th answer.delta."""
    seen: list[dict[str, Any]] = []
    while sum(1 for m in seen if m["type"] == "answer.delta") < count:
        seen.append(recv(ws))
    return seen


def seqs(messages: list[dict[str, Any]]) -> list[int]:
    return [m["seq"] for m in messages if m["seq"] > 0]


def assert_consecutive(messages: list[dict[str, Any]]) -> None:
    numbers = seqs(messages)
    assert numbers == list(range(numbers[0], numbers[0] + len(numbers)))


def channels(world: World) -> Any:
    return world.client.app.state.session_channels  # type: ignore[attr-defined]


# ----------------------------------------------------------------------------- detach and resume
def test_a_dropped_socket_does_not_cancel_the_answer_and_resume_replays_the_rest(
    make_world: Callable[..., World],
) -> None:
    world = make_world()
    provider = SlowTokensProvider(token_delay=0.05, tokens=40)
    limits = install(world, [provider])
    with reconnect(world) as first:
        hello_with(first)
        submit(first)
        head = read_deltas(first, 3)
    # the socket dropped mid-answer; the generation is NOT cancelled
    with reconnect(world) as second:
        ready = hello_with(second, resume_from_seq=head[-1]["seq"])
        assert ready["payload"]["resume_seq"] >= head[-1]["seq"]
        tail = until(second, *TERMINAL)
    events = head + tail
    assert tail[0]["seq"] == head[-1]["seq"] + 1  # replay starts right after the resume point
    assert_consecutive(events)  # no gap, no duplicate across the reconnect
    assert kinds(events)[-1] == "answer.done" and kinds(events).count("answer.done") == 1
    (answer,) = answers(world.run_sql)
    assert answer["status"] == "complete" and answer["text"] == streamed_text(events)
    assert len(questions(world.run_sql)) == 1 and provider.finished
    assert idle(limits, world.alice_id)


def test_an_event_missed_while_detached_is_replayed_including_the_terminal_one(
    make_world: Callable[..., World],
) -> None:
    world = make_world()
    provider = SlowTokensProvider(token_delay=0.01, tokens=8)
    limits = install(world, [provider])
    with reconnect(world) as first:
        hello_with(first)
        submit(first)
        head = until(first, "retrieval.done")
    assert wait_for(lambda: provider.finished)  # the whole answer is produced with nobody attached
    assert idle(limits, world.alice_id)
    with reconnect(world) as second:
        hello_with(second, resume_from_seq=head[-1]["seq"])
        tail = until(second, *TERMINAL)
    events = head + tail
    assert_consecutive(events) and kinds(events)[-1] == "answer.done"
    assert answers(world.run_sql)[0]["text"] == streamed_text(events)


def test_resuming_from_the_last_sequence_replays_nothing(
    make_world: Callable[..., World],
) -> None:
    world = make_world()
    install(world, [RecordingProvider()])
    with reconnect(world) as first:
        hello_with(first)
        submit(first)
        done = until(first, *TERMINAL)
    last = done[-1]["seq"]
    with reconnect(world) as second:
        ready = hello_with(second, resume_from_seq=last)
        assert ready["payload"]["resume_seq"] == last
        assert_pong(second)  # the very next message is the pong: no replay, no error


def test_a_reconnect_without_a_resume_point_continues_live_only(
    make_world: Callable[..., World],
) -> None:
    world = make_world()
    provider = SlowTokensProvider(token_delay=0.05, tokens=30)
    install(world, [provider])
    with reconnect(world) as first:
        hello_with(first)
        submit(first)
        head = read_deltas(first, 2)
    with reconnect(world) as second:
        ready = hello_with(second)  # a client that does not ask for a replay
        tail = until(second, *TERMINAL)
    assert ready["payload"]["resume_seq"] >= head[-1]["seq"]
    assert tail[0]["seq"] > ready["payload"]["resume_seq"]  # only events after it was attached
    assert kinds(tail)[-1] == "answer.done"


def test_the_ready_message_advertises_the_resume_limits(
    make_world: Callable[..., World],
) -> None:
    world = make_world(WS_RESUME_GRACE_SECONDS="12", WS_RESUME_BUFFER_EVENTS="321")
    with reconnect(world) as ws:
        ready = hello_with(ws)
    limits = ready["payload"]["limits"]
    assert (limits["resume_grace_seconds"], limits["resume_buffer_events"]) == (12, 321)
    assert ready["payload"]["resume_seq"] == 0  # a brand-new channel


# ----------------------------------------------------------------------------- unavailable resumes
def test_a_resume_point_ahead_of_the_channel_is_unavailable_and_live_still_works(
    make_world: Callable[..., World],
) -> None:
    world = make_world()
    install(world, [RecordingProvider()])
    with reconnect(world) as first:
        hello_with(first)
        submit(first)
        last = until(first, *TERMINAL)[-1]["seq"]
    with reconnect(world) as second:
        hello_with(second, resume_from_seq=last + 5)
        error = recv(second)
        assert error["type"] == "error" and error["seq"] == 0
        assert error["payload"] == {
            "code": "resume_unavailable",
            "message": proto.ERROR_MESSAGES["resume_unavailable"],
            "fatal": False,
        }
        assert_pong(second)


def test_events_dropped_from_the_buffer_make_a_resume_unavailable_but_the_database_is_whole(
    make_world: Callable[..., World],
) -> None:
    world = make_world(WS_RESUME_BUFFER_EVENTS="10")
    provider = SlowTokensProvider(token_delay=0.01, tokens=40)
    limits = install(world, [provider])
    with reconnect(world) as first:
        hello_with(first)
        submit(first)
        created = until(first, "question.created")
    assert wait_for(lambda: provider.finished)
    assert idle(limits, world.alice_id)
    with reconnect(world) as second:
        hello_with(second, resume_from_seq=created[-1]["seq"])
        assert recv(second)["payload"]["code"] == "resume_unavailable"
        assert_pong(second)  # nothing replayed
    (answer,) = answers(world.run_sql)  # the stored answer is complete regardless of delivery
    assert answer["status"] == "complete" and "word39" in answer["text"]


def test_after_the_grace_expires_the_channel_is_gone_and_numbering_restarts(
    make_world: Callable[..., World],
) -> None:
    world = make_world(WS_RESUME_GRACE_SECONDS="1")
    install(world, [RecordingProvider()])
    with reconnect(world) as first:
        hello_with(first)
        submit(first)
        until(first, *TERMINAL)
    assert wait_for(lambda: len(channels(world)) == 0, timeout=6)
    with reconnect(world) as second:
        ready = hello_with(second, resume_from_seq=3)
        assert ready["payload"]["resume_seq"] == 0
        assert recv(second)["payload"]["code"] == "resume_unavailable"
        submit(second, "A new question")
        events = until(second, *TERMINAL)
        assert events[0]["seq"] == 1  # a new channel restarts at 1; the client resyncs via GET
        assert kinds(events)[-1] == "answer.done"


# ----------------------------------------------------------------------------- grace and expiry
def test_the_grace_expiry_cancels_a_detached_generation_and_stores_the_partial_answer(
    make_world: Callable[..., World],
) -> None:
    world = make_world(WS_RESUME_GRACE_SECONDS="1")
    provider = SlowTokensProvider(token_delay=0.05, tokens=400)
    limits = install(world, [provider])
    with reconnect(world) as ws:
        hello_with(ws)
        submit(ws)
        read_deltas(ws, 2)
    assert idle(limits, world.alice_id)  # released after the grace, not at once
    assert wait_for(lambda: provider.closed and not provider.finished)
    assert wait_for(lambda: len(channels(world)) == 0, timeout=6)
    (answer,) = answers(world.run_sql)
    assert answer["status"] == "cancelled" and answer["text"] != ""
    assert provider.emitted < provider.tokens


def test_the_generation_survives_while_nobody_is_attached_inside_the_grace(
    make_world: Callable[..., World],
) -> None:
    world = make_world(WS_RESUME_GRACE_SECONDS="30")
    provider = SlowTokensProvider(token_delay=0.05, tokens=40)
    limits = install(world, [provider])
    with reconnect(world) as ws:
        hello_with(ws)
        submit(ws)
        read_deltas(ws, 1)
    assert wait_for(lambda: provider.emitted >= 15, timeout=6)  # it kept going, detached
    assert limits.active(world.alice_id) == 1  # and it still holds the user's slot
    assert wait_for(lambda: provider.finished, timeout=15)
    assert idle(limits, world.alice_id)
    assert answers(world.run_sql)[0]["status"] == "complete"


def test_reconnecting_inside_the_grace_prevents_the_expiry(
    make_world: Callable[..., World],
) -> None:
    world = make_world(WS_RESUME_GRACE_SECONDS="1")
    provider = SlowTokensProvider(token_delay=0.05, tokens=50)
    install(world, [provider])
    with reconnect(world) as first:
        hello_with(first)
        submit(first)
        head = read_deltas(first, 2)
    threading.Event().wait(0.5)
    with reconnect(world) as second:  # back before the 1 s deadline
        hello_with(second, resume_from_seq=head[-1]["seq"])
        tail = until(second, *TERMINAL)  # the answer takes ~2.5 s: far longer than the grace
    assert kinds(tail)[-1] == "answer.done" and provider.finished
    assert_consecutive(head + tail)


# ----------------------------------------------------------------------- one channel, many sockets
def test_a_second_socket_sees_the_running_generation_and_can_cancel_it(
    make_world: Callable[..., World],
) -> None:
    world = make_world()
    provider = SlowTokensProvider(token_delay=0.05, tokens=400)
    limits = install(world, [provider])
    with reconnect(world) as first:
        hello_with(first)
        submit(first)
        head = until(first, "answer.delta")
        question_id = head[0]["payload"]["question_id"]
        with reconnect(world) as second:  # supersedes the first socket
            hello_with(second, resume_from_seq=head[0]["seq"])
            submit(second, "Another question")  # from ANY socket of the channel
            error = next(m for m in until(second, "error") if m["type"] == "error")
            assert error["payload"]["code"] == "generation_in_progress"
            cancel(second, question_id)
            events = until(second, "answer.cancelled")
            assert kinds(events)[-1] == "answer.cancelled"
    assert idle(limits, world.alice_id)
    assert [a["status"] for a in answers(world.run_sql)] == ["cancelled"]
    assert len(questions(world.run_sql)) == 1


def test_superseding_a_socket_does_not_cancel_the_generation(
    make_world: Callable[..., World],
) -> None:
    world = make_world()
    provider = SlowTokensProvider(token_delay=0.05, tokens=30)
    install(world, [provider])
    with reconnect(world) as old:
        hello_with(old)
        submit(old)
        head = read_deltas(old, 2)
        with reconnect(world) as new:
            hello_with(new, resume_from_seq=head[-1]["seq"])
            with pytest.raises(WebSocketDisconnect) as closed:
                while True:
                    recv(old)
            assert closed.value.code == proto.CLOSE_SUPERSEDED
            tail = until(new, *TERMINAL)
    assert kinds(tail)[-1] == "answer.done" and provider.finished


# ----------------------------------------------------------------------------- isolation
def test_one_users_channel_can_never_be_resumed_by_another(
    make_world: Callable[..., World],
) -> None:
    world = make_world()
    install(world, [RecordingProvider(reply_fn=lambda m: "ALICE-SECRET-ANSWER words here")])
    with reconnect(world, "alice") as alice:
        hello_with(alice)
        submit(alice, "ALICE-SECRET-QUESTION")
        done = until(alice, *TERMINAL)
    last = done[-1]["seq"]
    with reconnect(world, "bob") as bob:  # Bob asks for "the events after seq 1"
        hello_with(bob, resume_from_seq=1)
        assert recv(bob)["payload"]["code"] == "resume_unavailable"
        assert_pong(bob)
    with reconnect(world, "alice", world.alice_session_2) as other_session:
        hello_with(other_session, resume_from_seq=1)  # a different session: a different channel
        assert recv(other_session)["payload"]["code"] == "resume_unavailable"
    with reconnect(world, "alice") as again:  # Alice's own channel is intact
        hello_with(again, resume_from_seq=1)
        replay = until(again, *TERMINAL)
    assert replay[-1]["seq"] == last and "ALICE-SECRET" in json.dumps(replay)


def test_a_disabled_user_cannot_reattach_and_the_detached_answer_still_expires(
    make_world: Callable[..., World],
) -> None:
    world = make_world(WS_RESUME_GRACE_SECONDS="1")
    provider = SlowTokensProvider(token_delay=0.05, tokens=400)
    limits = install(world, [provider])
    with reconnect(world) as first:
        hello_with(first)
        submit(first)
        read_deltas(first, 2)
    ticket = world.ticket()  # obtained while the account was still active
    world.run_sql("UPDATE users SET status = 'disabled' WHERE id = :i", i=world.alice_id)
    with pytest.raises(WebSocketDisconnect) as refused:
        with world.client.websocket_connect(
            f"/api/v1/ws/sessions/{world.alice_session}?ticket={ticket}"
        ):
            pass
    assert refused.value.code == 1008  # the same bare refusal as any other handshake failure
    assert idle(limits, world.alice_id)  # the grace expired and the generation was cancelled
    assert answers(world.run_sql)[0]["status"] == "cancelled"


# ----------------------------------------------------------------------------- resource limits
def test_detached_channels_are_capped_and_the_oldest_generation_is_cancelled(
    make_world: Callable[..., World],
) -> None:
    world = make_world(
        WS_MAX_TOTAL_CONNECTIONS="2", WS_MAX_CONNECTIONS_PER_USER="2", WS_RESUME_GRACE_SECONDS="30"
    )
    provider = SlowTokensProvider(token_delay=0.05, tokens=400)
    install(world, [provider])
    started = []
    for who, session in (
        ("alice", world.alice_session),
        ("alice", world.alice_session_2),
        ("bob", world.bob_session),
    ):
        with reconnect(world, who, session) as ws:
            hello_with(ws)
            submit(ws, f"Question from {who}")
            read_deltas(ws, 1)
            started.append(who)
    assert started == ["alice", "alice", "bob"]
    # three detached channels but a cap of two: the oldest (Alice's first session) was cancelled
    assert wait_for(lambda: channels(world).detached_count() == 2, timeout=6)
    assert wait_for(
        lambda: [a["status"] for a in answers(world.run_sql)].count("cancelled") == 1, timeout=8
    )
    assert provider.calls == 3


def test_detached_state_does_not_use_up_connection_capacity(
    make_world: Callable[..., World],
) -> None:
    world = make_world(WS_MAX_CONNECTIONS_PER_USER="1", WS_MAX_TOTAL_CONNECTIONS="1")
    install(world, [SlowTokensProvider(token_delay=0.05, tokens=400)])
    with reconnect(world) as first:
        hello_with(first)
        submit(first)
        read_deltas(first, 1)
    assert wait_for(lambda: world.registry_size() == 0)
    with reconnect(world) as second:  # the only allowed connection is free again
        hello_with(second, resume_from_seq=1)
        until(second, "answer.delta")  # the replay of the still-running answer


# ------------------------------------------------------------ cancel versus the final commit
def test_a_cancel_that_loses_the_race_with_the_final_commit_reports_the_completed_answer(
    make_world: Callable[..., World], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The database is the source of truth: the answer is `complete` in the row, so the client is
    told `answer.done` (rebuilt from the row) and never a false `answer.cancelled`."""
    world = make_world()
    provider = RecordingProvider()
    limits = install(world, [provider])
    committed = threading.Event()
    original = QuestionAnswerer._finish

    async def finish_then_linger(
        self: Any, user_id: Any, prepared: Any, result: Any, **kw: Any
    ) -> Any:
        outcome = await original(self, user_id, prepared, result, **kw)
        if result.status == "complete":
            committed.set()  # the final commit is in the database ...
            await asyncio.sleep(2.0)  # ... and the cancel arrives before `ask` knows it finished
        return outcome

    monkeypatch.setattr(QuestionAnswerer, "_finish", finish_then_linger)
    with reconnect(world) as ws:
        hello_with(ws)
        submit(ws)
        created = until(ws, "question.created")[0]
        assert committed.wait(10)
        cancel(ws, created["payload"]["question_id"])
        events = until(ws, *TERMINAL)
        assert kinds(events)[-1] == "answer.done"
        assert "answer.cancelled" not in kinds(events)
        done = events[-1]["payload"]
        (row,) = answers(world.run_sql)
        assert row["status"] == "complete"  # never downgraded
        assert (done["provider"], done["model"]) == (row["provider"], row["model"])
        assert done["metrics"] == {
            "retrieval_ms": row["retrieval_ms"],
            "first_token_ms": row["first_token_ms"],
            "total_ms": row["total_ms"],
        }
        assert (done["input_tokens"], done["output_tokens"]) == (
            row["input_tokens"],
            row["output_tokens"],
        )
        assert streamed_text(events) == row["text"]  # the deltas already delivered match the row
        assert_pong(ws)
    assert idle(limits, world.alice_id)
    assert kinds(events).count("answer.done") == 1


def test_the_stored_row_decides_what_a_cancel_reports(
    make_world: Callable[..., World], monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the row is `error` when the cancel interrupts, the client gets the stored error."""
    world = make_world()
    install(world, [SlowTokensProvider(token_delay=0.05, tokens=400)])
    real = repository.get_answer

    async def as_error(db: Any, user_id: Any, session_id: Any, answer_id: Any) -> Any:
        row = await real(db, user_id, session_id, answer_id)
        if row is not None:
            row.status, row.error_code = "error", "llm_timeout"
        return row

    monkeypatch.setattr(repository, "get_answer", as_error)
    with reconnect(world) as ws:
        hello_with(ws)
        submit(ws)
        head = until(ws, "answer.delta")
        cancel(ws, head[0]["payload"]["question_id"])
        end = until(ws, *TERMINAL)[-1]
    assert end["type"] == "answer.error" and end["payload"]["code"] == "llm_timeout"


# ----------------------------------------------------------------------------- logging
def test_no_question_answer_or_ticket_text_is_logged_across_a_resume(
    make_world: Callable[..., World], request: pytest.FixtureRequest
) -> None:
    captured = request.getfixturevalue("logs")
    world = make_world(WS_RESUME_GRACE_SECONDS="1")
    install(world, [SlowTokensProvider(token_delay=0.05, tokens=200)])
    with reconnect(world) as first:
        hello_with(first)
        submit(first, "CANARY-QUESTION-5531 please")
        read_deltas(first, 2)
    with reconnect(world) as second:
        hello_with(second, resume_from_seq=1)
        read_deltas(second, 2)
    assert wait_for(lambda: len(channels(world)) == 0, timeout=8)  # expiry path logged too
    text = "\n".join(captured.lines)
    assert "CANARY" not in text and "word1 " not in text and "ticket=" not in text
    assert uuid.UUID(int=0).hex not in text


# ----------------------------------------------------------------------------- shutdown
def test_the_server_shutdown_cancels_detached_generations(client: Any) -> None:
    """The real application lifespan: stopping the server cancels every generation (their answers
    are stored `cancelled` by `ask`) and empties the registry, so nothing outlives the process."""
    app = create_app()
    generation = FakeGeneration()

    async def scenario() -> None:
        async with app.router.lifespan_context(app):
            registry = app.state.session_channels
            channel = registry.get_or_create((uuid.uuid4(), uuid.uuid4()))
            channel.generation = generation  # type: ignore[assignment]
            channel.emit(lambda seq: adapter.to_message(AnswerDelta(uuid.uuid4(), 0, "x"), seq))
            assert len(registry) == 1 and generation.active
        assert generation.closed == 1 and not generation.active
        assert len(registry) == 0

    asyncio.run(scenario())
