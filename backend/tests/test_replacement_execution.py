"""
Tests for Task 12A: controlled replacement execution.

Pipeline under test:
    assessment (Task 10) -> Authority Engine (bounded replacement authority, Task 12A)
    -> replacement executor -> ReplacementProvider -> persisted state.

Two facts the suite keeps honest about:
  * No merchant replacement provider exists in this project, so the DEFAULT provider refuses
    explicitly. Tests assert that the executor records intent + 'pending' + HTTP 503 and never
    claims a replacement.
  * A test-only fake provider stands in for a future real one (mocks are permitted in automated
    tests ONLY); it is installed through the same seam production uses and is never selectable
    via configuration.
"""
from decimal import Decimal

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.authority.engine import Decision, ProposedAction, evaluate
from app.config import get_settings
from app.main import app
from app.models.db_models import AuditLogRecord, RefundRecord, ReplacementRecord
from app.models.mission import AllowedAction, Authority
from app.routers import missions as missions_module
from app.tools.replacement.base import (
    ReplacementAck,
    ReplacementProviderRejected,
    ReplacementProviderUnavailable,
    ReplacementStatusInfo,
)

from tests.test_refund_authority import _delegate, _verified_mission_product  # noqa: F401
from tests.test_refund_execution import (  # noqa: F401
    _confirmed_order_with_issue,
    _login,
    _register,
    _refund,
    install_fake_reasoning,
    mock_razorpay,
    recorded_events,
    refund_api,
    restart_env,
)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _uid() -> str:
    return uuid.uuid4().hex[:8]
class FakeReplacementProvider:
    """Test-only stand-in for a future merchant fulfilment provider."""

    name = "fake"

    def __init__(self):
        self.requests = []
        self.status_reads = []
        self.accepted = True
        self.reference = "MERC-REPL-1"
        self.error: Exception | None = None
        self.status_error: Exception | None = None
        self.outcome = "accepted"

    async def request_replacement(self, request):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return ReplacementAck(provider_reference=self.reference, accepted=self.accepted)

    async def get_replacement_status(self, provider_reference):
        self.status_reads.append(provider_reference)
        if self.status_error is not None:
            raise self.status_error
        return ReplacementStatusInfo(
            provider_reference=provider_reference, outcome=self.outcome
        )


@pytest.fixture
def replacement_provider(monkeypatch):
    provider = FakeReplacementProvider()
    monkeypatch.setattr(
        missions_module, "get_replacement_provider", lambda provider_name=None: provider
    )
    return provider


def _replacement_proposal() -> dict:
    return {
        "issue_assessment": "The delivered unit is the wrong variant; a replacement fits.",
        "proposed_action": "request_replacement",
        "reasoning_summary": "Replacement proposed by the resolution model.",
        "requested_amount": None,
        "currency": "INR",
        "confidence": 0.85,
        "needs_escalation": False,
    }


def _set_replacement_switch(monkeypatch, enabled: bool):
    monkeypatch.setattr(
        get_settings(), "shopping_replacement_authority_enabled", enabled
    )


def _mandate(**overrides) -> Authority:
    base = dict(
        allowed_actions=[AllowedAction.REQUEST_REPLACEMENT],
        recovery_budget=Decimal("0"),
    )
    base.update(overrides)
    return Authority(**base)


async def _issue(client, mission_id, kind: str = "replacement") -> str:
    # The order layer is idempotent, so this is safe to call for every new issue.
    assert (await client.post(f"/missions/{mission_id}/order", json={})).status_code == 200
    # 200 the first time; afterwards the order is already past confirmation.
    assert (
        await client.post(f"/missions/{mission_id}/order/confirm", json={})
    ).status_code in (200, 400)
    resp = await client.post(
        f"/missions/{mission_id}/order/issues",
        json={
            "issue_type": "wrong_item",
            # Unique description: identical issue reports are deduplicated by intake.
            "description": f"Wrong variant arrived; please send the right one. [{_uid()}]",
            "requested_resolution": kind,
        },
    )
    assert resp.status_code == 200
    return resp.json()["issue_id"]


