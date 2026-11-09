"""
Tests for Task 11A: controlled Razorpay TEST-MODE refund execution.

The pipeline under test:
    assessment (Task 10) -> execution-time Authority Engine re-gate -> Razorpay create
    -> Razorpay status verification -> verified.

Everything the provider sees is canonical server state — the endpoint has NO request
body, and every guard test asserts that ZERO provider calls happened. The Authority
Engine's policy code is never changed here; where a refund is allowed, the mission's
persisted mandate (the user's delegation) is updated in test data — exactly what a
delegating user would have stored.

Hermetic: mock reasoning/discovery via conftest; the Razorpay refund API is fully
mocked — NO real payment or refund API calls are made.
"""
import hashlib
import hmac
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.agents.conversation import as_event_frame
from app.config import get_settings
from app.database import Base, get_db
from app.main import app
from app.models.db_models import (
    AuditLogRecord,
    MissionRecord,
    RefundRecord,
    ResolutionAssessmentRecord,
)
from app.routers import missions as missions_module

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

SHAMPOO_PROMPT = "I want to order shampoo under ₹600 for anti-hairfall."
PASSWORD = "SecurePassword123!"

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def restart_env():
    """Client factory + sessionmaker sharing one engine (needed for mandate delegation
    and restart simulation)."""
    test_engine = create_engine_shim()
    test_session = make_sessionmaker(test_engine)

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


def create_engine_shim():
    from sqlalchemy.ext.asyncio import create_async_engine

    return create_async_engine(TEST_DATABASE_URL, echo=False)


def make_sessionmaker(engine):
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@pytest.fixture
def mock_razorpay(monkeypatch):
    """Deterministic stand-in for the Razorpay TEST order API."""
    counter = {"n": 0}

    def fake_create_test_order(amount_paise, currency, receipt, notes):
        counter["n"] += 1
        return {
            "id": f"order_TEST{counter['n']:04d}",
            "amount": amount_paise,
            "currency": currency,
            "status": "created",
            "receipt": receipt,
        }

    monkeypatch.setattr(missions_module, "create_test_order", fake_create_test_order)
    return counter


@pytest.fixture
def recorded_events(monkeypatch):
    """Capture every canonical event frame broadcast during a test (M21 Phase 2A).

    Refund/replacement/return transitions broadcast no frames until CP3 gives them
    durable conversation rows, so these tests assert stream quietness where the old
    ad-hoc broadcasts used to fire.
    """
    events: list[dict] = []

    async def fake_broadcast(row) -> None:
        events.append(as_event_frame(row))

    monkeypatch.setattr(missions_module, "broadcast_event_row", fake_broadcast)
    return events


class RefundApi:
    """In-memory Razorpay refund API with call counters and failure injection."""

    def __init__(self):
        self.test_mode = True
        self.refunds: dict[str, dict] = {}
        self.calls = {"create": 0, "fetch": 0, "list": 0}
        self.fetch_calls: list[tuple[str, str]] = []
        self.create_calls: list[tuple[str, int, str]] = []
        self.create_error: Exception | None = None
        self.fetch_error: Exception | None = None
        self.list_error: Exception | None = None
        self.create_response_status = "processed"
        self.fetch_status: str | None = None
        # Simulate a create whose response was lost AFTER the provider took it
        self.create_lands_despite_error = False

    def is_test_mode(self) -> bool:
        return self.test_mode

    def create(self, payment_id, amount_paise, resolve_refund_id):
        self.calls["create"] += 1
        self.create_calls.append((payment_id, amount_paise, resolve_refund_id))
        if self.create_error is not None:
            if self.create_lands_despite_error:
                rid = f"rfnd_TEST{len(self.refunds) + 1}"
                self.refunds[rid] = {
                    "id": rid, "status": "processed", "payment_id": payment_id,
                    "amount": amount_paise,
                    "notes": {"resolve_refund_id": resolve_refund_id},
                }
            raise self.create_error
        rid = f"rfnd_TEST{len(self.refunds) + 1}"
        self.refunds[rid] = {
            "id": rid, "status": self.create_response_status, "payment_id": payment_id,
            "amount": amount_paise, "notes": {"resolve_refund_id": resolve_refund_id},
        }
        return dict(self.refunds[rid])

    def fetch(self, payment_id, razorpay_refund_id):
        self.calls["fetch"] += 1
        self.fetch_calls.append((payment_id, razorpay_refund_id))
        if self.fetch_error is not None:
            raise self.fetch_error
        stored = self.refunds.get(razorpay_refund_id, {"id": razorpay_refund_id, "status": "processed"})
        status = self.fetch_status if self.fetch_status is not None else stored["status"]
        return {**stored, "status": status}

    def list_for_payment(self, payment_id):
        self.calls["list"] += 1
        if self.list_error is not None:
            raise self.list_error
        return [dict(r) for r in self.refunds.values() if r.get("payment_id") == payment_id]


