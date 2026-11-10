"""
Tests for Task 11B: bounded refund authority.

The rule under test lives in the Authority Engine, not in the executor:

    request_refund is allowed only when it is delegated in the mandate, bounded refund
    authority is switched on for the deployment, and the CANONICAL amount (derived
    server-side from the persisted payment/order) is within the resulting ceiling —
    min(deployment ceiling, the mandate's own refund_limit).

Engine tests are pure (no DB, no network, no provider). Integration tests drive the real
routes with the Task 11A executor's Razorpay seams mocked — no live payment API is called.
"""
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.authority.engine import Decision, ProposedAction, evaluate, refund_ceiling
from app.config import get_settings
from app.main import app
from app.models.db_models import RefundRecord
from app.models.mission import AllowedAction, Authority

from tests.test_refund_execution import (
    SHAMPOO_PROMPT,
    _checkout_signature,
    _confirmed_order_with_issue,
    _login,
    _refund,
    _register,
    install_fake_reasoning,  # noqa: F401  (fixture reused from the Task 11A module)
    mock_razorpay,  # noqa: F401
    recorded_events,  # noqa: F401
    refund_api,  # noqa: F401
    restart_env,  # noqa: F401
)

REFUND = ProposedAction(action=AllowedAction.REQUEST_REFUND, amount=Decimal("399"))


@pytest.fixture
def fake_reasoning(install_fake_reasoning):  # noqa: F811
    """The Task 11A controlled-provider installer, re-exported so it can be called here."""
    return install_fake_reasoning


def _settings(monkeypatch, *, enabled: bool, ceiling: str):
    """Point the deployment's bounded-refund policy at a specific configuration."""
    settings = get_settings()
    monkeypatch.setattr(settings, "shopping_refund_authority_enabled", enabled)
    monkeypatch.setattr(settings, "shopping_refund_max_inr", Decimal(ceiling))
    return settings


def _mandate(**overrides) -> Authority:
    base = dict(
        allowed_actions=[AllowedAction.REQUEST_REFUND],
        recovery_budget=Decimal("0"),
        refund_limit=None,
    )
    base.update(overrides)
    return Authority(**base)


# --------------------------------------------------------------------------- #
# 1: authority disabled -> deny
# --------------------------------------------------------------------------- #
def test_disabled_authority_blocks_refund(monkeypatch):
    _settings(monkeypatch, enabled=False, ceiling="500")
    authority = _mandate(refund_limit=Decimal("500"))
    assert refund_ceiling(authority) is None
    decision = evaluate(authority, REFUND)
    assert decision.decision is Decision.BLOCKED
    assert "not configured" in decision.reason


# --------------------------------------------------------------------------- #
# 2: authority enabled -> an eligible refund is allowed
# --------------------------------------------------------------------------- #
def test_enabled_authority_allows_eligible_refund(monkeypatch):
    _settings(monkeypatch, enabled=True, ceiling="500")
    decision = evaluate(_mandate(), REFUND)
    assert decision.decision is Decision.APPROVED


# --------------------------------------------------------------------------- #
# 3, 4, 5: only a delegated request_refund is eligible
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "action", [AllowedAction.REQUEST_REPLACEMENT, AllowedAction.REQUEST_RETURN]
)
def test_other_remedies_are_not_eligible_under_refund_authority(monkeypatch, action):
    _settings(monkeypatch, enabled=True, ceiling="500")
    decision = evaluate(_mandate(), ProposedAction(action=action))
    assert decision.decision is Decision.BLOCKED
    assert "never delegated" in decision.reason


def test_request_refund_is_the_eligible_action(monkeypatch):
    _settings(monkeypatch, enabled=True, ceiling="500")
    assert evaluate(_mandate(), REFUND).decision is Decision.APPROVED
    # ...and only when it is delegated at all.
    not_delegated = _mandate(allowed_actions=[])
    assert evaluate(not_delegated, REFUND).decision is Decision.BLOCKED


