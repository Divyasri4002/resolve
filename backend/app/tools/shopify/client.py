"""
Read-only Shopify Admin GraphQL client (Task 14B.3; gate added 14C.7N, made absolute 14C.7O).

Scope of this module — and its ONLY scope: authenticate with Shopify's client-credentials grant,
run read queries (products, orders, returns, fulfillment line items), and normalize the answers.

**This class has no write path at all.** `execute_graphql` classifies the document it was handed as
its first act — before the token exchange, before headers, before the request body, before the
transport — and transmits it only if it is exactly one root read query. There is no allowlist here,
no declared-write exception, and no method on this class that will carry a write, so nothing in
RESOLVE can widen this object by handing it a different document. A `mutation`, a `subscription`, a
malformed or unclassifiable document, or a document with more than one root operation raises
`ShopifyReadOnlyViolation` locally with no request ever built. That is what makes the read-only
boundary a property of code rather than a convention about which documents the methods below happen
to pass.

RESOLVE's one declared write lives somewhere else, on purpose. `ShopifyReturnWriteClient`
(`app/tools/shopify/return_write_client.py`) is a separate capability object over the SAME
configured credential — one Shopify app, one client id, one secret, one short-lived token — and it
carries only the return mutation. The auth and HTTP mechanics both capabilities use live in
`app/tools/shopify/admin_transport.py`, which holds no document policy of its own. Separating a
capability must never mean duplicating a secret.

Nothing here is wired into the refund/replacement/return executors or the Resolution Orchestrator,
and this task must not change that.

Credentials come from configuration (environment only). The access token lives in memory on the
transport instance, is never returned to callers, and never reaches a log line or an exception
message — every outbound string goes through redact() first. The refusal message names a document's
kind, never its text.
"""
from __future__ import annotations

import logging
from typing import Any

from app.tools.shopify.admin_transport import (
    DEFAULT_API_VERSION,
    DEFAULT_TIMEOUT_SECONDS,
    ShopifyAdminTransport,
)
from app.tools.shopify.document_gate import classify
from app.tools.shopify.errors import (
    ShopifyConfigError,
    ShopifyMalformedResponseError,
    ShopifyReadOnlyViolation,
)
from app.tools.shopify.models import (
    ShopifyCatalog,
    ShopifyCatalogProduct,
    ShopifyFulfillmentLineItem,
    ShopifyOrder,
    ShopifyOrderLineRef,
    ShopifyOrderLineIdentity,
    ShopifyOrderReference,
    ShopifyOrderReturnTargets,
    ShopifyProduct,
    ShopifyReturn,
    ShopifyShippedFulfillmentLine,
)

logger = logging.getLogger(__name__)

_PRODUCTS_QUERY = """
query ResolveReadProducts($first: Int!) {
  products(first: $first) {
    edges { node { id title handle status priceRangeV2 { minVariantPrice { amount currencyCode } } } }
  }
}
"""

# The catalog read product discovery uses (Task 14C.7D). Two things are deliberate here. The store
# itself is read in the same document as its products, so a Product GID can never be recorded
# without the store that issued it and the currency its prices are in. And every variant of a
# matched product is returned rather than a "best" one, because choosing between a merchant's
# variants is a decision RESOLVE makes from Shopify's own `availableForSale`/`selectedOptions`
# answers — not from whatever order the connection happens to arrive in.
_CATALOG_QUERY = """
query ResolveReadCatalog($first: Int!, $variantsPerProduct: Int!, $searchQuery: String) {
  shop { name currencyCode }
  products(first: $first, query: $searchQuery) {
    edges { node {
      id title handle status
      featuredImage { url altText }
      variants(first: $variantsPerProduct) {
        edges { node {
          id sku price availableForSale
          selectedOptions { name value }
        } }
      }
    } }
  }
}
"""

_ORDERS_QUERY = """
query ResolveReadOrders($first: Int!) {
  orders(first: $first, sortKey: CREATED_AT, reverse: true) {
    edges {
      node {
        id name createdAt displayFinancialStatus displayFulfillmentStatus
        currentTotalPriceSet { shopMoney { amount currencyCode } }
        lineItems(first: 25) {
          edges { node { id title quantity sku product { id } variant { id } } }
        }
      }
    }
  }
}
"""

