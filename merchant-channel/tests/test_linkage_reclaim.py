"""
Task 14C.7L — reclaiming a pairing RESOLVE refused, without ever buying it twice.

14C.7K measured the corner: a merchant order exists and is verified, and RESOLVE answers the claim
about it "no". That answer is permanent for its `event_id` — by design, because a settled replay that
re-asked Shopify would be replay protection that re-runs the thing it is supposed to skip. The only
way the pairing can be stated again is as a new event, and the channel had no way to make one: its
`event_id` was a pure function of (correlation, order), so every retry was the same spent claim.

These tests measure the recovery. The distinction running through all of them is that **an attempt
number is not a purchase**: rotating a claim may change which event RESOLVE is answering, and may not
change, re-open, or re-run anything about the order itself.

    one correlation_id  ->  at most one Shopify order, however many claims are made about it

Every case counts creates, read-backs and deliveries. A status string alone would pass while a second
order was being bought.
"""
from __future__ import annotations

import json
import sqlite3

from conftest import SECRET, TOKEN, handoff, make_config
from merchant_channel.channel import PurchaseChannel
from merchant_channel.correlation import correlation_id as derive_correlation
from merchant_channel.linkage import event_id_for
from merchant_channel.outbox import (
    LINKAGE_DELIVERY_UNKNOWN,
    LINKAGE_NOT_SENT,
    LINKAGE_REFUSED,
    LINKAGE_SENT,
    OUTCOME_UNKNOWN,
    ReclaimNotAllowed,
)

REFUSAL_STATUS = 409
REFUSAL_PAYLOAD = {"detail": "That Shopify order is already linked to a different RESOLVE order"}


async def _refused_pairing(channel, store, resolve, outbox):
    """One real purchase, one real refusal: the state a reclaim begins from."""
    resolve.status_code = REFUSAL_STATUS
    resolve.payload = dict(REFUSAL_PAYLOAD)
    outcome = await channel.fulfil(handoff())
    assert outcome.status == "REFUSED_LINKAGE"
    record = outbox.get(outcome.correlation_id)
    assert record.linkage == LINKAGE_REFUSED
    return outcome, record


async def _accepted(resolve):
    resolve.status_code = 200
    resolve.payload = {"outcome": "linked"}


def _rewrite_stored_line(store, correlation, **changes):
    """
    Change what the store says about an order it already has, after the fact.

    `FakeAdminStore` builds the order once, at create, and a reclaim reads the stored order back —
    which is the honest shape, because a reclaim has to be tested against what the store answers
    *now*, not against a template that only affects orders not yet created. Mutating the store's
    fields would leave an already-issued order untouched and the test would pass while verifying
    nothing. `MerchantOrder`/`OrderLine` are frozen; this models the store changing its answer, not
    the channel editing a purchase.
    """
    from dataclasses import replace as _replace

    for stored in store.orders.get(correlation, []):
        object.__setattr__(stored, "lines", tuple(_replace(line, **changes) for line in stored.lines))


# --------------------------------------------------------------------------- the reclaim itself
async def test_a_refused_pairing_can_be_re_stated_under_a_new_event_identity(
    channel, store, resolve, outbox
):
    """
    The whole point of the task, measured.

    Attempt 1 is refused and stays refused. Attempt 2 carries the same pairing under a different
    `event_id`, and RESOLVE — which had already said no — accepts it. The correlation is the same
    object in both, because it is the same purchase.
    """
    first, record = await _refused_pairing(channel, store, resolve, outbox)
    await _accepted(resolve)

    second = await channel.reclaim_linkage(first.correlation_id, handoff())

    assert second.status == "CONFIRMED_LINKED"
    assert second.correlation_id == first.correlation_id == record.correlation_id
    assert second.event_id != first.event_id
    assert second.external_order_id == first.external_order_id
    events = resolve.signed_events()
    assert [e["event_id"] for e in events] == [first.event_id, second.event_id]
    assert events[0]["shopify_order_id"] == events[1]["shopify_order_id"]
    assert events[0]["resolve_order_id"] == events[1]["resolve_order_id"]


async def test_a_reclaim_never_creates_a_second_shopify_order(
    channel, store, resolve, outbox
):
    """
    The invariant the design has to earn.

    A refusal is the exact moment a naive retry loop would go and buy the thing again — the order
    exists, the pairing failed, so "create it once more" looks like it would help. It does not: the
    create count is the assertion that matters here, and it does not move.
    """
    outcome, _ = await _refused_pairing(channel, store, resolve, outbox)
    assert len(store.create_calls) == 1
    await _accepted(resolve)

    await channel.reclaim_linkage(outcome.correlation_id, handoff())

    assert len(store.create_calls) == 1, "a refused claim is not a reason to purchase again"
    assert store.read_back_calls  # the reclaim did look, at the store it already owns