# --------------------------------------------------------------------------- #
# 6, 7, 8: below / at / above the ceiling
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "amount,expected",
    [
        (Decimal("499.99"), Decision.APPROVED),
        (Decimal("500"), Decision.APPROVED),
        (Decimal("500.01"), Decision.BLOCKED),
        (Decimal("4100"), Decision.BLOCKED),
    ],
)
def test_ceiling_boundaries(monkeypatch, amount, expected):
    _settings(monkeypatch, enabled=True, ceiling="500")
    decision = evaluate(_mandate(), ProposedAction(action=AllowedAction.REQUEST_REFUND, amount=amount))
    assert decision.decision is expected
    if expected is Decision.BLOCKED:
        assert "exceeds the delegated refund ceiling of ₹500" in decision.reason


def test_mandate_ceiling_narrows_but_never_raises(monkeypatch):
    _settings(monkeypatch, enabled=True, ceiling="500")
    narrower = _mandate(refund_limit=Decimal("300"))
    assert refund_ceiling(narrower) == Decimal("300")
    assert evaluate(narrower, REFUND).decision is Decision.BLOCKED
    assert evaluate(narrower, ProposedAction(action=AllowedAction.REQUEST_REFUND, amount=Decimal("300"))).decision is Decision.APPROVED
    # A mandate asking for more than the deployment allows is still capped by the deployment.
    wider = _mandate(refund_limit=Decimal("999999"))
    assert refund_ceiling(wider) == Decimal("500")


# --------------------------------------------------------------------------- #
# 9, 10: invalid or missing canonical amount
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("amount", [None, Decimal("0"), Decimal("-100")])
def test_invalid_canonical_amount_is_denied(monkeypatch, amount):
    _settings(monkeypatch, enabled=True, ceiling="500")
    decision = evaluate(_mandate(), ProposedAction(action=AllowedAction.REQUEST_REFUND, amount=amount))
    assert decision.decision is Decision.BLOCKED


# --------------------------------------------------------------------------- #
# 17: the decision is deterministic
# --------------------------------------------------------------------------- #
def test_decision_is_deterministic(monkeypatch):
    _settings(monkeypatch, enabled=True, ceiling="500")
    decisions = [evaluate(_mandate(), REFUND) for _ in range(5)]
    assert {d.decision for d in decisions} == {Decision.APPROVED}
    assert len({d.reason for d in decisions}) == 1
    # The spend budget is a different axis: it never changes a refund outcome.
    assert evaluate(
        _mandate(recovery_budget=Decimal("100000")), REFUND
    ).decision is Decision.APPROVED
    assert evaluate(_mandate(recovery_budget=Decimal("0")), REFUND).decision is Decision.APPROVED


# --------------------------------------------------------------------------- #
# Integration helpers
# --------------------------------------------------------------------------- #
async def _verified_mission_product(
    client, install_mock_discovery, sample_products, mock_razorpay, product_id, payment_id
) -> str:
    install_mock_discovery(sample_results=sample_products)
    await _register(client, "Beta Owner", "beta@shop.com")
    resp = await client.post("/missions/shopping", json={"prompt": SHAMPOO_PROMPT})
    mission_id = resp.json()["mission_id"]
    assert (
        await client.post(
            f"/missions/{mission_id}/shopping/select-product", json={"product_id": product_id}
        )
    ).status_code == 200
    assert (await client.post(f"/missions/{mission_id}/shopping/checkout/confirm")).status_code == 200
    order_resp = await client.post(
        f"/missions/{mission_id}/shopping/payment/create-order", json={}
    )
    razorpay_order_id = order_resp.json()["payment"]["razorpay_order_id"]
    verify = await client.post(
        f"/missions/{mission_id}/shopping/payment/verify",
        json={
            "razorpay_order_id": razorpay_order_id,
            "razorpay_payment_id": payment_id,
            "razorpay_signature": _checkout_signature(razorpay_order_id, payment_id),
        },
    )
    assert verify.status_code == 200
    return mission_id


