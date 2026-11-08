"""Reject oversized upload bodies before they are fully received.

Starlette spools multipart bodies to disk while parsing, so without a limit an oversized
(or endless chunked) upload could consume disk. This pure-ASGI middleware rejects early based on
the declared Content-Length and also counts the bytes actually received.
"""

import json

from starlette.types import ASGIApp, Message, Receive, Scope, Send


class _BodyTooLarge(Exception):
    pass


class BodySizeLimitMiddleware:
    def __init__(self, app: ASGIApp, limits: dict[str, int]) -> None:
        self.app = app
        self.limits = limits  # exact request path -> maximum body bytes (POST only)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        limit = self.limits.get(scope.get("path", "")) if scope["type"] == "http" else None
        if limit is None or scope["method"] != "POST":
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
            raised_by_limit = isinstance(exc, _BodyTooLarge) or (
                isinstance(exc, BaseExceptionGroup) and exc.subgroup(_BodyTooLarge) is not None
            )
            if not raised_by_limit:
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
