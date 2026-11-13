"""
P2.1 — request-body resource protection for the evidence supplement endpoint.

Enforcement point in the ASGI request lifecycle: a pure-ASGI middleware in the
Starlette user-middleware layer (between ServerErrorMiddleware and
ExceptionMiddleware). For POSTs to the supplement path it wraps `receive` BEFORE the
application is invoked, so every request-body byte is counted as it arrives from the
server and the limit is enforced before Starlette's multipart parser can spool
anything to disk. Two enforcement layers:

  Layer 1 — Content-Length pre-check. Read from the ASGI scope headers before the
  first body byte; a declared body over the limit is refused with 413 without
  invoking the application and without reading a single byte.

  Layer 2 — counting receive wrapper. Each `http.request` chunk is counted as it
  arrives; the wrapper raises the internal RequestBodyTooLarge exception BEFORE
  returning the chunk that would exceed the limit, so no oversized byte is ever
  handed to the application and the only buffering anywhere is the bounded,
  limit-sized prefix the parser legitimately consumed. The middleware catches its
  own exception around the application call and converts it to 413, so it never
  reaches ServerErrorMiddleware — the client gets 413, never a 500.

The threshold is the configured evidence batch limit plus a fixed multipart framing
allowance (boundaries, part headers), matching the endpoint's own Content-Length
pre-check so the two layers agree on one constant. The limit is enforced whether or
not the evidence store is configured — resource protection belongs to the endpoint,
not to the storage backend.
"""
from __future__ import annotations

import json
import re

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import get_settings
from app.evidence_store import EvidenceLimits

_SUPPLEMENT_PATH = re.compile(r"^/missions/[^/]+/order/issues/[^/]+/supplement$")

# Multipart framing overhead (boundaries + part headers) tolerated beyond the
# evidence batch limit — the same allowance the endpoint's Content-Length pre-check
# uses; single source of truth lives here.
FRAMING_ALLOWANCE_BYTES = 65536


class RequestBodyTooLarge(Exception):
    """Internal middleware signal raised by the counting receive wrapper; caught by
    the middleware itself and converted to HTTP 413 — never visible to the app."""


def _raw_body_limit() -> int:
    return (
        EvidenceLimits.from_settings(get_settings()).max_request_bytes
        + FRAMING_ALLOWANCE_BYTES
    )


def _declared_content_length(scope: Scope) -> int | None:
    # First Content-Length header wins; a declared value over the limit is refused
    # conservatively (layer 2 still counts the real bytes for any remaining case).
    for name, value in scope.get("headers") or ():
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


class EvidenceBodyLimitMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope.get("type") != "http"
            or scope.get("method") != "POST"
            or _SUPPLEMENT_PATH.match(scope.get("path") or "") is None
        ):
            await self.app(scope, receive, send)
            return

        limit = _raw_body_limit()
        declared = _declared_content_length(scope)
        if declared is not None and declared > limit:
            await self._reject(send, limit)
            return

        state = {"seen": 0, "response_started": False}

        async def counting_receive() -> Message:
            message = await receive()
            if message["type"] == "http.request":
                body = message.get("body", b"")
                if body and state["seen"] + len(body) > limit:
                    # Refuse BEFORE delivering the chunk: no byte past the limit is
                    # ever handed to the application or its parser.
                    raise RequestBodyTooLarge(
                        f"request body exceeded the {limit}-byte limit while streaming"
                    )
                state["seen"] += len(body)
            return message

        async def tracking_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                state["response_started"] = True
            await send(message)

        try:
            await self.app(scope, counting_receive, tracking_send)
        except RequestBodyTooLarge:
            if not state["response_started"]:
                await self._reject(send, limit)

    async def _reject(self, send: Send, limit: int) -> None:
        body = json.dumps(
            {
                "detail": (
                    "the supplement exceeds the "
                    f"{limit - FRAMING_ALLOWANCE_BYTES}-byte request limit"
                )
            }
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
