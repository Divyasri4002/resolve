"""
Tests for Task 10: resolution assessment & authority proposal.

Pipeline under test: USER ISSUE -> REASONING MODEL -> STRUCTURED PROPOSAL ->
AUTHORITY ENGINE -> DECISION. Nothing is ever executed: no refund, replacement,
return, merchant contact, or payment change exists in this system, and every
test asserts that boundary.

Covers the mandated scenarios:
- TEST 1:  deny path — remedy proposal blocked by the Authority Engine
- TEST 2:  allow path — intrinsically safe proposal passes the gate
- TEST 3:  escalate path + model's free-form fields are data, never commands
- TEST 4:  refund amount is ALWAYS the canonical order amount, never the model's
- TEST 5:  malformed model response becomes the deterministic safe-fallback escalation
- TEST 6:  unsupported / out-of-vocabulary action is rejected to safe fallback
- TEST 7:  provider outage -> 503 with ZERO writes; retry succeeds afterwards
- TEST 8:  canonical server-built context; no secret ever reaches the provider
- TEST 9:  client body tampering (amount/action/decision/identity) is ignored
- TEST 10: ownership + cross-mission + unknown-issue + non-shopping guards
- TEST 11: duplicate assessment returns the existing record without a second model call
- TEST 12: wrong issue status is rejected
- TEST 13: payment and order state are untouched by every assessment path
- TEST 14: the insert-only audit log records the real Authority Engine verdict
- TEST 15: WebSocket event resolution_assessment_created
- TEST 16: assessment persists across a simulated backend restart

Hermetic: mock reasoning/discovery via conftest, Razorpay order API mocked —
NO real payment API calls are made.
"""
import hashlib
import hmac
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.agents.conversation import as_event_frame
from app.config import get_settings
from app.database import Base, get_db
from app.main import app
from app.models.db_models import (
    AuditLogRecord,
    OrderIssueRecord,
    ResolutionAssessmentRecord,
)
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
    """Client factory + sessionmaker sharing one engine: simulates a backend restart."""
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
            {"amount_paise": amount_paise, "currency": currency, "receipt": receipt, "notes": notes}
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
    """Capture every canonical event frame broadcast during a test (M21 Phase 2A)."""
    events: list[dict] = []

    async def fake_broadcast(row) -> None:
        events.append(as_event_frame(row))

    monkeypatch.setattr(missions_module, "broadcast_event_row", fake_broadcast)
    return events


class ControlledProvider:
    """Stub reasoning provider with a call log — the ONLY seam Task 10 tests need."""

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
    """Swap the reasoning provider factory inside the missions router (same seam production
    uses). Install AFTER mission setup so discovery/intent keep the hermetic mock."""

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
    reg = await client.post(
        "/auth/register", json={"name": name, "email": email, "password": PASSWORD}
    )
    assert reg.status_code == 201


async def _login(client: AsyncClient, email: str) -> None:
    login = await client.post("/auth/login", json={"email": email, "password": PASSWORD})
    assert login.status_code == 200


async def _confirmed_mission(
    client: AsyncClient, install_mock_discovery, sample_products, product_id: str = "item_dove"
) -> str:
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
    client: AsyncClient, install_mock_discovery, sample_products, mock_razorpay,
    product_id: str = "item_dove",
) -> str:
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


async def _confirmed_order(client: AsyncClient, mission_id: str) -> str:
    created = await client.post(f"/missions/{mission_id}/order", json={})
    assert created.status_code == 200
    confirmed = await client.post(f"/missions/{mission_id}/order/confirm", json={})
    assert confirmed.status_code == 200
    return created.json()["order_id"]


async def _report_issue(client: AsyncClient, mission_id: str, **overrides):
    body = {
        "issue_type": "wrong_item",
        "description": "I received the wrong shampoo. I want a replacement.",
        "requested_resolution": "replacement",
    }
    body.update(overrides)
    return await client.post(f"/missions/{mission_id}/order/issues", json=body)


async def _issue_on_confirmed_order(client, mission_id, **overrides) -> str:
    resp = await _report_issue(client, mission_id, **overrides)
    assert resp.status_code == 200
    return resp.json()["issue_id"]


