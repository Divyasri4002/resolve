"""
Machine-to-machine service reads for the scratch purchase runner's read-only preflight.

The machine identity is a dedicated header credential (empty configured value DISABLES the
path) granted exactly two reads: a store-associated mission's canonical order and checkout
selection. Everything else stays closed, association is derived server-side from the
configured store domain (never from the caller), every refusal is 404-masked, and the
service path is strictly read-only — it must not advance anyone's checkout.
"""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base, get_db
from app.main import app
from app.models.db_models import MissionRecord, ResolveOrderRecord

SERVICE_TOKEN = "machine-credential-for-hermetic-tests"
HEADER_NAME = "X-Resolve-Service-Token"
TEST_STORE = "service-auth-test.myshopify.com"
MISSION_ID = "mission_service0001"
ORDER_ID = "order_0123456789ab"
RZP_ORDER_ID = "order_testmachine01"
RZP_PAYMENT_ID = "pay_testmachine01"


def _intent(state: str = "checkout_review", store: str | None = TEST_STORE) -> dict:
    selected = {
        "product_id": "prod_service",
        "title": "Service Test Item",
        "price": "799.00",
        "extracted_price": 799.0,
        "currency": "INR",
        "merchant": "RESOLVE Merchant Test",
        "source": "shopify",
        "shopify_product_gid": "gid://shopify/Product/1111",
        "shopify_variant_gid": "gid://shopify/ProductVariant/2222",
        "requires_variant_clarification": False,
        "variant_clarification": None,
    }
    if store is not None:
        selected["shopify_store_domain"] = store
    return {"state": state, "selected_product": selected}


@pytest.fixture
async def service_env(monkeypatch):
    """Hermetic app + session factory, with the service credential and store domain pinned."""
    monkeypatch.setattr("app.auth.dependencies.settings.service_runner_token", SERVICE_TOKEN)
    monkeypatch.setattr("app.auth.dependencies.settings.shopify_store_domain", TEST_STORE)

    test_engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    test_session = async_sessionmaker(test_engine, expire_on_commit=False, class_=AsyncSession)
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async def override_get_db():
        async with test_session() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield SimpleNamespace(client=client, session=test_session)

    app.dependency_overrides.clear()
    await test_engine.dispose()


async def _seed_mission(
    session,
    *,
    user_id: str,
    mission_id: str = MISSION_ID,
    store: str | None = TEST_STORE,
    intent_state: str = "checkout_review",
    mission_status: str = "checkout_review",
    with_order: bool = True,
    order_store: str | None = TEST_STORE,
    order_id: str = ORDER_ID,
) -> None:
    intent = _intent(state=intent_state, store=store)
    session.add(
        MissionRecord(
            mission_id=mission_id,
            user_id=user_id,
            mission_type="shopping",
            status=mission_status,
            mandate={"allowed_actions": [], "recovery_budget": "0"},
            transaction={"merchant_name": "RESOLVE Merchant Test"},
            shopping_intent=intent,
        )
    )
    if with_order:
        identified = order_store is not None
        session.add(
            ResolveOrderRecord(
                order_id=order_id,
                mission_id=mission_id,
                user_id=user_id,
                product_id="prod_service",
                title="Service Test Item",
                merchant="RESOLVE Merchant Test",
                product=copy.deepcopy(intent["selected_product"]),
                amount=799.00,
                currency="INR",
                razorpay_order_id=RZP_ORDER_ID,
                razorpay_payment_id=RZP_PAYMENT_ID,
                shopify_product_gid=(
                    "gid://shopify/Product/1111" if identified else None
                ),
                shopify_variant_gid=(
                    "gid://shopify/ProductVariant/2222" if identified else None
                ),
                shopify_store_domain=order_store,
                status="order_created",
            )
        )
    await session.commit()


# --------------------------------------------------------------------------- #
# Permitted read-only access
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_valid_credential_reads_both_canonical_views(service_env):
    async with service_env.session() as session:
        await _seed_mission(session, user_id="user_owner")
    client = service_env.client
    headers = {HEADER_NAME: SERVICE_TOKEN}

    order_resp = await client.get(f"/missions/{MISSION_ID}/order", headers=headers)
    assert order_resp.status_code == 200
    order = order_resp.json()
    assert order["order_id"] == ORDER_ID
    assert order["mission_id"] == MISSION_ID
    assert order["status"] == "order_created"
    assert order["razorpay_order_id"] == RZP_ORDER_ID
    assert order["razorpay_payment_id"] == RZP_PAYMENT_ID
    assert order["currency"] == "INR"
    assert order["merchant_order_linked"] is False

    checkout_resp = await client.get(f"/missions/{MISSION_ID}/shopping/checkout", headers=headers)
    assert checkout_resp.status_code == 200
    body = checkout_resp.json()
    selection = body["shopping_intent"]["selected_product"]
    assert selection["shopify_product_gid"] == "gid://shopify/Product/1111"
    assert selection["shopify_variant_gid"] == "gid://shopify/ProductVariant/2222"
    assert selection["shopify_store_domain"] == TEST_STORE
    assert body["mission_status"] == "checkout_review"


@pytest.mark.asyncio
async def test_the_machine_reads_by_store_association_not_by_user(service_env):
    """The machine serves the configured store: ownership by a registered user neither helps
    nor blocks the two reads — association is the server-side rule, not a user id."""
    async with service_env.session() as session:
        await _seed_mission(session, user_id="user_whomever")
    client = service_env.client
    headers = {HEADER_NAME: SERVICE_TOKEN}
    assert (await client.get(f"/missions/{MISSION_ID}/order", headers=headers)).status_code == 200
    assert (
        await client.get(f"/missions/{MISSION_ID}/shopping/checkout", headers=headers)
    ).status_code == 200