# A return must be grounded in a real fulfillment line rather than an assumed one, and that
# pairing exists nowhere but under Order.fulfillmentOrders. The id this query returns is a
# FulfillmentOrderLineItem id; verified against the live 2026-07 schema, `returnCreate`'s
# `fulfillmentLineItemId` wants a FulfillmentLineItem id instead, so nothing here should be
# passed to a mutation as-is. It is deliberately NOT folded into _ORDERS_QUERY: live
# verification against this store showed `fulfillmentOrders` access is granted or refused
# independently of `read_orders`, so adding it there would take the working orders read down
# with it. This separate READ keeps that risk contained to the return path.
_FULFILLMENT_LINE_ITEMS_QUERY = """
query ResolveReadFulfillmentLineItems($id: ID!) {
  order(id: $id) {
    id
    fulfillmentOrders(first: 10) {
      edges { node {
        id status
        lineItems(first: 25) {
          edges { node { id remainingQuantity lineItem { id } } }
        }
      } }
    }
  }
}
"""

# 2026-07 exposes returns only through the order they belong to (QueryRoot has a singular
# `return(id:)`, not a top-level `returns` connection), so read_returns walks recent orders
# and collects their nested returns. Still strictly a READ via the read_returns scope.
_RETURNS_QUERY = """
query ResolveReadReturns($first: Int!) {
  orders(first: $first, sortKey: CREATED_AT, reverse: true) {
    edges {
      node {
        id
        name
        returns(first: 25) {
          edges { node { id status createdAt } }
        }
      }
    }
  }
}
"""

# The smallest possible proof that one Shopify order exists at THIS store: its own global id and
# its display name. Used only to verify a server-side linkage before storing it, so a reference
# RESOLVE has never seen Shopify confirm can never become a permanent record. Scope: read_orders.
_ORDER_REFERENCE_QUERY = """
query ResolveReadOrderReference($id: ID!) {
  order(id: $id) {
    id
    name
  }
}
"""

# What ONE order's own lines say they sell (Task 14C.7F). `LineItem.product.id` and
# `LineItem.variant.id` are Shopify's global ids for the things on that order, and they are the
# only identity RESOLVE can compare against the canonical Shopify identity it persisted when it
# bought the item. Deliberately absent: title, price, currency, SKU, merchant, timestamp — every
# one of those is something a lookalike order could also satisfy, and a linkage that rests on them
# is an inference. `pageInfo.hasNextPage` is read because a partial line list must never be read as
# a match. Scope: read_orders, verified live against 2026-07.
_ORDER_LINE_IDENTITY_QUERY = """
query ResolveReadOrderLineIdentity($id: ID!) {
  order(id: $id) {
    id
    lineItems(first: 25) {
      edges { node { id product { id } variant { id } } }
      pageInfo { hasNextPage }
    }
  }
}
"""

# What a return must be pointed AT, read from the order itself: the order's lines with their
# products, and the lines of the fulfilments that actually shipped. `FulfillmentLineItem.id` is
# the identifier `returnCreate` accepts (verified live against 2026-07), and
# `FulfillmentLineItem.quantity` is the number of units that line shipped — the only locally
# readable bound on what can come back. `Order.fulfillments` is a plain list in this version, not
# a connection. Scope: read_orders. No customer, recipient, address or shipping field is read.
_RETURN_TARGETS_QUERY = """
query ResolveReadReturnTargets($id: ID!) {
  order(id: $id) {
    id
    returnStatus
    lineItems(first: 25) {
      edges { node { id product { id } variant { id } } }
    }
    fulfillments {
      id
      status
      fulfillmentLineItems(first: 25) {
        edges { node { id quantity lineItem { id } } }
      }
    }
  }
}
"""

# The returns already on one order, with the per-line quantity and reason note RESOLVE would have
# sent. This is the READ that makes recovery from a lost `returnCreate` answer possible without
# ever re-sending it. Scope: read_returns, nested under the order.
_ORDER_RETURNS_QUERY = """
query ResolveReadOrderReturns($id: ID!) {
  order(id: $id) {
    id
    name
    returns(first: 25) {
      edges {
        node {
          id
          status
          createdAt
          returnLineItems(first: 25) {
            edges { node { quantity returnReasonNote } }
          }
        }
      }
    }
  }
}
"""


