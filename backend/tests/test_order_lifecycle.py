"""
Tests for Task 08: post-payment RESOLVE order lifecycle foundation (order_created -> order_confirmed).

Covers the 11 mandated scenarios:
- TEST 1:  payment_verified -> RESOLVE order creation
- TEST 2:  canonical amount comes from persisted checkout/payment state
- TEST 3:  canonical product identity (product_id, title, merchant)
- TEST 4:  mission owner can create the order
- TEST 5:  unauthorized user cannot create or read the order (404, nothing leaked)
- TEST 6:  non-payment-verified mission cannot create an order
- TEST 7:  duplicate/idempotent creation — one order row per mission, reused forever
- TEST 8:  order persists across a simulated backend restart (fresh client + fresh DB session)
- TEST 9:  client-field tampering (amount/currency/product/Razorpay ids) is ignored
- TEST 10: lifecycle state machine — order_created -> order_confirmed, idempotent confirm
- TEST 11: WebSocket events (order_created / order_confirmed) are broadcast

Hermetic: REASONING_PROVIDER=mock / PRODUCT_SEARCH_PROVIDER=mock via conftest, Razorpay TEST
order API replaced by a deterministic mock — NO real payment API calls are made.
"""
import hashlib
import hmac
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.agents.conversation import as_event_frame
from app.database import Base, get_db
from app.main import app
from app.models.db_models import ResolveOrderRecord
from app.routers import missions as missions_module

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

SHAMPOO_PROMPT = "I want to order shampoo under ₹600 for anti-hairfall."
PASSWORD = "SecurePassword123!"


@pytest.fixture
async def async_client():
    test_engine = create_async_engine(TEST_DATABASE_URL, echo=False)
    test_session = async_sessionmaker(test_engine, expire_on_commit=False, class_=AsyncSession)

    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async def override_get_db():
        async with test_session() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    app.dependency_overrides.clear()
    await test_engine.dispose()


@pytest.fixture
async def restart_env():
    """
    Same as async_client but exposes a client factory + sessionmaker so a test can
    simulate a backend restart: a brand-new HTTP client and a brand-new DB session
    against the same persisted database.
    """
    test_engine = create_async_engine(TEST_DATABASE_URL, echo=False)
    test_session = async_sessionmaker(test_engine, expire_on_commit=False, class_=AsyncSession)

    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async def override_get_db():
        async with test_session() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db

    def make_client() -> AsyncClient:
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    yield make_client, test_session

    app.dependency_overrides.clear()
    await test_engine.dispose()


@pytest.fixture
def mock_razorpay(monkeypatch):
    """Deterministic stand-in for the Razorpay TEST order API (records all calls)."""
    calls: list[dict] = []
    counter = {"n": 0}

    def fake_create_test_order(amount_paise, currency, receipt, notes):
        calls.append(
            {
                "amount_paise": amount_paise,
                "currency": currency,
                "receipt": receipt,
                "notes": notes,
            }
        )
        counter["n"] += 1
        return {
            "id": f"order_TEST{counter['n']:04d}",
            "amount": amount_paise,
            "currency": currency,
            "status": "created",
            "receipt": receipt,
        }

    monkeypatch.setattr(missions_module, "create_test_order", fake_create_test_order)
    return calls


@pytest.fixture
def recorded_events(monkeypatch):
    """Capture every canonical event frame broadcast during a test (M21 Phase 2A).

    The stream carries exactly the committed conversation row's envelope, so frames are
    filtered on event_type/payload (e.g. payload["state"]) rather than an ad-hoc "event" key.
    """
    events: list[dict] = []

    async def fake_broadcast(row) -> None:
        events.append(as_event_frame(row))

    monkeypatch.setattr(missions_module, "broadcast_event_row", fake_broadcast)
    return events