async def _delegate(test_session, mission_id, actions, refund_limit=None, budget="0"):
    """Persist what a delegating user's mandate would hold: delegated action types and an
    optional per-mandate refund ceiling. The engine's policy code is never touched."""
    from app.models.db_models import MissionRecord

    async with test_session() as session:
        rec = await session.get(MissionRecord, mission_id)
        mandate = dict(rec.mandate or {})
        authority = dict(mandate.get("authority") or {})
        authority["allowed_actions"] = list(actions)
        authority["recovery_budget"] = str(budget)
        authority["refund_limit"] = None if refund_limit is None else str(refund_limit)
        mandate["authority"] = authority
        rec.mandate = mandate
        await session.commit()


def _refund_proposal(amount: object = None) -> dict:
    return {
        "issue_assessment": "The delivered item was wrong; the payment should be reversed.",
        "proposed_action": "request_refund",
        "reasoning_summary": "Refund proposed by the resolution model.",
        "requested_amount": amount,
        "currency": "INR",
        "confidence": 0.9,
        "needs_escalation": False,
    }


async def _assess(client, mission_id, issue_id):
    resp = await client.post(f"/missions/{mission_id}/order/issues/{issue_id}/assess")
    assert resp.status_code == 200
    return resp.json()


# --------------------------------------------------------------------------- #
# 1 (end-to-end): disabled authority -> DENY at assessment, executor refuses,
# zero provider calls
# --------------------------------------------------------------------------- #
async def test_disabled_authority_denies_end_to_end(
    restart_env,
    install_mock_discovery,
    sample_products,
    mock_razorpay,
    refund_api,
    fake_reasoning,
    monkeypatch,
):
    _settings(monkeypatch, enabled=False, ceiling="500")
    make_client, test_session = restart_env
    async with make_client() as client:
        mission_id = await _verified_mission_product(
            client, install_mock_discovery, sample_products, mock_razorpay, "item_dove", "pay_B1"
        )
        issue_id = await _confirmed_order_with_issue(client, mission_id)
        await _delegate(test_session, mission_id, ["request_refund"])

        # Installed only here: mission creation needs the hermetic mock reasoning provider.
        fake = fake_reasoning(_refund_proposal(399))
        assessment = await _assess(client, mission_id, issue_id)
        assert assessment["authority_decision"] == "deny"
        assert "not configured" in assessment["decision_reason"]

        resp = await _refund(client, mission_id, issue_id)
        assert resp.status_code == 400 and "requires an explicit allow" in resp.json()["detail"]
    assert fake.contexts, "the proposal was still generated — it was simply not authorized"
    assert refund_api.calls == {"create": 0, "fetch": 0, "list": 0}


# --------------------------------------------------------------------------- #
# 2 + 6 (end-to-end): enabled authority -> ALLOW, canonical amount refunded
# --------------------------------------------------------------------------- #
async def test_enabled_authority_allows_and_executes(
    restart_env,
    install_mock_discovery,
    sample_products,
    mock_razorpay,
    refund_api,
    fake_reasoning,
    monkeypatch,
):
    _settings(monkeypatch, enabled=True, ceiling="500")
    make_client, test_session = restart_env
    async with make_client() as client:
        mission_id = await _verified_mission_product(
            client, install_mock_discovery, sample_products, mock_razorpay, "item_dove", "pay_B2"
        )
        issue_id = await _confirmed_order_with_issue(client, mission_id)
        await _delegate(test_session, mission_id, ["request_refund"])
        # Installed only here: mission creation needs the hermetic mock reasoning provider.
        fake_reasoning(_refund_proposal(399))
        assessment = await _assess(client, mission_id, issue_id)
        assert assessment["authority_decision"] == "allow"
        assert assessment["requested_amount"] == "399.00"

        resp = await _refund(client, mission_id, issue_id)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "verified" and data["amount_paise"] == 39900
        assert refund_api.calls["create"] == 1
        async with test_session() as session:
            row = await session.scalar(select(RefundRecord).where(RefundRecord.issue_id == issue_id))
            assert row.amount_paise == 39900 and row.currency == "INR"


