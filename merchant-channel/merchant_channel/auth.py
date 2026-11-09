"""
The channel's Shopify authentication: a static token when supplied, a client-credentials grant
when it is not.

Two credential paths, decided once by ``ChannelConfig.credential_mode()`` and never mixed:

  * static — ``MERCHANT_SHOPIFY_ACCESS_TOKEN`` is used exactly as supplied, and no grant request
    is ever sent. This is the path the tests and controlled deployments use.
  * client-credentials — the current Shopify Dev Dashboard model: the channel exchanges
    ``MERCHANT_SHOPIFY_CLIENT_ID`` / ``MERCHANT_SHOPIFY_CLIENT_SECRET`` for a short-lived token at
    ``https://{store_domain}/admin/oauth/access_token``, caches it in memory, and refreshes it
    before it can go stale. Concurrent callers share one grant: an ``asyncio.Lock`` makes the
    refresh single-flight, so a burst of requests cannot mint a burst of grants.

The token this grant returns is opaque (``shpua_``/``shpat_``-shaped), and opaque tokens are
presented via ``X-Shopify-Access-Token`` — never ``Authorization: Bearer``, which Shopify ignores
for those tokens and answers with an unauthenticated-looking 401 (measured live against this same
grant from RESOLVE's side of the boundary; the two apps share the store, not their credentials).

The grant is authentication, not a purchase: it creates no order, so any grant failure — a
refusal, a timeout, a lost answer — is a *definite* failure and can never be an unknown outcome.
Error texts carry a status code or an exception class name and nothing else; every secret this
module knows is masked through ``mask()``, the package's redaction surface alongside ``Secret``.
"""
from __future__ import annotations

import asyncio
import time

import httpx

from merchant_channel.config import ChannelConfig

# A token is treated as stale this many seconds before Shopify says it expires, so it cannot die
# halfway through a request.
REFRESH_MARGIN_SECONDS = 60.0
# Only used when Shopify's answer omits ``expires_in``; the real value from the answer wins.
DEFAULT_EXPIRES_IN_SECONDS = 3600.0


class TokenAcquisitionError(Exception):
    """No token could be obtained. Credential-free by construction; nothing was created."""


class MerchantTokenProvider:
    """The channel's one source of Shopify credentials: static when configured, granted otherwise."""

    def __init__(
        self,
        config: ChannelConfig,
        client: httpx.AsyncClient | None = None,
        *,
        clock=time.monotonic,
        refresh_margin_seconds: float = REFRESH_MARGIN_SECONDS,
    ) -> None:
        self._mode = config.credential_mode()
        self._config = config
        self._client = client
        self._clock = clock
        self._margin = refresh_margin_seconds
        self._lock = asyncio.Lock()
        self._token: str | None = None
        self._expires_at: float = 0.0
        # Everything mask() must scrub from an error text: the client secret, the static token,
        # and every granted token this process has seen.
        self._secrets: list[str] = [
            value
            for value in (
                config.client_secret.reveal() if config.client_secret is not None else "",
                config.access_token.reveal() if config.access_token.configured else "",
            )
            if value
        ]

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def token_url(self) -> str:
        return f"https://{self._config.store_domain}/admin/oauth/access_token"

    def mask(self, text: object) -> str:
        value = str(text)
        for secret in self._secrets:
            if secret:
                value = value.replace(secret, "***")
        return value

    def invalidate(self) -> None:
        """Forget the cached token; the next ``token()`` re-grants. Static mode is unaffected."""
        self._token = None
        self._expires_at = 0.0

    async def token(self) -> str:
        if self._mode == "static":
            return self._config.access_token.reveal()
        if self._token is not None and self._clock() < self._expires_at - self._margin:
            return self._token
        async with self._lock:
            # A caller that waited on the lock re-checks: the grant it was waiting for may be
            # exactly the one that filled the cache.
            if self._token is not None and self._clock() < self._expires_at - self._margin:
                return self._token
            await self._acquire()
            assert self._token is not None
            return self._token

    async def _acquire(self) -> None:
        client = self._client
        owns_client = client is None
        if owns_client:
            client = httpx.AsyncClient(timeout=httpx.Timeout(20.0))
        try:
            response = await client.post(
                self.token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._config.client_id or "",
                    "client_secret": (
                        self._config.client_secret.reveal()
                        if self._config.client_secret is not None
                        else ""
                    ),
                },
            )
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            raise TokenAcquisitionError(
                f"the Shopify token grant did not answer ({type(exc).__name__}); nothing was created"
            ) from exc
        finally:
            if owns_client:
                await client.aclose()

        if response.status_code != 200:
            # Deliberately the status code and nothing else: the grant endpoint's error body can
            # carry request context, and no part of it belongs in this channel's errors.
            raise TokenAcquisitionError(
                f"the Shopify token grant was refused (HTTP {response.status_code}); verify "
                "MERCHANT_SHOPIFY_CLIENT_ID / MERCHANT_SHOPIFY_CLIENT_SECRET. Nothing was created."
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise TokenAcquisitionError(
                "the Shopify token grant answer was not valid JSON; nothing was created"
            ) from exc
        token = payload.get("access_token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token.strip():
            raise TokenAcquisitionError(
                "the Shopify token grant answer carried no access token; nothing was created"
            )

        self._token = token.strip()
        if self._token not in self._secrets:
            self._secrets.append(self._token)
        try:
            expires_in = float(payload.get("expires_in") or DEFAULT_EXPIRES_IN_SECONDS)
        except (TypeError, ValueError):
            expires_in = DEFAULT_EXPIRES_IN_SECONDS
        self._expires_at = self._clock() + expires_in

    def __repr__(self) -> str:
        return (
            f"MerchantTokenProvider(mode={self._mode!r}, "
            f"token_cached={self._token is not None})"
        )
