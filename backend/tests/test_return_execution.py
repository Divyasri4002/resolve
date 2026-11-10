"""
Tests for Task 12B: controlled return execution.

Pipeline under test:
    assessment (Task 10) -> Authority Engine (bounded RETURN authority, its own switch)
    -> return executor -> ReturnProvider -> persisted state.

Two facts kept honest by this suite:
  * No merchant return provider exists in this project, so the default provider refuses
    explicitly. The executor must record intent + 'pending' + HTTP 503 and never claim a return.
  * A test-only fake stands in for a future real one, installed through the same seam production
    uses; it cannot be selected by configuration.
"""
import uuid
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.authority.engine import Decision, ProposedAction, evaluate
from app.config import get_settings
from app.main import app
from app.models.db_models import (
    AuditLogRecord,
    RefundRecord,
    ReplacementRecord,
    ResolveOrderRecord,
    ReturnRecord,
)
from app.models.mission import AllowedAction, Authority
from app.routers import missions as missions_module
from app.tools.returns.base import (
    ReturnAck,
    ReturnOutcomeUnknown,
    ReturnProviderRejected,
    ReturnProviderUnavailable,
    ReturnReconciliation,
    ReturnStatusInfo,
    ReturnTarget,
)

from tests.test_refund_authority import _delegate, _verified_mission_product  # noqa: F401
from tests.test_refund_execution import (  # noqa: F401
    _confirmed_order_with_issue,
    _login,
    _refund,
    _register,
    install_fake_reasoning,
    mock_razorpay,
    recorded_events,
    refund_api,
    restart_env,
)
from tests.test_replacement_execution import (  # noqa: F401
    _assess,
    _issue,
    _replace,
    replacement_provider,
)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
class FakeReturnProvider:
    """
    Test-only stand-in for a future merchant return (RMA) provider.

    It implements the whole hardened contract (Task 14C.4) so the executor's ordering is what is
    under test: resolve a target, claim it, submit, and — if the answer was lost — RECONCILE by
    reading rather than submitting again. Every call is counted, so a test can prove a second
    mutation did not happen rather than assert that it probably did not.
    """

    name = "fake"

    def __init__(self):
        self.requests = []
        self.targets_sent = []
        self.status_reads = []
        self.resolutions = []
        self.reconciliations = []
        self.accepted = True
        self.reference = "RMA-FAKE-1"
        self.error: Exception | None = None
        self.status_error: Exception | None = None
        self.target: ReturnTarget | None = None
        self.target_error: Exception | None = None
        self.reconcile_result: ReturnReconciliation = ReturnReconciliation.no_match(
            "fake: nothing to match"
        )
        self.reconcile_error: Exception | None = None
        self.outcome = "accepted"

    async def resolve_return_target(self, request):
        self.resolutions.append(request)
        if self.target_error is not None:
            raise self.target_error
        return self.target

    async def request_return(self, request, target=None):
        self.requests.append(request)
        self.targets_sent.append(target)
        if self.error is not None:
            raise self.error
        return ReturnAck(provider_reference=self.reference, accepted=self.accepted)

    async def get_return_status(self, provider_reference):
        self.status_reads.append(provider_reference)
        if self.status_error is not None:
            raise self.status_error
        return ReturnStatusInfo(provider_reference=provider_reference, outcome=self.outcome)

    async def reconcile_return(self, submission):
        self.reconciliations.append(submission)
        if self.reconcile_error is not None:
            raise self.reconcile_error
        return self.reconcile_result


@pytest.fixture
def return_provider(monkeypatch):
    provider = FakeReturnProvider()
    monkeypatch.setattr(
        missions_module, "get_return_provider", lambda provider_name=None: provider
    )
    return provider


def _return_proposal() -> dict:
    return {
        "issue_assessment": "The wrong variant was delivered; the merchant should take it back.",
        "proposed_action": "request_return",
        "reasoning_summary": "Return proposed by the resolution model.",
        "requested_amount": None,
        "currency": "INR",
        "confidence": 0.8,
        "needs_escalation": False,
    }


def _set_return_switch(monkeypatch, enabled: bool):
    monkeypatch.setattr(get_settings(), "shopping_return_authority_enabled", enabled)


def _mandate(**overrides) -> Authority:
    base = dict(allowed_actions=[AllowedAction.REQUEST_RETURN], recovery_budget=Decimal("0"))
    base.update(overrides)
    return Authority(**base)


async def _ret(client, mission_id, issue_id, json=None):
    url = f"/missions/{mission_id}/order/issues/{issue_id}/return"
    return await client.post(url, json=json) if json is not None else await client.post(url)


async def _set_assessment(test_session, assessment_id, **values):
    from app.models.db_models import ResolutionAssessmentRecord

    async with test_session() as session:
        row = await session.get(ResolutionAssessmentRecord, assessment_id)
        for key, value in values.items():
            setattr(row, key, value)
        await session.commit()


async def _authorized_return_issue(client, test_session, mission_id) -> str:
    """Canonical mission -> issue -> ALLOWed request_return assessment."""
    issue_id = await _issue(client, mission_id, kind="none")
    await _delegate(test_session, mission_id, ["request_return"])
    assessment = await _assess(client, mission_id, issue_id)
    assert assessment["proposed_action"] == "request_return"
    assert assessment["authority_decision"] == "allow"
    return issue_id


async def _setup(restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake):
    """Mission factory that creates a verified-payment mission and only THEN installs the
    controlled return proposal (mission creation needs the hermetic mock reasoning provider)."""
    make_client, test_session = restart_env
    client = make_client()

    async def _mission(c):
        mission_id = await _verified_mission_product(
            c, install_mock_discovery, sample_products, mock_razorpay, "item_dove", "pay_T1"
        )
        install_fake(_return_proposal())
        return mission_id

    return client, test_session, _mission