@pytest.fixture
def refund_api(monkeypatch):
    """Swap the four Razorpay refund primitives inside the router (same seams production
    uses). Installed AFTER mission setup, so the payment mocks above stay intact."""
    api = RefundApi()
    monkeypatch.setattr(missions_module, "is_test_mode", api.is_test_mode)
    monkeypatch.setattr(missions_module, "create_payment_refund", api.create)
    monkeypatch.setattr(missions_module, "fetch_refund", api.fetch)
    monkeypatch.setattr(missions_module, "list_refunds_for_payment", api.list_for_payment)
    return api


class ControlledProvider:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.contexts: list[dict] = []

    async def assess_resolution(self, context):
        self.contexts.append(context)
        if self.error is not None:
            raise self.error
        return self.result


@pytest.fixture
def install_fake_reasoning(monkeypatch):
    def _install(result=None, error=None) -> ControlledProvider:
        provider = ControlledProvider(result=result, error=error)
        monkeypatch.setattr(
            missions_module, "get_reasoning_provider", lambda name, key: provider
        )
        return provider

    return _install


def _checkout_signature(order_id: str, payment_id: str) -> str:
    secret = get_settings().razorpay_key_secret.encode()
    return hmac.new(secret, f"{order_id}|{payment_id}".encode(), hashlib.sha256).hexdigest()


async def _register(client: AsyncClient, name: str, email: str) -> None:
    assert (
        await client.post(
            "/auth/register", json={"name": name, "email": email, "password": PASSWORD}
        )
    ).status_code == 201


async def _login(client: AsyncClient, email: str) -> None:
    assert (
        await client.post("/auth/login", json={"email": email, "password": PASSWORD})
    ).status_code == 200


async def _verified_mission(client, install_mock_discovery, sample_products, mock_razorpay) -> str:
    install_mock_discovery(sample_results=sample_products)
    await _register(client, "Olga Owner", "olga@shop.com")
    resp = await client.post("/missions/shopping", json={"prompt": SHAMPOO_PROMPT})
    mission_id = resp.json()["mission_id"]
    assert (
        await client.post(
            f"/missions/{mission_id}/shopping/select-product", json={"product_id": "item_dove"}
        )
    ).status_code == 200
    assert (await client.post(f"/missions/{mission_id}/shopping/checkout/confirm")).status_code == 200
    order_resp = await client.post(f"/missions/{mission_id}/shopping/payment/create-order", json={})
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
    return mission_id


async def _confirmed_order_with_issue(client, mission_id) -> str:
    assert (await client.post(f"/missions/{mission_id}/order", json={})).status_code == 200
    assert (await client.post(f"/missions/{mission_id}/order/confirm", json={})).status_code == 200
    resp = await client.post(
        f"/missions/{mission_id}/order/issues",
        json={
            "issue_type": "wrong_item",
            "description": "I received the wrong shampoo. I want a refund.",
            "requested_resolution": "refund",
        },
    )
    assert resp.status_code == 200
    return resp.json()["issue_id"]


async def _set_mandate_authority(test_session, mission_id, actions, budget):
    """Test-data stand-in for a user who delegated refund authority in their mandate.
    The Authority Engine's policy CODE is untouched."""
    async with test_session() as session:
        rec = await session.get(MissionRecord, mission_id)
        mandate = dict(rec.mandate)
        authority = dict(mandate.get("authority") or {})
        authority["allowed_actions"] = list(actions)
        authority["recovery_budget"] = str(budget)
        mandate["authority"] = authority
        rec.mandate = mandate
        await session.commit()


async def _assess(client, mission_id, issue_id):
    resp = await client.post(f"/missions/{mission_id}/order/issues/{issue_id}/assess")
    assert resp.status_code == 200
    return resp.json()


