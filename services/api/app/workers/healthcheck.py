"""Exit 0 when at least one ingestion worker is alive, else exit 1 (M-12, D-79).

    uv run python -m app.workers.healthcheck            # exit code 0 or 1
    uv run python -m app.workers.healthcheck --max-age 60

Used as the worker container's HEALTHCHECK and usable from any monitor. The output never contains a
Redis address, a credential or a job identifier.
"""

import argparse
import sys
import time
from collections.abc import Callable

import redis

from app.core.config import get_settings
from app.workers.heartbeat import HEARTBEAT_TTL_SECONDS, RedisLike, alive_workers


def _default_client() -> RedisLike:
    return redis.Redis.from_url(
        get_settings().redis_url, socket_connect_timeout=2, socket_timeout=2
    )


def main(
    argv: list[str] | None = None,
    *,
    client_factory: Callable[[], RedisLike] = _default_client,
    clock: Callable[[], float] = time.time,
) -> int:
    parser = argparse.ArgumentParser(description="Check that an ingestion worker is alive.")
    parser.add_argument("--max-age", type=float, default=float(HEARTBEAT_TTL_SECONDS))
    args = parser.parse_args(argv)
    try:
        workers = alive_workers(client_factory(), clock(), args.max_age)
    except Exception as exc:  # noqa: BLE001 - report the class only, never the message
        print(f"unhealthy: cannot check workers ({type(exc).__name__})")  # noqa: T201
        return 1
    if not workers:
        print(f"unhealthy: no worker heartbeat in the last {args.max_age:.0f} seconds")  # noqa: T201
        return 1
    print(f"healthy: {len(workers)} worker(s) alive")  # noqa: T201
    return 0


if __name__ == "__main__":
    sys.exit(main())
