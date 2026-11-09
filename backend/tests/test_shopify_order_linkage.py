"""
Tests for Task 14C.5: server-side linkage between a RESOLVE order and one Shopify order.

The linkage is the answer to a question RESOLVE must never guess at: which merchant order does
this RESOLVE order correspond to? A return can only be filed against a merchant order, so if this
value were inferred (by title, amount, timestamp, merchant name) or accepted from a client, a
return could be aimed at somebody else's purchase. Two properties hold this shut:

  * the reference is written by ONE server-side operation, and Shopify itself has to name that
    order before the value is stored;
  * no HTTP surface carries it — a request body cannot set, change, or clear it.

Hermetic: the Shopify side is a stub client standing where the real read-only client stands, and
the DB is in-memory SQLite. No network call is made anywhere in this file; every stub records that
it was or was not asked.
"""
from __future__ import annotations

import inspect
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.db_models import ResolveOrderRecord
from app.routers import missions as missions_module
from app.tools.shopify.errors import (
    ShopifyError,
    ShopifyMalformedResponseError,
    ShopifyServerError,
)
from app.tools.shopify.linkage import (
    ShopifyLinkageError,
    canonical_shopify_order_reference,
    link_order_to_shopify_order,
)
from app.tools.shopify.models import ShopifyOrderReference
from app.tools.shopify.sanitizer import redact, reset_registered_secrets

from tests.test_refund_execution import (  # noqa: F401
    _verified_mission,
    mock_razorpay,
    restart_env,
)

OWNER = "user_owner"
INTRUDER = "user_intruder"
ORDER_GID = "gid://shopify/Order/7990366601503"
OTHER_GID = "gid://shopify/Order/7990366601599"


@pytest.fixture(autouse=True)
def _clean_secrets():
    reset_registered_secrets()
    yield
    reset_registered_secrets()


class StubShopifyReader:
    """Stands where the real read-only client stands: records every question asked of Shopify."""

    def __init__(self, *, known=(ORDER_GID, OTHER_GID), error: Exception | None = None):
        self.known = set(known)
        self.error = error
        self.asks: list[str] = []

    async def read_order_reference(self, order_id: str) -> ShopifyOrderReference:
        self.asks.append(order_id)
        if self.error is not None:
            raise self.error
        if order_id not in self.known:
            raise ShopifyMalformedResponseError(
                "Shopify knows nothing about that order id, so it cannot be used as a canonical "
                "reference."
            )
        return ShopifyOrderReference(order_id=order_id, name="#1001")

    @property
    def calls(self) -> int:
        return len(self.asks)


async def _order(test_session, *, mission_id, user_id=OWNER, reference=None) -> str:
    """Insert a canonical confirmed RESOLVE order; return its order_id."""
    async with test_session() as session:
        row = ResolveOrderRecord(
            mission_id=mission_id,
            user_id=user_id,
            product_id="item_dove",
            title="Dove Hair Fall Rescue Shampoo",
            merchant="Nykaa",
            product={"product_id": "item_dove", "title": "Dove Hair Fall Rescue Shampoo"},
            amount=Decimal("399.00"),
            currency="INR",
            razorpay_order_id="order_TEST1",
            razorpay_payment_id="pay_TEST1",
            provider_order_reference=reference,
            status="order_confirmed",
        )
        session.add(row)
        await session.commit()
        return row.order_id


async def _reference(test_session, mission_id) -> str | None:
    async with test_session() as session:
        row = await session.scalar(
            select(ResolveOrderRecord).where(ResolveOrderRecord.mission_id == mission_id)
        )
        return row.provider_order_reference


async def _link(
    test_session,
    *,
    mission_id="mission_1",
    actor=OWNER,
    reference=ORDER_GID,
    client=None,
):
    async with test_session() as session:
        return await link_order_to_shopify_order(
            session,
            mission_id=mission_id,
            actor_user_id=actor,
            shopify_order_id=reference,
            client=client if client is not None else StubShopifyReader(),
        )


# --------------------------------------------------------------------------- #
# TEST 1: a valid server-side linkage is stored, and only after Shopify confirms the order
# --------------------------------------------------------------------------- #
async def test_linkage_is_stored_after_shopify_confirms_the_order(restart_env):
    _make_client, test_session = restart_env
    await _order(test_session, mission_id="mission_1")
    reader = StubShopifyReader()

    row = await _link(test_session, client=reader)

    assert row.provider_order_reference == ORDER_GID
    assert await _reference(test_session, "mission_1") == ORDER_GID
    # Exactly one question was asked of Shopify, and it was asked about that exact order.
    assert reader.asks == [ORDER_GID]


async def test_linkage_is_refused_and_nothing_stored_when_shopify_denies_the_order(restart_env):
    _make_client, test_session = restart_env
    await _order(test_session, mission_id="mission_1")

    with pytest.raises(ShopifyLinkageError) as exc:
        await _link(test_session, client=StubShopifyReader(known=("gid://shopify/Order/1",)))

    assert exc.value.status_code == 503
    assert await _reference(test_session, "mission_1") is None