_REFUSAL = (
    "RESOLVE's read-only Shopify client transmits one root read query and nothing else, so this "
    "GraphQL document was refused locally before any request was built"
)

# `no request was built` is the promise this message makes, and the sentence has to stay true: the
# gate below runs before the token exchange, which is itself an HTTP call.
_NOT_SENT = "No token exchange, no header, no request body and no HTTP call took place."


def _document_is_permitted(document: object) -> str | None:
    """
    Return None when this read-only client may send `document`, or the refusal message when not.

    Absolute since 14C.7O: the only answer that permits a send is "this is exactly one root query".
    There is no write allowance here to widen, no declared operation to name, and nothing on this
    class that reaches the transport with a document the gate has not accepted — so the read-only
    boundary no longer depends on any caller remembering it. RESOLVE's one declared write is sent
    from `ShopifyReturnWriteClient`, a separate capability over the same configured credential.

    The message names the document's kind and, when the classifier read one, its single root
    operation. It never quotes the document: a document is caller-supplied content, and anything
    inside it — an object id, a customer field, a pasted token — would otherwise arrive in a log
    line and an exception message.
    """
    verdict = classify(document)
    if verdict.is_read:
        return None

    attempted = verdict.surface_name
    detail = f"its kind is {verdict.kind} and its root operation is {attempted}" if attempted \
        else f"its kind is {verdict.kind}"
    why = verdict.reason
    if why:
        detail = f"{detail}; {why}" if attempted else why
    return f"{_REFUSAL}: {detail}. {_NOT_SENT}"