async def test_a_reclaim_changes_the_attempt_and_nothing_about_the_purchase(
    channel, store, resolve, outbox
):
    """
    Byte-for-byte: the only field a reclaim may alter is the one that names the claim.

    Everything immutable-by-step-3 — the RESOLVE order, the merchant order, the correlation the event
    id is derived from — is compared directly. `issued_at` is allowed to differ, and must: a reclaimed
    event has to be fresh enough to survive RESOLVE's own window.
    """
    first, record = await _refused_pairing(channel, store, resolve, outbox)
    await _accepted(resolve)
    second = await channel.reclaim_linkage(first.correlation_id, handoff())

    before, after = (json.loads(body) for body in resolve.bodies)
    assert before["resolve_order_id"] == after["resolve_order_id"]
    assert before["shopify_order_id"] == after["shopify_order_id"]
    assert before["event_type"] == after["event_type"] == "resolve.order_linkage.v1"
    assert before["source"] == after["source"]
    assert before["event_id"] != after["event_id"]

    rotated = outbox.get(record.correlation_id)
    assert rotated.attempt == 2
    assert rotated.previous_event_id == first.event_id
    assert rotated.resolve_order_id == record.resolve_order_id
    assert rotated.payload_hash == record.payload_hash
    assert rotated.external_order_id == record.external_order_id
    assert rotated.correlation_id == derive_correlation(handoff())


async def test_the_channel_can_reclaim_twice_and_each_claim_is_distinct(
    channel, store, resolve, outbox
):
    """
    A refusal is not the end of the pairing's life, and neither is a second one.

    What keeps this safe is that each rotation *requires* a fresh settled refusal: attempt 3 exists
    only because attempt 2 was answered "no". So the chain can extend, and can only extend one link
    per answer.
    """
    first, _ = await _refused_pairing(channel, store, resolve, outbox)
    await channel.reclaim_linkage(first.correlation_id, handoff())
    second_event_id = resolve.signed_events()[1]["event_id"]

    third = await channel.reclaim_linkage(first.correlation_id, handoff())

    assert third.status == "REFUSED_LINKAGE"  # RESOLVE is still answering 409
    assert third.event_id != second_event_id
    assert {event["event_id"] for event in resolve.signed_events()} == {
        first.event_id,
        second_event_id,
        third.event_id,
    }
    assert outbox.get(first.correlation_id).attempt == 3
    assert len(store.create_calls) == 1, (
        "three claims about one purchase, one purchase — the create count is the whole invariant"
    )
    assert [row["outcome"] for row in outbox.linkage_history(first.correlation_id)] == [
        "REFUSED",
        "REFUSED",
        "REFUSED",
    ]


# --------------------------------------------------------------------------- what may not be reclaimed
async def test_an_unanswered_claim_is_not_a_refused_claim(channel, store, resolve, outbox):
    """
    "No" and "I never got an answer" are different states, and only the first is reclaimable.

    Minting a new identity over a lost answer would put two live claims on one purchase with no
    refusal between them — so the channel keeps re-delivering the same bytes under the same id.
    """
    resolve.fail_with = lambda url: ConnectionError("RESOLVE is not answering")
    unresolved = await channel.fulfil(handoff())
    assert unresolved.status == "DELIVERY_UNKNOWN"

    resolve.fail_with = None
    outcome = await channel.reclaim_linkage(unresolved.correlation_id, handoff())

    assert outcome.status == "DELIVERY_UNKNOWN"
    assert outcome.event_id == unresolved.event_id
    assert resolve.requests == []
    assert outbox.get(unresolved.correlation_id).linkage == LINKAGE_DELIVERY_UNKNOWN


async def test_a_paired_claim_has_nothing_to_reclaim(channel, store, resolve, outbox):
    await channel.fulfil(handoff())
    delivered = len(resolve.requests)
    outcome = await channel.reclaim_linkage(
        derive_correlation(handoff()), handoff()
    )
    assert outcome.status == "CONFIRMED_LINKED"
    assert len(resolve.requests) == delivered, "a second claim about an accepted pairing is a duplicate claim"
    assert outbox.get(outcome.correlation_id).linkage == LINKAGE_SENT