async def _refund(client, mission_id, issue_id, json=None):
    url = f"/missions/{mission_id}/order/issues/{issue_id}/refund"
    return await client.post(url, json=json) if json is not None else await client.post(url)


async def _authorized_refundable_issue(client, test_session, mission_setup) -> str:
    """verified mission -> confirmed order -> issue -> ALLOWed refund assessment."""
    mission_id = await mission_setup(client)
    issue_id = await _confirmed_order_with_issue(client, mission_id)
    await _set_mandate_authority(test_session, mission_id, ["request_refund"], "1000")
    assessment = await _assess(client, mission_id, issue_id)
    assert assessment["proposed_action"] == "request_refund"
    assert assessment["authority_decision"] == "allow"
    return mission_id, issue_id


async def _refundable_setup(restart_env, install_mock_discovery, sample_products, mock_razorpay):
    make_client, test_session = restart_env
    client = make_client()

    async def _mission_setup(c):
        return await _verified_mission(c, install_mock_discovery, sample_products, mock_razorpay)

    return client, test_session, _mission_setup


# --------------------------------------------------------------------------- #
# TESTS 1, 13, 14, 16, 17: happy path — ALLOW, canonical amount/currency used,
# verified only after provider check, persisted, issue advanced
# --------------------------------------------------------------------------- #
async def test_allowed_refund_executes_and_verifies(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, refund_api
):
    client, test_session, setup = await _refundable_setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay
    )
    async with client:
        mission_id, issue_id = await _authorized_refundable_issue(client, test_session, setup)

        resp = await _refund(client, mission_id, issue_id)
        assert resp.status_code == 200
        data = resp.json()

        assert data["refund_id"].startswith("refund_")
        assert data["status"] == "verified"
        assert data["provider_status"] == "processed"
        assert data["razorpay_refund_id"] == "rfnd_TEST1"
        # Canonical amount/currency from the persisted payment, integer paise
        assert Decimal(str(data["amount"])) == Decimal("399.00")
        assert data["amount_paise"] == 39900
        assert data["currency"] == "INR"
        assert data["razorpay_payment_id"] == "pay_TESTORDER1"
        assert data["verified_at"] and data["test_mode"] is True
        assert "TEST-mode provider refund" in data["agent_message"]

        # Exactly one create + one verification fetch, carrying the claim notes
        assert refund_api.calls == {"create": 1, "fetch": 1, "list": 0}
        # Verification is a per-payment lookup: the canonical payment id, not a guess.
        assert refund_api.fetch_calls == [("pay_TESTORDER1", "rfnd_TEST1")]
        # The canonical captured amount is what the provider is asked to refund.
        assert refund_api.create_calls == [("pay_TESTORDER1", 39900, data["refund_id"])]
        stored = refund_api.refunds["rfnd_TEST1"]
        assert stored["notes"]["resolve_refund_id"] == data["refund_id"]
        assert stored["amount"] == 39900

        # Issue lifecycle advanced by backend code only
        issues = (await client.get(f"/missions/{mission_id}/order/issues")).json()["issues"]
        assert issues[0]["status"] == "resolution_pending"
        assert issues[0]["refund"]["status"] == "verified"

        async with test_session() as session:
            rows = (
                await session.execute(
                    select(RefundRecord).where(RefundRecord.issue_id == issue_id)
                )
            ).scalars().all()
            assert len(rows) == 1
            assert rows[0].user_id.startswith("user_")
            assert rows[0].order_id  # canonical order id


