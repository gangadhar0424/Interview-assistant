"""Protocol models, parsing and parity with the SSE events (Phase 9A). No database needed."""

import json
import re
import uuid
from pathlib import Path
from typing import Any

import pytest

from app.qa.events import (
    AnswerDelta,
    AnswerDone,
    AnswerFailed,
    AnswerStarted,
    QuestionCreated,
    RetrievalDone,
)
from app.qa.sse import event_to_frame
from app.realtime import schemas as proto

Q = uuid.UUID("11111111-1111-4111-8111-111111111111")
A = uuid.UUID("22222222-2222-4222-8222-222222222222")


def parse(obj: object) -> object:
    return proto.parse_client_frame(json.dumps(obj))


def violation_code(text: str) -> str:
    with pytest.raises(proto.ProtocolViolation) as caught:
        proto.parse_client_frame(text)
    return caught.value.code


def hello(**payload: object) -> dict[str, object]:
    return {"type": "client.hello", "payload": {"protocol_version": 1, **payload}}


# ---------------------------------------------------------------- client frames
def test_each_client_event_parses() -> None:
    full = hello(client_version="1.0", platform="win", capabilities=["a"])
    assert isinstance(parse(full), proto.ClientHello)
    assert isinstance(parse({"type": "ping", "payload": {}}), proto.Ping)
    submit = {"type": "question.submit", "payload": {"text": "Why?"}}
    assert isinstance(parse(submit), proto.QuestionSubmit)
    cancel = {"type": "answer.cancel", "payload": {"question_id": str(Q)}}
    assert isinstance(parse(cancel), proto.AnswerCancel)


def test_resume_from_seq_is_the_reconnect_field() -> None:
    message = parse(hello(resume_from_seq=7))
    assert isinstance(message, proto.ClientHello)
    assert message.payload.resume_from_seq == 7
    assert violation_code(json.dumps(hello(resume_from_seq=-1))) == proto.ERR_INVALID_MESSAGE
    assert violation_code(json.dumps(hello(last_event_id="x"))) == proto.ERR_INVALID_MESSAGE


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "[]",
        "42",
        "null",
        '"ping"',
        "{}",
        '{"type": 5, "payload": {}}',
        '{"type": "ping"}',  # payload is required
        '{"type": "ping", "payload": {"x": 1}}',  # unknown payload field
        '{"type": "ping", "payload": {}, "seq": 1}',  # server-only envelope field
        '{"type": "ping", "payload": {}, "user_id": "x"}',  # no identity from the client
        '{"type": "question.submit", "payload": {"text": ""}}',
        '{"type": "question.submit", "payload": {"text": "x", "user_id": "u"}}',
        '{"type": "question.submit", "payload": {"text": "x", "session_id": "s"}}',
        '{"type": "answer.cancel", "payload": {"question_id": "nope"}}',
        '{"type": "client.hello", "payload": {"protocol_version": "1"}}',
        '{"type": "ping", "payload": {}, "id": "' + "x" * 65 + '"}',
        "[" * 5000,  # a nesting bomb must not raise RecursionError out of the parser
    ],
)
def test_invalid_frames_are_rejected_with_a_fixed_code(text: str) -> None:
    assert violation_code(text) == proto.ERR_INVALID_MESSAGE


@pytest.mark.parametrize(
    "kind",
    [
        "audio.start",
        "audio.stop",
        "answer.regenerate",
        "prefs.update",
        "question.dismiss",
        "PING",
        "",
    ],
)
def test_unknown_types_get_their_own_code(kind: str) -> None:
    assert violation_code(json.dumps({"type": kind, "payload": {}})) == proto.ERR_UNKNOWN_TYPE


def test_violations_never_echo_the_input() -> None:
    secret = "SECRET-CANARY-123"  # noqa: S105
    for text in (f'{{"type": "{secret}"}}', f'{{"type": "ping", "payload": {{"{secret}": 1}}}}'):
        with pytest.raises(proto.ProtocolViolation) as caught:
            proto.parse_client_frame(text)
        assert secret not in str(caught.value)
        assert secret not in proto.ERROR_MESSAGES[caught.value.code]


