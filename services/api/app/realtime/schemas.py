"""WebSocket protocol (docs/API.md section 8, D-41): envelopes, events and close codes.

These models are the source of truth of the wire contract. `app/realtime/codegen.py` exports them to
`packages/protocol/` (JSON Schema + TypeScript) and a test fails when the committed files drift.

Client messages are strict (unknown fields are rejected). Server messages carry a `seq`: control
events (`session.ready`, `pong`, `error`) use 0 and are never replayed; sequenced Q&A events (9B)
use a per-(user, session) channel counter >= 1. Payloads of the Q&A events are identical to the SSE
events of Phase 8B (one core, two transports).
"""

import json
import time
import uuid
from typing import Annotated, Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, StrictInt, TypeAdapter, ValidationError

PROTOCOL_VERSION = 1

# ----------------------------------------------------------------------------- close codes
CLOSE_NORMAL = 1000
CLOSE_UNSUPPORTED_DATA = 1003  # a binary frame (audio is not part of Phase 9)
CLOSE_POLICY = 1008  # after accept: the account is no longer active (9B)
CLOSE_TOO_BIG = 1009
CLOSE_SUPERSEDED = 4001
CLOSE_IDLE_TIMEOUT = 4002
CLOSE_PROTOCOL_ERROR = 4003
CLOSE_UNSUPPORTED_VERSION = 4004
CLOSE_RATE_LIMITED = 4005
CLOSE_SLOW_CONSUMER = 4010  # the client does not read fast enough (9B backpressure)
CLOSE_TOO_MANY_CONNECTIONS = 4429

CLOSE_CODES: dict[str, int] = {
    "normal": CLOSE_NORMAL,
    "unsupported_data": CLOSE_UNSUPPORTED_DATA,
    "policy": CLOSE_POLICY,
    "too_big": CLOSE_TOO_BIG,
    "superseded": CLOSE_SUPERSEDED,
    "idle_timeout": CLOSE_IDLE_TIMEOUT,
    "protocol_error": CLOSE_PROTOCOL_ERROR,
    "unsupported_version": CLOSE_UNSUPPORTED_VERSION,
    "rate_limited": CLOSE_RATE_LIMITED,
    "slow_consumer": CLOSE_SLOW_CONSUMER,
    "too_many_connections": CLOSE_TOO_MANY_CONNECTIONS,
}

# ----------------------------------------------------------------------------- error codes
ERR_INVALID_MESSAGE = "invalid_message"
ERR_UNKNOWN_TYPE = "unknown_message_type"
ERR_PROTOCOL = "protocol_error"
ERR_UNSUPPORTED_VERSION = "unsupported_protocol_version"
ERR_RESUME_UNAVAILABLE = "resume_unavailable"
ERR_NOT_AVAILABLE = "not_available"
ERR_TOO_MANY_CONNECTIONS = "too_many_connections"
# 9B: Q&A over the socket. The pre-stream refusals carry the same codes as the REST endpoint.
ERR_GENERATION_IN_PROGRESS = "generation_in_progress"
ERR_NOTHING_TO_CANCEL = "nothing_to_cancel"
ERR_INTERNAL = "internal_error"
ERR_ACCOUNT_INACTIVE = "account_inactive"
PRE_STREAM_ERRORS = frozenset(
    {
        "session_not_found",
        "session_not_ready",
        "materials_not_ready",
        "session_preferences_invalid",
        "validation_error",
        "rate_limited",
        "too_many_generations",
    }
)

