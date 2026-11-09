"""
Buyer-flow Phase 3 — merchant purchase authorization integration.

Covers the mandated scenarios (mandate §9), all hermetic:
  - ENGINE: approval; default-off switch; missing/invalid payment evidence; missing Shopify
    identity; already-linked; amount mismatch; prior authorization blocks duplication;
    ineligible order state; mission/order mismatch; non-TEST mode; currency guard.
  - ENDPOINT: unauthorized user; no network call when authorization is refused; config
    fail-closed (503); confirmed_linked persisted as an untrusted report; duplicate
    authorization never resubmits; connect failure leaves a resubmittable claim; timeout
    and 5xx map to purchase_unknown; refused_before_create / refused_after_create mapping;
    unknown outcome persistence and no automatic retry; late linkage convergence;
    concurrent first purchase produces exactly one record and one submission; mission
    without an order; order not confirmed; GET contract.
  - SUBMISSION LEASE (Phase 4.4): one atomic owner per concurrent resubmission — the
    loser never calls the trigger and never writes the record; attempt_count increments
    exactly once per accepted attempt; a real outcome clears a stale not_sent note;
    late/stale reports never overwrite a settled claim; a fresh in_flight lease is
    returned without a second submission; an abandoned lease recovers to purchase_unknown
    — never failed, never idle.
  - AGENT TOOL: read-only eligibility check (no writes, no submissions, no state change).
  - ISOLATION: no merchant_channel imports anywhere in backend/app; the trigger token is
    never present in any response body.

No test opens a network connection: the trigger submission runs through an installed
httpx.MockTransport, exercising the real client code (request construction, headers,
response parsing, exception mapping).
"""
import asyncio
import hashlib
import hmac
import json
import os
import re
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.authority.engine import PurchaseFacts, evaluate_purchase
from app.config import get_settings
from app.database import Base, get_db
from app.main import app
from app.models.db_models import PurchaseAuthorizationRecord, ResolveOrderRecord
from app.models.mission import PurchaseAuthorizationStatus
from app.routers import missions as missions_module
from app.tools import purchase_trigger as purchase_trigger_module

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
HEADPHONES_PROMPT = "I want to order good wireless headphones under ₹1500."
PASSWORD = "SecurePassword123!"
TRIGGER_TOKEN = "trigger-token-TEST-DO-NOT-LEAK"
OWNER_EMAIL = "petra@shop.com"

# A Shopify-sourced product so the discovery -> selection -> order path persists the
# complete catalog identity through the normal production path, with no row surgery.
SHOPIFY_PRODUCT = {
    "product_id": "item_shopify_headphones",
    "title": "RESOLVE Test Headphones",
    "price": "₹799.00",
    "extracted_price": Decimal("799"),
    "currency": "INR",
    "merchant": "RESOLVE Test Store",
    "product_url": "https://resolve-commerce-lab.myshopify.com/products/test-headphones",
    "rating": 4.6,
    "review_count": 12,
    "availability": "ACTIVE",
    "source": "shopify",
    "shopify_product_gid": "gid://shopify/Product/123456",
    "shopify_variant_gid": "gid://shopify/ProductVariant/789012",
    "shopify_store_domain": "resolve-commerce-lab.myshopify.com",
}


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
async def purchase_env(monkeypatch):
    """Hermetic settings + SQLite database + client factory + session factory."""
    settings = get_settings()
    monkeypatch.setattr(settings, "razorpay_key_id", "rzp_test_hermetic", raising=False)
    monkeypatch.setattr(settings, "razorpay_key_secret", "hermetic-test-secret", raising=False)
    monkeypatch.setattr(settings, "shopping_purchase_authority_enabled", True, raising=False)
    monkeypatch.setattr(settings, "merchant_trigger_url", "http://trigger.test", raising=False)
    monkeypatch.setattr(settings, "merchant_trigger_token", TRIGGER_TOKEN, raising=False)

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
def trigger_transport(monkeypatch):
    """Install an httpx.MockTransport into the purchase_trigger seam and record every request."""
    calls: list[httpx.Request] = []

    def install(responder):
        async def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            result = responder(request)
            if asyncio.iscoroutine(result):
                result = await result
            return result

        monkeypatch.setattr(
            purchase_trigger_module,
            "_default_transport",
            lambda: httpx.MockTransport(handler),
        )

    return {"install": install, "calls": calls}


@pytest.fixture
def mock_razorpay(monkeypatch):
    """Deterministic stand-in for the Razorpay TEST order API."""
    calls: list[dict] = []
    counter = {"n": 0}

    def fake_create_test_order(amount_paise, currency, receipt, notes):
        calls.append({"amount_paise": amount_paise, "currency": currency})
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


def trigger_response(outcome_class: str, **extra) -> httpx.Response:
    body = {
        "outcome_class": outcome_class,
        "outcome_status": outcome_class,
        "correlation_id": "corr_test_0001",
        "external_order_id": "gid://shopify/Order/999000",
        "event_id": "evt_test_0001",
        "reason": "deterministic trigger test reason",
    }
    body.update(extra)
    return httpx.Response(200, json=body)


def assert_trigger_request(request: httpx.Request, mission_id: str, order_id: str) -> None:
    assert request.url.path == "/purchase"
    assert request.headers["X-Merchant-Trigger-Token"] == TRIGGER_TOKEN
    assert json.loads(request.content) == {
        "mission_id": mission_id,
        "resolve_order_id": order_id,
    }


def _checkout_signature(order_id: str, payment_id: str) -> str:
    secret = get_settings().razorpay_key_secret.encode()
    return hmac.new(secret, f"{order_id}|{payment_id}".encode(), hashlib.sha256).hexdigest()


async def _register(client: AsyncClient, name: str, email: str) -> None:
    reg = await client.post(
        "/auth/register", json={"name": name, "email": email, "password": PASSWORD}
    )
    assert reg.status_code == 201


async def _login(client: AsyncClient, email: str) -> None:
    login = await client.post("/auth/login", json={"email": email, "password": PASSWORD})
    assert login.status_code == 200


async def _purchase_ready_mission(
    make_client, session_factory, install_discovery, mock_razorpay
) -> tuple[str, str]:
    """
    A confirmed order with complete Shopify identity, reached entirely through the normal
    production path: discovery -> selection -> checkout confirm -> TEST payment -> verify
    -> order create -> order confirm. Returns (mission_id, order_id).
    """
    install_discovery(sample_results=[missions_module.ProductResult(**SHOPIFY_PRODUCT)])
    client = make_client()
    try:
        await _register(client, "Petra Purchaser", OWNER_EMAIL)
        resp = await client.post("/missions/shopping", json={"prompt": HEADPHONES_PROMPT})
        assert resp.status_code == 200
        mission_id = resp.json()["mission_id"]

        sel = await client.post(
            f"/missions/{mission_id}/shopping/select-product",
            json={"product_id": SHOPIFY_PRODUCT["product_id"]},
        )
        assert sel.status_code == 200, sel.text
        assert (await client.post(f"/missions/{mission_id}/shopping/checkout/confirm")).status_code == 200

        order_resp = await client.post(
            f"/missions/{mission_id}/shopping/payment/create-order", json={}
        )
        assert order_resp.status_code == 200
        razorpay_order_id = order_resp.json()["payment"]["razorpay_order_id"]
        payment_id = "pay_TESTPURCHASE1"
        verify = await client.post(
            f"/missions/{mission_id}/shopping/payment/verify",
            json={
                "razorpay_order_id": razorpay_order_id,
                "razorpay_payment_id": payment_id,
                "razorpay_signature": _checkout_signature(razorpay_order_id, payment_id),
            },
        )
        assert verify.status_code == 200

        created = await client.post(f"/missions/{mission_id}/order", json={})
        assert created.status_code == 200
        order_id = created.json()["order_id"]
        # The identity really was persisted by the normal order path
        async with session_factory() as session:
            row = await session.get(ResolveOrderRecord, order_id)
            assert row.shopify_product_gid == SHOPIFY_PRODUCT["shopify_product_gid"]
            assert row.shopify_variant_gid == SHOPIFY_PRODUCT["shopify_variant_gid"]

        confirmed = await client.post(f"/missions/{mission_id}/order/confirm", json={})
        assert confirmed.status_code == 200
        return mission_id, order_id
    finally:
        await client.aclose()