async def _assess(client, mission_id, issue_id) -> dict:
    resp = await client.post(f"/missions/{mission_id}/order/issues/{issue_id}/assess")
    assert resp.status_code == 200
    return resp.json()


async def _replace(client, mission_id, issue_id, json=None):
    url = f"/missions/{mission_id}/order/issues/{issue_id}/replacement"
    return await client.post(url, json=json) if json is not None else await client.post(url)


async def _set_assessment_decision(test_session, assessment_id, decision):
    from app.models.db_models import ResolutionAssessmentRecord

    async with test_session() as session:
        row = await session.get(ResolutionAssessmentRecord, assessment_id)
        row.authority_decision = decision
        await session.commit()


async def _authorized_replacement_issue(client, test_session, mission_id) -> str:
    """Confirmed order -> issue -> ALLOWed request_replacement assessment."""
    issue_id = await _issue(client, mission_id)
    await _delegate(test_session, mission_id, ["request_replacement"])
    assessment = await _assess(client, mission_id, issue_id)
    assert assessment["proposed_action"] == "request_replacement"
    assert assessment["authority_decision"] == "allow"
    return issue_id


async def _setup(restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake):
    """Client factory, session factory, and a mission factory that creates a verified
    payment-confirmed mission and only THEN installs the controlled resolution proposal —
    mission creation itself needs the hermetic mock reasoning provider."""
    make_client, test_session = restart_env
    client = make_client()

    async def _mission(c):
        mission_id = await _verified_mission_product(
            c, install_mock_discovery, sample_products, mock_razorpay, "item_dove", "pay_R1"
        )
        install_fake(_replacement_proposal())
        return mission_id

    return client, test_session, _mission


# --------------------------------------------------------------------------- #
# ENGINE: bounded replacement authority (items 1, 2, 3, 16-distinctness)
# --------------------------------------------------------------------------- #
def test_replacement_requires_its_own_authority(monkeypatch):
    _set_replacement_switch(monkeypatch, enabled=False)
    decision = evaluate(_mandate(), ProposedAction(action=AllowedAction.REQUEST_REPLACEMENT))
    assert decision.decision is Decision.BLOCKED
    assert "replacement authority" in decision.reason


def test_replacement_allowed_when_authority_enabled(monkeypatch):
    _set_replacement_switch(monkeypatch, enabled=True)
    assert (
        evaluate(_mandate(), ProposedAction(action=AllowedAction.REQUEST_REPLACEMENT)).decision
        is Decision.APPROVED
    )


def test_refund_authority_does_not_grant_replacement_authority(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "shopping_refund_authority_enabled", True)
    monkeypatch.setattr(settings, "shopping_refund_max_inr", Decimal("500"))
    monkeypatch.setattr(settings, "shopping_replacement_authority_enabled", False)
    mandate = _mandate(allowed_actions=[AllowedAction.REQUEST_REFUND])
    # Refund is delegated and authorized...
    assert (
        evaluate(
            mandate, ProposedAction(action=AllowedAction.REQUEST_REFUND, amount=Decimal("399"))
        ).decision
        is Decision.APPROVED
    )
    # ...and replacement is still unauthorized both ways: not delegated, switch off.
    assert (
        evaluate(mandate, ProposedAction(action=AllowedAction.REQUEST_REPLACEMENT)).decision
        is Decision.BLOCKED
    )
    delegated = _mandate(
        allowed_actions=[AllowedAction.REQUEST_REFUND, AllowedAction.REQUEST_REPLACEMENT]
    )
    assert (
        evaluate(delegated, ProposedAction(action=AllowedAction.REQUEST_REPLACEMENT)).decision
        is Decision.BLOCKED
    )


def test_replacement_switch_does_not_grant_refund_authority(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "shopping_replacement_authority_enabled", True)
    monkeypatch.setattr(settings, "shopping_refund_authority_enabled", False)
    mandate = _mandate(allowed_actions=[AllowedAction.REQUEST_REFUND])
    assert (
        evaluate(
            mandate, ProposedAction(action=AllowedAction.REQUEST_REFUND, amount=Decimal("399"))
        ).decision
        is Decision.BLOCKED
    )


