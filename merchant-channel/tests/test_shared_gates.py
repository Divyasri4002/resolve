"""
Gate-level tests for ``merchant_channel.gates`` — the shared purchase trust boundary.

``test_scratch_purchase_gates.py`` proves the gated runner end to end; this file proves the
shared module itself, independently of the runner: every mandated scenario refuses or passes
at the gate layer, the deployment pins (store, currency) come from caller parameters and not
from anything hardcoded in the shared module, and the order source machine-authenticates and
fails closed before any request. Everything here is hermetic: no network, no credential, no
Shopify call, and the outbox is a real ``Outbox`` at a temporary path.
"""
from __future__ import annotations

import asyncio
import inspect
import types

import pytest

from merchant_channel import gates
from merchant_channel.channel import PurchaseHandoff
from merchant_channel.correlation import (
    correlation_id as derive_correlation,
    payload_hash as derive_payload_hash,
)
from merchant_channel.outbox import (
    LINKAGE_SENT,
    Outbox,
)

import scratch_purchase as runner

MISSION_ID = "mission_gates0001"
ORDER_ID = "order_a1b2c3d4e5f6"
PRODUCT_GID = "gid://shopify/Product/15408674210109"
VARIANT_GID = "gid://shopify/ProductVariant/67625768649021"
STORE_DOMAIN = "resolve-merchant-test.myshopify.com"
OTHER_STORE = "other-deployment.example.myshopify.com"
MERCHANT_ORDER_GID = "gid://shopify/Order/9000000000000042"
EVENT_ID = "evt-gates0000000001"
SERVICE_TOKEN = "disposable-gates-machine-token-not-a-credential"


def make_facts(**overrides) -> gates.OrderFacts:
    values = dict(
        resolve_order_id=ORDER_ID,
        mission_id=MISSION_ID,
        status="order_created",
        currency="INR",
        razorpay_order_id="order_testdisposable01",
        razorpay_payment_id="pay_testdisposable01",
        merchant_order_linked=False,
        product_gid=PRODUCT_GID,
        variant_gid=VARIANT_GID,
        store_domain=STORE_DOMAIN,
        order_product_id="prod_gates",
        selected_product_id="prod_gates",
        title="Gate Item",
    )
    values.update(overrides)
    return gates.OrderFacts(**values)


def run_order_gates(facts, *, mission_id=MISSION_ID, resolve_order_id=None, quantity=1,
                    expected_currency="INR", expected_store_domain=STORE_DOMAIN):
    return gates.order_gates(
        facts, mission_id=mission_id, resolve_order_id=resolve_order_id, quantity=quantity,
        expected_currency=expected_currency, expected_store_domain=expected_store_domain,
    )


def handoff_for(facts: gates.OrderFacts, quantity: int = 1) -> PurchaseHandoff:
    return PurchaseHandoff(
        resolve_order_id=facts.resolve_order_id,
        shopify_product_gid=facts.product_gid or "",
        shopify_variant_gid=facts.variant_gid or "",
        quantity=quantity,
        currency=facts.currency or "",
    )


def reserve(outbox: Outbox, facts=None, quantity: int = 1, payload_hash: str | None = None) -> str:
    handoff = handoff_for(make_facts() if facts is None else facts, quantity)
    correlation = derive_correlation(handoff)
    outbox.reserve(
        correlation_id=correlation,
        resolve_order_id=handoff.resolve_order_id,
        payload_hash=payload_hash if payload_hash is not None else derive_payload_hash(handoff),
    )
    return correlation


# --------------------------------------------------------------- 1. valid facts pass

def test_valid_post_payment_facts_pass_every_gate():
    report = run_order_gates(make_facts())
    assert report.passed, report.problems
    names = [name for name, ok in report.checks if ok]
    assert any("canonical RESOLVE order id" in name for name in names)
    assert any("payment evidence present" in name for name in names)
    assert any("post-payment state" in name for name in names)
    assert any("no existing merchant linkage" in name for name in names)
    assert any("priced in INR" in name for name in names)
    assert any("canonical Shopify product GID" in name for name in names)
    assert any("canonical Shopify variant GID" in name for name in names)
    assert any("expected store" in name for name in names)
    assert any("agrees with the selected catalog item" in name for name in names)
    assert any("quantity is pinned to exactly 1" in name for name in names)


