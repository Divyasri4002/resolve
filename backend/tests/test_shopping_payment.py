"""
Tests for Task 07: Razorpay TEST-MODE payment (order creation + server-side verification).

Covers the 15 mandated scenarios:
- TEST 1:  Create a Razorpay test order from checkout_confirmed.
- TEST 2:  Amount is server-derived from the persisted checkout confirmation.
- TEST 3:  INR -> paise conversion is exact (₹549 -> 54900).
- TEST 4:  Client-supplied amount/currency manipulation is ignored.
- TEST 5:  Unauthorized user cannot create a payment order (404, owner untouched).
- TEST 6:  Non-shopping mission cannot create a payment order.
- TEST 7:  Payment before checkout confirmation is rejected.
- TEST 8:  Duplicate order protection — pending order reused, one provider call.
- TEST 9:  Valid checkout signature -> payment_verified.
- TEST 10: Invalid signature -> payment_failed (never payment_verified).
- TEST 11: Arbitrary order id / bare "success" flag cannot bypass verification.
- TEST 12: payment_verified persists across reload.
- TEST 13: payment_failed persists across reload.
- TEST 14: Razorpay secret is never returned by any payment endpoint.
- TEST 15: Existing recovery payment behavior (webhook signature) remains intact.
Plus: verified payment refuses a new order; failed payment may start one fresh attempt.

Hermetic: REASONING_PROVIDER=mock / PRODUCT_SEARCH_PROVIDER=mock via conftest, and the
Razorpay order API is replaced by a deterministic mock — the automated suite makes NO real
payment API calls.
"""
import ast
import hashlib
import hmac
import json

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.database import Base, get_db
from app.main import app
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
def mock_razorpay(monkeypatch):
    """
    Deterministic stand-in for the Razorpay TEST order API. Every call is recorded so tests
    can assert exactly how many orders the backend attempted to create and with what amount.
    """
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


async def _confirmed_mission(
    client: AsyncClient, install_mock_discovery, sample_products, product_id: str = "item_dove"
) -> str:
    """Fresh user + discovered mission + selection + checkout confirmation -> mission id."""
    install_mock_discovery(sample_results=sample_products)
    await _register(client, "Payton Payer", "payton@shop.com")

    resp = await client.post("/missions/shopping", json={"prompt": SHAMPOO_PROMPT})
    assert resp.status_code == 200
    mission_id = resp.json()["mission_id"]

    sel = await client.post(
        f"/missions/{mission_id}/shopping/select-product", json={"product_id": product_id}
    )
    assert sel.status_code == 200

    conf = await client.post(f"/missions/{mission_id}/shopping/checkout/confirm")
    assert conf.status_code == 200
    assert conf.json()["mission_status"] == "checkout_confirmed"
    return mission_id


async def _create_order(client: AsyncClient, mission_id: str, payload: dict | None = None):
    return await client.post(
        f"/missions/{mission_id}/shopping/payment/create-order",
        json=payload if payload is not None else {},
    )


# --------------------------------------------------------------------------- #
# TEST 1: order creation from checkout_confirmed
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_create_order_from_checkout_confirmed(
    async_client: AsyncClient, install_mock_discovery, sample_products, mock_razorpay
):
    mission_id = await _confirmed_mission(
        async_client, install_mock_discovery, sample_products
    )

    resp = await _create_order(async_client, mission_id)
    assert resp.status_code == 200
    data = resp.json()

    assert data["mission_status"] == "payment_pending"
    payment = data["payment"]
    assert payment["razorpay_order_id"].startswith("order_TEST")
    assert payment["status"] == "pending"
    assert payment["amount"] == "399.0"
    assert payment["amount_paise"] == 39900
    assert payment["currency"] == "INR"
    assert payment["provider"] == "razorpay_test_mode"
    assert payment["test_mode"] is True
    assert data["total_recovery_spend"] == "0"

    # Persisted on the intent
    intent = data["shopping_intent"]
    assert intent["state"] == "payment_pending"
    assert intent["payment"]["razorpay_order_id"] == payment["razorpay_order_id"]
    assert intent["selected_product"]["product_id"] == "item_dove"  # selection untouched

    # Exactly one provider call with the server-derived amount
    assert len(mock_razorpay) == 1
    assert mock_razorpay[0]["amount_paise"] == 39900
    assert mock_razorpay[0]["currency"] == "INR"
    assert mock_razorpay[0]["notes"]["mission_id"] == mission_id