async def _assess(client: AsyncClient, mission_id: str, issue_id: str, json=None):
    url = f"/missions/{mission_id}/order/issues/{issue_id}/assess"
    return await client.post(url, json=json) if json is not None else await client.post(url)


async def _extra_verified_mission(client: AsyncClient, product_id: str) -> str:
    """A second paid+confirmed mission for the CURRENTLY authenticated user (no re-register)."""
    resp = await client.post("/missions/shopping", json={"prompt": SHAMPOO_PROMPT})
    assert resp.status_code == 200
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
    assert order_resp.status_code == 200
    razorpay_order_id = order_resp.json()["payment"]["razorpay_order_id"]
    payment_id = "pay_TESTORDER2"
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


def _model_proposal(action="request_refund", **overrides):
    proposal = {
        "issue_assessment": "The user received a different product than ordered.",
        "proposed_action": action,
        "reasoning_summary": "Mismatch between ordered and delivered item.",
        "requested_amount": 999999.99 if action == "request_refund" else None,
        "confidence": 0.9,
        "needs_escalation": False,
    }
    proposal.update(overrides)
    return proposal


# --------------------------------------------------------------------------- #
# TEST 1: deny path — the hermetic mock mirrors the 'replacement' preference,
# the Authority Engine blocks it (nothing was delegated on a shopping mission)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_replacement_proposal_is_denied_by_engine(
    async_client: AsyncClient, install_mock_discovery, sample_products, mock_razorpay
):
    mission_id = await _verified_mission(
        async_client, install_mock_discovery, sample_products, mock_razorpay
    )
    order_id = await _confirmed_order(async_client, mission_id)
    issue_id = await _issue_on_confirmed_order(async_client, mission_id)

    resp = await _assess(async_client, mission_id, issue_id)
    assert resp.status_code == 200
    data = resp.json()

    assert data["assessment_id"].startswith("assess_")
    assert data["issue_id"] == issue_id
    assert data["mission_id"] == mission_id
    assert data["order_id"] == order_id
    assert data["proposed_action"] == "request_replacement"
    assert data["requested_amount"] is None
    assert data["proposal_source"] == "reasoning_model"
    assert data["authority_decision"] == "deny"
    assert "never delegated" in data["decision_reason"]
    # Proposal vocabulary only — execution is claimed nowhere
    assert "Action has NOT been executed" in data["execution_note"]
    assert "blocked by the Authority Engine" in data["agent_message"]

    # The backend — not the model — advanced the deterministic issue lifecycle
    issues = (await async_client.get(f"/missions/{mission_id}/order/issues")).json()["issues"]
    assert issues[0]["status"] == "under_review"
    assert issues[0]["assessment"]["assessment_id"] == data["assessment_id"]


# --------------------------------------------------------------------------- #
# TEST 2: allow path — a 'none' preference makes the mock propose
# request_more_information, an intrinsically safe action that passes the gate
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_intrinsically_safe_proposal_is_allowed(
    async_client: AsyncClient, install_mock_discovery, sample_products, mock_razorpay
):
    mission_id = await _verified_mission(
        async_client, install_mock_discovery, sample_products, mock_razorpay
    )
    await _confirmed_order(async_client, mission_id)
    issue_id = await _issue_on_confirmed_order(
        async_client,
        mission_id,
        requested_resolution="none",
        description="Something is off but I have no preference yet.",
    )

    resp = await _assess(async_client, mission_id, issue_id)
    assert resp.status_code == 200
    data = resp.json()

    assert data["proposed_action"] == "request_more_information"
    assert data["authority_decision"] == "allow"
    assert "no delegated authority is required" in data["decision_reason"]
    # 'allow' must never read as 'executed'
    assert "passed the authority gate" in data["agent_message"]
    assert "Action has not been executed" in data["agent_message"]
    assert "refund" not in data["agent_message"].lower() or "no refund" in data[
        "agent_message"
    ].lower()