def test_an_explicit_matching_requested_order_id_passes():
    report = run_order_gates(make_facts(), resolve_order_id=ORDER_ID)
    assert report.passed, report.problems
    assert any("matches the requested order id" in name for name, ok in report.checks if ok)


# --------------------------------------------------------------- 2. payment evidence

def test_missing_payment_evidence_refuses():
    for label, overrides in (
        ("both missing", dict(razorpay_order_id=None, razorpay_payment_id=None)),
        ("payment id missing", dict(razorpay_payment_id=None)),
        ("order id missing", dict(razorpay_order_id=None)),
    ):
        report = run_order_gates(make_facts(**overrides))
        assert not report.passed, label
        assert any("payment-verification evidence" in problem for problem in report.problems), label


def test_a_pre_payment_status_refuses():
    report = run_order_gates(make_facts(status="checkout_confirmed"))
    assert not report.passed
    assert any("not a post-payment state" in problem for problem in report.problems)


# --------------------------------------------------------------- 3. product/variant identity

def test_missing_or_malformed_shopify_identity_refuses():
    for label, overrides in (
        ("no product gid", dict(product_gid=None)),
        ("no variant gid", dict(variant_gid=None)),
        ("product gid in the variant slot", dict(variant_gid=PRODUCT_GID)),
        ("non-numeric product gid", dict(product_gid="gid://shopify/Product/abc")),
        ("half an identity", dict(product_gid=None, variant_gid=VARIANT_GID)),
    ):
        report = run_order_gates(make_facts(**overrides))
        assert not report.passed, label
        assert any("GID" in problem for problem in report.problems), label


def test_order_and_selection_disagreeing_on_product_identity_refuses():
    report = run_order_gates(make_facts(order_product_id="prod_other"))
    assert not report.passed
    assert any("disagrees with the selected catalog item" in problem for problem in report.problems)


# --------------------------------------------------------------- 4+5. deployment pins

def test_a_store_other_than_the_pinned_one_refuses():
    report = run_order_gates(make_facts(store_domain="someone-else.myshopify.com"))
    assert not report.passed
    assert any("could never be corroborated" in problem for problem in report.problems)


def test_a_currency_other_than_the_pinned_one_refuses():
    report = run_order_gates(make_facts(currency="USD"))
    assert not report.passed
    assert any("nothing converts" in problem for problem in report.problems)


def test_the_pins_come_from_parameters_not_from_the_shared_module():
    # A second deployment with its own store and currency passes facts that agree with ITS
    # pins — the same module, no scratch values anywhere in it.
    other_facts = make_facts(store_domain=OTHER_STORE, currency="EUR",
                             razorpay_order_id="order_testdisposable02",
                             razorpay_payment_id="pay_testdisposable02")
    report = gates.order_gates(
        other_facts, mission_id=MISSION_ID, expected_currency="EUR",
        expected_store_domain=OTHER_STORE,
    )
    assert report.passed, report.problems

    # And the scratch facts refuse under that other deployment's pins: a gate refusal follows
    # the caller's parameter, proving nothing is hardcoded in the shared module.
    for label, report in (
        ("store", run_order_gates(make_facts(), expected_store_domain=OTHER_STORE)),
        ("currency", run_order_gates(make_facts(), expected_currency="USD")),
    ):
        assert not report.passed, label

    source = inspect.getsource(gates)
    assert "resolve-merchant-test" not in source
    assert "host.docker.internal" not in source
    assert "INR" not in source


# --------------------------------------------------------------- 6. quantity

def test_quantity_is_pinned_to_exactly_one():
    assert run_order_gates(make_facts(), quantity=1).passed
    for bad in (2, 0, -1, True, "2"):
        report = run_order_gates(make_facts(), quantity=bad)
        assert not report.passed, bad
        assert any("pinned to 1" in problem for problem in report.problems), bad


# --------------------------------------------------------------- 7. already linked

def test_an_already_linked_order_refuses():
    report = run_order_gates(make_facts(merchant_order_linked=True))
    assert not report.passed
    assert any("already carries a merchant linkage" in problem for problem in report.problems)


# --------------------------------------------------------------- 8. order identity

