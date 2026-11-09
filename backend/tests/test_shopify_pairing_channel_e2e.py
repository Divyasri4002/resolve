"""
Task 14C.7G — the signed pairing channel driven END-TO-END over HTTP.

WHAT THIS PROVES, and what it deliberately does not.

There is no legitimate purchase channel in this environment. The dev store contains exactly one
order (#1001); it predates this task, it was created by a draft order inside Shopify, and RESOLVE
holds no write scope that could create another one — nor may it acquire one. So the positive
store-side pairing that STEP 5 describes is NOT available, and it is not fabricated here. See
`docs/shopify-order-linkage.md` for the read-only investigation that established this.

What CAN be proven, and is proven here without reaching the dev store or the dev database, is the
whole RESOLVE-side chain as it is actually wired, through the real HTTP routes:

    real mission → real selection of a Shopify-catalog product → real Razorpay TEST verification
    → real RESOLVE order row with canonical Shopify identity written by production code
    → merchant_order_linkage = "awaiting_linkage"
    → a signed `resolve.order_linkage.v1` event over HTTP, HMAC over the exact raw bytes
    → Shopify existence verification + Product/Variant corroboration
    → provider_order_reference persisted by the single writer
    → merchant_order_linkage = "linked", read back through the authenticated order endpoint
    → durable across a restart, idempotent on replay, refused on conflict
    → and only then consumable by the return-grounding seam, which still cannot execute a return.

The merchant store in these tests is a fixture. It answers with the GID shapes Shopify actually
uses, but it is not a purchase channel, and no linkage in this file corresponds to a real Shopify
order. That distinction is the point of STEP 2, and the report states it as
POSITIVE SHOPIFY PURCHASE CHANNEL NOT AVAILABLE. The `_no_live_shopify` guard below makes it
structurally true: any attempt to construct the real client fails the test rather than quietly
hitting the store.

Hermetic: an in-memory database, a mocked Razorpay, and a stand-in store with no mutation surface.
"""
from __future__ import annotations

import ast
import inspect
import json
import re
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

import app.tools.shopify as shopify_package
from app.config import Settings, get_settings
from app.database import _SCHEMA_UPGRADES
from app.integrations import order_linkage
from app.integrations.order_linkage import SIGNATURE_HEADER, sign_body
from app.models.db_models import AuditLogRecord, IntegrationEventRecord, ResolveOrderRecord
from app.models.mission import MissionStatus
from app.routers import integrations as integrations_module
from app.routers import missions as missions_module
from app.tools.shopify import client as client_module
from app.tools.shopify.errors import ShopifyMalformedResponseError
from app.tools.shopify.models import (
    ShopifyOrderLineIdentity,
    ShopifyOrderLineRef,
    ShopifyOrderReference,
    ShopifyOrderReturnTargets,
    ShopifyShippedFulfillmentLine,
)
from app.tools.shopify.return_provider import ShopifyReturnProvider
from app.tools.shopify.sanitizer import reset_registered_secrets
from app.tools.returns import get_return_provider
from app.tools.returns.base import ReturnProviderUnavailable, ReturnRequest
from app.tools.returns.unconnected import UnconnectedReturnProvider

from tests.test_refund_execution import (  # noqa: F401
    _login,
    recorded_events,
    restart_env,
)
from tests.test_shopify_discovery_identity import (  # noqa: F401
    _mission_with,
    _pay_and_create_order,
    _shopify_result,
    mock_razorpay,
)

# A throwaway value, generated for this process only. It is not a credential: it never leaves the
# test, is never committed, never printed, and the deployed default stays unset.
SECRET = "disposable-channel-secret-for-this-process-only"
ROUTE = "/integrations/shopify/order-linkage"
STORE = "resolve-commerce-lab.myshopify.com"
# Synthetic merchant order ids. The dev store's only real order (#1001) predates this task, was
# created by a draft order inside Shopify, and is NOT paired by anything here — reusing its id
# would make a fixture look like a claim about a real purchase. The product/variant ids below ARE
# real, because they are what production discovery persists for this store's catalog.
ORDER_GID = "gid://shopify/Order/910000000000001"
OTHER_ORDER_GID = "gid://shopify/Order/910000000000002"
PRODUCT_GID = "gid://shopify/Product/10622982422815"
VARIANT_GID = "gid://shopify/ProductVariant/52923834990879"
OTHER_PRODUCT_GID = "gid://shopify/Product/10622982717727"
OTHER_VARIANT_GID = "gid://shopify/ProductVariant/52923835973919"
LINE_GID = "gid://shopify/LineItem/18801909727519"
OTHER_LINE_GID = "gid://shopify/LineItem/18801909727520"
FULFILLMENT_LINE_GID = "gid://shopify/FulfillmentLineItem/16492059328799"