# --------------------------------------------------------------------------- #
# TEST 2: amount is server-derived (client sends nothing authoritative)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_order_amount_is_server_derived(
    async_client: AsyncClient, install_mock_discovery, sample_products, mock_razorpay
):
    mission_id = await _confirmed_mission(
        async_client, install_mock_discovery, sample_products
    )
    resp = await _create_order(async_client, mission_id)
    payment = resp.json()["payment"]
    # Matches the checkout confirmation subtotal (item_dove = ₹399), never client input
    assert payment["amount"] == "399.0"
    assert payment["amount_paise"] == 39900


# --------------------------------------------------------------------------- #
# TEST 3: INR -> paise conversion is exact
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_inr_paise_conversion_exact(
    async_client: AsyncClient, install_mock_discovery, sample_products, mock_razorpay
):
    mission_id = await _confirmed_mission(
        async_client, install_mock_discovery, sample_products, product_id="item_loreal"
    )
    resp = await _create_order(async_client, mission_id)
    payment = resp.json()["payment"]
    assert payment["amount"] == "549.0"      # ₹549
    assert payment["amount_paise"] == 54900  # -> 54900 paise exactly
    assert mock_razorpay[0]["amount_paise"] == 54900


# --------------------------------------------------------------------------- #
# TEST 4: client amount/currency manipulation ignored
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_client_amount_manipulation_ignored(
    async_client: AsyncClient, install_mock_discovery, sample_products, mock_razorpay
):
    mission_id = await _confirmed_mission(
        async_client, install_mock_discovery, sample_products
    )
    resp = await _create_order(
        async_client,
        mission_id,
        payload={"amount": 1, "amount_paise": 1, "currency": "USD", "razorpay_order_id": "order_HACK"},
    )
    assert resp.status_code == 200
    payment = resp.json()["payment"]
    assert payment["amount"] == "399.0"
    assert payment["amount_paise"] == 39900
    assert payment["currency"] == "INR"
    assert payment["razorpay_order_id"].startswith("order_TEST")
    assert mock_razorpay[0]["currency"] == "INR"


# --------------------------------------------------------------------------- #
# TEST 5: unauthorized user cannot create a payment order
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_unauthorized_user_cannot_create_payment_order(
    async_client: AsyncClient, install_mock_discovery, sample_products, mock_razorpay
):
    mission_id = await _confirmed_mission(
        async_client, install_mock_discovery, sample_products
    )

    await _register(async_client, "Mallory", "mallory@shop.com")
    resp = await _create_order(async_client, mission_id)
    assert resp.status_code == 404
    assert mock_razorpay == []  # no provider call on a rejected request

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as anon_client:
        anon_resp = await anon_client.post(
            f"/missions/{mission_id}/shopping/payment/create-order", json={}
        )
        assert anon_resp.status_code == 404

    # Owner's mission untouched; owner can still create the order themselves
    login = await async_client.post(
        "/auth/login",
        json={"email": "payton@shop.com", "password": PASSWORD},
    )
    assert login.status_code == 200
    owner_view = await async_client.get(f"/missions/{mission_id}")
    assert owner_view.json()["status"] == "checkout_confirmed"
    owner_order = await _create_order(async_client, mission_id)
    assert owner_order.status_code == 200


