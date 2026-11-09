"""
B9 — the seventeen failure cases it names, plus the eighteenth the real route forced.

The distinction that matters throughout is between a refusal and an unknown. A refusal is knowledge
("Shopify rejected the variant, so nothing exists"), and retrying it is correct. An unknown is the
absence of knowledge ("the answer never arrived"), and retrying it is how a shopper ends up with two
orders for one purchase — which is exactly why the 2026-07 schema's lack of an idempotency key on
`draftOrderCreate`/`draftOrderComplete`/`orderCreate` had to be measured before this file was written.

So each case below checks the counters: how many creates reached the merchant store, whether a
read-back happened, and whether a signed event left the channel. Never merely the status string.
"""
from __future__ import annotations

import json

import pytest
from conftest import handoff, make_config
from merchant_channel.config import Secret

from merchant_channel.channel import PurchaseChannel
from merchant_channel.outbox import (
    CONFIRMED,
    LINKAGE_DELIVERY_UNKNOWN,
    LINKAGE_NOT_SENT,
    LINKAGE_REFUSED,
    LINKAGE_SENT,
    OUTCOME_UNKNOWN,
)


# ------------------------------------------------------------------ 1 + 2 + 11 + 12
async def test_case_1_creation_succeeds_and_the_pairing_is_delivered(channel, store, resolve):
    outcome = await channel.fulfil(handoff())
    assert outcome.status == "CONFIRMED_LINKED"
    assert outcome.external_order_id.startswith("gid://shopify/Order/")
    assert len(store.create_calls) == 1
    assert len(resolve.requests) == 1
    assert resolve.signed_events()[0]["shopify_order_id"] == outcome.external_order_id


async def test_case_2_creation_is_refused_and_nothing_is_claimed(channel, store, resolve, outbox):
    store.create_behavior = "definite_failure"
    outcome = await channel.fulfil(handoff())
    assert outcome.status == "FAILED"
    assert len(store.create_calls) == 1
    # A create that Shopify rejected is not a read-back candidate, and it is certainly not an event.
    assert store.read_back_calls == []
    assert resolve.requests == []
    assert outbox.get(outcome.correlation_id).linkage == LINKAGE_NOT_SENT


async def test_case_11_the_same_resolve_order_retried_creates_exactly_one_order(
    channel, store, resolve
):
    first = await channel.fulfil(handoff())
    second = await channel.fulfil(handoff())
    assert first.correlation_id == second.correlation_id
    assert len(store.create_calls) == 1, "a retry must not become a second purchase"
    assert second.status == "CONFIRMED_LINKED"


async def test_case_12_the_same_correlation_retried_after_an_unknown_is_read_not_created(
    channel, store, resolve
):
    store.create_behavior = "outcome_unknown"
    unknown = await channel.fulfil(handoff())
    assert unknown.status == "OUTCOME_UNKNOWN"
    assert len(store.create_calls) == 1
    assert resolve.requests == []

    # The reconciliation reads first. Because the order really did land, it is confirmed and paired —
    # and the create count has still not moved.
    reconciled = await channel.reconcile(unknown.correlation_id, handoff())
    assert reconciled.status == "CONFIRMED_LINKED"
    assert len(store.create_calls) == 1
    assert len(resolve.requests) == 1


# ------------------------------------------------------------------ 3 + 4 + 6 + 7
async def test_case_3_creation_times_out_and_is_never_retried_blind(channel, store, resolve):
    store.create_behavior = "outcome_unknown"
    outcome = await channel.fulfil(handoff())
    assert outcome.status == "OUTCOME_UNKNOWN"
    assert "lost" in outcome.reason

    again = await channel.fulfil(handoff())
    assert again.status == "OUTCOME_UNKNOWN", (
        "the second call must not be allowed to reach Shopify — that is the duplicate order"
    )
    assert len(store.create_calls) == 1
    assert resolve.requests == []


async def test_case_4_a_lost_response_is_reconciled_by_reading_back(channel, store, resolve):
    """The request landed, the answer did not. Only Shopify's read can tell those apart."""
    store.create_behavior = "outcome_unknown"
    unknown = await channel.fulfil(handoff())

    matched = await channel.reconcile(unknown.correlation_id, handoff())
    assert matched.status == "CONFIRMED_LINKED"
    assert matched.external_order_id is not None
    assert len(store.create_calls) == 1


