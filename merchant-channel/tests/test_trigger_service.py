"""
Hermetic tests for the standalone trigger service.

Every test runs the real purchase path — the real ``fetch_order_facts`` against a fake httpx
module patched exactly where Phase 1 proved it must be patched (``merchant_channel.gates``), the
real gates, the real correlation, the real outbox, the real ``PurchaseChannel.fulfil`` — with only
the Shopify gateway and the linkage receiver replaced by stubs. No test opens a socket, contacts
Shopify, or contacts a real RESOLVE API: the two RESOLVE endpoints are canned payloads inside the
fake transport, and the trigger's environment is scrubbed and rebuilt per test from disposable
values.
"""
from __future__ import annotations

import json
import os
import sqlite3
import types

import pytest
from fastapi.testclient import TestClient

from merchant_channel import gates as gate_module
from merchant_channel.channel import (
    CONFIRMED_LINKED,
    OUTCOME_UNKNOWN,
    REFUSED_IDENTITY,
    REFUSED_LINKAGE,
)
from merchant_channel.config import RESOLVE_OWNED_ENV
from merchant_channel.correlation import correlation_id as derive_correlation
from merchant_channel.gateway import GatewayDefiniteFailure, GatewayOutcomeUnknown
from merchant_channel.outbox import (
    AMBIGUOUS,
    LINKAGE_DELIVERY_UNKNOWN,
    LINKAGE_REFUSED,
    LINKAGE_SENT,
    MATCHED,
    Outbox,
)
from merchant_channel.documents import MerchantOrder, OrderLine
from merchant_channel import trigger_service
from merchant_channel.trigger_service import create_trigger_app
import scratch_purchase as runner

# --------------------------------------------------------------- disposable values

# Disposable values for this process only: never credentials, never printed as configuration.
CHANNEL_ENV = {
    "MERCHANT_SHOPIFY_STORE_DOMAIN": "resolve-merchant-test.myshopify.com",
    "MERCHANT_SHOPIFY_API_VERSION": "2026-07",
    "MERCHANT_SHOPIFY_ACCESS_TOKEN": "shpua_disposable-trigger-test-token",
    "MERCHANT_LINKAGE_SECRET": "disposable-trigger-test-linkage-secret",
    "MERCHANT_RESOLVE_BASE_URL": "http://host.docker.internal:8000",
    "MERCHANT_EXPECTED_CURRENCY": "INR",
    "MERCHANT_EXPECTED_SHOP_NAME": "RESOLVE Merchant Test",
    "MERCHANT_RESOLVE_SERVICE_TOKEN": "disposable-trigger-test-machine-token",
    "MERCHANT_CHANNEL_SOURCE": "merchant-purchase-trigger",
}
TRIGGER_TOKEN = "disposable-trigger-test-caller-token"
STORE_DOMAIN = CHANNEL_ENV["MERCHANT_SHOPIFY_STORE_DOMAIN"]

MISSION_ID = "mission_trigger0001"
ORDER_ID = "order_a1b2c3d4e5f6"
PRODUCT_GID = "gid://shopify/Product/15408674210109"
VARIANT_GID = "gid://shopify/ProductVariant/67625768649021"
MERCHANT_ORDER_GID = "gid://shopify/Order/9000000000000042"

SECRET_VALUES = (
    TRIGGER_TOKEN,
    CHANNEL_ENV["MERCHANT_SHOPIFY_ACCESS_TOKEN"],
    CHANNEL_ENV["MERCHANT_LINKAGE_SECRET"],
    CHANNEL_ENV["MERCHANT_RESOLVE_SERVICE_TOKEN"],
)


def set_env(monkeypatch, tmp_path, **overrides) -> dict:
    env = dict(CHANNEL_ENV)
    env.update(overrides)
    env["MERCHANT_TRIGGER_TOKEN"] = TRIGGER_TOKEN
    env["MERCHANT_TRIGGER_OUTBOX"] = str(tmp_path / "outbox.sqlite3")
    env.setdefault("MERCHANT_EXPECTED_STORE_DOMAIN", STORE_DOMAIN)
    for name in list(os.environ):
        if name.startswith("MERCHANT_") or name in RESOLVE_OWNED_ENV:
            monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return env


ORDER_PAYLOAD = {
    "order_id": ORDER_ID,
    "mission_id": MISSION_ID,
    "status": "order_created",
    "product": {
        "product_id": "prod_trigger",
        "title": "Trigger Item",
        "merchant": "RESOLVE Merchant Test",
    },
    "amount": "1299.00",
    "currency": "INR",
    "razorpay_order_id": "order_testdisposable02",
    "razorpay_payment_id": "pay_testdisposable02",
    "created_at": "2026-11-06T00:00:00",
    "updated_at": "2026-11-06T00:00:00",
    "merchant_order_linked": False,
    "merchant_order_linkage": "unlinked",
    "test_mode": True,
    "fulfillment_note": "no merchant order, shipping, or delivery exists",
    "agent_message": None,
}