# --------------------------------------------------------------------------- #
# ENGINE tests (pure)
# --------------------------------------------------------------------------- #
def good_facts(**overrides) -> PurchaseFacts:
    base = dict(
        purchase_authority_enabled=True,
        test_mode=True,
        order_belongs_to_mission=True,
        order_state_eligible=True,
        merchant_identity_complete=True,
        already_linked=False,
        canonical_amount=Decimal("799"),
        currency="INR",
        payment_evidence_verified=True,
        payment_amount_matches_order=True,
        prior_authorization_state=None,
    )
    base.update(overrides)
    return PurchaseFacts(**base)


def test_engine_approves_complete_facts():
    decision = evaluate_purchase(good_facts())
    assert decision.approved
    assert decision.action.value == "place_merchant_order"
    assert decision.requested_amount == Decimal("799")


def test_engine_blocks_when_deployment_switch_off():
    decision = evaluate_purchase(good_facts(purchase_authority_enabled=False))
    assert not decision.approved
    assert "not configured" in decision.reason


def test_engine_blocks_missing_payment_evidence():
    decision = evaluate_purchase(good_facts(payment_evidence_verified=False))
    assert not decision.approved
    assert "payment" in decision.reason.lower()


def test_engine_blocks_amount_mismatch():
    decision = evaluate_purchase(good_facts(payment_amount_matches_order=False))
    assert not decision.approved
    assert "amount" in decision.reason.lower()


def test_engine_blocks_incomplete_merchant_identity():
    decision = evaluate_purchase(good_facts(merchant_identity_complete=False))
    assert not decision.approved
    assert "identity" in decision.reason.lower()


def test_engine_blocks_already_linked():
    decision = evaluate_purchase(good_facts(already_linked=True))
    assert not decision.approved
    assert "already linked" in decision.reason


def test_engine_blocks_every_terminal_prior_authorization():
    for state in sorted(PurchaseAuthorizationStatus.terminal_states()):
        decision = evaluate_purchase(good_facts(prior_authorization_state=state))
        assert not decision.approved, state
        assert "duplicate" in decision.reason


def test_engine_allows_resubmission_from_bare_claim():
    decision = evaluate_purchase(
        good_facts(prior_authorization_state=PurchaseAuthorizationStatus.AUTHORIZED.value)
    )
    assert decision.approved


def test_engine_blocks_ineligible_order_state():
    decision = evaluate_purchase(good_facts(order_state_eligible=False))
    assert not decision.approved
    assert "eligible" in decision.reason


def test_engine_blocks_mission_mismatch():
    decision = evaluate_purchase(good_facts(order_belongs_to_mission=False))
    assert not decision.approved


def test_engine_blocks_non_test_mode():
    decision = evaluate_purchase(good_facts(test_mode=False))
    assert not decision.approved
    assert "test mode" in decision.reason.lower()


def test_engine_blocks_non_positive_amount_and_wrong_currency():
    assert not evaluate_purchase(good_facts(canonical_amount=None)).approved
    assert not evaluate_purchase(good_facts(canonical_amount=Decimal("0"))).approved
    assert not evaluate_purchase(good_facts(currency="USD")).approved