def _checkout_signature(order_id: str, payment_id: str) -> str:
    """Valid checkout signature for the configured (backend-only) test key secret."""
    secret = get_settings().razorpay_key_secret.encode()
    return hmac.new(
        secret, f"{order_id}|{payment_id}".encode(), hashlib.sha256
    ).hexdigest()


async def _register(client: AsyncClient, name: str, email: str) -> None:
    reg = await client.post(
        "/auth/register",
        json={"name": name, "email": email, "password": PASSWORD},
    )
    assert reg.status_code == 201


async def _login(client: AsyncClient, email: str) -> None:
    login = await client.post(
        "/auth/login",
        json={"email": email, "password": PASSWORD},
    )
    assert login.status_code == 200


async def _confirmed_mission(
    client: AsyncClient, install_mock_discovery, sample_products, product_id: str = "item_dove"
) -> str:
    """Fresh user + discovered mission + selection + checkout confirmation -> mission id."""
    install_mock_discovery(sample_results=sample_products)
    await _register(client, "Olga Owner", "olga@shop.com")

    resp = await client.post("/missions/shopping", json={"prompt": SHAMPOO_PROMPT})
    assert resp.status_code == 200
    mission_id = resp.json()["mission_id"]

    sel = await client.post(
        f"/missions/{mission_id}/shopping/select-product", json={"product_id": product_id}
    )
    assert sel.status_code == 200

    conf = await client.post(f"/missions/{mission_id}/shopping/checkout/confirm")
    assert conf.status_code == 200
    return mission_id


async def _verified_mission(
    client: AsyncClient,
    install_mock_discovery,
    sample_products,
    mock_razorpay,
    product_id: str = "item_dove",
) -> str:
    """Confirmed mission + paid (mock) Razorpay order + valid signature -> payment_verified."""
    mission_id = await _confirmed_mission(
        client, install_mock_discovery, sample_products, product_id
    )
    order_resp = await client.post(
        f"/missions/{mission_id}/shopping/payment/create-order", json={}
    )
    assert order_resp.status_code == 200
    razorpay_order_id = order_resp.json()["payment"]["razorpay_order_id"]
    payment_id = "pay_TESTORDER1"
    verify = await client.post(
        f"/missions/{mission_id}/shopping/payment/verify",
        json={
            "razorpay_order_id": razorpay_order_id,
            "razorpay_payment_id": payment_id,
            "razorpay_signature": _checkout_signature(razorpay_order_id, payment_id),
        },
    )
    assert verify.status_code == 200
    assert verify.json()["mission_status"] == "payment_verified"
    return mission_id


async def _create_resolve_order(client: AsyncClient, mission_id: str, payload=None):
    return await client.post(
        f"/missions/{mission_id}/order",
        json=payload if payload is not None else {},
    )


# --------------------------------------------------------------------------- #
# TEST 1: payment_verified -> order creation
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_order_created_from_payment_verified(
    async_client: AsyncClient, install_mock_discovery, sample_products, mock_razorpay
):
    mission_id = await _verified_mission(
        async_client, install_mock_discovery, sample_products, mock_razorpay
    )
    provider_calls_after_payment = len(mock_razorpay)

    resp = await _create_resolve_order(async_client, mission_id)
    assert resp.status_code == 200
    data = resp.json()

    assert data["order_id"].startswith("order_")
    assert data["mission_id"] == mission_id
    assert data["status"] == "order_created"
    assert data["test_mode"] is True
    assert "merchant order" in data["fulfillment_note"]

    # Mission lifecycle advanced
    reopened = (await async_client.get(f"/missions/{mission_id}")).json()
    assert reopened["status"] == "order_created"
    assert reopened["shopping_intent"]["state"] == "order_created"

    # No payment-provider re-contact: order creation reads persisted state only
    assert len(mock_razorpay) == provider_calls_after_payment