async def test_a_reclaim_needs_a_previous_attempt(outbox, store, resolve):
    """Nothing was ever claimed, so nothing was ever refused: there is no spent identity to replace."""
    channel = PurchaseChannel(make_config(), store, outbox, http_client=resolve)
    correlation = derive_correlation(handoff())
    outbox.reserve(correlation_id=correlation, resolve_order_id="order_x", payload_hash="hash")
    outcome = await channel.reclaim_linkage(correlation, handoff())
    assert outcome.status == "FAILED"
    assert outbox.get(correlation).linkage == LINKAGE_NOT_SENT
    assert resolve.requests == []


async def test_the_outbox_refuses_to_rotate_a_row_that_was_never_refused(outbox):
    """The permission lives in the storage layer, so no caller can talk its way past it."""
    from merchant_channel.outbox import CONFIRMED

    correlation = derive_correlation(handoff())
    outbox.reserve(
        correlation_id=correlation, resolve_order_id="order_x", payload_hash="h"
    )
    outbox.mark_confirmed(correlation, "gid://shopify/Order/910000000000001")
    assert outbox.get(correlation).status == CONFIRMED
    try:
        outbox.begin_linkage_attempt(correlation)
    except ReclaimNotAllowed as exc:
        assert "never answered no" in str(exc)
    else:
        raise AssertionError("a non-refused row must not be rotatable")


async def test_a_reclaim_is_not_a_way_to_present_a_different_purchase(
    channel, store, resolve, outbox
):
    """
    The correlation is checked against the handoff before anything is read.

    A reclaim that took a fresh handoff and re-used an old correlation would be the one design flaw
    that makes two purchases share a name — so the mismatch is refused without asking Shopify or
    RESOLVE anything.
    """
    outcome, _ = await _refused_pairing(channel, store, resolve, outbox)
    swapped = handoff(quantity=2)

    refused = await channel.reclaim_linkage(outcome.correlation_id, swapped)

    assert refused.status == "REFUSED_IDENTITY"
    assert "different purchase" in refused.reason
    assert store.read_back_calls == [outcome.correlation_id]  # nothing further was looked at
    assert len(resolve.requests) == 1  # still only the original delivery


# --------------------------------------------------------------------------- re-verification on the new attempt
async def test_a_reclaim_re_verifies_the_product(channel, store, resolve, outbox):
    outcome, _ = await _refused_pairing(channel, store, resolve, outbox)
    await _accepted(resolve)
    _rewrite_stored_line(store, outcome.correlation_id, product_id="gid://shopify/Product/10622982717727")

    refused = await channel.reclaim_linkage(outcome.correlation_id, handoff())

    assert refused.status == "REFUSED_IDENTITY"
    assert "different product" in refused.reason
    assert len(resolve.requests) == 1, "a reclaim that fails verification sends nothing"
    assert len(store.create_calls) == 1


async def test_a_reclaim_re_verifies_the_variant(channel, store, resolve, outbox):
    outcome, _ = await _refused_pairing(channel, store, resolve, outbox)
    await _accepted(resolve)
    _rewrite_stored_line(
        store, outcome.correlation_id, variant_id="gid://shopify/ProductVariant/52923835973919"
    )

    refused = await channel.reclaim_linkage(outcome.correlation_id, handoff())

    assert refused.status == "REFUSED_IDENTITY"
    assert "0 line(s)" in refused.reason
    assert len(resolve.requests) == 1


async def test_a_reclaim_re_verifies_the_quantity(channel, store, resolve, outbox):
    outcome, _ = await _refused_pairing(channel, store, resolve, outbox)
    await _accepted(resolve)
    _rewrite_stored_line(store, outcome.correlation_id, quantity=5)

    refused = await channel.reclaim_linkage(outcome.correlation_id, handoff())

    assert refused.status == "REFUSED_IDENTITY"
    assert "5 unit(s)" in refused.reason
    assert len(resolve.requests) == 1


async def test_a_reclaim_re_verifies_the_store(channel, store, resolve, outbox):
    outcome, _ = await _refused_pairing(channel, store, resolve, outbox)
    await _accepted(resolve)
    store.shop_name = "Someone Else's Store"

    refused = await channel.reclaim_linkage(outcome.correlation_id, handoff())

    assert refused.status == "REFUSED_IDENTITY"
    assert "not the configured" in refused.reason
    assert len(resolve.requests) == 1