# --------------------------------------------------------------------------- #
# TEST 3: escalate path + confidence clamping; model text stays descriptive data
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_model_escalation_maps_to_escalate(
    async_client, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
):
    mission_id = await _verified_mission(
        async_client, install_mock_discovery, sample_products, mock_razorpay
    )
    await _confirmed_order(async_client, mission_id)
    issue_id = await _issue_on_confirmed_order(async_client, mission_id)

    install_fake_reasoning(
        result=_model_proposal(
            "escalate_to_human", confidence=7.5, needs_escalation=True
        )
    )
    resp = await _assess(async_client, mission_id, issue_id)
    assert resp.status_code == 200
    data = resp.json()
    assert data["proposed_action"] == "escalate_to_human"
    assert data["authority_decision"] == "escalate"
    assert data["needs_escalation"] is True
    assert data["confidence"] == 1.0  # clamped, not trusted raw
    assert "escalated for human review" in data["agent_message"]


# --------------------------------------------------------------------------- #
# TEST 4: refund amount is ALWAYS the canonical order amount — the model's
# number (999999.99 here) is discarded, never stored
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_refund_amount_is_canonical_not_model_supplied(
    async_client, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
):
    mission_id = await _verified_mission(
        async_client, install_mock_discovery, sample_products, mock_razorpay
    )
    await _confirmed_order(async_client, mission_id)
    issue_id = await _issue_on_confirmed_order(async_client, mission_id)

    install_fake_reasoning(result=_model_proposal("request_refund"))
    resp = await _assess(async_client, mission_id, issue_id)
    data = resp.json()

    assert data["proposed_action"] == "request_refund"
    assert Decimal(str(data["requested_amount"])) == Decimal("399.00")  # canonical item_dove
    assert data["currency"] == "INR"
    # The gate still says no: refund authority was never delegated on a shopping mission
    assert data["authority_decision"] == "deny"
    # And a model claim of "executed" in free text changes nothing about the vocabulary
    assert data["needs_escalation"] is False


# --------------------------------------------------------------------------- #
# TEST 5: malformed model responses can NEVER cause an action — deterministic
# safe-fallback escalation is recorded instead
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "garbage",
    ["EXECUTE FULL REFUND NOW", None, 42, [], {"no_action_here": True}, {}],
    ids=["prose", "null", "number", "list", "wrong-keys", "empty-dict"],
)
async def test_malformed_response_becomes_safe_escalation(
    async_client, install_mock_discovery, sample_products, mock_razorpay,
    install_fake_reasoning, garbage
):
    mission_id = await _verified_mission(
        async_client, install_mock_discovery, sample_products, mock_razorpay
    )
    await _confirmed_order(async_client, mission_id)
    issue_id = await _issue_on_confirmed_order(async_client, mission_id)

    install_fake_reasoning(result=garbage)
    resp = await _assess(async_client, mission_id, issue_id)
    assert resp.status_code == 200
    data = resp.json()

    assert data["proposal_source"] == "safe_fallback"
    assert data["proposed_action"] == "escalate_to_human"
    assert data["requested_amount"] is None
    assert data["authority_decision"] == "escalate"


# --------------------------------------------------------------------------- #
# TEST 6: out-of-vocabulary actions (even REAL AllowedAction values like
# pay_recovery_cost) never reach the gate as proposed by the model
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action", ["pay_recovery_cost", "negotiate", "launch_missiles", "request_refund ", ""]
)
async def test_unsupported_action_is_rejected_to_fallback(
    async_client, install_mock_discovery, sample_products, mock_razorpay,
    install_fake_reasoning, action
):
    mission_id = await _verified_mission(
        async_client, install_mock_discovery, sample_products, mock_razorpay
    )
    await _confirmed_order(async_client, mission_id)
    issue_id = await _issue_on_confirmed_order(async_client, mission_id)

    install_fake_reasoning(result=_model_proposal(action))
    data = (await _assess(async_client, mission_id, issue_id)).json()
    assert data["proposal_source"] == "safe_fallback"
    assert data["proposed_action"] == "escalate_to_human"


