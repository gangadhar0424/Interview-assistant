"""Worker liveness (M-12, D-79): heartbeat, expiry, healthcheck and failure behaviour."""

import threading
import time

import pytest

from app.workers import healthcheck
from app.workers.heartbeat import (
    HEARTBEAT_KEY_PREFIX,
    HEARTBEAT_TTL_SECONDS,
    HeartbeatMiddleware,
    alive_workers,
    worker_key,
)


class FakeRedis:
    """Just enough Redis, with TTLs honoured against an injected clock."""

    def __init__(self, clock: "Clock") -> None:
        self.clock = clock
        self.data: dict[str, tuple[str, float | None]] = {}
        self.fail = False
        self.calls = 0

    def _check(self) -> None:
        self.calls += 1
        if self.fail:
            raise ConnectionError("redis://user:secret@10.0.0.9:6379 refused")

    def set(self, name: str, value: str, ex: int | None = None) -> bool:
        self._check()
        self.data[name] = (value, None if ex is None else self.clock.now + ex)
        return True

    def delete(self, *names: str) -> int:
        self._check()
        return sum(1 for n in names if self.data.pop(n, None) is not None)

    def _live(self) -> dict[str, str]:
        return {k: v for k, (v, exp) in self.data.items() if exp is None or exp > self.clock.now}

    def scan_iter(self, match: str | None = None):  # type: ignore[no-untyped-def]
        self._check()
        prefix = (match or "*").rstrip("*")
        return iter([k.encode() for k in self._live() if k.startswith(prefix)])

    def get(self, name: str) -> bytes | None:
        self._check()
        value = self._live().get(name)
        return None if value is None else value.encode()


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def _middleware(redis: FakeRedis, clock: Clock, **kw: object) -> HeartbeatMiddleware:
    return HeartbeatMiddleware(lambda: redis, worker_id="host:1", clock=clock, **kw)  # type: ignore[arg-type]


def test_a_beat_writes_a_key_that_expires_on_its_own() -> None:
    clock = Clock()
    redis = FakeRedis(clock)
    mw = _middleware(redis, clock)
    assert mw.beat() is True
    assert mw.key == worker_key("host:1") == f"{HEARTBEAT_KEY_PREFIX}host:1"
    assert [w.worker_id for w in alive_workers(redis, clock.now, 45)] == ["host:1"]
    clock.now += HEARTBEAT_TTL_SECONDS + 1  # the worker died and stopped beating
    assert alive_workers(redis, clock.now, 45) == []


def test_a_stale_value_is_not_alive_even_if_the_key_still_exists() -> None:
    clock = Clock()
    redis = FakeRedis(clock)
    redis.data[worker_key("old")] = (str(int(clock.now - 600)), None)
    redis.data[worker_key("fresh")] = (str(int(clock.now - 3)), None)
    redis.data[worker_key("junk")] = ("not a number", None)
    assert [w.worker_id for w in alive_workers(redis, clock.now, 45)] == ["fresh"]


def test_clock_skew_between_hosts_is_tolerated_but_the_future_is_not() -> None:
    clock = Clock()
    redis = FakeRedis(clock)
    redis.data[worker_key("a")] = (str(int(clock.now + 2)), None)
    redis.data[worker_key("b")] = (str(int(clock.now + 3600)), None)
    assert [w.worker_id for w in alive_workers(redis, clock.now, 45)] == ["a"]


def test_a_redis_failure_never_raises_into_the_worker_and_recovers() -> None:
    clock = Clock()
    redis = FakeRedis(clock)
    mw = _middleware(redis, clock)
    redis.fail = True
    assert mw.beat() is False  # reported, not raised
    redis.fail = False
    assert mw.beat() is True  # reconnects on the next beat


def test_the_heartbeat_thread_starts_beats_and_removes_its_key_on_shutdown() -> None:
    clock = Clock()
    redis = FakeRedis(clock)
    mw = _middleware(redis, clock, interval_seconds=0.02)
    mw.after_worker_boot(object(), object())
    deadline = time.time() + 2
    while mw.key not in redis.data and time.time() < deadline:
        time.sleep(0.01)
    assert mw.key in redis.data
    first = redis.calls
    time.sleep(0.1)
    assert redis.calls > first  # it keeps beating
    mw.after_worker_boot(object(), object())  # a second boot does not start a second thread
    assert sum(1 for t in threading.enumerate() if t.name == "worker-heartbeat") <= 1
    mw.before_worker_shutdown(object(), object())
    assert mw.key not in redis.data


def test_shutdown_survives_redis_being_down() -> None:
    clock = Clock()
    redis = FakeRedis(clock)
    mw = _middleware(redis, clock, interval_seconds=0.02)
    mw.after_worker_boot(object(), object())
    time.sleep(0.05)
    redis.fail = True
    mw.before_worker_shutdown(object(), object())  # must not raise


def test_healthcheck_exit_codes_and_messages(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    clock = Clock()
    redis = FakeRedis(clock)

    def run(*argv: str) -> int:
        return healthcheck.main(list(argv), client_factory=lambda: redis, clock=clock)

    assert run() == 1
    assert "no worker heartbeat" in capsys.readouterr().out

    _middleware(redis, clock).beat()
    assert run() == 0
    assert "healthy: 1 worker" in capsys.readouterr().out

    clock.now += 120
    assert run() == 1  # the key expired with its TTL
    redis.data[worker_key("persistent")] = (str(int(clock.now - 100)), None)  # no TTL, 100 s old
    assert run() == 1  # older than the default 45 s limit
    assert run("--max-age", "300") == 0


def test_healthcheck_with_redis_down_fails_without_leaking_the_address(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    redis = FakeRedis(Clock())
    redis.fail = True
    assert healthcheck.main([], client_factory=lambda: redis, clock=Clock()) == 1
    out = capsys.readouterr().out
    assert "ConnectionError" in out
    assert "10.0.0.9" not in out and "secret" not in out and "redis://" not in out


def test_healthcheck_against_a_closed_port_fails_cleanly(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # the default test environment points REDIS_URL at a closed port
    assert healthcheck.main([]) == 1
    out = capsys.readouterr().out
    assert out.startswith("unhealthy") and "127.0.0.1" not in out


def test_the_broker_carries_the_heartbeat_middleware() -> None:
    from app.workers.broker import build_broker

    broker = build_broker("redis://127.0.0.1:1/0")
    assert any(isinstance(m, HeartbeatMiddleware) for m in broker.middleware)
