"""
Tests for Task 09: post-purchase order context & issue intake.

Covers the mandated scenarios:
- TEST 1:  successful issue creation on an order_confirmed mission (-> reported)
- TEST 2:  issue persistence + listing via GET
- TEST 3:  ownership — owner creates/reads; non-owner and anonymous get 404
- TEST 4:  non-shopping mission rejected
- TEST 5:  invalid mission state — no order (404) / order not confirmed (400)
- TEST 6:  malformed issue type rejected
- TEST 7:  malformed requested_resolution rejected
- TEST 8:  description validation (empty / oversized)
- TEST 9:  client tampering (user_id/order_id/payment ids/amounts/authority) ignored
- TEST 10: duplicate behavior — exact repeat idempotent, different issue allowed
- TEST 11: WebSocket event order_issue_reported
- TEST 12: requested resolution stored as preference, never authorization;
           agent context exposure (deterministic agent_message)
- TEST 13: issue persists across a simulated backend restart
- TEST 14: order transitions to awaiting_post_purchase_context on first issue

Hermetic: mock reasoning/discovery via conftest, Razorpay order API mocked —
NO real payment API calls are made.
"""
import hashlib
import hmac
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.agents.conversation import as_event_frame
from app.config import get_settings
from app.database import Base, get_db
from app.main import app
from app.models.db_models import OrderIssueRecord
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
    filtered on event_type/payload rather than an ad-hoc "event" key.
    """
    events: list[dict] = []

    async def fake_broadcast(row) -> None:
        events.append(as_event_frame(row))

    monkeypatch.setattr(missions_module, "broadcast_event_row", fake_broadcast)
    return events


def _checkout_signature(order_id: str, payment_id: str) -> str:
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
    """Run the Task 08 lifecycle to order_confirmed; return the canonical order id."""
    created = await client.post(f"/missions/{mission_id}/order", json={})
    assert created.status_code == 200
    confirmed = await client.post(f"/missions/{mission_id}/order/confirm", json={})
    assert confirmed.status_code == 200
    return created.json()["order_id"]


async def _report_issue(client: AsyncClient, mission_id: str, extra=None, **overrides):
    body = {
        "issue_type": "wrong_item",
        "description": "I received the wrong shampoo. I want a replacement.",
        "requested_resolution": "replacement",
    }
    body.update(overrides)
    if extra:
        body.update(extra)
    return await client.post(f"/missions/{mission_id}/order/issues", json=body)


# --------------------------------------------------------------------------- #
# TEST 1: successful issue creation on an order_confirmed mission
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_issue_created_on_confirmed_order(
    async_client: AsyncClient, install_mock_discovery, sample_products, mock_razorpay
):
    mission_id = await _verified_mission(
        async_client, install_mock_discovery, sample_products, mock_razorpay
    )
    order_id = await _confirmed_order(async_client, mission_id)

    resp = await _report_issue(
        async_client,
        mission_id,
        evidence=[{"kind": "photo", "url": "https://example.com/receipt.jpg"}],
    )
    assert resp.status_code == 200
    data = resp.json()

    assert data["issue_id"].startswith("issue_")
    assert data["order_id"] == order_id
    assert data["mission_id"] == mission_id
    assert data["issue_type"] == "wrong_item"
    assert data["requested_resolution"] == "replacement"
    assert data["status"] == "reported"
    assert data["already_existed"] is False
    assert len(data["evidence"]) == 1
    assert data["created_at"] and data["updated_at"]
    assert "preference only" in data["agent_message"]


# --------------------------------------------------------------------------- #
# TEST 2: persistence + listing
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_issue_persists_and_lists(
    async_client: AsyncClient, install_mock_discovery, sample_products, mock_razorpay
):
    mission_id = await _verified_mission(
        async_client, install_mock_discovery, sample_products, mock_razorpay
    )
    order_id = await _confirmed_order(async_client, mission_id)
    created = (await _report_issue(async_client, mission_id)).json()

    for _ in range(2):  # simulate refresh + reopen reads
        resp = await async_client.get(f"/missions/{mission_id}/order/issues")
        assert resp.status_code == 200
        body = resp.json()
        assert body["mission_id"] == mission_id
        assert body["order_id"] == order_id
        assert len(body["issues"]) == 1
        issue = body["issues"][0]
        assert issue["issue_id"] == created["issue_id"]
        assert issue["issue_type"] == "wrong_item"
        assert issue["status"] == "reported"


# --------------------------------------------------------------------------- #
# TEST 3: ownership — non-owner and anonymous get 404, nothing leaked
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_ownership_enforced_on_create_and_list(
    async_client: AsyncClient, install_mock_discovery, sample_products, mock_razorpay
):
    mission_id = await _verified_mission(
        async_client, install_mock_discovery, sample_products, mock_razorpay
    )
    await _confirmed_order(async_client, mission_id)

    # Authenticated non-owner
    await _register(async_client, "Mallory", "mallory@shop.com")
    assert (await _report_issue(async_client, mission_id)).status_code == 404
    assert (
        await async_client.get(f"/missions/{mission_id}/order/issues")
    ).status_code == 404

    # Anonymous
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as anon_client:
        assert (await _report_issue(anon_client, mission_id)).status_code == 404
        assert (
            await anon_client.get(f"/missions/{mission_id}/order/issues")
        ).status_code == 404

    # Owner still reports fine; nothing was created by the attackers
    await _login(async_client, "olga@shop.com")
    assert (await _report_issue(async_client, mission_id)).status_code == 200
    body = (await async_client.get(f"/missions/{mission_id}/order/issues")).json()
    assert len(body["issues"]) == 1


# --------------------------------------------------------------------------- #
# TEST 4: non-shopping mission rejected
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_non_shopping_mission_rejected(
    async_client: AsyncClient, install_mock_discovery, sample_products, mock_razorpay
):
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
    assert rec.status_code == 200
    resp = await _report_issue(async_client, rec.json()["mission_id"])
    assert resp.status_code == 400


# --------------------------------------------------------------------------- #
# TEST 5: invalid mission state — no order / order not confirmed
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_issue_requires_confirmed_order(
    async_client: AsyncClient, install_mock_discovery, sample_products, mock_razorpay
):
    mission_id = await _verified_mission(
        async_client, install_mock_discovery, sample_products, mock_razorpay
    )

    # payment_verified but no RESOLVE order yet -> 404 (no order exists)
    resp = await _report_issue(async_client, mission_id)
    assert resp.status_code == 404
    assert (
        await async_client.get(f"/missions/{mission_id}/order/issues")
    ).status_code == 404

    # order_created but NOT confirmed -> 400
    created = await async_client.post(f"/missions/{mission_id}/order", json={})
    assert created.status_code == 200
    resp = await _report_issue(async_client, mission_id)
    assert resp.status_code == 400
    assert "confirm" in resp.json()["detail"].lower()

    # after confirming -> 200
    assert (
        await async_client.post(f"/missions/{mission_id}/order/confirm", json={})
    ).status_code == 200
    assert (await _report_issue(async_client, mission_id)).status_code == 200


# --------------------------------------------------------------------------- #
# TEST 6: malformed issue type rejected
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_invalid_issue_type_rejected(
    async_client: AsyncClient, install_mock_discovery, sample_products, mock_razorpay
):
    mission_id = await _verified_mission(
        async_client, install_mock_discovery, sample_products, mock_razorpay
    )
    await _confirmed_order(async_client, mission_id)

    for bad in ["", "lost_cat", "REFUND", "refund", {"evil": True}]:
        resp = await _report_issue(async_client, mission_id, issue_type=bad)
        assert resp.status_code == 422, f"issue_type={bad!r} accepted"

    # every controlled category is accepted
    for good in [
        "damaged",
        "defective",
        "wrong_item",
        "missing_item",
        "not_received",
        "quality_issue",
        "other",
    ]:
        resp = await _report_issue(
            async_client, mission_id, issue_type=good, description=f"detail for {good}"
        )
        assert resp.status_code == 200


# --------------------------------------------------------------------------- #
# TEST 7: malformed requested_resolution rejected
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_invalid_requested_resolution_rejected(
    async_client: AsyncClient, install_mock_discovery, sample_products, mock_razorpay
):
    mission_id = await _verified_mission(
        async_client, install_mock_discovery, sample_products, mock_razorpay
    )
    await _confirmed_order(async_client, mission_id)

    resp = await _report_issue(async_client, mission_id, requested_resolution="free_house")
    assert resp.status_code == 422

    # omitted preference defaults to "none"
    resp = await _report_issue(
        async_client, mission_id, requested_resolution=None, description="no preference given"
    )
    assert resp.status_code == 200
    assert resp.json()["requested_resolution"] == "none"


# --------------------------------------------------------------------------- #
# TEST 8: description validation
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_description_validation(
    async_client: AsyncClient, install_mock_discovery, sample_products, mock_razorpay
):
    mission_id = await _verified_mission(
        async_client, install_mock_discovery, sample_products, mock_razorpay
    )
    await _confirmed_order(async_client, mission_id)

    for bad in ["", "     ", "x" * 2001]:
        resp = await _report_issue(async_client, mission_id, description=bad)
        assert resp.status_code == 422, f"description={bad[:10]!r} accepted"

    resp = await _report_issue(async_client, mission_id, description="x" * 2000)
    assert resp.status_code == 200
    # description is stored stripped
    resp = await _report_issue(async_client, mission_id, description="  padded  ")
    assert resp.json()["description"] == "padded"


# --------------------------------------------------------------------------- #
# TEST 9: client tampering — identity/payment/authority fields are ignored
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_client_tampering_ignored(
    async_client: AsyncClient, install_mock_discovery, sample_products, mock_razorpay
):
    mission_id = await _verified_mission(
        async_client, install_mock_discovery, sample_products, mock_razorpay
    )
    order_id = await _confirmed_order(async_client, mission_id)
    payment = (await async_client.get(f"/missions/{mission_id}")).json()["shopping_intent"][
        "payment"
    ]

    resp = await _report_issue(
        async_client,
        mission_id,
        extra={"mission_id": "mission_EVIL"},
        order_id="order_EVIL",
        user_id="user_attacker",
        razorpay_payment_id="pay_EVIL",
        payment_id="pay_EVIL",
        amount="1.00",
        refund_amount="999999",
        status="resolution_pending",
        replacement_authorized=True,
        authority={"allowed_actions": ["pay_recovery_cost"], "recovery_budget": "99999"},
    )
    assert resp.status_code == 200
    data = resp.json()

    # Canonical associations come from the authenticated mission + its order row
    assert data["order_id"] == order_id
    assert data["mission_id"] == mission_id
    assert data["user_id"] != "user_attacker"
    assert data["user_id"].startswith("user_")
    assert data["status"] == "reported"
    assert data["issue_id"].startswith("issue_")
    # No decision/authority fields exist in the stored record at all
    assert "refund_amount" not in data
    assert "replacement_authorized" not in data
    assert "razorpay_payment_id" not in data
    # The persisted payment references are untouched by the report
    after = (await async_client.get(f"/missions/{mission_id}")).json()["shopping_intent"][
        "payment"
    ]
    assert after["razorpay_payment_id"] == payment["razorpay_payment_id"]
    assert Decimal(str(after["amount"])) == Decimal("399")


# --------------------------------------------------------------------------- #
# TEST 10: duplicate behavior — exact repeat idempotent; different issue allowed
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_duplicate_behavior(
    async_client: AsyncClient, install_mock_discovery, sample_products, mock_razorpay
):
    mission_id = await _verified_mission(
        async_client, install_mock_discovery, sample_products, mock_razorpay
    )
    await _confirmed_order(async_client, mission_id)

    first = await _report_issue(async_client, mission_id)
    assert first.status_code == 200
    issue_id = first.json()["issue_id"]

    # Exact same request again: same issue, already_existed, no new row
    repeat = await _report_issue(async_client, mission_id)
    assert repeat.status_code == 200
    assert repeat.json()["issue_id"] == issue_id
    assert repeat.json()["already_existed"] is True

    # A genuinely different issue on the same order IS allowed
    different = await _report_issue(
        async_client,
        mission_id,
        issue_type="damaged",
        description="The bottle arrived cracked and leaking.",
    )
    assert different.status_code == 200
    assert different.json()["issue_id"] != issue_id

    body = (await async_client.get(f"/missions/{mission_id}/order/issues")).json()
    assert len(body["issues"]) == 2

    # Same type + resolution but different description = different issue
    third = await _report_issue(
        async_client, mission_id, description="Different wording, same wrong item."
    )
    assert third.json()["already_existed"] is False
    assert (
        len((await async_client.get(f"/missions/{mission_id}/order/issues")).json()["issues"])
        == 3
    )


# --------------------------------------------------------------------------- #
# TEST 11: WebSocket event order_issue_reported
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_issue_event_broadcast(
    async_client: AsyncClient,
    install_mock_discovery,
    sample_products,
    mock_razorpay,
    recorded_events,
):
    mission_id = await _verified_mission(
        async_client, install_mock_discovery, sample_products, mock_razorpay
    )
    await _confirmed_order(async_client, mission_id)
    created = (await _report_issue(async_client, mission_id)).json()

    issue_events = [
        e for e in recorded_events
        if e.get("event_type") == "system_state_change"
        and e.get("payload", {}).get("state") == "issue_reported"
    ]
    assert len(issue_events) == 1
    event = issue_events[0]
    assert event["kind"] == "event"
    assert event["mission_id"] == mission_id
    # The conversation row is deliberately narrow: issue identity, not the order row.
    assert event["payload"]["issue_id"] == created["issue_id"]
    assert event["payload"]["issue_type"] == "wrong_item"

    # A duplicate submission appends no row and therefore broadcasts nothing — no state changed
    await _report_issue(async_client, mission_id)
    assert len(issue_events) == 1


# --------------------------------------------------------------------------- #
# TEST 12: preference not authorization + agent context exposure
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_requested_resolution_is_preference_and_agent_context_exposed(
    async_client: AsyncClient, install_mock_discovery, sample_products, mock_razorpay
):
    mission_id = await _verified_mission(
        async_client, install_mock_discovery, sample_products, mock_razorpay
    )
    order_id = await _confirmed_order(async_client, mission_id)

    resp = await _report_issue(async_client, mission_id)
    data = resp.json()

    # "I received the wrong shampoo. I want a replacement." stores the PREFERENCE only
    assert data["issue_type"] == "wrong_item"
    assert data["requested_resolution"] == "replacement"
    assert data["status"] == "reported"  # not resolution_pending, not any 'approved' state
    assert "no refund, replacement, or merchant action has been initiated" in data[
        "resolution_note"
    ]

    # Agent context: deterministic response records the issue for review without
    # claiming any action
    assert data["agent_message"]
    assert "recorded" in data["agent_message"]
    assert "review the available resolution options" in data["agent_message"]
    assert "refund, replacement, or merchant action has been initiated" in data["agent_message"]


# --------------------------------------------------------------------------- #
# TEST 13: issue persists across a simulated backend restart
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_issue_persists_across_restart(
    restart_env, install_mock_discovery, sample_products, mock_razorpay
):
    make_client, test_session = restart_env

    async with make_client() as client_a:
        mission_id = await _verified_mission(
            client_a, install_mock_discovery, sample_products, mock_razorpay
        )
        order_id = await _confirmed_order(client_a, mission_id)
        created = (await _report_issue(client_a, mission_id)).json()

        # "Restart": brand-new client + fresh DB session against the same database
        async with make_client() as client_b:
            await _login(client_b, "olga@shop.com")
            body = (
                await client_b.get(f"/missions/{mission_id}/order/issues")
            ).json()
            assert len(body["issues"]) == 1
            issue = body["issues"][0]
            assert issue["issue_id"] == created["issue_id"]
            assert issue["order_id"] == order_id
            assert issue["status"] == "reported"
            # Exact-repeat after restart stays idempotent
            repeat = await _report_issue(client_b, mission_id)
            assert repeat.json()["issue_id"] == created["issue_id"]
            assert repeat.json()["already_existed"] is True

        async with test_session() as session:
            count = await session.scalar(
                select(func.count()).select_from(OrderIssueRecord)
            )
            assert count == 1
            row = (
                await session.execute(
                    select(OrderIssueRecord).where(OrderIssueRecord.mission_id == mission_id)
                )
            ).scalar_one()
            assert row.issue_id == created["issue_id"]
            assert row.order_id == order_id
            assert row.user_id.startswith("user_")


# --------------------------------------------------------------------------- #
# TEST 14: first issue moves the order into awaiting_post_purchase_context
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_order_enters_awaiting_post_purchase_context(
    async_client: AsyncClient, install_mock_discovery, sample_products, mock_razorpay
):
    mission_id = await _verified_mission(
        async_client, install_mock_discovery, sample_products, mock_razorpay
    )
    await _confirmed_order(async_client, mission_id)

    before = (await async_client.get(f"/missions/{mission_id}/order")).json()
    assert before["status"] == "order_confirmed"

    await _report_issue(async_client, mission_id)

    order = (await async_client.get(f"/missions/{mission_id}/order")).json()
    assert order["status"] == "awaiting_post_purchase_context"
    mission = (await async_client.get(f"/missions/{mission_id}")).json()
    assert mission["status"] == "awaiting_post_purchase_context"
    assert mission["shopping_intent"]["state"] == "awaiting_post_purchase_context"

    # A later, different issue keeps the order in the same lifecycle state
    await _report_issue(
        async_client,
        mission_id,
        issue_type="damaged",
        description="Bottle cracked on arrival.",
    )
    order = (await async_client.get(f"/missions/{mission_id}/order")).json()
    assert order["status"] == "awaiting_post_purchase_context"
