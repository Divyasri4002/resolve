"""
M20.1 — ambiguous purchase recovery: the fail-closed policy, injected and measured.

The rule the whole file exists to pin: a zero-result search is proof of absence only when
something independent of the search vouches for it. Shopify's order search is eventually
consistent — an order it created has been observed invisible to `orders(query:)` right after
creation — so:

    the request never left (sent_at IS NULL)  ->  zero results vouch for absence; retry is licensed.
    a definitive provider refusal              ->  the refusal vouches; retry is licensed.
    an unknown outcome whose request left      ->  nothing vouches; the row stays closed, however
                                                   many empty answers accumulate, and only a
                                                   recorded human authorization licenses exactly
                                                   one more create.

Every test checks the counters — how many creates reached the store, whether anything was signed
and sent — not merely the status string.
"""
from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timezone

import pytest
from conftest import handoff, make_config

from merchant_channel.channel import CONFIRMED_LINKED, PurchaseChannel
from merchant_channel.correlation import correlation_id as derive_correlation
from merchant_channel.correlation import payload_hash as derive_payload_hash
from merchant_channel.fake_store import FakeAdminStore
from merchant_channel.outbox import (
    CONFIRMED,
    OUTCOME_UNKNOWN,
    CreateNotAuthorized,
    Outbox,
)

_CLOCK = lambda: datetime(2026, 9, 28, 12, 0, 0, tzinfo=timezone.utc)  # noqa: E731
_AUTHORIZATION_REASON = (
    "operator checked the store admin directly: no order carries this correlation; "
    "accepting the risk that the original may still surface"
)


def _channel(store, outbox, resolve) -> PurchaseChannel:
    return PurchaseChannel(make_config(), store, outbox, clock=_CLOCK, http_client=resolve)


async def _seed_unknown(outbox, store, resolve):
    """
    The state every closed-row test starts from: the request left, its answer was lost, and a
    zero-result reconciliation has since recorded no_match without reopening the row.
    """
    store.create_behavior = "outcome_unknown"
    channel = _channel(store, outbox, resolve)
    unknown = await channel.fulfil(handoff())
    store.read_back_behavior = "none"
    await channel.reconcile(unknown.correlation_id)
    return unknown


async def test_an_unknown_outcome_followed_by_zero_search_results_stays_closed(
    channel, store, resolve, outbox
):
    store.create_behavior = "outcome_unknown"  # the request left; its answer was lost
    unknown = await channel.fulfil(handoff())
    assert unknown.status == "OUTCOME_UNKNOWN"

    store.read_back_behavior = "none"  # the index has not caught up
    reconciled = await channel.reconcile(unknown.correlation_id, handoff())
    assert reconciled.status == "AWAITING_RECONCILIATION"
    assert "does not prove absence" in reconciled.reason

    record = outbox.get(unknown.correlation_id)
    assert record.sent_at is not None, "the request did leave, and the row knows it"
    assert record.status == OUTCOME_UNKNOWN
    assert record.reconciliation_state == "no_match", "the zero-result read is recorded"
    assert record.may_create is False


async def test_several_consecutive_zero_results_never_accumulate_into_proof(
    channel, store, resolve, outbox
):
    store.create_behavior = "outcome_unknown"
    unknown = await channel.fulfil(handoff())
    store.read_back_behavior = "none"
    for _ in range(3):
        reconciled = await channel.reconcile(unknown.correlation_id)
        assert reconciled.status == "AWAITING_RECONCILIATION"
        assert "does not prove absence" in reconciled.reason

    record = outbox.get(unknown.correlation_id)
    assert record.status == OUTCOME_UNKNOWN and record.reconciliation_state == "no_match"
    assert record.may_create is False

    again = await channel.fulfil(handoff())
    assert again.status == "OUTCOME_UNKNOWN"
    assert len(store.create_calls) == 1, "three empty answers are not three proofs"
    assert resolve.requests == []


async def test_the_original_order_appearing_after_a_no_match_closes_the_row_the_honest_way(
    channel, store, resolve
):
    store.create_behavior = "outcome_unknown"
    unknown = await channel.fulfil(handoff())
    store.read_back_behavior = "none"
    await channel.reconcile(unknown.correlation_id)

    store.read_back_behavior = "one"  # the index caught up; the original is visible
    recovered = await channel.reconcile(unknown.correlation_id, handoff())
    assert recovered.status == CONFIRMED_LINKED
    assert recovered.external_order_id is not None
    assert len(store.create_calls) == 1
    assert len(resolve.requests) == 1


async def test_the_original_order_appearing_after_the_waiting_period_recovers(
    outbox, store, resolve, tmp_path
):
    unknown = await _seed_unknown(outbox, store, resolve)

    # The row sits closed through the waiting period, and a restarted process agrees.
    restarted = _channel(store, Outbox(tmp_path / "outbox.sqlite3"), resolve)
    stalled = await restarted.fulfil(handoff())
    assert stalled.status == "OUTCOME_UNKNOWN"
    assert len(store.create_calls) == 1

    store.read_back_behavior = "one"  # whenever the index catches up, however late
    recovered = await restarted.reconcile(unknown.correlation_id, handoff())
    assert recovered.status == CONFIRMED_LINKED
    assert len(store.create_calls) == 1


