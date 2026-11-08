"""Test doubles for the realtime gateway (Phase 9). Synthetic data only.

`FakeRedis` implements the two commands `TicketStore` uses (`SET ... NX EX`, `GETDEL`) with real
TTL semantics driven by an injectable clock, so the production `TicketStore` class is what the
tests exercise; there is no in-memory production store (D-41).
"""

import time
import uuid
from collections.abc import Callable
from typing import Any

from fastapi.testclient import TestClient

from app.realtime.tickets import TicketStore


class FakeRedis:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self.data: dict[str, tuple[str, float]] = {}
        self.calls: list[str] = []
        self.fail = False  # simulate an outage

    def _live(self, name: str) -> str | None:
        item = self.data.get(name)
        if item is None:
            return None
        value, expires = item
        if self._clock() >= expires:
            del self.data[name]
            return None
        return value

    async def set(self, name: str, value: str, *, ex: int, nx: bool) -> bool | None:
        self.calls.append("set")
        if self.fail:
            raise ConnectionError("simulated redis outage")
        if nx and self._live(name) is not None:
            return None
        self.data[name] = (value, self._clock() + ex)
        return True

    async def getdel(self, name: str) -> str | None:
        self.calls.append("getdel")
        if self.fail:
            raise ConnectionError("simulated redis outage")
        value = self._live(name)
        self.data.pop(name, None)
        return value


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def install_fake_tickets(app: Any, ttl: int = 30) -> tuple[FakeRedis, FakeClock]:
    """Swap the app's ticket store for the real `TicketStore` over a fake Redis."""
    clock = FakeClock()
    redis = FakeRedis(clock)
    app.state.ticket_store = TicketStore(redis, ttl)
    return redis, clock


def ws_path(session_id: str | uuid.UUID, ticket: str | None) -> str:
    base = f"/api/v1/ws/sessions/{session_id}"
    return base if ticket is None else f"{base}?ticket={ticket}"


def request_ticket(client: TestClient, headers: dict[str, str], session_id: str) -> str:
    response = client.post(
        "/api/v1/auth/ws-ticket", json={"session_id": session_id}, headers=headers
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    ticket: str = body["ticket"]
    return ticket