# Fixed, user-safe texts. Nothing from a request or an exception is ever interpolated.
ERROR_MESSAGES: dict[str, str] = {
    ERR_INVALID_MESSAGE: "The message is not valid.",
    ERR_UNKNOWN_TYPE: "This message type is not supported.",
    ERR_PROTOCOL: "The connection did not follow the protocol.",
    ERR_UNSUPPORTED_VERSION: "This protocol version is not supported.",
    ERR_RESUME_UNAVAILABLE: "The missed events are no longer available. Reload the history.",
    ERR_NOT_AVAILABLE: "This message is not available yet.",
    ERR_TOO_MANY_CONNECTIONS: "Too many open connections. Close another one and retry.",
    ERR_GENERATION_IN_PROGRESS: "An answer is already being generated. Wait for it or cancel it.",
    ERR_NOTHING_TO_CANCEL: "No answer is being generated for that question.",
    ERR_INTERNAL: "Something went wrong. Please try again.",
    ERR_ACCOUNT_INACTIVE: "This account is no longer active.",
    "session_not_found": "Session not found.",
    "session_not_ready": "Questions can only be asked in a ready session.",
    "materials_not_ready": "A selected resume or document is no longer ready. Check your files.",
    "session_preferences_invalid": "This session's preferences need to be updated first.",
    "validation_error": "The question is not valid.",
    "rate_limited": "Too many questions. Please wait a moment before asking again.",
    "too_many_generations": "Too many answers are being generated at once. Wait for one to finish.",
}


class _Strict(BaseModel):
    # Serialized models list defaulted fields (the `type` discriminator) as required, so the
    # generated TypeScript unions can be narrowed on `type`.
    model_config = ConfigDict(extra="forbid", json_schema_serialization_defaults_required=True)


# ============================================================================ client -> server
class EmptyPayload(_Strict):
    pass


class ClientHelloPayload(_Strict):
    protocol_version: StrictInt  # "1" or 1.0 are not the integer 1
    client_version: str | None = Field(default=None, max_length=64)
    platform: str | None = Field(default=None, max_length=64)
    capabilities: list[Annotated[str, Field(max_length=64)]] = Field(
        default_factory=list, max_length=32
    )
    # Last sequenced event the client processed; the reconnect field (D-41).
    resume_from_seq: StrictInt | None = Field(default=None, ge=0)


class QuestionSubmitPayload(_Strict):
    # Same bounds as the REST body (app.qa.schemas.QuestionCreate); the orchestrator enforces the
    # real 2000-character limit after cleaning.
    text: str = Field(min_length=1, max_length=20000)
    question_type_override: str | None = Field(default=None, max_length=64)


class AnswerCancelPayload(_Strict):
    question_id: uuid.UUID


class _ClientEnvelope(_Strict):
    id: str | None = Field(default=None, max_length=64)  # client correlation id


class ClientHello(_ClientEnvelope):
    type: Literal["client.hello"]
    payload: ClientHelloPayload


class Ping(_ClientEnvelope):
    type: Literal["ping"]
    payload: EmptyPayload


class QuestionSubmit(_ClientEnvelope):
    type: Literal["question.submit"]
    payload: QuestionSubmitPayload


class AnswerCancel(_ClientEnvelope):
    type: Literal["answer.cancel"]
    payload: AnswerCancelPayload


ClientMessage = Annotated[
    ClientHello | Ping | QuestionSubmit | AnswerCancel, Field(discriminator="type")
]
CLIENT_MESSAGE_TYPES = frozenset({"client.hello", "ping", "question.submit", "answer.cancel"})
client_adapter: TypeAdapter[Any] = TypeAdapter(ClientMessage)  # type: ignore[arg-type]


# ============================================================================ server -> client
class SessionLimits(_Strict):
    max_frame_bytes: int
    heartbeat_interval_seconds: float
    idle_timeout_seconds: float
    max_messages_per_second: int
    resume_grace_seconds: float
    resume_buffer_events: int


class SessionReadyPayload(_Strict):
    session_id: uuid.UUID
    server_time: int  # epoch milliseconds
    resume_seq: int  # last sequenced seq of the channel (0 until Q&A events exist)
    protocol_version: int
    limits: SessionLimits


class ErrorPayload(_Strict):
    code: str
    message: str
    fatal: bool


class QuestionCreatedPayload(_Strict):
    question_id: uuid.UUID
    answer_id: uuid.UUID


class RetrievalDonePayload(_Strict):
    question_id: uuid.UUID
    chunk_count: int
    ms: int


class AnswerStartedPayload(_Strict):
    answer_id: uuid.UUID
    question_id: uuid.UUID
    provider: str
    model: str


class AnswerDeltaPayload(_Strict):
    answer_id: uuid.UUID
    index: int
    text: str


class AnswerMetrics(_Strict):
    retrieval_ms: int
    first_token_ms: int
    total_ms: int


