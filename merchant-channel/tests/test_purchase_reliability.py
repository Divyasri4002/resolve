"""
M20 — purchase-execution reliability: the failure injections the older suites did not name.

The distinction this file protects is the one that keeps one purchase from becoming two orders:

    an outcome may only be called definite when nothing can still be true on Shopify's side.

The historical incidents both live exactly where that distinction is easy to get wrong: a GraphQL
`errors` array arriving beside a mutation payload the mutation *did* create (reading the errors as
"nothing happened" marks an acted mutation FAILED, the outbox reopens the row, and the retry is the
second order), and a read-back that answers zero because Shopify's index had not caught up. Every
test here injects one of those shapes — through the real gateway over a mock transport, or through
the channel with its outbox — and then checks the counters (how many creates reached Shopify,
whether a read-back ran, what the outbox now licenses), not merely the status string.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from datetime import datetime, timezone

import httpx
import pytest
from conftest import make_config, handoff, ORDER_ID, PRODUCT_GID, VARIANT_GID

from merchant_channel.channel import PurchaseChannel
from merchant_channel.correlation import correlation_id as derive_correlation
from merchant_channel.correlation import payload_hash as derive_payload_hash
from merchant_channel.documents import MerchantOrder, OrderLine
from merchant_channel.gateway import (
    GatewayDefiniteFailure,
    GatewayOutcomeUnknown,
    ShopifyAdminGateway,
)
from merchant_channel.outbox import (
    CONFIRMED,
    LINKAGE_REFUSED,
    NO_MATCH,
    OUTCOME_UNKNOWN,
    AmbiguousOutcome,
    Outbox,
)

DRAFT_ORDER_GID = "gid://shopify/DraftOrder/18989131727135"
ORDER_GID = "gid://shopify/Order/900000000000042"
STRANGER_ORDER_GID = "gid://shopify/Order/900000000000099"
FIELD_ERRORS = [
    {
        "message": "ACCESS_DENIED: this app is not approved to access the field",
        "extensions": {"code": "ACCESS_DENIED"},
    }
]


def _created_order_json(correlation: str) -> dict:
    """The order Shopify would report for this correlation, matching the handoff on every field."""
    return {
        "id": ORDER_GID,
        "name": "#1042",
        "poNumber": correlation,
        "currencyCode": "INR",
        "sourceName": "merchant-purchase-channel",
        "displayFinancialStatus": "PENDING",
        "createdAt": "2026-09-28T00:00:00Z",
        "lineItems": {
            "edges": [
                {
                    "node": {
                        "id": "gid://shopify/LineItem/42",
                        "quantity": 1,
                        "product": {"id": PRODUCT_GID},
                        "variant": {"id": VARIANT_GID},
                    }
                }
            ]
        },
    }


def _gateway_with(responses) -> tuple[ShopifyAdminGateway, list[dict]]:
    """A real gateway whose transport records every request and answers from `responses` in order.

    An int answer is answered as that HTTP status with an empty JSON body; an Exception answer is
    raised, which is how a lost read-back is injected.
    """
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        seen.append({"headers": dict(request.headers), "body": body, "url": str(request.url)})
        answer = responses[len(seen) - 1] if len(seen) <= len(responses) else responses[-1]
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, int):
            return httpx.Response(answer, json={})
        return httpx.Response(200, json=answer)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return ShopifyAdminGateway(make_config(), client=client), seen


def _channel(gateway, outbox, resolve) -> PurchaseChannel:
    return PurchaseChannel(
        make_config(),
        gateway,
        outbox,
        clock=lambda: datetime(2026, 9, 28, 12, 0, 0, tzinfo=timezone.utc),
        http_client=resolve,
    )


_CREATE_OK = {"data": {"draftOrderCreate": {"draftOrder": {"id": DRAFT_ORDER_GID}, "userErrors": []}}}
_COMPLETE_OK_CORRELATION = (
    lambda correlation: {
        "data": {
            "draftOrderComplete": {
                "draftOrder": {"id": DRAFT_ORDER_GID, "order": _created_order_json(correlation)},
                "userErrors": [],
            }
        }
    }
)


# ------------------------------------------------------------------ partial data and errors


async def test_a_field_level_error_beside_a_created_draft_is_an_acted_mutation():
    """
    The historical defect: ACCESS_DENIED beside a draft the mutation did create.

    The errors array is an infrastructure signal; the payload is the fact. This mutation acted, so
    the gateway returns the data and the caller's own checks decide — the create is never read as a
    definite failure, which is what would have reopened the outbox row and produced the retry that
    becomes a second order.
    """
    gateway, seen = _gateway_with(
        [
            {"errors": FIELD_ERRORS, "data": {"draftOrderCreate": {"draftOrder": {"id": DRAFT_ORDER_GID}, "userErrors": []}}},
            _COMPLETE_OK_CORRELATION(derive_correlation(handoff())),
        ]
    )
    order = await gateway.create_order(
        correlation_id=derive_correlation(handoff()),
        variant_gid=VARIANT_GID,
        quantity=1,
    )
    assert order.order_id == ORDER_GID
    assert len(seen) == 2
    assert "draftOrderCreate" in seen[0]["body"]["query"]
    assert "draftOrderComplete" in seen[1]["body"]["query"]


@pytest.mark.parametrize(
    "payload",
    [
        {"errors": FIELD_ERRORS},
        {"errors": FIELD_ERRORS, "data": {"draftOrderCreate": None}},
    ],
    ids=["no-data", "null-root-payload"],
)
async def test_a_graphql_error_with_no_mutation_payload_is_definite(payload):
    """Without the root payload the resolver refused before acting: a retry is a new attempt."""
    gateway, seen = _gateway_with([payload])
    with pytest.raises(GatewayDefiniteFailure):
        await gateway.create_order(
            correlation_id=derive_correlation(handoff()),
            variant_gid=VARIANT_GID,
            quantity=1,
        )
    assert len(seen) == 1, "a refused create is never followed by a completion or a retry"


async def test_a_read_that_answers_errors_is_still_definite():
    """A read creates nothing, so its errors stay a definite failure — unchanged by the mutation rule."""
    gateway, _seen = _gateway_with([{"errors": FIELD_ERRORS, "data": {"orders": None}}])
    with pytest.raises(GatewayDefiniteFailure):
        await gateway.read_back(derive_correlation(handoff()))


async def test_a_create_that_answers_without_naming_a_draft_is_unknown():
    """
    A payload came back but named no draft. Whether Shopify holds a draft from this mutation is
    precisely what cannot be told from here — a definite failure would invite the blind retry.
    """
    gateway, _seen = _gateway_with(
        [{"data": {"draftOrderCreate": {"draftOrder": None, "userErrors": []}}}]
    )
    with pytest.raises(GatewayOutcomeUnknown):
        await gateway.create_order(
            correlation_id=derive_correlation(handoff()),
            variant_gid=VARIANT_GID,
            quantity=1,
        )


async def test_an_error_beside_a_completed_order_still_parses_the_order():
    gateway, _seen = _gateway_with(
        [
            _CREATE_OK,
            {
                "errors": FIELD_ERRORS,
                "data": {
                    "draftOrderComplete": {
                        "draftOrder": {"id": DRAFT_ORDER_GID, "order": _created_order_json(derive_correlation(handoff()))},
                        "userErrors": [],
                    }
                },
            },
        ]
    )
    order = await gateway.create_order(
        correlation_id=derive_correlation(handoff()),
        variant_gid=VARIANT_GID,
        quantity=1,
    )
    assert order.order_id == ORDER_GID


async def test_the_historical_defect_end_to_end_is_a_confirmed_pairing_not_a_failure(outbox, resolve):
    """The whole flow over the defect shape: errors beside a created draft must end linked, once."""
    correlation = derive_correlation(handoff())
    gateway, seen = _gateway_with(
        [
            {"errors": FIELD_ERRORS, "data": {"draftOrderCreate": {"draftOrder": {"id": DRAFT_ORDER_GID}, "userErrors": []}}},
            _COMPLETE_OK_CORRELATION(correlation),
            {"data": {"orders": {"edges": [{"node": _created_order_json(correlation)}]}}},
            {"data": {"shop": {"name": "RESOLVE Commerce Lab", "currencyCode": "USD"}}},
        ]
    )
    channel = _channel(gateway, outbox, resolve)
    outcome = await channel.fulfil(handoff())
    assert outcome.status == "CONFIRMED_LINKED"
    assert outcome.external_order_id == ORDER_GID
    creates = [request for request in seen if "draftOrderCreate" in request["body"]["query"]]
    assert len(creates) == 1, "an acted mutation must never be retried"
    record = outbox.get(correlation)
    assert record.status == CONFIRMED
    assert resolve.signed_events()[0]["shopify_order_id"] == ORDER_GID


# ------------------------------------------------------------------ outbox: the claim and the stale conclusion


def test_a_stale_no_match_does_not_ride_along_with_a_new_unknown(outbox: Outbox):
    """
    The hole between two rules: `no_match` reopens a row for a create, and an unknown closes it.

    `no_match` said Shopify had no order *then*. A new attempt whose outcome is unknown says nothing
    about *now* — and if the stale conclusion rode along, the row would stay creatable while the
    fate of the very create whose outcome is unknown was still undecided. So the unknown clears the
    conclusion, and the row is closed until a fresh read reconciles it.
    """
    correlation = "RESOLVE-order_m-000000000000"
    outbox.reserve(correlation_id=correlation, resolve_order_id="order_m", payload_hash="a" * 16)
    outbox.begin_create(correlation)
    outbox.mark_outcome_unknown(correlation, "the create answer was lost")
    outbox.mark_reconciled(correlation, NO_MATCH)
    outbox.begin_create(correlation)  # licensed: a deterministic step, not a guess

    outbox.mark_outcome_unknown(correlation, "the second answer was lost")
    record = outbox.get(correlation)
    assert record.status == OUTCOME_UNKNOWN
    assert record.reconciliation_state is None
    assert record.may_create is False
    with pytest.raises(AmbiguousOutcome):
        outbox.begin_create(correlation)


def test_two_threads_cannot_both_claim_the_create(tmp_path):
    """
    begin_create's check and its flip to PENDING are one conditional UPDATE.

    Two processes can both observe a creatable row; only one UPDATE can match it. The loser is
    refused with what the winner left (PENDING), which is the exact moment a duplicate Shopify
    order would otherwise be born.
    """
    path = tmp_path / "outbox.sqlite3"
    first, second = Outbox(path), Outbox(path)
    correlation = "RESOLVE-order_t-000000000000"
    first.reserve(correlation_id=correlation, resolve_order_id="order_t", payload_hash="b" * 16)

    barrier = threading.Barrier(2, timeout=10)
    winners: list = []
    refusals: list = []

    def claim(outbox: Outbox) -> None:
        barrier.wait()
        try:
            winners.append(outbox.begin_create(correlation))
        except AmbiguousOutcome as refused:
            refusals.append(refused)

    threads = [threading.Thread(target=claim, args=(o,)) for o in (first, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert len(winners) == 1 and len(refusals) == 1
    assert winners[0].status == "PENDING"
    assert "will not be created for it" in str(refusals[0])


def test_a_concurrent_reserve_yields_one_row_and_no_crash(tmp_path):
    path = tmp_path / "outbox.sqlite3"
    outboxes = [Outbox(path) for _ in range(4)]
    correlation = "RESOLVE-order_r-000000000000"
    barrier = threading.Barrier(4, timeout=10)
    results: list = []
    failures: list = []

    def reserve(outbox: Outbox) -> None:
        barrier.wait()
        try:
            results.append(
                outbox.reserve(
                    correlation_id=correlation, resolve_order_id="order_r", payload_hash="c" * 16
                )
            )
        except Exception as exc:  # noqa: BLE001 - the test's whole point is that nothing escapes
            failures.append(exc)

    threads = [threading.Thread(target=reserve, args=(o,)) for o in outboxes]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert failures == []
    assert all(row.correlation_id == correlation for row in results)
    with sqlite3.connect(str(path)) as conn:
        count = conn.execute("SELECT COUNT(*) FROM purchase_outbox").fetchone()[0]
    assert count == 1


# ------------------------------------------------------------------ crashes, restarts, concurrency


async def test_a_crash_between_claim_and_create_recovers_as_unknown(outbox, store, resolve):
    """
    The process died after claiming the row and before sending anything.

    The restart must read that as "we meant to send and cannot tell whether we did" — unknown, not
    absent — and refuse until a read reconciles it. Nothing reached Shopify, and the restart is not
    allowed to assume that.
    """
    correlation = derive_correlation(handoff())
    outbox.reserve(
        correlation_id=correlation,
        resolve_order_id=ORDER_ID,
        payload_hash=derive_payload_hash(handoff()),
    )
    outbox.begin_create(correlation)  # the crash happens here

    restarted = PurchaseChannel(
        make_config(),
        store,
        outbox,
        clock=lambda: datetime(2026, 9, 28, 12, 0, 0, tzinfo=timezone.utc),
        http_client=resolve,
    )
    outcome = await restarted.fulfil(handoff())
    assert outcome.status == "OUTCOME_UNKNOWN"
    assert store.create_calls == []
    assert resolve.requests == []


async def test_a_restart_while_unknown_refuses_then_reconciles_without_a_second_create(
    outbox, store, resolve
):
    store.create_behavior = "outcome_unknown"  # the request left; the order exists; the answer is gone
    first = PurchaseChannel(
        make_config(),
        store,
        outbox,
        clock=lambda: datetime(2026, 9, 28, 12, 0, 0, tzinfo=timezone.utc),
        http_client=resolve,
    )
    unknown = await first.fulfil(handoff())
    assert unknown.status == "OUTCOME_UNKNOWN"

    restarted = PurchaseChannel(
        make_config(),
        store,
        outbox,
        clock=lambda: datetime(2026, 9, 28, 12, 0, 0, tzinfo=timezone.utc),
        http_client=resolve,
    )
    refused = await restarted.fulfil(handoff())
    assert refused.status == "OUTCOME_UNKNOWN"
    assert len(store.create_calls) == 1, "a restart must not become a second purchase"

    reconciled = await restarted.reconcile(unknown.correlation_id, handoff())
    assert reconciled.status == "CONFIRMED_LINKED"
    assert len(store.create_calls) == 1
    assert len(resolve.requests) == 1


async def test_two_concurrent_purchase_attempts_create_exactly_one_order(channel, store, resolve):
    results = await asyncio.gather(channel.fulfil(handoff()), channel.fulfil(handoff()))
    assert {outcome.status for outcome in results} == {"CONFIRMED_LINKED"}
    assert len(store.create_calls) == 1
    assert len(resolve.requests) == 2, "the second attempt redelivers the same bytes"
    assert resolve.bodies[0] == resolve.bodies[1]
    assert results[0].event_id == results[1].event_id


async def test_a_zero_result_reconcile_after_a_sent_create_never_reopens_the_row(
    channel, store, resolve, outbox
):
    """
    M20.1's fail-closed rule, end to end.

    The create request left and its answer was lost; Shopify's index has not caught up (the
    historical zero-order read-back), so a reconciliation honestly reports no order. That empty
    answer does not reopen the row: the request did leave, and an eventually-consistent search
    does not prove absence. Only a recorded human authorization may license the next create, and
    it is consumed by the attempt it licenses — which here ends unknown again, so the row closes
    and a fresh decision is needed. No create ever happens without one.
    """
    store.create_behavior = "outcome_unknown"
    first = await channel.fulfil(handoff())
    assert first.status == "OUTCOME_UNKNOWN"

    store.read_back_behavior = "none"
    reconciled = await channel.reconcile(first.correlation_id, handoff())
    assert reconciled.status == "AWAITING_RECONCILIATION"
    assert "does not prove absence" in reconciled.reason

    record = outbox.get(first.correlation_id)
    assert record.status == "OUTCOME_UNKNOWN"
    assert record.reconciliation_state == "no_match", "the zero-result read is recorded"
    assert record.may_create is False

    # Every later attempt is refused, however many empty answers accumulate.
    store.read_back_behavior = "one"
    second = await channel.fulfil(handoff())
    third = await channel.fulfil(handoff())
    assert second.status == "OUTCOME_UNKNOWN" and third.status == "OUTCOME_UNKNOWN"
    assert len(store.create_calls) == 1, "an unproven absence never becomes a second purchase"
    assert resolve.requests == []


# ------------------------------------------------------------------ reads after a created order


async def test_a_refused_read_back_after_a_created_order_is_unknown_not_failed(outbox, resolve):
    """
    The create returned without error; the read-back was refused. The refusal is about the read,
    not the order — an order may well exist, and calling the attempt FAILED would reopen the row
    for the second order.
    """
    correlation = derive_correlation(handoff())
    gateway, seen = _gateway_with(
        [
            _CREATE_OK,
            _COMPLETE_OK_CORRELATION(correlation),
            422,  # the read-back is refused
            {"data": {"orders": {"edges": [{"node": _created_order_json(correlation)}]}}},
            {"data": {"shop": {"name": "RESOLVE Commerce Lab", "currencyCode": "USD"}}},
        ]
    )
    channel = _channel(gateway, outbox, resolve)
    outcome = await channel.fulfil(handoff())
    assert outcome.status == "OUTCOME_UNKNOWN"
    record = outbox.get(correlation)
    assert record.status == OUTCOME_UNKNOWN
    assert record.may_create is False

    # RESOLVE is told nothing while the outcome is unknown, and a reconciliation that can read
    # again closes it without a second create.
    assert resolve.requests == []
    reconciled = await channel.reconcile(correlation, handoff())
    assert reconciled.status == "CONFIRMED_LINKED"
    creates = [request for request in seen if "draftOrderCreate" in request["body"]["query"]]
    assert len(creates) == 1


async def test_a_lost_read_back_after_a_created_order_is_unknown_not_failed(outbox, resolve):
    correlation = derive_correlation(handoff())
    gateway, _seen = _gateway_with(
        [
            _CREATE_OK,
            _COMPLETE_OK_CORRELATION(correlation),
            httpx.ReadTimeout("the read-back answer never arrived"),
        ]
    )
    channel = _channel(gateway, outbox, resolve)
    outcome = await channel.fulfil(handoff())
    assert outcome.status == "OUTCOME_UNKNOWN"
    assert "lost" in outcome.reason
    assert outbox.get(correlation).may_create is False


async def test_an_unreadable_shop_identity_is_unknown_not_a_pairing(outbox, resolve):
    """The order matches, but the store answering cannot be identified. Nothing is paired on a guess."""
    correlation = derive_correlation(handoff())
    gateway, _seen = _gateway_with(
        [
            _CREATE_OK,
            _COMPLETE_OK_CORRELATION(correlation),
            {"data": {"orders": {"edges": [{"node": _created_order_json(correlation)}]}}},
            httpx.ReadTimeout("the identity answer never arrived"),
        ]
    )
    channel = _channel(gateway, outbox, resolve)
    outcome = await channel.fulfil(handoff())
    assert outcome.status == "OUTCOME_UNKNOWN"
    assert resolve.requests == []
    assert outbox.get(correlation).may_create is False


# ------------------------------------------------------------------ identity


async def test_an_order_carrying_a_different_correlation_is_never_adopted(channel, store, resolve, outbox):
    """
    The stranger order resembles this purchase in every checkable way except the stamp.

    Same variant, same product, same quantity, same currency, same store — and a `poNumber`
    belonging to someone else. Resemblance is not identity: nothing is paired, nothing is sent,
    and the refusal is recorded against this correlation's linkage.
    """
    stranger = MerchantOrder(
        order_id=STRANGER_ORDER_GID,
        name="#1999",
        po_number="RESOLVE-order_someoneelse-000000000000",
        currency="INR",
        source_name="web",
        financial_status="PENDING",
        created_at="2026-09-28T00:00:00Z",
        lines=(
            OrderLine(
                line_item_id="gid://shopify/LineItem/1999",
                product_id=PRODUCT_GID,
                variant_id=VARIANT_GID,
                quantity=1,
            ),
        ),
    )
    store.read_back_override = [stranger]
    outcome = await channel.fulfil(handoff())
    assert outcome.status == "REFUSED_IDENTITY"
    assert "not stamped with this correlation" in outcome.reason
    assert resolve.requests == []
    record = outbox.get(outcome.correlation_id)
    assert record.linkage == LINKAGE_REFUSED
    assert record.may_create is False