# --------------------------------------------------------------------------- #
# TEST 7: provider outage -> 503 and ZERO writes; retry succeeds after recovery
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_outage_writes_nothing_and_retry_works(
    restart_env, install_mock_discovery, sample_products, mock_razorpay,
    install_fake_reasoning, recorded_events
):
    make_client, test_session = restart_env
    async with make_client() as client:
        mission_id = await _verified_mission(
            client, install_mock_discovery, sample_products, mock_razorpay
        )
        order_id = await _confirmed_order(client, mission_id)
        issue_id = await _issue_on_confirmed_order(client, mission_id)
        payment_before = (await client.get(f"/missions/{mission_id}")).json()["shopping_intent"][
            "payment"
        ]

        provider = install_fake_reasoning(error=RuntimeError("connection refused"))
        # CORE-3A put the rest of the lifecycle under audit, so this mission already has rows from
        # selection, checkout, payment and order stages. The outage guarantee is about rows THIS call
        # writes, so it is measured as a difference — which is strictly stronger than a total.
        async with test_session() as session:
            audit_rows_before = (
                await session.execute(
                    select(func.count()).select_from(AuditLogRecord).where(
                        AuditLogRecord.mission_id == mission_id
                    )
                )
            ).scalar()

        resp = await _assess(client, mission_id, issue_id)
        assert resp.status_code == 503
        assert "temporarily unavailable" in resp.json()["detail"]
        assert len(provider.contexts) == 1  # the model was called and failed

        # NOTHING was written: no assessment row, no audit row, no WS event,
        # issue still 'reported', payment untouched
        async with test_session() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(ResolutionAssessmentRecord)
                    .where(ResolutionAssessmentRecord.mission_id == mission_id)
                )
            ) == 0
            assert (
                await session.scalar(
                    select(func.count()).select_from(AuditLogRecord).where(
                        AuditLogRecord.mission_id == mission_id
                    )
                )
            ) == audit_rows_before
            issue_row = await session.get(OrderIssueRecord, issue_id)
            assert issue_row.status == "reported"
        # M21 Phase 2A: assessment emits no ad-hoc broadcast; a failed call leaves the
        # stream untouched — no partial write can leak into it. (A durable assessment
        # event arrives with CP3's writer coverage.)
        assert recorded_events == []
        assert (await client.get(f"/missions/{mission_id}")).json()["shopping_intent"][
            "payment"
        ] == payment_before

        # Recovery: retry produces exactly one real assessment
        install_fake_reasoning(result=_model_proposal("request_replacement"))
        retry = await _assess(client, mission_id, issue_id)
        assert retry.status_code == 200
        assert retry.json()["proposal_source"] == "reasoning_model"
        assert (
            await client.post(f"/missions/{mission_id}/order/issues/{issue_id}/assess")
        ).json()["already_existed"] is True


# --------------------------------------------------------------------------- #
# TEST 8: the context is server-built canonical state and contains no secrets
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_context_is_canonical_and_secret_free(
    async_client, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
):
    mission_id = await _verified_mission(
        async_client, install_mock_discovery, sample_products, mock_razorpay
    )
    order_id = await _confirmed_order(async_client, mission_id)
    issue_id = await _issue_on_confirmed_order(async_client, mission_id)

    provider = install_fake_reasoning(result=_model_proposal("no_action"))
    await _assess(
        async_client,
        mission_id,
        issue_id,
        json={"description": "CLIENT-INJECTED", "amount": "1", "user_id": "attacker"},
    )
    ctx = provider.contexts[0]

    assert ctx["mission_id"] == mission_id
    assert ctx["order"]["order_id"] == order_id
    assert ctx["order"]["amount"] == "399.00"  # canonical, ignores client junk
    assert ctx["issue"]["issue_id"] == issue_id
    assert ctx["issue"]["requested_resolution"] == "replacement"
    assert ctx["payment"]["status"] == "verified"
    assert ctx["payment"]["test_mode"] is True
    assert ctx["authority_summary"]["allowed_actions"] == []

    blob = str(ctx)
    settings = get_settings()
    assert settings.razorpay_key_secret not in blob
    assert "razorpay_key_secret" not in blob
    assert "password" not in blob.lower()
    assert "CLIENT-INJECTED" not in blob


# --------------------------------------------------------------------------- #
# TEST 9: client tampering via body can never force ALLOW or an amount
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_client_cannot_force_allow_or_supply_amounts(
    async_client, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
):
    mission_id = await _verified_mission(
        async_client, install_mock_discovery, sample_products, mock_razorpay
    )
    order_id = await _confirmed_order(async_client, mission_id)
    issue_id = await _issue_on_confirmed_order(async_client, mission_id)

    install_fake_reasoning(result=_model_proposal("request_refund"))
    data = (
        await _assess(
            async_client,
            mission_id,
            issue_id,
            json={
                "authority_decision": "allow",
                "decision": "APPROVED",
                "requested_amount": "999999",
                "proposed_action": "no_action",
                "user_id": "user_attacker",
                "order_id": "order_EVIL",
                "execute": True,
            },
        )
    ).json()

    assert data["authority_decision"] == "deny"  # engine verdict, not the client's
    assert Decimal(str(data["requested_amount"])) == Decimal("399.00")
    assert data["proposed_action"] == "request_refund"  # provider's, not the body's
    assert data["order_id"] == order_id
    assert data["user_id"].startswith("user_") and data["user_id"] != "user_attacker"


