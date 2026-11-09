"""
The channel's Shopify gateway: its own credential, its own transport, its own failure vocabulary.

This is the only place in the package that talks to a merchant's store, and it is deliberately not
RESOLVE's client. RESOLVE's app holds five read scopes plus `write_returns`; the channel holds a
different app's write capability, and the two must never share a token, a settings module, or a
configuration name. The token is revealed only into the request header, and every error text is
masked before it can leave this module.

The credential is supplied by ``merchant_channel.auth``: a static token when one is configured
(no grant request is ever sent), otherwise the Dev Dashboard client-credentials grant with an
in-memory cache and a refresh margin. Either way the header is ``X-Shopify-Access-Token`` — the
granted tokens are opaque, and Shopify answers ``Authorization: Bearer`` for those with a
misleading 401. A GraphQL 401 re-authenticates once and sends the same request again; a second
refusal is a real one.

The failure vocabulary is what makes the rest of the channel honest about timeouts:

    GatewayDefiniteFailure   nothing was created. A retry is a new attempt, not a duplicate.
    GatewayOutcomeUnknown    the request may have reached Shopify, or its answer was lost.
                             A create is NOT allowed until a read-back reconciles it.

Which of the two applies is decided by what can still be true, not by how the error felt: a refusal
before the mutation (auth, bad request) can only mean no order; a lost answer cannot. The token
grant itself creates nothing, so a failure there is always definite. And a GraphQL error array that
arrives beside a mutation's payload is not a refusal: the mutation acted, and the payload's own
`userErrors` and node checks decide what it means.
"""
from __future__ import annotations

import httpx

from merchant_channel.auth import MerchantTokenProvider, TokenAcquisitionError
from merchant_channel.config import ChannelConfig, ChannelConfigError
from merchant_channel.documents import (
    DRAFT_ORDER_COMPLETE,
    DRAFT_ORDER_CREATE,
    READ_BACK_BY_CORRELATION,
    SHOP_IDENTITY,
    MerchantOrder,
    draft_order_input,
    parse_complete,
    parse_read_back,
    parse_shop,
    user_errors,
)


class GatewayDefiniteFailure(Exception):
    """Shopify refused before creating anything. Safe to treat as a failed attempt."""


class GatewayOutcomeUnknown(Exception):
    """The request may have landed and the answer is missing. Never retried blind."""


