"""SEC-3 regression: every JSON write under /api/ has a body cap, not only the upload paths."""

import asyncio
import json
from typing import Any

from starlette.types import Message, Receive, Scope, Send

from app.core.body_limit import BodySizeLimitMiddleware

DEFAULT = 1000


async def echo_length(scope: Scope, receive: Receive, send: Send) -> None:
    total = 0
    while True:
        message = await receive()
        total += len(message.get("body", b""))
        if not message.get("more_body", False):
            break
    body = json.dumps({"received": total}).encode()
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": body})


def call(
    path: str,
    chunks: list[bytes],
    *,
    method: str = "POST",
    declared: int | None = None,
    default_limit: int | None = DEFAULT,
) -> int:
    app = BodySizeLimitMiddleware(
        echo_length, {"/api/v1/resumes": 5000}, default_limit=default_limit
    )
    headers = [(b"content-length", str(declared).encode())] if declared is not None else []
    scope: Scope = {"type": "http", "method": method, "path": path, "headers": headers}
    queue = list(chunks)

    async def receive() -> Message:
        body = queue.pop(0) if queue else b""
        return {"type": "http.request", "body": body, "more_body": bool(queue)}

    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    start: Any = next(m for m in sent if m["type"] == "http.response.start")
    return int(start["status"])


def test_unauthenticated_json_login_body_over_the_default_is_refused_early() -> None:
    assert call("/api/v1/auth/login", [b"x"], declared=DEFAULT + 1) == 413


def test_chunked_json_body_without_a_declared_length_is_counted() -> None:
    assert call("/api/v1/auth/login", [b"x" * 600, b"x" * 600]) == 413


def test_patch_and_put_are_covered() -> None:
    assert call("/api/v1/sessions/abc", [b"x" * 2000], method="PATCH") == 413
    assert call("/api/v1/sessions/abc", [b"x" * 2000], method="PUT") == 413


def test_a_normal_json_body_passes() -> None:
    assert call("/api/v1/auth/login", [b"x" * 500]) == 200


def test_upload_paths_keep_their_own_larger_limit() -> None:
    assert call("/api/v1/resumes", [b"x" * 3000]) == 200
    assert call("/api/v1/resumes", [b"x" * 6000]) == 413


def test_reads_and_non_api_paths_are_not_limited() -> None:
    assert call("/api/v1/sessions", [b"x" * 5000], method="GET") == 200
    assert call("/health", [b"x" * 5000]) == 200


def test_default_limit_is_off_unless_configured() -> None:
    assert call("/api/v1/auth/login", [b"x" * 5000], default_limit=None) == 200