# --------------------------------------------------------------------------- #
# TESTS 2, 3, 4, 5, 6, 7, 8, 9, 10: every guard rejects BEFORE any provider call
# --------------------------------------------------------------------------- #
async def test_guards_reject_without_touching_provider(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, refund_api
):
    make_client, test_session = restart_env
    async with make_client() as client:
        mission_id = await _verified_mission(
            client, install_mock_discovery, sample_products, mock_razorpay
        )
        issue_id = await _confirmed_order_with_issue(client, mission_id)

        # TEST 7: no assessment yet
        resp = await _refund(client, mission_id, issue_id)
        assert resp.status_code == 400 and "assessed" in resp.json()["detail"]

        # TEST 8: assessment proposed a replacement -> cannot execute a refund
        replacement_issue = (
            await client.post(
                f"/missions/{mission_id}/order/issues",
                json={
                    "issue_type": "damaged",
                    "description": "Bottle cracked; I want a replacement.",
                    "requested_resolution": "replacement",
                },
            )
        ).json()["issue_id"]
        await _set_mandate_authority(test_session, mission_id, ["request_replacement"], "1000")
        replacement = await _assess(client, mission_id, replacement_issue)
        assert replacement["proposed_action"] == "request_replacement"
        assert replacement["authority_decision"] == "allow"
        resp = await _refund(client, mission_id, replacement_issue)
        assert resp.status_code == 400 and "request_refund" in resp.json()["detail"]

        # TEST 9: Authority Engine DENY (refund proposed but nothing delegated)
        await _set_mandate_authority(test_session, mission_id, [], "0")
        denied = await _assess(client, mission_id, issue_id)
        assert denied["authority_decision"] == "deny"
        resp = await _refund(client, mission_id, issue_id)
        assert resp.status_code == 400 and "requires an explicit allow" in resp.json()["detail"]

        # TEST 10: ESCALATE decision on a refund proposal blocks execution. The Task 10
        # pipeline maps an approved escalate_to_human ACTION to decision "escalate"; a
        # refund proposal that comes back with an escalate decision must be rejected too.
        # The stored decision is set in test data — the engine's policy code is untouched.
        esc_issue = (
            await client.post(
                f"/missions/{mission_id}/order/issues",
                json={
                    "issue_type": "wrong_item",
                    "description": "Wrong bottle; refund it.",
                    "requested_resolution": "refund",
                },
            )
        ).json()["issue_id"]
        await _set_mandate_authority(test_session, mission_id, ["request_refund"], "1000")
        esc_assess = await _assess(client, mission_id, esc_issue)
        assert esc_assess["proposed_action"] == "request_refund"
        assert esc_assess["authority_decision"] == "allow"
        async with test_session() as session:
            row = await session.get(ResolutionAssessmentRecord, esc_assess["assessment_id"])
            row.authority_decision = "escalate"
            await session.commit()
        resp = await _refund(client, mission_id, esc_issue)
        assert resp.status_code == 400 and "requires an explicit allow" in resp.json()["detail"]

        # Execution-time re-gate: a fresh issue gets a genuine ALLOWing assessment, then
        # the delegation is revoked — the stored checks pass, the fresh gate call refuses.
        rg_issue = (
            await client.post(
                f"/missions/{mission_id}/order/issues",
                json={
                    "issue_type": "other",
                    "description": "Not as described; refund.",
                    "requested_resolution": "refund",
                },
            )
        ).json()["issue_id"]
        rg_assess = await _assess(client, mission_id, rg_issue)
        assert rg_assess["authority_decision"] == "allow"
        await _set_mandate_authority(test_session, mission_id, [], "0")
        resp = await _refund(client, mission_id, rg_issue)
        assert resp.status_code == 400 and "at execution time" in resp.json()["detail"]

        # None of the paths above created anything
        assert refund_api.calls["create"] == 0 and refund_api.calls["fetch"] == 0
        async with test_session() as session:
            count = await session.scalar(
                select(func.count()).select_from(RefundRecord).where(
                    RefundRecord.mission_id == mission_id
                )
            )
            assert count == 0

        # Restore authority for the ownership checks below
        await _set_mandate_authority(test_session, mission_id, ["request_refund"], "1000")
        await _assess(client, mission_id, issue_id)  # already exists -> returns the denied one
        # Fresh assessable+allowed issue for ownership tests
        owner_issue = (
            await client.post(
                f"/missions/{mission_id}/order/issues",
                json={
                    "issue_type": "defective",
                    "description": "Pump does not work; refund please.",
                    "requested_resolution": "none",
                },
            )
        ).json()["issue_id"]
        await _set_mandate_authority(test_session, mission_id, ["request_refund"], "1000")
        provider_result = {
            "issue_assessment": "Defective unit.",
            "proposed_action": "request_refund",
            "reasoning_summary": "Refund proposed.",
            "requested_amount": 1,
            "confidence": 0.8,
            "needs_escalation": False,
        }
        # use module-level provider seam for this one assessment
        import app.routers.missions as mm

        original = mm.get_reasoning_provider

        class P:
            async def assess_resolution(self, ctx):
                return provider_result

        mm.get_reasoning_provider = lambda name, key: P()
        try:
            allowed = await _assess(client, mission_id, owner_issue)
        finally:
            mm.get_reasoning_provider = original
        assert allowed["authority_decision"] == "allow"

        # TEST 2: authenticated non-owner -> 404
        await _register(client, "Mallory", "mallory@shop.com")
        resp = await _refund(client, mission_id, owner_issue)
        assert resp.status_code == 404

        # TEST 2b: anonymous -> 404
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anon:
            resp = await _refund(anon, mission_id, owner_issue)
            assert resp.status_code == 404

        await _login(client, "olga@shop.com")

        # Unknown / foreign issue -> 404
        assert (await _refund(client, mission_id, "issue_nope123456")).status_code == 404

        # TEST 3: non-shopping mission -> 400
        rec = await client.post(
            "/missions",
            json={
                "mission_type": "recovery",
                "objective": {"description": "Recover damaged package", "success_criteria": []},
                "authority": {"allowed_actions": ["request_refund"], "recovery_budget": "5000"},
                "transaction": {"merchant_name": "S", "order_id": "o", "amount_paid": "100"},
            },
        )
        assert (await _refund(client, rec.json()["mission_id"], owner_issue)).status_code == 400

        # TEST 4: mission without an order -> 404 (payment first, then a fresh mission)
        await _login(client, "olga@shop.com")
        resp = await client.post("/missions/shopping", json={"prompt": SHAMPOO_PROMPT})
        no_order = resp.json()["mission_id"]
        assert (await _refund(client, no_order, owner_issue)).status_code == 404
        assert refund_api.calls["create"] == 0