# --------------------------------------------------------------------------- #
# ENGINE: return authority is its own axis
# --------------------------------------------------------------------------- #
def test_return_requires_its_own_authority(monkeypatch):
    _set_return_switch(monkeypatch, enabled=False)
    decision = evaluate(_mandate(), ProposedAction(action=AllowedAction.REQUEST_RETURN))
    assert decision.decision is Decision.BLOCKED
    assert "return authority" in decision.reason


def test_return_allowed_when_authority_enabled(monkeypatch):
    _set_return_switch(monkeypatch, enabled=True)
    assert (
        evaluate(_mandate(), ProposedAction(action=AllowedAction.REQUEST_RETURN)).decision
        is Decision.APPROVED
    )


def test_other_authorities_never_grant_return_authority(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "shopping_refund_authority_enabled", True)
    monkeypatch.setattr(settings, "shopping_refund_max_inr", Decimal("500"))
    monkeypatch.setattr(settings, "shopping_replacement_authority_enabled", True)
    monkeypatch.setattr(settings, "shopping_return_authority_enabled", False)
    mandate = _mandate(
        allowed_actions=[
            AllowedAction.REQUEST_REFUND,
            AllowedAction.REQUEST_REPLACEMENT,
            AllowedAction.REQUEST_RETURN,
        ]
    )
    assert (
        evaluate(
            mandate, ProposedAction(action=AllowedAction.REQUEST_REFUND, amount=Decimal("399"))
        ).decision
        is Decision.APPROVED
    )
    assert (
        evaluate(mandate, ProposedAction(action=AllowedAction.REQUEST_REPLACEMENT)).decision
        is Decision.APPROVED
    )
    # ...and return is still denied: its switch is off.
    assert (
        evaluate(mandate, ProposedAction(action=AllowedAction.REQUEST_RETURN)).decision
        is Decision.BLOCKED
    )


def test_return_authority_grants_neither_refund_nor_replacement(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "shopping_return_authority_enabled", True)
    monkeypatch.setattr(settings, "shopping_refund_authority_enabled", False)
    monkeypatch.setattr(settings, "shopping_replacement_authority_enabled", False)
    mandate = _mandate(allowed_actions=[AllowedAction.REQUEST_RETURN])
    assert (
        evaluate(
            mandate, ProposedAction(action=AllowedAction.REQUEST_REFUND, amount=Decimal("399"))
        ).decision
        is Decision.BLOCKED
    )
    assert (
        evaluate(mandate, ProposedAction(action=AllowedAction.REQUEST_REPLACEMENT)).decision
        is Decision.BLOCKED
    )


