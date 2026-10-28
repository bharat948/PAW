import logging
import time
import uuid

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from paw_server.core.logging import correlation_id_var

log = logging.getLogger("paw.request")
HEADER = b"x-correlation-id"


class CorrelationMiddleware:
    """Assigns a correlation ID per request, echoes it, and logs request latency."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = dict(scope["headers"]).get(HEADER, b"").decode()[:64]
        cid = (
            incoming
            if incoming.isascii() and incoming.isprintable() and incoming
            else uuid.uuid4().hex
        )
        token = correlation_id_var.set(cid)
        start = time.perf_counter()
        status = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message.setdefault("headers", []).append((HEADER, cid.encode()))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            log.info(
                "request",
                extra={
                    "method": scope["method"],
                    "path": scope["path"],
                    "status": status,
                    "latency_ms": round((time.perf_counter() - start) * 1000, 1),
                },
            )
            correlation_id_var.reset(token)
