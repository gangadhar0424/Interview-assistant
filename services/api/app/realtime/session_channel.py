"""The per-(user, session) realtime channel (Phase 9C, D-43).

One `SessionChannel` exists per (user_id, session_id), both taken from the authenticated ticket and
nothing else, so no client can address another user's channel. It owns:

- the sequence counter of the sequenced Q&A events (control messages are `seq` 0, never numbered);
- a bounded buffer of the encoded events (`max_events`, and an internal byte budget so a few huge
  deltas cannot hold unbounded memory), oldest dropped first;
- the single active `Generation`, which is NOT tied to any socket.

A connection attaches at `client.hello` and detaches when its socket ends. A detached channel keeps
the generation running and the buffer for the grace period; reattaching inside it replays what the
client missed (`resume_from_seq`) and continues live. Replay and attach happen in one synchronous
step (no await), so no live event can overtake or be lost between them. Expiry, the shutdown of the
server and the cap on detached channels cancel the generation (the answer is stored `cancelled` with
its partial text by `ask`) and free the buffer.

Events are delivery only: the database is the source of truth for every answer's terminal state.
"""

import asyncio
import logging
import uuid
from collections import deque
from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel

from app.realtime import schemas as proto

if TYPE_CHECKING:
    from app.realtime.generation import Generation

logger = logging.getLogger("app.realtime")

Key = tuple[uuid.UUID, uuid.UUID]
RETAINED_BYTES = 2 * 1024 * 1024  # internal budget per channel; not a setting (D-43)


class Subscriber(Protocol):
    """The connection side (`app.realtime.channel.Channel`)."""

    def subscribe(self, session: "SessionChannel", cursor: int) -> None: ...

    def unsubscribe(self) -> None: ...

    def notify(self) -> None: ...

    def send(self, message: BaseModel) -> bool: ...