@pytest.fixture(autouse=True)
def _clean_secrets():
    reset_registered_secrets()
    yield
    reset_registered_secrets()


@pytest.fixture(autouse=True)
def _no_live_shopify(monkeypatch):
    """
    STEP 2's guarantee, made structural: nothing in this file can talk to the real store.

    The credential factories are the single path the whole app shares, so refusing them here means a
    test that ever needed a live Shopify answer fails loudly instead of quietly reading — or worse,
    writing — a merchant's real data.
    """

    def refuse(*_args, **_kwargs):
        raise AssertionError(
            "No 14C.7G test may construct the live Shopify client: the merchant store in this "
            "file is a fixture, and no real order is being paired."
        )

    monkeypatch.setattr(shopify_package, "get_shopify_client", refuse)
    # Since 14C.7O the return provider is built from the pair factory. Refusing only the read name
    # would leave a hole exactly as wide as the one this fixture exists to close.
    monkeypatch.setattr(shopify_package, "get_shopify_clients", refuse)


# --------------------------------------------------------------------------- #
# the fixture store
# --------------------------------------------------------------------------- #
class FixtureStore:
    """
    A stand-in merchant store: it answers the three reads this chain makes and nothing else.

    No `execute_graphql`, no mutation method — so a test that tried to make RESOLVE write would
    fail with AttributeError instead of quietly passing. Every question it receives is recorded,
    which is how "no unnecessary Shopify read on replay" is measured rather than asserted.
    """

    def __init__(
        self,
        *,
        known: tuple[str, ...] = (ORDER_GID, OTHER_ORDER_GID),
        lines: list[ShopifyOrderLineRef] | None = None,
        all_lines_read: bool = True,
        store_domain: str = STORE,
        shipped: list[ShopifyShippedFulfillmentLine] | None = None,
    ) -> None:
        self.known = set(known)
        self.store_domain = store_domain
        self.lines = (
            [ShopifyOrderLineRef(LINE_GID, PRODUCT_GID, VARIANT_GID)]
            if lines is None
            else list(lines)
        )
        self.all_lines_read = all_lines_read
        self.shipped = (
            [
                ShopifyShippedFulfillmentLine(
                    fulfillment_line_item_id=FULFILLMENT_LINE_GID,
                    quantity=1,
                    order_line_item_id=LINE_GID,
                    fulfillment_id="gid://shopify/Fulfillment/7166055481631",
                    fulfillment_status="SUCCESS",
                )
            ]
            if shipped is None
            else list(shipped)
        )
        self.reference_asks: list[str] = []
        self.line_asks: list[str] = []
        self.target_asks: list[str] = []

    @property
    def asks(self) -> int:
        return len(self.reference_asks) + len(self.line_asks) + len(self.target_asks)

    async def read_order_reference(self, order_id: str) -> ShopifyOrderReference:
        self.reference_asks.append(order_id)
        if order_id not in self.known:
            raise ShopifyMalformedResponseError(
                "Shopify knows nothing about that order id, so it cannot be used as a canonical "
                "reference."
            )
        return ShopifyOrderReference(order_id=order_id, name="#9101")

    async def read_order_line_identity(self, order_id: str) -> ShopifyOrderLineIdentity:
        self.line_asks.append(order_id)
        return ShopifyOrderLineIdentity(
            order_id=order_id, lines=list(self.lines), all_lines_read=self.all_lines_read
        )

    async def read_return_targets(self, order_id: str) -> ShopifyOrderReturnTargets:
        self.target_asks.append(order_id)
        return ShopifyOrderReturnTargets(
            order_id=order_id,
            return_status="NO_RETURN",
            order_lines=list(self.lines),
            shipped_lines=list(self.shipped),
        )


@pytest.fixture
def store_factory(monkeypatch):
    """Install a fixture store as the client the route would build — and prove there is no second."""

    def build(**kwargs) -> FixtureStore:
        fixture = FixtureStore(**kwargs)
        monkeypatch.setattr(order_linkage, "_client", lambda: fixture)
        return fixture

    return build


@pytest.fixture
def store(store_factory) -> FixtureStore:
    return store_factory()


@pytest.fixture
def with_secret(monkeypatch):
    """STEP 4: the HMAC secret exists for this process only, and is never printed anywhere."""
    monkeypatch.setattr(get_settings(), "linkage_integration_secret", SECRET)
    return SECRET


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _payload(**overrides) -> dict:
    base = {
        "event_id": "evt-channel-0001",
        "event_type": "resolve.order_linkage.v1",
        "shopify_order_id": ORDER_GID,
        "issued_at": datetime.now(timezone.utc).isoformat(),
        "source": "disposable-purchase-channel-fixture",
    }
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not None}