async def test_linkage_is_refused_when_shopify_cannot_be_asked(restart_env):
    """An unreachable store is a refusal, never a licence to record an unverified pointer."""
    _make_client, test_session = restart_env
    await _order(test_session, mission_id="mission_1")

    for error in (
        ShopifyServerError("server error 500"),
        ShopifyError("credentials rejected"),
        RuntimeError("unexpected"),
    ):
        with pytest.raises(ShopifyLinkageError) as exc:
            await _link(test_session, client=StubShopifyReader(error=error))
        assert exc.value.status_code == 503
        assert await _reference(test_session, "mission_1") is None


# --------------------------------------------------------------------------- #
# TEST 2: a missing linkage is a safe refusal, in both directions
# --------------------------------------------------------------------------- #
async def test_missing_reference_is_refused_without_touching_shopify(restart_env):
    _make_client, test_session = restart_env
    await _order(test_session, mission_id="mission_1")
    reader = StubShopifyReader()

    for value in (None, "", "   "):
        with pytest.raises(ShopifyLinkageError):
            await _link(test_session, reference=value, client=reader)  # type: ignore[arg-type]

    assert reader.calls == 0
    assert await _reference(test_session, "mission_1") is None


async def test_linkage_of_an_order_that_does_not_exist_is_refused(restart_env):
    _make_client, test_session = restart_env
    reader = StubShopifyReader()

    with pytest.raises(ShopifyLinkageError) as exc:
        await _link(test_session, mission_id="mission_missing", client=reader)

    assert exc.value.status_code == 404
    assert reader.calls == 0


# --------------------------------------------------------------------------- #
# TEST 3: a malformed reference is refused before it can become a record
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "value",
    [
        "ord_canonical_1",                      # somebody else's id scheme
        "gid://shopify/Order/",                 # prefix with no id
        "gid://shopify/Order/7990366601503/x",  # trailing path
        "gid://shopify/Order/79903666015O3",    # letter O inside a numeric id
        "gid://shopify/Product/10622982422815",  # right shape, wrong object
        "gid://shopify/FulfillmentLineItem/16492059328799",
        "https://example.myshopify.com/admin/orders/7990366601503",
        7990366601503,                           # not a string at all
    ],
)
def test_a_malformed_shopify_order_reference_is_refused(value):
    with pytest.raises(ShopifyLinkageError):
        canonical_shopify_order_reference(value)


async def test_a_malformed_reference_never_reaches_storage_or_shopify(restart_env):
    _make_client, test_session = restart_env
    await _order(test_session, mission_id="mission_1")
    reader = StubShopifyReader()

    with pytest.raises(ShopifyLinkageError) as exc:
        await _link(test_session, reference="ord_canonical_1", client=reader)

    assert exc.value.status_code == 400
    assert reader.calls == 0
    assert await _reference(test_session, "mission_1") is None


def test_a_canonical_reference_passes_unchanged():
    assert canonical_shopify_order_reference(ORDER_GID) == ORDER_GID
    # Surrounding whitespace is not part of an id; nothing else is repaired.
    assert canonical_shopify_order_reference(f"  {ORDER_GID}\n") == ORDER_GID


# --------------------------------------------------------------------------- #
# TEST 4: ownership — another user's mission cannot be linked by them
# --------------------------------------------------------------------------- #
async def test_another_users_order_cannot_be_linked_by_them(restart_env):
    _make_client, test_session = restart_env
    await _order(test_session, mission_id="mission_1", user_id=OWNER)
    reader = StubShopifyReader()

    with pytest.raises(ShopifyLinkageError) as exc:
        await _link(test_session, actor=INTRUDER, client=reader)

    # The same answer as "no such order": nothing about someone else's mission is revealed.
    assert exc.value.status_code == 404
    assert redact("does not belong") in exc.value.message
    assert reader.calls == 0
    assert await _reference(test_session, "mission_1") is None


# --------------------------------------------------------------------------- #
# TEST 5: the client cannot set, change, or clear a linkage over HTTP
# --------------------------------------------------------------------------- #
def test_no_http_surface_carries_a_shopify_order_reference():
    """
    Structural proof that the linkage has no request path.

    The order-creating endpoint must never assign the column, the router module must not import
    the linkage operation at all, and no endpoint anywhere in the app may take a parameter that
    could carry a merchant order id.
    """
    from fastapi.routing import APIRoute

    from app.main import app

    source = inspect.getsource(missions_module)
    assert "provider_order_reference" in source  # it READS the persisted value...
    assert "order.provider_order_reference =" not in source  # ...and never writes it.
    assert "link_order_to_shopify_order" not in source
    assert "ShopifyLinkageError" not in source

    routes = [r for r in app.routes if isinstance(r, APIRoute)]
    assert routes, "the app exposes no routes to check"
    for route in routes:
        parameters = set(inspect.signature(route.endpoint).parameters)
        for name in parameters:
            assert "shopify" not in name.lower(), (route.path, name)
            assert "provider_order" not in name.lower(), (route.path, name)