# --------------------------------------------------------------------------- #
# ENDPOINT tests
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_purchase_reports_confirmed_linkage_as_untrusted(
    purchase_env, trigger_transport, mock_razorpay, install_mock_discovery
):
    make_client, session_factory = purchase_env
    mission_id, order_id = await _purchase_ready_mission(
        make_client, session_factory, install_mock_discovery, mock_razorpay
    )
    trigger_transport["install"](lambda request: trigger_response("confirmed_linked"))

    client = make_client()
    await _login(client, OWNER_EMAIL)
    try:
        resp = await client.post(f"/missions/{mission_id}/order/purchase")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == PurchaseAuthorizationStatus.PURCHASE_REPORTED_LINKED.value
        assert data["authorization_id"].startswith("purch_")
        assert data["attempt_count"] == 1
        assert data["authority_decision"] == "approved"
        assert data["trigger_report"]["outcome_class"] == "confirmed_linked"
        assert "Untrusted report" in data["trigger_report"]["note"]
        # The report alone is not linkage: the authoritative order row has no reference yet
        assert data["merchant_order_linked"] is False
        assert data["merchant_order_linkage"] == "awaiting_linkage"

        assert len(trigger_transport["calls"]) == 1
        assert_trigger_request(trigger_transport["calls"][0], mission_id, order_id)

        async with session_factory() as session:
            row = (
                await session.execute(
                    select(PurchaseAuthorizationRecord).where(
                        PurchaseAuthorizationRecord.order_id == order_id
                    )
                )
            ).scalar_one()
            assert row.status == PurchaseAuthorizationStatus.PURCHASE_REPORTED_LINKED.value
            assert row.trigger_reported_outcome_class == "confirmed_linked"
            assert row.attempt_count == 1

        # Secrets never appear in the response
        assert TRIGGER_TOKEN not in resp.text
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_duplicate_authorization_never_resubmits(
    purchase_env, trigger_transport, mock_razorpay, install_mock_discovery
):
    make_client, session_factory = purchase_env
    mission_id, _ = await _purchase_ready_mission(
        make_client, session_factory, install_mock_discovery, mock_razorpay
    )
    trigger_transport["install"](lambda request: trigger_response("confirmed_linked"))

    client = make_client()
    await _login(client, OWNER_EMAIL)
    try:
        first = await client.post(f"/missions/{mission_id}/order/purchase")
        second = await client.post(f"/missions/{mission_id}/order/purchase")
        third = await client.post(f"/missions/{mission_id}/order/purchase")
        assert first.status_code == second.status_code == third.status_code == 200
        assert first.json()["authorization_id"] == second.json()["authorization_id"]
        assert second.json()["authorization_id"] == third.json()["authorization_id"]
        assert second.json()["attempt_count"] == first.json()["attempt_count"] == 1
        assert len(trigger_transport["calls"]) == 1
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_connect_failure_leaves_resubmittable_claim(
    purchase_env, trigger_transport, mock_razorpay, install_mock_discovery
):
    make_client, session_factory = purchase_env
    mission_id, _ = await _purchase_ready_mission(
        make_client, session_factory, install_mock_discovery, mock_razorpay
    )
    trigger_transport["install"](
        lambda request: (_ for _ in ()).throw(
            httpx.ConnectError("connection refused", request=request)
        )
    )

    client = make_client()
    await _login(client, OWNER_EMAIL)
    try:
        first = await client.post(f"/missions/{mission_id}/order/purchase")
        assert first.status_code == 200
        data = first.json()
        # Nothing was sent, so the claim stays resubmittable — not unknown, not failed
        assert data["status"] == PurchaseAuthorizationStatus.AUTHORIZED.value
        assert "connection" in (data["error_note"] or "").lower()
        assert data["trigger_report"]["outcome_class"] is None

        # A second explicit POST submits from the surviving claim
        trigger_transport["install"](lambda request: trigger_response("confirmed_linked"))
        second = await client.post(f"/missions/{mission_id}/order/purchase")
        assert second.status_code == 200
        assert second.json()["authorization_id"] == data["authorization_id"]
        assert second.json()["attempt_count"] == 2
        assert second.json()["status"] == PurchaseAuthorizationStatus.PURCHASE_REPORTED_LINKED.value
        assert second.json()["error_note"] is None
        assert len(trigger_transport["calls"]) == 2  # both POSTs attempted the trigger
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_timeout_maps_to_purchase_unknown_and_never_retries(
    purchase_env, trigger_transport, mock_razorpay, install_mock_discovery
):
    make_client, session_factory = purchase_env
    mission_id, order_id = await _purchase_ready_mission(
        make_client, session_factory, install_mock_discovery, mock_razorpay
    )
    trigger_transport["install"](
        lambda request: (_ for _ in ()).throw(
            httpx.ReadTimeout("answer lost", request=request)
        )
    )

    client = make_client()
    await _login(client, OWNER_EMAIL)
    try:
        first = await client.post(f"/missions/{mission_id}/order/purchase")
        assert first.status_code == 200
        assert first.json()["status"] == PurchaseAuthorizationStatus.PURCHASE_UNKNOWN.value
        assert "unknown" in (first.json()["trigger_report"]["reason"] or "").lower()

        # A further POST returns the existing record and never submits again
        second = await client.post(f"/missions/{mission_id}/order/purchase")
        assert second.status_code == 200
        assert second.json()["status"] == PurchaseAuthorizationStatus.PURCHASE_UNKNOWN.value
        assert second.json()["attempt_count"] == 1
        assert len(trigger_transport["calls"]) == 1

        async with session_factory() as session:
            row = (
                await session.execute(
                    select(PurchaseAuthorizationRecord).where(
                        PurchaseAuthorizationRecord.order_id == order_id
                    )
                )
            ).scalar_one()
            assert row.status == PurchaseAuthorizationStatus.PURCHASE_UNKNOWN.value
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_5xx_maps_to_purchase_unknown(
    purchase_env, trigger_transport, mock_razorpay, install_mock_discovery
):
    make_client, session_factory = purchase_env
    mission_id, _ = await _purchase_ready_mission(
        make_client, session_factory, install_mock_discovery, mock_razorpay
    )
    trigger_transport["install"](lambda request: httpx.Response(502, text="bad gateway"))

    client = make_client()
    await _login(client, OWNER_EMAIL)
    try:
        resp = await client.post(f"/missions/{mission_id}/order/purchase")
        assert resp.status_code == 200
        assert resp.json()["status"] == PurchaseAuthorizationStatus.PURCHASE_UNKNOWN.value
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_unknown_outcome_class_maps_to_purchase_unknown(
    purchase_env, trigger_transport, mock_razorpay, install_mock_discovery
):
    make_client, session_factory = purchase_env
    mission_id, _ = await _purchase_ready_mission(
        make_client, session_factory, install_mock_discovery, mock_razorpay
    )
    trigger_transport["install"](
        lambda request: trigger_response("unknown_requires_reconciliation")
    )

    client = make_client()
    await _login(client, OWNER_EMAIL)
    try:
        resp = await client.post(f"/missions/{mission_id}/order/purchase")
        assert resp.status_code == 200
        assert resp.json()["status"] == PurchaseAuthorizationStatus.PURCHASE_UNKNOWN.value
        assert resp.json()["trigger_report"]["outcome_class"] == "unknown_requires_reconciliation"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_refused_before_create_maps_to_purchase_failed(
    purchase_env, trigger_transport, mock_razorpay, install_mock_discovery
):
    make_client, session_factory = purchase_env
    mission_id, _ = await _purchase_ready_mission(
        make_client, session_factory, install_mock_discovery, mock_razorpay
    )
    trigger_transport["install"](
        lambda request: trigger_response(
            "refused_before_create", external_order_id=None, reason="a gate refused: currency mismatch"
        )
    )

    client = make_client()
    await _login(client, OWNER_EMAIL)
    try:
        resp = await client.post(f"/missions/{mission_id}/order/purchase")
        assert resp.status_code == 200
        assert resp.json()["status"] == PurchaseAuthorizationStatus.PURCHASE_FAILED.value
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_refused_after_create_maps_to_linkage_refused_never_failed(
    purchase_env, trigger_transport, mock_razorpay, install_mock_discovery
):
    make_client, session_factory = purchase_env
    mission_id, _ = await _purchase_ready_mission(
        make_client, session_factory, install_mock_discovery, mock_razorpay
    )
    trigger_transport["install"](lambda request: trigger_response("refused_after_create"))

    client = make_client()
    await _login(client, OWNER_EMAIL)
    try:
        resp = await client.post(f"/missions/{mission_id}/order/purchase")
        assert resp.status_code == 200
        data = resp.json()
        # A merchant order MAY exist: never 'failed', never retried
        assert data["status"] == PurchaseAuthorizationStatus.LINKAGE_REFUSED.value
        assert data["status"] != PurchaseAuthorizationStatus.PURCHASE_FAILED.value
        again = await client.post(f"/missions/{mission_id}/order/purchase")
        assert again.json()["attempt_count"] == 1
        assert len(trigger_transport["calls"]) == 1
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_pre_execution_refusals_leave_claim_resubmittable(
    purchase_env, trigger_transport, mock_razorpay, install_mock_discovery
):
    make_client, session_factory = purchase_env
    mission_id, _ = await _purchase_ready_mission(
        make_client, session_factory, install_mock_discovery, mock_razorpay
    )
    trigger_transport["install"](lambda request: httpx.Response(401, json={"reason": "bad token"}))

    client = make_client()
    await _login(client, OWNER_EMAIL)
    try:
        resp = await client.post(f"/missions/{mission_id}/order/purchase")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == PurchaseAuthorizationStatus.AUTHORIZED.value
        assert "401" in (data["error_note"] or "")
        # 409 and 422 are the same pre-execution refusals
        trigger_transport["install"](lambda request: httpx.Response(409, json={}))
        resp2 = await client.post(f"/missions/{mission_id}/order/purchase")
        assert resp2.json()["status"] == PurchaseAuthorizationStatus.AUTHORIZED.value
        assert resp2.json()["attempt_count"] == 2
        trigger_transport["install"](lambda request: httpx.Response(422, json={"problems": []}))
        resp3 = await client.post(f"/missions/{mission_id}/order/purchase")
        assert resp3.json()["status"] == PurchaseAuthorizationStatus.AUTHORIZED.value
        assert resp3.json()["attempt_count"] == 3
        assert len(trigger_transport["calls"]) == 3
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_unauthorized_user_cannot_purchase_and_nothing_leaks(
    purchase_env, trigger_transport, mock_razorpay, install_mock_discovery
):
    make_client, session_factory = purchase_env
    mission_id, _ = await _purchase_ready_mission(
        make_client, session_factory, install_mock_discovery, mock_razorpay
    )
    trigger_transport["install"](lambda request: trigger_response("confirmed_linked"))

    client = make_client()
    await _login(client, OWNER_EMAIL)
    try:
        # A different authenticated user
        await _register(client, "Mallory Buyer", "mallory@shop.com")
        resp = await client.post(f"/missions/{mission_id}/order/purchase")
        assert resp.status_code == 404
        get_resp = await client.get(f"/missions/{mission_id}/purchase")
        assert get_resp.status_code == 404

        # Anonymous client
        async with make_client() as anon:
            anon_resp = await anon.post(f"/missions/{mission_id}/order/purchase")
            assert anon_resp.status_code == 404

        # Nothing was written and nothing was submitted
        async with session_factory() as session:
            count = await session.scalar(select(func.count()).select_from(PurchaseAuthorizationRecord))
            assert count == 0
        assert len(trigger_transport["calls"]) == 0
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_no_network_when_authority_refused(
    purchase_env, trigger_transport, mock_razorpay, install_mock_discovery, monkeypatch
):
    make_client, session_factory = purchase_env
    monkeypatch.setattr(
        get_settings(), "shopping_purchase_authority_enabled", False, raising=False
    )
    mission_id, _ = await _purchase_ready_mission(
        make_client, session_factory, install_mock_discovery, mock_razorpay
    )
    trigger_transport["install"](lambda request: trigger_response("confirmed_linked"))

    client = make_client()
    await _login(client, OWNER_EMAIL)
    try:
        resp = await client.post(f"/missions/{mission_id}/order/purchase")
        assert resp.status_code == 403
        assert "not configured" in resp.json()["detail"]
        async with session_factory() as session:
            count = await session.scalar(select(func.count()).select_from(PurchaseAuthorizationRecord))
            assert count == 0
        assert len(trigger_transport["calls"]) == 0
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_config_fail_closed_before_any_claim(
    purchase_env, trigger_transport, mock_razorpay, install_mock_discovery, monkeypatch
):
    make_client, session_factory = purchase_env
    monkeypatch.setattr(get_settings(), "merchant_trigger_url", "", raising=False)
    monkeypatch.setattr(get_settings(), "merchant_trigger_token", "", raising=False)
    mission_id, _ = await _purchase_ready_mission(
        make_client, session_factory, install_mock_discovery, mock_razorpay
    )
    trigger_transport["install"](lambda request: trigger_response("confirmed_linked"))

    client = make_client()
    await _login(client, OWNER_EMAIL)
    try:
        resp = await client.post(f"/missions/{mission_id}/order/purchase")
        assert resp.status_code == 503
        async with session_factory() as session:
            count = await session.scalar(select(func.count()).select_from(PurchaseAuthorizationRecord))
            assert count == 0
        assert len(trigger_transport["calls"]) == 0
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_order_created_but_not_confirmed_is_blocked(
    purchase_env, trigger_transport, mock_razorpay, install_mock_discovery
):
    make_client, session_factory = purchase_env
    install_discovery = install_mock_discovery
    client = make_client()
    try:
        install_discovery(sample_results=[missions_module.ProductResult(**SHOPIFY_PRODUCT)])
        await _register(client, "Nina Noconfirm", "nina@shop.com")
        resp = await client.post("/missions/shopping", json={"prompt": HEADPHONES_PROMPT})
        mission_id = resp.json()["mission_id"]
        await client.post(
            f"/missions/{mission_id}/shopping/select-product",
            json={"product_id": SHOPIFY_PRODUCT["product_id"]},
        )
        await client.post(f"/missions/{mission_id}/shopping/checkout/confirm")
        order_resp = await client.post(
            f"/missions/{mission_id}/shopping/payment/create-order", json={}
        )
        razorpay_order_id = order_resp.json()["payment"]["razorpay_order_id"]
        await client.post(
            f"/missions/{mission_id}/shopping/payment/verify",
            json={
                "razorpay_order_id": razorpay_order_id,
                "razorpay_payment_id": "pay_TESTNOCONFIRM",
                "razorpay_signature": _checkout_signature(razorpay_order_id, "pay_TESTNOCONFIRM"),
            },
        )
        created = await client.post(f"/missions/{mission_id}/order", json={})
        assert created.status_code == 200
        assert created.json()["status"] == "order_created"

        trigger_transport["install"](lambda request: trigger_response("confirmed_linked"))
        resp = await client.post(f"/missions/{mission_id}/order/purchase")
        assert resp.status_code == 403
        assert "confirmed" in resp.json()["detail"].lower()
        async with session_factory() as session:
            count = await session.scalar(select(func.count()).select_from(PurchaseAuthorizationRecord))
            assert count == 0
        assert len(trigger_transport["calls"]) == 0
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_mission_without_order_is_404(
    purchase_env, trigger_transport, mock_razorpay, install_mock_discovery
):
    make_client, session_factory = purchase_env
    install_discovery = install_mock_discovery
    client = make_client()
    try:
        install_discovery(sample_results=[missions_module.ProductResult(**SHOPIFY_PRODUCT)])
        await _register(client, "Omar Orderless", "omar@shop.com")
        resp = await client.post("/missions/shopping", json={"prompt": HEADPHONES_PROMPT})
        mission_id = resp.json()["mission_id"]
        purchase = await client.post(f"/missions/{mission_id}/order/purchase")
        assert purchase.status_code == 404
        assert len(trigger_transport["calls"]) == 0
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_concurrent_first_purchase_makes_one_record_one_submission(
    purchase_env, trigger_transport, mock_razorpay, install_mock_discovery, tmp_path
):
    make_client, session_factory = purchase_env

    # A file-backed database gives the two concurrent requests genuinely separate
    # connections: the unique order_id constraint decides the winner, and the loser's
    # rollback cannot stomp the winner's in-flight transaction (which a single shared
    # in-memory StaticPool connection would allow).
    file_engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'concurrent.db'}", echo=False
    )
    file_session = async_sessionmaker(
        file_engine, expire_on_commit=False, class_=AsyncSession
    )
    async with file_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async def override_with_file_db():
        async with file_session() as session:
            yield session

    app.dependency_overrides[get_db] = override_with_file_db
    try:
        mission_id, order_id = await _purchase_ready_mission(
            make_client, file_session, install_mock_discovery, mock_razorpay
        )

        async def slow_confirmed(request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(0.3)
            return trigger_response("confirmed_linked")

        trigger_transport["install"](slow_confirmed)

        async with make_client() as client_a, make_client() as client_b:
            await _login(client_a, OWNER_EMAIL)
            await _login(client_b, OWNER_EMAIL)
            first, second = await asyncio.gather(
                client_a.post(f"/missions/{mission_id}/order/purchase"),
                client_b.post(f"/missions/{mission_id}/order/purchase"),
            )
            assert first.status_code == 200
            assert second.status_code == 200
            assert first.json()["authorization_id"] == second.json()["authorization_id"]
            # Exactly one submission reached the trigger
            assert len(trigger_transport["calls"]) == 1
            assert_trigger_request(trigger_transport["calls"][0], mission_id, order_id)

        async with file_session() as session:
            rows = (
                await session.execute(select(PurchaseAuthorizationRecord))
            ).scalars().all()
            assert len(rows) == 1
    finally:
        await file_engine.dispose()


@pytest.mark.asyncio
async def test_late_linkage_convergence_on_get(
    purchase_env, trigger_transport, mock_razorpay, install_mock_discovery
):
    make_client, session_factory = purchase_env
    mission_id, order_id = await _purchase_ready_mission(
        make_client, session_factory, install_mock_discovery, mock_razorpay
    )
    trigger_transport["install"](lambda request: trigger_response("confirmed_linked"))

    client = make_client()
    await _login(client, OWNER_EMAIL)
    try:
        posted = (await client.post(f"/missions/{mission_id}/order/purchase")).json()
        assert posted["merchant_order_linked"] is False

        # The authoritative linkage arrives later, written by the linkage receiver
        # (app.tools.shopify.linkage) — simulated here by the same write it performs.
        async with session_factory() as session:
            row = await session.get(ResolveOrderRecord, order_id)
            row.provider_order_reference = "gid://shopify/Order/999000"
            await session.commit()

        got = (await client.get(f"/missions/{mission_id}/purchase")).json()
        assert got["merchant_order_linked"] is True
        assert got["merchant_order_linkage"] == "linked"
        # The authorization record's own status is untouched by the late linkage
        assert got["status"] == PurchaseAuthorizationStatus.PURCHASE_REPORTED_LINKED.value
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_get_purchase_contract_and_404(
    purchase_env, trigger_transport, mock_razorpay, install_mock_discovery
):
    make_client, session_factory = purchase_env
    mission_id, _ = await _purchase_ready_mission(
        make_client, session_factory, install_mock_discovery, mock_razorpay
    )
    trigger_transport["install"](lambda request: trigger_response("confirmed_linked"))

    client = make_client()
    await _login(client, OWNER_EMAIL)
    try:
        before = await client.get(f"/missions/{mission_id}/purchase")
        assert before.status_code == 404

        await client.post(f"/missions/{mission_id}/order/purchase")
        got = await client.get(f"/missions/{mission_id}/purchase")
        assert got.status_code == 200
        data = got.json()
        for key in (
            "authorization_id",
            "order_id",
            "mission_id",
            "status",
            "authority_decision",
            "decision_reason",
            "attempt_count",
            "submission_state",
            "trigger_report",
            "merchant_order_linked",
            "merchant_order_linkage",
            "fulfillment_note",
        ):
            assert key in data
        assert TRIGGER_TOKEN not in got.text
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_crash_window_retry_persists_purchase_unknown_never_failed(
    purchase_env, trigger_transport, mock_razorpay, install_mock_discovery
):
    """
    The mandated crash-window regression. The trigger created the Shopify order and then died
    before answering; the backend's claim survives resubmittable; the user retries; the
    trigger's PENDING outbox row refuses the second create as an unresolved prior attempt;
    the backend must persist purchase_unknown — never purchase_failed, which would claim no
    merchant order exists. Exactly one Shopify create occurs across the whole scenario.
    """
    make_client, session_factory = purchase_env
    mission_id, order_id = await _purchase_ready_mission(
        make_client, session_factory, install_mock_discovery, mock_razorpay
    )

    created = {"orders": 0}

    def crash_after_create(request: httpx.Request) -> httpx.Response:
        created["orders"] += 1  # the Shopify create happened, then the trigger died
        raise httpx.ReadTimeout("the trigger died before it could answer", request=request)

    trigger_transport["install"](crash_after_create)

    client = make_client()
    await _login(client, OWNER_EMAIL)
    try:
        first = await client.post(f"/missions/{mission_id}/order/purchase")
        assert first.status_code == 200
        assert first.json()["status"] == PurchaseAuthorizationStatus.PURCHASE_UNKNOWN.value
        assert created["orders"] == 1

        # Only a crash BEFORE the report is persisted leaves the claim resubmittable.
        # Phase 4.4: a crashed-mid-flight attempt instead leaves an abandoned lease that
        # recovery closes as unknown (tested separately below); this scenario rewinds to
        # the bare resubmittable claim — the state after a lease the owner released
        # because nothing was sent — so the trigger's unresolved-prior-attempt refusal is
        # still exercised end-to-end.
        async with session_factory() as session:
            row = (
                await session.execute(
                    select(PurchaseAuthorizationRecord).where(
                        PurchaseAuthorizationRecord.order_id == order_id
                    )
                )
            ).scalar_one()
            row.status = PurchaseAuthorizationStatus.AUTHORIZED.value
            row.submission_state = "idle"
            row.submission_owner = None
            row.trigger_reported_outcome_class = None
            row.trigger_reported_outcome_status = None
            row.trigger_reported_correlation_id = None
            row.trigger_reported_external_order_id = None
            row.trigger_reported_event_id = None
            row.trigger_reported_reason = None
            row.error_note = None
            await session.commit()

        # The retry reaches the trigger, whose PENDING outbox row refuses as unknown
        # (the classification corrected in this phase: never refused_before_create).
        trigger_transport["install"](
            lambda request: trigger_response(
                "unknown_requires_reconciliation",
                outcome_status=None,
                reason=(
                    "a previous attempt left the outbox row PENDING, so whether its create "
                    "happened is unknown; reconcile (a separate deliberate step) is the way "
                    "out, never a retry"
                ),
            )
        )
        second = await client.post(f"/missions/{mission_id}/order/purchase")
        assert second.status_code == 200
        data = second.json()
        assert data["status"] == PurchaseAuthorizationStatus.PURCHASE_UNKNOWN.value
        assert data["status"] != PurchaseAuthorizationStatus.PURCHASE_FAILED.value
        assert data["trigger_report"]["outcome_class"] == "unknown_requires_reconciliation"
        assert data["attempt_count"] == 2
        assert len(trigger_transport["calls"]) == 2  # the retry did reach the trigger
        assert created["orders"] == 1  # ...and no second Shopify create occurred

        async with session_factory() as session:
            row = (
                await session.execute(
                    select(PurchaseAuthorizationRecord).where(
                        PurchaseAuthorizationRecord.order_id == order_id
                    )
                )
            ).scalar_one()
            assert row.status == PurchaseAuthorizationStatus.PURCHASE_UNKNOWN.value
            assert row.trigger_reported_outcome_class == "unknown_requires_reconciliation"
            # Reason text and persisted status never contradict each other
            note = missions_module._purchase_submission_note(row)
            assert "no merchant order exists" not in note
            assert "unknown" in note.lower()

        # The user-facing GET reports the unknown honestly
        got = (await client.get(f"/missions/{mission_id}/purchase")).json()
        assert got["status"] == PurchaseAuthorizationStatus.PURCHASE_UNKNOWN.value
        assert got["merchant_order_linked"] is False
        assert got["merchant_order_linkage"] == "awaiting_linkage"

        # purchase_unknown is terminal: a further POST never resubmits
        third = await client.post(f"/missions/{mission_id}/order/purchase")
        assert third.json()["attempt_count"] == 2
        assert len(trigger_transport["calls"]) == 2

        # A late linkage event still converges the authoritative order linkage
        async with session_factory() as session:
            order_row = await session.get(ResolveOrderRecord, order_id)
            order_row.provider_order_reference = "gid://shopify/Order/999000"
            await session.commit()
        got_after = (await client.get(f"/missions/{mission_id}/purchase")).json()
        assert got_after["merchant_order_linked"] is True
        assert got_after["merchant_order_linkage"] == "linked"
        assert got_after["status"] == PurchaseAuthorizationStatus.PURCHASE_UNKNOWN.value
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_refused_after_create_get_is_honest_and_linkage_converges(
    purchase_env, trigger_transport, mock_razorpay, install_mock_discovery
):
    """The order-exists refusal: GET never reads as failed, and a late linkage still lands."""
    make_client, session_factory = purchase_env
    mission_id, order_id = await _purchase_ready_mission(
        make_client, session_factory, install_mock_discovery, mock_razorpay
    )
    trigger_transport["install"](lambda request: trigger_response("refused_after_create"))

    client = make_client()
    await _login(client, OWNER_EMAIL)
    try:
        posted = (await client.post(f"/missions/{mission_id}/order/purchase")).json()
        assert posted["status"] == PurchaseAuthorizationStatus.LINKAGE_REFUSED.value

        got = (await client.get(f"/missions/{mission_id}/purchase")).json()
        assert got["status"] == PurchaseAuthorizationStatus.LINKAGE_REFUSED.value
        assert got["trigger_report"]["outcome_class"] == "refused_after_create"
        assert got["merchant_order_linked"] is False
        assert got["merchant_order_linkage"] == "awaiting_linkage"

        async with session_factory() as session:
            row = (
                await session.execute(
                    select(PurchaseAuthorizationRecord).where(
                        PurchaseAuthorizationRecord.order_id == order_id
                    )
                )
            ).scalar_one()
            note = missions_module._purchase_submission_note(row)
            assert "never marked failed" in note
            assert "no merchant order exists" not in note

        # Terminal: a further POST never resubmits
        again = await client.post(f"/missions/{mission_id}/order/purchase")
        assert again.json()["attempt_count"] == 1
        assert len(trigger_transport["calls"]) == 1

        # The linkage event, when it eventually arrives, converges the authoritative order
        async with session_factory() as session:
            order_row = await session.get(ResolveOrderRecord, order_id)
            order_row.provider_order_reference = "gid://shopify/Order/999000"
            await session.commit()
        got_after = (await client.get(f"/missions/{mission_id}/purchase")).json()
        assert got_after["merchant_order_linked"] is True
        assert got_after["merchant_order_linkage"] == "linked"
        assert got_after["status"] == PurchaseAuthorizationStatus.LINKAGE_REFUSED.value
    finally:
        await client.aclose()


# --------------------------------------------------------------------------- #
# SUBMISSION LEASE (Phase 4.4) — atomic ownership, monotonic reports, crash honesty
# --------------------------------------------------------------------------- #
PG_TEST_URL = os.environ.get("RESOLVE_PG_TEST_URL", "")


def _run_lock_refusal() -> httpx.Response:
    """The trigger's pre-execution 409: nothing ran, so the claim stays resubmittable."""
    return httpx.Response(
        409,
        json={"outcome_class": "concurrent_execution", "reason": "the run lock is held"},
    )


@pytest.fixture
async def pg_purchase_env(purchase_env):
    """The purchase_env settings with the database pointed at a THROWAWAY PostgreSQL
    database named by RESOLVE_PG_TEST_URL (never the development database) — the same
    convention as test_m21_sequence_allocation. Skips when the runner provides none."""
    if not PG_TEST_URL:
        pytest.skip(
            "RESOLVE_PG_TEST_URL not set — PostgreSQL lease concurrency not exercised here"
        )
    make_client, _ = purchase_env
    pg_engine = create_async_engine(PG_TEST_URL, echo=False)
    pg_session = async_sessionmaker(pg_engine, expire_on_commit=False, class_=AsyncSession)
    async with pg_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    async def override_with_pg():
        async with pg_session() as session:
            yield session

    app.dependency_overrides[get_db] = override_with_pg
    try:
        yield make_client, pg_session
    finally:
        await pg_engine.dispose()


@pytest.mark.asyncio
async def test_lease_race_admits_exactly_one_owner_and_counts_each_attempt(
    purchase_env, tmp_path
):
    """
    The direct lease contract: two simultaneous acquisitions of one authorized claim's
    lease admit exactly ONE owner; attempt_count is incremented by that single statement;
    a token that does not own the lease can never release it; the owner's release admits
    exactly one further attempt, counted again.
    """
    _, _ = purchase_env
    file_engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'lease_race.db'}", echo=False
    )
    file_session = async_sessionmaker(
        file_engine, expire_on_commit=False, class_=AsyncSession
    )
    async with file_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        async with file_session() as session:
            record = PurchaseAuthorizationRecord(
                order_id="order_lease_race",
                mission_id="mis_lease_race",
                user_id="user_lease_race",
                authority_decision="approved",
                decision_reason="lease race fixture",
                status=PurchaseAuthorizationStatus.AUTHORIZED.value,
            )
            session.add(record)
            await session.commit()
            auth_id = record.authorization_id

        async with file_session() as first, file_session() as second:
            won_a, won_b = await asyncio.gather(
                missions_module._acquire_submission_lease(first, auth_id),
                missions_module._acquire_submission_lease(second, auth_id),
            )
        owners = [token for token in (won_a, won_b) if token is not None]
        assert len(owners) == 1
        owner = owners[0]

        async def row_state() -> PurchaseAuthorizationRecord:
            async with file_session() as reader:
                return (
                    await reader.execute(
                        select(PurchaseAuthorizationRecord).where(
                            PurchaseAuthorizationRecord.authorization_id == auth_id
                        )
                    )
                ).scalar_one()

        row = await row_state()
        assert row.attempt_count == 1
        assert row.submission_state == "in_flight"
        assert row.submission_owner == owner

        # A token that does not own the lease can never release it — and writes nothing.
        async with file_session() as stranger:
            await missions_module._release_submission_lease(
                stranger, auth_id, "not-the-owner", note="stranger note"
            )
            await stranger.commit()
        row = await row_state()
        assert row.submission_state == "in_flight"
        assert row.submission_owner == owner
        assert row.error_note is None

        # The real owner releases; exactly one further attempt is admitted and counted.
        async with file_session() as owner_session:
            await missions_module._release_submission_lease(
                owner_session, auth_id, owner, note="definitively not sent"
            )
            await owner_session.commit()
        row = await row_state()
        assert row.submission_state == "idle"
        assert row.submission_owner is None
        assert row.error_note == "definitively not sent"
        async with file_session() as next_attempt:
            assert (
                await missions_module._acquire_submission_lease(next_attempt, auth_id)
                is not None
            )
        row = await row_state()
        assert row.attempt_count == 2
        assert row.submission_state == "in_flight"
    finally:
        await file_engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_resubmissions_admit_one_submission_and_one_report(
    purchase_env, trigger_transport, mock_razorpay, install_mock_discovery
):
    """
    Two simultaneous resubmissions of one authorized claim: exactly one request wins the
    lease and calls the trigger; attempt_count equals the accepted attempts; and the
    settled row carries the winner's report — the loser never writes anything.
    """
    make_client, session_factory = purchase_env
    mission_id, order_id = await _purchase_ready_mission(
        make_client, session_factory, install_mock_discovery, mock_razorpay
    )
    trigger_transport["install"](lambda request: _run_lock_refusal())

    client = make_client()
    await _login(client, OWNER_EMAIL)
    try:
        seeded = (await client.post(f"/missions/{mission_id}/order/purchase")).json()
        assert seeded["status"] == PurchaseAuthorizationStatus.AUTHORIZED.value
        assert seeded["attempt_count"] == 1
        assert seeded["submission_state"] == "idle"
        assert "refused the submission" in seeded["error_note"]
    finally:
        await client.aclose()

    async def slow_confirmed(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.3)
        return trigger_response("confirmed_linked")

    trigger_transport["install"](slow_confirmed)

    async with make_client() as client_a, make_client() as client_b:
        await _login(client_a, OWNER_EMAIL)
        await _login(client_b, OWNER_EMAIL)
        barrier = asyncio.Barrier(2)

        async def fire(racer: AsyncClient) -> dict:
            await barrier.wait()
            response = await racer.post(f"/missions/{mission_id}/order/purchase")
            assert response.status_code == 200
            return response.json()

        response_a, response_b = await asyncio.gather(fire(client_a), fire(client_b))

        assert response_a["authorization_id"] == response_b["authorization_id"]
        for raced in (response_a, response_b):
            # The loser may answer while the winner is still in flight: authorized +
            # in_flight is honest; the winner's terminal record is honest. A fabricated
            # refusal or a second report is not.
            assert raced["status"] in (
                PurchaseAuthorizationStatus.AUTHORIZED.value,
                PurchaseAuthorizationStatus.PURCHASE_REPORTED_LINKED.value,
            )
        assert len(trigger_transport["calls"]) == 2  # the seed + exactly ONE racer

    async with session_factory() as session:
        row = (
            await session.execute(
                select(PurchaseAuthorizationRecord).where(
                    PurchaseAuthorizationRecord.order_id == order_id
                )
            )
        ).scalar_one()
        assert row.attempt_count == 2
        assert row.status == PurchaseAuthorizationStatus.PURCHASE_REPORTED_LINKED.value
        assert row.submission_state == "settled"
        assert row.submission_owner is None
        assert row.trigger_reported_outcome_class == "confirmed_linked"
        assert row.error_note is None  # the winner cleared it; the loser never wrote