def _signed(payload: dict, secret: str = SECRET) -> tuple[bytes, str]:
    body = json.dumps(payload, sort_keys=True).encode("utf-8")
    return body, sign_body(secret, body)


async def _post(client, body: bytes, signature: str | None = None, **headers):
    hdrs = dict(headers)
    if signature is not None:
        hdrs[SIGNATURE_HEADER] = signature
    hdrs.setdefault("content-type", "application/json")
    return await client.post(ROUTE, content=body, headers=hdrs)


async def _purchased(client, install_mock_discovery, email: str) -> tuple[str, dict]:
    """
    The real flow, unshortened: discovery of a Shopify-catalog product, selection, checkout,
    Razorpay TEST verification, and RESOLVE order creation. Production code writes the identity
    columns; this helper never touches the database directly.
    """
    mission_id = await _mission_with(client, install_mock_discovery, [_shopify_result()], email)
    selected = await client.post(
        f"/missions/{mission_id}/shopping/select-product", json={"product_id": PRODUCT_GID}
    )
    assert selected.status_code == 200, selected.text
    return mission_id, await _pay_and_create_order(client, mission_id)


async def _linkage_events(test_session) -> list[IntegrationEventRecord]:
    async with test_session() as session:
        return list(
            (
                await session.scalars(
                    select(IntegrationEventRecord).order_by(IntegrationEventRecord.event_id)
                )
            ).all()
        )


async def _linkage_audits(test_session) -> list[AuditLogRecord]:
    async with test_session() as session:
        return list(
            (
                await session.scalars(
                    select(AuditLogRecord).where(
                        AuditLogRecord.action == order_linkage.AUDIT_ACTION
                    )
                )
            ).all()
        )


def _line(node_id: str, product_id, variant_id) -> ShopifyOrderLineRef:
    return ShopifyOrderLineRef(node_id, product_id, variant_id)


# --------------------------------------------------------------------------- #
# STEP 6 + 7 + 11 — the positive path, and the state the client is allowed to see
# --------------------------------------------------------------------------- #
async def test_a_signed_event_pairs_a_shopify_native_order_row_end_to_end(
    restart_env, install_mock_discovery, with_secret, store, mock_razorpay
):
    make_client, test_session = restart_env
    async with make_client() as client:
        mission_id, order = await _purchased(client, install_mock_discovery, "channel@shop.test")
        order_id = order["order_id"]

        # STEP 11 before: payment is verified, identity is canonical, and nothing is claimed about
        # the merchant order.
        assert order["merchant_order_linked"] is False
        assert order["merchant_order_linkage"] == "awaiting_linkage"

        body, signature = _signed(_payload(resolve_order_id=order_id))
        response = await _post(client, body, signature)
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["outcome"] == "linked"
        assert payload["resolve_order_id"] == order_id
        assert payload["provider_order_reference"] == ORDER_GID
        assert payload["already_processed"] is False

        # STEP 11: the state change is observable through the normal authenticated read.
        after = (await client.get(f"/missions/{mission_id}/order")).json()
        assert after["merchant_order_linked"] is True
        assert after["merchant_order_linkage"] == "linked"
        # The browser is told the state and nothing else: no merchant identifier crosses the line,
        # so there is nothing for it to hand back.
        assert ORDER_GID not in json.dumps(after)
        assert "provider_order_reference" not in after
        assert "has not asserted shipping, delivery, or a return" in after["fulfillment_note"]

    async with test_session() as session:
        row = await session.get(ResolveOrderRecord, order_id)
        assert row.provider_order_reference == ORDER_GID
        # Corroboration narrows a pairing; it never rewrites RESOLVE's own identity.
        assert row.shopify_product_gid == PRODUCT_GID
        assert row.shopify_variant_gid == VARIANT_GID
        assert row.shopify_store_domain == STORE
        assert row.status == "order_created"  # no status value invented by this task
    # One question about existence, one about lines. Not a search for an order that fits.
    assert store.reference_asks == [ORDER_GID]
    assert store.line_asks == [ORDER_GID]
    events = await _linkage_events(test_session)
    assert [(e.outcome, e.provider_order_reference) for e in events] == [("linked", ORDER_GID)]
    assert [a.decision for a in await _linkage_audits(test_session)] == ["observed"]