# --------------------------------------------------------------------------- #
# TEST 6: non-shopping mission rejected
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_non_shopping_mission_rejects_payment_order(
    async_client: AsyncClient, mock_razorpay
):
    await _register(async_client, "Rex Recovery", "rex.pay@shop.com")
    resp = await async_client.post(
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
    assert resp.status_code == 200
    mission_id = resp.json()["mission_id"]

    pay = await _create_order(async_client, mission_id)
    assert pay.status_code == 400
    assert "shopping" in pay.json()["detail"].lower()
    assert mock_razorpay == []


# --------------------------------------------------------------------------- #
# TEST 7: payment before checkout confirmation rejected
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_payment_before_checkout_confirmed_rejected(
    async_client: AsyncClient, install_mock_discovery, sample_products, mock_razorpay
):
    install_mock_discovery(sample_results=sample_products)
    await _register(async_client, "Pat Precocious", "pat@shop.com")
    resp = await async_client.post("/missions/shopping", json={"prompt": SHAMPOO_PROMPT})
    mission_id = resp.json()["mission_id"]

    # discovery_completed, no selection
    early = await _create_order(async_client, mission_id)
    assert early.status_code == 400

    # product_selected but not confirmed
    sel = await async_client.post(
        f"/missions/{mission_id}/shopping/select-product", json={"product_id": "item_dove"}
    )
    assert sel.status_code == 200
    unconfirmed = await _create_order(async_client, mission_id)
    assert unconfirmed.status_code == 400
    assert "checkout" in unconfirmed.json()["detail"].lower()
    assert mock_razorpay == []

    # checkout_review (review opened) but not confirmed
    review = await async_client.get(f"/missions/{mission_id}/shopping/checkout")
    assert review.status_code == 200
    still_blocked = await _create_order(async_client, mission_id)
    assert still_blocked.status_code == 400
    assert mock_razorpay == []


# --------------------------------------------------------------------------- #
# TEST 8: duplicate order protection (server-side idempotency)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_duplicate_order_protection(
    async_client: AsyncClient, install_mock_discovery, sample_products, mock_razorpay
):
    mission_id = await _confirmed_mission(
        async_client, install_mock_discovery, sample_products
    )

    first = await _create_order(async_client, mission_id)
    second = await _create_order(async_client, mission_id)
    third = await _create_order(async_client, mission_id)

    assert first.status_code == second.status_code == third.status_code == 200
    assert first.json()["payment"]["razorpay_order_id"] == second.json()["payment"]["razorpay_order_id"]
    assert second.json()["payment"]["razorpay_order_id"] == third.json()["payment"]["razorpay_order_id"]
    assert len(mock_razorpay) == 1  # exactly one provider call despite three requests