class ShopifyReadOnlyClient:
    """
    Client-credentials auth + Admin GraphQL READS against one Shopify store.

    Zero mutation behavior, by construction rather than by discipline: this class has one document
    entry point, it accepts one root read query and nothing else, and it holds no allowlist, no
    declared-write exception, and no method that would carry a write. RESOLVE's declared return
    operation is sent by `ShopifyReturnWriteClient`, never from here.
    """

    name = "shopify_readonly"

    def __init__(
        self,
        *,
        store_domain: str = "",
        client_id: str = "",
        client_secret: str = "",
        api_version: str = DEFAULT_API_VERSION,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        transport: Any = None,
        admin: ShopifyAdminTransport | None = None,
    ) -> None:
        # Blank/absent credentials are NOT an error here — the error surfaces when a call is
        # actually attempted, so importing or constructing without Shopify configured is safe.
        #
        # `admin` exists for Task 14C.7O: RESOLVE's read capability and its return-write capability
        # are separate OBJECTS over one configured credential, and the pair is built by handing both
        # the same transport. Constructing this class with its own arguments — which every read path
        # and every existing test does — still gives it its own transport, so nothing about being
        # read-only changes.
        self._admin = admin or ShopifyAdminTransport(
            store_domain=store_domain,
            client_id=client_id,
            client_secret=client_secret,
            api_version=api_version,
            timeout_seconds=timeout_seconds,
            transport=transport,
        )

    # ------------------------------------------------------------------ endpoints
    @property
    def store_domain(self) -> str:
        return self._admin.store_domain

    @property
    def api_version(self) -> str:
        return self._admin.api_version

    @property
    def timeout_seconds(self) -> float:
        return self._admin.timeout_seconds

    @property
    def token_url(self) -> str:
        return self._admin.token_url

    @property
    def graphql_url(self) -> str:
        return self._admin.graphql_url

    def validate_configuration(self) -> None:
        """Public, network-free check that all three SHOPIFY_* settings are present."""
        self._admin.validate_configuration()

    # ------------------------------------------------------------------ auth
    async def obtain_access_token(self) -> bool:
        """
        Exchange client credentials for a short-lived access token (client_credentials grant).
        The token is kept in memory only. Returns True on success; raises on any failure —
        always with a credential-free message.
        """
        return await self._admin.obtain_access_token()

    # ------------------------------------------------------------------ graphql
    async def execute_graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict:
        """
        Run one GraphQL query and return its `data` dict. Raises a typed, credential-free error for
        transport failures, HTTP errors (incl. 429), GraphQL-level errors, and malformed answers.
        Query text is never logged.

        Absolute since Task 14C.7O: the document's KIND is decided locally, as the first act, and
        the only answer that permits a send is "exactly one root read query". Anything else — a
        write of any kind, a subscription, a malformed or unclassifiable document — raises
        ``ShopifyReadOnlyViolation`` before the token exchange, so no header, no body and no request
        exists to leak a credential into in the first place. There is no write exception in this
        method, and no other method on this class reaches the transport.
        """
        refusal = _document_is_permitted(query)
        if refusal is not None:
            logger.error("Shopify read-only boundary: %s", refusal)
            raise ShopifyReadOnlyViolation(refusal)

        return await self._admin.execute(query, variables)

    # ------------------------------------------------------------------ reads
    async def read_products(self, first: int = 50) -> list[ShopifyProduct]:
        """List current products (read_products scope). Normalized, never raw nodes."""
        data = await self.execute_graphql(_PRODUCTS_QUERY, {"first": max(1, min(first, 250))})
        return self._normalize_edges(data.get("products"), ShopifyProduct.from_node, "products")

    async def read_catalog(
        self,
        *,
        search_query: str | None = None,
        first: int = 20,
        variants_per_product: int = 10,
    ) -> ShopifyCatalog:
        """
        Read the connected store's own catalog for discovery. READ ONLY — this creates nothing.

        Shopify answers with its own Product and ProductVariant global ids, its own prices in the
        store's own currency, and its own `availableForSale` / `selectedOptions`. Nothing here is
        assembled, guessed or defaulted: a product Shopify did not name is simply not in the
        result, and a store that cannot be reached raises rather than returns an empty catalog that
        would look like "this merchant has nothing".
        """
        cleaned = search_query.strip() if isinstance(search_query, str) else ""
        data = await self.execute_graphql(
            _CATALOG_QUERY,
            {
                "first": max(1, min(first, 250)),
                "variantsPerProduct": max(1, min(variants_per_product, 100)),
                "searchQuery": cleaned or None,
            },
        )
        shop = data.get("shop") if isinstance(data.get("shop"), dict) else {}
        products = [
            product
            for product in (
                ShopifyCatalogProduct.from_node(
                    edge.get("node") or {}, currency=shop.get("currencyCode")
                )
                for edge in self._edges(data.get("products"), "products")
                if isinstance(edge, dict)
            )
            if product is not None
        ]
        if not self.store_domain:
            raise ShopifyConfigError(
                "No Shopify store domain is configured, so no catalog identity can be recorded."
            )
        return ShopifyCatalog(
            store_domain=self.store_domain,
            shop_name=shop.get("name") if isinstance(shop.get("name"), str) else None,
            currency=shop.get("currencyCode") if isinstance(shop.get("currencyCode"), str) else None,
            products=tuple(products),
        )

    async def read_orders(self, first: int = 20) -> list[ShopifyOrder]:
        """List the most recent orders (read_orders scope)."""
        data = await self.execute_graphql(_ORDERS_QUERY, {"first": max(1, min(first, 250))})
        return self._normalize_edges(data.get("orders"), ShopifyOrder.from_node, "orders")

    async def read_returns(self, first: int = 20) -> list[ShopifyReturn]:
        """
        List returns via the read_returns scope. READ ONLY — this creates nothing. In 2026-07
        returns are exposed through the order they belong to (there is no top-level `returns`
        connection), so this walks the most recent orders and collects their nested returns.
        If the configured app version or scopes do not expose them, the ShopifyGraphQLError
        with Shopify's own explanation propagates; no empty answer is faked.
        """
        data = await self.execute_graphql(_RETURNS_QUERY, {"first": max(1, min(first, 250))})
        returns: list[ShopifyReturn] = []
        for order_edge in self._edges(data.get("orders"), "orders"):
            order_node = order_edge.get("node")
            if not isinstance(order_node, dict):
                continue
            for return_edge in self._edges(order_node.get("returns"), "returns"):
                normalized = ShopifyReturn.from_node(return_edge.get("node") or {}, order=order_node)
                if normalized is not None:
                    returns.append(normalized)
        return returns

    async def read_fulfillment_line_items(
        self, order_id: str
    ) -> list[ShopifyFulfillmentLineItem]:
        """
        Read the fulfillment order line items of one order. READ ONLY — this creates nothing.

        Each row pairs a fulfillment order line with its `LineItem` and reports how many units
        are still outstanding on it (`remainingQuantity`). Note what these are NOT: verified
        against the live 2026-07 schema, `ReturnLineItemInput.fulfillmentLineItemId` expects a
        `FulfillmentLineItem.id`, a different object read via
        `Order.fulfillments.fulfillmentLineItems`, and `remainingQuantity` counts units awaiting
        fulfilment — it reads 0 on a line that has already shipped, so it is not a returnable
        maximum either. Nothing here is a return ceiling, and no id read here should be handed to
        a return mutation as-is.

        The query is deliberately separate from `_ORDERS_QUERY`: live verification showed this
        store grants or refuses `Order.fulfillmentOrders` independently of the other order
        fields, and folding it in would let a refusal take the general orders read down with it.
        A refusal here surfaces as Shopify's own sanitized error — no id is guessed, and no
        return is requested.
        """
        data = await self.execute_graphql(_FULFILLMENT_LINE_ITEMS_QUERY, {"id": order_id})
        order_node = data.get("order")
        if order_node is None:
            raise ShopifyMalformedResponseError(
                "Shopify returned no order for that id, so no fulfillment line item could be read."
            )
        if not isinstance(order_node, dict):
            raise ShopifyMalformedResponseError("Shopify's order answer had an unexpected shape.")
        items: list[ShopifyFulfillmentLineItem] = []
        for fo_edge in self._edges(order_node.get("fulfillmentOrders"), "fulfillmentOrders"):
            fo_node = fo_edge.get("node")
            if not isinstance(fo_node, dict):
                continue
            fo_id = fo_node.get("id")
            for edge in self._edges(fo_node.get("lineItems"), "lineItems"):
                normalized = ShopifyFulfillmentLineItem.from_node(
                    edge.get("node") or {}, fulfillment_order_id=fo_id
                )
                if normalized is not None:
                    items.append(normalized)
        return items

    async def read_order_reference(self, order_id: str) -> ShopifyOrderReference:
        """
        Ask Shopify whether ONE canonical order exists at this store. READ ONLY.

        This is the verification behind a server-side linkage: the reference is confirmed against
        Shopify's own answer before it is ever stored, so a mistyped, invented, or foreign id is
        refused rather than recorded. Anything Shopify does not return as an order raises — it is
        never normalized into a reference that looks valid.
        """
        data = await self.execute_graphql(_ORDER_REFERENCE_QUERY, {"id": order_id})
        order_node = data.get("order")
        if not isinstance(order_node, dict) or not order_node.get("id"):
            raise ShopifyMalformedResponseError(
                "Shopify knows nothing about that order id, so it cannot be used as a canonical "
                "reference."
            )
        reference = ShopifyOrderReference.from_node(order_node)
        if reference is None:
            raise ShopifyMalformedResponseError(
                "Shopify's order answer carried no id, so it cannot be used as a canonical "
                "reference."
            )
        return reference

    async def read_order_line_identity(self, order_id: str) -> ShopifyOrderLineIdentity:
        """
        Read what ONE order's own lines sell: Shopify's product and variant global ids. READ ONLY.

        This is the read that turns a merchant-order pairing from a claim into a check. A signed
        event can name any real Shopify order; whether that order contains the thing RESOLVE
        actually bought is answered only by Shopify's own `LineItem.product.id` /
        `LineItem.variant.id`. Nothing here decides a pairing on its own, and nothing here looks at
        a title, a price, a currency, a SKU or a timestamp: those are the fields two different
        purchases can share, which is why they are not read.

        A truncated line list is reported as truncated rather than silently treated as the whole
        order, because "not found in the part I read" is not evidence of absence.
        """
        data = await self.execute_graphql(_ORDER_LINE_IDENTITY_QUERY, {"id": order_id})
        order_node = self._order_node(data, "line identity")
        lines = [
            line
            for line in (
                ShopifyOrderLineRef.from_node(edge.get("node") or {})
                for edge in self._edges(order_node.get("lineItems"), "lineItems")
            )
            if line is not None
        ]
        page_info = (order_node.get("lineItems") or {}).get("pageInfo")
        has_next_page = bool(isinstance(page_info, dict) and page_info.get("hasNextPage"))
        return ShopifyOrderLineIdentity(
            order_id=str(order_node["id"]),
            lines=lines,
            all_lines_read=not has_next_page,
        )

    async def read_return_targets(self, order_id: str) -> ShopifyOrderReturnTargets:
        """
        Read what a return on this order must reference. READ ONLY — this creates nothing.

        Returns the order's own lines (with their products) and the lines of the fulfilments that
        actually shipped, so a caller can pair "the item the customer bought" with "the shipment
        line a return has to name" without inventing either. The shipped line carries
        `FulfillmentLineItem.quantity` — the units that went out, which is the only honest local
        bound on a return quantity. If Shopify answers with no such line, the answer is an empty
        list and the caller must refuse; nothing here synthesizes a target.
        """
        data = await self.execute_graphql(_RETURN_TARGETS_QUERY, {"id": order_id})
        order_node = self._order_node(data, "return target")
        targets = ShopifyOrderReturnTargets(
            order_id=str(order_node["id"]),
            return_status=order_node.get("returnStatus"),
            order_lines=[
                line
                for line in (
                    ShopifyOrderLineRef.from_node(edge.get("node") or {})
                    for edge in self._edges(order_node.get("lineItems"), "lineItems")
                )
                if line is not None
            ],
        )
        shipped: list[ShopifyShippedFulfillmentLine] = []
        for fulfillment in self._plain_list(order_node.get("fulfillments"), "fulfillments"):
            status = fulfillment.get("status")
            for edge in self._edges(
                fulfillment.get("fulfillmentLineItems"), "fulfillmentLineItems"
            ):
                normalized = ShopifyShippedFulfillmentLine.from_node(
                    edge.get("node") or {},
                    fulfillment_id=fulfillment.get("id"),
                    fulfillment_status=status if isinstance(status, str) else None,
                )
                if normalized is not None:
                    shipped.append(normalized)
        targets.shipped_lines.extend(shipped)
        return targets

    async def read_order_returns(self, order_id: str) -> list[ShopifyReturn]:
        """
        List the returns already recorded against ONE order, including their line quantities and
        reason notes. READ ONLY — this is the read that replaces a re-submitted mutation.
        """
        data = await self.execute_graphql(_ORDER_RETURNS_QUERY, {"id": order_id})
        order_node = self._order_node(data, "returns")
        return [
            normalized
            for normalized in (
                ShopifyReturn.from_node(edge.get("node") or {}, order=order_node)
                for edge in self._edges(order_node.get("returns"), "returns")
            )
            if normalized is not None
        ]

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _order_node(data: dict, label: str) -> dict:
        """Pull `data.order` out of a single-order read, refusing a missing or odd-shaped one."""
        order_node = data.get("order")
        if order_node is None:
            raise ShopifyMalformedResponseError(
                f"Shopify returned no order for that id, so no {label} could be read."
            )
        if not isinstance(order_node, dict):
            raise ShopifyMalformedResponseError("Shopify's order answer had an unexpected shape.")
        return order_node

    @staticmethod
    def _plain_list(container: Any, field_name: str) -> list[dict]:
        """Validate a field Shopify exposes as a plain list rather than a connection."""
        if container is None:
            return []
        if not isinstance(container, list):
            raise ShopifyMalformedResponseError(
                f"Shopify's '{field_name}' payload was not a list."
            )
        return [item for item in container if isinstance(item, dict)]

    @staticmethod
    def _edges(container: Any, field_name: str) -> list[dict]:
        """Validate a GraphQL connection container and return its edges."""
        if container is None:
            raise ShopifyMalformedResponseError(
                f"Shopify's answer carried no '{field_name}' container."
            )
        if not isinstance(container, dict):
            raise ShopifyMalformedResponseError(
                f"Shopify's '{field_name}' payload had an unexpected shape."
            )
        edges = container.get("edges")
        if edges is None:
            return []
        if not isinstance(edges, list):
            raise ShopifyMalformedResponseError(
                f"Shopify's '{field_name}.edges' payload was not a list."
            )
        return [edge for edge in edges if isinstance(edge, dict)]

    def _normalize_edges(self, container: Any, from_node, field_name: str) -> list:
        return [
            normalized
            for normalized in (
                from_node(edge.get("node") or {}) for edge in self._edges(container, field_name)
            )
            if normalized is not None
        ]

__all__ = ["ShopifyReadOnlyClient", "DEFAULT_API_VERSION"]