async def test_only_one_row_carries_the_merchant_reference_and_the_invariant_holds(
    restart_env, install_mock_discovery, with_secret, store, mock_razorpay
):
    make_client, test_session = restart_env
    async with make_client() as client:
        _, order = await _purchased(client, install_mock_discovery, "unique@shop.test")
        body, signature = _signed(_payload(resolve_order_id=order["order_id"]))
        assert (await _post(client, body, signature)).status_code == 200

    async with test_session() as session:
        assert await session.scalar(
            select(func.count())
            .select_from(ResolveOrderRecord)
            .where(ResolveOrderRecord.provider_order_reference.is_not(None))
        ) == 1
        # The unique index is the storage-level answer to "can two orders share one merchant order".
        names = {index.name for index in ResolveOrderRecord.__table__.indexes}
        assert "uq_resolve_orders_provider_order_reference" in names


# --------------------------------------------------------------------------- #
# STEP 8 — durability across a restart, and the settled-replay contract
# --------------------------------------------------------------------------- #
async def test_the_pairing_survives_a_restart_and_a_settled_replay_asks_shopify_nothing(
    restart_env, install_mock_discovery, with_secret, store, mock_razorpay
):
    make_client, test_session = restart_env
    async with make_client() as client:
        mission_id, order = await _purchased(client, install_mock_discovery, "restart@shop.test")
        body, signature = _signed(_payload(resolve_order_id=order["order_id"]))
        assert (await _post(client, body, signature)).status_code == 200
        asks_after_pairing = store.asks

    # A new client with a freshly issued session is a new request path over the same storage —
    # which is what a restart leaves behind: the row is the only memory.
    async with make_client() as client_b:
        await _login(client_b, "restart@shop.test")
        fetched = (await client_b.get(f"/missions/{mission_id}/order")).json()
        assert fetched["order_id"] == order["order_id"]
        assert fetched["merchant_order_linkage"] == "linked"
        assert fetched["merchant_order_linked"] is True

        replay = await _post(client_b, body, signature)
        assert replay.status_code == 200, replay.text
        settled = replay.json()
        assert settled["already_processed"] is True
        assert settled["outcome"] == "linked"

        again = (await client_b.get(f"/missions/{mission_id}/order")).json()
        assert again["merchant_order_linkage"] == "linked"

    # A settled event is answered from storage: the merchant store is not re-asked, so a second
    # corroboration read is not even possible.
    assert store.asks == asks_after_pairing
    events = await _linkage_events(test_session)
    assert len(events) == 1
    assert events[0].outcome == "linked"


# --------------------------------------------------------------------------- #
# STEP 9 — the replay matrix
# --------------------------------------------------------------------------- #
async def test_case_a_the_same_event_with_the_same_bytes_is_harmless(
    restart_env, install_mock_discovery, with_secret, store, mock_razorpay
):
    make_client, test_session = restart_env
    async with make_client() as client:
        _, order = await _purchased(client, install_mock_discovery, "replaya@shop.test")
        body, signature = _signed(_payload(resolve_order_id=order["order_id"]))
        first = await _post(client, body, signature)
        second = await _post(client, body, signature)

    assert first.json()["outcome"] == "linked"
    assert second.status_code == 200
    assert second.json()["already_processed"] is True
    assert second.json()["resolve_order_id"] == order["order_id"]
    assert store.asks == 2  # the second delivery asked Shopify nothing
    events = await _linkage_events(test_session)
    assert len(events) == 1


@pytest.mark.parametrize(
    "changed", ["shopify_order_id", "resolve_order_id"], ids=["new-merchant-order", "new-resolve-order"]
)
async def test_cases_b_c_a_reused_event_id_carrying_a_different_payload_is_a_conflict(
    restart_env, install_mock_discovery, with_secret, store, changed, mock_razorpay
):
    make_client, test_session = restart_env
    async with make_client() as client:
        mission_id, order = await _purchased(client, install_mock_discovery, "reuse@shop.test")
        body, signature = _signed(_payload(resolve_order_id=order["order_id"]))
        assert (await _post(client, body, signature)).status_code == 200

        overrides = {"resolve_order_id": order["order_id"]}
        if changed == "shopify_order_id":
            overrides["shopify_order_id"] = OTHER_ORDER_GID
        else:
            overrides["resolve_order_id"] = "order_someone_elses"
        conflicting, signature_b = _signed(_payload(**overrides))
        response = await _post(client, conflicting, signature_b)

        assert response.status_code == 409, response.text
        assert response.json() == {
            "detail": "That event_id was already recorded for a different payload."
        }
        after = (await client.get(f"/missions/{mission_id}/order")).json()
        assert after["merchant_order_linkage"] == "linked"
    # The conflict is detected on the stored digest: the merchant store was asked twice and no more.
    assert store.asks == 2
    events = await _linkage_events(test_session)
    assert [(e.outcome, e.event_id) for e in events] == [("linked", "evt-channel-0001")]