@pytest.mark.asyncio
async def test_not_sent_then_confirmed_clears_stale_note_and_settles(
    purchase_env, trigger_transport, mock_razorpay, install_mock_discovery
):
    """A real outcome must clear a stale not_sent note and settle the lease."""
    make_client, session_factory = purchase_env
    mission_id, order_id = await _purchase_ready_mission(
        make_client, session_factory, install_mock_discovery, mock_razorpay
    )
    trigger_transport["install"](lambda request: _run_lock_refusal())

    client = make_client()
    await _login(client, OWNER_EMAIL)
    try:
        first = (await client.post(f"/missions/{mission_id}/order/purchase")).json()
        assert first["status"] == PurchaseAuthorizationStatus.AUTHORIZED.value
        assert first["error_note"]

        trigger_transport["install"](
            lambda request: trigger_response("confirmed_linked")
        )
        second = (await client.post(f"/missions/{mission_id}/order/purchase")).json()
        assert second["status"] == PurchaseAuthorizationStatus.PURCHASE_REPORTED_LINKED.value
        assert second["attempt_count"] == 2
        assert second["error_note"] is None
        assert second["trigger_report"]["outcome_class"] == "confirmed_linked"
        assert second["submission_state"] == "settled"
        assert second["last_submission_at"] is not None

        async with session_factory() as session:
            row = (
                await session.execute(
                    select(PurchaseAuthorizationRecord).where(
                        PurchaseAuthorizationRecord.order_id == order_id
                    )
                )
            ).scalar_one()
            assert row.submission_owner is None
            assert row.trigger_reported_reason == "deterministic trigger test reason"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_late_or_stale_reports_never_overwrite_a_settled_claim(purchase_env):
    """
    Report monotonicity, proven directly against _apply_trigger_report: an application
    only lands while the caller still owns an in_flight lease on an authorized claim. A
    settled reported_linked claim ignores stale and late reports of every class; a
    settled unknown claim ignores late confirmed/refused reports. The positive controls
    prove the same call applies while ownership holds.
    """
    _, session_factory = purchase_env
    async with session_factory() as session:
        linked = PurchaseAuthorizationRecord(
            order_id="order_mono_linked",
            mission_id="mis_mono",
            user_id="user_mono",
            authority_decision="approved",
            decision_reason="monotonic fixture",
            status=PurchaseAuthorizationStatus.PURCHASE_REPORTED_LINKED.value,
            attempt_count=1,
            submission_state=missions_module.SUBMISSION_SETTLED,
        )
        unknown = PurchaseAuthorizationRecord(
            order_id="order_mono_unknown",
            mission_id="mis_mono",
            user_id="user_mono",
            authority_decision="approved",
            decision_reason="monotonic fixture",
            status=PurchaseAuthorizationStatus.PURCHASE_UNKNOWN.value,
            attempt_count=1,
            submission_state=missions_module.SUBMISSION_SETTLED,
            error_note="recovery note",
        )
        session.add_all([linked, unknown])
        await session.commit()
        linked_id = linked.authorization_id
        unknown_id = unknown.authorization_id

    def report(classification: str) -> missions_module.TriggerReport:
        return missions_module.TriggerReport(
            classification=classification, reason="late probe"
        )

    async with session_factory() as session:
        row = await session.get(PurchaseAuthorizationRecord, linked_id)
        for classification in (
            missions_module.NOT_SENT,
            missions_module.CONFIRMED_LINKED,
            missions_module.REFUSED_BEFORE_CREATE,
            missions_module.REFUSED_AFTER_CREATE,
            missions_module.UNKNOWN,
        ):
            applied = await missions_module._apply_trigger_report(
                session, row, "long-gone-owner", report(classification)
            )
            assert applied is False
        assert row.status == PurchaseAuthorizationStatus.PURCHASE_REPORTED_LINKED.value
        assert row.trigger_reported_outcome_class is None
        assert row.error_note is None
        assert row.submission_state == missions_module.SUBMISSION_SETTLED

    async with session_factory() as session:
        row = await session.get(PurchaseAuthorizationRecord, unknown_id)
        for classification in (
            missions_module.CONFIRMED_LINKED,
            missions_module.REFUSED_BEFORE_CREATE,
            missions_module.NOT_SENT,
        ):
            applied = await missions_module._apply_trigger_report(
                session, row, "long-gone-owner", report(classification)
            )
            assert applied is False
        assert row.status == PurchaseAuthorizationStatus.PURCHASE_UNKNOWN.value
        assert row.error_note == "recovery note"

    async with session_factory() as session:
        pending = PurchaseAuthorizationRecord(
            order_id="order_mono_pending",
            mission_id="mis_mono",
            user_id="user_mono",
            authority_decision="approved",
            decision_reason="monotonic fixture",
            status=PurchaseAuthorizationStatus.AUTHORIZED.value,
            attempt_count=1,
            submission_state=missions_module.SUBMISSION_IN_FLIGHT,
            submission_owner="live-owner",
        )
        session.add(pending)
        await session.commit()
        pending_id = pending.authorization_id

    async with session_factory() as session:
        row = await session.get(PurchaseAuthorizationRecord, pending_id)
        # A not_sent report lands under real ownership: no status change, lease released.
        assert (
            await missions_module._apply_trigger_report(
                session, row, "live-owner", report(missions_module.NOT_SENT)
            )
            is True
        )
        assert row.status == PurchaseAuthorizationStatus.AUTHORIZED.value
        assert row.submission_state == missions_module.SUBMISSION_IDLE
        assert row.submission_owner is None
        assert row.error_note == "late probe"
        # The released claim is no longer owned: even the SAME token can no longer land.
        assert (
            await missions_module._apply_trigger_report(
                session, row, "live-owner", report(missions_module.CONFIRMED_LINKED)
            )
            is False
        )
        assert row.status == PurchaseAuthorizationStatus.AUTHORIZED.value
        assert row.error_note == "late probe"