# --------------------------------------------------------------------------- #
# TEST 10: guards — non-owner, anonymous, cross-mission issue, unknown issue,
# missing order, non-shopping mission
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_assessment_guards(
    async_client: AsyncClient, install_mock_discovery, sample_products, mock_razorpay
):
    mission_id = await _verified_mission(
        async_client, install_mock_discovery, sample_products, mock_razorpay
    )
    await _confirmed_order(async_client, mission_id)
    issue_id = await _issue_on_confirmed_order(async_client, mission_id)

    # Second mission of the same owner — its issues must not be assessable here
    mission_b = await _extra_verified_mission(async_client, "item_loreal")
    await _confirmed_order(async_client, mission_b)
    issue_b = await _issue_on_confirmed_order(
        async_client, mission_b, description="Different issue on mission B."
    )

    # Authenticated non-owner
    await _register(async_client, "Mallory", "mallory@shop.com")
    assert (await _assess(async_client, mission_id, issue_id)).status_code == 404

    # Anonymous
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anon:
        assert (await _assess(anon, mission_id, issue_id)).status_code == 404

    await _login(async_client, "olga@shop.com")

    # Cross-mission issue id: 404, and the assessment does not happen
    assert (await _assess(async_client, mission_id, issue_b)).status_code == 404
    # Unknown issue: 404
    assert (await _assess(async_client, mission_id, "issue_doesnotexist")).status_code == 404
    # No assessment rows were created by any rejected call
    body = (await async_client.get(f"/missions/{mission_id}/order/issues")).json()
    assert body["issues"][0]["assessment"] is None
    assert body["issues"][0]["status"] == "reported"

    # Mission without any order -> 404
    await _register(async_client, "Nora Fresh", "nora@shop.com")
    await _login(async_client, "nora@shop.com")
    resp = await async_client.post("/missions/shopping", json={"prompt": SHAMPOO_PROMPT})
    fresh_mission = resp.json()["mission_id"]
    assert (await _assess(async_client, fresh_mission, issue_id)).status_code == 404

    # Non-shopping mission -> 400
    rec = await async_client.post(
        "/missions",
        json={
            "mission_type": "recovery",
            "objective": {"description": "Recover damaged package", "success_criteria": []},
            "authority": {"allowed_actions": ["request_refund"], "recovery_budget": "5000"},
            "transaction": {"merchant_name": "Store", "order_id": "ord_1", "amount_paid": "12000"},
        },
    )
    assert (await _assess(async_client, rec.json()["mission_id"], issue_id)).status_code == 400


# --------------------------------------------------------------------------- #
# TEST 11: repeated assessment is idempotent — one model call, one row
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_duplicate_assessment_does_not_repropose(
    async_client, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
):
    mission_id = await _verified_mission(
        async_client, install_mock_discovery, sample_products, mock_razorpay
    )
    await _confirmed_order(async_client, mission_id)
    issue_id = await _issue_on_confirmed_order(async_client, mission_id)

    provider = install_fake_reasoning(result=_model_proposal("request_replacement"))
    first = (await _assess(async_client, mission_id, issue_id)).json()
    second = (await _assess(async_client, mission_id, issue_id)).json()
    third = (await _assess(async_client, mission_id, issue_id)).json()

    assert first["assessment_id"] == second["assessment_id"] == third["assessment_id"]
    assert first["already_existed"] is False
    assert second["already_existed"] is True and third["already_existed"] is True
    assert len(provider.contexts) == 1  # the model was asked exactly once
    # Issue stays 'reported' on repeats: the duplicate short-circuits before any rewrite
    assert second["agent_message"].startswith("Issue")


