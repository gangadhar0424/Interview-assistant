"""WebSocket route `/api/v1/ws/sessions/{session_id}?ticket=...` (Phase 9A, D-41).

9A: handshake authentication, `session.ready`, `client.hello`, ping/pong, idle/hello timeouts,
frame and message-rate limits, close codes, one socket per (user, session), connection caps.
9B (D-42): typed Q&A (`question.submit`), `answer.cancel`, and backpressure.
9C (D-43): the answer belongs to the (user, session) `SessionChannel`, not to the socket. The
connection's `Channel` is its single writer (control queue + a cursor into the channel's retained
buffer). A socket attaches at `client.hello` (replaying from `resume_from_seq`) and detaches when it
ends; the generation keeps running for the grace period so a reconnect can resume it.

Handshake refusals happen BEFORE the socket is accepted and are all the same bare refusal (the ASGI
close before accept becomes HTTP 403): bad Origin, missing/unknown/expired/used ticket, a ticket for
another session, a malformed session id, a user that is not active, a session that is not theirs.
The ticket is never logged; the log carries a fixed reason code only.
"""

import asyncio
import logging
import uuid
from typing import Any

from fastapi import APIRouter, WebSocket
from starlette.websockets import WebSocketDisconnect

from app.core.config import Settings, get_settings
from app.core.metrics import websocket_disconnects
from app.db.models import User
from app.db.session import set_tenant_context
from app.qa.limits import QuestionLimits
from app.qa.service import QuestionAnswerer
from app.realtime import schemas as proto
from app.realtime.channel import Channel
from app.realtime.generation import Generation
from app.realtime.limits import Connection, ConnectionRegistry, MessageRateLimiter
from app.realtime.session_channel import SessionChannel, SessionChannels
from app.realtime.tickets import TicketStore, TicketStoreUnavailable
from app.sessions import repository as sessions_repository

logger = logging.getLogger("app.realtime")

router = APIRouter(prefix="/ws", tags=["realtime"])

REFUSE_CODE = 1008  # closing before accept: the client sees HTTP 403, nothing else
MAX_VIOLATIONS = 5  # invalid/unknown messages tolerated per connection before it is closed


async def _refuse(websocket: WebSocket, reason: str) -> None:
    logger.info("ws refused", extra={"reason": reason})  # fixed code only; never the ticket
    await websocket.close(code=REFUSE_CODE)


async def _authenticate(
    websocket: WebSocket, settings: Settings, raw_session_id: str
) -> tuple[uuid.UUID, uuid.UUID] | None:
    """Return (user_id, session_id) or refuse the handshake and return None."""
    origin = websocket.headers.get("origin")
    if origin is not None and origin not in settings.cors_origin_list:
        await _refuse(websocket, "origin")
        return None
    ticket = websocket.query_params.get("ticket")
    if not ticket:
        await _refuse(websocket, "ticket_missing")
        return None
    try:
        session_id = uuid.UUID(raw_session_id)
    except ValueError:
        await _refuse(websocket, "bad_session_id")
        return None

    store: TicketStore = websocket.app.state.ticket_store
    try:
        binding = await store.redeem(ticket)
    except TicketStoreUnavailable:
        logger.warning("ws ticket store unavailable")
        await _refuse(websocket, "ticket_store_unavailable")
        return None
    if binding is None:
        await _refuse(websocket, "ticket_invalid")
        return None
    # Checked before any database access, so a valid ticket cannot be used to probe other sessions.
    if binding.session_id != session_id:
        await _refuse(websocket, "ticket_session_mismatch")
        return None

    async with websocket.app.state.sessionmaker() as db:
        await set_tenant_context(db, binding.user_id)
        user = await db.get(User, binding.user_id)
        if user is None or user.status != "active":
            await _refuse(websocket, "user_inactive")
            return None
        owned = await sessions_repository.get_for_user(db, binding.user_id, session_id)
        if owned is None:
            await _refuse(websocket, "session_not_owned")
            return None
    return binding.user_id, session_id


async def _send(websocket: WebSocket, message: Any) -> bool:
    try:
        await websocket.send_text(proto.encode(message))
    except (RuntimeError, WebSocketDisconnect, OSError):
        return False
    return True


CLOSE_TIMEOUT_SECONDS = 2.0  # a close on a stuck peer must not hang the connection task


async def _close(websocket: WebSocket, code: int) -> None:
    try:
        await asyncio.wait_for(websocket.close(code=code), CLOSE_TIMEOUT_SECONDS)
    except (RuntimeError, WebSocketDisconnect, OSError, TimeoutError):
        pass  # the peer is already gone or not reading