async def test_an_authorized_second_create_that_hits_the_originals_correlation_stops_at_ambiguity(
    outbox, store, resolve
):
    """
    The risk the human accepted, arriving: the authorized create goes out, and then the original
    order surfaces. Two orders now carry one correlation, and the channel's answer is to stop —
    two candidates are a human decision, and nothing is signed or sent on a guess. The spent
    authorization does not refill either: an ambiguity is not a state authorize_create accepts.
    """
    unknown = await _seed_unknown(outbox, store, resolve)
    outbox.authorize_create(unknown.correlation_id, reason=_AUTHORIZATION_REASON)

    store.create_behavior = "ok"  # the second create succeeds; the store now holds two orders
    store.read_back_behavior = "one"
    channel = _channel(store, outbox, resolve)
    outcome = await channel.fulfil(handoff())

    assert outcome.status == "AWAITING_RECONCILIATION"
    assert outcome.candidates == 2
    assert len(store.create_calls) == 2, "both creates went out; that is the risk the human took"
    assert resolve.requests == [], "ambiguity is never resolved by sending a pairing"

    record = outbox.get(unknown.correlation_id)
    assert record.status == OUTCOME_UNKNOWN
    assert record.reconciliation_state == "ambiguous"
    assert record.create_authorized_at is None, "one authorization, one create, spent"
    assert record.may_create is False

    again = await channel.fulfil(handoff())
    assert again.status == "OUTCOME_UNKNOWN"
    assert len(store.create_calls) == 2
    with pytest.raises(CreateNotAuthorized):
        outbox.authorize_create(unknown.correlation_id, reason=_AUTHORIZATION_REASON)


async def test_a_process_restart_during_recovery_does_not_forget_the_row_is_closed(
    outbox, store, resolve, tmp_path
):
    unknown = await _seed_unknown(outbox, store, resolve)

    restarted = _channel(store, Outbox(tmp_path / "outbox.sqlite3"), resolve)
    after_restart = await restarted.fulfil(handoff())
    assert after_restart.status == "OUTCOME_UNKNOWN"
    assert len(store.create_calls) == 1

    record = outbox.get(unknown.correlation_id)
    assert record.sent_at is not None, "the fact that a request left survives the restart"
    assert record.status == OUTCOME_UNKNOWN and record.reconciliation_state == "no_match"
    assert record.may_create is False

    still_closed = await restarted.reconcile(unknown.correlation_id)
    assert "does not prove absence" in still_closed.reason
    assert len(store.create_calls) == 1


async def test_a_human_authorization_survives_a_restart_and_licenses_exactly_one_create(
    outbox, store, resolve, tmp_path
):
    unknown = await _seed_unknown(outbox, store, resolve)
    authorized = outbox.authorize_create(unknown.correlation_id, reason=_AUTHORIZATION_REASON)
    assert authorized.create_authorized_at is not None
    assert authorized.create_authorization_reason == _AUTHORIZATION_REASON

    # The index has long since settled: a restarted process, a fresh search, exactly one order.
    fresh_store = FakeAdminStore()
    restarted = _channel(fresh_store, Outbox(tmp_path / "outbox.sqlite3"), resolve)
    recovered = await restarted.fulfil(handoff())

    assert recovered.status == CONFIRMED_LINKED
    assert len(fresh_store.create_calls) == 1

    record = outbox.get(unknown.correlation_id)
    assert record.status == CONFIRMED
    assert record.create_authorized_at is None, "the authorization was consumed by the create"
    assert record.create_authorization_reason is None

    again = await restarted.fulfil(handoff())
    assert again.status == CONFIRMED_LINKED, "the paired purchase re-delivers, it does not re-buy"
    assert len(fresh_store.create_calls) == 1


