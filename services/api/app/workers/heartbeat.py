"""Worker liveness (M-12, D-79).

A running Dramatiq worker writes a short-lived Redis key every few seconds. The key expires on its
own, so a crashed or hung worker disappears from view without any cleanup, and
`python -m app.workers.healthcheck` (a container healthcheck or a monitor probe) can tell whether at
least one worker is alive. Nothing here carries job data, user data or secrets: the key holds only a
process id and a timestamp. A Redis failure never affects job processing.
"""

import logging
import os
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import dramatiq

logger = logging.getLogger(__name__)

HEARTBEAT_KEY_PREFIX = "ia:worker:heartbeat:"
HEARTBEAT_INTERVAL_SECONDS = 15.0
HEARTBEAT_TTL_SECONDS = 45


# A redis.Redis client, or any object with set/delete/scan_iter/get (tests pass a small fake).
RedisLike = Any


def worker_key(worker_id: str) -> str:
    return f"{HEARTBEAT_KEY_PREFIX}{worker_id}"


def default_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


class HeartbeatMiddleware(dramatiq.Middleware):
    """Starts a daemon thread when the worker boots and removes the key when it shuts down."""

    def __init__(
        self,
        client_factory: Callable[[], RedisLike],
        *,
        worker_id: str | None = None,
        interval_seconds: float = HEARTBEAT_INTERVAL_SECONDS,
        ttl_seconds: int = HEARTBEAT_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._client_factory = client_factory
        self._worker_id = worker_id or default_worker_id()
        self._interval = interval_seconds
        self._ttl = ttl_seconds
        self._clock = clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._client: RedisLike | None = None

    @property
    def key(self) -> str:
        return worker_key(self._worker_id)

    def beat(self) -> bool:
        """Write one heartbeat; False (class name logged) when Redis is unavailable."""
        try:
            if self._client is None:
                self._client = self._client_factory()
            self._client.set(self.key, str(int(self._clock())), ex=self._ttl)
            return True
        except Exception as exc:  # noqa: BLE001 - a heartbeat must never disturb the worker
            self._client = None  # reconnect on the next beat
            logger.warning("worker heartbeat failed", extra={"error_type": type(exc).__name__})
            return False

    def _run(self) -> None:
        while not self._stop.is_set():
            self.beat()
            self._stop.wait(self._interval)

    def after_worker_boot(self, broker: object, worker: object) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="worker-heartbeat", daemon=True)
        self._thread.start()

    def before_worker_shutdown(self, broker: object, worker: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        try:
            if self._client is not None:
                self._client.delete(self.key)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "worker heartbeat cleanup failed", extra={"error_type": type(exc).__name__}
            )


@dataclass(frozen=True)
class WorkerBeat:
    worker_id: str
    age_seconds: float


def alive_workers(client: RedisLike, now: float, max_age_seconds: float) -> list[WorkerBeat]:
    """Workers whose last heartbeat is recent. A malformed value is ignored, never an error."""
    found: list[WorkerBeat] = []
    for raw_key in client.scan_iter(match=f"{HEARTBEAT_KEY_PREFIX}*"):
        key = raw_key.decode() if isinstance(raw_key, bytes) else str(raw_key)
        value = client.get(key)
        if value is None:
            continue
        try:
            beat = float(value.decode() if isinstance(value, bytes) else value)
        except ValueError:
            continue
        age = now - beat
        if -5 <= age <= max_age_seconds:  # a few seconds of clock skew between hosts is tolerated
            found.append(WorkerBeat(key.removeprefix(HEARTBEAT_KEY_PREFIX), max(age, 0.0)))
    return found