# --------------------------------------------------------------------------- #
# 8 (end-to-end): canonical amount above the ceiling -> deny, zero provider calls
# --------------------------------------------------------------------------- #
async def test_canonical_amount_above_ceiling_is_denied(
    restart_env,
    install_mock_discovery,
    sample_products,
    mock_razorpay,
    refund_api,
    fake_reasoning,
    monkeypatch,
):
    _settings(monkeypatch, enabled=True, ceiling="300")
    make_client, test_session = restart_env
    async with make_client() as client:
        mission_id = await _verified_mission_product(
            client, install_mock_discovery, sample_products, mock_razorpay, "item_dove", "pay_B3"
        )
        issue_id = await _confirmed_order_with_issue(client, mission_id)
        await _delegate(test_session, mission_id, ["request_refund"])
        # The model asks for almost nothing — the gate evaluates the canonical amount instead.
        fake_reasoning(_refund_proposal(1))
        assessment = await _assess(client, mission_id, issue_id)
        assert assessment["requested_amount"] == "399.00"  # canonical, not the model's ₹1
        assert assessment["authority_decision"] == "escalate"
        assert "exceeds the delegated refund ceiling of ₹300" in assessment["decision_reason"]

        resp = await _refund(client, mission_id, issue_id)
        assert resp.status_code == 400
    assert refund_api.calls == {"create": 0, "fetch": 0, "list": 0}


# --------------------------------------------------------------------------- #
# 15: Gemini's requested amount can neither widen nor narrow what is evaluated
# --------------------------------------------------------------------------- #
async def test_model_amount_cannot_change_authority(
    restart_env,
    install_mock_discovery,
    sample_products,
    mock_razorpay,
    refund_api,
    fake_reasoning,
    monkeypatch,
):
    _settings(monkeypatch, enabled=True, ceiling="500")
    make_client, test_session = restart_env
    async with make_client() as client:
        # A huge model-requested amount does not defeat the ceiling...
        big = await _verified_mission_product(
            client, install_mock_discovery, sample_products, mock_razorpay, "item_dove", "pay_B4"
        )
        big_issue = await _confirmed_order_with_issue(client, big)
        await _delegate(test_session, big, ["request_refund"])
        # A vast model-requested amount is never what the gate sees...
        fake_reasoning(_refund_proposal(999999))
        assessment = await _assess(client, big, big_issue)
        assert assessment["requested_amount"] == "399.00"
        assert assessment["authority_decision"] == "allow"

        resp = await _refund(client, big, big_issue)
        assert resp.status_code == 200
        assert resp.json()["amount"] == "399.00"  # never ₹999999
    assert refund_api.calls["create"] == 1


# --------------------------------------------------------------------------- #
# 16: a request body cannot change authority
# --------------------------------------------------------------------------- #
async def test_client_body_cannot_change_authority(
    restart_env,
    install_mock_discovery,
    sample_products,
    mock_razorpay,
    refund_api,
    fake_reasoning,
    monkeypatch,
):
    _settings(monkeypatch, enabled=True, ceiling="300")
    make_client, test_session = restart_env
    async with make_client() as client:
        mission_id = await _verified_mission_product(
            client, install_mock_discovery, sample_products, mock_razorpay, "item_dove", "pay_B5"
        )
        issue_id = await _confirmed_order_with_issue(client, mission_id)
        await _delegate(
            test_session, mission_id, ["request_refund"], refund_limit="999999"
        )
        # Installed only here: mission creation needs the hermetic mock reasoning provider.
        fake_reasoning(_refund_proposal(399))
        assessment = await _assess(client, mission_id, issue_id)
        # The mandate cannot raise itself past the deployment ceiling either.
        assert assessment["authority_decision"] == "escalate"
        assert "exceeds the delegated refund ceiling of ₹300" in assessment["decision_reason"]

        resp = await _refund(
            client,
            mission_id,
            issue_id,
            json={
                "amount": 1,
                "shopping_refund_max_inr": 999999,
                "authority_decision": "allow",
                "proposed_action": "request_refund",
                "razorpay_payment_id": "pay_OTHER",
            },
        )
        assert resp.status_code == 400 and "explicit allow" in resp.json()["detail"]
    assert refund_api.calls == {"create": 0, "fetch": 0, "list": 0}