@router.websocket("/sessions/{session_id}")
async def session_socket(websocket: WebSocket, session_id: str) -> None:
    settings = get_settings()
    identity = await _authenticate(websocket, settings, session_id)
    if identity is None:
        return
    user_id, sid = identity

    registry: ConnectionRegistry = websocket.app.state.connection_registry
    conn = Connection(user_id, sid)
    await websocket.accept()
    admission = registry.admit(conn)
    if not admission.admitted:
        await _send(websocket, proto.error_event(proto.ERR_TOO_MANY_CONNECTIONS, fatal=True))
        await _close(websocket, proto.CLOSE_TOO_MANY_CONNECTIONS)
        logger.info("ws capped", extra={"user_id": str(user_id), "session_id": str(sid)})
        return
    if admission.superseded is not None:
        admission.superseded.request_close(proto.CLOSE_SUPERSEDED)

    loop = asyncio.get_running_loop()
    started = loop.time()
    logger.info("ws connected", extra={"user_id": str(user_id), "session_id": str(sid)})
    code = proto.CLOSE_NORMAL
    try:
        code = await _serve(websocket, conn, settings)
    finally:
        registry.release(conn)
        websocket_disconnects.labels("normal" if code == proto.CLOSE_NORMAL else "other").inc()
        logger.info(
            "ws closed",
            extra={
                "user_id": str(user_id),
                "session_id": str(sid),
                "close_code": code,
                "duration_ms": int((loop.time() - started) * 1000),
            },
        )