CHECKOUT_PAYLOAD = {
    "mission_id": MISSION_ID,
    "checkout": {"currency": "INR", "subtotal": "1299.00"},
    "confirmed": True,
    "mission_status": "order_created",
    "shopping_intent": {
        "state": "checkout_review",
        "selected_product": {
            "product_id": "prod_trigger",
            "title": "Trigger Item",
            "price": "1299.00",
            "currency": "INR",
            "merchant": "RESOLVE Merchant Test",
            "source": "shopify",
            "shopify_product_gid": PRODUCT_GID,
            "shopify_variant_gid": VARIANT_GID,
            "shopify_store_domain": STORE_DOMAIN,
            "requires_variant_clarification": False,
            "variant_clarification": None,
        },
    },
    "total_recovery_spend": "0",
}


def _copy(payload: dict) -> dict:
    return json.loads(json.dumps(payload))


def _mutated_order(**changes) -> dict:
    payload = _copy(ORDER_PAYLOAD)
    for key, value in changes.items():
        if value is ...:
            payload.pop(key, None)
        else:
            payload[key] = value
    return payload


def _mutated_selection(**changes) -> dict:
    payload = _copy(CHECKOUT_PAYLOAD)
    selected = payload["shopping_intent"]["selected_product"]
    for key, value in changes.items():
        if value is ...:
            selected.pop(key, None)
        else:
            selected[key] = value
    return payload


class _Response:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _FakeHTTPError(Exception):
    pass


def make_fake_httpx(order_result, checkout_result):
    """
    A stand-in for the httpx module with exactly the surface `fetch_order_facts` uses, routing
    the two real URL shapes to canned results: (status, payload) tuples, or an exception to
    raise. No socket is opened anywhere.
    """

    class _Client:
        requests: list = []

        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return False

        async def get(self, url, headers=None):
            _Client.requests.append((url, dict(headers or {})))
            if url.endswith("/order"):
                result = order_result
            elif url.endswith("/shopping/checkout"):
                result = checkout_result
            else:
                raise AssertionError(f"unexpected URL requested: {url}")
            if isinstance(result, Exception):
                raise result
            status, payload = result
            return _Response(status, payload)

    return types.SimpleNamespace(
        Timeout=lambda *args, **kwargs: None,
        HTTPError=_FakeHTTPError,
        AsyncClient=_Client,
    )


class GatewayStub:
    """Counts every create; answers with one consistent order so the happy path can complete."""

    def __init__(
        self,
        *,
        create_raises: Exception | None = None,
        shop: tuple[str | None, str | None] = ("RESOLVE Merchant Test", "INR"),
    ) -> None:
        self.create_calls = 0
        self.create_quantities: list[int] = []
        self.create_variant_gids: list[str] = []
        self.create_raises = create_raises
        self.shop = shop
        self._created: MerchantOrder | None = None

    def _order(self, correlation_id: str, quantity: int) -> MerchantOrder:
        return MerchantOrder(
            order_id=MERCHANT_ORDER_GID,
            name="#TG1",
            po_number=correlation_id,
            currency="INR",
            source_name="trigger",
            financial_status="PENDING",
            created_at=None,
            lines=(
                OrderLine(
                    line_item_id="gid://shopify/LineItem/2",
                    product_id=PRODUCT_GID,
                    variant_id=VARIANT_GID,
                    quantity=quantity,
                ),
            ),
        )

    async def create_order(self, *, correlation_id: str, variant_gid: str, quantity: int):
        self.create_calls += 1
        self.create_quantities.append(quantity)
        self.create_variant_gids.append(variant_gid)
        if self.create_raises is not None:
            raise self.create_raises
        self._created = self._order(correlation_id, quantity)
        return self._created

    async def read_back(self, correlation_id: str) -> list[MerchantOrder]:
        return [self._created] if self._created is not None else []

    async def shop_identity(self) -> tuple[str | None, str | None]:
        return self.shop


class ReceiverStub:
    """The receiver double: records what it was handed, answers with a scripted status."""

    def __init__(self, *, status_code: int = 200, payload: dict | None = None) -> None:
        self.status_code = status_code
        self.payload = payload if payload is not None else {"outcome": "linked"}
        self.requests: list[dict] = []

    async def post(self, url, *, content=None, headers=None, **kwargs):
        self.requests.append({"url": url, "content": content, "headers": headers})
        return _Response(self.status_code, self.payload)


def make_app(
    monkeypatch,
    tmp_path,
    *,
    order_result=None,
    checkout_result=None,
    gateway=None,
    env_overrides=None,
):
    set_env(monkeypatch, tmp_path, **(env_overrides or {}))
    monkeypatch.setattr(
        gate_module,
        "httpx",
        make_fake_httpx(
            order_result if order_result is not None else (200, _copy(ORDER_PAYLOAD)),
            checkout_result if checkout_result is not None else (200, _copy(CHECKOUT_PAYLOAD)),
        ),
    )
    gateway = gateway if gateway is not None else GatewayStub()
    receiver = ReceiverStub()
    app = create_trigger_app(gateway=gateway, http_client=receiver)
    return app, gateway, receiver