# --------------------------------------------------------------------------- #
# TEST 2: canonical amount from persisted checkout/payment state
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_order_amount_is_canonical(
    async_client: AsyncClient, install_mock_discovery, sample_products, mock_razorpay
):
    mission_id = await _verified_mission(
        async_client, install_mock_discovery, sample_products, mock_razorpay
    )
    data = (await _create_resolve_order(async_client, mission_id)).json()

    # item_dove price ₹399 — equal to the persisted checkout subtotal / verified payment
    assert Decimal(data["amount"]) == Decimal("399")
    assert data["currency"] == "INR"


# --------------------------------------------------------------------------- #
# TEST 3: canonical product identity
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_order_product_is_canonical(
    async_client: AsyncClient, install_mock_discovery, sample_products, mock_razorpay
):
    mission_id = await _verified_mission(
        async_client, install_mock_discovery, sample_products, mock_razorpay
    )
    data = (await _create_resolve_order(async_client, mission_id)).json()

    assert data["product"]["product_id"] == "item_dove"
    assert data["product"]["title"] == "Dove Hair Fall Rescue Shampoo"
    assert data["product"]["merchant"] == "Nykaa"

    # Razorpay references match the verified payment exactly
    payment = (await async_client.get(f"/missions/{mission_id}")).json()["shopping_intent"][
        "payment"
    ]
    assert data["razorpay_order_id"] == payment["razorpay_order_id"]
    assert data["razorpay_payment_id"] == payment["razorpay_payment_id"]


# --------------------------------------------------------------------------- #
# TEST 4: mission owner can create the order
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_owner_can_create_order(
    async_client: AsyncClient, install_mock_discovery, sample_products, mock_razorpay
):
    mission_id = await _verified_mission(
        async_client, install_mock_discovery, sample_products, mock_razorpay
    )
    resp = await _create_resolve_order(async_client, mission_id)
    assert resp.status_code == 200

    get_resp = await async_client.get(f"/missions/{mission_id}/order")
    assert get_resp.status_code == 200
    assert get_resp.json()["order_id"] == resp.json()["order_id"]


# --------------------------------------------------------------------------- #
# TEST 5: unauthorized user gets 404 and learns nothing
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_unauthorized_user_cannot_create_or_read_order(
    async_client: AsyncClient, install_mock_discovery, sample_products, mock_razorpay
):
    mission_id = await _verified_mission(
        async_client, install_mock_discovery, sample_products, mock_razorpay
    )

    # A different authenticated user
    await _register(async_client, "Mallory", "mallory@shop.com")
    create_resp = await _create_resolve_order(async_client, mission_id)
    assert create_resp.status_code == 404
    get_resp = await async_client.get(f"/missions/{mission_id}/order")
    assert get_resp.status_code == 404

    # Anonymous client
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as anon_client:
        anon_create = await anon_client.post(f"/missions/{mission_id}/order", json={})
        assert anon_create.status_code == 404
        anon_get = await anon_client.get(f"/missions/{mission_id}/order")
        assert anon_get.status_code == 404

    # Owner still creates successfully; nothing was written by the attackers
    await _login(async_client, "olga@shop.com")
    owner_resp = await _create_resolve_order(async_client, mission_id)
    assert owner_resp.status_code == 200