class ShopifyAdminGateway:
    """Minimal Admin GraphQL client for the merchant-owned app. Reads and the draft-order create."""

    def __init__(self, config: ChannelConfig, client: httpx.AsyncClient | None = None) -> None:
        if not config.store_domain or not config.api_version:
            raise ChannelConfigError(
                "The merchant channel needs its own store domain and API version; nothing was sent."
            )
        # Raises when neither credential path is configured; the static token wins when both are.
        config.credential_mode()
        self._config = config
        self._client = client
        self._url = (
            f"https://{config.store_domain}/admin/api/{config.api_version}/graphql.json"
        )
        self._tokens = MerchantTokenProvider(config, client=client)

    def _mask(self, text: object) -> str:
        return self._tokens.mask(text)

    async def _execute(
        self, document: str, variables: dict | None = None, *, payload_key: str | None = None
    ) -> dict:
        """
        Send one document and return its `data`.

        `payload_key` marks the call as a mutation and names its root field, and it changes how a
        top-level GraphQL `errors` array is read. GraphQL answers errors on a 200 alongside `data`,
        and the historical incident this module was rebuilt against is exactly that shape: a
        field-level ACCESS_DENIED *next to* a draft the mutation did create. Treating every error
        array as "nothing happened" marks an acted mutation as a definite failure, the outbox reopens
        the row, and the retry is the second order. So:

        - no `payload_key` (a read): errors mean the read told us nothing — definite, nothing to
          protect;
        - mutation, root payload absent (`data` is null or lacks the field): the root resolver
          refused before acting — definite;
        - mutation, root payload present: the mutation acted regardless of what the errors array
          says — the data is returned and the caller's own checks (`userErrors`, node presence)
          decide what it means.
        """
        client = self._client
        owns_client = client is None
        if owns_client:
            client = httpx.AsyncClient(timeout=httpx.Timeout(20.0))

        async def _send(token: str):
            return await client.post(
                self._url,
                json={"query": document, "variables": variables or {}},
                headers={
                    "X-Shopify-Access-Token": token,
                    "Content-Type": "application/json",
                },
            )

        try:
            try:
                token = await self._tokens.token()
                response = await _send(token)
                if response.status_code == 401:
                    # The cached token went stale between requests. Re-authenticate once and send
                    # the same request again; a second refusal with a fresh credential is real.
                    self._tokens.invalidate()
                    token = await self._tokens.token()
                    response = await _send(token)
                    if response.status_code == 401:
                        raise GatewayDefiniteFailure(
                            "Shopify answered HTTP 401 twice, including with a freshly acquired "
                            "credential, and the mutation was not accepted."
                        )
            except TokenAcquisitionError as exc:
                # The grant is authentication, not a purchase: nothing can have been created.
                raise GatewayDefiniteFailure(str(exc)) from exc
            except (httpx.TimeoutException, httpx.RequestError) as exc:
                # The request may already have reached Shopify. We cannot tell, and guessing is what
                # creates the second order.
                raise GatewayOutcomeUnknown(self._mask(f"{type(exc).__name__}: {exc}")) from exc
        finally:
            if owns_client:
                await client.aclose()

        if response.status_code in (401, 403, 422, 400):
            # A refusal to accept the request means no mutation ran.
            raise GatewayDefiniteFailure(
                f"Shopify answered HTTP {response.status_code} and the mutation was not accepted."
            )
        if response.status_code == 429 or response.status_code >= 500:
            raise GatewayOutcomeUnknown(
                f"Shopify answered HTTP {response.status_code}; the outcome of the request is "
                "unknown and will be reconciled by read-back, not retried."
            )
        payload = response.json()
        data = payload.get("data") or {}
        errors = payload.get("errors")
        if errors:
            if payload_key is None:
                raise GatewayDefiniteFailure(
                    "Shopify returned a GraphQL error: " + self._mask(str(errors))[:400]
                )
            acted = data.get(payload_key)
            if not isinstance(acted, dict):
                raise GatewayDefiniteFailure(
                    "Shopify returned a GraphQL error and no mutation payload for "
                    f"{payload_key}: " + self._mask(str(errors))[:400]
                )
        return data

    async def create_order(self, *, correlation_id: str, variant_gid: str, quantity: int):
        """
        Create a draft order stamped with the correlation, complete it, and return the real order.

        No payment argument is sent on either step: the channel has not been paid, so it does not
        record that it was.
        """
        created = await self._execute(
            DRAFT_ORDER_CREATE,
            {
                "input": draft_order_input(
                    correlation_id=correlation_id, variant_gid=variant_gid, quantity=quantity
                )
            },
            payload_key="draftOrderCreate",
        )
        errors = user_errors(created, "draftOrderCreate")
        if errors:
            raise GatewayDefiniteFailure(f"draftOrderCreate refused: {self._mask(errors)[:400]}")
        draft = (created.get("draftOrderCreate") or {}).get("draftOrder") or {}
        draft_id = draft.get("id")
        if not draft_id:
            # The payload came back but named no draft. Whether Shopify holds a draft from this
            # mutation is exactly what cannot be told from here — a definite failure would invite
            # the blind retry that becomes a second order.
            raise GatewayOutcomeUnknown(
                "draftOrderCreate answered without naming a draft order, so whether one exists is "
                "unknown until it is read back"
            )

        completed = await self._execute(DRAFT_ORDER_COMPLETE, {"id": draft_id}, payload_key="draftOrderComplete")
        errors = user_errors(completed, "draftOrderComplete")
        if errors:
            raise GatewayDefiniteFailure(f"draftOrderComplete refused: {self._mask(errors)[:400]}")
        order, _ = parse_complete(completed)
        if order is None:
            raise GatewayOutcomeUnknown(
                "the draft order completed without naming an order, so whether a purchase exists is "
                "unknown until it is read back"
            )
        return order

    async def read_back(self, correlation_id: str) -> list[MerchantOrder]:
        """Every order Shopify says carries this correlation. Zero, one, or many — reported as read."""
        data = await self._execute(
            READ_BACK_BY_CORRELATION, {"query": f"po_number:{correlation_id}"}
        )
        return parse_read_back(data)

    async def shop_identity(self) -> tuple[str | None, str | None]:
        return parse_shop(await self._execute(SHOP_IDENTITY))