def post(client: TestClient, body=None, *, token=TRIGGER_TOKEN, headers=None, content=None):
    sent_headers = dict(headers or {})
    if token is not None:
        sent_headers["X-Merchant-Trigger-Token"] = token
    if content is None:
        content = json.dumps(body).encode("utf-8") if body is not None else b""
    return client.post("/purchase", content=content, headers=sent_headers)


def default_body(**overrides) -> dict:
    body = {"mission_id": MISSION_ID, "resolve_order_id": ORDER_ID}
    body.update(overrides)
    return body


def seed_outbox_row(tmp_path, *, state: str) -> str:
    """
    Leave one outbox row in the named state, under the correlation the trigger itself will
    derive for the default request body. Every row is reached through the real Outbox API in
    the order the channel itself reaches them — no state is written directly — so each seed is
    a state the production outbox can genuinely hold.

    Returns the correlation id of the seeded row.
    """
    handoff = runner.handoff_from_facts(_facts_for_seeding(), quantity=1)
    correlation = derive_correlation(handoff)
    outbox = Outbox(tmp_path / "outbox.sqlite3")
    outbox.reserve(
        correlation_id=correlation,
        resolve_order_id=ORDER_ID,
        payload_hash=runner.derive_payload_hash(handoff),
    )
    if state == "NOT_STARTED":
        return correlation
    outbox.begin_create(correlation)
    if state == "PENDING":
        # The crash-window shape: a run decided to send and settlement never happened, so the
        # request may or may not have left.
        outbox.mark_request_sent(correlation)
        return correlation
    if state == "OUTCOME_UNKNOWN":
        outbox.mark_outcome_unknown(correlation, "seeded: the create answer was lost")
    elif state == "AMBIGUOUS":
        outbox.mark_reconciled(correlation, AMBIGUOUS)
    elif state == "FAILED":
        outbox.mark_failed(correlation, "seeded: Shopify answered HTTP 422")
    elif state == "CONFIRMED":
        outbox.mark_confirmed(correlation, MERCHANT_ORDER_GID, event_id="evt_seed_confirmed")
    elif state == "LINKAGE_SENT":
        outbox.mark_confirmed(correlation, MERCHANT_ORDER_GID, event_id="evt_seed_sent")
        outbox.set_linkage(correlation, LINKAGE_SENT, event_id="evt_seed_sent", event_body="{}")
    elif state == "LINKAGE_DELIVERY_UNKNOWN":
        outbox.mark_confirmed(correlation, MERCHANT_ORDER_GID, event_id="evt_seed_delivery")
        outbox.set_linkage(
            correlation,
            LINKAGE_DELIVERY_UNKNOWN,
            failure_reason="seeded: the linkage claim was never answered",
        )
    elif state == "LINKAGE_REFUSED":
        outbox.mark_confirmed(correlation, MERCHANT_ORDER_GID, event_id="evt_seed_refused")
        outbox.set_linkage(
            correlation,
            LINKAGE_REFUSED,
            event_id="evt_seed_refused",
            event_body="{}",
            failure_reason="seeded: RESOLVE settled a refusal for this pairing",
        )
    elif state == "RECONCILED_MATCHED":
        outbox.mark_reconciled(correlation, MATCHED)
    else:
        raise ValueError(f"not a seedable outbox state: {state!r}")
    return correlation


def _facts_for_seeding():
    return gate_module.OrderFacts(
        resolve_order_id=ORDER_ID,
        mission_id=MISSION_ID,
        status="order_created",
        currency="INR",
        razorpay_order_id="order_testdisposable02",
        razorpay_payment_id="pay_testdisposable02",
        merchant_order_linked=False,
        product_gid=PRODUCT_GID,
        variant_gid=VARIANT_GID,
        store_domain=STORE_DOMAIN,
        order_product_id="prod_trigger",
        selected_product_id="prod_trigger",
        title="Trigger Item",
    )


def assert_no_secrets(response) -> None:
    body = response.text
    for secret in SECRET_VALUES:
        assert secret not in body


# --------------------------------------------------------------- 1. the golden path

def test_valid_authenticated_request_maps_confirmed_outcome_verbatim(monkeypatch, tmp_path):
    app, gateway, receiver = make_app(monkeypatch, tmp_path)
    with TestClient(app) as client:
        response = post(client, default_body())
    assert response.status_code == 200
    body = response.json()
    assert body["outcome_class"] == "confirmed_linked"
    assert body["outcome_status"] == CONFIRMED_LINKED
    assert body["correlation_id"] and body["correlation_id"].startswith("RESOLVE-")
    assert body["external_order_id"] == MERCHANT_ORDER_GID
    assert body["event_id"]
    assert gateway.create_calls == 1
    assert gateway.create_quantities == [1]
    assert gateway.create_variant_gids == [VARIANT_GID]
    assert len(receiver.requests) == 1
    assert_no_secrets(response)


# --------------------------------------------------------------- 2. authentication

