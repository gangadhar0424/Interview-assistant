"""Phase 9C over REAL sockets (uvicorn in a thread, real WebSocket clients): reconnect and resume,
resync after a slow-consumer close, and the load smoke (N concurrent connections, default 50, with
fake providers; half of them drop and resume mid-answer).

Run the load size with LOAD_SMOKE_N (default 50). These tests need the real server and take a while.
"""

import json
import os
import re
import socket
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from fastapi.testclient import TestClient
from websockets.sync.client import ClientConnection

from tests.conftest import RunSql
from tests.helpers import as_legacy, register  # noqa: F401
from tests.qa_fakes import RecordingProvider, SlowTokensProvider
from tests.test_qa_service import answers, questions
from tests.test_realtime_live import live as live  # noqa: F401  (fixture reused)
from tests.test_realtime_qa_live import (  # noqa: F401  (fixtures reused)
    FloodProvider,
    Person,
    Rig,
    read_until,
    rig_factory,
    say,
    small_buffers,
    small_grace,
)
from tests.test_sessions import body, create, new_file

TERMINAL = ("answer.done", "answer.error", "answer.cancelled")
LOAD_N = int(os.environ.get("LOAD_SMOKE_N", "50"))


@pytest.fixture
def rigs(request: pytest.FixtureRequest) -> Callable[[Any], Rig]:
    factory: Callable[[Any], Rig] = request.getfixturevalue("rig_factory")
    return factory


def until_deltas(ws: ClientConnection, count: int, timeout: float = 20.0) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []
    deadline = time.monotonic() + timeout
    while sum(1 for m in seen if m["type"] == "answer.delta") < count:
        seen.append(json.loads(ws.recv(timeout=max(0.1, deadline - time.monotonic()))))
    return seen


def drop(ws: ClientConnection) -> None:
    """The TCP connection just disappears: no close frame."""
    ws.socket.shutdown(socket.SHUT_RDWR)
    ws.socket.close()


def streamed(events: list[dict[str, Any]]) -> str:
    return "".join(m["payload"]["text"] for m in events if m["type"] == "answer.delta")


def assert_consecutive(events: list[dict[str, Any]]) -> None:
    numbers = [m["seq"] for m in events if m["seq"] > 0]
    assert numbers == list(range(numbers[0], numbers[0] + len(numbers))), numbers


# ----------------------------------------------------------------------------- resume
def test_resume_over_a_real_socket_after_an_abrupt_drop(rigs: Callable[[Any], Rig]) -> None:
    provider = SlowTokensProvider(token_delay=0.03, tokens=60)
    rig = rigs(provider)
    first = rig.open(rig.alice)
    say(first)
    head = until_deltas(first, 3)
    drop(first)  # mid-answer; the generation is NOT cancelled

    second = rig.open(rig.alice, resume_from_seq=head[-1]["seq"])
    try:
        tail = read_until(second, *TERMINAL, timeout=30)
    finally:
        second.close()
    events = head + tail
    assert tail[0]["seq"] == head[-1]["seq"] + 1
    assert_consecutive(events)  # no gap and no duplicate across the reconnect
    assert [m["type"] for m in events].count("answer.done") == 1
    (stored,) = answers(rig.run_sql)
    assert stored["status"] == "complete" and stored["text"] == streamed(events)
    (item,) = rig.history(rig.alice)  # the source of truth agrees
    assert item["answers"][0]["status"] == "complete"
    assert item["answers"][0]["text"] == streamed(events)
    assert provider.finished


@pytest.mark.usefixtures("small_buffers")
def test_after_a_slow_consumer_close_the_client_resyncs_from_the_history(
    rigs: Callable[[Any], Rig],
) -> None:
    provider = FloodProvider()
    rig = rigs(provider)
    slow = rig.open(rig.alice)
    say(slow)  # the client reads nothing and is cut with 4010
    assert wait(lambda: 4010 in rig.records.closed_codes())
    assert wait(lambda: provider.finished)
    # It comes back asking for "everything after seq 1": the buffer cannot hold that any more.
    again = rig.open(rig.alice, resume_from_seq=1)
    try:
        error = json.loads(again.recv(timeout=10))
        assert error["type"] == "error" and error["payload"]["code"] == "resume_unavailable"
        assert error["payload"]["fatal"] is False
        assert rig.ready["payload"]["resume_seq"] > 1  # the baseline to continue from
    finally:
        again.close()
    # ... and the full answer is there (the final write lands just after the provider finishes)
    assert wait(lambda: rig.history(rig.alice)[0]["answers"][0]["status"] == "complete")
    (item,) = rig.history(rig.alice)
    assert len(item["answers"][0]["text"]) == provider.tokens * provider.size