class AnswerDonePayload(_Strict):
    answer_id: uuid.UUID
    question_id: uuid.UUID
    provider: str
    model: str
    metrics: AnswerMetrics
    fallback_used: bool
    input_tokens: int | None
    output_tokens: int | None


class AnswerErrorPayload(_Strict):
    answer_id: uuid.UUID
    question_id: uuid.UUID
    code: str
    message: str
    retryable: bool


class AnswerCancelledPayload(_Strict):
    answer_id: uuid.UUID
    question_id: uuid.UUID


class _ServerEnvelope(_Strict):
    id: str  # random hex message id
    seq: int = Field(ge=0)
    ts: int  # epoch milliseconds


class SessionReady(_ServerEnvelope):
    type: Literal["session.ready"] = "session.ready"
    payload: SessionReadyPayload


class Pong(_ServerEnvelope):
    type: Literal["pong"] = "pong"
    payload: EmptyPayload


class ErrorEvent(_ServerEnvelope):
    type: Literal["error"] = "error"
    payload: ErrorPayload


class QuestionCreated(_ServerEnvelope):
    type: Literal["question.created"] = "question.created"
    payload: QuestionCreatedPayload


class RetrievalDone(_ServerEnvelope):
    type: Literal["retrieval.done"] = "retrieval.done"
    payload: RetrievalDonePayload


class AnswerStarted(_ServerEnvelope):
    type: Literal["answer.started"] = "answer.started"
    payload: AnswerStartedPayload


class AnswerDelta(_ServerEnvelope):
    type: Literal["answer.delta"] = "answer.delta"
    payload: AnswerDeltaPayload


class AnswerDone(_ServerEnvelope):
    type: Literal["answer.done"] = "answer.done"
    payload: AnswerDonePayload


class AnswerError(_ServerEnvelope):
    type: Literal["answer.error"] = "answer.error"
    payload: AnswerErrorPayload


class AnswerCancelled(_ServerEnvelope):
    type: Literal["answer.cancelled"] = "answer.cancelled"
    payload: AnswerCancelledPayload


ServerMessage = Annotated[
    SessionReady
    | Pong
    | ErrorEvent
    | QuestionCreated
    | RetrievalDone
    | AnswerStarted
    | AnswerDelta
    | AnswerDone
    | AnswerError
    | AnswerCancelled,
    Field(discriminator="type"),
]
server_adapter: TypeAdapter[Any] = TypeAdapter(ServerMessage)  # type: ignore[arg-type]


# ============================================================================ helpers
def now_ms() -> int:
    return int(time.time() * 1000)


def new_message_id() -> str:
    return uuid.uuid4().hex


def encode(message: BaseModel) -> str:
    """The JSON text of one server message."""
    return message.model_dump_json()


def error_event(code: str, *, fatal: bool, seq: int = 0) -> ErrorEvent:
    return ErrorEvent(
        id=new_message_id(),
        seq=seq,
        ts=now_ms(),
        payload=ErrorPayload(code=code, message=ERROR_MESSAGES[code], fatal=fatal),
    )


# ============================================================================ parsing
class ProtocolViolation(Exception):  # noqa: N818 - not an error condition of the program
    """A client frame that cannot be accepted. Carries only a fixed error code, never input."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def parse_client_frame(text: str) -> ClientHello | Ping | QuestionSubmit | AnswerCancel:
    """Parse one client text frame; raise `ProtocolViolation` (fixed code) on any problem."""
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        raise ProtocolViolation(ERR_INVALID_MESSAGE) from None
    if not isinstance(data, dict):
        raise ProtocolViolation(ERR_INVALID_MESSAGE)
    message_type = data.get("type")
    if not isinstance(message_type, str):
        raise ProtocolViolation(ERR_INVALID_MESSAGE)
    if message_type not in CLIENT_MESSAGE_TYPES:
        raise ProtocolViolation(ERR_UNKNOWN_TYPE)
    try:
        return cast(
            ClientHello | Ping | QuestionSubmit | AnswerCancel,
            client_adapter.validate_python(data),
        )
    except ValidationError:
        raise ProtocolViolation(ERR_INVALID_MESSAGE) from None
