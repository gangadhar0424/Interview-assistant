"""Realtime settings: the approved defaults (D-41) and their validation. No database needed."""

import pytest

from app.core.config import ConfigurationError, load_settings


def test_defaults_are_the_approved_ones() -> None:
    s = load_settings()
    assert s.ws_ticket_ttl_seconds == 30
    assert s.ws_ticket_rate_limit_per_minute == 30
    assert s.ws_heartbeat_interval_seconds == 20
    assert s.ws_idle_timeout_seconds == 45
    assert s.ws_hello_timeout_seconds == 10
    assert s.ws_max_frame_bytes == 16 * 1024
    assert s.ws_max_messages_per_second == 10
    assert s.ws_max_connections_per_user == 3
    assert s.ws_max_total_connections == 200
    assert s.ws_send_queue_events == 1000  # 9B backpressure (D-42)
    assert s.ws_send_timeout_seconds == 10


def test_every_limit_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in {
        "WS_TICKET_TTL_SECONDS": "60",
        "WS_TICKET_RATE_LIMIT_PER_MINUTE": "5",
        "WS_HEARTBEAT_INTERVAL_SECONDS": "10",
        "WS_IDLE_TIMEOUT_SECONDS": "25",
        "WS_HELLO_TIMEOUT_SECONDS": "3",
        "WS_MAX_FRAME_BYTES": "4096",
        "WS_MAX_MESSAGES_PER_SECOND": "4",
        "WS_MAX_CONNECTIONS_PER_USER": "2",
        "WS_MAX_TOTAL_CONNECTIONS": "50",
        "WS_SEND_QUEUE_EVENTS": "25",
        "WS_SEND_TIMEOUT_SECONDS": "3",
    }.items():
        monkeypatch.setenv(name, value)
    s = load_settings()
    assert (s.ws_ticket_ttl_seconds, s.ws_max_frame_bytes, s.ws_max_total_connections) == (
        60,
        4096,
        50,
    )


@pytest.mark.parametrize(
    "env",
    [
        {"WS_IDLE_TIMEOUT_SECONDS": "20"},  # not above the 20 s heartbeat
        {"WS_MAX_CONNECTIONS_PER_USER": "5", "WS_MAX_TOTAL_CONNECTIONS": "4"},
        {"WS_TICKET_TTL_SECONDS": "0"},
        {"WS_TICKET_TTL_SECONDS": "100000"},
        {"WS_MAX_FRAME_BYTES": "10"},
        {"WS_MAX_MESSAGES_PER_SECOND": "0"},
        {"WS_SEND_QUEUE_EVENTS": "1"},
        {"WS_SEND_QUEUE_EVENTS": "10000000"},
        {"WS_SEND_TIMEOUT_SECONDS": "0"},
    ],
)
def test_invalid_values_fail_fast(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(ConfigurationError) as caught:
        load_settings()
    assert "WS_" in str(caught.value)