async def test_creating_an_order_through_http_ignores_a_tampered_linkage(
    restart_env, install_mock_discovery, sample_products, mock_razorpay
):
    """
    Behavioural half of the same rule: a body that names a Shopify order changes nothing, on the
    create path and on every later repeat of it.
    """
    make_client, test_session = restart_env
    client = make_client()
    async with client:
        mission_id = await _verified_mission(
            client, install_mock_discovery, sample_products, mock_razorpay
        )
        created = await client.post(
            f"/missions/{mission_id}/order",
            json={"provider_order_reference": ORDER_GID, "amount": "0.01"},
        )
        assert created.status_code == 200
        assert await _reference(test_session, mission_id) is None

        # Repeating the tampered write after the row exists changes it no further.
        again = await client.post(
            f"/missions/{mission_id}/order", json={"provider_order_reference": OTHER_GID}
        )
        assert again.status_code == 200
        assert await _reference(test_session, mission_id) is None


# --------------------------------------------------------------------------- #
# TEST 6: duplicates — the same linkage is a no-op, a different one is refused
# --------------------------------------------------------------------------- #
async def test_repeating_the_same_linkage_is_idempotent_and_asks_shopify_nothing(restart_env):
    _make_client, test_session = restart_env
    await _order(test_session, mission_id="mission_1", reference=ORDER_GID)
    reader = StubShopifyReader()

    row = await _link(test_session, client=reader)

    assert row.provider_order_reference == ORDER_GID
    assert reader.calls == 0  # an established linkage is not re-verified on a repeat


async def test_a_second_different_linkage_is_refused_and_the_first_stands(restart_env):
    _make_client, test_session = restart_env
    await _order(test_session, mission_id="mission_1", reference=ORDER_GID)
    reader = StubShopifyReader()

    with pytest.raises(ShopifyLinkageError) as exc:
        await _link(test_session, reference=OTHER_GID, client=reader)

    assert exc.value.status_code == 409
    assert "already linked" in exc.value.message
    assert reader.calls == 0
    assert await _reference(test_session, "mission_1") == ORDER_GID


async def test_one_shopify_order_cannot_back_two_resolve_orders(restart_env):
    _make_client, test_session = restart_env
    await _order(test_session, mission_id="mission_1", reference=ORDER_GID)
    await _order(test_session, mission_id="mission_2")
    reader = StubShopifyReader()

    with pytest.raises(ShopifyLinkageError) as exc:
        await _link(test_session, mission_id="mission_2", client=reader)

    assert exc.value.status_code == 409
    assert await _reference(test_session, "mission_2") is None


async def test_the_unique_index_enforces_one_to_one_even_if_the_read_is_bypassed(restart_env):
    """
    The invariant lives in storage, not only in this module's code: writing the same Shopify
    reference to two rows fails at the database, whatever path attempted it.
    """
    from sqlalchemy.exc import IntegrityError

    _make_client, test_session = restart_env
    await _order(test_session, mission_id="mission_1", reference=ORDER_GID)
    async with test_session() as session:
        session.add(
            ResolveOrderRecord(
                mission_id="mission_2",
                user_id=OWNER,
                title="Other",
                product={},
                amount=Decimal("1.00"),
                currency="INR",
                razorpay_order_id="order_TEST2",
                razorpay_payment_id="pay_TEST2",
                provider_order_reference=ORDER_GID,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


# --------------------------------------------------------------------------- #
# TEST 7: the linkage is durable, and it is what the return path reads
# --------------------------------------------------------------------------- #
async def test_the_linkage_survives_a_restart(restart_env):
    make_client, test_session = restart_env
    await _order(test_session, mission_id="mission_1")
    await _link(test_session)

    # A brand-new client and a brand-new session over the same engine — the process coming back up.
    async with make_client():
        assert await _reference(test_session, "mission_1") == ORDER_GID
        # ...and the restart does not make the linkage re-writable or re-verifiable.
        reader = StubShopifyReader()
        row = await _link(test_session, client=reader)
        assert row.provider_order_reference == ORDER_GID
        assert reader.calls == 0


def test_the_return_request_reads_the_linkage_from_the_persisted_order():
    """
    The return flow may only aim a merchant mutation at a persisted linkage, so the seam that
    builds the provider request must read it off the order row and nothing else.
    """
    source = inspect.getsource(missions_module._return_request)
    assert "order.provider_order_reference" in source
    assert "payload" not in source
    assert "body" not in source
    # The request-builder takes the persisted order, never a client-supplied merchant id.
    parameters = list(inspect.signature(missions_module._return_request).parameters)
    assert "order" in parameters and not any("shopify" in p.lower() for p in parameters)