def test_missing_token_header_is_an_authentication_failure(monkeypatch, tmp_path):
    app, gateway, _ = make_app(monkeypatch, tmp_path)
    with TestClient(app) as client:
        response = post(client, default_body(), token=None)
    assert response.status_code == 401
    assert response.json()["outcome_class"] == "authentication_failure"
    assert gateway.create_calls == 0


def test_wrong_token_is_an_authentication_failure_and_is_never_echoed(monkeypatch, tmp_path):
    app, gateway, _ = make_app(monkeypatch, tmp_path)
    with TestClient(app) as client:
        response = post(client, default_body(), token="totally-wrong-token")
    assert response.status_code == 401
    body = response.json()
    assert body["outcome_class"] == "authentication_failure"
    assert "totally-wrong-token" not in response.text
    assert_no_secrets(response)
    assert gateway.create_calls == 0


def test_unset_trigger_token_env_fails_every_request_closed(monkeypatch, tmp_path):
    app, gateway, _ = make_app(monkeypatch, tmp_path)
    monkeypatch.delenv("MERCHANT_TRIGGER_TOKEN")
    with TestClient(app) as client:
        response = post(client, default_body(), token=TRIGGER_TOKEN)
    assert response.status_code == 401
    assert gateway.create_calls == 0


# --------------------------------------------------------------- 3. request shape

def test_extra_fields_are_rejected_without_execution(monkeypatch, tmp_path):
    app, gateway, _ = make_app(monkeypatch, tmp_path)
    body = default_body(quantity=99, variant_gid="gid://shopify/ProductVariant/999")
    with TestClient(app) as client:
        response = post(client, body)
    assert response.status_code == 422
    payload = response.json()
    assert payload["outcome_class"] == "invalid_request"
    assert any("extra" in problem for problem in payload["problems"])
    # The caller's values are never echoed back into the service's responses.
    assert "99" not in response.text
    assert "gid://shopify/ProductVariant/999" not in response.text
    assert gateway.create_calls == 0


def test_missing_identifiers_are_rejected(monkeypatch, tmp_path):
    app, gateway, _ = make_app(monkeypatch, tmp_path)
    with TestClient(app) as client:
        empty = post(client, {})
        no_order = post(client, {"mission_id": MISSION_ID})
        no_mission = post(client, {"resolve_order_id": ORDER_ID})
        garbage = post(client, None, content=b"not json at all")
    for response in (empty, no_order, no_mission, garbage):
        assert response.status_code == 422
        assert response.json()["outcome_class"] == "invalid_request"
    assert any("mission_id" in problem for problem in empty.json()["problems"])
    assert any("resolve_order_id" in problem for problem in empty.json()["problems"])
    assert gateway.create_calls == 0


def test_malformed_identifiers_are_rejected(monkeypatch, tmp_path):
    app, gateway, _ = make_app(monkeypatch, tmp_path)
    cases = [
        {"mission_id": MISSION_ID, "resolve_order_id": "purchase_a1b2c3d4e5f6"},
        {"mission_id": MISSION_ID, "resolve_order_id": "order_a1b2c3d4e5f"},
        {"mission_id": MISSION_ID, "resolve_order_id": "order_A1B2C3D4E5F6"},
        {"mission_id": "", "resolve_order_id": ORDER_ID},
        {"mission_id": "   ", "resolve_order_id": ORDER_ID},
        {"mission_id": "x" * 200, "resolve_order_id": ORDER_ID},
        {"mission_id": 7, "resolve_order_id": ORDER_ID},
        {"mission_id": MISSION_ID, "resolve_order_id": 7},
    ]
    with TestClient(app) as client:
        responses = [post(client, body) for body in cases]
    for response in responses:
        assert response.status_code == 422
        assert response.json()["outcome_class"] == "invalid_request"
    assert gateway.create_calls == 0


def test_a_well_formed_but_unserved_order_id_cannot_influence_execution(monkeypatch, tmp_path):
    app, gateway, _ = make_app(monkeypatch, tmp_path)
    with TestClient(app) as client:
        response = post(client, default_body(resolve_order_id="order_ffffffffffff"))
    assert response.status_code == 200
    body = response.json()
    # No inspectable outbox means no proof that a prior attempt for this order does not
    # exist, so the honest answer is unknown. Execution is still refused either way.
    assert body["outcome_class"] == "unknown_requires_reconciliation"
    assert "cannot be ruled out" in body["reason"]
    assert gateway.create_calls == 0


# --------------------------------------------------------------- 4. the RESOLVE read

def test_resolve_auth_failure_refuses_without_contacting_shopify(monkeypatch, tmp_path):
    app, gateway, _ = make_app(
        monkeypatch, tmp_path, order_result=(401, {"detail": "invalid service token"})
    )
    with TestClient(app) as client:
        response = post(client, default_body())
    assert response.status_code == 200
    body = response.json()
    assert body["outcome_class"] == "unknown_requires_reconciliation"
    assert any("401" in check_or_problem for check_or_problem in body["problems"])
    assert "no merchant order" not in body["reason"]
    assert gateway.create_calls == 0