async def test_case_6_read_back_finds_nothing_so_nothing_is_claimed(channel, store, resolve, outbox):
    store.read_back_behavior = "none"
    outcome = await channel.fulfil(handoff())
    assert outcome.status == "OUTCOME_UNKNOWN"
    assert resolve.requests == []
    record = outbox.get(outcome.correlation_id)
    assert record.status == OUTCOME_UNKNOWN
    assert record.may_create is False  # still unknown: a create waits for a reconciliation


async def test_case_7_read_back_finds_two_candidates_and_stops(channel, store, resolve):
    store.read_back_behavior = "two"
    outcome = await channel.fulfil(handoff())
    assert outcome.status == "AWAITING_RECONCILIATION"
    assert outcome.candidates == 2
    assert resolve.requests == [], "picking one of two orders is a guess, and a guess is not a pairing"


# ------------------------------------------------------------------ 5 + 8 + 9 + 10
async def test_case_5_read_back_of_exactly_one_matching_order_pairs(channel, store):
    await channel.fulfil(handoff())
    assert store.read_back_calls and len(store.read_back_calls) == 1


async def test_case_8_correlation_matches_but_the_product_does_not(channel, store, resolve, outbox):
    store.product_gid = "gid://shopify/Product/10622982717727"
    outcome = await channel.fulfil(handoff())
    assert outcome.status == "REFUSED_IDENTITY"
    assert "different product" in outcome.reason
    assert resolve.requests == []
    assert outbox.get(outcome.correlation_id).linkage == LINKAGE_REFUSED


async def test_case_9_correlation_matches_but_the_variant_does_not(channel, store, resolve):
    store.variant_gid = "gid://shopify/ProductVariant/52923835973919"
    outcome = await channel.fulfil(handoff())
    assert outcome.status == "REFUSED_IDENTITY"
    assert "variant" in outcome.reason
    assert resolve.requests == []


async def test_case_9b_the_variant_appears_on_no_line_at_all(channel, store, resolve):
    """The read-back has to name the line; 'no line' is a refusal, not an empty pairing."""
    store.stamp_po_number = True
    store.variant_gid = "gid://shopify/ProductVariant/52923836039455"
    outcome = await channel.fulfil(handoff())
    assert outcome.status == "REFUSED_IDENTITY"
    assert "0 line(s)" in outcome.reason
    assert resolve.requests == []


async def test_case_9c_the_quantity_sold_is_not_the_quantity_asked(channel, store, resolve):
    store.line_quantity = 3
    outcome = await channel.fulfil(handoff())
    assert outcome.status == "REFUSED_IDENTITY"
    assert "3 unit(s)" in outcome.reason
    assert resolve.requests == []


async def test_case_10_the_order_was_read_from_a_different_merchant(channel, store, resolve):
    store.shop_name = "Someone Else's Store"
    outcome = await channel.fulfil(handoff())
    assert outcome.status == "REFUSED_IDENTITY"
    assert "not the configured" in outcome.reason
    assert resolve.requests == []


async def test_a_missing_correlation_stamp_is_a_refusal(channel, store, resolve):
    """
    An order with no `poNumber` is not this purchase, however much else it looks like.

    This is the case every heuristic matcher would get wrong: Shopify sold the same variant, the
    quantities agree, the currency agrees — and nothing ties it to this order but a resemblance.
    """
    store.stamp_po_number = False
    outcome = await channel.fulfil(handoff())
    assert outcome.status == "REFUSED_IDENTITY"
    assert "not stamped with this correlation" in outcome.reason
    assert resolve.requests == []


# ------------------------------------------------------------------ 13 + 14 + 15 + 16 + 17
async def test_cases_13_16_a_redelivered_event_is_byte_identical_under_the_same_id(
    channel, store, resolve, outbox
):
    first = await channel.fulfil(handoff())
    second = await channel.fulfil(handoff())

    assert first.event_id == second.event_id
    assert resolve.bodies[0] == resolve.bodies[1], "a resend must not re-stamp issued_at"
    assert len(store.create_calls) == 1
    record = outbox.get(first.correlation_id)
    assert record.linkage == LINKAGE_SENT
    assert json.loads(record.event_body)["event_id"] == first.event_id


