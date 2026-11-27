"""Reject oversized upload bodies before they are fully received.

Starlette spools multipart bodies to disk while parsing, so without a limit an oversized
(or endless chunked) upload could consume disk. This pure-ASGI middleware rejects early based on
the declared Content-Length and also counts the bytes actually received.
"""

import json
import re
from collections.abc import Sequence

from starlette.types import ASGIApp, Message, Receive, Scope, Send


class _BodyTooLarge(Exception):  # noqa: N818 - internal control-flow signal, not an error type
    pass


def is_body_limit_signal(exc: BaseException) -> bool:
    """True for the internal "request body too large" signal (maybe wrapped in an exception group).

    It is raised inside the request stream and answered with a 413 by BodySizeLimitMiddleware, the
    outermost layer. No inner layer (such as the catch-all in main.py) may swallow it.
    """
    return isinstance(exc, _BodyTooLarge) or (
        isinstance(exc, BaseExceptionGroup) and exc.subgroup(_BodyTooLarge) is not None
    )


class BodySizeLimitMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        limits: dict[str, int],
        pattern_limits: Sequence[tuple[re.Pattern[str], int]] = (),
        default_limit: int | None = None,
    ) -> None:
        self.app = app
        self.limits = limits  # exact request path -> maximum body bytes (POST only)
        # Full-match path patterns for paths that carry an id (e.g. /sessions/<id>/screenshots).
        self.pattern_limits = tuple(pattern_limits)
        # Cap for every OTHER write (POST, PUT, PATCH) under /api/, i.e. the JSON endpoints (login,
        # signup, handoff redeem, session create/edit, questions, job import). None disables it.
        self.default_limit = default_limit

    def _limit_for(self, path: str, method: str) -> int | None:
        if method == "POST":
            exact = self.limits.get(path)
            if exact is not None:
                return exact
            for pattern, limit in self.pattern_limits:
                if pattern.fullmatch(path):
                    return limit
        if method in ("POST", "PUT", "PATCH") and path.startswith("/api/"):
            return self.default_limit
        return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        limit = (
            self._limit_for(scope.get("path", ""), scope["method"])
            if scope["type"] == "http"
            else None
        )
        if limit is None:
            await self.app(scope, receive, send)
            return

        declared = dict(scope["headers"]).get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > limit:
            await self._reject(send)
            return

        received = 0
        response_started = False

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise _BodyTooLarge
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except (_BodyTooLarge, BaseExceptionGroup) as exc:
            # Inner BaseHTTPMiddleware runs the app in an anyio task group, which wraps errors
            # in an exception group; accept either form but re-raise anything else untouched.
            if not is_body_limit_signal(exc):
                raise
            if not response_started:
                await self._reject(send)

    @staticmethod
    async def _reject(send: Send) -> None:
        body = json.dumps(
            {
                "error": {
                    "code": "file_too_large",
                    "message": "The upload is too large.",
                    "request_id": None,
                }
            }
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"connection", b"close"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