def test_resolve_timeout_refuses_without_contacting_shopify(monkeypatch, tmp_path):
    app, gateway, _ = make_app(
        monkeypatch,
        tmp_path,
        order_result=_FakeHTTPError("timed out while reading the order"),
    )
    with TestClient(app) as client:
        response = post(client, default_body())
    assert response.status_code == 200
    body = response.json()
    assert body["outcome_class"] == "unknown_requires_reconciliation"
    assert "no merchant order" not in body["reason"]
    assert gateway.create_calls == 0


def test_malformed_resolve_response_refuses_without_contacting_shopify(monkeypatch, tmp_path):
    app, gateway, _ = make_app(
        monkeypatch, tmp_path, order_result=(200, {"unexpected": "shape"}), checkout_result=(200, {})
    )
    with TestClient(app) as client:
        response = post(client, default_body())
    assert response.status_code == 200
    body = response.json()
    assert body["outcome_class"] == "unknown_requires_reconciliation"
    assert gateway.create_calls == 0


# --------------------------------------------------------------- 5. the order gates

def test_missing_payment_evidence_refuses_before_create(monkeypatch, tmp_path):
    app, gateway, _ = make_app(
        monkeypatch, tmp_path, order_result=(200, _mutated_order(razorpay_payment_id=...))
    )
    with TestClient(app) as client:
        response = post(client, default_body())
    assert response.status_code == 200
    body = response.json()
    assert body["outcome_class"] == "unknown_requires_reconciliation"
    assert any("payment" in problem.lower() for problem in body["problems"])
    assert gateway.create_calls == 0


def test_missing_shopify_identity_refuses_before_create(monkeypatch, tmp_path):
    app, gateway, _ = make_app(
        monkeypatch, tmp_path, checkout_result=(200, _mutated_selection(shopify_variant_gid=...))
    )
    with TestClient(app) as client:
        response = post(client, default_body())
    assert response.status_code == 200
    body = response.json()
    assert body["outcome_class"] == "unknown_requires_reconciliation"
    assert gateway.create_calls == 0


def test_a_store_other_than_the_configured_pin_refuses(monkeypatch, tmp_path):
    app, gateway, _ = make_app(
        monkeypatch,
        tmp_path,
        checkout_result=(
            200,
            _mutated_selection(shopify_store_domain="someone-elses-store.myshopify.com"),
        ),
    )
    with TestClient(app) as client:
        response = post(client, default_body())
    assert response.status_code == 200
    body = response.json()
    assert body["outcome_class"] == "unknown_requires_reconciliation"
    assert gateway.create_calls == 0


def test_a_currency_other_than_the_configured_pin_refuses(monkeypatch, tmp_path):
    app, gateway, _ = make_app(
        monkeypatch,
        tmp_path,
        order_result=(200, _mutated_order(currency="USD")),
        checkout_result=(200, _mutated_selection(currency="USD")),
    )
    with TestClient(app) as client:
        response = post(client, default_body())
    assert response.status_code == 200
    body = response.json()
    assert body["outcome_class"] == "unknown_requires_reconciliation"
    assert gateway.create_calls == 0


def test_trigger_store_pin_diverging_from_the_channel_store_refuses(monkeypatch, tmp_path):
    app, gateway, _ = make_app(
        monkeypatch,
        tmp_path,
        env_overrides={"MERCHANT_EXPECTED_STORE_DOMAIN": "another-store.myshopify.com"},
    )
    with TestClient(app) as client:
        response = post(client, default_body())
    assert response.status_code == 200
    body = response.json()
    assert body["outcome_class"] == "unknown_requires_reconciliation"
    assert any("another-store.myshopify.com" in problem for problem in body["problems"])
    assert gateway.create_calls == 0


def test_unset_trigger_outbox_refuses_before_anything_runs(monkeypatch, tmp_path):
    app, gateway, _ = make_app(monkeypatch, tmp_path)
    monkeypatch.delenv("MERCHANT_TRIGGER_OUTBOX")
    with TestClient(app) as client:
        response = post(client, default_body())
    assert response.status_code == 200
    body = response.json()
    assert body["outcome_class"] == "unknown_requires_reconciliation"
    assert "cannot be ruled out" in body["reason"]
    assert gateway.create_calls == 0


# --------------------------------------------------------------- 6. the outbox and the lock

def test_a_confirmed_row_refuses_a_second_purchase(monkeypatch, tmp_path):
    app, gateway, _ = make_app(monkeypatch, tmp_path)
    with TestClient(app) as client:
        first = post(client, default_body())
        second = post(client, default_body())
    assert first.status_code == 200
    assert first.json()["outcome_class"] == "confirmed_linked"
    assert second.status_code == 200
    body = second.json()
    # The delivered row records a verified create: the honest refusal says the order exists,
    # not that nothing happened.
    assert body["outcome_class"] == "refused_after_create"
    assert gateway.create_calls == 1, "a confirmed purchase must never be created twice"