async def test_case_14_a_different_purchase_never_reuses_an_event_id(channel, store, resolve):
    """
    RESOLVE's 409 for "same event_id, different payload" is a guard, not a feature to lean on.

    The channel's own rule is stronger: a changed intent produces a changed correlation, and
    therefore a different event id, so the two purchases are never asked to share one claim.
    """
    one = await channel.fulfil(handoff())
    two = await channel.fulfil(handoff(quantity=2))
    assert one.event_id != two.event_id
    assert len({body for body in resolve.bodies}) == 2
    assert len(store.create_calls) == 2  # genuinely two purchases, not one retried


async def test_case_15_resolve_is_unavailable_after_the_order_exists(channel, store, resolve, outbox):
    resolve.fail_with = lambda url: ConnectionError("RESOLVE is not answering")
    outcome = await channel.fulfil(handoff())
    assert outcome.status == "DELIVERY_UNKNOWN"
    assert store.read_back_calls  # the order was confirmed locally before the delivery was attempted
    assert outbox.get(outcome.correlation_id).linkage == LINKAGE_DELIVERY_UNKNOWN

    # RESOLVE comes back. The same bytes go out under the same id; Shopify is never asked again.
    resolve.fail_with = None
    retry = await channel.fulfil(handoff())
    assert retry.status == "CONFIRMED_LINKED"
    assert retry.event_id == outcome.event_id
    assert len(store.create_calls) == 1


async def test_case_17_resolve_refuses_the_pairing_and_no_second_order_is_created(
    channel, store, resolve
):
    resolve.payload = {"detail": "That Shopify order is already linked to a different RESOLVE order"}
    resolve.status_code = 409
    outcome = await channel.fulfil(handoff())
    assert outcome.status == "REFUSED_LINKAGE"
    assert "HTTP 409" in outcome.reason
    assert len(store.create_calls) == 1, "a refused pairing is never a reason to buy again"
    assert store.read_back_calls == [outcome.correlation_id]


async def test_case_18_a_server_side_answer_from_resolve_is_unknown_not_a_verdict(
    channel, store, resolve, outbox
):
    """
    "I could not check" is not "no".

    RESOLVE answers 503 when its own Shopify read fails. Treating that as a refusal would let a
    transient outage close a legitimate pairing forever; treating it as knowledge would be a lie in
    the other direction. So the attempt is recorded as unresolved and re-sent with the same bytes,
    and only a settled answer from RESOLVE ends it.
    """
    resolve.status_code = 503
    resolve.payload = {"detail": "The merchant store could not confirm that order."}
    unresolved = await channel.fulfil(handoff())
    assert unresolved.status == "DELIVERY_UNKNOWN"
    assert "HTTP 503" in unresolved.reason
    assert outbox.get(unresolved.correlation_id).linkage == LINKAGE_DELIVERY_UNKNOWN

    # RESOLVE came back with a settled decision. The retry carries the identical bytes, and Shopify
    # is asked for no second order either way.
    resolve.status_code = 200
    resolve.payload = {"outcome": "rejected", "already_processed": True}
    settled = await channel.fulfil(handoff())
    assert settled.status == "REFUSED_LINKAGE"
    assert settled.event_id == unresolved.event_id
    assert len(store.create_calls) == 1


async def test_an_unsigned_delivery_is_never_attempted(outbox, store, resolve):
    """With no secret configured the channel has no voice: it refuses rather than send plain."""
    resolve.requests.clear()
    channel = PurchaseChannel(
        make_config(linkage_secret=Secret("")), store, outbox, http_client=resolve
    )
    outcome = await channel.fulfil(handoff())
    assert outcome.status == "REFUSED_LINKAGE"
    assert "secret" in outcome.reason
    assert resolve.requests == []
    assert len(store.create_calls) == 1  # the merchant order still exists; only the claim is withheld