def wait(condition: Callable[[], bool], timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return condition()


# ----------------------------------------------------------------------------- load smoke
@pytest.fixture
def load_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WS_MAX_CONNECTIONS_PER_USER", str(LOAD_N + 10))
    monkeypatch.setenv("WS_MAX_TOTAL_CONNECTIONS", str(LOAD_N * 4))
    monkeypatch.setenv("WS_TICKET_RATE_LIMIT_PER_MINUTE", "600")
    monkeypatch.setenv("WS_RESUME_GRACE_SECONDS", "10")  # reconnects under load take seconds


def test_load_smoke_concurrent_connections_with_a_reconnect_storm(
    load_settings: None,
    rigs: Callable[[Any], Rig],
    client: TestClient,
    run_sql: RunSql,
) -> None:
    def reply(messages: Any) -> str:
        marker = re.search(r"LOAD-Q\d+", messages[-1].content)
        assert marker is not None
        return f"ANSWER-FOR-{marker.group(0)} " + " ".join(f"w{i}" for i in range(20))

    provider = RecordingProvider(first_token_delay=0.3, reply_fn=reply)
    rig = rigs(provider)
    people = [rig.alice]
    while len(people) < LOAD_N:
        resume = new_file(client, run_sql, rig.alice.headers, "resumes")
        response = create(client, rig.alice.headers, body(resume, []))
        assert response.status_code == 201, response.text
        people.append(Person(rig.alice.headers, as_legacy(str(response.json()["id"]))))
    uid = rig.user_id(rig.alice)

    def one_client(index: int) -> tuple[int, list[dict[str, Any]], float]:
        started = time.monotonic()
        ws = rig.open(people[index])
        say(ws, f"LOAD-Q{index} tell me about yourself")
        if index % 2 == 0:  # the reconnect storm: drop right after the question was created
            first = read_until(ws, "question.created", timeout=30)
            drop(ws)
            ws = rig.open(people[index], resume_from_seq=first[-1]["seq"])
            events = first + read_until(ws, *TERMINAL, timeout=60)
        else:
            events = read_until(ws, *TERMINAL, timeout=60)
        ws.close()
        return index, events, time.monotonic() - started

    began = time.monotonic()
    with ThreadPoolExecutor(max_workers=LOAD_N) as pool:
        outcomes = list(pool.map(one_client, range(LOAD_N)))
    elapsed = time.monotonic() - began

    for index, events, _ in outcomes:
        assert_consecutive(events)
        assert events[0]["seq"] == 1 and events[0]["type"] == "question.created"
        assert [m["type"] for m in events].count("answer.done") == 1, index
        assert events[-1]["type"] == "answer.done"
        text = streamed(events)
        assert text.startswith(f"ANSWER-FOR-LOAD-Q{index} ")  # its own answer ...
        assert set(re.findall(r"LOAD-Q\d+", text)) == {f"LOAD-Q{index}"}  # ... and nobody else's

    stored = questions(run_sql)
    assert len(stored) == LOAD_N and {q["raw_text"][:9] for q in stored} >= {
        "LOAD-Q0 t",
        "LOAD-Q1 t",
    }
    assert sorted(a["status"] for a in answers(run_sql)) == ["complete"] * LOAD_N
    owners = {(str(q["user_id"]), str(q["session_id"])) for q in stored}
    assert owners == {(str(uid), p.session_id) for p in people}  # each in its own session
    assert max(latency for *_, latency in outcomes) < 45 and elapsed < 90
    assert wait(lambda: rig.limits.active(uid) == 0, timeout=10)
    assert wait(lambda: rig.registry_size() == 0, timeout=10)
    channels = rig.live.app.state.session_channels
    assert wait(lambda: len(channels) == 0, timeout=30)  # every grace period ended: nothing leaks
    assert channels.detached_count() == 0