def test_an_ambiguous_outbox_row_never_creates(monkeypatch, tmp_path):
    app, gateway, _ = make_app(monkeypatch, tmp_path)
    seed_outbox_row(tmp_path, state="AMBIGUOUS")
    with TestClient(app) as client:
        response = post(client, default_body())
    assert response.status_code == 200
    body = response.json()
    assert body["outcome_class"] == "unknown_requires_reconciliation"
    assert gateway.create_calls == 0


def test_a_held_run_lock_answers_409_and_never_executes(monkeypatch, tmp_path):
    app, gateway, _ = make_app(monkeypatch, tmp_path)
    lock, problem = runner.acquire_run_lock(tmp_path / "outbox.sqlite3", mission_id=MISSION_ID)
    assert lock is not None and problem is None
    try:
        with TestClient(app) as client:
            response = post(client, default_body())
        assert response.status_code == 409
        body = response.json()
        assert body["outcome_class"] == "concurrent_execution"
        assert gateway.create_calls == 0
    finally:
        runner.release_run_lock(lock)
    assert not (tmp_path / ".scratch-purchase.lock").exists()


def test_the_trigger_releases_the_lock_after_every_run(monkeypatch, tmp_path):
    app, gateway, _ = make_app(monkeypatch, tmp_path)
    with TestClient(app) as client:
        post(client, default_body())
    assert not (tmp_path / ".scratch-purchase.lock").exists()
    lock, problem = runner.acquire_run_lock(tmp_path / "outbox.sqlite3", mission_id=MISSION_ID)
    assert lock is not None and problem is None
    runner.release_run_lock(lock)


# --------------------------------------------------------------- 7. the outcome mapping

def test_a_definite_gateway_failure_maps_to_refused_before_create(monkeypatch, tmp_path):
    app, gateway, _ = make_app(
        monkeypatch,
        tmp_path,
        gateway=GatewayStub(create_raises=GatewayDefiniteFailure("Shopify answered HTTP 422")),
    )
    with TestClient(app) as client:
        response = post(client, default_body())
    assert response.status_code == 200
    body = response.json()
    assert body["outcome_class"] == "refused_before_create"
    assert body["outcome_status"] == "FAILED"
    assert gateway.create_calls == 1


def test_refused_identity_maps_to_refused_after_create(monkeypatch, tmp_path):
    app, gateway, _ = make_app(
        monkeypatch,
        tmp_path,
        gateway=GatewayStub(shop=("A Different Shopfront", "INR")),
    )
    with TestClient(app) as client:
        response = post(client, default_body())
    assert response.status_code == 200
    body = response.json()
    assert body["outcome_class"] == "refused_after_create"
    assert body["outcome_status"] == REFUSED_IDENTITY
    assert body["external_order_id"] == MERCHANT_ORDER_GID


def test_refused_linkage_maps_to_refused_after_create(monkeypatch, tmp_path):
    app, gateway, receiver = make_app(
        monkeypatch, tmp_path, gateway=GatewayStub()
    )
    receiver.status_code = 403
    receiver.payload = {"outcome": "refused"}
    with TestClient(app) as client:
        response = post(client, default_body())
    assert response.status_code == 200
    body = response.json()
    assert body["outcome_class"] == "refused_after_create"
    assert body["outcome_status"] == REFUSED_LINKAGE
    assert body["external_order_id"] == MERCHANT_ORDER_GID


def test_unknown_outcome_stays_unknown_and_never_answers_5xx(monkeypatch, tmp_path):
    app, gateway, _ = make_app(
        monkeypatch,
        tmp_path,
        gateway=GatewayStub(create_raises=GatewayOutcomeUnknown("the answer was lost")),
    )
    with TestClient(app) as client:
        response = post(client, default_body())
    assert response.status_code == 200
    body = response.json()
    assert body["outcome_class"] == "unknown_requires_reconciliation"
    assert body["outcome_status"] == OUTCOME_UNKNOWN
    assert gateway.create_calls == 1


def test_an_unknown_outcome_is_never_retried_blind(monkeypatch, tmp_path):
    app, gateway, _ = make_app(
        monkeypatch,
        tmp_path,
        gateway=GatewayStub(create_raises=GatewayOutcomeUnknown("the answer was lost")),
    )
    with TestClient(app) as client:
        first = post(client, default_body())
        second = post(client, default_body())
    assert first.json()["outcome_class"] == "unknown_requires_reconciliation"
    assert second.status_code == 200
    body = second.json()
    # The second request hits the OUTCOME_UNKNOWN row: whether the first create happened is
    # still unknown, so the retry is refused as unknown, never as "nothing was created".
    assert body["outcome_class"] == "unknown_requires_reconciliation"
    assert gateway.create_calls == 1, "the second request must not create again"


def test_an_unrecognized_outcome_status_classifies_as_unknown(monkeypatch, tmp_path):
    # A status this module has never heard of must degrade to "requires reconciliation",
    # never to confirmed and never to a refusal that would invite a retry.
    assert trigger_service._CLASS_BY_OUTCOME.get("SOMETHING_NEW", trigger_service.UNKNOWN_CLASS) == (
        trigger_service.UNKNOWN_CLASS
    )