@pytest.mark.parametrize(
    "signature", [None, "", "deadbeef", "c" * 64], ids=["missing", "empty", "malformed", "wrong"]
)
async def test_cases_d_e_an_event_that_is_not_signed_by_this_secret_stores_nothing(
    restart_env, install_mock_discovery, with_secret, store, signature, mock_razorpay
):
    make_client, test_session = restart_env
    async with make_client() as client:
        mission_id, order = await _purchased(client, install_mock_discovery, "unsigned@shop.test")
        body, _real = _signed(_payload(resolve_order_id=order["order_id"]))
        response = await _post(client, body, signature)
        assert response.status_code == 401, response.text
        # Missing, empty, malformed and wrong all answer the same way: nothing says which.
        assert response.json() == {"detail": "The event signature is not valid."}
        after = (await client.get(f"/missions/{mission_id}/order")).json()
        assert after["merchant_order_linkage"] == "awaiting_linkage"
    assert store.asks == 0
    assert await _linkage_events(test_session) == []


@pytest.mark.parametrize("label", ["stale", "future"])
async def test_cases_f_g_an_event_outside_the_freshness_window_is_refused_before_it_is_claimed(
    restart_env, install_mock_discovery, with_secret, store, label
):
    make_client, test_session = restart_env
    window = get_settings().linkage_event_max_age_seconds
    moment = datetime.now(timezone.utc) + (
        timedelta(seconds=-(window + 600)) if label == "stale" else timedelta(seconds=window + 600)
    )
    body, signature = _signed(
        _payload(
            event_id=f"evt-channel-{label}",
            resolve_order_id="order_not_relevant_here",
            issued_at=moment.isoformat(),
        )
    )
    async with make_client() as client:
        response = await _post(client, body, signature)
        assert response.status_code == 400, response.text
        assert response.json() == {
            "detail": "issued_at is outside the accepted window, so this event was not processed."
        }
    # Refused before the claim: the event id stays unspent, and no linkage row was touched.
    assert await _linkage_events(test_session) == []
    assert store.asks == 0


# --------------------------------------------------------------------------- #
# STEP 10 — negative corroboration, each case through the real route
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "case, store_kwargs, expected_status, expected_fragment",
    [
        (
            "variant_not_on_the_order",
            {"lines": [_line(LINE_GID, PRODUCT_GID, OTHER_VARIANT_GID)]},
            409,
            "not the variant RESOLVE sold",
        ),
        (
            "product_not_on_the_order",
            {"lines": [_line(LINE_GID, OTHER_PRODUCT_GID, OTHER_VARIANT_GID)]},
            409,
            "does not sell the product",
        ),
        (
            "variant_under_another_product",
            {"lines": [_line(LINE_GID, OTHER_PRODUCT_GID, VARIANT_GID)]},
            409,
            "different product than the one",
        ),
        ("no_lines_at_all", {"lines": []}, 409, "does not sell the product"),
        (
            "variant_on_two_lines",
            {
                "lines": [
                    _line(LINE_GID, PRODUCT_GID, VARIANT_GID),
                    _line(OTHER_LINE_GID, PRODUCT_GID, VARIANT_GID),
                ]
            },
            409,
            "2 lines of that order",
        ),
        ("lines_not_fully_returned", {"all_lines_read": False}, 409, "more lines than RESOLVE read"),
        (
            "foreign_store",
            {"store_domain": "someone-elses-store.myshopify.com"},
            409,
            "different store",
        ),
        (
            "unknown_shopify_order",
            {"known": (OTHER_ORDER_GID,)},
            503,
            "could not confirm that order",
        ),
    ],
    ids=[
        "variant-not-on-order",
        "product-not-on-order",
        "variant-under-other-product",
        "no-lines",
        "variant-on-two-lines",
        "has-next-page",
        "foreign-store-domain",
        "unknown-order",
    ],
)
async def test_every_unclean_corroboration_refuses_and_leaves_the_row_unchanged(
    restart_env, install_mock_discovery, with_secret, store_factory, case, store_kwargs,
    expected_status, expected_fragment, mock_razorpay
):
    fixture = store_factory(**store_kwargs)
    make_client, test_session = restart_env
    async with make_client() as client:
        mission_id, order = await _purchased(client, install_mock_discovery, f"neg-{case}@shop.test")
        body, signature = _signed(_payload(resolve_order_id=order["order_id"]))
        response = await _post(client, body, signature)

        assert response.status_code == expected_status, response.text
        assert response.json()["outcome"] == "rejected"
        assert expected_fragment in response.json()["detail"]

        after = (await client.get(f"/missions/{mission_id}/order")).json()
        assert after["merchant_order_linked"] is False
        assert after["merchant_order_linkage"] == "awaiting_linkage"

    async with test_session() as session:
        row = await session.get(ResolveOrderRecord, order["order_id"])
        assert row.provider_order_reference is None
        assert row.shopify_product_gid == PRODUCT_GID  # a refusal never rewrites identity
        events = (await session.scalars(select(IntegrationEventRecord))).all()
        assert [e.outcome for e in events] == ["rejected"]
    refused = await _linkage_audits(test_session)
    assert [a.decision for a in refused] == ["refused"]
    # A refusal never writes: the fixture has no mutation surface at all, and the real client is
    # unreachable from this file.
    assert not hasattr(fixture, "execute_graphql")
    assert not hasattr(fixture, "execute_mutation")
    assert not hasattr(fixture, "request_return")


