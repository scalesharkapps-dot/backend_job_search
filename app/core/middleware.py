"""HTTP middleware: request IDs, access logging, request-size limits, security headers."""

from __future__ import annotations

import logging
import re
import time
import uuid

from starlette.exceptions import HTTPException
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import get_settings
from app.core.exceptions import error_body
from app.core.logging import request_id_ctx

logger = logging.getLogger("sharkhire.access")

_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9\-_.]{8,64}$")


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assign a request ID, enforce body size limits and log every request.

    Only method, path (never the query string), status, and duration are logged,
    so tokens or personal data in query strings or bodies never reach the logs.
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        incoming = request.headers.get("x-request-id", "")
        request_id = incoming if _VALID_REQUEST_ID.match(incoming) else uuid.uuid4().hex
        token = request_id_ctx.set(request_id)
        start = time.perf_counter()
        status_code = 500
        try:
            length = request.headers.get("content-length")
            if length is not None:
                try:
                    too_large = int(length) > get_settings().MAX_REQUEST_BODY_BYTES
                except ValueError:
                    too_large = True
                if too_large:
                    response: Response = JSONResponse(
                        status_code=413,
                        content=error_body("payload_too_large", "The request body is too large."),
                    )
                    response.headers["X-Request-ID"] = request_id
                    status_code = 413
                    return response
            response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Request-ID"] = request_id
            response.headers.setdefault("X-Content-Type-Options", "nosniff")
            response.headers.setdefault("X-Frame-Options", "DENY")
            response.headers.setdefault("Referrer-Policy", "no-referrer")
            return response
        finally:
            duration_ms = round((time.perf_counter() - start) * 1000, 2)
            logger.info(
                "%s %s -> %s (%.2f ms)",
                request.method,
                request.url.path,
                status_code,
                duration_ms,
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": status_code,
                    "duration_ms": duration_ms,
                },
            )
            request_id_ctx.reset(token)


class BodySizeLimitMiddleware:
    """Pure-ASGI guard that counts streamed body bytes.

    The Content-Length check in ``RequestContextMiddleware`` rejects declared
    oversized bodies early; this middleware also stops chunked uploads (no
    Content-Length) once they exceed ``MAX_REQUEST_BODY_BYTES``.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = get_settings().MAX_REQUEST_BODY_BYTES
        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    # HTTPException is re-raised by FastAPI's body parsing and
                    # rendered by the standard error envelope handler.
                    raise HTTPException(status_code=413, detail="The request body is too large.")
            return message

        await self.app(scope, limited_receive, send)