# --------------------------------------------------------------------------- #
# TEST 6: non-payment-verified mission cannot create an order
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_non_verified_mission_rejected(
    async_client: AsyncClient, install_mock_discovery, sample_products, mock_razorpay
):
    # checkout_confirmed — payment not even started
    mission_id = await _confirmed_mission(
        async_client, install_mock_discovery, sample_products
    )
    resp = await _create_resolve_order(async_client, mission_id)
    assert resp.status_code == 400
    assert "payment" in resp.json()["detail"].lower()

    # payment_failed — a failed test payment must never produce an order
    order_resp = await async_client.post(
        f"/missions/{mission_id}/shopping/payment/create-order", json={}
    )
    razorpay_order_id = order_resp.json()["payment"]["razorpay_order_id"]
    await async_client.post(
        f"/missions/{mission_id}/shopping/payment/verify",
        json={
            "razorpay_order_id": razorpay_order_id,
            "razorpay_payment_id": "pay_TESTBAD",
            "razorpay_signature": "0" * 64,
        },
    )
    failed_resp = await _create_resolve_order(async_client, mission_id)
    assert failed_resp.status_code == 400
    assert "payment" in failed_resp.json()["detail"].lower()

    # Non-shopping mission also rejected
    await _register(async_client, "Rex Recovery", "rex@shop.com")
    rec = await async_client.post(
        "/missions",
        json={
            "mission_type": "recovery",
            "objective": {"description": "Recover damaged package", "success_criteria": []},
            "authority": {"allowed_actions": ["request_refund"], "recovery_budget": "5000"},
            "transaction": {
                "merchant_name": "Store",
                "order_id": "ord_1",
                "amount_paid": "12000",
            },
        },
    )
    rec_resp = await _create_resolve_order(async_client, rec.json()["mission_id"])
    assert rec_resp.status_code == 400


# --------------------------------------------------------------------------- #
# TEST 7: duplicate / idempotent creation — exactly one order per mission
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_duplicate_creation_is_idempotent(
    async_client: AsyncClient, install_mock_discovery, sample_products, mock_razorpay
):
    mission_id = await _verified_mission(
        async_client, install_mock_discovery, sample_products, mock_razorpay
    )

    first = await _create_resolve_order(async_client, mission_id)
    second = await _create_resolve_order(async_client, mission_id)
    third = await _create_resolve_order(async_client, mission_id)

    assert first.status_code == second.status_code == third.status_code == 200
    assert first.json()["order_id"] == second.json()["order_id"] == third.json()["order_id"]

    # After confirmation, a repeated create still returns the same single order
    await async_client.post(f"/missions/{mission_id}/order/confirm", json={})
    fourth = await _create_resolve_order(async_client, mission_id)
    assert fourth.status_code == 200
    assert fourth.json()["order_id"] == first.json()["order_id"]
    assert fourth.json()["status"] == "order_confirmed"


# --------------------------------------------------------------------------- #
# TEST 8: order persists across a simulated backend restart
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_order_persists_across_restart(
    restart_env, install_mock_discovery, sample_products, mock_razorpay
):
    make_client, test_session = restart_env

    async with make_client() as client_a:
        mission_id = await _verified_mission(
            client_a, install_mock_discovery, sample_products, mock_razorpay
        )
        created = (await _create_resolve_order(client_a, mission_id)).json()
        order_id = created["order_id"]

        # "Restart": brand-new HTTP client + brand-new DB session, same database
        async with make_client() as client_b:
            await _login(client_b, "olga@shop.com")
            reopened = (await client_b.get(f"/missions/{mission_id}/order")).json()
            assert reopened["order_id"] == order_id
            assert reopened["status"] == "order_created"
            assert Decimal(reopened["amount"]) == Decimal("399")
            assert reopened["product"]["title"] == "Dove Hair Fall Rescue Shampoo"

        # Durable row exists independent of any app-level cache: exactly one
        async with test_session() as session:
            count = await session.scalar(
                select(func.count()).select_from(ResolveOrderRecord)
            )
            assert count == 1
            row = (
                await session.execute(
                    select(ResolveOrderRecord).where(
                        ResolveOrderRecord.mission_id == mission_id
                    )
                )
            ).scalar_one()
            assert row.order_id == order_id
            assert row.status == "order_created"
            assert row.user_id is not None and row.user_id.startswith("user_")