async def test_a_second_resolve_order_cannot_share_the_same_merchant_order(
    restart_env, install_mock_discovery, with_secret, store, mock_razorpay
):
    make_client, _ = restart_env
    async with make_client() as client_a:
        mission_a, order_a = await _purchased(client_a, install_mock_discovery, "share1@shop.test")
        body, signature = _signed(_payload(resolve_order_id=order_a["order_id"]))
        assert (await _post(client_a, body, signature)).status_code == 200

        async with make_client() as client_b:
            mission_b, order_b = await _purchased(
                client_b, install_mock_discovery, "share2@shop.test"
            )
            competing, signature_b = _signed(
                _payload(event_id="evt-channel-competing", resolve_order_id=order_b["order_id"])
            )
            response = await _post(client_b, competing, signature_b)
            assert response.status_code == 409, response.text
            assert "different RESOLVE order" in response.json()["detail"]

            assert (
                await client_b.get(f"/missions/{mission_b}/order")
            ).json()["merchant_order_linkage"] == "awaiting_linkage"

        assert (
            await client_a.get(f"/missions/{mission_a}/order")
        ).json()["merchant_order_linkage"] == "linked"


async def test_an_established_linkage_is_not_re_pointed_by_a_later_event(
    restart_env, install_mock_discovery, with_secret, store, mock_razorpay
):
    """
    The tenth negative of STEP 10: a row that is already paired stays paired to the same order.

    A second, differently-numbered event naming a different Shopify order would silently re-aim
    every future return at another purchase, so the writer refuses it outright.
    """
    make_client, test_session = restart_env
    async with make_client() as client:
        mission_id, order = await _purchased(client, install_mock_discovery, "repoint@shop.test")
        body, signature = _signed(_payload(resolve_order_id=order["order_id"]))
        assert (await _post(client, body, signature)).status_code == 200

        moved, signature_b = _signed(
            _payload(event_id="evt-channel-repoint", shopify_order_id=OTHER_ORDER_GID,
                     resolve_order_id=order["order_id"])
        )
        response = await _post(client, moved, signature_b)
        assert response.status_code == 409, response.text
        assert "already linked to a Shopify order" in response.json()["detail"]
        assert (
            await client.get(f"/missions/{mission_id}/order")
        ).json()["merchant_order_linkage"] == "linked"

    async with test_session() as session:
        row = await session.get(ResolveOrderRecord, order["order_id"])
        assert row.provider_order_reference == ORDER_GID
    # The refusal is made from RESOLVE's own row: the new GID was never asked about.
    assert store.reference_asks == [ORDER_GID]


async def test_a_partial_canonical_identity_is_refused_on_resolves_own_data(
    restart_env, install_mock_discovery, with_secret, store, mock_razorpay
):
    """
    The mirror columns are written together by one server-side selection, so a row carrying a
    product and no variant is RESOLVE contradicting itself. It is refused without asking the
    merchant store a single question about lines.
    """
    make_client, test_session = restart_env
    async with make_client() as client:
        _, order = await _purchased(client, install_mock_discovery, "partial@shop.test")
        async with test_session() as session:
            row = await session.get(ResolveOrderRecord, order["order_id"])
            row.shopify_variant_gid = None  # test-only corruption of a fixture row
            await session.commit()

        body, signature = _signed(_payload(resolve_order_id=order["order_id"]))
        response = await _post(client, body, signature)
        assert response.status_code == 409, response.text
        assert "only part of a Shopify product identity" in response.json()["detail"]
    assert store.line_asks == []