# --------------------------------------------------------------------------- #
# TEST 9: valid signature -> payment_verified
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_valid_signature_verifies_payment(
    async_client: AsyncClient, install_mock_discovery, sample_products, mock_razorpay
):
    mission_id = await _confirmed_mission(
        async_client, install_mock_discovery, sample_products
    )
    order = (await _create_order(async_client, mission_id)).json()["payment"]
    order_id = order["razorpay_order_id"]
    payment_id = "pay_TEST12345"
    signature = _checkout_signature(order_id, payment_id)

    resp = await async_client.post(
        f"/missions/{mission_id}/shopping/payment/verify",
        json={
            "razorpay_order_id": order_id,
            "razorpay_payment_id": payment_id,
            "razorpay_signature": signature,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["mission_status"] == "payment_verified"

    payment = data["payment"]
    assert payment["status"] == "verified"
    assert payment["razorpay_payment_id"] == payment_id
    assert payment["verified_at"] is not None
    assert payment["amount_paise"] == 39900  # amount still the server-derived one

    intent = data["shopping_intent"]
    assert intent["state"] == "payment_verified"
    assert intent["payment"]["status"] == "verified"
    assert "TEST MODE" in data["agent_message"]
    assert "Dove Hair Fall Rescue Shampoo" in data["agent_message"]


# --------------------------------------------------------------------------- #
# TEST 10: invalid signature -> payment_failed (never payment_verified)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_invalid_signature_fails_payment(
    async_client: AsyncClient, install_mock_discovery, sample_products, mock_razorpay
):
    mission_id = await _confirmed_mission(
        async_client, install_mock_discovery, sample_products
    )
    order_id = (await _create_order(async_client, mission_id)).json()["payment"]["razorpay_order_id"]

    resp = await async_client.post(
        f"/missions/{mission_id}/shopping/payment/verify",
        json={
            "razorpay_order_id": order_id,
            "razorpay_payment_id": "pay_TEST12345",
            "razorpay_signature": "deadbeef" * 8,  # garbage signature
        },
    )
    assert resp.status_code == 400

    reopened = (await async_client.get(f"/missions/{mission_id}")).json()
    assert reopened["status"] == "payment_failed"
    assert reopened["status"] != "payment_verified"
    payment = reopened["shopping_intent"]["payment"]
    assert payment["status"] == "failed"
    assert payment["failure_reason"] == "signature_verification_failed"
    assert payment.get("razorpay_payment_id") is None  # never credited


# --------------------------------------------------------------------------- #
# TEST 11: arbitrary order id / bare success flag cannot bypass
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_arbitrary_order_id_rejected(
    async_client: AsyncClient, install_mock_discovery, sample_products, mock_razorpay
):
    mission_id = await _confirmed_mission(
        async_client, install_mock_discovery, sample_products
    )
    real_order_id = (await _create_order(async_client, mission_id)).json()["payment"]["razorpay_order_id"]

    # Correct signature math, but for an order that is NOT this mission's order
    foreign_order, foreign_payment = "order_FOREIGN99", "pay_FOREIGN99"
    resp = await async_client.post(
        f"/missions/{mission_id}/shopping/payment/verify",
        json={
            "razorpay_order_id": foreign_order,
            "razorpay_payment_id": foreign_payment,
            "razorpay_signature": _checkout_signature(foreign_order, foreign_payment),
        },
    )
    assert resp.status_code == 400
    assert "match" in resp.json()["detail"].lower()

    # Mission state unchanged: still pending, original order intact
    reopened = (await async_client.get(f"/missions/{mission_id}")).json()
    assert reopened["status"] == "payment_pending"
    assert reopened["shopping_intent"]["payment"]["razorpay_order_id"] == real_order_id


@pytest.mark.asyncio
async def test_frontend_success_flag_cannot_bypass(
    async_client: AsyncClient, install_mock_discovery, sample_products, mock_razorpay
):
    mission_id = await _confirmed_mission(
        async_client, install_mock_discovery, sample_products
    )
    (await _create_order(async_client, mission_id)).json()

    resp = await async_client.post(
        f"/missions/{mission_id}/shopping/payment/verify",
        json={"success": True},
    )
    assert resp.status_code == 422  # schema requires the three Razorpay fields

    reopened = (await async_client.get(f"/missions/{mission_id}")).json()
    assert reopened["status"] == "payment_pending"
    assert reopened["shopping_intent"]["payment"]["status"] == "pending"


# --------------------------------------------------------------------------- #
# TEST 12: payment_verified persists across reload
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_payment_verified_persists_across_reload(
    async_client: AsyncClient, install_mock_discovery, sample_products, mock_razorpay
):
    mission_id = await _confirmed_mission(
        async_client, install_mock_discovery, sample_products
    )
    order_id = (await _create_order(async_client, mission_id)).json()["payment"]["razorpay_order_id"]
    payment_id = "pay_TEST777"
    await async_client.post(
        f"/missions/{mission_id}/shopping/payment/verify",
        json={
            "razorpay_order_id": order_id,
            "razorpay_payment_id": payment_id,
            "razorpay_signature": _checkout_signature(order_id, payment_id),
        },
    )

    for _ in range(2):  # simulate refresh + reopen reads
        get_resp = await async_client.get(f"/missions/{mission_id}")
        assert get_resp.status_code == 200
        reopened = get_resp.json()
        assert reopened["status"] == "payment_verified"
        payment = reopened["shopping_intent"]["payment"]
        assert payment["status"] == "verified"
        assert payment["razorpay_payment_id"] == payment_id
        assert payment["amount_paise"] == 39900


# --------------------------------------------------------------------------- #
# TEST 13: payment_failed persists across reload
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_payment_failed_persists_across_reload(
    async_client: AsyncClient, install_mock_discovery, sample_products, mock_razorpay
):
    mission_id = await _confirmed_mission(
        async_client, install_mock_discovery, sample_products
    )
    order_id = (await _create_order(async_client, mission_id)).json()["payment"]["razorpay_order_id"]
    await async_client.post(
        f"/missions/{mission_id}/shopping/payment/verify",
        json={
            "razorpay_order_id": order_id,
            "razorpay_payment_id": "pay_TEST000",
            "razorpay_signature": "0" * 64,
        },
    )

    for _ in range(2):
        reopened = (await async_client.get(f"/missions/{mission_id}")).json()
        assert reopened["status"] == "payment_failed"
        payment = reopened["shopping_intent"]["payment"]
        assert payment["status"] == "failed"
        assert payment["failure_reason"] == "signature_verification_failed"


# --------------------------------------------------------------------------- #
# TEST 14: Razorpay secrets never returned
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_secret_keys_never_returned(
    async_client: AsyncClient, install_mock_discovery, sample_products, mock_razorpay
):
    settings = get_settings()
    mission_id = await _confirmed_mission(
        async_client, install_mock_discovery, sample_products
    )

    order_resp = await _create_order(async_client, mission_id)
    order_raw = json.dumps(order_resp.json())

    order_id = order_resp.json()["payment"]["razorpay_order_id"]
    payment_id = "pay_TESTSECRET"
    verify_resp = await async_client.post(
        f"/missions/{mission_id}/shopping/payment/verify",
        json={
            "razorpay_order_id": order_id,
            "razorpay_payment_id": payment_id,
            "razorpay_signature": _checkout_signature(order_id, payment_id),
        },
    )
    verify_raw = json.dumps(verify_resp.json())

    assert settings.razorpay_key_secret not in order_raw
    assert settings.razorpay_key_secret not in verify_raw
    assert settings.razorpay_webhook_secret not in order_raw
    assert settings.razorpay_webhook_secret not in verify_raw
    # The publishable key id IS provided for checkout.js — it is safe by design
    assert order_resp.json()["payment"]["key_id"] == settings.razorpay_key_id
    assert settings.razorpay_key_id  # configured


# --------------------------------------------------------------------------- #
# TEST 15: existing recovery payment behavior remains intact
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_recovery_webhook_signature_validation_intact():
    """The recovery webhook signature mechanism is untouched by the shopping payment flow."""
    from app.payments.razorpay_client import verify_webhook_signature

    settings = get_settings()
    body = json.dumps(
        {"event": "payment.captured", "payload": {"payment": {"entity": {"id": "pay_X"}}}}
    ).encode()
    good = hmac.new(
        settings.razorpay_webhook_secret.encode(), body, hashlib.sha256
    ).hexdigest()
    bad = hmac.new(b"some-other-secret", body, hashlib.sha256).hexdigest()

    assert verify_webhook_signature(body, good) is True
    assert verify_webhook_signature(body, bad) is False
    assert verify_webhook_signature(body, "") is False


# --------------------------------------------------------------------------- #
# Terminal-state rules: verified refuses new orders; failed may retry once fresh
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_verified_payment_refuses_new_order(
    async_client: AsyncClient, install_mock_discovery, sample_products, mock_razorpay
):
    mission_id = await _confirmed_mission(
        async_client, install_mock_discovery, sample_products
    )
    order_id = (await _create_order(async_client, mission_id)).json()["payment"]["razorpay_order_id"]
    payment_id = "pay_TESTFINAL"
    await async_client.post(
        f"/missions/{mission_id}/shopping/payment/verify",
        json={
            "razorpay_order_id": order_id,
            "razorpay_payment_id": payment_id,
            "razorpay_signature": _checkout_signature(order_id, payment_id),
        },
    )

    again = await _create_order(async_client, mission_id)
    assert again.status_code == 400
    assert "verified" in again.json()["detail"].lower()
    assert len(mock_razorpay) == 1  # no second order was ever requested


@pytest.mark.asyncio
async def test_failed_payment_can_start_fresh_attempt(
    async_client: AsyncClient, install_mock_discovery, sample_products, mock_razorpay
):
    mission_id = await _confirmed_mission(
        async_client, install_mock_discovery, sample_products
    )
    first_order = (await _create_order(async_client, mission_id)).json()["payment"]["razorpay_order_id"]

    await async_client.post(
        f"/missions/{mission_id}/shopping/payment/verify",
        json={
            "razorpay_order_id": first_order,
            "razorpay_payment_id": "pay_TESTBAD",
            "razorpay_signature": "1" * 64,
        },
    )
    failed = (await async_client.get(f"/missions/{mission_id}")).json()
    assert failed["status"] == "payment_failed"

    # Minimum failure handling: one fresh order may be created from payment_failed
    retry = await _create_order(async_client, mission_id)
    assert retry.status_code == 200
    retry_payment = retry.json()["payment"]
    assert retry_payment["razorpay_order_id"] != first_order
    assert retry_payment["status"] == "pending"
    assert retry.json()["mission_status"] == "payment_pending"
    assert len(mock_razorpay) == 2

    # And the fresh attempt verifies normally
    order_id = retry_payment["razorpay_order_id"]
    payment_id = "pay_TESTRETRY"
    ok = await async_client.post(
        f"/missions/{mission_id}/shopping/payment/verify",
        json={
            "razorpay_order_id": order_id,
            "razorpay_payment_id": payment_id,
            "razorpay_signature": _checkout_signature(order_id, payment_id),
        },
    )
    assert ok.status_code == 200
    assert ok.json()["mission_status"] == "payment_verified"


@pytest.mark.asyncio
async def test_verify_without_pending_payment_rejected(
    async_client: AsyncClient, install_mock_discovery, sample_products, mock_razorpay
):
    """A verify call before any order exists is a no-target request, not a success."""
    mission_id = await _confirmed_mission(
        async_client, install_mock_discovery, sample_products
    )
    resp = await async_client.post(
        f"/missions/{mission_id}/shopping/payment/verify",
        json={
            "razorpay_order_id": "order_WHATEVER",
            "razorpay_payment_id": "pay_WHATEVER",
            "razorpay_signature": _checkout_signature("order_WHATEVER", "pay_WHATEVER"),
        },
    )
    assert resp.status_code == 400
    assert "pending" in resp.json()["detail"].lower()
    assert mock_razorpay == []


# --------------------------------------------------------------------------- #
# Suite hermeticity invariant (Task 14C.7K, followed up in Task CORE-2)
# --------------------------------------------------------------------------- #
# The helper names are kept apart from the "(" so this file's own text never matches the rule it
# enforces. They are a floor, not the rule: a named list is only as good as the last author's memory,
# and Task CORE-2 proved it again by adding a purchase helper nobody listed.
_PURCHASE_HELPERS = ("_pay_and_create_order", "_purchased")

# The Razorpay order URL, assembled so this file cannot match its own needle.
_ORDER_PATH = "create-" + "order"


def _calls_of(node: ast.AST) -> set[str]:
    return {
        call.func.id
        for call in ast.walk(node)
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
    }


def _executable(node: ast.AST) -> str:
    """A function's text with its docstring removed — prose about a route is not a call to it."""
    body = list(getattr(node, "body", []))
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    return "\n".join(ast.unparse(statement) for statement in body)


def _reaches_payment(test: ast.AST, helpers: dict) -> bool:
    """
    True when this test, or any function it calls — directly or several helpers deep — creates an
    order through the router.

    Following the call graph rather than a list of names is the point: the rule now covers the helper
    that does not exist yet.
    """
    seen: set[str] = set()
    queue = [test]
    while queue:
        node = queue.pop()
        if _ORDER_PATH in _executable(node):
            return True
        for name in _calls_of(node):
            if name not in seen:
                seen.add(name)
                definition = helpers.get(name)
                if definition is not None:
                    queue.append(definition)
    return False


def test_no_test_ever_buys_an_order_without_the_mocked_razorpay_client():
    """
    The payment provider is never reached from this suite — and now a test says so.

    `create_test_order` is a live Razorpay call. A test that drives the real router through the
    purchase helpers without patching it produces a genuinely green suite that quietly creates a real
    TEST-mode order on the account — hermetic in appearance only. `mock_razorpay` is what keeps the
    boundary, and the only way to keep keeping it is to make the omission fail rather than look fine:
    every test whose body calls one of those helpers must name `mock_razorpay` in its own fixture
    list, scanned from the AST instead of trusted to the next author's memory.
    """
    import pathlib

    offenders = []
    for path in sorted(pathlib.Path(__file__).resolve().parent.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        helpers = {
            node.name: node
            for node in tree.body
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
        }
        for node in tree.body:
            if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue
            if not node.name.startswith("test_"):
                continue
            fixtures = [argument.arg for argument in node.args.args]
            fixtures += [argument.arg for argument in node.args.kwonlyargs]
            by_name = any(f"{name}(" in ast.unparse(node) for name in _PURCHASE_HELPERS)
            if (by_name or _reaches_payment(node, helpers)) and "mock_razorpay" not in fixtures:
                offenders.append(f"{path.name}::{node.name}")
    assert offenders == [], (
        "these tests create a RESOLVE order through the real payment path without mocking Razorpay, "
        f"so they would contact the provider: {offenders}"
    )