# --------------------------------------------------------------------------- #
# Items 19, 24 (default reality): no provider connected -> explicit, no false success
# --------------------------------------------------------------------------- #
async def test_no_provider_connected_is_explicit_and_unresolved(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    async with client:
        mission_id = await mission_setup(client)
        issue_id = await _authorized_replacement_issue(client, test_session, mission_id)

        resp = await _replace(client, mission_id, issue_id)
        assert resp.status_code == 503
        data = resp.json()
        # The 503 detail is a statement of missing capability, not a completed action.
        assert "No merchant replacement provider is connected" in data["detail"]

        rows = await client.get(f"/missions/{mission_id}/order/issues")
        issue = [i for i in rows.json()["issues"] if i["issue_id"] == issue_id][0]
        replacement = issue["replacement"]
        assert replacement["status"] == "pending"
        assert replacement["provider"] == "none"
        assert replacement["provider_reference"] is None
        assert replacement["confirmed_at"] is None
        assert "no merchant replacement order exists" in replacement["replacement_note"]
        # The issue is NOT resolved or requested — it stays under review.
        assert issue["status"] == "under_review"

        # Repeating the call must not multiply records or claim progress.
        again = await _replace(client, mission_id, issue_id)
        assert again.status_code == 503
        assert again.json()["replacement_id"] == replacement["replacement_id"]
        assert again.json()["already_existed"] is True

        async with test_session() as session:
            count = await session.scalar(
                select(func.count()).select_from(ReplacementRecord).where(
                    ReplacementRecord.mission_id == mission_id
                )
            )
            assert count == 1


# --------------------------------------------------------------------------- #
# Items 2, 15, 16, 17, 23, 24, 25: with a real-shaped provider, state follows the provider
# --------------------------------------------------------------------------- #
async def test_accepted_request_becomes_requested_never_confirmed(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning,
    replacement_provider, recorded_events,
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    async with client:
        mission_id = await mission_setup(client)
        issue_id = await _authorized_replacement_issue(client, test_session, mission_id)

        resp = await _replace(client, mission_id, issue_id)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "requested"
        assert data["provider"] == "fake"
        assert data["provider_reference"] == "MERC-REPL-1"
        # Canonical product identity, copied from the persisted order — nothing client-supplied.
        assert data["original_product_id"] == "item_dove"
        assert data["original_title"] == "Dove Hair Fall Rescue Shampoo"
        assert data["original_merchant"] == "Nykaa"
        assert data["replacement_product_id"] == "item_dove"
        assert Decimal(str(data["value"])) == Decimal("399.00")
        assert data["quantity"] == 1 and data["currency"] == "INR"
        assert "NOT" in data["replacement_note"]  # never claims shipment/delivery

        sent = replacement_provider.requests[0]
        assert sent.product_id == "item_dove" and sent.title == "Dove Hair Fall Rescue Shampoo"
        assert sent.order_id == data["order_id"] and sent.mission_id == mission_id

        issues = (await client.get(f"/missions/{mission_id}/order/issues")).json()["issues"]
        issue = [i for i in issues if i["issue_id"] == issue_id][0]
        assert issue["status"] == "replacement_requested"

        # Item 16: a repeat does NOT re-submit; it reads the provider's state instead.
        second = await _replace(client, mission_id, issue_id)
        assert second.json()["status"] == "requested"
        assert len(replacement_provider.requests) == 1
        assert replacement_provider.status_reads == ["MERC-REPL-1"]

        # Item 17: once the provider confirms, repeats return the record with no provider call.
        replacement_provider.outcome = "completed"
        third = await _replace(client, mission_id, issue_id)
        assert third.json()["status"] == "confirmed" and third.json()["confirmed_at"]
        calls_before = len(replacement_provider.status_reads)
        fourth = await _replace(client, mission_id, issue_id)
        assert fourth.json()["status"] == "confirmed"
        assert fourth.json()["already_existed"] is True
        assert len(replacement_provider.status_reads) == calls_before

        # Item 23: audit records the action, decision, provider, reference and status.
        async with test_session() as session:
            audits = (
                await session.scalars(
                    select(AuditLogRecord).where(AuditLogRecord.mission_id == mission_id)
                )
            ).all()
        entry = [a for a in audits if "replacement " in (a.reason or "")][-1]
        assert entry.action == "request_replacement"
        assert "provider=fake" in entry.reason
        assert "MERC-REPL-1" in entry.reason and "status=confirmed" in entry.reason

    # Item 24: events mirror persisted transitions only.
    names = [e["event"] for e in recorded_events if e["event"].startswith("replacement_")]
    assert names == ["replacement_pending", "replacement_requested", "replacement_confirmed"]


async def test_replacement_state_survives_restart(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning,
    replacement_provider,
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    async with client:
        mission_id = await mission_setup(client)
        issue_id = await _authorized_replacement_issue(client, test_session, mission_id)
        resp = await _replace(client, mission_id, issue_id)
        replacement_id = resp.json()["replacement_id"]

    client_b = restart_env[0]()
    async with client_b as second_client:
        await _login(second_client, "beta@shop.com")
        issues = (await second_client.get(f"/missions/{mission_id}/order/issues")).json()["issues"]
        issue = [i for i in issues if i["issue_id"] == issue_id][0]
        assert issue["replacement"]["replacement_id"] == replacement_id
        assert issue["replacement"]["status"] == "requested"
        assert issue["status"] == "replacement_requested"
    assert len(replacement_provider.requests) == 1


async def test_provider_rejection_persists_failed_safely(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning,
    replacement_provider, recorded_events,
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    replacement_provider.error = ReplacementProviderRejected("merchant is out of stock")
    async with client:
        mission_id = await mission_setup(client)
        issue_id = await _authorized_replacement_issue(client, test_session, mission_id)
        resp = await _replace(client, mission_id, issue_id)
        assert resp.status_code == 502
        data = resp.json()
        assert data["status"] == "failed"
        assert "out of stock" in data["error_reason"]
        assert data["provider_reference"] is None
        issue = [
            i for i in (await client.get(f"/missions/{mission_id}/order/issues")).json()["issues"]
            if i["issue_id"] == issue_id
        ][0]
        assert issue["status"] == "under_review"  # never advanced on a failed request

        # Item 9/D: retry goes through the SAME record once the provider can accept.
        replacement_provider.error = None
        retry = await _replace(client, mission_id, issue_id)
        assert retry.json()["replacement_id"] == data["replacement_id"]
        assert retry.json()["status"] == "requested"
        async with test_session() as session:
            count = await session.scalar(
                select(func.count()).select_from(ReplacementRecord).where(
                    ReplacementRecord.mission_id == mission_id
                )
            )
            assert count == 1
    assert [e["event"] for e in recorded_events if e["event"].startswith("replacement_")] == [
        "replacement_pending",
        "replacement_failed",
        "replacement_requested",
    ]


async def test_unaccepted_ack_never_marks_requested(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning,
    replacement_provider,
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    replacement_provider.accepted = False
    async with client:
        mission_id = await mission_setup(client)
        issue_id = await _authorized_replacement_issue(client, test_session, mission_id)
        resp = await _replace(client, mission_id, issue_id)
        assert resp.status_code == 200  # a 200 here reports a failed request, honestly
        assert resp.json()["status"] == "failed"
        assert resp.json()["provider_reference"] is None
        issue = [
            i for i in (await client.get(f"/missions/{mission_id}/order/issues")).json()["issues"]
            if i["issue_id"] == issue_id
        ][0]
        assert issue["status"] == "under_review"


# --------------------------------------------------------------------------- #
# Items 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 18: guards, with zero provider contact
# --------------------------------------------------------------------------- #
async def test_guards_reject_before_any_provider_call(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning,
    replacement_provider, monkeypatch,
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    async with client:
        mission_id = await mission_setup(client)

        # 12: no assessment yet
        plain_issue = await _issue(client, mission_id)
        resp = await _replace(client, mission_id, plain_issue)
        assert resp.status_code == 400 and "assessed" in resp.json()["detail"]

        # 1 (end-to-end): authority disabled -> assessment denies, endpoint refuses
        _set_replacement_switch(monkeypatch, enabled=False)
        disabled_issue = await _issue(client, mission_id)
        denied_assessment = await _assess(client, mission_id, disabled_issue)
        assert denied_assessment["authority_decision"] == "deny"
        resp = await _replace(client, mission_id, disabled_issue)
        assert resp.status_code == 400 and "explicit allow" in resp.json()["detail"]
        _set_replacement_switch(monkeypatch, enabled=True)

        # 4: a refund proposal cannot use the replacement endpoint
        refund_issue = await _issue(client, mission_id, kind="refund")
        install_fake_reasoning(
            {
                **_replacement_proposal(),
                "proposed_action": "request_refund",
                "requested_amount": 399,
            }
        )
        await _delegate(test_session, mission_id, ["request_refund"])
        await _assess(client, mission_id, refund_issue)
        resp = await _replace(client, mission_id, refund_issue)
        assert resp.status_code == 400 and "request_replacement" in resp.json()["detail"]

        # 5: a return proposal likewise
        install_fake_reasoning({**_replacement_proposal(), "proposed_action": "request_return"})
        return_issue = await _issue(client, mission_id)
        await _delegate(test_session, mission_id, ["request_return"])
        ra = await _assess(client, mission_id, return_issue)
        assert ra["proposed_action"] == "request_return"
        resp = await _replace(client, mission_id, return_issue)
        assert resp.status_code == 400 and "request_replacement" in resp.json()["detail"]

        # 7: an ESCALATE decision on a replacement proposal blocks execution
        install_fake_reasoning(_replacement_proposal())
        esc_issue = await _authorized_replacement_issue(client, test_session, mission_id)
        assessment_id = (await _assess(client, mission_id, esc_issue))["assessment_id"]
        await _set_assessment_decision(test_session, assessment_id, "escalate")
        resp = await _replace(client, mission_id, esc_issue)
        assert resp.status_code == 400 and "explicit allow" in resp.json()["detail"]

        # Execution-time revocation: a fresh, genuinely ALLOWed issue whose delegation is
        # withdrawn afterwards — the stored checks pass, the fresh gate call refuses.
        rg_issue = await _authorized_replacement_issue(client, test_session, mission_id)
        await _delegate(test_session, mission_id, [])
        resp = await _replace(client, mission_id, rg_issue)
        assert resp.status_code == 400 and "at execution time" in resp.json()["detail"]

        # 13 + 14: a body cannot choose the product, quantity, or authority
        await _delegate(test_session, mission_id, ["request_replacement"])
        ok_issue = await _authorized_replacement_issue(client, test_session, mission_id)
        resp = await _replace(
            client,
            mission_id,
            ok_issue,
            json={
                "product_id": "item_premium",
                "quantity": 50,
                "merchant": "Somebody Else",
                "authority_decision": "allow",
                "provider_reference": "HACK-1",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        # Canonical product from the persisted order won; none of the body's values applied.
        assert data["original_product_id"] == "item_dove"
        assert data["replacement_product_id"] == "item_dove"
        assert data["quantity"] == 1
        assert data["original_merchant"] == "Nykaa"
        assert data["provider_reference"] == "MERC-REPL-1"
        assert replacement_provider.requests[0].quantity == 1

        async with test_session() as session:
            rows = (
                await session.scalars(
                    select(ReplacementRecord).where(ReplacementRecord.mission_id == mission_id)
                )
            ).all()
            # Only the attempt that passed every guard created a row.
            assert len(rows) == 1

        # 8: non-owner and anonymous
        await _register(client, "Mallory", "mallory@shop.com")
        assert (await _replace(client, mission_id, ok_issue)).status_code == 404
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anon:
            assert (await _replace(anon, mission_id, ok_issue)).status_code == 404
        await _login(client, "beta@shop.com")

        # 11: unknown issue
        assert (await _replace(client, mission_id, "issue_nope123456")).status_code == 404

        # 9: non-shopping mission
        recovery = await client.post(
            "/missions",
            json={
                "mission_type": "recovery",
                "objective": {"description": "Recover a package", "success_criteria": []},
                "authority": {
                    "allowed_actions": ["request_replacement"],
                    "recovery_budget": "5000",
                },
                "transaction": {
                    "merchant_name": "Store",
                    "order_id": "o-1",
                    "amount_paid": "100",
                },
            },
        )
        assert (
            await _replace(client, recovery.json()["mission_id"], ok_issue)
        ).status_code == 400

        # 10: a shopping mission with no order record
        resp = await client.post("/missions/shopping", json={"prompt": "shampoo under 600"})
        no_order = resp.json()["mission_id"]
        assert (await _replace(client, no_order, ok_issue)).status_code == 404


# --------------------------------------------------------------------------- #
# Items 20, 21: refund and replacement cannot both resolve one issue
# --------------------------------------------------------------------------- #
async def test_verified_refund_blocks_replacement(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning,
    replacement_provider, refund_api,
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    async with client:
        mission_id = await mission_setup(client)
        # Refund path first: assess as refund, allow, execute and verify.
        issue_id = await _confirmed_order_with_issue(client, mission_id)
        install_fake_reasoning(
            {
                **_replacement_proposal(),
                "proposed_action": "request_refund",
                "requested_amount": 399,
            }
        )
        await _delegate(test_session, mission_id, ["request_refund"], refund_limit="500")
        await _assess(client, mission_id, issue_id)
        assert (await _refund(client, mission_id, issue_id)).status_code == 200
        issues = (await client.get(f"/missions/{mission_id}/order/issues")).json()["issues"]
        assert issues[0]["refund"]["status"] == "verified"

        # Now make the persisted assessment say "replacement" (test data — as if the model had
        # proposed that on an issue whose payment was already reversed). The conflict guard must
        # still hold, and the provider must never be touched.
        from app.models.db_models import ResolutionAssessmentRecord

        async with test_session() as session:
            row = await session.scalar(
                select(ResolutionAssessmentRecord).where(
                    ResolutionAssessmentRecord.issue_id == issue_id
                )
            )
            row.proposed_action = "request_replacement"
            row.authority_decision = "allow"
            await session.commit()

        resp = await _replace(client, mission_id, issue_id)
        assert resp.status_code == 400 and "verified refund" in resp.json()["detail"]
    assert replacement_provider.requests == []


async def test_confirmed_replacement_blocks_refund(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning,
    replacement_provider, refund_api,
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    async with client:
        mission_id = await mission_setup(client)
        issue_id = await _authorized_replacement_issue(client, test_session, mission_id)
        assert (await _replace(client, mission_id, issue_id)).status_code == 200

        # Flip the assessment to a refund proposal + allow, as if the model had proposed that.
        from app.models.db_models import ResolutionAssessmentRecord

        async with test_session() as session:
            row = await session.scalar(
                select(ResolutionAssessmentRecord).where(
                    ResolutionAssessmentRecord.issue_id == issue_id
                )
            )
            row.proposed_action = "request_refund"
            row.authority_decision = "allow"
            row.requested_amount = Decimal("399.00")
            await session.commit()

        await _delegate(test_session, mission_id, ["request_refund", "request_replacement"], refund_limit="500")
        resp = await _refund(client, mission_id, issue_id)
        assert resp.status_code == 400 and "replacement" in resp.json()["detail"]
    assert refund_api.calls["create"] == 0


# --------------------------------------------------------------------------- #
# Item 22: no secret leakage
# --------------------------------------------------------------------------- #
async def test_no_secret_leakage(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning,
    replacement_provider,
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    secret = get_settings().razorpay_key_secret
    replacement_provider.error = ReplacementProviderRejected(
        f"merchant refused; token={secret}"
    )
    async with client:
        mission_id = await mission_setup(client)
        issue_id = await _authorized_replacement_issue(client, test_session, mission_id)
        body = (await _replace(client, mission_id, issue_id)).json()
        assert secret not in str(body)
        assert "[redacted]" in body["error_reason"]

        issues_raw = str(
            (await client.get(f"/missions/{mission_id}/order/issues")).json()
        )
        assert secret not in issues_raw

        async with test_session() as session:
            audits = (
                await session.scalars(
                    select(AuditLogRecord).where(AuditLogRecord.mission_id == mission_id)
                )
            ).all()
            assert audits
            assert all(secret not in (a.reason or "") for a in audits)