# --------------------------------------------------------------------------- #
# TEST 5: payment not verified -> rejected
# --------------------------------------------------------------------------- #
async def test_unverified_payment_rejected(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, refund_api
):
    client, test_session, setup = await _refundable_setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay
    )
    async with client:
        mission_id = await setup(client)
        issue_id = await _confirmed_order_with_issue(client, mission_id)

        # Demote the persisted payment behind the order (data tampering in test only)
        from sqlalchemy import update

        async with test_session() as session:
            rec = await session.get(MissionRecord, mission_id)
            intent = dict(rec.shopping_intent)
            payment = dict(intent["payment"])
            payment["status"] = "failed"
            intent["payment"] = payment
            rec.shopping_intent = intent
            await session.commit()

        await _set_mandate_authority(test_session, mission_id, ["request_refund"], "1000")
        await _assess(client, mission_id, issue_id)
        resp = await _refund(client, mission_id, issue_id)
        assert resp.status_code == 400
        assert "verified" in resp.json()["detail"]
        assert refund_api.calls["create"] == 0


# --------------------------------------------------------------------------- #
# TESTS 11, 12: client tampering with amount / payment id / authority is ignored
# --------------------------------------------------------------------------- #
async def test_client_cannot_control_refund_parameters(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, refund_api
):
    client, test_session, setup = await _refundable_setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay
    )
    async with client:
        mission_id, issue_id = await _authorized_refundable_issue(client, test_session, setup)
        resp = await _refund(
            client,
            mission_id,
            issue_id,
            json={
                "amount": "0.01",
                "refund_amount": "999999",
                "currency": "USD",
                "payment_id": "pay_EVIL",
                "razorpay_payment_id": "pay_EVIL",
                "order_id": "order_EVIL",
                "mission_id": "mission_EVIL",
                "issue_id": "issue_EVIL",
                "authority_decision": "allow",
                "razorpay_refund_id": "rfnd_EVIL",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["order_id"] != "order_EVIL"
        assert data["razorpay_payment_id"] == "pay_TESTORDER1"
        assert data["razorpay_refund_id"] == "rfnd_TEST1"
        assert data["amount"] == "399.00"
        assert data["currency"] == "INR"


# --------------------------------------------------------------------------- #
# TEST 10 (amount safety): canonical payment paise are capped by the order amount
# --------------------------------------------------------------------------- #
async def test_refund_never_exceeds_canonical_captured_amount(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, refund_api
):
    client, test_session, setup = await _refundable_setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay
    )
    async with client:
        mission_id, issue_id = await _authorized_refundable_issue(client, test_session, setup)

        # Corrupt the persisted payment paise upward (test data only): the canonical
        # cross-check against the order amount must clamp the refund back.
        async with test_session() as session:
            rec = await session.get(MissionRecord, mission_id)
            intent = dict(rec.shopping_intent)
            payment = dict(intent["payment"])
            payment["amount_paise"] = 99999999
            intent["payment"] = payment
            rec.shopping_intent = intent
            await session.commit()

        data = (await _refund(client, mission_id, issue_id)).json()
        assert data["status"] == "verified"
        assert data["amount_paise"] == 39900
        assert refund_api.refunds["rfnd_TEST1"]["amount"] == 39900
        assert refund_api.create_calls[0][1] == 39900


# --------------------------------------------------------------------------- #
# TESTS 18, 19: idempotency of a verified refund
# --------------------------------------------------------------------------- #
async def test_verified_refund_repeat_returns_existing_without_provider_calls(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, refund_api
):
    client, test_session, setup = await _refundable_setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay
    )
    async with client:
        mission_id, issue_id = await _authorized_refundable_issue(client, test_session, setup)
        first = (await _refund(client, mission_id, issue_id)).json()
        calls_after_first = dict(refund_api.calls)
        cursor_after_first = (
            await client.get(f"/missions/{mission_id}/agent/conversation")
        ).json()["latest_sequence"]

        for _ in range(3):
            repeat = await _refund(client, mission_id, issue_id)
            assert repeat.status_code == 200
            data = repeat.json()
            assert data["refund_id"] == first["refund_id"]
            assert data["already_existed"] is True
            assert "already verified" in data["agent_message"]

        assert refund_api.calls == calls_after_first  # zero further provider calls
        # A repeat appends no conversation row, so the stream announces nothing (M21 2A).
        assert (
            await client.get(f"/missions/{mission_id}/agent/conversation")
        ).json()["latest_sequence"] == cursor_after_first
        async with test_session() as session:
            assert (
                await session.scalar(
                    select(func.count()).select_from(RefundRecord)
                )
            ) == 1
            assert len(refund_api.refunds) == 1  # one refund at the provider, period


