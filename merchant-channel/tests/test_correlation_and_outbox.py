"""
Correlation and the outbox: the two mechanisms that keep one purchase to one Shopify order.

Verified live against 2026-07, and the reason these tests exist at all: `draftOrderCreate(input)`,
`draftOrderComplete(id, paymentGatewayId, sourceName)` and `orderCreate(order, options)` expose no
idempotency-key argument (of 523 mutation fields only `appUsageRecordCreate` and
`orderCreateMandatePayment` carry one), and `custom_attribute:` is not a filter Shopify honours while
`po_number:` is. So the channel must name the purchase identically every time, stamp that name into a
field it can search by, and be unable to create a second order while the first one's outcome is
unknown.

That inability is not a convention here. `Outbox.begin_create` is the only door to a create, and it
refuses.
"""
from __future__ import annotations

import sqlite3

import pytest
from conftest import ORDER_ID, PRODUCT_GID, VARIANT_GID, handoff

from merchant_channel.correlation import (
    canonical_payload,
    correlation_id,
    is_correlation_id,
    payload_hash,
)
from merchant_channel.outbox import (
    AMBIGUOUS,
    CONFIRMED,
    FAILED,
    MATCHED,
    NO_MATCH,
    NOT_STARTED,
    OUTCOME_UNKNOWN,
    PENDING,
    RECONCILED,
    AmbiguousOutcome,
    Outbox,
)


# ------------------------------------------------------------------ correlation
def test_the_correlation_id_is_a_function_of_the_purchase_not_a_roll_of_the_dice():
    first = correlation_id(handoff())
    assert first == correlation_id(handoff())
    assert is_correlation_id(first)
    assert ORDER_ID in first


@pytest.mark.parametrize(
    "changed",
    [
        {"quantity": 2},
        {"currency": "USD"},
        {"shopify_variant_gid": "gid://shopify/ProductVariant/52923835973919"},
        {"shopify_product_gid": "gid://shopify/Product/10622982717727"},
        {"resolve_order_id": "order_somewhere_else"},
    ],
    ids=["quantity", "currency", "variant", "product", "resolve-order"],
)
def test_a_different_purchase_gets_a_different_correlation(changed):
    assert correlation_id(handoff(**changed)) != correlation_id(handoff())
    assert correlation_id(handoff(**changed)) != correlation_id(handoff()), changed


def test_the_correlation_payload_names_the_purchase_and_nothing_about_the_shopper():
    payload = canonical_payload(handoff())
    keys = set(__import__("json").loads(payload))
    assert keys == {
        "resolve_order_id",
        "product_gid",
        "variant_gid",
        "quantity",
        "currency",
    }
    assert "amount" not in payload, "the channel must not pin a price it does not own"
    assert payload == canonical_payload(handoff())  # byte-stable, so the hash is stable


def test_payload_hash_is_the_sha_of_the_canonical_payload():
    import hashlib

    # The stored hash is the whole digest, not a truncation: the only place a shortened form belongs
    # is the correlation id's 12-hex suffix, and a second truncation length would be a constant to
    # keep in sync with no reader who benefits from it.
    assert payload_hash(handoff()) == hashlib.sha256(
        canonical_payload(handoff()).encode("utf-8")
    ).hexdigest()


# ------------------------------------------------------------------ outbox rules
def test_a_new_correlation_starts_at_not_started(outbox: Outbox):
    record = outbox.reserve(
        correlation_id="RESOLVE-order_a-000000000000",
        resolve_order_id="order_a",
        payload_hash="0" * 16,
    )
    assert record.status == NOT_STARTED
    assert record.may_create is True
    # Reserving the same correlation again is the same row, not a second intent.
    again = outbox.reserve(
        correlation_id="RESOLVE-order_a-000000000000",
        resolve_order_id="order_a",
        payload_hash="0" * 16,
    )
    assert again.correlation_id == record.correlation_id
    assert len(outbox.all()) == 1


def test_begin_create_moves_the_row_to_pending_and_stamps_a_time(outbox: Outbox):
    outbox.reserve(correlation_id="RESOLVE-order_b-000000000000", resolve_order_id="order_b", payload_hash="1" * 16)
    record = outbox.begin_create("RESOLVE-order_b-000000000000")
    assert record.status == PENDING
    assert record.attempted_at


def test_an_unknown_outcome_blocks_the_next_create(outbox: Outbox):
    correlation = "RESOLVE-order_c-000000000000"
    outbox.reserve(correlation_id=correlation, resolve_order_id="order_c", payload_hash="2" * 16)
    outbox.begin_create(correlation)
    outbox.mark_outcome_unknown(correlation, "the answer was lost")

    with pytest.raises(AmbiguousOutcome) as refused:
        outbox.begin_create(correlation)
    assert "will not be created for it" in str(refused.value)
    assert outbox.get(correlation).status == OUTCOME_UNKNOWN


