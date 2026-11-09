"""
One Shopify credential, one token cache, one typed-error POST — and no document policy (14C.7O).

Why this module exists: until now the read-only client was the only object that knew how to
authenticate and send an Admin GraphQL document, so the one write RESOLVE declares (`returnCreate`)
had to ride that same object. That made "read-only" a rule about which documents the class happened
to allow, rather than a property of the object — and 14C.7N's gate therefore needed an exception.

The exception is removed by moving the *mechanics* here and leaving the *policy* in the capability
classes that use it:

  * `ShopifyReadOnlyClient` (client.py) transmits documents only if they are one read query — with
    no allowance for any write, ever;
  * `ShopifyReturnWriteClient` (return_write_client.py) transmits only the single declared return
    write, and cannot be used to read.

Both share the credential VALUES from configuration, so this is one Shopify app, one client id, one
client secret and one short-lived token — never a second credential and never a second installed
app. Splitting a capability must not silently duplicate a secret.

Nothing in this module inspects what it is asked to send. That is deliberate: a policy-free sender
belongs to neither capability, and each one gates its documents here — before `execute` is reached.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from app.tools.shopify.errors import (
    ShopifyAuthError,
    ShopifyConfigError,
    ShopifyError,
    ShopifyGraphQLError,
    ShopifyMalformedResponseError,
    ShopifyRateLimitError,
    ShopifyServerError,
    ShopifyTransportError,
)
from app.tools.shopify.sanitizer import redact, register_secret

logger = logging.getLogger(__name__)

DEFAULT_API_VERSION = "2026-07"
DEFAULT_TIMEOUT_SECONDS = 15.0


class ShopifyAdminTransport:
    """
    Client-credentials auth and one Admin GraphQL POST against one Shopify store.

    It carries no opinion about the document: `execute` sends whatever a capability class has
    already decided it may send. Every refusal a document can earn is the caller's job, and every
    caller makes it before reaching here.
    """

    def __init__(
        self,
        *,
        store_domain: str,
        client_id: str,
        client_secret: str,
        api_version: str = DEFAULT_API_VERSION,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        # Blank/absent credentials are NOT an error here — the error surfaces when a call is
        # actually attempted, so importing or constructing without Shopify configured is safe.
        self.store_domain = (store_domain or "").strip()
        self._client_id = (client_id or "").strip()
        self._client_secret = (client_secret or "").strip()
        self.api_version = (api_version or DEFAULT_API_VERSION).strip()
        self.timeout_seconds = timeout_seconds
        # A test seam, not a production knob: tests inject httpx.MockTransport here.
        self._transport = transport
        # The access token is held ONLY in memory, never logged, never returned to callers.
        self._access_token: str | None = None
        self._token_is_jwt: bool = False
        self._token_expires_at: float = 0.0
        # Register whatever secret values exist so redact() can scrub them everywhere.
        register_secret(self._client_secret)
        register_secret(self._client_id)

    # ------------------------------------------------------------------ endpoints
    @property
    def token_url(self) -> str:
        return f"https://{self.store_domain}/admin/oauth/access_token"

    @property
    def graphql_url(self) -> str:
        return f"https://{self.store_domain}/admin/api/{self.api_version}/graphql.json"

    def validate_configuration(self) -> None:
        """Public, network-free check that all three SHOPIFY_* settings are present."""
        self._require_configured()

    # ------------------------------------------------------------------ auth
    async def obtain_access_token(self) -> bool:
        """
        Exchange client credentials for a short-lived access token (client_credentials grant).
        The token is kept in memory only. Returns True on success; raises on any failure —
        always with a credential-free message.
        """
        self._require_configured()
        if self._access_token and time.monotonic() < self._token_expires_at - 60:
            return True

        data = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds, transport=self._transport) as client:
                resp = await client.post(self.token_url, data=data)
        except httpx.TimeoutException as exc:
            raise ShopifyError("Shopify authentication timed out.", status_code=504) from exc
        except httpx.RequestError as exc:
            raise ShopifyError(
                f"Network error while authenticating with Shopify: {redact(str(exc))}", status_code=502
            ) from exc

        if resp.status_code != 200:
            # Deliberately report only the status code: Shopify's auth error bodies can carry
            # request context, and we do not forward them.
            logger.error("Shopify token endpoint returned HTTP %s", resp.status_code)
            if resp.status_code == 429:
                raise ShopifyRateLimitError()
            raise ShopifyAuthError(
                f"Shopify authentication was refused (HTTP {resp.status_code}). "
                "Verify SHOPIFY_CLIENT_ID / SHOPIFY_CLIENT_SECRET configuration.",
                status_code=502,
            )

        try:
            payload = resp.json()
        except ValueError as exc:
            raise ShopifyMalformedResponseError(
                "Shopify's authentication response was not valid JSON."
            ) from exc
        if not isinstance(payload, dict):
            raise ShopifyMalformedResponseError("Shopify's authentication response had an unexpected shape.")

        token = payload.get("access_token")
        register_secret(token if isinstance(token, str) else None)
        if not isinstance(token, str) or not token.strip():
            raise ShopifyMalformedResponseError("Shopify's authentication response carried no access token.")

        self._access_token = token.strip()
        # Header scheme depends on the token shape (verified live against the Dev store):
        # Managed-API JWTs go as `Authorization: Bearer`; opaque store tokens (shpat_/shpua_)
        # are presented via the legacy `X-Shopify-Access-Token` header — Shopify ignores the
        # Bearer form for those and answers an unauthenticated-looking 401.
        self._token_is_jwt = self._access_token.count(".") == 2
        try:
            expires_in = float(payload.get("expires_in") or 3600)
        except (TypeError, ValueError):
            expires_in = 3600.0
        self._token_expires_at = time.monotonic() + expires_in
        logger.info("Shopify access token obtained (value not logged).")
        return True

    # ------------------------------------------------------------------ transport
    async def execute(self, document: str, variables: dict[str, Any] | None = None) -> dict:
        """
        Send one already-permitted document and return its `data` dict.

        Raises a typed, credential-free error for transport failures, HTTP errors (incl. 429),
        GraphQL-level errors and malformed answers. The document text is never logged.
        """
        if not self._access_token or time.monotonic() >= self._token_expires_at - 60:
            await self.obtain_access_token()

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self._token_is_jwt:
            headers["Authorization"] = f"Bearer {self._access_token}"
        else:
            headers["X-Shopify-Access-Token"] = self._access_token
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds, transport=self._transport) as client:
                resp = await client.post(
                    self.graphql_url,
                    json={"query": document, "variables": variables or {}},
                    headers=headers,
                )
        except httpx.TimeoutException as exc:
            # A timeout after the request was handed to the transport: Shopify may or may not have
            # acted on it. Callers that mutate must treat this as an unknown outcome, so it gets
            # its own error type rather than reading as "nothing happened".
            raise ShopifyTransportError("Shopify GraphQL request timed out.", status_code=504) from exc
        except httpx.RequestError as exc:
            raise ShopifyTransportError(
                f"Network error while querying Shopify: {redact(str(exc))}", status_code=502
            ) from exc

        if resp.status_code == 401 or resp.status_code == 403:
            self._access_token = None  # force a fresh exchange on the next attempt
            raise ShopifyAuthError(
                f"Shopify refused the request as unauthenticated (HTTP {resp.status_code}).",
                status_code=502,
            )
        if resp.status_code == 429:
            raise ShopifyRateLimitError()
        if resp.status_code >= 500:
            # Shopify's own side failed. The request was transmitted, so a mutation's fate is
            # unknown — reported as a transport-class error for exactly that reason.
            raise ShopifyServerError(
                f"Shopify returned HTTP {resp.status_code}.", status_code=502
            )
        if resp.status_code >= 400:
            raise ShopifyError(
                f"Shopify returned HTTP {resp.status_code}.", status_code=502
            )

        try:
            payload = resp.json()
        except ValueError as exc:
            raise ShopifyMalformedResponseError(
                "Shopify returned a non-JSON GraphQL response.", details={"phase": "graphql"}
            ) from exc
        if not isinstance(payload, dict):
            raise ShopifyMalformedResponseError(
                "Shopify's GraphQL response had an unexpected shape.", details={"phase": "graphql"}
            )

        errors = payload.get("errors")
        if errors:
            messages: list[str] = []
            extensions: list[dict] = []
            if isinstance(errors, list):
                for item in errors:
                    if isinstance(item, dict):
                        messages.append(str(item.get("message") or "unknown GraphQL error"))
                        if isinstance(item.get("extensions"), dict):
                            extensions.append(item["extensions"])
                    else:
                        messages.append(str(item))
            else:
                messages.append(str(errors))
            joined = "; ".join(redact(m) for m in messages)
            logger.error("Shopify GraphQL errors: %s", joined)
            raise ShopifyGraphQLError(f"Shopify GraphQL error: {joined}", extensions=extensions)

        data = payload.get("data")
        if not isinstance(data, dict):
            raise ShopifyMalformedResponseError(
                "Shopify's GraphQL response carried no data object.",
                details={"phase": "graphql"},
            )
        return data

    # ------------------------------------------------------------------ configuration
    def _require_configured(self) -> None:
        missing = [
            name
            for name, value in (
                ("SHOPIFY_STORE_DOMAIN", self.store_domain),
                ("SHOPIFY_CLIENT_ID", self._client_id),
                ("SHOPIFY_CLIENT_SECRET", self._client_secret),
            )
            if not value
        ]
        if missing:
            raise ShopifyConfigError(
                "Shopify is not configured — set " + ", ".join(missing) + " in the environment."
            )


__all__ = ["ShopifyAdminTransport", "DEFAULT_API_VERSION", "DEFAULT_TIMEOUT_SECONDS"]