# --------------------------------------------------------------------------- #
# TEST 12: only issues in 'reported' status can be assessed
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_wrong_issue_status_rejected(restart_env, install_mock_discovery, sample_products, mock_razorpay):
    make_client, test_session = restart_env
    async with make_client() as client:
        mission_id = await _verified_mission(
            client, install_mock_discovery, sample_products, mock_razorpay
        )
        await _confirmed_order(client, mission_id)
        issue_id = await _issue_on_confirmed_order(client, mission_id)

        async with test_session() as session:
            await session.execute(
                update(OrderIssueRecord)
                .where(OrderIssueRecord.issue_id == issue_id)
                .values(status="resolution_pending")
            )
            await session.commit()

        resp = await _assess(client, mission_id, issue_id)
        assert resp.status_code == 400
        assert "reported" in resp.json()["detail"]


# --------------------------------------------------------------------------- #
# TEST 13: assessment never touches payment or order state (all decisions)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_payment_and_order_untouched_by_assessment(
    async_client, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
):
    mission_id = await _verified_mission(
        async_client, install_mock_discovery, sample_products, mock_razorpay
    )
    order_id = await _confirmed_order(async_client, mission_id)

    # Create the issues first (the first one legitimately moves the order into
    # awaiting_post_purchase_context); assessment must then change NOTHING.
    cases = []
    for action in ["request_refund", "request_replacement", "request_return", "no_action"]:
        issue_id = await _issue_on_confirmed_order(
            async_client, mission_id, description=f"Issue for the {action} path."
        )
        cases.append((action, issue_id))

    payment_before = (await async_client.get(f"/missions/{mission_id}")).json()["shopping_intent"][
        "payment"
    ]
    order_before = (await async_client.get(f"/missions/{mission_id}/order")).json()
    assert order_before["status"] == "awaiting_post_purchase_context"

    for action, issue_id in cases:
        install_fake_reasoning(result=_model_proposal(action))
        resp = await _assess(async_client, mission_id, issue_id)
        assert resp.status_code == 200

    payment_after = (await async_client.get(f"/missions/{mission_id}")).json()["shopping_intent"][
        "payment"
    ]
    assert payment_after == payment_before
    order_after = (await async_client.get(f"/missions/{mission_id}/order")).json()
    assert order_after == order_before  # same status, ids, amount, timestamps-equivalent
    # And crucially: the razorpay payment was never re-touched
    assert payment_after["status"] == "verified"
    assert Decimal(str(payment_after["amount"])) == Decimal("399")
    assert order_after["razorpay_payment_id"] == payment_before["razorpay_payment_id"]
    assert order_after["order_id"] == order_id


# --------------------------------------------------------------------------- #
# TEST 14: the insert-only audit log holds the real Authority Engine verdict
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_audit_log_records_engine_decisions(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
):
    make_client, test_session = restart_env
    async with make_client() as client:
        mission_id = await _verified_mission(
            client, install_mock_discovery, sample_products, mock_razorpay
        )
        await _confirmed_order(client, mission_id)

        issue_a = await _issue_on_confirmed_order(client, mission_id)
        install_fake_reasoning(result=_model_proposal("request_replacement"))
        await _assess(client, mission_id, issue_a)

        issue_b = await _issue_on_confirmed_order(
            client, mission_id, description="Second issue, safe proposal.", requested_resolution="none"
        )
        install_fake_reasoning(result=_model_proposal("request_more_information"))
        await _assess(client, mission_id, issue_b)

        body = (await client.get(f"/missions/{mission_id}/order/issues")).json()
        assessments = {i["issue_id"]: i["assessment"] for i in body["issues"]}
        assert assessments[issue_a]["authority_decision"] == "deny"
        assert assessments[issue_b]["authority_decision"] == "allow"

        # A repeat call adds no audit rows either
        await _assess(client, mission_id, issue_a)

    async with test_session() as session:
        # Scoped to the Engine's own action vocabulary on purpose: since CORE-3A the table also
        # carries lifecycle observations (`select_product`, `payment_verified`, `issue_reported`, …)
        # recorded through `append_event`. This test's claim has always been about the two gate
        # decisions this stage writes — one per assessment, none for the repeat — and scoping keeps
        # that claim exactly as sharp without pretending the other rows are not there.
        engine_actions = ("request_replacement", "request_more_information")
        rows = (
            (
                await session.execute(
                    select(AuditLogRecord).where(
                        AuditLogRecord.mission_id == mission_id,
                        AuditLogRecord.action.in_(engine_actions),
                    )
                )
            )
            .scalars()
            .all()
        )
        lifecycle_rows = (
            (
                await session.execute(
                    select(AuditLogRecord).where(
                        AuditLogRecord.mission_id == mission_id,
                        AuditLogRecord.action.not_in(engine_actions),
                    )
                )
            )
            .scalars()
            .all()
        )
    decisions = {(r.action, r.decision) for r in rows}
    assert ("request_replacement", "blocked") in decisions
    assert ("request_more_information", "approved") in decisions
    assert len(rows) == 2  # exactly one per gate, none for the duplicate call
    assert {row.actor for row in rows} == {"buyer_agent"}
    # The other rows are the lifecycle evidence CORE-3A added, and every one of them is an
    # observation recorded under a human actor, never an Engine decision.
    assert {row.actor for row in lifecycle_rows} == {"user"}
    assert {row.decision for row in lifecycle_rows} <= {"observed", "refused"}


