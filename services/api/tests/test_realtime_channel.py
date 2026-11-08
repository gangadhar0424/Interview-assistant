"""Backpressure and the connection writer (`Channel`, Phases 9B/9C): no database needed.

A fake socket stands in for the WebSocket so the client behaviours are exact: it reads everything
("ok"), stops reading so a write never completes ("hang"), reads slowly ("gated": the first write
waits until the test opens the gate), or is gone ("gone").
"""

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from functools import partial
from typing import Any, cast

from app.qa.events import AnswerDelta
from app.realtime import adapter
from app.realtime import schemas as proto
from app.realtime.channel import Channel
from app.realtime.session_channel import SessionChannel, SessionChannels


class FakeSocket:
    def __init__(self, mode: str = "ok") -> None:
        self.mode = mode
        self.sent: list[str] = []
        self.attempts = 0
        self.gate = asyncio.Event()

    async def send_text(self, text: str) -> None:
        self.attempts += 1
        if self.mode == "hang":
            await asyncio.Event().wait()  # a peer that never reads: the write never completes
        if self.mode == "gated" and self.attempts == 1:
            await self.gate.wait()
        if self.mode == "gone":
            raise RuntimeError("peer closed")
        self.sent.append(text)


def make(socket: FakeSocket, *, control: int = 100, timeout: float = 5.0) -> Channel:
    return Channel(cast(Any, socket), max_control=control, send_timeout=timeout)


def session(*, max_events: int = 100) -> SessionChannel:
    channels = SessionChannels(max_events=max_events, grace_seconds=30, max_detached=10)
    return channels.get_or_create((uuid.uuid4(), uuid.uuid4()))


def run(scenario: Callable[[], Awaitable[None]]) -> None:
    asyncio.run(asyncio.wait_for(scenario(), 20))


def emit(channel: SessionChannel, text: str = "x") -> int:
    return channel.emit(partial(adapter.to_message, AnswerDelta(uuid.uuid4(), 0, text)))


def test_control_messages_and_events_are_written_in_order_by_one_writer() -> None:
    async def scenario() -> None:
        socket = FakeSocket()
        channel = make(socket)
        shared = session()
        channel.start()
        shared.attach(channel, None)
        assert channel.send(proto.error_event("invalid_message", fatal=False))
        for i in range(5):
            emit(shared, f"t{i}")
        assert await channel.drain(2.0)
        sent = [json.loads(text) for text in socket.sent]
        deltas = [m for m in sent if m["type"] == "answer.delta"]
        assert [m["seq"] for m in deltas] == [1, 2, 3, 4, 5]
        assert [m["payload"]["text"] for m in deltas] == [f"t{i}" for i in range(5)]
        assert [m["seq"] for m in sent if m["type"] == "error"] == [0]
        assert channel.lag() == 0 and channel.failure is None
        channel.stop()

    run(scenario)


def test_attaching_with_a_resume_point_replays_then_continues_live_in_order() -> None:
    async def scenario() -> None:
        shared = session()
        for i in range(6):
            emit(shared, f"e{i}")  # seq 1..6, no socket attached yet
        socket = FakeSocket()
        channel = make(socket)
        channel.start()
        assert shared.attach(channel, 3) is False  # the client processed up to 3
        emit(shared, "live-7")
        emit(shared, "live-8")
        assert await channel.drain(2.0)
        seqs = [json.loads(t)["seq"] for t in socket.sent]
        texts = [json.loads(t)["payload"]["text"] for t in socket.sent]
        assert seqs == [4, 5, 6, 7, 8]  # no gap, no duplicate, replay before live
        assert texts == ["e3", "e4", "e5", "live-7", "live-8"]
        channel.stop()

    run(scenario)