# ---------------------------------------------------------------- server messages
def test_session_ready_has_the_documented_shape() -> None:
    msg = proto.SessionReady(
        id=proto.new_message_id(),
        seq=0,
        ts=proto.now_ms(),
        payload=proto.SessionReadyPayload(
            session_id=Q,
            server_time=1,
            resume_seq=0,
            protocol_version=1,
            limits=proto.SessionLimits(
                max_frame_bytes=16384,
                heartbeat_interval_seconds=20,
                idle_timeout_seconds=45,
                max_messages_per_second=10,
                resume_grace_seconds=30,
                resume_buffer_events=2000,
            ),
        ),
    )
    data = json.loads(proto.encode(msg))
    assert set(data) == {"type", "id", "seq", "ts", "payload"}
    assert data["type"] == "session.ready" and data["seq"] == 0
    assert set(data["payload"]) == {
        "session_id",
        "server_time",
        "resume_seq",
        "protocol_version",
        "limits",
    }
    assert "audio_params" not in data["payload"]
    assert proto.server_adapter.validate_python(data).type == "session.ready"


def test_error_events_use_fixed_messages_and_the_fatal_flag() -> None:
    for code in proto.ERROR_MESSAGES:
        event = proto.error_event(code, fatal=True)
        assert event.payload.message == proto.ERROR_MESSAGES[code]
        assert event.payload.fatal is True and event.seq == 0


def test_close_codes_are_unique_and_in_range() -> None:
    values = list(proto.CLOSE_CODES.values())
    assert len(values) == len(set(values))
    assert all(v < 1100 or 4000 <= v <= 4999 for v in values)


def _sse_payload(event: Any) -> dict[str, Any]:
    frame = event_to_frame(event)
    data_line = frame.decode().split("\n")[1]
    assert data_line.startswith("data: ")
    payload: dict[str, Any] = json.loads(data_line[len("data: ") :])
    return payload


def test_ws_payload_models_accept_exactly_the_sse_payloads() -> None:
    """One core, two transports: the WS payloads are the SSE payloads (owner decision 2)."""
    cases: list[tuple[Any, Any]] = [
        (QuestionCreated(Q, A), proto.QuestionCreatedPayload),
        (RetrievalDone(Q, 3, 41), proto.RetrievalDonePayload),
        (AnswerStarted(A, Q, "fake", "fake-echo"), proto.AnswerStartedPayload),
        (AnswerDelta(A, 2, "x"), proto.AnswerDeltaPayload),
        (AnswerDone(A, Q, "fake", "m", 1, 2, 3, True, 4, None), proto.AnswerDonePayload),
        (AnswerFailed(A, Q, "llm_timeout", "m", True), proto.AnswerErrorPayload),
    ]
    for event, model in cases:
        payload = _sse_payload(event)
        parsed = model.model_validate(payload)  # extra="forbid": no missing or additional field
        assert json.loads(parsed.model_dump_json()) == payload


def test_answer_done_over_ws_carries_the_same_fields_as_sse() -> None:
    sse = _sse_payload(AnswerDone(A, Q, "fake", "m", 1, 2, 3, False, None, None))
    assert set(sse) == set(proto.AnswerDonePayload.model_fields)


def test_9b_close_codes_and_error_codes_are_defined_with_fixed_messages() -> None:
    assert proto.CLOSE_CODES["slow_consumer"] == 4010 and proto.CLOSE_CODES["policy"] == 1008
    for code in (
        proto.ERR_GENERATION_IN_PROGRESS,
        proto.ERR_NOTHING_TO_CANCEL,
        proto.ERR_INTERNAL,
        proto.ERR_ACCOUNT_INACTIVE,
        *proto.PRE_STREAM_ERRORS,
    ):
        assert proto.ERROR_MESSAGES[code]  # nothing is ever built from a request


def test_every_refusal_the_question_core_can_raise_has_a_websocket_code() -> None:
    """A new `AppError` code in the Q&A core must also be added to the protocol, otherwise the
    socket would report it as a generic `internal_error` instead of the code REST returns."""
    root = Path(__file__).resolve().parents[1] / "app"
    raised: set[str] = set()
    for relative in ("qa/service.py", "qa/limits.py"):
        source = (root / relative).read_text(encoding="utf-8")
        raised |= set(re.findall(r'AppError\(\s*\d+,\s*"([a-z_]+)"', source))
    # `session_not_found` is raised through `app.sessions.service.session_not_found()`
    assert raised | {"session_not_found"} == proto.PRE_STREAM_ERRORS