# ------------------------------------------- 8. every outbox state, one honest answer

OUTBOX_STATE_MATRIX = [
    # (seeded state, expected outcome_class, permitted creates)
    # A row that records no prior attempt, or a definitive failure (no order exists),
    # licenses a new create and reports it honestly as confirmed.
    ("NOT_STARTED", "confirmed_linked", 1),
    ("FAILED", "confirmed_linked", 1),
    # A prior attempt whose outcome is unresolved: whether an order exists is unknown.
    ("PENDING", "unknown_requires_reconciliation", 0),
    ("OUTCOME_UNKNOWN", "unknown_requires_reconciliation", 0),
    ("AMBIGUOUS", "unknown_requires_reconciliation", 0),
    # A row that records a verified create: the order exists on the merchant side.
    ("CONFIRMED", "refused_after_create", 0),
    ("LINKAGE_SENT", "refused_after_create", 0),
    ("LINKAGE_DELIVERY_UNKNOWN", "refused_after_create", 0),
    ("LINKAGE_REFUSED", "refused_after_create", 0),
    ("RECONCILED_MATCHED", "refused_after_create", 0),
]


@pytest.mark.parametrize("state,expected_class,expected_creates", OUTBOX_STATE_MATRIX)
def test_each_outbox_state_answers_with_the_honest_outcome_class(
    monkeypatch, tmp_path_factory, state, expected_class, expected_creates
):
    tmp = tmp_path_factory.mktemp(f"outbox-{state.lower()}")
    app, gateway, _ = make_app(monkeypatch, tmp)
    seed_outbox_row(tmp, state=state)
    with TestClient(app) as client:
        response = post(client, default_body())
    assert response.status_code == 200, state
    body = response.json()
    assert body["outcome_class"] == expected_class, state
    assert gateway.create_calls == expected_creates, state
    if expected_creates == 0:
        # Nothing executed, so there is no channel outcome status to relay.
        assert body["outcome_status"] is None, state
    assert_no_secrets(response)


def test_a_row_for_a_different_purchase_is_unknown_not_refused(monkeypatch, tmp_path):
    # The correlation-collision guard fires when the row under this correlation names a
    # different payload. Nothing can be established about THIS purchase's attempts, so the
    # honest answer is unknown — never a refusal that implies no order exists.
    app, gateway, _ = make_app(monkeypatch, tmp_path)
    seed_outbox_row(tmp_path, state="NOT_STARTED")
    connection = sqlite3.connect(tmp_path / "outbox.sqlite3")
    try:
        connection.execute("UPDATE purchase_outbox SET payload_hash = 'foreign-payload-hash'")
        connection.commit()
    finally:
        connection.close()
    with TestClient(app) as client:
        response = post(client, default_body())
    assert response.status_code == 200
    body = response.json()
    assert body["outcome_class"] == "unknown_requires_reconciliation"
    assert gateway.create_calls == 0


def test_a_pending_row_reports_unknown_and_never_claims_the_order_is_absent(monkeypatch, tmp_path):
    # The exact crash-window report: a run that decided to send and never settled. Every word
    # of the reason must stay inside what is known — nothing may claim the merchant order
    # does not exist.
    app, gateway, _ = make_app(monkeypatch, tmp_path)
    seed_outbox_row(tmp_path, state="PENDING")
    with TestClient(app) as client:
        response = post(client, default_body())
    body = response.json()
    assert body["outcome_class"] == "unknown_requires_reconciliation"
    assert "unknown" in body["reason"]
    assert "no merchant order" not in body["reason"]
    assert "nothing exists" not in body["reason"]
    assert gateway.create_calls == 0


# ------------------------------------------- 9. the prior-attempt check (H1 honesty)

def facts_failure_app(monkeypatch, tmp_path, **make_app_kwargs):
    """An app whose RESOLVE order read fails transiently: the refusal happens pre-outbox."""
    return make_app(
        monkeypatch,
        tmp_path,
        order_result=_FakeHTTPError("the RESOLVE read timed out"),
        **make_app_kwargs,
    )


def seed_foreign_order_row(tmp_path) -> str:
    """
    One outbox row under the correlation this purchase would derive, but for a DIFFERENT
    resolve order: the shape that would poison this order's history if the prior-attempt
    lookup ran by correlation instead of by resolve_order_id.
    """
    handoff = runner.handoff_from_facts(_facts_for_seeding(), quantity=1)
    correlation = derive_correlation(handoff)
    outbox = Outbox(tmp_path / "outbox.sqlite3")
    outbox.reserve(
        correlation_id=correlation,
        resolve_order_id="order_ffffffffffff",
        payload_hash=runner.derive_payload_hash(handoff),
    )
    return correlation


