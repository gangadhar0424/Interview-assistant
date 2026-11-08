"""Dramatiq broker setup (Redis). Importing this module configures the global broker.

The queue is only a delivery mechanism: job state lives in PostgreSQL, and lost or undelivered
messages are repaired by the recovery sweep (app/workers/recovery.py).
"""

import dramatiq
from dramatiq.brokers.redis import RedisBroker

from app.core.config import get_settings


def build_broker(redis_url: str) -> RedisBroker:
    # A short connect timeout keeps API dispatch from hanging when Redis is unreachable.
    return RedisBroker(url=redis_url, socket_connect_timeout=2)  # type: ignore[no-untyped-call]


dramatiq.set_broker(build_broker(get_settings().redis_url))
