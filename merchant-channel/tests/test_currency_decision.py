"""
Phase A's currency decision, expressed as behavior the channel cannot talk its way around.

Option C (INR-denominated merchant order, INR payment, no FX anywhere) was chosen in
`docs/purchase-channel-and-currency-boundary.md` §11 on the basis of what this codebase already is:
a single-currency INR payment rail whose currency is a code constant, an INR-named refund ceiling,
and no exchange-rate implementation of any kind. The consequence tests here is the one that matters
for the pairing: the channel is the party that would have to lie about money, so it is the party
whose rules must be measurable.

A handoff in the wrong currency is refused before a create call exists — not after, and not by
fixing the number. Refusing early is the difference between "RESOLVE cannot pay for this store's
goods" and "RESOLVE paid ₹410 and told Shopify it paid $410".
"""
from __future__ import annotations

import json

import pytest
from conftest import PRODUCT_GID, VARIANT_GID, handoff, make_config

from merchant_channel.channel import PurchaseChannel, REFUSED_HANDOFF
from merchant_channel.documents import (
    DRAFT_ORDER_COMPLETE,
    DRAFT_ORDER_CREATE,
    draft_order_input,
)


async def test_a_handoff_in_the_payment_currency_creates_and_pairs(channel, store, resolve):
    outcome = await channel.fulfil(handoff())
    assert outcome.status == "CONFIRMED_LINKED"
    assert store.create_calls == [
        {"correlation_id": outcome.correlation_id, "variant_gid": VARIANT_GID, "quantity": 1}
    ]
    # Read-back happened exactly once, and it happened before the event was sent.
    assert store.read_back_calls == [outcome.correlation_id]
    assert len(resolve.requests) == 1


async def test_a_handoff_in_another_currency_is_refused_before_anything_is_created(
    channel, store, resolve
):
    outcome = await channel.fulfil(handoff(currency="USD"))
    assert outcome.status == REFUSED_HANDOFF
    assert "does not convert" in outcome.reason
    assert store.create_calls == []
    assert store.read_back_calls == []
    assert resolve.requests == []


async def test_the_channel_refuses_to_pair_a_merchant_order_it_did_not_expect(
    outbox, store, resolve
):
    """
    The currency check is not only on the request — it is on what Shopify actually published.

    A store whose own currency is USD answers `Order.currencyCode == "USD"` even when the handoff
    asked for INR, and that mismatch stops the pairing rather than being reconciled by a rate.
    """
    store.publish_currency = "USD"
    outcome = await PurchaseChannel(make_config(), store, outbox).fulfil(handoff())
    assert outcome.status == "REFUSED_IDENTITY"
    assert "USD" in outcome.reason and "INR" in outcome.reason
    assert resolve.requests == [], "a currency mismatch must never produce a linkage event"


async def test_the_create_payload_carries_no_price_and_no_conversion(channel, store):
    """
    The merchant publishes the price. A channel that set its own unit price would be inventing the
    number it later asks RESOLVE to corroborate, so the payload names a variant and a quantity only.
    """
    await channel.fulfil(handoff())
    assert store.create_calls[0]["quantity"] == 1
    payload = draft_order_input(
        correlation_id="RESOLVE-order_x-000000000000", variant_gid=VARIANT_GID, quantity=2
    )
    assert "price" not in json.dumps(payload).lower()
    assert set(payload["lineItems"][0]) == {"variantId", "quantity"}


@pytest.mark.parametrize(
    "document", [DRAFT_ORDER_CREATE, DRAFT_ORDER_COMPLETE], ids=["create", "complete"]
)
def test_the_channel_never_claims_a_payment_it_did_not_receive(document):
    """
    B5, encoded in the documents rather than in a promise.

    `draftOrderComplete` does accept `paymentGatewayId`, and `orderMarkAsPaid` /
    `orderCreateManualPayment` exist in 2026-07 — so marking a merchant order paid is possible and
    would be a lie: no money reached this merchant through this path. The channel's own test payment
    fact stays on RESOLVE's side of the boundary.
    """
    for forbidden in ("paymentGatewayId", "orderMarkAsPaid", "orderCreateManualPayment", "capturable"):
        assert forbidden not in document


async def test_a_quantity_of_zero_or_negative_is_not_a_purchase(channel, store):
    for quantity in (0, -1, "1", 1.0, True):
        outcome = await channel.fulfil(handoff(quantity=quantity))
        assert outcome.status == REFUSED_HANDOFF, quantity
    assert store.create_calls == []


async def test_an_identity_that_is_not_a_canonical_gid_is_refused(channel, store):
    cases = [
        handoff(shopify_product_gid="10622982422815"),
        handoff(shopify_product_gid="gid://shopify/ProductVariant/52923834990879"),
        handoff(shopify_variant_gid="gid://shopify/Product/10622982422815"),
        handoff(shopify_variant_gid="sku-of-the-shampoo"),
        handoff(shopify_product_gid=PRODUCT_GID, shopify_variant_gid=PRODUCT_GID),
    ]
    for case in cases:
        outcome = await channel.fulfil(case)
        assert outcome.status == REFUSED_HANDOFF, case
    assert store.create_calls == []