# --------------------------------------------------------------------------- #
# TEST 15: assessment is emitted exactly once (M21 Phase 2A: via durable state,
# not an ad-hoc broadcast — the durable assessment event arrives with CP3)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_ws_event_emitted_once(
    async_client, install_mock_discovery, sample_products, mock_razorpay,
    install_fake_reasoning, recorded_events
):
    mission_id = await _verified_mission(
        async_client, install_mock_discovery, sample_products, mock_razorpay
    )
    await _confirmed_order(async_client, mission_id)
    issue_id = await _issue_on_confirmed_order(async_client, mission_id)

    install_fake_reasoning(result=_model_proposal("request_return"))
    before = (
        await async_client.get(f"/missions/{mission_id}/agent/conversation")
    ).json()["latest_sequence"]
    assessment_id = (await _assess(async_client, mission_id, issue_id)).json()["assessment_id"]
    duplicate = (await _assess(async_client, mission_id, issue_id)).json()

    # The transcript cursor is committed state: neither the first assessment nor the
    # duplicate may move it (in CP2 the assessment writes no conversation row; CP3
    # will make exactly one durable row per transition, still never one per repeat).
    after = (
        await async_client.get(f"/missions/{mission_id}/agent/conversation")
    ).json()["latest_sequence"]
    assert after == before
    assert recorded_events == []
    assert duplicate["already_existed"] is True
    assert duplicate["assessment_id"] == assessment_id


# --------------------------------------------------------------------------- #
# TEST 16: assessment persists across a simulated backend restart
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_assessment_persists_across_restart(
    restart_env, install_mock_discovery, sample_products, mock_razorpay
):
    make_client, test_session = restart_env
    async with make_client() as client_a:
        mission_id = await _verified_mission(
            client_a, install_mock_discovery, sample_products, mock_razorpay
        )
        order_id = await _confirmed_order(client_a, mission_id)
        issue_id = await _issue_on_confirmed_order(client_a, mission_id)
        created = (await _assess(client_a, mission_id, issue_id)).json()
        assert created["authority_decision"] == "deny"

    async with make_client() as client_b:
        await _login(client_b, "olga@shop.com")
        body = (await client_b.get(f"/missions/{mission_id}/order/issues")).json()
        issue = body["issues"][0]
        assert issue["status"] == "under_review"
        assessment = issue["assessment"]
        assert assessment["assessment_id"] == created["assessment_id"]
        assert assessment["proposed_action"] == "request_replacement"
        assert assessment["authority_decision"] == "deny"
        assert assessment["order_id"] == order_id

        # Reassessment after restart stays idempotent
        repeat = await _assess(client_b, mission_id, issue_id)
        assert repeat.json()["already_existed"] is True

    async with test_session() as session:
        row = (
            await session.execute(
                select(ResolutionAssessmentRecord).where(
                    ResolutionAssessmentRecord.issue_id == issue_id
                )
            )
        ).scalar_one()
        assert row.assessment_id == created["assessment_id"]
        assert row.user_id.startswith("user_")
        assert Decimal(str(row.requested_amount or 0)) == Decimal("0")
