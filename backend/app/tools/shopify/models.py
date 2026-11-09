"""
Normalized read models for Shopify Admin GraphQL reads (Task 14B.3).

These are deliberately small, frozen snapshots of what Shopify SAID — raw GraphQL nodes never
escape the client. Money is Decimal (never float). Nothing here is authoritative application
data: the canonical records RESOLVE acts on remain the persisted Postgres rows.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation


def to_decimal(value: object) -> Decimal | None:
    """Safely coerce a Shopify MoneyV2 amount string to Decimal; unreadable values -> None."""
    if isinstance(value, Decimal):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return Decimal(value.strip())
    except InvalidOperation:
        return None


@dataclass(frozen=True)
class ShopifyProduct:
    product_id: str
    title: str
    handle: str | None = None
    status: str | None = None
    price: Decimal | None = None
    currency: str | None = None

    @classmethod
    def from_node(cls, node: dict) -> "ShopifyProduct | None":
        if not isinstance(node, dict) or not node.get("id"):
            return None
        price_fields = node.get("priceV2") or node.get("priceRangeV2", {}).get("minVariantPrice") or {}
        if not isinstance(price_fields, dict):
            price_fields = {}
        return cls(
            product_id=str(node["id"]),
            title=str(node.get("title") or ""),
            handle=node.get("handle"),
            status=node.get("status"),
            price=to_decimal(price_fields.get("amount")),
            currency=price_fields.get("currencyCode"),
        )


@dataclass(frozen=True)
class ShopifyLineItem:
    title: str
    quantity: int
    line_item_gid: str | None = None
    product_id: str | None = None
    variant_id: str | None = None
    sku: str | None = None
    unit_price: Decimal | None = None
    currency: str | None = None

    @classmethod
    def from_node(cls, node: dict) -> "ShopifyLineItem | None":
        if not isinstance(node, dict):
            return None
        product = node.get("product") if isinstance(node.get("product"), dict) else None
        variant = node.get("variant") if isinstance(node.get("variant"), dict) else None
        product_id = product.get("id") if product else None
        unit = node.get("originalUnitPrice")
        return cls(
            title=str(node.get("title") or ""),
            quantity=int(node.get("quantity") or 0),
            line_item_gid=node.get("id"),
            product_id=product_id,
            variant_id=variant.get("id") if variant else None,
            sku=node.get("sku"),
            unit_price=to_decimal(unit if isinstance(unit, str) else None),
            currency=node.get("unitPriceSet", {}).get("shopMoney", {}).get("currencyCode")
            if isinstance(node.get("unitPriceSet"), dict)
            else None,
        )


@dataclass(frozen=True)
class ShopifyFulfillmentLineItem:
    """
    One line of one fulfillment order — read-only normalization of
    `FulfillmentOrderLineItem`. It is read from Shopify, never derived from a
    product/variant/order/database id.

    NOT the identifier `returnCreate` wants. Verified against the live 2026-07 schema,
    `ReturnLineItemInput.fulfillmentLineItemId` "expects a `FulfillmentLineItem.id`" — a
    different object, read via `Order.fulfillments.fulfillmentLineItems`. This row's value is
    the pairing and the fulfilment count: `lineItem.id` joins it to the order line, and
    `remainingQuantity` says how many units are still awaiting fulfilment — which is 0 on a
    line that already shipped, so it is not a returnable maximum.
    """

    fulfillment_line_item_id: str
    remaining_quantity: int | None = None
    fulfillment_order_id: str | None = None
    order_line_item_id: str | None = None

    @classmethod
    def from_node(cls, node: dict, fulfillment_order_id: str | None = None) -> "ShopifyFulfillmentLineItem | None":
        if not isinstance(node, dict) or not node.get("id"):
            return None
        line_item = node.get("lineItem") if isinstance(node.get("lineItem"), dict) else None
        remaining = node.get("remainingQuantity")
        return cls(
            fulfillment_line_item_id=str(node["id"]),
            remaining_quantity=int(remaining) if isinstance(remaining, (int, str)) else None,
            fulfillment_order_id=fulfillment_order_id,
            order_line_item_id=line_item.get("id") if line_item else None,
        )


@dataclass(frozen=True)
class ShopifyOrder:
    order_id: str
    name: str
    financial_status: str | None = None
    fulfillment_status: str | None = None
    total: Decimal | None = None
    currency: str | None = None
    line_items: list[ShopifyLineItem] = field(default_factory=list)
    created_at: str | None = None

    @classmethod
    def from_node(cls, node: dict) -> "ShopifyOrder | None":
        if not isinstance(node, dict) or not node.get("id"):
            return None
        items = [
            item
            for item in (
                ShopifyLineItem.from_node(edge.get("node") or {})
                for edge in (node.get("lineItems") or {}).get("edges", [])
                if isinstance(edge, dict)
            )
            if item is not None
        ]
        total_set = node.get("currentTotalPriceSet") or {}
        total_price = node.get("currentTotalPrice")
        currency = None
        if isinstance(total_set, dict) and isinstance(total_set.get("shopMoney"), dict):
            currency = total_set["shopMoney"].get("currencyCode")
            total_price = total_set["shopMoney"].get("amount") or total_price
        return cls(
            order_id=str(node["id"]),
            name=str(node.get("name") or ""),
            financial_status=node.get("displayFinancialStatus"),
            fulfillment_status=node.get("displayFulfillmentStatus"),
            total=to_decimal(total_price if isinstance(total_price, str) else None),
            currency=currency,
            line_items=items,
            created_at=node.get("createdAt"),
        )


@dataclass(frozen=True)
class ShopifyCatalogVariant:
    """
    One variant of one catalog product, as Shopify described it (Task 14C.7D).

    `variant_id` and `product_id` are Shopify's own global ids and are the only authoritative
    identity here: title, price and options exist for the shopper, and must never be used to
    decide *which* object a result is. `sku` is whatever the merchant typed — measured on a real
    store it is routinely None, which is exactly why the variant GID, not the SKU, carries
    identity.
    """

    variant_id: str
    product_id: str
    title: str
    handle: str | None = None
    status: str | None = None
    sku: str | None = None
    price: Decimal | None = None
    currency: str | None = None
    available_for_sale: bool = False
    selected_options: tuple[tuple[str, str], ...] = ()
    image_url: str | None = None

    @classmethod
    def from_node(
        cls,
        node: dict,
        *,
        product_id: str,
        product_title: str,
        handle: str | None,
        status: str | None,
        image_url: str | None,
        currency: str | None,
    ) -> "ShopifyCatalogVariant | None":
        if not isinstance(node, dict) or not node.get("id"):
            return None
        options = tuple(
            (str(option.get("name") or ""), str(option.get("value") or ""))
            for option in (node.get("selectedOptions") or [])
            if isinstance(option, dict)
        )
        return cls(
            variant_id=str(node["id"]),
            product_id=product_id,
            title=product_title,
            handle=handle,
            status=status,
            sku=node.get("sku") if isinstance(node.get("sku"), str) else None,
            price=to_decimal(node.get("price")),
            currency=currency,
            available_for_sale=node.get("availableForSale") is True,
            selected_options=options,
            image_url=image_url,
        )


@dataclass(frozen=True)
class ShopifyCatalogProduct:
    """One catalog product with the variants Shopify returned for it."""

    product_id: str
    title: str
    handle: str | None = None
    status: str | None = None
    variants: tuple[ShopifyCatalogVariant, ...] = ()

    @classmethod
    def from_node(cls, node: dict, *, currency: str | None) -> "ShopifyCatalogProduct | None":
        if not isinstance(node, dict) or not node.get("id"):
            return None
        product_id = str(node["id"])
        title = str(node.get("title") or "")
        handle = node.get("handle") if isinstance(node.get("handle"), str) else None
        status = node.get("status") if isinstance(node.get("status"), str) else None
        image = node.get("featuredImage")
        # Shopify answers `featuredImage: null` for a product with no published image — measured on
        # the live dev store — so both the image and its url have to be checked, not assumed.
        image_url = (
            image.get("url")
            if isinstance(image, dict) and isinstance(image.get("url"), str)
            else None
        )
        variants = tuple(
            variant
            for variant in (
                ShopifyCatalogVariant.from_node(
                    edge.get("node") or {},
                    product_id=product_id,
                    product_title=title,
                    handle=handle,
                    status=status,
                    image_url=image_url,
                    currency=currency,
                )
                for edge in ((node.get("variants") or {}).get("edges") or [])
                if isinstance(edge, dict)
            )
            if variant is not None
        )
        return cls(
            product_id=product_id, title=title, handle=handle, status=status, variants=variants
        )


@dataclass(frozen=True)
class ShopifyCatalog:
    """
    One read of the connected store's catalog.

    The store identity and its currency travel with the products on purpose. A GID means nothing
    without saying which store issued it, and a price means nothing without saying which currency
    it is in — both are Shopify's answers, never RESOLVE's assumptions.
    """

    store_domain: str
    shop_name: str | None
    currency: str | None
    products: tuple[ShopifyCatalogProduct, ...] = ()


@dataclass(frozen=True)
class ShopifyOrderReference:
    """
    Proof that one canonical Shopify order exists at THIS store — nothing more.

    This is the smallest read that can back a linkage: Shopify's own global id and its display
    name. A linkage is stored only after Shopify names the order itself, so a mistyped or
    invented id cannot become a permanent record. No customer, address, totals, or line data is
    read here.
    """

    order_id: str
    name: str | None = None

    @classmethod
    def from_node(cls, node: dict) -> "ShopifyOrderReference | None":
        if not isinstance(node, dict) or not node.get("id"):
            return None
        name = node.get("name")
        return cls(order_id=str(node["id"]), name=name if isinstance(name, str) else None)


@dataclass(frozen=True)
class ShopifyOrderLineRef:
    """
    One line of the ORDER itself: the join key plus the product AND variant it sells. Read-only.

    `variant_id` is Shopify's own `ProductVariant` global id for the line (Task 14C.7E). It is the
    third link in the return chain — Product GID → Variant GID → Order GID → FulfillmentLineItem —
    and it is read here rather than inferred: a return must be able to say WHICH item shipped, and
    on a multi-line or multi-variant order only Shopify's own id can say that.
    """

    order_line_item_id: str
    product_id: str | None = None
    variant_id: str | None = None

    @classmethod
    def from_node(cls, node: dict) -> "ShopifyOrderLineRef | None":
        if not isinstance(node, dict) or not node.get("id"):
            return None
        product = node.get("product") if isinstance(node.get("product"), dict) else None
        variant = node.get("variant") if isinstance(node.get("variant"), dict) else None
        return cls(
            order_line_item_id=str(node["id"]),
            product_id=product.get("id") if product else None,
            variant_id=variant.get("id") if variant else None,
        )


@dataclass(frozen=True)
class ShopifyShippedFulfillmentLine:
    """
    One line of an actual FULFILMENT — read-only normalization of `FulfillmentLineItem`.

    This is the object a 2026-07 return references: `ReturnLineItemInput.fulfillmentLineItemId`
    "expects a `FulfillmentLineItem.id`" (verified against the live schema), which is a different
    object from `FulfillmentOrderLineItem`. `quantity` is the number of units that line shipped —
    the only locally readable count that can bound a return, because you cannot return more than
    was sent. `FulfillmentOrderLineItem.remainingQuantity` is NOT that number: it counts units
    still AWAITING fulfilment and reads 0 on any shipped line.
    """

    fulfillment_line_item_id: str
    quantity: int | None = None
    order_line_item_id: str | None = None
    fulfillment_id: str | None = None
    fulfillment_status: str | None = None

    @classmethod
    def from_node(
        cls, node: dict, fulfillment_id: str | None = None, fulfillment_status: str | None = None
    ) -> "ShopifyShippedFulfillmentLine | None":
        if not isinstance(node, dict) or not node.get("id"):
            return None
        line_item = node.get("lineItem") if isinstance(node.get("lineItem"), dict) else None
        quantity = node.get("quantity")
        return cls(
            fulfillment_line_item_id=str(node["id"]),
            quantity=int(quantity) if isinstance(quantity, (int, str)) and not isinstance(quantity, bool) else None,
            order_line_item_id=line_item.get("id") if line_item else None,
            fulfillment_id=fulfillment_id,
            fulfillment_status=fulfillment_status,
        )


@dataclass(frozen=True)
class ShopifyOrderReturnTargets:
    """What a return must reference on one order: its lines, and what actually shipped."""

    order_id: str
    return_status: str | None = None
    order_lines: list[ShopifyOrderLineRef] = field(default_factory=list)
    shipped_lines: list[ShopifyShippedFulfillmentLine] = field(default_factory=list)


@dataclass(frozen=True)
class ShopifyOrderLineIdentity:
    """
    What ONE Shopify order's own lines say about which products and variants were sold (14C.7F).

    This is the read that lets a merchant-order pairing be *checked* instead of trusted on words:
    Shopify's own `LineItem.product.id` and `LineItem.variant.id` against the canonical Shopify
    identity RESOLVE persisted when it bought the item. It carries no customer, no money, no
    address, and no title — a title could be matched, and matching a title is exactly the inference
    this chain refuses to make.

    `all_lines_read` is Shopify's own answer to "did you see every line". When a connection reports
    a next page, RESOLVE has NOT seen the whole order, and an unverified remainder must not be read
    as a match.
    """

    order_id: str
    lines: list[ShopifyOrderLineRef] = field(default_factory=list)
    all_lines_read: bool = True


@dataclass(frozen=True)
class ShopifyReturnLine:
    """One line of an existing return, as the merchant recorded it. Used to correlate a return
    whose creation response RESOLVE never received — matched on quantity and the reason token
    RESOLVE itself sent, never on anything personal to the customer."""

    quantity: int | None = None
    reason_note: str | None = None

    @classmethod
    def from_node(cls, node: dict) -> "ShopifyReturnLine | None":
        if not isinstance(node, dict):
            return None
        quantity = node.get("quantity")
        note = node.get("returnReasonNote")
        return cls(
            quantity=int(quantity) if isinstance(quantity, (int, str)) and not isinstance(quantity, bool) else None,
            reason_note=str(note) if isinstance(note, str) else None,
        )


@dataclass(frozen=True)
class ShopifyReturn:
    return_id: str
    status: str | None = None
    order_id: str | None = None
    order_name: str | None = None
    created_at: str | None = None
    lines: list[ShopifyReturnLine] = field(default_factory=list)

    @classmethod
    def from_node(cls, node: dict, order: dict | None = None) -> "ShopifyReturn | None":
        """
        Build a return from its node. `order` is the PARENT order node when the return was
        read through `Order.returns` (2026-07 exposes returns only per order); a node that
        carries its own `order` field wins.
        """
        if not isinstance(node, dict) or not node.get("id"):
            return None
        embedded = node.get("order") if isinstance(node.get("order"), dict) else {}
        parent = order if isinstance(order, dict) else {}
        lines = [
            line
            for line in (
                ShopifyReturnLine.from_node(edge.get("node") or {})
                for edge in ((node.get("returnLineItems") or {}).get("edges") or [])
                if isinstance(edge, dict)
            )
            if line is not None
        ]
        return cls(
            return_id=str(node["id"]),
            status=node.get("status"),
            order_id=embedded.get("id") or parent.get("id"),
            order_name=embedded.get("name") or parent.get("name"),
            created_at=node.get("createdAt"),
            lines=lines,
        )


__all__ = [
    "ShopifyCatalog",
    "ShopifyCatalogProduct",
    "ShopifyCatalogVariant",
    "ShopifyFulfillmentLineItem",
    "ShopifyLineItem",
    "ShopifyOrder",
    "ShopifyOrderLineRef",
    "ShopifyOrderLineIdentity",
    "ShopifyOrderReference",
    "ShopifyOrderReturnTargets",
    "ShopifyProduct",
    "ShopifyReturn",
    "ShopifyReturnLine",
    "ShopifyShippedFulfillmentLine",
    "to_decimal",
]
