"""
Task 14C.7K §12 / 14C.7L — a merchant order that is CONFIRMED while RESOLVE refuses the linkage.

This is the case the deterministic `event_id` was always going to meet. The channel derives
`event_id` from the pairing itself so that a retry can never look like a new claim. That is exactly
what makes a *refusal* dangerous: if the first attempt is turned down for a reason that later goes
away, the claim it wanted to make is still true, but its identity is spent.

Everything here is measured against the real route over HTTP, with the real event state machine, a
fixture merchant store, and an in-memory database. Nothing is argued from the code's shape:

* a settled refusal is terminal **for that event id** — replaying the identical bytes after the
  underlying condition is corrected still answers the old refusal, and links nothing;
* the pairing is not lost. RESOLVE's own public wording names the way out ("this event id is now
  spent; a further attempt must arrive as a new event"), and it is measured to work: a fresh
  event identity carrying the same pairing links, with the same Shopify-side verification run
  independently all over again;
* and the new identity relaxes nothing. `event_id` guards replay. The thing that stops one merchant
  order from backing two RESOLVE orders is the UNIQUE index plus the single writer's ownership
  check, and both still refuse — which is why a second identity is a recovery path rather than a
  loophole.

14C.7K left one half unfinished and recorded it: the channel could not mint that second identity.
14C.7L closes it, so this file now measures the seam from both sides — a refused attempt 1 and an
accepted attempt 2, the identities derived by the channel's own code and posted to the real route.
What did not move: `event_id` stays permanently spent, replay protection is unchanged, and a second
identity buys a second attempt at *verification*, never a second purchase.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

import app.tools.shopify as shopify_package
from app.config import get_settings
from app.integrations import order_linkage
from app.integrations.order_linkage import SIGNATURE_HEADER, sign_body
from app.models.db_models import AuditLogRecord, IntegrationEventRecord, ResolveOrderRecord
from app.tools.shopify.linkage import ShopifyLinkageError
from app.tools.shopify.sanitizer import reset_registered_secrets

from tests.test_merchant_channel_linkage_acceptance import _CHANNEL_ROOT  # noqa: F401 (same mount rule)
from tests.test_refund_execution import restart_env  # noqa: F401
from tests.test_shopify_discovery_identity import mock_razorpay  # noqa: F401
from tests.test_shopify_pairing_channel_e2e import (  # noqa: F401
    FixtureStore,
    LINE_GID,
    OTHER_PRODUCT_GID,
    OTHER_VARIANT_GID,
    PRODUCT_GID,
    VARIANT_GID,
    ORDER_GID,
    _line,
    _linkage_events,
    _purchased,
    store_factory,
)

# A throwaway value generated for this process only. Not a credential: never committed, never
# printed, and the deployed default stays unset.
SECRET = "disposable-refused-recovery-secret-for-this-process-only"
ROUTE = "/integrations/shopify/order-linkage"
RECOVERY_EVENT_ID = "evt-recovery-0000000000000001"


@pytest.fixture(autouse=True)
def _clean_secrets():
    reset_registered_secrets()
    yield
    reset_registered_secrets()


@pytest.fixture(autouse=True)
def _no_live_shopify(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError(
            "No §12 test may construct the live Shopify client: the merchant store here is a "
            "fixture and no real order is being paired."
        )

    monkeypatch.setattr(shopify_package, "get_shopify_client", refuse)
    # 14C.7O: the return provider is built through the pair factory, so both names are refused.
    monkeypatch.setattr(shopify_package, "get_shopify_clients", refuse)


@pytest.fixture
def with_secret(monkeypatch):
    monkeypatch.setattr(get_settings(), "linkage_integration_secret", SECRET)
    return SECRET


def _payload(**overrides) -> dict:
    base = {
        "event_id": "evt-refused-0000000000000001",
        "event_type": "resolve.order_linkage.v1",
        "shopify_order_id": ORDER_GID,
        "issued_at": datetime.now(timezone.utc).isoformat(),
        "source": "merchant-purchase-channel",
    }
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not None}


def _signed(payload: dict) -> tuple[bytes, str]:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return body, sign_body(SECRET, body)


async def _post(client, body: bytes, signature: str):
    return await client.post(
        ROUTE, content=body, headers={SIGNATURE_HEADER: signature, "content-type": "application/json"}
    )


async def _events(test_session) -> list[IntegrationEventRecord]:
    async with test_session() as session:
        return list((await session.execute(select(IntegrationEventRecord))).scalars().all())


async def _reference(test_session, order_id: str) -> str | None:
    async with test_session() as session:
        return (await session.get(ResolveOrderRecord, order_id)).provider_order_reference


# --------------------------------------------------------------------------- #
# 1. the refusal settles, and correcting the cause does not reopen it
# --------------------------------------------------------------------------- #
async def test_a_settled_refusal_stays_settled_even_after_the_condition_is_corrected(
    restart_env, install_mock_discovery, store_factory, with_secret, mock_razorpay
):
    """
    Identical bytes can never turn a spent event id into a linkage.

    The first delivery is refused because Shopify does not confirm the order (the 503 shape a real
    outage looks like). Then the cause of the refusal is *fixed* — the store now answers for that
    order — and the same bytes are delivered again. RESOLVE still says `rejected`, links nothing,
    and asks Shopify nothing: `event_id` is the whole answer to a replay.
    """
    store = store_factory(known=())
    make_client, test_session = restart_env
    async with make_client() as client:
        _, order = await _purchased(client, install_mock_discovery, "refused@shop.test")
        body, signature = _signed(_payload(resolve_order_id=order["order_id"]))

        refused = await _post(client, body, signature)
        assert refused.status_code == 503
        assert "now spent" in refused.json()["detail"]
        asks_at_refusal = store.asks

        # The underlying condition is corrected. This is the moment a "retry the same event" design
        # would hope to succeed — and the moment it must not.
        store.known.add(ORDER_GID)
        replayed = await _post(client, body, signature)

        assert replayed.status_code == 200, replayed.text
        answered = replayed.json()
        assert answered["outcome"] == "rejected"
        assert answered["already_processed"] is True
        assert answered["provider_order_reference"] is None
        # Answered from storage: the replay asked the merchant store for nothing at all.
        assert store.asks == asks_at_refusal
        assert [event.outcome for event in await _events(test_session)] == ["rejected"]

    assert await _reference(test_session, order["order_id"]) is None


# --------------------------------------------------------------------------- #
# 2. the recovery path the protocol itself names
# --------------------------------------------------------------------------- #
async def test_the_protocol_names_the_recovery_path_and_it_is_a_new_event_identity(
    restart_env, install_mock_discovery, store_factory, with_secret, mock_razorpay
):
    """
    RESOLVE's public refusal text is a contract, not a apology — and it is true.

    It tells the caller the attempt "must arrive as a new event". Measured: a fresh `event_id`
    carrying the same pairing, after the cause of the refusal is gone, is verified from scratch and
    links. So option A ("reuse the same bytes forever") is not what this state machine supports,
    while option C ("a new identity") is explicitly supported by the protocol's own words.
    """
    store = store_factory(known=())
    make_client, test_session = restart_env
    async with make_client() as client:
        _, order = await _purchased(client, install_mock_discovery, "recover@shop.test")
        order_id = order["order_id"]

        first, first_sig = _signed(_payload(resolve_order_id=order_id))
        assert (await _post(client, first, first_sig)).status_code == 503

        store.known.add(ORDER_GID)
        retry, retry_sig = _signed(
            _payload(
                event_id=RECOVERY_EVENT_ID,
                resolve_order_id=order_id,
                issued_at=datetime.now(timezone.utc).isoformat(),
            )
        )
        recovered = await _post(client, retry, retry_sig)

        assert recovered.status_code == 200, recovered.text
        assert recovered.json()["outcome"] == "linked"
        assert recovered.json()["provider_order_reference"] == ORDER_GID
        assert recovered.json()["already_processed"] is False
        # A new identity means a NEW verification, not a remembered answer.
        assert ORDER_GID in store.reference_asks

    assert await _reference(test_session, order_id) == ORDER_GID
    outcomes = {event.event_id: event.outcome for event in await _events(test_session)}
    assert outcomes == {
        "evt-refused-0000000000000001": "rejected",
        RECOVERY_EVENT_ID: "linked",
    }


# --------------------------------------------------------------------------- #
# 3. a new identity is a new attempt, not a loophole
# --------------------------------------------------------------------------- #
async def test_a_new_event_identity_relaxes_nothing_about_duplicates(
    restart_env, install_mock_discovery, store_factory, with_secret, mock_razorpay
):
    """
    Two claims, two event ids, one merchant order: the index still decides.

    The reason a fresh identity is safe to allow is that `event_id` was never the duplicate guard
    for the *pairing*. That is `uq_resolve_orders_provider_order_reference` plus the writer's
    ownership check, and neither cares how the event is named.
    """
    store = store_factory(known=(ORDER_GID,))
    make_client, test_session = restart_env
    async with make_client() as client:
        _, first_order = await _purchased(client, install_mock_discovery, "holder@shop.test")
        _body, signature = _signed(_payload(resolve_order_id=first_order["order_id"]))
        assert (await _post(client, _body, signature)).status_code == 200

        _, second_order = await _purchased(client, install_mock_discovery, "challenger@shop.test")
        rival, rival_sig = _signed(
            _payload(
                event_id="evt-rival-00000000000000001",
                resolve_order_id=second_order["order_id"],
                issued_at=datetime.now(timezone.utc).isoformat(),
            )
        )
        refused = await _post(client, rival, rival_sig)
        assert refused.status_code == 409
        assert "already linked" in refused.json()["detail"]

        # And the original claim, re-issued under yet another identity, cannot become a second
        # pairing either: the row is already linked, so the writer confirms rather than moves it.
        again, again_sig = _signed(
            _payload(
                event_id="evt-again-00000000000000001",
                resolve_order_id=first_order["order_id"],
                issued_at=datetime.now(timezone.utc).isoformat(),
            )
        )
        confirmed = await _post(client, again, again_sig)
        assert confirmed.status_code == 200
        assert confirmed.json()["outcome"] == "already_linked"

    async with test_session() as session:
        assert (await session.get(ResolveOrderRecord, first_order["order_id"])
                ).provider_order_reference == ORDER_GID
        assert (await session.get(ResolveOrderRecord, second_order["order_id"])
                ).provider_order_reference is None
        linked = [
            event.outcome
            for event in await _events(test_session)
            if event.outcome in ("linked", "already_linked")
        ]
        assert sorted(linked) == ["already_linked", "linked"]


# --------------------------------------------------------------------------- #
# 4. where the gap was, and what closed it
# --------------------------------------------------------------------------- #
def test_attempt_one_keeps_the_frozen_derivation_and_a_second_needs_an_attempt_number():
    """
    14C.7K recorded this as a gap: `event_id_for` was a pure function of (correlation, order), so the
    channel had no second identity to mint and a refused pairing was terminal on both sides.

    The property that replaced it is narrower, and it is the one that matters: attempt 1's derivation
    is byte-frozen — so nothing that was already settled can be re-named by an upgrade — and a second
    identity exists only for a caller that has an attempt number, which only the outbox can hand out.
    """
    import sys

    if str(_CHANNEL_ROOT) not in sys.path:
        sys.path.insert(0, str(_CHANNEL_ROOT))
    from merchant_channel.linkage import event_id_for

    correlation = "RESOLVE-order_recover001-000000000000"
    first = event_id_for(correlation_id=correlation, shopify_order_id=ORDER_GID)
    assert first == event_id_for(correlation_id=correlation, shopify_order_id=ORDER_GID, attempt=1)
    assert first == event_id_for(
        correlation_id=correlation, shopify_order_id=ORDER_GID
    ), "a resend of attempt 1 is still the same spent claim, after this task as before it"

    second = event_id_for(correlation_id=correlation, shopify_order_id=ORDER_GID, attempt=2)
    assert second != first
    assert second == event_id_for(
        correlation_id=correlation, shopify_order_id=ORDER_GID, attempt=2
    ), "the second identity is as deterministic as the first, so it cannot become a claim factory"
    # A different pairing must still get a different identity, or replay protection would be
    # weakened in the opposite direction.
    assert event_id_for(
        correlation_id=correlation, shopify_order_id="gid://shopify/Order/910000000000002"
    ) != first


async def test_the_reclaimed_identity_the_channel_mints_is_accepted_by_the_real_route(
    restart_env, install_mock_discovery, store_factory, with_secret, mock_razorpay
):
    """
    The two halves now agree, and this is the test that says so.

    Nothing here is RESOLVE's own invented id: the handoff is built from the RESOLVE order row that
    production code wrote, the correlation and the attempt-2 event are derived by the channel's own
    `build_event`, and the bytes it would send are signed and posted to the real route. A refusal on
    attempt 1 then a link on attempt 2 is the §12 gap, closed at the seam rather than on one side of
    it.
    """
    import sys

    if str(_CHANNEL_ROOT) not in sys.path:
        sys.path.insert(0, str(_CHANNEL_ROOT))
    from merchant_channel.channel import PurchaseHandoff
    from merchant_channel.correlation import correlation_id as derive_correlation
    from merchant_channel.linkage import build_event, serialize_event

    store = store_factory(known=())
    make_client, test_session = restart_env
    async with make_client() as client:
        _, order = await _purchased(client, install_mock_discovery, "seam@shop.test")
        order_id = order["order_id"]

        # Attempt 1: the pairing the channel would claim, and RESOLVE cannot confirm the order.
        handoff = PurchaseHandoff(
            resolve_order_id=order_id,
            shopify_product_gid=PRODUCT_GID,
            shopify_variant_gid=VARIANT_GID,
            quantity=1,
            currency="INR",
        )
        correlation = derive_correlation(handoff)

        def _channel_event(attempt: int) -> bytes:
            return serialize_event(
                build_event(
                    correlation_id=correlation,
                    shopify_order_id=ORDER_GID,
                    resolve_order_id=order_id,
                    issued_at=datetime.now(timezone.utc).isoformat(),
                    source="merchant-purchase-channel",
                    attempt=attempt,
                )
            )

        attempt_one = _channel_event(1)
        refused = await _post(client, attempt_one, sign_body(SECRET, attempt_one))
        assert refused.status_code == 503
        spent_id = json.loads(attempt_one)["event_id"]

        store.known.add(ORDER_GID)
        attempt_two = _channel_event(2)
        recovered = await _post(client, attempt_two, sign_body(SECRET, attempt_two))

        assert recovered.status_code == 200, recovered.text
        assert recovered.json()["outcome"] == "linked"
        assert json.loads(attempt_two)["event_id"] != spent_id
        assert await _reference(test_session, order_id) == ORDER_GID

        # And the spent claim is still spent: a successful reclaim does not reopen attempt 1.
        again = await _post(client, attempt_one, sign_body(SECRET, attempt_one))
        assert again.status_code == 200
        assert again.json()["already_processed"] is True
        assert again.json()["outcome"] == "rejected"

    audits = await _linkage_events(test_session)
    assert {event.outcome for event in audits} == {"rejected", "linked"}


# --------------------------------------------------------------------------- #
# 5. what a new identity does NOT get to skip
# --------------------------------------------------------------------------- #
async def test_a_reclaimed_event_is_corroborated_against_the_product_and_variant_again(
    restart_env, install_mock_discovery, store_factory, with_secret, mock_razorpay
):
    """
    E2 is independently validated — measured, not assumed.

    The first claim is refused because the store could not confirm the order at all. Then the store
    *can* be asked, and its own lines disagree about which purchase the merchant order is. If a new
    identity skipped corroboration, this is where it would link. It does not: each mismatch below is
    Shopify's answer refusing the reclaim, and RESOLVE's row stays unlinked — a second attempt buys a
    second look at the merchant store, never a softer one.
    """
    cases = [
        (
            "product",
            [_line(LINE_GID, OTHER_PRODUCT_GID, VARIANT_GID)],
            "Shopify's own line data puts that variant on a different product",
        ),
        (
            "variant",
            [_line(LINE_GID, PRODUCT_GID, OTHER_VARIANT_GID)],
            "sells this product but not the variant RESOLVE sold",
        ),
        (
            "both",
            [_line(LINE_GID, OTHER_PRODUCT_GID, OTHER_VARIANT_GID)],
            "does not sell the product RESOLVE sold",
        ),
    ]
    for index, (_name, mismatched, expected) in enumerate(cases):
        store = store_factory(known=())
        make_client, test_session = restart_env
        async with make_client() as client:
            _, order = await _purchased(client, install_mock_discovery, f"corrob-{index}@shop.test")
            spent_id = f"evt-corrob-spent-{index}-00000001"
            first, first_sig = _signed(
                _payload(
                    event_id=spent_id,
                    resolve_order_id=order["order_id"],
                    issued_at=datetime.now(timezone.utc).isoformat(),
                )
            )
            refused_first = await _post(client, first, first_sig)
            assert refused_first.status_code == 503, refused_first.text

            # The outage is over, and the order is readable — as something that is not this purchase.
            store.known.add(ORDER_GID)
            store.lines = list(mismatched)
            reclaim_id = f"evt-reclaim-corroboration-{index}-00001"
            second, second_sig = _signed(
                _payload(
                    event_id=reclaim_id,
                    resolve_order_id=order["order_id"],
                    issued_at=datetime.now(timezone.utc).isoformat(),
                )
            )
            refused = await _post(client, second, second_sig)
            assert refused.status_code == 409, refused.text
            assert expected in refused.json()["detail"], refused.text
            assert await _reference(test_session, order["order_id"]) is None
            outcomes = {
                event.event_id: event.outcome for event in await _linkage_events(test_session)
            }
            assert outcomes[spent_id] == "rejected"
            assert outcomes[reclaim_id] == "rejected"
            # Across every mismatch tried here, no claim of any attempt has ever linked.
            assert "linked" not in outcomes.values()
            # The new identity really did go back to the merchant store rather than reuse an answer.
            assert store.line_asks.count(ORDER_GID) >= 1


async def test_a_second_resolve_order_cannot_re_claim_an_already_claimed_merchant_order(
    restart_env, install_mock_discovery, store_factory, with_secret, mock_razorpay
):
    """
    STEP 4 item 8, re-proved on a reclaim: a new identity does not unlock someone else's pairing.

    The duplicate guard for a *pairing* was never `event_id` — it is the unique index plus the
    writer's ownership check — so a second RESOLVE order arriving with a fresh event id for the same
    merchant order is refused, and refuses without touching the row that already holds it.
    """
    store = store_factory(known=(ORDER_GID,))
    make_client, test_session = restart_env
    async with make_client() as client:
        _, holder = await _purchased(client, install_mock_discovery, "holder@shop.test")
        body, signature = _signed(_payload(resolve_order_id=holder["order_id"]))
        assert (await _post(client, body, signature)).status_code == 200

        _, challenger = await _purchased(client, install_mock_discovery, "challenger@shop.test")
        rival, rival_sig = _signed(
            _payload(
                event_id="evt-reclaim-rival-000000001",
                resolve_order_id=challenger["order_id"],
                issued_at=datetime.now(timezone.utc).isoformat(),
            )
        )
        refused = await _post(client, rival, rival_sig)
        assert refused.status_code == 409
        assert "already linked" in refused.json()["detail"]
        assert await _reference(test_session, challenger["order_id"]) is None
        assert await _reference(test_session, holder["order_id"]) == ORDER_GID


async def test_a_reclaimed_event_is_still_bound_by_the_freshness_window(
    restart_env, install_mock_discovery, store_factory, with_secret, mock_razorpay
):
    """
    A new identity is a new claim, so it has to be a *current* one.

    Re-stamping `issued_at` is what makes a reclaimed body differ from the refused one, and the
    freshness rule is what stops that becoming a licence to file a captured event at leisure.
    """
    store = store_factory()
    stale = datetime.now(timezone.utc) - timedelta(days=400)
    make_client, test_session = restart_env
    async with make_client() as client:
        _, order = await _purchased(client, install_mock_discovery, "stale@shop.test")
        body, signature = _signed(
            _payload(
                event_id="evt-reclaim-stale-000000001",
                resolve_order_id=order["order_id"],
                issued_at=stale.isoformat(),
            )
        )
        refused = await _post(client, body, signature)
        assert refused.status_code == 400
        assert "window" in refused.json()["detail"]
        # Refused before it is claimed: no event row, and Shopify was never asked anything.
        assert await _linkage_events(test_session) == []
        assert store.asks == 0


async def test_no_reclaim_leaves_a_secret_or_a_signature_anywhere_it_is_stored(
    restart_env, install_mock_discovery, store_factory, with_secret, mock_razorpay
):
    """
    Two claims, two refusals-or-links, one rule: the HMAC material goes nowhere but the header.

    `integration_events` keeps a digest of the body, the audit keeps a sentence about the attempt, and
    neither is allowed to keep the means to forge either of them.
    """
    store = store_factory(known=())
    make_client, test_session = restart_env
    async with make_client() as client:
        _, order = await _purchased(client, install_mock_discovery, "leak@shop.test")
        first, first_sig = _signed(_payload(resolve_order_id=order["order_id"]))
        assert (await _post(client, first, first_sig)).status_code == 503
        store.known.add(ORDER_GID)
        second, second_sig = _signed(
            _payload(
                event_id="evt-reclaim-leak-check-0001",
                resolve_order_id=order["order_id"],
                issued_at=datetime.now(timezone.utc).isoformat(),
            )
        )
        assert (await _post(client, second, second_sig)).status_code == 200
        for signature in (first_sig, second_sig):
            assert signature not in (await _dump(test_session))

    dumped = await _dump(test_session)
    assert SECRET not in dumped
    assert "x-resolve-signature" not in dumped


async def _dump(test_session) -> str:
    """Every persisted character this chain could have written a secret into."""
    from app.models.db_models import AuditLogRecord

    async with test_session() as session:
        events = (await session.execute(select(IntegrationEventRecord))).scalars().all()
        orders = (await session.execute(select(ResolveOrderRecord))).scalars().all()
        audits = (await session.execute(select(AuditLogRecord))).scalars().all()
    return " ".join(
        str(value)
        for row in list(events) + list(orders) + list(audits)
        for value in getattr(row, "__dict__", {}).values()
        if isinstance(value, (str, int, float))
    )


def test_the_fixture_store_exposes_no_way_to_write():
    """
    STEP 9 case 19, by construction rather than by convention.

    These tests drive a fixture store that answers the three reads this chain makes. It has no
    mutation method and no `execute_graphql`, so a test that tried to make RESOLVE write would raise
    AttributeError instead of quietly passing.
    """
    allowed = {
        "read_order_reference",
        "read_order_line_identity",
        "read_return_targets",
        "known",
        "lines",
        "all_lines_read",
        "store_domain",
        "shipped",
        "reference_asks",
        "line_asks",
        "target_asks",
        "asks",
    }
    surface = {
        name for name in set(vars(FixtureStore)) | set(dir(FixtureStore))
        if not name.startswith("_")
    }
    assert surface - allowed == set(), sorted(surface - allowed)
    assert not [name for name in surface if "create" in name or "cancel" in name or "mutation" in name]


def test_the_spent_event_id_wording_is_pinned_so_the_contract_cannot_drift():
    """
    The recovery instruction is in the response a caller actually gets, so it is part of the
    contract and belongs pinned: if the wording changes, the caller's only guidance changed with it.
    """
    spent = order_linkage._public_detail(_refusal(503))
    assert "event id is now spent" in spent
    assert "new event" in spent
    # A 409-class refusal relays its own reason instead, and must not inherit this wording.
    moved = order_linkage._public_detail(_refusal(409, "already linked to a different RESOLVE order"))
    assert "spent" not in moved
    assert "already linked" in moved


def _refusal(status_code: int, message: str = "The merchant store could not confirm that order."):
    return ShopifyLinkageError(message, status_code=status_code)