# --------------------------------------------------------------------------- #
# TEST 17 + case C: create accepted but provider still pending -> pending state,
# then status re-check (never a second create) verifies it
# --------------------------------------------------------------------------- #
async def test_pending_then_rechecked_at_provider_not_recreated(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, refund_api
):
    client, test_session, setup = await _refundable_setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay
    )
    async with client:
        mission_id, issue_id = await _authorized_refundable_issue(client, test_session, setup)
        refund_api.create_response_status = "pending"

        data = (await _refund(client, mission_id, issue_id)).json()
        assert data["status"] == "pending"
        assert data["verified_at"] is None
        assert "NOT a completed refund" in data["agent_message"]
        # Issue stays under_review while the refund is unverified
        issues = (await client.get(f"/missions/{mission_id}/order/issues")).json()["issues"]
        assert issues[0]["status"] == "under_review"

        # Retry: provider now reports processed. Must re-check, not re-create.
        refund_api.fetch_status = "processed"
        again = (await _refund(client, mission_id, issue_id)).json()
        assert again["status"] == "verified"
        assert refund_api.calls["create"] == 1
        issues = (await client.get(f"/missions/{mission_id}/order/issues")).json()["issues"]
        assert issues[0]["status"] == "resolution_pending"


# --------------------------------------------------------------------------- #
# TEST 15 + case D: failed create persists safely; retry reuses the same row
# --------------------------------------------------------------------------- #
async def test_failed_create_persists_and_retry_reuses_row(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, refund_api
):
    client, test_session, setup = await _refundable_setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay
    )
    async with client:
        mission_id, issue_id = await _authorized_refundable_issue(client, test_session, setup)
        cursor_before = (
            await client.get(f"/missions/{mission_id}/agent/conversation")
        ).json()["latest_sequence"]
        refund_api.create_error = RuntimeError("provider unavailable")

        resp = await _refund(client, mission_id, issue_id)
        assert resp.status_code == 502
        async with test_session() as session:
            row = (
                await session.execute(
                    select(RefundRecord).where(RefundRecord.issue_id == issue_id)
                )
            ).scalar_one()
            assert row.status == "failed"
            assert row.razorpay_refund_id is None
            assert "provider unavailable" in row.error_reason

        issues = (await client.get(f"/missions/{mission_id}/order/issues")).json()["issues"]
        assert issues[0]["refund"]["status"] == "failed"
        assert issues[0]["status"] == "under_review"  # no false success
        # M21 2A: refund transitions write no conversation row yet (CP3 adds the durable
        # writer) — a failed attempt must not move the transcript cursor either way.
        assert (
            await client.get(f"/missions/{mission_id}/agent/conversation")
        ).json()["latest_sequence"] == cursor_before

        # Recovery: retry goes through the SAME row — never a second refund record
        refund_api.create_error = None
        data = (await _refund(client, mission_id, issue_id)).json()
        assert data["status"] == "verified"
        assert data["refund_id"] == row.refund_id
        async with test_session() as session:
            rows = (
                await session.execute(
                    select(RefundRecord).where(RefundRecord.issue_id == issue_id)
                )
            ).scalars().all()
            assert len(rows) == 1
        assert refund_api.calls["create"] == 2  # one failed + one real create
        assert len(refund_api.refunds) == 1  # but only ONE exists at the provider