def test_a_non_canonical_or_foreign_order_identity_refuses():
    for label, overrides, needle in (
        ("not canonical", dict(resolve_order_id="not-an-order-id"), "not a canonical RESOLVE order id"),
        ("wrong prefix", dict(resolve_order_id="pay_a1b2c3d4e5f6"), "not a canonical RESOLVE order id"),
        ("short hex", dict(resolve_order_id="order_a1b2c3d4e5f"), "not a canonical RESOLVE order id"),
        ("non-hex", dict(resolve_order_id="order_a1b2c3d4e5fx"), "not a canonical RESOLVE order id"),
    ):
        report = run_order_gates(make_facts(**overrides))
        assert not report.passed, label
        assert any(needle in problem for problem in report.problems), label

    report = run_order_gates(make_facts(), resolve_order_id="order_000000000000")
    assert not report.passed
    assert any("not the one that was requested" in problem for problem in report.problems)


def test_an_order_from_a_different_mission_refuses():
    report = run_order_gates(make_facts(), mission_id="mission_other9999")
    assert not report.passed
    assert any("does not belong to the requested mission" in problem for problem in report.problems)


# --------------------------------------------------------------- 9. outbox states

def _seed_pending(outbox: Outbox, correlation: str) -> None:
    outbox.begin_create(correlation)


def _seed_unknown(outbox: Outbox, correlation: str) -> None:
    outbox.mark_outcome_unknown(correlation, "the create answer was lost")


def _seed_ambiguous(outbox: Outbox, correlation: str) -> None:
    outbox.mark_reconciled(correlation, "ambiguous")


def _seed_linked(outbox: Outbox, correlation: str) -> None:
    outbox.record_event(correlation, event_id=EVENT_ID, event_body="{}")
    outbox.mark_confirmed(correlation, MERCHANT_ORDER_GID)
    outbox.set_linkage(correlation, LINKAGE_SENT, event_id=EVENT_ID)


def test_blocked_outbox_states_fail_closed(tmp_path):
    for label, seed in (
        ("pending", _seed_pending),
        ("unknown", _seed_unknown),
        ("ambiguous", _seed_ambiguous),
        ("already-linked", _seed_linked),
    ):
        outbox = Outbox(tmp_path / f"{label}.sqlite3")
        correlation = reserve(outbox)
        seed(outbox, correlation)
        report = gates.outbox_state_gates(
            outbox, correlation, ORDER_ID, derive_payload_hash(handoff_for(make_facts()))
        )
        assert not report.passed, label
        assert report.problems, label


def test_an_unknown_outcome_never_authorizes_another_create(tmp_path):
    outbox = Outbox(tmp_path / "outbox.sqlite3")
    correlation = reserve(outbox)
    outbox.mark_outcome_unknown(correlation, "the create answer was lost")
    report = gates.outbox_state_gates(
        outbox, correlation, ORDER_ID, derive_payload_hash(handoff_for(make_facts()))
    )
    assert not report.passed
    assert any("outcome is unknown" in problem for problem in report.problems)
    assert outbox.get(correlation).may_create is False


def test_an_outbox_row_for_a_different_purchase_refuses(tmp_path):
    outbox = Outbox(tmp_path / "outbox.sqlite3")
    correlation = reserve(outbox, payload_hash="f" * 64)
    report = gates.outbox_state_gates(
        outbox, correlation, ORDER_ID, derive_payload_hash(handoff_for(make_facts()))
    )
    assert not report.passed
    assert any("different purchase" in problem for problem in report.problems)


def test_an_empty_outbox_licenses_the_create(tmp_path):
    outbox = Outbox(tmp_path / "outbox.sqlite3")
    correlation = reserve(outbox)
    report = gates.outbox_state_gates(
        outbox, correlation, ORDER_ID, derive_payload_hash(handoff_for(make_facts()))
    )
    assert report.passed, report.problems


# --------------------------------------------------------------- 10. the runner exposes the shared gates

def test_the_runner_uses_the_shared_gates_not_a_copy():
    assert runner.order_gates is gates.order_gates
    assert runner.fetch_order_facts is gates.fetch_order_facts
    assert runner.outbox_state_gates is gates.outbox_state_gates
    assert runner.OrderFacts is gates.OrderFacts
    assert runner.GateReport is gates.GateReport
    for name in ("run", "main", "RunnerResult", "RunLock", "acquire_run_lock",
                 "release_run_lock", "handoff_from_facts"):
        assert hasattr(runner, name), name


# --------------------------------------------------------------- 11. the order source

ORDER_PAYLOAD = {
    "order_id": ORDER_ID,
    "mission_id": MISSION_ID,
    "status": "order_created",
    "product": {"product_id": "prod_gates", "title": "Gate Item", "merchant": "RESOLVE Merchant Test"},
    "amount": "1299.00",
    "currency": "INR",
    "razorpay_order_id": "order_testdisposable01",
    "razorpay_payment_id": "pay_testdisposable01",
    "merchant_order_linked": False,
}

