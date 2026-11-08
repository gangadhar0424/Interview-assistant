"""The outbound side of ONE WebSocket connection (Phase 9B, reworked in 9C, D-42/D-43).

A `Channel` is the connection's single writer. It sends two kinds of things, one at a time:

- *control messages* (`pong`, `error`; `seq` 0) from a small bounded queue of its own;
- the *sequenced events* of the (user, session) `SessionChannel` it is subscribed to, read from that
  channel's retained buffer by a per-connection cursor, at the client's own pace.

Properties:
- a single writer per socket (frames never interleave, no concurrent `send_text`);
- generation is decoupled from the socket: producers append to the shared buffer and never wait;
- backpressure and resume share one mechanism: a connection that falls behind the oldest retained
  event, overflows its control queue, or has one write that exceeds the send timeout is a *slow
  consumer* (the gateway closes 4010). A write that fails because the peer is gone is recorded as
  `gone`. Neither cancels the answer: the connection simply detaches (resume, D-43);
- replay on reconnect is the same read path: the attach step positions the cursor behind the live
  edge, so the writer sends the retained events first, in order, then continues live.
"""

import asyncio
import logging
from collections import deque
from typing import Literal

from pydantic import BaseModel
from starlette.websockets import WebSocket, WebSocketDisconnect

from app.realtime import schemas as proto
from app.realtime.session_channel import SessionChannel

logger = logging.getLogger("app.realtime")

Failure = Literal["slow", "gone"]


class Channel:
    def __init__(self, websocket: WebSocket, *, max_control: int, send_timeout: float) -> None:
        self._websocket = websocket
        self._max_control = max_control
        self._send_timeout = send_timeout
        self._control: deque[str] = deque()
        self._session: SessionChannel | None = None
        self._cursor = 1
        self._wake = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()
        self._writer: asyncio.Task[None] | None = None
        self._stopped = False
        self.failure: Failure | None = None
        self.failed = asyncio.Event()

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._writer is None:
            self._writer = asyncio.create_task(self._write_loop())

    def stop(self) -> None:
        """Stop the writer (a write stuck on a slow peer is cancelled); drop what is queued.

        Synchronous on purpose: connection teardown must not have an await that a cancellation of
        the connection task could interrupt when there is nothing to wait for."""
        self._stopped = True
        writer, self._writer = self._writer, None
        if writer is not None:
            writer.cancel()
        self._control.clear()
        self._session = None

    # ------------------------------------------------------------------ the session subscription
    def subscribe(self, session: SessionChannel, cursor: int) -> None:
        self._session = session
        self._cursor = cursor
        self._poke()

    def unsubscribe(self) -> None:
        self._session = None

    def notify(self) -> None:
        self._poke()

    def lag(self) -> int:
        """Sequenced events not yet written to this socket."""
        session = self._session
        return max(0, session.last_seq - self._cursor + 1) if session is not None else 0

    # ------------------------------------------------------------------ sending
    def pending(self) -> int:
        return len(self._control)

    def send(self, message: BaseModel) -> bool:
        """Queue a control message (seq 0). Never waits. False if it could not be queued."""
        if self.failure is not None or self._stopped:
            return False
        if len(self._control) >= self._max_control:
            self._fail("slow")
            return False
        self._control.append(proto.encode(message))
        self._poke()
        return True

    def _poke(self) -> None:
        self._idle.clear()
        self._wake.set()

    async def drain(self, wait_seconds: float) -> bool:
        """Wait until everything queued and every retained event up to now has been written
        (False on failure or timeout)."""
        try:
            await asyncio.wait_for(self._idle.wait(), wait_seconds)
        except TimeoutError:
            return False
        return self.failure is None

    # ------------------------------------------------------------------ the writer
    def _fail(self, failure: Failure) -> None:
        if self.failure is None:
            self.failure = failure
            self._control.clear()
            self._idle.set()  # nothing will be written any more: release anyone in `drain`
            self.failed.set()

    def _next(self) -> str | None:
        """The next message to write, or None when there is nothing to write right now."""
        if self._control:
            return self._control.popleft()
        session = self._session
        if session is None or self._cursor > session.last_seq:
            return None
        if self._cursor < session.oldest_seq:  # fell behind what the channel still retains
            self._fail("slow")
            return None
        text = session.get(self._cursor)
        self._cursor += 1
        return text

    async def _write_loop(self) -> None:
        while True:
            await self._wake.wait()
            while True:
                text = self._next()
                if self.failure is not None:
                    return
                if text is None:
                    break
                try:
                    await asyncio.wait_for(self._websocket.send_text(text), self._send_timeout)
                except TimeoutError:
                    self._fail("slow")
                    return
                except (RuntimeError, WebSocketDisconnect, OSError):
                    self._fail("gone")
                    return
            self._wake.clear()
            self._idle.set()
