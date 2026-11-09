"""
The channel's Shopify documents, and the parsing of their answers.

Every field name and argument listed here was read out of the live 2026-07 Admin schema before being
written down (read-only introspection, 0 mutations):

    draftOrderCreate(input: DraftOrderInput!)      -> DraftOrderCreatePayload { draftOrder userErrors }
    draftOrderComplete(id: ID!, paymentGatewayId: String, sourceName: String)
                                                   -> DraftOrderCompletePayload { draftOrder userErrors }
    orders(first: Int, query: String)              -> OrderConnection
    DraftOrderInput.poNumber: String, .lineItems: [DraftOrderLineItemInput!]
    DraftOrderLineItemInput { variantId: ID, quantity: Int!, originalUnitPriceWithCurrency: MoneyInput }
    DraftOrder.order: Order                        Order.currencyCode: CurrencyCode!
    Order.poNumber: String, Order.displayFinancialStatus, LineItem.product{id}/variant{id}/quantity

Deliberately absent, and each absence is a decision:

  * no ``paymentGatewayId``, no ``orderMarkAsPaid``, no ``orderCreateManualPayment`` — the channel has
    not received money, so it does not tell Shopify that it has (Phase A, Option C);
  * no ``priceOverride``/``originalUnitPriceWithCurrency`` by default — the merchant's catalog owns the
    price, and a channel that set its own price would be inventing the number it later asks RESOLVE
    to corroborate;
  * no customer, address, email or phone: the correlation lives in ``poNumber``, which is a
    merchant-side reference and not shopper data;
  * no ``customAttributes`` — measured live, ``custom_attribute:`` is not a filter Shopify honours
    (an impossible value returns every order), so it cannot support a deterministic read-back.
"""
from __future__ import annotations

from dataclasses import dataclass, field

DRAFT_ORDER_CREATE = """
mutation MerchantDraftOrderCreate($input: DraftOrderInput!) {
  draftOrderCreate(input: $input) {
    draftOrder {
      id
      poNumber
      currencyCode
      order { id }
    }
    userErrors { field message }
  }
}
"""

DRAFT_ORDER_COMPLETE = """
mutation MerchantDraftOrderComplete($id: ID!) {
  draftOrderComplete(id: $id) {
    draftOrder {
      id
      poNumber
      completedAt
      order {
        id
        name
        poNumber
        currencyCode
        sourceName
        displayFinancialStatus
        createdAt
        lineItems(first: 25) {
          edges { node { id quantity product { id } variant { id } } }
        }
      }
    }
    userErrors { field message }
  }
}
"""

# The correlation read-back. `po_number:` is the filter that was measured to actually narrow the
# result set, which is what makes zero/one/many a reconciliation answer rather than a guess.
READ_BACK_BY_CORRELATION = """
query MerchantReadBackByCorrelation($query: String!) {
  orders(first: 5, query: $query) {
    edges {
      node {
        id
        name
        poNumber
        currencyCode
        sourceName
        displayFinancialStatus
        createdAt
        lineItems(first: 25) {
          edges { node { id quantity product { id } variant { id } } }
        }
      }
    }
  }
}
"""

SHOP_IDENTITY = """
query MerchantShopIdentity {
  shop { name currencyCode }
}
"""

ORDER_GID_PREFIX = "gid://shopify/Order/"


@dataclass(frozen=True)
class OrderLine:
    line_item_id: str
    product_id: str | None
    variant_id: str | None
    quantity: int | None


@dataclass(frozen=True)
class MerchantOrder:
    """One Shopify order, as the channel saw it."""

    order_id: str
    name: str | None
    po_number: str | None
    currency: str | None
    source_name: str | None
    financial_status: str | None
    created_at: str | None
    lines: tuple[OrderLine, ...] = ()


@dataclass
class GatewayResult:
    """A channel-side answer: either data, or a refusal with a reason. Never a half-truth."""

    ok: bool
    reason: str | None = None
    order: MerchantOrder | None = None
    candidates: tuple[MerchantOrder, ...] = ()
    shop_name: str | None = None
    shop_currency: str | None = None
    user_errors: list = field(default_factory=list)


def draft_order_input(*, correlation_id: str, variant_gid: str, quantity: int) -> dict:
    """
    The whole create payload: the variant the merchant sells, how many, and the correlation stamp.

    The price is deliberately not sent — Shopify publishes it from the catalog, so the number the
    channel later verifies against is the merchant's own, not the channel's invention.
    """
    if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 1:
        raise ValueError("a draft order line needs a whole quantity of at least 1")
    return {
        "poNumber": correlation_id,
        "lineItems": [{"variantId": variant_gid, "quantity": quantity}],
    }


def _nodes(payload: dict, connection_key: str) -> list[dict]:
    connection = payload.get(connection_key) or {}
    edges = connection.get("edges") or []
    return [e["node"] for e in edges if isinstance(e, dict) and isinstance(e.get("node"), dict)]


def parse_order_node(node: dict) -> MerchantOrder:
    lines = []
    for line in _nodes(node, "lineItems"):
        product = line.get("product") if isinstance(line.get("product"), dict) else {}
        variant = line.get("variant") if isinstance(line.get("variant"), dict) else {}
        lines.append(
            OrderLine(
                line_item_id=str(line.get("id") or ""),
                product_id=product.get("id"),
                variant_id=variant.get("id"),
                quantity=line.get("quantity"),
            )
        )
    return MerchantOrder(
        order_id=str(node.get("id") or ""),
        name=node.get("name"),
        po_number=node.get("poNumber"),
        currency=node.get("currencyCode"),
        source_name=node.get("sourceName"),
        financial_status=node.get("displayFinancialStatus"),
        created_at=node.get("createdAt"),
        lines=tuple(lines),
    )


def user_errors(data: dict, payload_key: str) -> list[dict]:
    payload = data.get(payload_key) or {}
    errors = payload.get("userErrors") or []
    return [e for e in errors if isinstance(e, dict)]


def parse_complete(data: dict) -> tuple[MerchantOrder | None, list[dict]]:
    """
    The completed order, or nothing.

    `DraftOrderCompletePayload` returns the *draft order*, whose `order` field is the real order —
    reading `id` off the draft instead would put a DraftOrder GID in a linkage event, which is not an
    order at all.
    """
    errors = user_errors(data, "draftOrderComplete")
    payload = (data.get("draftOrderComplete") or {})
    draft = payload.get("draftOrder") or {}
    node = draft.get("order") if isinstance(draft.get("order"), dict) else None
    if node is None:
        return None, errors
    return parse_order_node(node), errors


def parse_read_back(data: dict) -> list[MerchantOrder]:
    return [parse_order_node(node) for node in _nodes(data, "orders")]


def parse_shop(data: dict) -> tuple[str | None, str | None]:
    shop = data.get("shop") or {}
    return shop.get("name"), shop.get("currencyCode")