# --------------------------------------------------------------------------- #
# Missing / invalid credentials
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_missing_credential_reads_nothing(service_env):
    async with service_env.session() as session:
        await _seed_mission(session, user_id="user_owner")
    client = service_env.client
    assert (await client.get(f"/missions/{MISSION_ID}/order")).status_code == 404
    assert (await client.get(f"/missions/{MISSION_ID}/shopping/checkout")).status_code == 404


@pytest.mark.asyncio
async def test_an_invalid_credential_reads_nothing(service_env):
    async with service_env.session() as session:
        await _seed_mission(session, user_id="user_owner")
    client = service_env.client
    wrong = {HEADER_NAME: "not-the-credential"}
    assert (await client.get(f"/missions/{MISSION_ID}/order", headers=wrong)).status_code == 404
    assert (
        await client.get(f"/missions/{MISSION_ID}/shopping/checkout", headers=wrong)
    ).status_code == 404


@pytest.mark.asyncio
async def test_an_unconfigured_token_disables_the_service_path_entirely(service_env, monkeypatch):
    monkeypatch.setattr("app.auth.dependencies.settings.service_runner_token", "")
    async with service_env.session() as session:
        await _seed_mission(session, user_id="user_owner")
    client = service_env.client
    headers = {HEADER_NAME: SERVICE_TOKEN}
    assert (await client.get(f"/missions/{MISSION_ID}/order", headers=headers)).status_code == 404
    assert (
        await client.get(f"/missions/{MISSION_ID}/shopping/checkout", headers=headers)
    ).status_code == 404


# --------------------------------------------------------------------------- #
# Server-side association
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_selection_from_another_store_is_not_served(service_env):
    async with service_env.session() as session:
        await _seed_mission(
            session, user_id="user_owner", store="other-store.myshopify.com"
        )
    client = service_env.client
    headers = {HEADER_NAME: SERVICE_TOKEN}
    assert (
        await client.get(f"/missions/{MISSION_ID}/shopping/checkout", headers=headers)
    ).status_code == 404


@pytest.mark.asyncio
async def test_an_order_without_this_stores_identity_is_not_served(service_env):
    async with service_env.session() as session:
        await _seed_mission(session, user_id="user_owner", order_store=None)
    client = service_env.client
    headers = {HEADER_NAME: SERVICE_TOKEN}
    assert (await client.get(f"/missions/{MISSION_ID}/order", headers=headers)).status_code == 404


@pytest.mark.asyncio
async def test_an_unknown_mission_is_the_same_404_as_a_refused_one(service_env):
    client = service_env.client
    headers = {HEADER_NAME: SERVICE_TOKEN}
    assert (await client.get("/missions/mission_nosuch0000/order", headers=headers)).status_code == 404
    assert (
        await client.get("/missions/mission_nosuch0000/shopping/checkout", headers=headers)
    ).status_code == 404


@pytest.mark.asyncio
async def test_each_mission_is_served_only_its_own_order(service_env):
    """The order is loaded BY the mission id in the URL: mission B's URL can never yield
    mission A's order row, so the runner's cross-check has a real association to verify."""
    async with service_env.session() as session:
        await _seed_mission(
            session, user_id="user_owner", mission_id="mission_aaaaaaaaaa", order_id=ORDER_ID
        )
        await _seed_mission(
            session,
            user_id="user_owner",
            mission_id="mission_bbbbbbbbbb",
            order_id="order_bbbbbbbbbbbb",
        )
    client = service_env.client
    headers = {HEADER_NAME: SERVICE_TOKEN}
    resp = await client.get("/missions/mission_bbbbbbbbbb/order", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["order_id"] == "order_bbbbbbbbbbbb"
    assert resp.json()["mission_id"] == "mission_bbbbbbbbbb"


# --------------------------------------------------------------------------- #
# Strictly read-only: no state advance, no mutation capability
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_service_read_does_not_advance_the_checkout_state(service_env):
    """The user path's GET persists product_selected -> checkout_review and broadcasts; the
    machine path must return the payload and change nothing."""
    async with service_env.session() as session:
        await _seed_mission(
            session,
            user_id="user_owner",
            intent_state="product_selected",
            mission_status="product_selected",
        )
    client = service_env.client
    headers = {HEADER_NAME: SERVICE_TOKEN}
    resp = await client.get(f"/missions/{MISSION_ID}/shopping/checkout", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["mission_status"] == "product_selected"
    async with service_env.session() as session:
        record = await session.get(MissionRecord, MISSION_ID)
        assert record.status == "product_selected"
        assert record.shopping_intent["state"] == "product_selected"


@pytest.mark.asyncio
async def test_the_service_credential_is_not_a_session_and_mutates_nothing(service_env):
    async with service_env.session() as session:
        await _seed_mission(session, user_id="user_owner")
    client = service_env.client
    headers = {HEADER_NAME: SERVICE_TOKEN}

    # Not a user identity: mission detail, listing and the profile endpoint stay closed.
    assert (await client.get(f"/missions/{MISSION_ID}", headers=headers)).status_code == 404
    assert (await client.get("/missions", headers=headers)).status_code == 401
    assert (await client.get("/auth/me", headers=headers)).status_code == 401

    # No mutation surface: the agent loop and the order-creation/payment boundary refuse the
    # same way they refuse any anonymous caller.
    agent = await client.post(
        f"/missions/{MISSION_ID}/agent", json={"user_input": "hello"}, headers=headers
    )
    assert agent.status_code == 404
    create_order = await client.post(f"/missions/{MISSION_ID}/order", json={}, headers=headers)
    assert create_order.status_code == 404
    confirm = await client.post(
        f"/missions/{MISSION_ID}/shopping/checkout/confirm", json={}, headers=headers
    )
    assert confirm.status_code == 404