# --------------------------------------------------------------------------- #
# C, G: no provider connected -> explicit, pending, idempotent, no false claim
# --------------------------------------------------------------------------- #
async def test_no_provider_connected_stays_pending_and_unresolved(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning,
    recorded_events,
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    async with client:
        mission_id = await mission_setup(client)
        issue_id = await _authorized_return_issue(client, test_session, mission_id)

        resp = await _ret(client, mission_id, issue_id)
        assert resp.status_code == 503
        data = resp.json()
        assert "No merchant return provider is connected" in data["detail"]
        assert data["status"] == "pending"
        assert data["provider"] == "none"
        assert data["provider_reference"] is None
        assert data["requested_at"] is None and data["confirmed_at"] is None
        assert "no merchant return exists" in data["return_note"]

        issue = [
            i for i in (await client.get(f"/missions/{mission_id}/order/issues")).json()["issues"]
            if i["issue_id"] == issue_id
        ][0]
        assert issue["status"] == "under_review"  # never advanced without a provider
        assert issue["return"]["return_id"] == data["return_id"]

        # G: a repeat reuses the same row and claims nothing new.
        again = await _ret(client, mission_id, issue_id)
        assert again.status_code == 503
        assert again.json()["return_id"] == data["return_id"]
        assert again.json()["already_existed"] is True
        async with test_session() as session:
            count = await session.scalar(
                select(func.count()).select_from(ReturnRecord).where(
                    ReturnRecord.mission_id == mission_id
                )
            )
            assert count == 1
    assert [e["event"] for e in recorded_events if e["event"].startswith("return_")] == [
        "return_pending"
    ]


# --------------------------------------------------------------------------- #
# D, E, audit, WS, issue state: state follows the provider and nothing else
# --------------------------------------------------------------------------- #
async def test_provider_accepted_then_confirmed(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning,
    return_provider, recorded_events,
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    async with client:
        mission_id = await mission_setup(client)
        issue_id = await _authorized_return_issue(client, test_session, mission_id)

        resp = await _ret(client, mission_id, issue_id)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "requested"
        assert data["provider"] == "fake"
        assert data["provider_reference"] == "RMA-FAKE-1"
        assert data["requested_at"] and data["confirmed_at"] is None
        assert data["original_product_id"] == "item_dove"
        assert data["original_title"] == "Dove Hair Fall Rescue Shampoo"
        assert "NOT an approved" in data["return_note"]

        sent = return_provider.requests[0]
        assert sent.product_id == "item_dove" and sent.order_id == data["order_id"]
        assert "Wrong variant arrived" in sent.reason  # canonical persisted issue text
        issue = [
            i for i in (await client.get(f"/missions/{mission_id}/order/issues")).json()["issues"]
            if i["issue_id"] == issue_id
        ][0]
        assert issue["status"] == "return_requested"

        # A repeat reads the provider's state instead of re-requesting.
        second = await _ret(client, mission_id, issue_id)
        assert second.json()["status"] == "requested"
        assert len(return_provider.requests) == 1
        assert return_provider.status_reads == ["RMA-FAKE-1"]

        # E: only the provider's own confirmation moves it to confirmed.
        return_provider.outcome = "confirmed"
        third = await _ret(client, mission_id, issue_id)
        assert third.json()["status"] == "confirmed" and third.json()["confirmed_at"]
        before = len(return_provider.status_reads)
        fourth = await _ret(client, mission_id, issue_id)
        assert fourth.json()["status"] == "confirmed"
        assert fourth.json()["already_existed"] is True
        assert len(return_provider.status_reads) == before  # confirmed: no provider call at all

        async with test_session() as session:
            audits = (
                await session.scalars(
                    select(AuditLogRecord).where(AuditLogRecord.mission_id == mission_id)
                )
            ).all()
        entry = [a for a in audits if "return " in (a.reason or "")][-1]
        assert entry.action == "request_return"
        assert "provider=fake" in entry.reason and "RMA-FAKE-1" in entry.reason
        assert "status=confirmed" in entry.reason
    assert [e["event"] for e in recorded_events if e["event"].startswith("return_")] == [
        "return_pending",
        "return_requested",
        "return_confirmed",
    ]


async def test_return_state_survives_restart(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning,
    return_provider,
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    async with client:
        mission_id = await mission_setup(client)
        issue_id = await _authorized_return_issue(client, test_session, mission_id)
        return_id = (await _ret(client, mission_id, issue_id)).json()["return_id"]

    async with restart_env[0]() as second_client:
        await _login(second_client, "beta@shop.com")
        issue = [
            i for i in (await second_client.get(f"/missions/{mission_id}/order/issues")).json()["issues"]
            if i["issue_id"] == issue_id
        ][0]
        assert issue["return"]["return_id"] == return_id
        assert issue["return"]["status"] == "requested"
        assert issue["status"] == "return_requested"
    assert len(return_provider.requests) == 1


# --------------------------------------------------------------------------- #
# F: provider rejection; and an ack that does not accept
# --------------------------------------------------------------------------- #
async def test_provider_rejection_persisted_and_retried_same_row(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning,
    return_provider, recorded_events,
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    return_provider.error = ReturnProviderRejected("return window has closed")
    async with client:
        mission_id = await mission_setup(client)
        issue_id = await _authorized_return_issue(client, test_session, mission_id)
        resp = await _ret(client, mission_id, issue_id)
        assert resp.status_code == 502
        data = resp.json()
        assert data["status"] == "failed"
        assert "return window has closed" in data["failure_reason"]
        assert data["provider_reference"] is None
        issue = [
            i for i in (await client.get(f"/missions/{mission_id}/order/issues")).json()["issues"]
            if i["issue_id"] == issue_id
        ][0]
        assert issue["status"] == "under_review"

        return_provider.error = None
        retry = await _ret(client, mission_id, issue_id)
        assert retry.json()["return_id"] == data["return_id"]
        assert retry.json()["status"] == "requested"
        async with test_session() as session:
            count = await session.scalar(
                select(func.count()).select_from(ReturnRecord).where(
                    ReturnRecord.mission_id == mission_id
                )
            )
            assert count == 1
    assert [e["event"] for e in recorded_events if e["event"].startswith("return_")] == [
        "return_pending",
        "return_failed",
        "return_requested",
    ]


async def test_unaccepted_ack_never_marks_requested(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning,
    return_provider,
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    return_provider.accepted = False
    async with client:
        mission_id = await mission_setup(client)
        issue_id = await _authorized_return_issue(client, test_session, mission_id)
        resp = await _ret(client, mission_id, issue_id)
        assert resp.json()["status"] == "failed"
        assert resp.json()["provider_reference"] is None


# --------------------------------------------------------------------------- #
# A, B, H, I, J, K, L, O: guards first, provider never touched
# --------------------------------------------------------------------------- #
async def test_guards_reject_before_any_provider_call(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning,
    return_provider, monkeypatch,
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    async with client:
        mission_id = await mission_setup(client)

        # J: no assessment yet
        plain = await _issue(client, mission_id, kind="none")
        resp = await _ret(client, mission_id, plain)
        assert resp.status_code == 400 and "assessed" in resp.json()["detail"]

        # A: deployment return authority off -> assessment denies, endpoint refuses
        _set_return_switch(monkeypatch, enabled=False)
        disabled = await _issue(client, mission_id, kind="none")
        denied = await _assess(client, mission_id, disabled)
        assert denied["authority_decision"] == "deny"
        resp = await _ret(client, mission_id, disabled)
        assert resp.status_code == 400 and "explicit allow" in resp.json()["detail"]
        _set_return_switch(monkeypatch, enabled=True)

        # B: authority on but request_return not delegated
        undelegated = await _issue(client, mission_id, kind="none")
        await _delegate(test_session, mission_id, ["request_replacement"])
        denied2 = await _assess(client, mission_id, undelegated)
        assert denied2["authority_decision"] == "escalate"
        assert "never delegated" in denied2["decision_reason"]
        resp = await _ret(client, mission_id, undelegated)
        assert resp.status_code == 400 and "explicit allow" in resp.json()["detail"]

        # K: an assessment proposing a different action cannot use the return endpoint
        install_fake_reasoning(
            {**_return_proposal(), "proposed_action": "request_refund", "requested_amount": 399}
        )
        refund_issue = await _issue(client, mission_id, kind="refund")
        await _delegate(test_session, mission_id, ["request_refund"], refund_limit="500")
        await _assess(client, mission_id, refund_issue)
        resp = await _ret(client, mission_id, refund_issue)
        assert resp.status_code == 400 and "request_return" in resp.json()["detail"]
        install_fake_reasoning(_return_proposal())

        # L: a stale stored approval cannot bypass the current Authority Engine
        stale = await _authorized_return_issue(client, test_session, mission_id)
        _set_return_switch(monkeypatch, enabled=False)
        resp = await _ret(client, mission_id, stale)
        assert resp.status_code == 400 and "at execution time" in resp.json()["detail"]
        _set_return_switch(monkeypatch, enabled=True)

        # O: a body cannot inject amount / product / order id / provider ref / authority
        ok = await _authorized_return_issue(client, test_session, mission_id)
        resp = await _ret(
            client,
            mission_id,
            ok,
            json={
                "amount": 99999,
                "product_id": "item_premium",
                "order_id": "order_fake",
                "provider_reference": "HACK-1",
                "authority_decision": "allow",
                "proposed_action": "request_refund",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["original_product_id"] == "item_dove"
        assert data["provider_reference"] == "RMA-FAKE-1"
        assert return_provider.requests[-1].product_id == "item_dove"

        # H: non-owner and anonymous -> 404
        await _register(client, "Mallory", "mallory@shop.com")
        assert (await _ret(client, mission_id, ok)).status_code == 404
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anon:
            assert (await _ret(anon, mission_id, ok)).status_code == 404
        await _login(client, "beta@shop.com")

        # unknown issue -> 404
        assert (await _ret(client, mission_id, "issue_nope123456")).status_code == 404

        # I: non-shopping mission -> 400
        recovery = await client.post(
            "/missions",
            json={
                "mission_type": "recovery",
                "objective": {"description": "Recover a package", "success_criteria": []},
                "authority": {"allowed_actions": ["request_return"], "recovery_budget": "5000"},
                "transaction": {
                    "merchant_name": "Store",
                    "order_id": "o-1",
                    "amount_paid": "100",
                },
            },
        )
        assert (await _ret(client, recovery.json()["mission_id"], ok)).status_code == 400

        # missing order -> 404
        fresh = await client.post("/missions/shopping", json={"prompt": "shampoo under 600"})
        assert (await _ret(client, fresh.json()["mission_id"], ok)).status_code == 404

        # None of the rejected attempts created rows: only the two accepted ones did.
        async with test_session() as session:
            rows = (
                await session.scalars(
                    select(ReturnRecord).where(ReturnRecord.mission_id == mission_id)
                )
            ).all()
            assert len(rows) == 1
            # A return consumes no recovery budget and creates no refund/replacement row.
            from app.models.db_models import MissionRecord

            mission = await session.get(MissionRecord, mission_id)
            authority = mission.mandate["authority"]
            assert authority["spent_so_far"] == "0"  # a return consumes no recovery budget
            assert await session.scalar(
                select(func.count()).select_from(RefundRecord)
            ) == 0
            assert await session.scalar(
                select(func.count()).select_from(ReplacementRecord)
            ) == 0
    assert len(return_provider.requests) == 1


# --------------------------------------------------------------------------- #
# M, N: conflicts with the other two executors (Task 12A semantics extended)
# --------------------------------------------------------------------------- #
async def test_verified_refund_blocks_return(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning,
    return_provider, refund_api,
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    async with client:
        mission_id = await mission_setup(client)
        issue_id = await _confirmed_order_with_issue(client, mission_id)
        install_fake_reasoning(
            {**_return_proposal(), "proposed_action": "request_refund", "requested_amount": 399}
        )
        await _delegate(test_session, mission_id, ["request_refund"], refund_limit="500")
        assessment = await _assess(client, mission_id, issue_id)
        assert assessment["authority_decision"] == "allow"
        assert (await _refund(client, mission_id, issue_id)).status_code == 200

        # Flip the persisted assessment to a return proposal: the conflict guard must still hold.
        await _set_assessment(
            test_session, assessment["assessment_id"],
            proposed_action="request_return", authority_decision="allow",
        )
        resp = await _ret(client, mission_id, issue_id)
        assert resp.status_code == 400 and "verified refund" in resp.json()["detail"]
    assert return_provider.requests == []


async def test_requested_replacement_blocks_return(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning,
    return_provider, replacement_provider,
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    async with client:
        mission_id = await mission_setup(client)
        # Replacement first, through its own authorized path.
        install_fake_reasoning(
            {**_return_proposal(), "proposed_action": "request_replacement"}
        )
        issue_id = await _issue(client, mission_id, kind="replacement")
        await _delegate(test_session, mission_id, ["request_replacement", "request_return"])
        assessment = await _assess(client, mission_id, issue_id)
        assert assessment["authority_decision"] == "allow"
        assert (await _replace(client, mission_id, issue_id)).json()["status"] == "requested"

        # Now a return on the same issue: blocked, provider untouched.
        await _set_assessment(
            test_session, assessment["assessment_id"],
            proposed_action="request_return", authority_decision="allow",
        )
        resp = await _ret(client, mission_id, issue_id)
        assert resp.status_code == 400 and "replacement" in resp.json()["detail"]
    assert return_provider.requests == []


async def test_pending_replacement_does_not_block_return(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning,
):
    """Task 12A semantics: a replacement that was never submitted resolved nothing, so it must
    not lock the issue. Uses the REAL default provider, so the outcome is provider-unavailable."""
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    async with client:
        mission_id = await mission_setup(client)
        issue_id = await _authorized_return_issue(client, test_session, mission_id)
        # Create a pending replacement row directly (no provider connected for replacements).
        async with test_session() as session:
            existing_replacements = (
                await session.scalars(
                    select(ReplacementRecord).where(
                        ReplacementRecord.mission_id == mission_id
                    )
                )
            ).all()
            assert existing_replacements == []
            session.add(
                ReplacementRecord(
                    issue_id=issue_id,
                    mission_id=mission_id,
                    order_id="order_x",
                    user_id="user_x",
                    original_product_id="item_dove",
                    original_title="Dove Hair Fall Rescue Shampoo",
                    provider="none",
                    status="pending",
                    value=Decimal("399.00"),
                )
            )
            await session.commit()
        resp = await _ret(client, mission_id, issue_id)
        assert resp.status_code == 503  # reaches the provider boundary, unblocked
        assert resp.json()["status"] == "pending"


async def test_returned_issue_blocks_refund_and_replacement(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning,
    return_provider, refund_api, replacement_provider,
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    async with client:
        mission_id = await mission_setup(client)
        issue_id = await _authorized_return_issue(client, test_session, mission_id)
        assert (await _ret(client, mission_id, issue_id)).json()["status"] == "requested"

        # With a return accepted by a provider, neither a refund nor a replacement may also run.
        await _delegate(
            test_session, mission_id, ["request_return", "request_refund", "request_replacement"],
            refund_limit="500",
        )
        await _set_assessment(
            test_session,
            (await _assess(client, mission_id, issue_id))["assessment_id"],
            proposed_action="request_refund",
            authority_decision="allow",
            requested_amount=Decimal("399.00"),
        )
        refund_resp = await _refund(client, mission_id, issue_id)
        assert refund_resp.status_code == 400 and "return" in refund_resp.json()["detail"]

        await _set_assessment(
            test_session,
            (await _assess(client, mission_id, issue_id))["assessment_id"],
            proposed_action="request_replacement",
            authority_decision="allow",
        )
        replace_resp = await _replace(client, mission_id, issue_id)
        assert replace_resp.status_code == 400 and "return" in replace_resp.json()["detail"]
    assert refund_api.calls["create"] == 0
    assert replacement_provider.requests == []


# --------------------------------------------------------------------------- #
# secrets and unhandled provider errors
# --------------------------------------------------------------------------- #
async def test_no_secret_leakage_and_status_read_failure_keeps_state(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning,
    return_provider,
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    secret = get_settings().razorpay_key_secret
    async with client:
        mission_id = await mission_setup(client)
        issue_id = await _authorized_return_issue(client, test_session, mission_id)
        assert (await _ret(client, mission_id, issue_id)).json()["status"] == "requested"

        # A later status read that fails must not change the recorded state or leak anything.
        return_provider.status_error = ReturnProviderUnavailable(
            f"provider unreachable; token={secret}"
        )
        resp = await _ret(client, mission_id, issue_id)
        data = resp.json()
        assert data["status"] == "requested"
        assert secret not in str(data)
        assert "[redacted]" in data["failure_reason"]

        raw = str((await client.get(f"/missions/{mission_id}/order/issues")).json())
        assert secret not in raw
        async with test_session() as session:
            audits = (
                await session.scalars(
                    select(AuditLogRecord).where(AuditLogRecord.mission_id == mission_id)
                )
            ).all()
            assert all(secret not in (a.reason or "") for a in audits)


# --------------------------------------------------------------------------- #
# Task 14C.4: claim-before-mutation, outcome-unknown, and read-only reconciliation
#
# A canonical merchant-side target a fake provider can hand back. The values are shaped like a
# merchant's own identifiers; nothing here is derived from a RESOLVE id.
# --------------------------------------------------------------------------- #
def _target(quantity: int = 1) -> ReturnTarget:
    return ReturnTarget(
        provider_order_reference="gid://merchant/Order/7990366601503",
        provider_line_reference="gid://merchant/FulfillmentLineItem/16492059328799",
        provider_quantity=quantity,
        provider_reason="DEFECTIVE",
        line_quantity=quantity,
        detail="fake provider's own read",
    )


async def _set_return_row(test_session, return_id, **values):
    async with test_session() as session:
        row = await session.get(ReturnRecord, return_id)
        for key, value in values.items():
            setattr(row, key, value)
        await session.commit()


async def _read_return_row(test_session, return_id) -> ReturnRecord:
    async with test_session() as session:
        return await session.get(ReturnRecord, return_id)


# A: a trustworthy success is one row, one mutation, and never a third call.
async def test_accepted_submission_persists_one_claim_and_never_resubmits(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning,
    return_provider,
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    return_provider.target = _target(quantity=2)
    async with client:
        mission_id = await mission_setup(client)
        issue_id = await _authorized_return_issue(client, test_session, mission_id)

        data = (await _ret(client, mission_id, issue_id)).json()
        assert data["status"] == "requested"
        assert len(return_provider.requests) == 1
        assert len(return_provider.resolutions) == 1
        # The submission context was persisted: what was sent is knowable without asking again.
        row = await _read_return_row(test_session, data["return_id"])
        assert row.provider_order_reference == "gid://merchant/Order/7990366601503"
        assert row.provider_line_reference == "gid://merchant/FulfillmentLineItem/16492059328799"
        assert row.provider_quantity == 2
        assert row.provider_reason == "DEFECTIVE"
        # Answered, so the claim is handed back — but the submission record stays.
        assert row.submission_claim is None
        assert row.submission_claimed_at is None

        # The provider was given the target it resolved, not something the caller assembled.
        assert return_provider.targets_sent == [return_provider.target]

        await _ret(client, mission_id, issue_id)
        await _ret(client, mission_id, issue_id)
        assert len(return_provider.requests) == 1  # only ever read back from here on
        async with test_session() as session:
            assert await session.scalar(
                select(func.count()).select_from(ReturnRecord).where(
                    ReturnRecord.mission_id == mission_id
                )
            ) == 1


# B: a deterministic rejection is a failure, never an unknown, and never two mutations at once.
async def test_deterministic_rejection_fails_without_inventing_a_reference(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning,
    return_provider,
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    return_provider.target = _target()
    return_provider.error = ReturnProviderRejected("this line is not returnable")
    async with client:
        mission_id = await mission_setup(client)
        issue_id = await _authorized_return_issue(client, test_session, mission_id)
        resp = await _ret(client, mission_id, issue_id)
        assert resp.status_code == 502
        data = resp.json()
        assert data["status"] == "failed"
        assert data["provider_reference"] is None
        assert "not returnable" in data["failure_reason"]
        assert len(return_provider.requests) == 1  # one call in, one attempt out
        row = await _read_return_row(test_session, data["return_id"])
        assert row.submission_claim is None  # answered: the claim is released
        assert row.provider_line_reference  # what was refused stays on the record
        assert return_provider.reconciliations == []  # a known 'no' needs no reconciliation


# C: the answer was lost. That is NOT a failure, and the next attempt must not submit again.
async def test_lost_answer_becomes_outcome_unknown_and_next_attempt_only_reads(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning,
    return_provider, recorded_events,
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    return_provider.target = _target()
    return_provider.error = ReturnOutcomeUnknown("connection reset before the answer arrived")
    async with client:
        mission_id = await mission_setup(client)
        issue_id = await _authorized_return_issue(client, test_session, mission_id)
        resp = await _ret(client, mission_id, issue_id)
        assert resp.status_code == 503
        data = resp.json()
        assert data["status"] == "outcome_unknown"
        assert "does not claim to know" in resp.json()["detail"]
        assert data["provider_reference"] is None
        assert "not know whether the merchant holds a return" in data["return_note"]

        row = await _read_return_row(test_session, data["return_id"])
        assert row.submission_claim  # kept: an attempt exists whose fate is unknown
        assert row.submission_claimed_at is not None
        assert row.provider_line_reference == "gid://merchant/FulfillmentLineItem/16492059328799"
        assert row.provider_quantity == 1

        # The retry reconciles. It does not re-send.
        return_provider.error = None
        second = await _ret(client, mission_id, issue_id)
        assert len(return_provider.requests) == 1, "an outcome-unknown return was submitted twice"
        assert len(return_provider.reconciliations) == 1
        assert second.status_code == 503
        assert second.json()["status"] == "outcome_unknown"

        submission = return_provider.reconciliations[0]
        assert submission.return_id == data["return_id"]
        assert submission.provider_line_reference == row.provider_line_reference
        assert submission.provider_quantity == row.provider_quantity
        assert submission.provider_reason == row.provider_reason
        assert submission.submitted_at == row.submission_claimed_at
    assert [e["event"] for e in recorded_events if e["event"].startswith("return_")] == [
        "return_pending",
        "return_outcome_unknown",
    ]


# D: reconciliation finds the return, adopts it, and the mutation count stays at one.
async def test_reconciliation_adopts_a_matching_return_without_submitting_again(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning,
    return_provider,
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    return_provider.target = _target()
    return_provider.error = ReturnOutcomeUnknown("answer lost")
    async with client:
        mission_id = await mission_setup(client)
        issue_id = await _authorized_return_issue(client, test_session, mission_id)
        data = (await _ret(client, mission_id, issue_id)).json()
        return_provider.error = None

        return_provider.reconcile_result = ReturnReconciliation.matched(
            provider_reference="RMA-FOUND-777", outcome="accepted", detail="one match"
        )
        adopted = await _ret(client, mission_id, issue_id)
        assert adopted.status_code == 200
        body = adopted.json()
        assert body["status"] == "requested"
        assert body["provider_reference"] == "RMA-FOUND-777"
        assert body["return_id"] == data["return_id"]  # the same row, now accounted for
        assert len(return_provider.requests) == 1
        row = await _read_return_row(test_session, data["return_id"])
        assert row.submission_claim is None
        issue = [
            i for i in (await client.get(f"/missions/{mission_id}/order/issues")).json()["issues"]
            if i["issue_id"] == issue_id
        ][0]
        assert issue["status"] == "return_requested"

        # A confirmed match confirms; it is still not a second submission.
        await _set_return_row(
            test_session,
            data["return_id"],
            status="outcome_unknown",
            provider_reference=None,
            submission_claim="in-flight",
        )
        return_provider.reconcile_result = ReturnReconciliation.matched(
            provider_reference="RMA-FOUND-777", outcome="confirmed"
        )
        third = await _ret(client, mission_id, issue_id)
        assert third.json()["status"] == "confirmed"
        assert third.json()["confirmed_at"]
        assert len(return_provider.requests) == 1


# E: nothing could be matched. The honest answer is still 'unknown', and nothing is re-sent.
async def test_reconciliation_without_a_match_stays_unknown(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning,
    return_provider,
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    return_provider.target = _target()
    return_provider.error = ReturnOutcomeUnknown("answer lost")
    async with client:
        mission_id = await mission_setup(client)
        issue_id = await _authorized_return_issue(client, test_session, mission_id)
        data = (await _ret(client, mission_id, issue_id)).json()
        return_provider.error = None
        return_provider.reconcile_result = ReturnReconciliation.no_match(
            "no return on that order matches the submitted quantity and reason"
        )
        resp = await _ret(client, mission_id, issue_id)
        assert resp.status_code == 503
        assert resp.json()["status"] == "outcome_unknown"
        assert "still unknown" in resp.json()["detail"]
        assert len(return_provider.requests) == 1
        row = await _read_return_row(test_session, data["return_id"])
        assert row.submission_claim  # still held: nothing proved the attempt finished


# F: several candidates. Choosing one would be a guess, so this says so and stops.
async def test_ambiguous_reconciliation_demands_a_human_and_mutates_nothing(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning,
    return_provider,
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    return_provider.target = _target()
    return_provider.error = ReturnOutcomeUnknown("answer lost")
    async with client:
        mission_id = await mission_setup(client)
        issue_id = await _authorized_return_issue(client, test_session, mission_id)
        data = (await _ret(client, mission_id, issue_id)).json()
        return_provider.error = None
        return_provider.reconcile_result = ReturnReconciliation.ambiguous(
            "3 returns on this order match the lost submission"
        )
        resp = await _ret(client, mission_id, issue_id)
        assert resp.status_code == 503
        assert resp.json()["status"] == "outcome_unknown"
        assert resp.json()["provider_reference"] is None
        assert "or a human" in resp.json()["detail"]
        assert len(return_provider.requests) == 1
        row = await _read_return_row(test_session, data["return_id"])
        assert row.submission_claim  # ambiguous is not resolved by picking one of them


# G: nothing a caller posts can name the merchant order, the line, the quantity or the reason.
async def test_client_cannot_name_the_return_target(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning,
    return_provider,
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    return_provider.target = _target(quantity=1)
    async with client:
        mission_id = await mission_setup(client)
        issue_id = await _authorized_return_issue(client, test_session, mission_id)
        resp = await _ret(
            client,
            mission_id,
            issue_id,
            json={
                "provider_order_reference": "gid://merchant/Order/9999999",
                "shopify_order_id": "gid://shopify/Order/9999999",
                "provider_line_reference": "gid://merchant/FulfillmentLineItem/1",
                "fulfillment_line_item_id": "gid://shopify/FulfillmentOrderLineItem/1",
                "provider_quantity": 500,
                "quantity": 500,
                "provider_reason": "NOT_MY_BIZ",
                "return_reason_note": "free text from the client",
                "submission_claim": "caller-supplied",
            },
        )
        assert resp.status_code == 200
        row = await _read_return_row(test_session, resp.json()["return_id"])
        assert row.provider_order_reference == "gid://merchant/Order/7990366601503"
        assert row.provider_line_reference == "gid://merchant/FulfillmentLineItem/16492059328799"
        assert row.provider_quantity == 1
        assert row.provider_reason == "DEFECTIVE"
        assert return_provider.targets_sent == [return_provider.target]
        # And the request body is not even parsed by this endpoint: it takes no parameters.
        assert "9999999" not in str(resp.json())


# An unresolvable target stops before the claim: no mutation, no invented identifiers.
async def test_unresolvable_target_never_reaches_a_mutation(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning,
    return_provider,
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    return_provider.target_error = ReturnProviderUnavailable(
        "No canonical merchant order is linked to this RESOLVE order."
    )
    async with client:
        mission_id = await mission_setup(client)
        issue_id = await _authorized_return_issue(client, test_session, mission_id)
        resp = await _ret(client, mission_id, issue_id)
        assert resp.status_code == 503
        data = resp.json()
        assert data["status"] == "pending"
        assert data["provider_reference"] is None
        assert len(return_provider.requests) == 0  # never submitted, so never claimed either
        row = await _read_return_row(test_session, data["return_id"])
        assert row.submission_claim is None
        assert row.provider_line_reference is None


# A claim already in flight blocks a second submission for the same issue.
async def test_a_held_claim_blocks_a_second_submission(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning,
    return_provider,
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    return_provider.target = _target()
    async with client:
        mission_id = await mission_setup(client)
        issue_id = await _authorized_return_issue(client, test_session, mission_id)
        first = await _ret(client, mission_id, issue_id)
        return_id = first.json()["return_id"]
        assert first.status_code == 200

        # Simulate a concurrent attempt that grabbed the claim on the same row.
        await _set_return_row(
            test_session,
            return_id,
            status="pending",
            provider_reference=None,
            submission_claim="someone-elses-attempt",
        )
        resp = await _ret(client, mission_id, issue_id)
        assert resp.status_code == 503
        assert "in progress" in resp.json()["detail"]
        assert len(return_provider.requests) == 1


# J: another user cannot read, reconcile, or force a re-submission of someone else's return.
async def test_another_user_cannot_touch_an_outcome_unknown_return(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning,
    return_provider,
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    return_provider.target = _target()
    return_provider.error = ReturnOutcomeUnknown("answer lost")
    async with client:
        mission_id = await mission_setup(client)
        issue_id = await _authorized_return_issue(client, test_session, mission_id)
        return_id = (await _ret(client, mission_id, issue_id)).json()["return_id"]
        return_provider.error = None

        await _register(client, "Mallory", "mallory@shop.com")
        assert (await _ret(client, mission_id, issue_id)).status_code == 404
        assert (await client.get(f"/missions/{mission_id}/order")).status_code == 404
        assert len(return_provider.requests) == 1
        assert return_provider.reconciliations == []
        async with test_session() as session:
            row = await session.get(ReturnRecord, return_id)
            assert row.status == "outcome_unknown"
            assert row.user_id != "mallory"


# --------------------------------------------------------------------------- #
# Task 14C.5: an unreconciled return outcome occupies the issue — for refunds and
# replacements too.
#
# `outcome_unknown` is the one return state whose truth RESOLVE does not know: a submission went
# out and its answer was lost. Compensating the issue another way while that is unresolved would
# pay out twice against a state nobody can see, so both other executors refuse — before touching
# a provider. Every other return state keeps exactly the behaviour it had: `pending` and `failed`
# mean no merchant return exists and must NOT be blocked, and `requested`/`confirmed` stay covered
# by the accepted-return exclusion, which is a different reason with a different message.
# --------------------------------------------------------------------------- #
async def _issue_with_return_in_state(
    client, test_session, mission_setup, return_provider, *, expected_status, provider_error
):
    """
    Canonical issue whose return row is driven into a chosen state by the REAL executor, with both
    compensation authorities delegated — so any later refusal is the guard's, not a missing grant.
    """
    return_provider.target = _target()
    return_provider.error = provider_error
    mission_id = await mission_setup(client)
    issue_id = await _authorized_return_issue(client, test_session, mission_id)
    data = (await _ret(client, mission_id, issue_id)).json()
    assert data["status"] == expected_status
    await _delegate(
        test_session, mission_id, ["request_return", "request_refund", "request_replacement"],
        refund_limit="500",
    )
    return mission_id, issue_id, data["return_id"]


async def _aim_assessment(client, test_session, mission_id, issue_id, action):
    assessment = await _assess(client, mission_id, issue_id)
    await _set_assessment(
        test_session,
        assessment["assessment_id"],
        proposed_action=action,
        authority_decision="allow",
        requested_amount=Decimal("399.00") if action == "request_refund" else None,
    )


# A: an outcome-unknown return blocks refund execution, with no Razorpay call at all.
async def test_outcome_unknown_return_blocks_refund_until_reconciled(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning,
    return_provider, refund_api,
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    async with client:
        mission_id, issue_id, return_id = await _issue_with_return_in_state(
            client, test_session, mission_setup, return_provider,
            expected_status="outcome_unknown",
            provider_error=ReturnOutcomeUnknown("answer lost"),
        )
        await _aim_assessment(client, test_session, mission_id, issue_id, "request_refund")

        resp = await _refund(client, mission_id, issue_id)
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert "outcome is unknown" in detail
        assert return_id in detail
        assert "reconciled" in detail
        # Refused at the guard: the payment provider was never asked for anything.
    assert refund_api.calls == {"create": 0, "fetch": 0, "list": 0}
    async with test_session() as session:
        assert await session.scalar(
            select(func.count()).select_from(RefundRecord).where(
                RefundRecord.issue_id == issue_id
            )
        ) == 0
        row = await session.get(ReturnRecord, return_id)
        assert row.status == "outcome_unknown"  # the guard mutates nothing, including the return


# B: the same state blocks replacement execution, with no provider call.
async def test_outcome_unknown_return_blocks_replacement_until_reconciled(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning,
    return_provider, replacement_provider,
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    async with client:
        mission_id, issue_id, return_id = await _issue_with_return_in_state(
            client, test_session, mission_setup, return_provider,
            expected_status="outcome_unknown",
            provider_error=ReturnOutcomeUnknown("answer lost"),
        )
        await _aim_assessment(client, test_session, mission_id, issue_id, "request_replacement")

        resp = await _replace(client, mission_id, issue_id)
        assert resp.status_code == 400
        assert "outcome is unknown" in resp.json()["detail"]
        assert "reconciled" in resp.json()["detail"]
        assert len(return_provider.requests) == 1  # still the one lost submission, never a second
    assert replacement_provider.requests == []
    async with test_session() as session:
        assert await session.scalar(
            select(func.count()).select_from(ReplacementRecord).where(
                ReplacementRecord.issue_id == issue_id
            )
        ) == 0


# C: a FAILED return means no merchant return exists — it must not block compensation.
async def test_failed_return_does_not_trigger_the_reconciliation_guard(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning,
    return_provider, refund_api,
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    async with client:
        mission_id, issue_id, _ = await _issue_with_return_in_state(
            client, test_session, mission_setup, return_provider,
            expected_status="failed",
            provider_error=ReturnProviderRejected("return window has closed"),
        )
        await _aim_assessment(client, test_session, mission_id, issue_id, "request_refund")

        resp = await _refund(client, mission_id, issue_id)
        assert resp.status_code == 200
        assert resp.json()["status"] == "verified"
    assert refund_api.calls["create"] == 1


# C2: a PENDING return (no provider accepted it) is equally unblocked.
async def test_pending_return_does_not_trigger_the_reconciliation_guard(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning,
    return_provider, refund_api,
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    async with client:
        mission_id = await mission_setup(client)
        issue_id = await _authorized_return_issue(client, test_session, mission_id)
        return_id = (await _ret(client, mission_id, issue_id)).json()["return_id"]
        await _set_return_row(
            test_session, return_id, status="pending", provider_reference=None,
            submission_claim=None,
        )
        await _delegate(
            test_session, mission_id, ["request_return", "request_refund"], refund_limit="500"
        )
        await _aim_assessment(client, test_session, mission_id, issue_id, "request_refund")

        resp = await _refund(client, mission_id, issue_id)
        assert resp.status_code == 200
        assert resp.json()["status"] == "verified"
    assert refund_api.calls["create"] == 1


# D: requested/confirmed keep their own pre-existing exclusion, reason and wording intact.
@pytest.mark.parametrize("state", ["requested", "confirmed"])
async def test_accepted_return_keeps_its_existing_exclusion_wording(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning,
    return_provider, refund_api, state,
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    async with client:
        mission_id = await mission_setup(client)
        issue_id = await _authorized_return_issue(client, test_session, mission_id)
        return_id = (await _ret(client, mission_id, issue_id)).json()["return_id"]
        await _set_return_row(test_session, return_id, status=state)
        await _delegate(
            test_session, mission_id, ["request_return", "request_refund"], refund_limit="500"
        )
        await _aim_assessment(client, test_session, mission_id, issue_id, "request_refund")

        resp = await _refund(client, mission_id, issue_id)
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        # The accepted-return rule, not the new one: it says the issue already HAS a return.
        assert f"already has a return ('{state}'" in detail
        assert "reconcil" not in detail
        assert "unknown" not in detail
    assert refund_api.calls["create"] == 0


# E: with no canonical linkage there is nothing to aim a mutation at — and none is attempted.
async def test_a_missing_linkage_stops_the_flow_before_any_submission(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning,
    return_provider,
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    async with client:
        mission_id = await mission_setup(client)
        issue_id = await _authorized_return_issue(client, test_session, mission_id)
        async with test_session() as session:
            order = await session.scalar(
                select(ResolveOrderRecord).where(ResolveOrderRecord.mission_id == mission_id)
            )
        assert order.provider_order_reference is None  # nothing has ever linked it

        async def _no_linkage(_request):
            raise ReturnProviderUnavailable(
                "No canonical Shopify order is linked to this RESOLVE order, so there is nothing "
                "for a return to reference, and nothing was submitted."
            )

        return_provider.resolve_return_target = _no_linkage
        resp = await _ret(client, mission_id, issue_id)
        assert resp.status_code == 503
        assert resp.json()["status"] == "pending"
        assert len(return_provider.requests) == 0


# Objective D: reconciliation is aimed at the persisted linkage, and it is a READ.
async def test_reconciliation_is_aimed_at_the_persisted_linkage_and_submits_nothing(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning,
    return_provider,
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    return_provider.target = _target()
    return_provider.error = ReturnOutcomeUnknown("answer lost")
    async with client:
        mission_id = await mission_setup(client)
        issue_id = await _authorized_return_issue(client, test_session, mission_id)
        data = (await _ret(client, mission_id, issue_id)).json()

        # The merchant order the claim was filed against is the one the provider resolved, and it
        # is now on the row — that stored reference is what a later read has to go back to.
        row = await _read_return_row(test_session, data["return_id"])
        assert row.provider_order_reference == "gid://merchant/Order/7990366601503"

        return_provider.error = None
        return_provider.reconcile_result = ReturnReconciliation.matched(
            provider_reference="RMA-FROM-LINKAGE", outcome="accepted"
        )
        resp = await _ret(client, mission_id, issue_id)
        assert resp.status_code == 200
        assert resp.json()["status"] == "requested"

        submission = return_provider.reconciliations[0]
        assert submission.provider_order_reference == row.provider_order_reference
        # One submission ever, and the read that followed it was not a second one.
        assert len(return_provider.requests) == 1
        assert return_provider.status_reads == []
