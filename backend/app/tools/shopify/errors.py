"""
Errors raised by the read-only Shopify client (Task 14B.3).

Every message that leaves this module has been passed through `redact()` first: a provider
error body must never smuggle a client secret, an access token, or an authorization header
into a log line, an exception message, or a script output.
"""
from __future__ import annotations

from app.tools.shopify.sanitizer import redact, register_secret


class ShopifyError(Exception):
    """Base Shopify connectivity error. `status_code` follows the project's provider convention."""

    def __init__(self, message: str, status_code: int = 500, details: dict | None = None) -> None:
        super().__init__(redact(message))
        self.message = redact(message)
        self.status_code = status_code
        self.details = details or {}


class ShopifyConfigError(ShopifyError):
    """A required SHOPIFY_* setting is missing or empty — nothing was sent to Shopify."""

    def __init__(self, message: str) -> None:
        super().__init__(message, status_code=503)


class ShopifyAuthError(ShopifyError):
    """The client-credentials token exchange was refused. Never echoes any credential."""

    def __init__(self, message: str = "Shopify authentication failed.", status_code: int = 502) -> None:
        super().__init__(message, status_code=status_code)


class ShopifyRateLimitError(ShopifyError):
    """Shopify answered with HTTP 429. Read-only retry later; nothing is forced through."""

    def __init__(self, message: str = "Shopify rate limit reached (HTTP 429). Please retry shortly.") -> None:
        super().__init__(message, status_code=429)


class ShopifyGraphQLError(ShopifyError):
    """Shopify returned GraphQL-level errors for an otherwise-successful HTTP call."""

    def __init__(self, message: str, extensions: list[dict] | None = None) -> None:
        super().__init__(message, status_code=502, details={"extensions": extensions or []})
        self.extensions = extensions or []


class ShopifyMalformedResponseError(ShopifyError):
    """Shopify's answer could not be understood as the expected JSON shape."""

    def __init__(self, message: str = "Received a malformed response from Shopify.", details: dict | None = None) -> None:
        super().__init__(message, status_code=502, details=details)


class ShopifyTransportError(ShopifyError):
    """
    The HTTP exchange with Shopify did not produce a usable answer: a timeout, a connection
    failure, or a server-side HTTP error.

    Separated from every other error on purpose, because this is the one case where RESOLVE
    cannot say what happened to its own request — the bytes may have reached Shopify and been
    acted on before the answer was lost. A read can simply be tried again; a mutation may not.
    """

    def __init__(self, message: str, status_code: int = 504) -> None:
        super().__init__(message, status_code=status_code)


class ShopifyServerError(ShopifyTransportError):
    """Shopify answered with a server-side HTTP error, so the request's fate is unknown."""

    def __init__(self, message: str, status_code: int = 502) -> None:
        super().__init__(message, status_code=status_code)


class ShopifyReadOnlyViolation(ShopifyError):
    """
    A document this client is not permitted to send was refused locally (Task 14C.7N).

    The whole point of this error is that it happens before anything is built: no token exchange,
    no headers, no request body, no connection. So nothing here can have reached Shopify, and the
    message says so rather than pretending an answer came back.

    It carries no credential and no document text — only the operation kind, and the attempted
    operation name when that name is a plain GraphQL identifier. A refused document's own text is
    never quoted, because a document is caller-supplied data and could carry anything in it.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, status_code=500)


class ShopifyWriteCapabilityViolation(ShopifyError):
    """
    The isolated return-write capability refused a document it does not declare (Task 14C.7O).

    `ShopifyReturnWriteClient` may transmit exactly one mutation — the return operation RESOLVE
    declares — and anything else is turned down here, locally, before a token is exchanged or a
    request is built. So this error always means 'nothing happened at Shopify', which is why it is
    a plain ShopifyError rather than anything whose message could read as an unknown outcome: the
    return provider maps it onto a definite unavailability, never onto outcome-unknown.

    Like its read-only sibling it carries no credential and no document text — only the operation
    kind and, when it is a plain GraphQL identifier, the attempted operation name.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, status_code=500)


__all__ = [
    "ShopifyAuthError",
    "ShopifyConfigError",
    "ShopifyError",
    "ShopifyGraphQLError",
    "ShopifyMalformedResponseError",
    "ShopifyRateLimitError",
    "ShopifyReadOnlyViolation",
    "ShopifyServerError",
    "ShopifyTransportError",
    "ShopifyWriteCapabilityViolation",
    "register_secret",
]