async def _serve(websocket: WebSocket, conn: Connection, settings: Settings) -> int:
    """Run one accepted connection to its end; returns the close code that was used."""
    loop = asyncio.get_running_loop()
    limiter = MessageRateLimiter(settings.ws_max_messages_per_second)
    channel = Channel(
        websocket,
        max_control=settings.ws_send_queue_events,
        send_timeout=settings.ws_send_timeout_seconds,
    )
    channel.start()
    channels: SessionChannels = websocket.app.state.session_channels
    key = (conn.user_id, conn.session_id)
    channel.send(
        proto.SessionReady(
            id=proto.new_message_id(),
            seq=0,
            ts=proto.now_ms(),
            payload=proto.SessionReadyPayload(
                session_id=conn.session_id,
                server_time=proto.now_ms(),
                resume_seq=channels.last_seq(key),  # the channel's last sequenced event
                protocol_version=proto.PROTOCOL_VERSION,
                limits=proto.SessionLimits(
                    max_frame_bytes=settings.ws_max_frame_bytes,
                    heartbeat_interval_seconds=settings.ws_heartbeat_interval_seconds,
                    idle_timeout_seconds=settings.ws_idle_timeout_seconds,
                    max_messages_per_second=settings.ws_max_messages_per_second,
                    resume_grace_seconds=settings.ws_resume_grace_seconds,
                    resume_buffer_events=settings.ws_resume_buffer_events,
                ),
            ),
        )
    )

    app_state = websocket.app.state
    answerer: QuestionAnswerer = app_state.question_answerer
    limits: QuestionLimits = app_state.question_limits
    flush_seconds = min(settings.ws_send_timeout_seconds, CLOSE_TIMEOUT_SECONDS)

    hello_done = False
    violations = 0
    last_activity = loop.time()
    hello_deadline = last_activity + settings.ws_hello_timeout_seconds
    closing: asyncio.Task[Any] = asyncio.create_task(conn.closing.wait())
    failed: asyncio.Task[Any] = asyncio.create_task(channel.failed.wait())
    receiver: asyncio.Task[Any] | None = None
    session: SessionChannel | None = None  # attached at hello

    async def end(close_code: int) -> int:
        """Deliver what is already queued (errors, a final event), then close."""
        await channel.drain(flush_seconds)
        await _close(websocket, close_code)
        return close_code

    async def fatal(error_code: str, close_code: int) -> int:
        channel.send(proto.error_event(error_code, fatal=True))
        return await end(close_code)

    try:
        while True:
            if receiver is None:
                receiver = asyncio.create_task(websocket.receive())
            deadline = last_activity + settings.ws_idle_timeout_seconds
            if not hello_done:
                deadline = min(deadline, hello_deadline)
            done, _ = await asyncio.wait(
                {receiver, closing, failed},
                timeout=max(0.0, deadline - loop.time()),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if failed in done:
                if channel.failure == "slow":  # the client does not read: nothing more is sent
                    logger.info(
                        "ws slow consumer",
                        extra={"user_id": str(conn.user_id), "session_id": str(conn.session_id)},
                    )
                    await _close(websocket, proto.CLOSE_SLOW_CONSUMER)
                    return proto.CLOSE_SLOW_CONSUMER
                return proto.CLOSE_NORMAL  # the peer is gone
            if closing in done:  # superseded by a newer connection, or closed by the server
                return await end(conn.close_code or proto.CLOSE_NORMAL)
            if not done:
                if not hello_done and loop.time() >= hello_deadline:
                    return await fatal(proto.ERR_PROTOCOL, proto.CLOSE_PROTOCOL_ERROR)
                await _close(websocket, proto.CLOSE_IDLE_TIMEOUT)
                return proto.CLOSE_IDLE_TIMEOUT

            try:
                frame = receiver.result()
            except Exception:  # noqa: BLE001 - the socket is gone; nothing to report to the peer
                return proto.CLOSE_NORMAL
            receiver = None
            if frame["type"] == "websocket.disconnect":
                return int(frame.get("code") or proto.CLOSE_NORMAL)
            last_activity = loop.time()

            if frame.get("bytes") is not None:  # binary: audio does not exist in Phase 9
                await _close(websocket, proto.CLOSE_UNSUPPORTED_DATA)
                return proto.CLOSE_UNSUPPORTED_DATA
            text = frame.get("text")
            if text is None:
                continue
            if len(text) > settings.ws_max_frame_bytes or (
                len(text.encode("utf-8")) > settings.ws_max_frame_bytes
            ):
                await _close(websocket, proto.CLOSE_TOO_BIG)
                return proto.CLOSE_TOO_BIG
            if not limiter.allow():
                await _close(websocket, proto.CLOSE_RATE_LIMITED)
                return proto.CLOSE_RATE_LIMITED

            try:
                message = proto.parse_client_frame(text)
            except proto.ProtocolViolation as violation:
                if not hello_done:
                    return await fatal(proto.ERR_PROTOCOL, proto.CLOSE_PROTOCOL_ERROR)
                violations += 1
                if violations >= MAX_VIOLATIONS:
                    return await fatal(proto.ERR_PROTOCOL, proto.CLOSE_PROTOCOL_ERROR)
                channel.send(proto.error_event(violation.code, fatal=False))
                continue

            if not hello_done:
                if not isinstance(message, proto.ClientHello):
                    return await fatal(proto.ERR_PROTOCOL, proto.CLOSE_PROTOCOL_ERROR)
                if message.payload.protocol_version != proto.PROTOCOL_VERSION:
                    return await fatal(
                        proto.ERR_UNSUPPORTED_VERSION, proto.CLOSE_UNSUPPORTED_VERSION
                    )
                hello_done = True
                session = channels.get_or_create(key)
                # One synchronous step: choose the replay point and subscribe, so replayed events
                # precede live ones. Unavailable (expired, evicted, ahead of the channel) is
                # non-fatal and replays nothing; the client resyncs through the history endpoint.
                if session.attach(channel, message.payload.resume_from_seq):
                    channel.send(proto.error_event(proto.ERR_RESUME_UNAVAILABLE, fatal=False))
                continue

            if isinstance(message, proto.ClientHello):
                return await fatal(proto.ERR_PROTOCOL, proto.CLOSE_PROTOCOL_ERROR)
            if isinstance(message, proto.Ping):
                channel.send(
                    proto.Pong(
                        id=proto.new_message_id(),
                        seq=0,
                        ts=proto.now_ms(),
                        payload=proto.EmptyPayload(),
                    )
                )
                continue
            if session is None:  # unreachable: hello_done implies an attached session
                return await fatal(proto.ERR_PROTOCOL, proto.CLOSE_PROTOCOL_ERROR)
            if isinstance(message, proto.QuestionSubmit):
                if session.generation_active:
                    # any socket of the channel; never cancels the running answer (D-41 decision 6)
                    channel.send(proto.error_event(proto.ERR_GENERATION_IN_PROGRESS, fatal=False))
                    continue
                generation = Generation(
                    user_id=conn.user_id,
                    session_id=conn.session_id,
                    session=session,
                    answerer=answerer,
                    limits=limits,
                    sessionmaker=app_state.sessionmaker,
                    text=message.payload.text,
                    question_type_override=message.payload.question_type_override,
                    on_account_inactive=lambda: conn.request_close(proto.CLOSE_POLICY),
                )
                session.generation = generation
                generation.start()
                continue
            # answer.cancel: only the running generation of THIS socket can be cancelled; every
            # other id (unknown, foreign, finished) gets the same answer.
            running = session.generation
            if running is None or not running.cancel(message.payload.question_id):
                channel.send(proto.error_event(proto.ERR_NOTHING_TO_CANCEL, fatal=False))
    finally:
        closing.cancel()
        failed.cancel()
        if receiver is not None and not receiver.done():
            receiver.cancel()
        # The generation is NOT stopped here: it belongs to the channel and keeps running for the
        # grace period (D-43). Detaching starts the grace timer, or drops a channel with no events.
        if session is not None:
            session.detach(channel)
        channel.stop()
