"""
A deterministic stand-in merchant store, for the tests that must not reach a real one.

It is a fake of the *merchant's* side, not of RESOLVE: it holds the write capability the channel
needs and answers with the shapes the live 2026-07 schema was measured to have (`Order.currencyCode`,
`poNumber`, `lineItems{ node{ quantity product{ id } variant{ id } } }`, `displayFinancialStatus`).
Every behaviour a failure test needs is scripted, and every question it receives is recorded, so the
channel's rules — read back before you trust a create, never create twice on an unknown outcome,
refuse to pair a mismatch — are measured rather than asserted in prose.

Two things it makes impossible are the point: it has no payment mutation to call, and it has no way
for a caller to say "make it USD" after the fact without the channel noticing on read-back.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from merchant_channel.documents import MerchantOrder, OrderLine
from merchant_channel.gateway import GatewayDefiniteFailure, GatewayOutcomeUnknown

ORDER_GID_PREFIX = "gid://shopify/Order/"


@dataclass
class FakeAdminStore:
    """Scripted merchant store: what it publishes, and what it was asked to do."""

    shop_name: str = "RESOLVE Commerce Lab"
    shop_currency: str = "USD"
    publish_currency: str = "INR"
    product_gid: str = "gid://shopify/Product/10622982422815"
    variant_gid: str = "gid://shopify/ProductVariant/52923834990879"
    # None means the store sold exactly what was asked. A number here is a scripted mismatch: the
    # shelf answered with a different quantity than the purchase handoff named.
    line_quantity: int | None = None
    stamp_po_number: bool = True
    create_behavior: str = "ok"  # ok | definite_failure | outcome_unknown
    read_back_behavior: str = "one"  # one | none | two
    # When set, read_back answers with exactly these orders instead of what it holds — the way to
    # script "Shopify returned an order this purchase did not create".
    read_back_override: list | None = None
    orders: dict = field(default_factory=dict)

    create_calls: list = field(default_factory=list)
    read_back_calls: list = field(default_factory=list)
    shop_identity_calls: int = 0

    async def create_order(self, *, correlation_id: str, variant_gid: str, quantity: int):
        self.create_calls.append(
            {"correlation_id": correlation_id, "variant_gid": variant_gid, "quantity": quantity}
        )
        if self.create_behavior == "definite_failure":
            raise GatewayDefiniteFailure("draftOrderCreate refused: variant does not exist")
        if self.create_behavior == "outcome_unknown":
            # The request left. Whether Shopify has an order from it is precisely what is unknown,
            # and the order below models the branch where it does exist but the answer never arrived.
            self._make(correlation_id, requested_variant=variant_gid, requested_quantity=quantity)
            raise GatewayOutcomeUnknown("ReadTimeout: the create answer was lost")
        return self._make(correlation_id, requested_variant=variant_gid, requested_quantity=quantity)

    def _make(self, correlation_id: str, *, requested_variant: str, requested_quantity: int) -> MerchantOrder:
        """
        What the store has on the shelf for this correlation, not an echo of the request.

        The line carries the store's own product and variant GIDs, and its quantity is what was asked
        unless `line_quantity` scripts a mismatch. A fake that only ever agreed with the request
        could not distinguish "the channel verified the read-back" from "the channel trusted a
        response", which is the distinction every identity test here depends on.
        """
        sequence = len(self.orders) + 1
        order = MerchantOrder(
            order_id=f"{ORDER_GID_PREFIX}9{sequence:015}",
            name=f"#{1000 + sequence}",
            po_number=correlation_id if self.stamp_po_number else None,
            currency=self.publish_currency,
            source_name="merchant-purchase-channel",
            financial_status="PENDING",
            created_at="2026-09-28T00:00:00Z",
            lines=(
                OrderLine(
                    line_item_id=f"gid://shopify/LineItem/188019{sequence:07}",
                    product_id=self.product_gid,
                    variant_id=self.variant_gid or requested_variant,
                    quantity=self.line_quantity
                    if self.line_quantity is not None
                    else requested_quantity,
                ),
            ),
        )
        self.orders[correlation_id] = self.orders.get(correlation_id, []) + [order]
        return order

    async def read_back(self, correlation_id: str) -> list[MerchantOrder]:
        """A read. It never writes: a reconciliation that invents an order is the bug this models."""
        self.read_back_calls.append(correlation_id)
        if self.read_back_override is not None:
            return list(self.read_back_override)
        if self.read_back_behavior == "none":
            return []
        stored = self.orders.get(correlation_id, [])
        if self.read_back_behavior == "two":
            first = stored[0] if stored else self._make(
                correlation_id, requested_variant=self.variant_gid, requested_quantity=1
            )
            second = MerchantOrder(
                order_id=first.order_id[:-1] + "9",
                name="#1999",
                po_number=correlation_id,
                currency=first.currency,
                source_name=first.source_name,
                financial_status=first.financial_status,
                created_at=first.created_at,
                lines=first.lines,
            )
            return [first, second]
        return list(stored)

    async def shop_identity(self):
        self.shop_identity_calls += 1
        return self.shop_name, self.shop_currency