async def test_a_reclaim_rechecks_the_correlation_stamp(channel, store, resolve, outbox):
    """An order that no longer carries the correlation is not this purchase, whatever it resembles."""
    outcome, _ = await _refused_pairing(channel, store, resolve, outbox)
    await _accepted(resolve)
    for orders in store.orders.values():
        for stored in orders:
            object.__setattr__(stored, "po_number", "RESOLVE-someone-elses-order")

    refused = await channel.reclaim_linkage(outcome.correlation_id, handoff())

    assert refused.status == "REFUSED_IDENTITY"
    assert "not stamped with this correlation" in refused.reason


# --------------------------------------------------------------------------- zero / one / many on the reclaim read
async def test_a_reclaim_that_finds_no_order_leaves_the_refusal_and_reopens_nothing(
    channel, store, resolve, outbox
):
    """
    The subtlest of the three counts, and the one a convenience retry would get wrong.

    Shopify holding no order for a refused correlation is *not* licence to create one: that would
    turn "RESOLVE declined to pair this" into "so the channel will go and buy it again". The row is
    left refused and `may_create` is left false.
    """
    outcome, _ = await _refused_pairing(channel, store, resolve, outbox)
    await _accepted(resolve)
    store.read_back_behavior = "none"

    awaited = await channel.reclaim_linkage(outcome.correlation_id, handoff())

    assert awaited.status == "AWAITING_RECONCILIATION"
    assert awaited.candidates == 0
    assert len(store.create_calls) == 1
    assert len(resolve.requests) == 1
    record = outbox.get(outcome.correlation_id)
    assert record.may_create is False
    assert record.linkage == LINKAGE_REFUSED
    assert record.attempt == 1


async def test_a_reclaim_that_finds_two_orders_stops_as_ambiguous(
    channel, store, resolve, outbox
):
    outcome, _ = await _refused_pairing(channel, store, resolve, outbox)
    await _accepted(resolve)
    store.read_back_behavior = "two"

    awaited = await channel.reclaim_linkage(outcome.correlation_id, handoff())

    assert awaited.status == "AWAITING_RECONCILIATION"
    assert awaited.candidates == 2
    assert len(resolve.requests) == 1
    record = outbox.get(outcome.correlation_id)
    assert record.status == OUTCOME_UNKNOWN
    assert record.may_create is False


async def test_a_reclaim_adopts_the_one_order_the_store_names(channel, store, resolve, outbox):
    """One candidate is not a choice to make; it is the order. It is paired, not guessed."""
    outcome, _ = await _refused_pairing(channel, store, resolve, outbox)
    await _accepted(resolve)
    before = list(store.read_back_calls)

    paired = await channel.reclaim_linkage(outcome.correlation_id, handoff())

    assert paired.status == "CONFIRMED_LINKED"
    assert store.read_back_calls[len(before) :] == [outcome.correlation_id]
    assert outbox.get(outcome.correlation_id).attempt == 2


# --------------------------------------------------------------------------- fulfil cannot do this
async def test_repeated_fulfil_after_a_refusal_never_mints_a_second_identity(
    channel, store, resolve, outbox
):
    """
    The rotation is not a side effect of trying again.

    If an ordinary retry could advance the attempt, every delivery loop in existence would quietly
    become a claim factory. It cannot: the same bytes go out under the same id, forever refused.
    """
    outcome, _ = await _refused_pairing(channel, store, resolve, outbox)

    for _ in range(3):
        retry = await channel.fulfil(handoff())
        assert retry.event_id == outcome.event_id
        assert retry.status == "REFUSED_LINKAGE"

    assert len(store.create_calls) == 1
    assert {json.loads(body)["event_id"] for body in resolve.bodies} == {outcome.event_id}
    assert outbox.get(outcome.correlation_id).attempt == 1


# --------------------------------------------------------------------------- the audit
async def test_a_reclaim_is_auditable_and_the_chain_reads_backwards(
    channel, store, resolve, outbox
):
    """
    Original event id, new event id, correlation, attempt number, result, timestamp.

    Nothing else: no signature, no secret, no body. The audit is the part an operator has to be able
    to read, and a table that copies the signed payload would be a second place to leak from.
    """
    outcome, _ = await _refused_pairing(channel, store, resolve, outbox)
    await _accepted(resolve)
    reclaimed = await channel.reclaim_linkage(outcome.correlation_id, handoff())

    history = outbox.linkage_history(outcome.correlation_id)
    assert [row["attempt"] for row in history] == [1, 2]
    assert [row["outcome"] for row in history] == ["REFUSED", "SENT"]
    assert history[0]["event_id"] == outcome.event_id
    assert history[0]["supersedes_event_id"] is None
    assert history[1]["event_id"] == reclaimed.event_id
    assert history[1]["supersedes_event_id"] == outcome.event_id
    assert history[1]["external_order_id"] == reclaimed.external_order_id
    assert all(row["recorded_at"] for row in history)
    assert set(history[0]) == {
        "attempt",
        "event_id",
        "supersedes_event_id",
        "outcome",
        "external_order_id",
        "recorded_at",
    }


