"""The per-(user, session) channel and its registry (Phase 9C, D-43): no database needed.

Covers numbering and retention, the resume decision table, the grace timer, expiry, the cap on
detached channels, shutdown, and the identity-checked attach/detach that supersede relies on.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from functools import partial

import pytest
from pydantic import BaseModel

from app.qa.events import AnswerDelta
from app.realtime import adapter
from app.realtime import schemas as realtime_schemas
from app.realtime import session_channel as sc
from app.realtime.session_channel import SessionChannel, SessionChannels


class FakeSubscriber:
    def __init__(self) -> None:
        self.cursor: int | None = None
        self.unsubscribed = False
        self.notified = 0
        self.controls: list[BaseModel] = []

    def subscribe(self, session: SessionChannel, cursor: int) -> None:
        self.cursor = cursor

    def unsubscribe(self) -> None:
        self.unsubscribed = True

    def notify(self) -> None:
        self.notified += 1

    def send(self, message: BaseModel) -> bool:
        self.controls.append(message)
        return True


class FakeGeneration:
    def __init__(self, active: bool = True) -> None:
        self.active = active
        self.closed = 0

    async def aclose(self) -> None:
        self.closed += 1
        self.active = False


def registry(*, events: int = 100, grace: float = 0.2, detached: int = 10) -> SessionChannels:
    return SessionChannels(max_events=events, grace_seconds=grace, max_detached=detached)


def key() -> tuple[uuid.UUID, uuid.UUID]:
    return uuid.uuid4(), uuid.uuid4()


def emit(channel: SessionChannel, text: str = "x") -> int:
    return channel.emit(partial(adapter.to_message, AnswerDelta(uuid.uuid4(), 0, text)))


def run(scenario: Callable[[], Awaitable[None]]) -> None:
    asyncio.run(asyncio.wait_for(scenario(), 20))


def fake_generation(channel: SessionChannel, active: bool = True) -> FakeGeneration:
    generation = FakeGeneration(active)
    channel.generation = generation  # type: ignore[assignment]
    return generation


# ----------------------------------------------------------------------------- numbering, retention
def test_events_are_numbered_from_one_and_retained_in_order() -> None:
    async def scenario() -> None:
        channel = registry().get_or_create(key())
        assert [emit(channel) for _ in range(4)] == [1, 2, 3, 4]
        assert channel.last_seq == 4 and channel.oldest_seq == 1
        assert [channel.get(n).count('"seq":' + str(n)) for n in (1, 2, 3, 4)] == [1, 1, 1, 1]

    run(scenario)


def test_retention_is_bounded_by_the_event_count_oldest_first() -> None:
    async def scenario() -> None:
        channel = registry(events=10).get_or_create(key())
        for _ in range(25):
            emit(channel)
        assert (channel.oldest_seq, channel.last_seq) == (16, 25)
        assert '"seq":16' in channel.get(16) and '"seq":25' in channel.get(25)

    run(scenario)


def test_retention_is_also_bounded_by_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(sc, "RETAINED_BYTES", 4000)
        channel = registry(events=1000).get_or_create(key())
        for _ in range(50):
            emit(channel, "y" * 500)  # ~700 bytes each: only a handful fit
        retained = channel.last_seq - channel.oldest_seq + 1
        assert 1 <= retained <= 6 and channel.last_seq == 50

    run(scenario)


def test_one_huge_event_is_never_dropped_to_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(sc, "RETAINED_BYTES", 100)
        channel = registry().get_or_create(key())
        emit(channel, "z" * 5000)
        assert channel.oldest_seq == channel.last_seq == 1  # the newest event is always kept

    run(scenario)


# ----------------------------------------------------------------------------- the resume decision
@pytest.mark.parametrize(
    "emitted, retained_events, resume, unavailable, cursor",
    [
        (10, 100, None, False, 11),  # no resume point: live only
        (10, 100, 0, False, 11),  # zero is the same
        (10, 100, 10, False, 11),  # nothing missed
        (10, 100, 4, False, 5),  # replay 5..10
        (10, 100, 11, True, 11),  # ahead of the channel: not valid
        (10, 100, 1000, True, 11),
        (10, 3, 6, True, 11),  # events 7.. retained? oldest is 8: 7 is gone
        (10, 3, 7, False, 8),  # exactly the first retained event is the next one needed
        (10, 3, 8, False, 9),
        (0, 100, 5, True, 1),  # an empty channel cannot have a resume point above 0
        (0, 100, 0, False, 1),
    ],
)
def test_the_resume_decision(
    emitted: int, retained_events: int, resume: int | None, unavailable: bool, cursor: int
) -> None:
    async def scenario() -> None:
        channel = registry(events=retained_events).get_or_create(key())
        for _ in range(emitted):
            emit(channel)
        subscriber = FakeSubscriber()
        assert channel.attach(subscriber, resume) is unavailable
        assert subscriber.cursor == cursor  # unavailable also means: live only, no replay

    run(scenario)


def test_attaching_a_newer_socket_replaces_the_older_one() -> None:
    async def scenario() -> None:
        channel = registry().get_or_create(key())
        old, new = FakeSubscriber(), FakeSubscriber()
        channel.attach(old, None)
        channel.attach(new, None)
        assert old.unsubscribed and not new.unsubscribed
        emit(channel)
        assert (old.notified, new.notified) == (0, 1)  # only the current socket is woken

    run(scenario)


def test_a_late_detach_of_a_replaced_socket_changes_nothing() -> None:
    async def scenario() -> None:
        channels = registry()
        channel = channels.get_or_create(key())
        old, new = FakeSubscriber(), FakeSubscriber()
        generation = fake_generation(channel)
        emit(channel)
        channel.attach(old, None)
        channel.attach(new, None)
        channel.detach(old)  # the superseded socket finishes closing afterwards
        assert channel.attached and channels.detached_count() == 0 and generation.closed == 0
        await asyncio.sleep(0.3)  # longer than the grace: still attached, so still alive
        assert generation.closed == 0 and channels.get(channel.key) is channel

    run(scenario)


# ----------------------------------------------------------------------------- grace and expiry
def test_a_channel_that_never_carried_an_event_is_dropped_on_detach() -> None:
    async def scenario() -> None:
        channels = registry()
        channel = channels.get_or_create(key())
        subscriber = FakeSubscriber()
        channel.attach(subscriber, None)
        channel.detach(subscriber)
        assert len(channels) == 0 and channels.detached_count() == 0

    run(scenario)


def test_a_detached_channel_keeps_its_buffer_and_generation_during_the_grace() -> None:
    async def scenario() -> None:
        channels = registry(grace=0.3)
        channel = channels.get_or_create(key())
        generation = fake_generation(channel)
        subscriber = FakeSubscriber()
        channel.attach(subscriber, None)
        emit(channel)
        channel.detach(subscriber)
        await asyncio.sleep(0.1)
        assert channels.get(channel.key) is channel and channels.detached_count() == 1
        assert generation.closed == 0 and channel.last_seq == 1  # still running, still retained

    run(scenario)


def test_reattaching_inside_the_grace_cancels_the_expiry() -> None:
    async def scenario() -> None:
        channels = registry(grace=0.2)
        channel = channels.get_or_create(key())
        generation = fake_generation(channel)
        first = FakeSubscriber()
        channel.attach(first, None)
        emit(channel)
        channel.detach(first)
        await asyncio.sleep(0.1)
        second = FakeSubscriber()
        assert channel.attach(second, 0) is False
        await asyncio.sleep(0.3)  # well past the original deadline
        assert generation.closed == 0 and channels.get(channel.key) is channel
        assert channels.detached_count() == 0

    run(scenario)


def test_expiry_cancels_the_generation_and_frees_the_channel() -> None:
    async def scenario() -> None:
        channels = registry(grace=0.1)
        channel = channels.get_or_create(key())
        generation = fake_generation(channel)
        subscriber = FakeSubscriber()
        channel.attach(subscriber, None)
        emit(channel)
        channel.detach(subscriber)
        await asyncio.sleep(0.4)
        assert generation.closed == 1 and not generation.active
        assert channels.get(channel.key) is None and channels.detached_count() == 0
        fresh = channels.get_or_create(channel.key)  # a later reconnect starts a new channel
        assert fresh is not channel and fresh.last_seq == 0

    run(scenario)


def test_expiry_of_a_finished_generation_just_frees_the_buffer() -> None:
    async def scenario() -> None:
        channels = registry(grace=0.1)
        channel = channels.get_or_create(key())
        generation = fake_generation(channel, active=False)
        subscriber = FakeSubscriber()
        channel.attach(subscriber, None)
        emit(channel)
        channel.detach(subscriber)
        await asyncio.sleep(0.3)
        assert generation.closed == 0 and len(channels) == 0

    run(scenario)


def test_the_number_of_detached_channels_is_capped_oldest_first() -> None:
    async def scenario() -> None:
        channels = registry(grace=30, detached=2)
        made = []
        for _ in range(4):
            channel = channels.get_or_create(key())
            generation = fake_generation(channel)
            subscriber = FakeSubscriber()
            channel.attach(subscriber, None)
            emit(channel)
            channel.detach(subscriber)
            made.append((channel, generation))
        await asyncio.sleep(0.05)
        assert channels.detached_count() == 2
        assert [g.closed for _, g in made] == [1, 1, 0, 0]  # the two oldest were cancelled
        assert [channels.get(c.key) is c for c, _ in made] == [False, False, True, True]
        await channels.shutdown()

    run(scenario)


def test_shutdown_cancels_every_generation_and_empties_the_registry() -> None:
    async def scenario() -> None:
        channels = registry(grace=30)
        generations: list[FakeGeneration] = []
        for attached in (True, False):
            channel = channels.get_or_create(key())
            generations.append(fake_generation(channel))
            subscriber = FakeSubscriber()
            channel.attach(subscriber, None)
            emit(channel)
            if not attached:
                channel.detach(subscriber)
        await channels.shutdown()
        assert [g.closed for g in generations] == [1, 1] and len(channels) == 0

    run(scenario)


def test_an_expired_or_attached_channel_is_never_expired_twice_or_while_attached() -> None:
    async def scenario() -> None:
        channels = registry(grace=0.1)
        channel = channels.get_or_create(key())
        generation = fake_generation(channel)
        subscriber = FakeSubscriber()
        channel.attach(subscriber, None)
        emit(channel)
        channels.expire(channel)  # attached: refused
        assert generation.closed == 0
        channel.detach(subscriber)
        await asyncio.sleep(0.3)
        channels.expire(channel)  # already expired: no second cancel
        await asyncio.sleep(0.05)
        assert generation.closed == 1

    run(scenario)


def test_control_messages_go_only_to_the_attached_socket() -> None:
    async def scenario() -> None:
        channel = registry().get_or_create(key())
        channel.send_control(realtime_schemas.error_event("invalid_message", fatal=False))  # nobody
        subscriber = FakeSubscriber()
        channel.attach(subscriber, None)
        channel.send_control(realtime_schemas.error_event("nothing_to_cancel", fatal=False))
        assert len(subscriber.controls) == 1

    run(scenario)


def test_channels_of_different_identities_never_share_events() -> None:
    async def scenario() -> None:
        channels = registry()
        alice, bob = channels.get_or_create(key()), channels.get_or_create(key())
        emit(alice, "ALICE-SECRET")
        assert bob.last_seq == 0 and bob.oldest_seq == 1
        subscriber = FakeSubscriber()
        assert bob.attach(subscriber, 1) is True  # Bob cannot "resume" Alice's event number
        assert subscriber.cursor == 1

    run(scenario)