@pytest.mark.asyncio
async def test_fresh_in_flight_claim_is_returned_never_second_submitted(
    purchase_env, trigger_transport, mock_razorpay, install_mock_discovery
):
    """
    A request that finds a FRESH in_flight lease returns the persisted record without
    submitting: no second trigger call, no state flip, no attempt counted. This is the
    crashed-after-lease-before-send window as the next explicit POST sees it.
    """
    make_client, session_factory = purchase_env
    mission_id, order_id = await _purchase_ready_mission(
        make_client, session_factory, install_mock_discovery, mock_razorpay
    )
    trigger_transport["install"](lambda request: _run_lock_refusal())

    client = make_client()
    await _login(client, OWNER_EMAIL)
    try:
        seeded = (await client.post(f"/missions/{mission_id}/order/purchase")).json()
        assert seeded["attempt_count"] == 1
        calls_after_seed = len(trigger_transport["calls"])

        async with session_factory() as session:
            row = (
                await session.execute(
                    select(PurchaseAuthorizationRecord).where(
                        PurchaseAuthorizationRecord.order_id == order_id
                    )
                )
            ).scalar_one()
            row.submission_state = "in_flight"
            row.submission_owner = "owner-crashed-before-send"
            row.last_attempt_at = datetime.utcnow()
            await session.commit()

        found = (await client.post(f"/missions/{mission_id}/order/purchase")).json()
        assert found["status"] == PurchaseAuthorizationStatus.AUTHORIZED.value
        assert found["submission_state"] == "in_flight"
        assert found["attempt_count"] == 1
        assert len(trigger_transport["calls"]) == calls_after_seed

        async with session_factory() as session:
            row = (
                await session.execute(
                    select(PurchaseAuthorizationRecord).where(
                        PurchaseAuthorizationRecord.order_id == order_id
                    )
                )
            ).scalar_one()
            assert row.status == PurchaseAuthorizationStatus.AUTHORIZED.value
            assert row.submission_state == "in_flight"
            assert row.submission_owner == "owner-crashed-before-send"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_abandoned_lease_recovers_to_purchase_unknown_never_failed(
    purchase_env, trigger_transport, mock_razorpay, install_mock_discovery
):
    """
    The stale window: a lease abandoned by a crashed backend is potentially sent —
    persisted state cannot prove otherwise — so the next explicit POST closes the claim
    as purchase_unknown: terminal, honest, never purchase_failed, never reset to idle,
    and no submission is started behind it.
    """
    make_client, session_factory = purchase_env
    mission_id, order_id = await _purchase_ready_mission(
        make_client, session_factory, install_mock_discovery, mock_razorpay
    )
    trigger_transport["install"](lambda request: _run_lock_refusal())

    client = make_client()
    await _login(client, OWNER_EMAIL)
    try:
        seeded = (await client.post(f"/missions/{mission_id}/order/purchase")).json()
        assert seeded["attempt_count"] == 1
        calls_after_seed = len(trigger_transport["calls"])

        async with session_factory() as session:
            row = (
                await session.execute(
                    select(PurchaseAuthorizationRecord).where(
                        PurchaseAuthorizationRecord.order_id == order_id
                    )
                )
            ).scalar_one()
            row.submission_state = "in_flight"
            row.submission_owner = "owner-lost-mid-flight"
            row.last_attempt_at = datetime.utcnow() - timedelta(
                seconds=missions_module._LEASE_STALE_AFTER_SECONDS + 30
            )
            await session.commit()

        recovered = (await client.post(f"/missions/{mission_id}/order/purchase")).json()
        assert recovered["status"] == PurchaseAuthorizationStatus.PURCHASE_UNKNOWN.value
        assert recovered["status"] != PurchaseAuthorizationStatus.PURCHASE_FAILED.value
        assert recovered["attempt_count"] == 1  # recovery counts no attempt
        assert recovered["submission_state"] == "settled"
        assert "unknown" in recovered["error_note"].lower()
        assert len(trigger_transport["calls"]) == calls_after_seed  # never submits

        async with session_factory() as session:
            row = (
                await session.execute(
                    select(PurchaseAuthorizationRecord).where(
                        PurchaseAuthorizationRecord.order_id == order_id
                    )
                )
            ).scalar_one()
            assert row.submission_owner is None
            note = missions_module._purchase_submission_note(row)
            assert "unknown" in note.lower()
            assert "no merchant order exists" not in note

        # Terminal: a further POST neither resubmits nor flips anything.
        again = (await client.post(f"/missions/{mission_id}/order/purchase")).json()
        assert again["attempt_count"] == 1
        assert again["status"] == PurchaseAuthorizationStatus.PURCHASE_UNKNOWN.value
        assert len(trigger_transport["calls"]) == calls_after_seed
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_pg_lease_race_admits_one_submission(
    pg_purchase_env, trigger_transport, mock_razorpay, install_mock_discovery
):
    """The single-owner proof again under real PostgreSQL row locks."""
    make_client, pg_session = pg_purchase_env
    mission_id, order_id = await _purchase_ready_mission(
        make_client, pg_session, install_mock_discovery, mock_razorpay
    )
    trigger_transport["install"](lambda request: _run_lock_refusal())

    client = make_client()
    await _login(client, OWNER_EMAIL)
    try:
        seeded = (await client.post(f"/missions/{mission_id}/order/purchase")).json()
        assert seeded["attempt_count"] == 1
    finally:
        await client.aclose()

    trigger_transport["install"](lambda request: trigger_response("confirmed_linked"))

    async with make_client() as client_a, make_client() as client_b:
        await _login(client_a, OWNER_EMAIL)
        await _login(client_b, OWNER_EMAIL)
        barrier = asyncio.Barrier(2)

        async def fire(racer: AsyncClient) -> dict:
            await barrier.wait()
            response = await racer.post(f"/missions/{mission_id}/order/purchase")
            assert response.status_code == 200
            return response.json()

        await asyncio.gather(fire(client_a), fire(client_b))
        assert len(trigger_transport["calls"]) == 2  # the seed + exactly ONE racer

    async with pg_session() as session:
        row = (
            await session.execute(
                select(PurchaseAuthorizationRecord).where(
                    PurchaseAuthorizationRecord.order_id == order_id
                )
            )
        ).scalar_one()
        assert row.attempt_count == 2
        assert row.status == PurchaseAuthorizationStatus.PURCHASE_REPORTED_LINKED.value
        assert row.submission_state == "settled"
        assert row.submission_owner is None