async def test_no_attempt_history_row_is_written_for_an_unanswered_delivery(
    channel, store, resolve, outbox
):
    """A lost answer is not a result. Recording one as history would be writing the absence of a fact."""
    resolve.fail_with = lambda url: ConnectionError("RESOLVE is not answering")
    await channel.fulfil(handoff())
    assert outbox.linkage_history(derive_correlation(handoff())) == []


async def test_secrets_and_signatures_never_enter_the_outbox(channel, store, resolve, outbox, tmp_path):
    outcome, _ = await _refused_pairing(channel, store, resolve, outbox)
    await _accepted(resolve)
    await channel.reclaim_linkage(outcome.correlation_id, handoff())

    conn = sqlite3.connect(str(tmp_path / "outbox.sqlite3"))
    try:
        dumped = " ".join(
            str(value)
            for table in ("purchase_outbox", "linkage_attempt_history")
            for row in conn.execute(f"SELECT * FROM {table}").fetchall()
            for value in row
        )
    finally:
        conn.close()
    assert SECRET not in dumped
    assert TOKEN not in dumped
    for request in resolve.requests:
        signature = request["headers"]["x-resolve-signature"]
        assert signature not in dumped


# --------------------------------------------------------------------------- the frozen derivation
def test_attempt_one_keeps_the_frozen_derivation_and_only_later_attempts_are_named():
    """
    Re-naming a settled attempt-1 claim would be the duplicate this file is about.

    So the preimage for attempt 1 must stay exactly what 14C.7I shipped, byte for byte, and an
    unknown attempt number must be a refusal rather than a fresh identity.
    """
    import hashlib

    correlation = "RESOLVE-order_recover001-000000000000"
    order = "gid://shopify/Order/910000000000001"
    frozen = "evt-" + hashlib.sha256(f"{correlation}|{order}".encode()).hexdigest()[:24]

    assert event_id_for(correlation_id=correlation, shopify_order_id=order) == frozen
    assert event_id_for(correlation_id=correlation, shopify_order_id=order, attempt=1) == frozen
    assert event_id_for(correlation_id=correlation, shopify_order_id=order, attempt=2) != frozen
    assert event_id_for(correlation_id=correlation, shopify_order_id=order, attempt=3) != event_id_for(
        correlation_id=correlation, shopify_order_id=order, attempt=2
    )


def test_a_bogus_attempt_number_is_refused_rather_than_ignored():
    from merchant_channel.linkage import LinkageContractError

    for bad in (0, -1, "2", True, None):
        try:
            event_id_for(correlation_id="RESOLVE-o-000000000000", shopify_order_id="gid://shopify/Order/1", attempt=bad)
        except LinkageContractError:
            continue
        raise AssertionError(f"attempt={bad!r} was accepted")


# --------------------------------------------------------------------------- the file migration
def test_an_existing_outbox_file_gains_the_attempt_columns_without_losing_a_row(tmp_path):
    """
    The outbox is the channel's only memory that a create already happened.

    An upgrade that recreated the table would forget it, and forgetting is how a second Shopify order
    gets made by a process that means well.
    """
    from merchant_channel.outbox import OUTCOME_UNKNOWN as UNKNOWN
    from merchant_channel.outbox import Outbox

    path = tmp_path / "outbox.sqlite3"
    legacy = Outbox(path)
    correlation = derive_correlation(handoff())
    legacy.reserve(correlation_id=correlation, resolve_order_id="order_legacy", payload_hash="hash")
    legacy.begin_create(correlation)
    legacy.mark_outcome_unknown(correlation, "the create answer was lost")

    conn = sqlite3.connect(str(path))
    try:
        conn.execute("ALTER TABLE purchase_outbox DROP COLUMN previous_event_id")
        conn.execute("ALTER TABLE purchase_outbox DROP COLUMN attempt")
        conn.execute("DROP TABLE linkage_attempt_history")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(purchase_outbox)")}
        assert "attempt" not in columns
    finally:
        conn.close()

    upgraded = Outbox(path)
    record = upgraded.get(correlation)
    assert record.status == UNKNOWN
    assert record.resolve_order_id == "order_legacy"
    assert record.attempt == 1
    assert record.previous_event_id is None
    assert upgraded.linkage_history(correlation) == []