async def test_an_event_naming_nothing_resolve_owns_is_refused_and_still_recorded(
    restart_env, with_secret, store
):
    """
    A valid signature over a well-formed event is not a licence to invent a row: the RESOLVE order
    must exist, and the refusal is remembered rather than dropped.
    """
    make_client, test_session = restart_env
    async with make_client() as client:
        body, signature = _signed(_payload(resolve_order_id="order_never_created"))
        response = await _post(client, body, signature)
        assert response.status_code == 404, response.text
        assert response.json()["outcome"] == "rejected"
    assert store.asks == 0
    events = await _linkage_events(test_session)
    assert [e.outcome for e in events] == ["rejected"]


# --------------------------------------------------------------------------- #
# STEP 12 — readiness only: a paired row can be aimed, and still cannot execute
# --------------------------------------------------------------------------- #
async def test_a_paired_order_is_consumable_by_the_return_seam_and_still_cannot_execute(
    restart_env, install_mock_discovery, with_secret, store, mock_razorpay
):
    make_client, test_session = restart_env
    async with make_client() as client:
        _, order = await _purchased(client, install_mock_discovery, "ready@shop.test")
        body, signature = _signed(_payload(resolve_order_id=order["order_id"]))
        assert (await _post(client, body, signature)).status_code == 200

    async with test_session() as session:
        row = await session.get(ResolveOrderRecord, order["order_id"])

    # The persisted pairing is exactly what the grounding seam was waiting for…
    target = await ShopifyReturnProvider(client=store).resolve_return_target(
        ReturnRequest(
            mission_id=row.mission_id,
            issue_id="issue_fixture",
            order_id=row.order_id,
            return_id="return_fixture",
            product_id=row.product_id,
            title=row.title,
            merchant=row.merchant,
            issue_type="damaged",
            provider_order_reference=row.provider_order_reference,
            provider_product_reference=row.shopify_product_gid,
            provider_variant_reference=row.shopify_variant_gid,
        )
    )
    assert target.provider_order_reference == ORDER_GID
    assert target.provider_line_reference == FULFILLMENT_LINE_GID
    assert target.line_quantity == 1  # units shipped, never remainingQuantity

    # …and the deployment still refuses to run a return at all.
    provider = get_return_provider()
    assert isinstance(provider, UnconnectedReturnProvider)
    with pytest.raises(ReturnProviderUnavailable):
        await provider.request_return(
            ReturnRequest(
                mission_id=row.mission_id,
                issue_id="issue_fixture",
                order_id=row.order_id,
                return_id="return_fixture",
                product_id=row.product_id,
                title=row.title,
                merchant=row.merchant,
            )
        )
    assert store.target_asks == [ORDER_GID]
    async with test_session() as session:
        # No ReturnRecord, no second order row, nothing executed.
        assert await session.scalar(select(func.count()).select_from(ResolveOrderRecord)) == 1


async def test_the_committed_deployment_keeps_the_return_provider_disconnected():
    assert Settings.model_fields["return_provider"].default == "none"
    assert get_settings().return_provider == "none"
    assert isinstance(get_return_provider(), UnconnectedReturnProvider)


# --------------------------------------------------------------------------- #
# STEP 4 + 11 + 21 — nothing pushed, nothing leaked
# --------------------------------------------------------------------------- #
async def test_no_linkage_is_pushed_and_no_new_realtime_event_name_exists(
    restart_env, install_mock_discovery, with_secret, store, recorded_events, mock_razorpay
):
    """
    STEP 11 explicitly: no WebSocket event was added. The state is read, and reading it is the
    authority — a push would be a second, unfalsifiable copy of a fact the column already decides.
    """
    from app.tools.shopify import linkage as linkage_module

    for module in (order_linkage, linkage_module):
        source = inspect.getsource(module)
        assert "broadcast_mission_event" not in source
        assert "websocket" not in source.lower()
        assert "WebSocket" not in source

    make_client, _ = restart_env
    async with make_client() as client:
        _, order = await _purchased(client, install_mock_discovery, "nopush@shop.test")
        body, signature = _signed(_payload(resolve_order_id=order["order_id"]))
        assert (await _post(client, body, signature)).status_code == 200

    # M21 Phase 2A: frames are canonical conversation-row envelopes — the lifecycle
    # discriminator is payload["state"] (all lifecycle rows share event_type).
    states = {event.get("payload", {}).get("state") for event in recorded_events}
    assert "order_created" in states  # the pre-existing lifecycle event is untouched
    refused = [name for name in states if re.search(r"link|merchant|shopify", str(name), re.I)]
    assert refused == []