# --------------------------------------------------------------------------- #
# AGENT TOOL — read-only eligibility
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_check_purchase_eligibility_tool_is_read_only(
    purchase_env, trigger_transport, mock_razorpay, install_mock_discovery, monkeypatch
):
    from app.agents.tools import execute_tool

    make_client, session_factory = purchase_env
    mission_id, _ = await _purchase_ready_mission(
        make_client, session_factory, install_mock_discovery, mock_razorpay
    )
    trigger_transport["install"](lambda request: trigger_response("confirmed_linked"))

    async with session_factory() as session:
        record = await session.get(missions_module.MissionRecord, mission_id)
        result = await execute_tool(
            "check_purchase_eligibility",
            {},
            mission_id=mission_id,
            record=record,
            current_user=None,
            db=session,
            reasoning=None,
        )
        assert result.success
        assert result.result["eligible"] is True
        assert result.result["purchase_authorization_status"] is None
        # Strictly read-only: no authorization row was created and nothing was submitted
        await session.rollback()
        count = await session.scalar(select(func.count()).select_from(PurchaseAuthorizationRecord))
        assert count == 0
    assert len(trigger_transport["calls"]) == 0

    # Ineligible variant: switch off -> not eligible, same read-only guarantees
    monkeypatch.setattr(
        get_settings(), "shopping_purchase_authority_enabled", False, raising=False
    )
    async with session_factory() as session:
        record = await session.get(missions_module.MissionRecord, mission_id)
        result = await execute_tool(
            "check_purchase_eligibility",
            {},
            mission_id=mission_id,
            record=record,
            current_user=None,
            db=session,
            reasoning=None,
        )
        assert result.result["eligible"] is False
        assert "not configured" in result.result["reason"]
    assert len(trigger_transport["calls"]) == 0


# --------------------------------------------------------------------------- #
# ISOLATION
# --------------------------------------------------------------------------- #
def test_no_merchant_channel_imports_in_backend():
    backend_app = Path(__file__).resolve().parents[1] / "app"
    offenders = []
    import_statement = re.compile(
        r"^\s*(?:from\s+merchant_channel[\s.]|import\s+merchant_channel)", re.MULTILINE
    )
    for path in backend_app.rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        if import_statement.search(text):
            offenders.append(str(path))
    assert offenders == []