async def test_concurrent_reconciliations_cannot_reopen_a_closed_row(outbox, store, resolve):
    unknown = await _seed_unknown(outbox, store, resolve)
    channel = _channel(store, outbox, resolve)
    barrier = threading.Barrier(2)
    results: list = []
    errors: list = []

    def reconcile_once():
        barrier.wait(timeout=10)
        try:
            results.append(asyncio.run(channel.reconcile(unknown.correlation_id)))
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    threads = [threading.Thread(target=reconcile_once) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert len(results) == 2
    assert all(r.status == "AWAITING_RECONCILIATION" for r in results)
    assert all("does not prove absence" in r.reason for r in results)

    record = outbox.get(unknown.correlation_id)
    assert record.status == OUTCOME_UNKNOWN and record.reconciliation_state == "no_match"
    assert record.may_create is False
    assert len(store.create_calls) == 1


async def test_concurrent_authorized_attempts_create_exactly_once(
    outbox, store, resolve, tmp_path
):
    unknown = await _seed_unknown(outbox, store, resolve)
    outbox.authorize_create(unknown.correlation_id, reason=_AUTHORIZATION_REASON)

    # The index has settled and a fresh process takes the authorized attempt; two workers race it.
    fresh_store = FakeAdminStore()
    restarted = _channel(fresh_store, Outbox(tmp_path / "outbox.sqlite3"), resolve)
    correlation = unknown.correlation_id
    barrier = threading.Barrier(2)
    results: list = []
    failures: list = []

    def attempt():
        barrier.wait(timeout=10)
        try:
            results.append(asyncio.run(restarted.fulfil(handoff())))
        except Exception as exc:  # noqa: BLE001
            failures.append(repr(exc))

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert failures == []
    assert len(results) == 2
    assert all(r.correlation_id == correlation for r in results)
    assert {r.status for r in results} <= {CONFIRMED_LINKED, "OUTCOME_UNKNOWN"}
    assert CONFIRMED_LINKED in {r.status for r in results}
    assert len(fresh_store.create_calls) == 1, "two racers, one authorized create"
    if len(resolve.bodies) > 1:
        assert len(set(resolve.bodies)) == 1, "a redelivery is the same signed bytes, not a new claim"
    assert resolve.signed_events(), "the authorized create ended in a delivered pairing"

    record = outbox.get(correlation)
    assert record.status == CONFIRMED, "the winner's confirmation is never clobbered by the loser"
    assert record.create_authorized_at is None


async def test_a_crash_before_the_request_left_licenses_retry_after_zero_results(
    channel, outbox, store, resolve
):
    """
    Failure class (a): the process died after claiming the create but before anything was sent.
    `sent_at` is still NULL, and that is the one absence a zero-result search can vouch for all by
    itself — nothing left, so nothing can be out there.
    """
    planned = handoff()
    correlation = derive_correlation(planned)
    outbox.reserve(
        correlation_id=correlation,
        resolve_order_id=planned.resolve_order_id,
        payload_hash=derive_payload_hash(planned),
    )
    outbox.begin_create(correlation)  # claimed, then the process died before sending
    assert outbox.get(correlation).sent_at is None

    stalled = await channel.fulfil(handoff())
    assert stalled.status == "OUTCOME_UNKNOWN"
    assert outbox.get(correlation).sent_at is None, "nothing ever left; the row still knows that"

    store.read_back_behavior = "none"
    reconciled = await channel.reconcile(correlation)
    assert reconciled.status == "AWAITING_RECONCILIATION"
    assert "no request for this correlation ever left this channel" in reconciled.reason
    assert outbox.get(correlation).status == "RECONCILED"
    assert outbox.get(correlation).may_create is True

    store.read_back_behavior = "one"
    recovered = await channel.fulfil(handoff())
    assert recovered.status == CONFIRMED_LINKED
    assert len(store.create_calls) == 1


async def test_a_definitive_provider_refusal_licenses_retry_without_human_authorization(
    channel, outbox, store, resolve
):
    store.create_behavior = "definite_failure"
    refused = await channel.fulfil(handoff())
    assert refused.status == "FAILED"

    record = outbox.get(refused.correlation_id)
    assert record.status == "FAILED"
    assert record.may_create is True, "Shopify said no; the absence is vouched for by the refusal"

    store.create_behavior = "ok"
    retried = await channel.fulfil(handoff())
    assert retried.status == CONFIRMED_LINKED
    assert len(store.create_calls) == 2, "the retry is deterministic, not a second purchase"


async def test_authorization_is_refused_outside_its_one_state(channel, outbox, store, resolve):
    store.create_behavior = "outcome_unknown"  # the request left; no reconciliation has run yet
    unknown = await channel.fulfil(handoff())
    assert unknown.status == "OUTCOME_UNKNOWN"

    with pytest.raises(CreateNotAuthorized):
        outbox.authorize_create(unknown.correlation_id, reason=_AUTHORIZATION_REASON)
    assert outbox.get(unknown.correlation_id).create_authorized_at is None

    store.read_back_behavior = "none"  # the reconciliation that reports no order
    await channel.reconcile(unknown.correlation_id)

    authorized = outbox.authorize_create(unknown.correlation_id, reason=_AUTHORIZATION_REASON)
    assert authorized.create_authorized_at is not None


async def test_an_empty_search_never_reopens_a_verified_order(channel, outbox, store, resolve):
    linked = await channel.fulfil(handoff())
    assert linked.status == CONFIRMED_LINKED

    store.read_back_behavior = "none"  # a later search that comes up empty
    observed = await channel.reconcile(linked.correlation_id)
    assert observed.status == "AWAITING_RECONCILIATION"
    assert "already found, verified and confirmed" in observed.reason

    record = outbox.get(linked.correlation_id)
    assert record.status == CONFIRMED, "the confirmed purchase stands"
    assert record.reconciliation_state == "no_match", "the empty answer is recorded, not obeyed"
    assert record.may_create is False
    with pytest.raises(CreateNotAuthorized):
        outbox.authorize_create(linked.correlation_id, reason=_AUTHORIZATION_REASON)

    again = await channel.fulfil(handoff())
    assert again.status == CONFIRMED_LINKED, "the pairing is re-delivered, not re-bought"
    assert len(store.create_calls) == 1