CHECKOUT_PAYLOAD = {
    "mission_id": MISSION_ID,
    "checkout": {"currency": "INR", "subtotal": "1299.00"},
    "confirmed": True,
    "mission_status": "order_created",
    "shopping_intent": {
        "state": "checkout_review",
        "selected_product": {
            "product_id": "prod_gates",
            "title": "Gate Item",
            "price": "1299.00",
            "currency": "INR",
            "merchant": "RESOLVE Merchant Test",
            "source": "shopify",
            "shopify_product_gid": PRODUCT_GID,
            "shopify_variant_gid": VARIANT_GID,
            "shopify_store_domain": STORE_DOMAIN,
        },
    },
    "total_recovery_spend": "0",
}


class _FakeHTTPError(Exception):
    pass


class _Response:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


def make_fake_httpx(order_result, checkout_result):
    """
    A stand-in for the httpx module with exactly the surface ``fetch_order_facts`` uses,
    recording every (url, headers) it is handed. No socket is opened anywhere.
    """
    requests: list = []

    class _Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return False

        async def get(self, url, headers=None):
            requests.append((url, dict(headers or {})))
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
        requests=requests,
    )


def fetch_with(monkeypatch, order_result, checkout_result, *, token=SERVICE_TOKEN):
    if token is None:
        monkeypatch.delenv(gates.SERVICE_TOKEN_ENV, raising=False)
    else:
        monkeypatch.setenv(gates.SERVICE_TOKEN_ENV, token)
    fake = make_fake_httpx(order_result, checkout_result)
    monkeypatch.setattr(gates, "httpx", fake)
    facts, report = asyncio.run(
        gates.fetch_order_facts("http://resolve.test", mission_id=MISSION_ID)
    )
    return facts, report, fake


def test_fetch_builds_facts_from_the_served_rows_and_authenticates(monkeypatch):
    facts, report, fake = fetch_with(monkeypatch, (200, ORDER_PAYLOAD), (200, CHECKOUT_PAYLOAD))
    assert report.passed, report.problems
    assert fake.requests and all(
        headers.get(gates.SERVICE_TOKEN_HEADER) == SERVICE_TOKEN for _, headers in fake.requests
    )
    assert {url for url, _ in fake.requests} == {
        f"http://resolve.test/missions/{MISSION_ID}/order",
        f"http://resolve.test/missions/{MISSION_ID}/shopping/checkout",
    }
    assert facts == gates.OrderFacts(
        resolve_order_id=ORDER_ID,
        mission_id=MISSION_ID,
        status="order_created",
        currency="INR",
        razorpay_order_id="order_testdisposable01",
        razorpay_payment_id="pay_testdisposable01",
        merchant_order_linked=False,
        product_gid=PRODUCT_GID,
        variant_gid=VARIANT_GID,
        store_domain=STORE_DOMAIN,
        order_product_id="prod_gates",
        selected_product_id="prod_gates",
        title="Gate Item",
    )
    # The identity comes from the served selection only; the fetched facts then pass the gates.
    assert run_order_gates(facts).passed


def test_fetch_fails_closed_without_a_service_token(monkeypatch):
    facts, report, fake = fetch_with(monkeypatch, (200, ORDER_PAYLOAD), (200, CHECKOUT_PAYLOAD),
                                     token=None)
    assert facts is None
    assert not report.passed
    assert any(gates.SERVICE_TOKEN_ENV in problem for problem in report.problems)
    # Fail closed BEFORE any request: no read was attempted without the credential.
    assert fake.requests == []


def test_fetch_refuses_when_resolve_cannot_be_asked(monkeypatch):
    facts, report, fake = fetch_with(
        monkeypatch, _FakeHTTPError("connection refused"), (200, CHECKOUT_PAYLOAD)
    )
    assert facts is None
    assert not report.passed
    assert any("could not be asked" in problem for problem in report.problems)


def test_fetch_refuses_a_non_200_order_answer(monkeypatch):
    facts, report, fake = fetch_with(monkeypatch, (404, {"detail": "not found"}),
                                     (200, CHECKOUT_PAYLOAD))
    assert facts is None
    assert not report.passed
    assert any("HTTP 404" in problem for problem in report.problems)