async def test_the_secret_the_signature_and_the_raw_body_are_stored_nowhere(
    restart_env, install_mock_discovery, with_secret, store, mock_razorpay
):
    make_client, test_session = restart_env
    async with make_client() as client:
        _, order = await _purchased(client, install_mock_discovery, "leak@shop.test")
        body, signature = _signed(_payload(resolve_order_id=order["order_id"]))
        response = await _post(client, body, signature)
        assert response.status_code == 200
        echoed = response.json()

    events = await _linkage_events(test_session)
    audits = await _linkage_audits(test_session)
    stored = json.dumps(
        [
            {
                column.name: getattr(event, column.name)
                for column in IntegrationEventRecord.__table__.columns
            }
            for event in events
        ]
        + [[a.actor, a.action, a.reason, a.decision] for a in audits]
        + [echoed],
        default=str,
    )
    assert SECRET not in stored
    assert signature not in stored
    assert body.decode("utf-8") not in stored
    # What is kept about the body is a digest, and nothing else.
    assert re.fullmatch(r"[0-9a-f]{64}", events[0].body_sha256)
    assert events[0].body_sha256 != signature
    assert any("evt-channel-0001" in (a.reason or "") for a in audits)


def test_the_committed_configuration_leaves_the_channel_disabled():
    """
    STEP 4's other half: the repository's defaults must not have been changed to make this task
    demonstrable. An unconfigured deployment refuses with 503 rather than trusting anything.
    """
    assert Settings.model_fields["linkage_integration_secret"].default == ""


async def test_the_channel_reaches_the_writer_only_through_the_route(
    restart_env, install_mock_discovery, with_secret, store, mock_razorpay
):
    make_client, test_session = restart_env
    async with make_client() as client:
        mission_id, order = await _purchased(client, install_mock_discovery, "seam@shop.test")
        # A logged-in shopper's own session buys nothing: the route authenticates the sender, not
        # the user, and an unsigned body from an authenticated browser is still refused.
        body, _ = _signed(_payload(resolve_order_id=order["order_id"]))
        unsigned = await client.post(
            ROUTE, content=body, headers={"content-type": "application/json"}
        )
        assert unsigned.status_code == 401
        assert (
            await client.get(f"/missions/{mission_id}/order")
        ).json()["merchant_order_linkage"] == "awaiting_linkage"

        signed = await _post(client, body, sign_body(SECRET, body))
        assert signed.status_code == 200
        assert signed.json()["outcome"] == "linked"

    async with test_session() as session:
        row = await session.get(ResolveOrderRecord, order["order_id"])
        assert row.provider_order_reference == ORDER_GID


def test_the_route_takes_no_body_model_and_no_user_identity():
    params = inspect.signature(integrations_module.submit_order_linkage).parameters
    assert set(params) == {"request", "x_resolve_signature", "db"}
    source = inspect.getsource(integrations_module.submit_order_linkage)
    for forbidden in ("current_user", "provider_order_reference =", "link_order_to_shopify_order"):
        assert forbidden not in source, forbidden


# --------------------------------------------------------------------------- #
# the boundary as a whole: no new surface, no schema, no mutations
# --------------------------------------------------------------------------- #
def test_the_pairing_channel_needed_no_schema_and_no_new_status():
    assert {name for name, _ddl in _SCHEMA_UPGRADES["resolve_orders"]} == {
        "provider_order_reference",
        "shopify_product_gid",
        "shopify_variant_gid",
        "shopify_store_domain",
    }
    values = {member.value for member in MissionStatus}
    assert not [value for value in values if "merchant" in value or "link" in value], values
    # The client-facing state is derived from the column, never stored beside it.
    source = inspect.getsource(missions_module._merchant_order_linkage)
    assert "provider_order_reference" in source
    assert "await " not in source


def test_no_shopify_mutation_document_exists_in_the_read_only_client():
    """
    STEP 13's static half: the client the linkage path stands behind can only ask questions. The
    single mutation in the repository is 14C.4's `returnCreate`, reachable only from the return
    provider — which this deployment does not run.
    """
    tree = ast.parse(inspect.getsource(client_module))
    literals = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    assert not [text for text in literals if text.strip().startswith("mutation ")]
    for forbidden in ("orderCreate", "draftOrderCreate", "draftOrderComplete", "orderUpdate"):
        assert forbidden not in json.dumps(literals)
    # The reads this channel makes, and nothing else.
    documents = [text for text in literals if text.strip().startswith("query Resolve")]
    assert any("ResolveReadOrderReference" in text for text in documents)
    assert any("ResolveReadOrderLineIdentity" in text for text in documents)


def test_the_event_contract_still_names_two_references_and_nothing_else():
    from app.integrations.order_linkage import OrderLinkageEvent

    assert set(OrderLinkageEvent.model_fields) == {
        "event_id",
        "event_type",
        "shopify_order_id",
        "issued_at",
        "resolve_order_id",
        "mission_id",
        "source",
    }
    assert OrderLinkageEvent.model_config.get("extra") == "forbid"
