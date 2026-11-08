"""Usage metering: one record per provider attempt. Never contains prompt or answer text."""

import logging
import uuid
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger("app.ai.usage")


@dataclass(frozen=True, slots=True)
class UsageRecord:
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    ok: bool
    error_code: str | None = None
    user_id: uuid.UUID | None = None


class UsageSink(Protocol):
    def record(self, record: UsageRecord) -> None: ...


class LoggingUsageSink:
    """Default sink: structured log line with identifiers and counts only (no content)."""

    def record(self, record: UsageRecord) -> None:
        logger.info(
            "llm_usage",
            extra={
                "provider": record.provider,
                "model": record.model,
                "input_tokens": record.input_tokens,
                "output_tokens": record.output_tokens,
                "latency_ms": record.latency_ms,
                "ok": record.ok,
                "error_code": record.error_code,
                "user_id": str(record.user_id) if record.user_id else None,
            },
        )


class MemoryUsageSink:
    """Collects records in memory (tests)."""

    def __init__(self) -> None:
        self.records: list[UsageRecord] = []

    def record(self, record: UsageRecord) -> None:
        self.records.append(record)