# --------------------------------------------------------------------------- #
# Crash-resilience: create reached the provider but the response was lost ->
# the notes claim is adopted on retry; exactly one provider refund ever exists
# --------------------------------------------------------------------------- #
async def test_lost_create_response_adopts_claimed_refund(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, refund_api
):
    client, test_session, setup = await _refundable_setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay
    )
    async with client:
        mission_id, issue_id = await _authorized_refundable_issue(client, test_session, setup)
        refund_api.create_error = TimeoutError("connection reset after provider accepted")
        refund_api.create_lands_despite_error = True

        assert (await _refund(client, mission_id, issue_id)).status_code == 502
        assert len(refund_api.refunds) == 1  # provider actually has it

        refund_api.create_error = None
        data = (await _refund(client, mission_id, issue_id)).json()
        assert data["status"] == "verified"
        assert data["razorpay_refund_id"] == "rfnd_TEST1"
        assert refund_api.calls["create"] == 1  # never created a second refund
        assert len(refund_api.refunds) == 1


# --------------------------------------------------------------------------- #
# Create succeeded but verification fetch failed -> stays pending, never verified
# --------------------------------------------------------------------------- #
async def test_verification_unavailable_keeps_pending(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, refund_api
):
    client, test_session, setup = await _refundable_setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay
    )
    async with client:
        mission_id, issue_id = await _authorized_refundable_issue(client, test_session, setup)
        refund_api.fetch_error = TimeoutError("status endpoint down")

        data = (await _refund(client, mission_id, issue_id)).json()
        assert data["status"] == "pending"
        assert data["razorpay_refund_id"] == "rfnd_TEST1"
        assert "status endpoint down" in (data["error_reason"] or "")

        refund_api.fetch_error = None
        again = (await _refund(client, mission_id, issue_id)).json()
        assert again["status"] == "verified"
        assert refund_api.calls["create"] == 1


# --------------------------------------------------------------------------- #
# TEST mode fence: non-test credentials refuse execution
# --------------------------------------------------------------------------- #
async def test_non_test_mode_blocks_execution(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, refund_api
):
    client, test_session, setup = await _refundable_setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay
    )
    async with client:
        mission_id, issue_id = await _authorized_refundable_issue(client, test_session, setup)
        refund_api.test_mode = False

        resp = await _refund(client, mission_id, issue_id)
        assert resp.status_code == 400
        assert "TEST mode" in resp.json()["detail"]
        assert refund_api.calls["create"] == 0
        async with test_session() as session:
            row = (
                await session.execute(
                    select(RefundRecord).where(RefundRecord.issue_id == issue_id)
                )
            ).scalar_one()
            assert row.status == "failed"
            assert "TEST mode" in row.error_reason