def test_a_full_control_queue_makes_the_client_a_slow_consumer() -> None:
    async def scenario() -> None:
        channel = make(FakeSocket("hang"), control=3)
        channel.start()
        results = [
            channel.send(proto.error_event("invalid_message", fatal=False)) for _ in range(6)
        ]
        assert results == [True, True, True, False, False, False]
        assert channel.failure == "slow" and channel.failed.is_set()
        assert channel.pending() == 0  # the backlog is dropped, not kept in memory
        assert await channel.drain(0.1) is False
        channel.stop()

    run(scenario)


def test_a_client_that_falls_behind_the_retained_buffer_is_a_slow_consumer() -> None:
    async def scenario() -> None:
        socket = FakeSocket("gated")
        channel = make(socket, timeout=30)
        shared = session(max_events=5)
        channel.start()
        shared.attach(channel, None)
        emit(shared)  # the writer starts this write and waits at the gate
        await asyncio.sleep(0.05)
        for _ in range(20):  # 20 more while it is stuck: only the newest 5 are retained
            emit(shared)
        assert shared.last_seq - shared.oldest_seq + 1 == 5  # bounded
        socket.gate.set()  # the client wakes up and finds the events it needs are gone
        await asyncio.wait_for(channel.failed.wait(), 3)
        assert channel.failure == "slow"
        channel.stop()

    run(scenario)


def test_a_write_that_never_completes_is_a_slow_consumer_after_the_send_timeout() -> None:
    async def scenario() -> None:
        socket = FakeSocket("hang")
        channel = make(socket, timeout=0.1)
        shared = session()
        channel.start()
        shared.attach(channel, None)
        emit(shared)
        await asyncio.wait_for(channel.failed.wait(), 3)
        assert channel.failure == "slow" and socket.attempts == 1
        channel.stop()

    run(scenario)


def test_a_peer_that_is_gone_is_not_reported_as_slow() -> None:
    async def scenario() -> None:
        channel = make(FakeSocket("gone"))
        shared = session()
        channel.start()
        shared.attach(channel, None)
        emit(shared)
        await asyncio.wait_for(channel.failed.wait(), 3)
        assert channel.failure == "gone"
        assert channel.send(proto.error_event("invalid_message", fatal=False)) is False
        channel.stop()

    run(scenario)


def test_the_retained_buffer_never_grows_past_its_bound_however_fast_events_arrive() -> None:
    async def scenario() -> None:
        shared = session(max_events=50)
        highest = 0
        for _ in range(10_000):
            emit(shared)
            highest = max(highest, shared.last_seq - shared.oldest_seq + 1)
        assert highest <= 50 and shared.last_seq == 10_000 and shared.oldest_seq == 9_951

    run(scenario)


def test_drain_waits_for_the_writer_and_reports_failure() -> None:
    async def scenario() -> None:
        ok = make(FakeSocket())
        shared = session()
        ok.start()
        shared.attach(ok, None)
        emit(shared)
        assert await ok.drain(2.0) is True and ok.lag() == 0
        ok.stop()

        stuck = make(FakeSocket("hang"), timeout=30)
        other = session()
        stuck.start()
        other.attach(stuck, None)
        emit(other)
        assert await stuck.drain(0.1) is False  # not written within the wait
        stuck.stop()

    run(scenario)


def test_stop_cancels_a_blocked_writer_and_nothing_is_accepted_afterwards() -> None:
    async def scenario() -> None:
        socket = FakeSocket("hang")
        channel = make(socket, timeout=30)
        shared = session()
        channel.start()
        shared.attach(channel, None)
        emit(shared)
        await asyncio.sleep(0.05)  # the writer is now blocked inside the write
        before = len(asyncio.all_tasks())
        channel.stop()
        await asyncio.sleep(0.05)
        assert len(asyncio.all_tasks()) == before - 1  # the writer task is gone
        assert channel.send(proto.error_event("invalid_message", fatal=False)) is False
        assert channel.pending() == 0 and channel.lag() == 0
        channel.stop()  # idempotent

    run(scenario)
