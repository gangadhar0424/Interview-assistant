"""Dramatiq broker setup (Redis). Importing this module configures the global broker.

The queue is only a delivery mechanism: job state lives in PostgreSQL, and lost or undelivered
messages are repaired by the recovery sweep (app/workers/recovery.py).
"""

import dramatiq
import redis
from dramatiq.brokers.redis import RedisBroker

from app.core.config import get_settings
from app.workers.heartbeat import HeartbeatMiddleware


def build_broker(redis_url: str) -> RedisBroker:
    # A short connect timeout keeps API dispatch from hanging when Redis is unreachable.
    broker = RedisBroker(url=redis_url, socket_connect_timeout=2)  # type: ignore[no-untyped-call]
    # Worker liveness (M-12): only acts inside a running worker process (after_worker_boot), never
    # in the API process that merely enqueues messages.
    broker.add_middleware(  # type: ignore[no-untyped-call]
        HeartbeatMiddleware(
            lambda: redis.Redis.from_url(redis_url, socket_connect_timeout=2, socket_timeout=2)
        )
    )
    return broker


dramatiq.set_broker(build_broker(get_settings().redis_url))