@pytest.mark.parametrize("state", ["PENDING", "OUTCOME_UNKNOWN", "AMBIGUOUS"])
def test_prior_attempt_unresolved_state_survives_a_facts_failure_as_unknown(
    monkeypatch, tmp_path_factory, state
):
    tmp = tmp_path_factory.mktemp(f"h1-unresolved-{state.lower()}")
    app, gateway, _ = facts_failure_app(monkeypatch, tmp)
    seed_outbox_row(tmp, state=state)
    with TestClient(app) as client:
        response = post(client, default_body())
    assert response.status_code == 200
    body = response.json()
    assert body["outcome_class"] == "unknown_requires_reconciliation"
    assert "no merchant order" not in body["reason"]
    assert gateway.create_calls == 0


def test_prior_attempt_confirmed_create_survives_a_facts_failure_as_an_existing_order(
    monkeypatch, tmp_path
):
    app, gateway, _ = facts_failure_app(monkeypatch, tmp_path)
    seed_outbox_row(tmp_path, state="CONFIRMED")
    with TestClient(app) as client:
        response = post(client, default_body())
    assert response.status_code == 200
    body = response.json()
    assert body["outcome_class"] == "refused_after_create"
    assert body["outcome_status"] is None
    assert gateway.create_calls == 0


def test_prior_attempt_not_started_licenses_the_honest_refused_before_create(
    monkeypatch, tmp_path
):
    # A definite pre-execution failure (the RESOLVE read answered 401) over an inspected
    # outbox that records no attempt for this order: nothing was created, nothing ambiguous
    # exists, so refused_before_create is exactly the honest answer.
    app, gateway, _ = make_app(monkeypatch, tmp_path, order_result=(401, {"detail": "no"}))
    seed_outbox_row(tmp_path, state="NOT_STARTED")
    with TestClient(app) as client:
        response = post(client, default_body())
    assert response.status_code == 200
    body = response.json()
    assert body["outcome_class"] == "refused_before_create"
    assert {"name": "prior_attempt_check", "passed": True} in body["checks"]
    assert gateway.create_calls == 0


def test_prior_attempt_failed_preserves_the_safe_retry_classification(monkeypatch, tmp_path):
    # A FAILED row is the record of a definite non-creation; a facts failure on top of it
    # must not downgrade that to unknown — the safe retry classification is preserved.
    app, gateway, _ = facts_failure_app(monkeypatch, tmp_path)
    seed_outbox_row(tmp_path, state="FAILED")
    with TestClient(app) as client:
        response = post(client, default_body())
    assert response.status_code == 200
    body = response.json()
    assert body["outcome_class"] == "refused_before_create"
    assert {"name": "prior_attempt_check", "passed": True} in body["checks"]
    assert gateway.create_calls == 0


def test_prior_attempt_missing_outbox_fails_closed_without_claiming_no_prior_order(
    monkeypatch, tmp_path
):
    app, gateway, _ = facts_failure_app(monkeypatch, tmp_path)
    assert not (tmp_path / "outbox.sqlite3").exists()
    with TestClient(app) as client:
        response = post(client, default_body())
    assert response.status_code == 200
    body = response.json()
    assert body["outcome_class"] == "unknown_requires_reconciliation"
    assert "no merchant order" not in body["reason"]
    assert "cannot be ruled out" in body["reason"]
    assert gateway.create_calls == 0


def test_prior_attempt_unwritable_outbox_fails_closed(monkeypatch, tmp_path):
    outbox_dir = tmp_path / "outbox-dir"
    outbox_dir.mkdir()
    app, gateway, _ = facts_failure_app(
        monkeypatch, tmp_path, env_overrides={"MERCHANT_TRIGGER_OUTBOX": str(outbox_dir)}
    )
    with TestClient(app) as client:
        response = post(client, default_body())
    assert response.status_code == 200
    body = response.json()
    assert body["outcome_class"] == "unknown_requires_reconciliation"
    assert "no merchant order" not in body["reason"]
    assert gateway.create_calls == 0


def test_prior_attempt_corrupt_outbox_fails_closed(monkeypatch, tmp_path):
    (tmp_path / "outbox.sqlite3").write_bytes(b"this file is not a sqlite database at all")
    app, gateway, _ = facts_failure_app(monkeypatch, tmp_path)
    with TestClient(app) as client:
        response = post(client, default_body())
    assert response.status_code == 200
    body = response.json()
    assert body["outcome_class"] == "unknown_requires_reconciliation"
    assert "no merchant order" not in body["reason"]
    assert gateway.create_calls == 0


def test_prior_attempt_ignores_another_orders_history(monkeypatch, tmp_path):
    app, gateway, _ = facts_failure_app(monkeypatch, tmp_path)
    seed_foreign_order_row(tmp_path)
    with TestClient(app) as client:
        response = post(client, default_body())
    assert response.status_code == 200
    body = response.json()
    # The row under this correlation belongs to a different resolve order; this order has no
    # recorded attempt, so the refusal stays honestly refused_before_create.
    assert body["outcome_class"] == "refused_before_create"
    assert {"name": "prior_attempt_check", "passed": True} in body["checks"]
    assert gateway.create_calls == 0