class SessionChannel:
    def __init__(self, key: Key, registry: "SessionChannels") -> None:
        self.key = key
        self._registry = registry
        self._events: deque[tuple[int, str]] = deque()
        self._bytes = 0
        self.last_seq = 0
        self.generation: Generation | None = None
        self._attached: Subscriber | None = None
        self._timer: asyncio.TimerHandle | None = None
        self.expired = False

    # ------------------------------------------------------------------ the buffer
    @property
    def oldest_seq(self) -> int:
        """The first `seq` still retained (`last_seq + 1` when nothing is)."""
        return self._events[0][0] if self._events else self.last_seq + 1

    def get(self, seq: int) -> str:
        """The encoded event `seq` (must satisfy `oldest_seq <= seq <= last_seq`)."""
        return self._events[seq - self._events[0][0]][1]

    def emit(self, build: Callable[[int], BaseModel]) -> int:
        """Number, encode and retain one event; wake the attached socket. Never waits."""
        seq = self.last_seq + 1
        text = proto.encode(build(seq))
        self.last_seq = seq
        self._events.append((seq, text))
        self._bytes += len(text)
        while len(self._events) > self._registry.max_events or (
            self._bytes > RETAINED_BYTES and len(self._events) > 1
        ):
            _, dropped = self._events.popleft()
            self._bytes -= len(dropped)
        if self._attached is not None:
            self._attached.notify()
        return seq

    def send_control(self, message: BaseModel) -> None:
        """A control message (`error`, ...) to the attached socket, if there is one."""
        if self._attached is not None:
            self._attached.send(message)

    @property
    def generation_active(self) -> bool:
        return self.generation is not None and self.generation.active

    # ------------------------------------------------------------------ attach / detach
    @property
    def attached(self) -> bool:
        return self._attached is not None

    def attach(self, subscriber: Subscriber, resume_from_seq: int | None) -> bool:
        """Attach a socket and decide what it receives. Returns True when the requested resume
        point is not available (the caller sends `resume_unavailable`; nothing is replayed).

        Synchronous on purpose: the cursor is chosen and the socket subscribed with no await in
        between, so replayed events always precede live ones and none is skipped or repeated."""
        self._registry.on_attach(self)
        cursor = self.last_seq + 1  # live only
        unavailable = False
        if resume_from_seq:
            if resume_from_seq > self.last_seq or resume_from_seq + 1 < self.oldest_seq:
                unavailable = True
            else:
                cursor = resume_from_seq + 1
        previous, self._attached = self._attached, subscriber
        if previous is not None and previous is not subscriber:
            previous.unsubscribe()  # a stale socket: the newer one takes over
        subscriber.subscribe(self, cursor)
        return unavailable

    def detach(self, subscriber: Subscriber) -> None:
        """The socket ended. A late detach of a socket that was already replaced is a no-op."""
        if self._attached is not subscriber:
            return
        self._attached = None
        self._registry.on_detach(self)

    # ------------------------------------------------------------------ expiry
    def start_grace(self, seconds: float) -> None:
        self.cancel_grace()
        self._timer = asyncio.get_running_loop().call_later(seconds, self._registry.expire, self)

    def cancel_grace(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None


class SessionChannels:
    """All channels of the process, with the grace timers and the cap on detached ones."""

    def __init__(self, *, max_events: int, grace_seconds: float, max_detached: int) -> None:
        self.max_events = max_events
        self.grace_seconds = grace_seconds
        self._max_detached = max_detached
        self._channels: dict[Key, SessionChannel] = {}
        self._detached: dict[Key, SessionChannel] = {}  # in detach order: oldest first
        self._cleanups: set[asyncio.Task[None]] = set()

    def __len__(self) -> int:
        return len(self._channels)

    def detached_count(self) -> int:
        return len(self._detached)

    def last_seq(self, key: Key) -> int:
        channel = self._channels.get(key)
        return channel.last_seq if channel is not None else 0

    def get(self, key: Key) -> SessionChannel | None:
        return self._channels.get(key)

    def get_or_create(self, key: Key) -> SessionChannel:
        channel = self._channels.get(key)
        if channel is None:
            channel = self._channels[key] = SessionChannel(key, self)
        return channel

    # ------------------------------------------------------------------ lifecycle hooks
    def on_attach(self, channel: SessionChannel) -> None:
        channel.cancel_grace()
        self._detached.pop(channel.key, None)

    def on_detach(self, channel: SessionChannel) -> None:
        if channel.last_seq == 0 and not channel.generation_active:
            self._discard(channel)  # it never carried an event: nothing to resume
            return
        self._detached[channel.key] = channel
        channel.start_grace(self.grace_seconds)
        while len(self._detached) > self._max_detached:  # bounded across reconnects
            oldest = next(iter(self._detached.values()))
            self.expire(oldest)
            logger.info("ws detached channel evicted", extra={"reason": "cap"})

    def expire(self, channel: SessionChannel) -> None:
        """Grace over (or evicted, or shutdown): cancel the generation and free the buffer."""
        if channel.expired or channel.attached:
            return
        channel.expired = True
        channel.cancel_grace()
        self._discard(channel)
        generation = channel.generation
        if generation is not None and generation.active:
            task = asyncio.get_running_loop().create_task(generation.aclose())
            self._cleanups.add(task)
            task.add_done_callback(self._cleanups.discard)
            logger.info(
                "ws detached generation cancelled",
                extra={"user_id": str(channel.key[0]), "session_id": str(channel.key[1])},
            )

    def _discard(self, channel: SessionChannel) -> None:
        if self._channels.get(channel.key) is channel:
            del self._channels[channel.key]
        if self._detached.get(channel.key) is channel:
            del self._detached[channel.key]

    async def shutdown(self) -> None:
        """Server stop: cancel every generation and wait for their cleanup (answers are stored)."""
        for channel in list(self._channels.values()):
            channel._attached = None  # sockets are being closed by the server anyway
            self.expire(channel)
        if self._cleanups:
            await asyncio.gather(*self._cleanups, return_exceptions=True)