def test_a_dangling_pending_row_is_read_as_unknown_not_as_absent(outbox: Outbox):
    """A process that died between deciding and sending cannot be assumed to have failed."""
    correlation = "RESOLVE-order_d-000000000000"
    outbox.reserve(correlation_id=correlation, resolve_order_id="order_d", payload_hash="3" * 16)
    outbox.begin_create(correlation)

    with pytest.raises(AmbiguousOutcome):
        outbox.begin_create(correlation)
    assert outbox.get(correlation).status == OUTCOME_UNKNOWN


def test_reconciling_to_no_match_is_what_permits_a_create_again(outbox: Outbox):
    correlation = "RESOLVE-order_e-000000000000"
    outbox.reserve(correlation_id=correlation, resolve_order_id="order_e", payload_hash="4" * 16)
    outbox.begin_create(correlation)
    outbox.mark_outcome_unknown(correlation, "timeout")
    assert outbox.get(correlation).may_create is False

    outbox.mark_reconciled(correlation, NO_MATCH)
    assert outbox.get(correlation).status == RECONCILED
    assert outbox.get(correlation).may_create is True
    outbox.begin_create(correlation)  # now a deterministic step, not a guess

    outbox.mark_outcome_unknown(correlation, "timeout again")
    outbox.mark_reconciled(correlation, AMBIGUOUS)
    assert outbox.get(correlation).status == OUTCOME_UNKNOWN
    with pytest.raises(AmbiguousOutcome):
        outbox.begin_create(correlation)


def test_a_confirmed_row_never_creates_again(outbox: Outbox):
    correlation = "RESOLVE-order_f-000000000000"
    outbox.reserve(correlation_id=correlation, resolve_order_id="order_f", payload_hash="5" * 16)
    outbox.begin_create(correlation)
    outbox.mark_confirmed(correlation, "gid://shopify/Order/9000")
    assert outbox.get(correlation).status == CONFIRMED
    with pytest.raises(AmbiguousOutcome):
        outbox.begin_create(correlation)


def test_a_definitive_failure_may_be_retried_with_the_same_correlation(outbox: Outbox):
    correlation = "RESOLVE-order_g-000000000000"
    outbox.reserve(correlation_id=correlation, resolve_order_id="order_g", payload_hash="6" * 16)
    outbox.begin_create(correlation)
    outbox.mark_failed(correlation, "Shopify refused the variant")
    record = outbox.get(correlation)
    assert record.status == FAILED and record.reconciliation_state == NO_MATCH
    assert record.may_create is True
    outbox.begin_create(correlation)


def test_the_outbox_rejects_an_unknown_reconciliation_state(outbox: Outbox):
    correlation = "RESOLVE-order_h-000000000000"
    outbox.reserve(correlation_id=correlation, resolve_order_id="order_h", payload_hash="7" * 16)
    with pytest.raises(ValueError):
        outbox.mark_reconciled(correlation, "probably")


def test_two_different_purchases_for_one_resolve_order_are_two_rows_not_one(outbox: Outbox):
    """A changed intent must not be silently deduped into the order already made."""
    first, second = correlation_id(handoff()), correlation_id(handoff(quantity=2))
    for correlation, quantity in ((first, 1), (second, 2)):
        outbox.reserve(
            correlation_id=correlation,
            resolve_order_id=handoff(quantity=quantity).resolve_order_id,
            payload_hash=payload_hash(handoff(quantity=quantity)),
        )
    assert len(outbox.all()) == 2


def test_the_outbox_is_durable_across_a_restart(tmp_path):
    path = tmp_path / "outbox.sqlite3"
    first = Outbox(path)
    first.reserve(correlation_id="RESOLVE-order_i-000000000000", resolve_order_id="order_i", payload_hash="8" * 16)
    first.begin_create("RESOLVE-order_i-000000000000")
    first.mark_outcome_unknown("RESOLVE-order_i-000000000000", "process died")

    # The channel restarting is the normal case, and the refusal has to survive it.
    second = Outbox(path)
    assert second.get("RESOLVE-order_i-000000000000").status == OUTCOME_UNKNOWN
    with pytest.raises(AmbiguousOutcome):
        second.begin_create("RESOLVE-order_i-000000000000")


def test_the_storage_level_invariant_is_a_unique_index_not_a_hope(outbox: Outbox):
    with sqlite3.connect(outbox._path) as conn:
        indexes = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'purchase_outbox'"
            )
        ]
    assert "sqlite_autoindex_purchase_outbox_1" in indexes  # correlation_id PRIMARY KEY
    assert "ux_outbox_order_payload" in indexes