# --------------------------------------------------------------------------- #
# TEST 9: client-field tampering is ignored
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_client_field_tampering_ignored(
    async_client: AsyncClient, install_mock_discovery, sample_products, mock_razorpay
):
    mission_id = await _verified_mission(
        async_client, install_mock_discovery, sample_products, mock_razorpay
    )
    payment = (await async_client.get(f"/missions/{mission_id}")).json()["shopping_intent"][
        "payment"
    ]

    resp = await _create_resolve_order(
        async_client,
        mission_id,
        payload={
            "amount": "1.00",
            "currency": "USD",
            "product": {"product_id": "item_premium", "title": "Premium", "merchant": "Ebay"},
            "razorpay_order_id": "order_EVIL",
            "razorpay_payment_id": "pay_EVIL",
            "user_id": "user_attacker",
            "status": "order_confirmed",
        },
    )
    assert resp.status_code == 200
    data = resp.json()

    # Everything canonical — nothing from the tampered body survived
    assert Decimal(data["amount"]) == Decimal("399")
    assert data["currency"] == "INR"
    assert data["product"]["product_id"] == "item_dove"
    assert data["product"]["merchant"] == "Nykaa"
    assert data["razorpay_order_id"] == payment["razorpay_order_id"]
    assert data["razorpay_payment_id"] == payment["razorpay_payment_id"]
    assert data["status"] == "order_created"


# --------------------------------------------------------------------------- #
# TEST 10: lifecycle state machine order_created -> order_confirmed
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_lifecycle_order_created_to_confirmed(
    async_client: AsyncClient, install_mock_discovery, sample_products, mock_razorpay
):
    mission_id = await _verified_mission(
        async_client, install_mock_discovery, sample_products, mock_razorpay
    )

    # Confirm before create is rejected
    early = await async_client.post(f"/missions/{mission_id}/order/confirm", json={})
    assert early.status_code == 400

    created = (await _create_resolve_order(async_client, mission_id)).json()
    assert created["status"] == "order_created"
    mission = (await async_client.get(f"/missions/{mission_id}")).json()
    assert mission["status"] == "order_created"
    assert mission["shopping_intent"]["state"] == "order_created"

    confirmed = (
        await async_client.post(f"/missions/{mission_id}/order/confirm", json={})
    ).json()
    assert confirmed["order_id"] == created["order_id"]
    assert confirmed["status"] == "order_confirmed"
    mission = (await async_client.get(f"/missions/{mission_id}")).json()
    assert mission["status"] == "order_confirmed"
    assert mission["shopping_intent"]["state"] == "order_confirmed"

    # Repeated confirm is idempotent — same order, same state, no error
    again = (
        await async_client.post(f"/missions/{mission_id}/order/confirm", json={})
    ).json()
    assert again["order_id"] == created["order_id"]
    assert again["status"] == "order_confirmed"


# --------------------------------------------------------------------------- #
# TEST 11: WebSocket events are broadcast for both transitions
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_order_events_broadcast(
    async_client: AsyncClient,
    install_mock_discovery,
    sample_products,
    mock_razorpay,
    recorded_events,
):
    mission_id = await _verified_mission(
        async_client, install_mock_discovery, sample_products, mock_razorpay
    )

    created = (await _create_resolve_order(async_client, mission_id)).json()
    await async_client.post(f"/missions/{mission_id}/order/confirm", json={})

    # M21 Phase 2A: the stream carries the committed conversation row's envelope. Both
    # transitions share event_type="system_state_change"; the state discriminates in payload.
    order_events = [
        e for e in recorded_events
        if e.get("event_type") == "system_state_change"
        and str(e.get("payload", {}).get("state", "")).startswith("order_")
    ]
    assert [e["payload"]["state"] for e in order_events] == ["order_created", "order_confirmed"]
    for event in order_events:
        assert event["kind"] == "event"
        assert event["mission_id"] == mission_id
        assert event["payload"]["order_id"] == created["order_id"]


# --------------------------------------------------------------------------- #
# Extra: no order record exists before creation (GET 404)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_get_order_404_before_creation(
    async_client: AsyncClient, install_mock_discovery, sample_products, mock_razorpay
):
    mission_id = await _verified_mission(
        async_client, install_mock_discovery, sample_products, mock_razorpay
    )
    resp = await async_client.get(f"/missions/{mission_id}/order")
    assert resp.status_code == 404
