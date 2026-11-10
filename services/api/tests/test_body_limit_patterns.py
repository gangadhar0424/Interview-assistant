"""Phase 11A: the body-size middleware also limits id-bearing paths (pattern limits), pure ASGI."""

import asyncio
import json
import re
from typing import Any

from starlette.types import Message, Receive, Scope, Send

from app.core.body_limit import BodySizeLimitMiddleware

SESSION = "11111111-2222-4333-8444-555555555555"
PATH = f"/api/v1/sessions/{SESSION}/screenshots"
PATTERN = re.compile(r"/api/v1/sessions/[0-9a-fA-F-]{36}/screenshots")


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
    path: str, chunks: list[bytes], *, declared: int | None = None, method: str = "POST"
) -> tuple[int, dict[str, Any]]:
    app = BodySizeLimitMiddleware(echo_length, {"/api/v1/resumes": 5000}, [(PATTERN, 1000)])
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
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    body = next((m["body"] for m in sent if m["type"] == "http.response.body"), b"{}")
    return int(status), json.loads(body)


def test_a_declared_oversize_body_on_the_screenshot_path_is_refused_early() -> None:
    status, body = call(PATH, [b"x"], declared=5000)
    assert status == 413 and body["error"]["code"] == "file_too_large"


def test_an_undeclared_chunked_body_is_counted_and_refused() -> None:
    status, _ = call(PATH, [b"x" * 600, b"x" * 600])
    assert status == 413


def test_a_body_within_the_limit_passes_through() -> None:
    assert call(PATH, [b"x" * 900], declared=900) == (200, {"received": 900})


def test_other_paths_and_methods_and_malformed_ids_are_not_affected() -> None:
    big = [b"x" * 3000]
    assert call("/api/v1/sessions/not-a-uuid/screenshots", big)[0] == 200
    assert call(f"{PATH}/extra", big)[0] == 200
    assert call(PATH, big, method="GET")[0] == 200
    assert call("/api/v1/resumes", [b"x" * 6000])[0] == 413  # exact-path limits still work