# --------------------------------------------------------------------------- #
# TEST 21: no secret leakage anywhere in responses or persisted errors
# --------------------------------------------------------------------------- #
async def test_no_secret_leakage(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, refund_api
):
    client, test_session, setup = await _refundable_setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay
    )
    secret = get_settings().razorpay_key_secret
    async with client:
        mission_id, issue_id = await _authorized_refundable_issue(client, test_session, setup)
        refund_api.create_error = RuntimeError(f"auth rejected for key {secret}")

        resp = await _refund(client, mission_id, issue_id)
        assert resp.status_code == 502
        body = resp.text
        assert secret not in body
        async with test_session() as session:
            row = (
                await session.execute(
                    select(RefundRecord).where(RefundRecord.issue_id == issue_id)
                )
            ).scalar_one()
            assert secret not in (row.error_reason or "")
            assert "[redacted]" in row.error_reason
            audits = (
                (await session.execute(select(AuditLogRecord).where(AuditLogRecord.mission_id == mission_id)))
                .scalars()
                .all()
            )
            for a in audits:
                assert secret not in (a.reason or "")
        issues_text = (await client.get(f"/missions/{mission_id}/order/issues")).text
        assert secret not in issues_text


# --------------------------------------------------------------------------- #
# TEST 22: audit records the execution with the Authority Engine decision
# --------------------------------------------------------------------------- #
async def test_audit_records_execution(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, refund_api
):
    client, test_session, setup = await _refundable_setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay
    )
    async with client:
        mission_id, issue_id = await _authorized_refundable_issue(client, test_session, setup)
        data = (await _refund(client, mission_id, issue_id)).json()

    async with test_session() as session:
        rows = (
            (
                await session.execute(
                    select(AuditLogRecord)
                    .where(AuditLogRecord.mission_id == mission_id)
                    .order_by(AuditLogRecord.created_at.asc())
                )
            )
            .scalars()
            .all()
        )
    execution = [r for r in rows if "refund" in (r.reason or "") and r.action == "request_refund"]
    assert execution, "expected an execution audit row"
    row = execution[-1]
    assert row.actor == "buyer_agent"
    assert row.decision == "approved"
    assert Decimal(str(row.requested_amount)) == Decimal("399.00")
    assert data["refund_id"] in row.reason
    assert data["razorpay_refund_id"] in row.reason
    assert "status=verified" in row.reason
    assert "INR" in row.reason


# --------------------------------------------------------------------------- #
# TEST 23: stream behavior of a refund (M21 2A: no frames until CP3's durable
# refund writer lands; a repeat must never move the transcript cursor)
# --------------------------------------------------------------------------- #
async def test_ws_events(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, refund_api
):
    client, test_session, setup = await _refundable_setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay
    )
    async with client:
        mission_id, issue_id = await _authorized_refundable_issue(client, test_session, setup)
        cursor_before = (
            await client.get(f"/missions/{mission_id}/agent/conversation")
        ).json()["latest_sequence"]
        data = (await _refund(client, mission_id, issue_id)).json()
        assert data["status"] == "verified"
        assert data["razorpay_refund_id"] == "rfnd_TEST1"

        # Neither the verified refund nor a repeat appends a conversation row (CP2);
        # CP3 will make exactly one durable event per real transition, never per repeat.
        assert (
            await client.get(f"/missions/{mission_id}/agent/conversation")
        ).json()["latest_sequence"] == cursor_before

        await _refund(client, mission_id, issue_id)
        assert (
            await client.get(f"/missions/{mission_id}/agent/conversation")
        ).json()["latest_sequence"] == cursor_before


# --------------------------------------------------------------------------- #
# TEST 20: refund state survives a simulated backend restart
# --------------------------------------------------------------------------- #
async def test_refund_persists_across_restart(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, refund_api
):
    make_client, test_session = restart_env
    async with make_client() as client_a:
        mission_id, issue_id = await _authorized_refundable_issue(
            client_a, test_session,
            lambda c: _verified_mission(c, install_mock_discovery, sample_products, mock_razorpay),
        )
        created = (await _refund(client_a, mission_id, issue_id)).json()

    async with make_client() as client_b:
        await _login(client_b, "olga@shop.com")
        issues = (await client_b.get(f"/missions/{mission_id}/order/issues")).json()["issues"]
        assert issues[0]["refund"]["refund_id"] == created["refund_id"]
        assert issues[0]["refund"]["status"] == "verified"
        assert issues[0]["status"] == "resolution_pending"
        # Repeated execution after restart still returns the existing verified refund
        again = await _refund(client_b, mission_id, issue_id)
        assert again.json()["already_existed"] is True
        assert refund_api.calls["create"] == 1