# --------------------------------------------------------------------------- #
# 11, 12, 13, 14: the surrounding guards keep holding under the new policy
# --------------------------------------------------------------------------- #
async def test_unverified_payment_still_denied(
    restart_env,
    install_mock_discovery,
    sample_products,
    mock_razorpay,
    refund_api,
    fake_reasoning,
    monkeypatch,
):
    _settings(monkeypatch, enabled=True, ceiling="500")
    make_client, test_session = restart_env
    async with make_client() as client:
        mission_id = await _verified_mission_product(
            client, install_mock_discovery, sample_products, mock_razorpay, "item_dove", "pay_B6"
        )
        issue_id = await _confirmed_order_with_issue(client, mission_id)
        await _delegate(test_session, mission_id, ["request_refund"])
        # Demote the persisted payment back to pending — the canonical proof is gone.
        from app.models.db_models import MissionRecord

        async with test_session() as session:
            rec = await session.get(MissionRecord, mission_id)
            intent = dict(rec.shopping_intent)
            payment = dict(intent["payment"])
            payment["status"] = "pending"
            intent["payment"] = payment
            rec.shopping_intent = intent
            await session.commit()

        fake_reasoning(_refund_proposal(399))
        await _assess(client, mission_id, issue_id)

        resp = await _refund(client, mission_id, issue_id)
        assert resp.status_code == 400 and "verified" in resp.json()["detail"]
    assert refund_api.calls == {"create": 0, "fetch": 0, "list": 0}


async def test_non_shopping_mission_foreign_user_and_duplicate_refund(
    restart_env,
    install_mock_discovery,
    sample_products,
    mock_razorpay,
    refund_api,
    fake_reasoning,
    monkeypatch,
):
    _settings(monkeypatch, enabled=True, ceiling="500")
    make_client, test_session = restart_env
    async with make_client() as client:
        mission_id = await _verified_mission_product(
            client, install_mock_discovery, sample_products, mock_razorpay, "item_dove", "pay_B7"
        )
        issue_id = await _confirmed_order_with_issue(client, mission_id)
        await _delegate(test_session, mission_id, ["request_refund"])
        fake_reasoning(_refund_proposal(399))
        assessment = await _assess(client, mission_id, issue_id)
        assert assessment["authority_decision"] == "allow"

        # 13: an authenticated non-owner is refused by the existing access guard.
        await _register(client, "Mallory", "mallory@shop.com")
        assert (await _refund(client, mission_id, issue_id)).status_code == 404
        await _login(client, "beta@shop.com")
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as anon:
            # 13 (cont.): an anonymous caller gets the same 404.
            assert (await _refund(anon, mission_id, issue_id)).status_code == 404

        # 12: a recovery mission has none of the shopping order/payment context.
        recovery = await client.post(
            "/missions",
            json={
                "mission_type": "recovery",
                "objective": {"description": "Recover a package", "success_criteria": []},
                "authority": {"allowed_actions": ["request_refund"], "recovery_budget": "5000"},
                "transaction": {
                    "merchant_name": "Store",
                    "order_id": "o-1",
                    "amount_paid": "100",
                },
            },
        )
        assert (
            await _refund(client, recovery.json()["mission_id"], issue_id)
        ).status_code == 400

        # 14: once verified, a repeat call returns the same record — no second refund.
        first = await _refund(client, mission_id, issue_id)
        assert first.status_code == 200 and first.json()["status"] == "verified"
        second = await _refund(client, mission_id, issue_id)
        assert second.json()["refund_id"] == first.json()["refund_id"]
        assert second.json()["already_existed"] is True
    assert refund_api.calls["create"] == 1
