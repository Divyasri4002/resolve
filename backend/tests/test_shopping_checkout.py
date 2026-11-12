"""
Tests for Task 06: Checkout Review / Payment Boundary.

Covers:
- TEST 1:  Checkout review is derived from the persisted selected_product (server-authoritative).
- TEST 2:  Opening the review advances product_selected -> checkout_review, idempotently.
- TEST 3:  Checkout review without a selected product is rejected.
- TEST 4:  Another user (or an anonymous caller) cannot access checkout (404).
- TEST 5:  Checkout is refused on non-shopping (recovery) missions.
- TEST 6:  Client-supplied price/subtotal manipulation is ignored; canonical values returned.
- TEST 7:  Client-supplied product identity manipulation is ignored.
- TEST 8:  Confirm transitions checkout_review -> checkout_confirmed with derived confirmation.
- TEST 9:  Re-confirming is idempotent (same confirmation record, still 200).
- TEST 10: Confirmation persists across reloads.
- TEST 11: Confirmation survives a simulated backend restart (fresh client + re-login).
- TEST 12: NO Razorpay API is called during review or confirm (tripwire on the client factory).
- TEST 13: NO order is created (no order/payment identifiers, transaction still pending).
- TEST 14: The product selection remains intact after confirmation.

Hermetic: REASONING_PROVIDER=mock and PRODUCT_SEARCH_PROVIDER=mock via tests/conftest.py —
no live Gemini, SerpApi, or Razorpay calls.
"""
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base, get_db
from app.main import app

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


async def _register(client: AsyncClient, name: str, email: str) -> None:
    reg = await client.post(
        "/auth/register",
        json={"name": name, "email": email, "password": PASSWORD},
    )
    assert reg.status_code == 201


async def _create_selected_mission(client: AsyncClient) -> str:
    """Full path to product_selected: register-free helper expects caller registered;
    returns mission_id with item_dove selected (₹399, Nykaa)."""
    resp = await client.post("/missions/shopping", json={"prompt": SHAMPOO_PROMPT})
    assert resp.status_code == 200
    mission_id = resp.json()["mission_id"]

    sel = await client.post(
        f"/missions/{mission_id}/shopping/select-product",
        json={"product_id": "item_dove"},
    )
    assert sel.status_code == 200
    return mission_id


async def _confirm(client: AsyncClient, mission_id: str, payload: dict | None = None):
    return await client.post(
        f"/missions/{mission_id}/shopping/checkout/confirm",
        json=payload if payload is not None else {},
    )


# --------------------------------------------------------------------------- #
# TEST 1 + 2: review derivation + status transition
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_checkout_review_derived_from_selected_product(
    async_client: AsyncClient, install_mock_discovery, sample_products
):
    """TEST 1/2: GET checkout returns server-derived review values (title, merchant,
    price, subtotal, qty=1, currency, payment provider) and advances the mission to
    checkout_review. A repeated GET is an idempotent no-op."""
    install_mock_discovery(sample_results=sample_products)
    await _register(async_client, "Cody Checkout", "cody@shop.com")
    mission_id = await _create_selected_mission(async_client)

    resp = await async_client.get(f"/missions/{mission_id}/shopping/checkout")
    assert resp.status_code == 200
    data = resp.json()

    assert data["mission_status"] == "checkout_review"
    assert data["confirmed"] is False
    assert data["total_recovery_spend"] == "0"

    checkout = data["checkout"]
    product = checkout["product"]
    assert product["product_id"] == "item_dove"
    assert product["title"] == "Dove Hair Fall Rescue Shampoo"
    assert product["merchant"] == "Nykaa"
    assert product["extracted_price"] == "399.0"
    assert checkout["quantity"] == 1
    assert checkout["subtotal"] == "399.0"
    assert checkout["currency"] == "INR"
    assert checkout["payment_provider"] == "razorpay_test_mode"

    # Intent state advanced and persisted
    intent = data["shopping_intent"]
    assert intent["state"] == "checkout_review"
    assert intent["selected_product"]["product_id"] == "item_dove"
    assert intent["checkout"] is None  # not confirmed yet

    # Idempotent reopen: status stays checkout_review, no duplicate side effects
    again = await async_client.get(f"/missions/{mission_id}/shopping/checkout")
    assert again.status_code == 200
    assert again.json()["mission_status"] == "checkout_review"
    assert again.json()["shopping_intent"]["checkout"] is None


# --------------------------------------------------------------------------- #
# TEST 3: review requires a selection
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_checkout_review_without_selected_product_rejected(
    async_client: AsyncClient, install_mock_discovery, sample_products
):
    """TEST 3: A discovery_completed mission (nothing selected) cannot enter checkout."""
    install_mock_discovery(sample_results=sample_products)
    await _register(async_client, "Noel NoSelect", "noel@shop.com")

    mission_id = await client_post_mission(async_client)

    review = await async_client.get(f"/missions/{mission_id}/shopping/checkout")
    assert review.status_code == 400
    assert "select" in review.json()["detail"].lower()

    confirm = await _confirm(async_client, mission_id)
    assert confirm.status_code == 400


async def client_post_mission(client: AsyncClient) -> str:
    resp = await client.post("/missions/shopping", json={"prompt": SHAMPOO_PROMPT})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "discovery_completed"
    return data["mission_id"]


# --------------------------------------------------------------------------- #
# TEST 4: unauthorized access
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_unauthorized_user_cannot_access_checkout(
    async_client: AsyncClient, install_mock_discovery, sample_products
):
    """TEST 4: User B gets 404 on review AND confirm; an anonymous caller is likewise
    blocked from an owned mission."""
    install_mock_discovery(sample_results=sample_products)
    await _register(async_client, "Owner Orla", "orla@shop.com")
    mission_id = await _create_selected_mission(async_client)

    await _register(async_client, "Mallory", "mallory2@shop.com")
    review = await async_client.get(f"/missions/{mission_id}/shopping/checkout")
    assert review.status_code == 404
    confirm = await _confirm(async_client, mission_id)
    assert confirm.status_code == 404

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as anon_client:
        anon_review = await anon_client.get(f"/missions/{mission_id}/shopping/checkout")
        assert anon_review.status_code == 404
        anon_confirm = await _confirm(anon_client, mission_id)
        assert anon_confirm.status_code == 404

    # Owner's mission is still only product_selected — no state was touched.
    # (The shared client's cookie now belongs to Mallory, so re-login as Orla.)
    login = await async_client.post(
        "/auth/login",
        json={"email": "orla@shop.com", "password": PASSWORD},
    )
    assert login.status_code == 200
    owner_view = (await async_client.get(f"/missions/{mission_id}")).json()
    assert owner_view["status"] == "product_selected"


# --------------------------------------------------------------------------- #
# TEST 5: non-shopping mission rejected
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_non_shopping_mission_rejects_checkout(async_client: AsyncClient):
    """TEST 5: Recovery missions do not have a checkout boundary."""
    await _register(async_client, "Rex Recovery", "rex2@shop.com")
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

    review = await async_client.get(f"/missions/{mission_id}/shopping/checkout")
    assert review.status_code == 400
    assert "shopping" in review.json()["detail"].lower()

    confirm = await _confirm(async_client, mission_id)
    assert confirm.status_code == 400


# --------------------------------------------------------------------------- #
# TEST 6 + 7: client manipulation ignored
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_client_cannot_manipulate_price(
    async_client: AsyncClient, install_mock_discovery, sample_products
):
    """TEST 6: A confirm payload carrying attacker-chosen price/subtotal is accepted
    (extra fields ignored) but the returned checkout keeps the canonical ₹399 values."""
    install_mock_discovery(sample_results=sample_products)
    await _register(async_client, "Pia Pricehack", "pia@shop.com")
    mission_id = await _create_selected_mission(async_client)
    await async_client.get(f"/missions/{mission_id}/shopping/checkout")

    resp = await _confirm(
        async_client,
        mission_id,
        payload={
            "price": "₹1",
            "extracted_price": 1,
            "subtotal": "1",
            "currency": "USD",
            "quantity": 99,
        },
    )
    assert resp.status_code == 200
    checkout = resp.json()["checkout"]
    assert checkout["product"]["extracted_price"] == "399.0"
    assert checkout["subtotal"] == "399.0"
    assert checkout["currency"] == "INR"
    assert checkout["quantity"] == 1
    assert resp.json()["confirmation"]["subtotal"] == "399.0"
    assert resp.json()["confirmation"]["quantity"] == 1


@pytest.mark.asyncio
async def test_client_cannot_manipulate_product(
    async_client: AsyncClient, install_mock_discovery, sample_products
):
    """TEST 7: A confirm payload carrying a fabricated product identity is ignored;
    the confirmed checkout still refers to the persisted selection."""
    install_mock_discovery(sample_results=sample_products)
    await _register(async_client, "Ida Inject", "ida@shop.com")
    mission_id = await _create_selected_mission(async_client)

    resp = await _confirm(
        async_client,
        mission_id,
        payload={
            "product_id": "item_fake",
            "title": "Free iPhone 16 Pro",
            "merchant": "scam.example",
            "product_url": "https://scam.example/iphone",
        },
    )
    assert resp.status_code == 200
    product = resp.json()["checkout"]["product"]
    assert product["product_id"] == "item_dove"
    assert product["title"] == "Dove Hair Fall Rescue Shampoo"
    assert product["merchant"] == "Nykaa"
    assert product["product_url"] == "https://example.com/dove-shampoo"


# --------------------------------------------------------------------------- #
# TEST 8 + 9: confirm transition + idempotency
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_confirm_checkout_changes_state(
    async_client: AsyncClient, install_mock_discovery, sample_products
):
    """TEST 8: Confirm persists checkout_confirmed with server-derived details."""
    install_mock_discovery(sample_results=sample_products)
    await _register(async_client, "Fern Confirm", "fern@shop.com")
    mission_id = await _create_selected_mission(async_client)
    await async_client.get(f"/missions/{mission_id}/shopping/checkout")

    resp = await _confirm(async_client, mission_id)
    assert resp.status_code == 200
    data = resp.json()

    assert data["mission_status"] == "checkout_confirmed"
    assert data["total_recovery_spend"] == "0"

    confirmation = data["confirmation"]
    assert confirmation["quantity"] == 1
    assert confirmation["subtotal"] == "399.0"
    assert confirmation["currency"] == "INR"
    assert confirmation["payment_provider"] == "razorpay_test_mode"
    assert confirmation["confirmed_at"]

    intent = data["shopping_intent"]
    assert intent["state"] == "checkout_confirmed"
    assert intent["selected_product"]["product_id"] == "item_dove"

    # The agent message marks the boundary honestly: ready for payment, none made
    assert "Checkout confirmed" in data["agent_message"]
    assert "Dove Hair Fall Rescue Shampoo" in data["agent_message"]
    assert "No payment has been made" in data["agent_message"]
    # The message describes the REAL next step — the Razorpay TEST checkout — never a
    # "future task"; and it still states that no order exists yet.
    assert "future task" not in data["agent_message"]
    assert "Razorpay TEST checkout opens next" in data["agent_message"]
    assert "no order has been created" in data["agent_message"]


@pytest.mark.asyncio
async def test_confirm_checkout_is_idempotent(
    async_client: AsyncClient, install_mock_discovery, sample_products
):
    """TEST 9: Re-confirming returns the same persisted confirmation record."""
    install_mock_discovery(sample_results=sample_products)
    await _register(async_client, "Ike Idem", "ike@shop.com")
    mission_id = await _create_selected_mission(async_client)

    first = await _confirm(async_client, mission_id)
    assert first.status_code == 200
    second = await _confirm(async_client, mission_id)
    assert second.status_code == 200
    assert second.json()["confirmation"] == first.json()["confirmation"]
    assert second.json()["mission_status"] == "checkout_confirmed"


# --------------------------------------------------------------------------- #
# TEST 10 + 11: persistence (reload + simulated restart)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_confirmation_persists_across_reload(
    async_client: AsyncClient, install_mock_discovery, sample_products
):
    """TEST 10: Reopening the mission returns the persisted checkout confirmation."""
    install_mock_discovery(sample_results=sample_products)
    await _register(async_client, "Lena Reload", "lena@shop.com")
    mission_id = await _create_selected_mission(async_client)
    await _confirm(async_client, mission_id)

    for _ in range(2):  # simulate refresh + reopen reads
        get_resp = await async_client.get(f"/missions/{mission_id}")
        assert get_resp.status_code == 200
        reopened = get_resp.json()
        assert reopened["status"] == "checkout_confirmed"
        assert reopened["shopping_intent"]["state"] == "checkout_confirmed"
        checkout = reopened["shopping_intent"]["checkout"]
        assert checkout["quantity"] == 1
        assert checkout["subtotal"] == "399.0"

    # The review endpoint still serves the summary, now flagged confirmed
    review = await async_client.get(f"/missions/{mission_id}/shopping/checkout")
    assert review.status_code == 200
    assert review.json()["confirmed"] is True
    assert review.json()["checkout"]["product"]["product_id"] == "item_dove"


@pytest.mark.asyncio
async def test_checkout_state_survives_restart(
    async_client: AsyncClient, install_mock_discovery, sample_products
):
    """TEST 11: A brand-new client (fresh session cookie via re-login) still sees the
    confirmed checkout — the state lives in the database, not in any connection."""
    install_mock_discovery(sample_results=sample_products)
    await _register(async_client, "Stella Survive", "stella@shop.com")
    mission_id = await _create_selected_mission(async_client)
    await _confirm(async_client, mission_id)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as restarted_client:
        login = await restarted_client.post(
            "/auth/login",
            json={"email": "stella@shop.com", "password": PASSWORD},
        )
        assert login.status_code == 200

        get_resp = await restarted_client.get(f"/missions/{mission_id}")
        assert get_resp.status_code == 200
        reopened = get_resp.json()
        assert reopened["status"] == "checkout_confirmed"
        assert reopened["shopping_intent"]["checkout"]["subtotal"] == "399.0"


# --------------------------------------------------------------------------- #
# TEST 12: no Razorpay call
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_no_razorpay_api_called(
    async_client: AsyncClient, install_mock_discovery, sample_products, monkeypatch
):
    """TEST 12: Review + confirm must never touch the Razorpay client. The client factory
    in app.payments.razorpay_client is replaced with a tripwire that fails the test if
    anything instantiates it."""
    from app.payments import razorpay_client

    calls: list[str] = []

    def _exploding_client(*args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append("client")
        raise AssertionError("Razorpay client instantiated during checkout boundary!")

    monkeypatch.setattr(razorpay_client, "_client", _exploding_client)

    install_mock_discovery(sample_results=sample_products)
    await _register(async_client, "Rae Razorpay", "rae@shop.com")
    mission_id = await _create_selected_mission(async_client)

    review = await async_client.get(f"/missions/{mission_id}/shopping/checkout")
    assert review.status_code == 200
    confirm = await _confirm(async_client, mission_id)
    assert confirm.status_code == 200

    assert calls == []  # the tripwire never fired


# --------------------------------------------------------------------------- #
# TEST 13: no order created
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_no_order_created(
    async_client: AsyncClient, install_mock_discovery, sample_products
):
    """TEST 13: Confirming checkout creates no order and no payment identifiers; the
    mission's transaction is still the pending discovery placeholder and spend is zero."""
    install_mock_discovery(sample_results=sample_products)
    await _register(async_client, "Omar Orderless", "omar@shop.com")
    mission_id = await _create_selected_mission(async_client)

    resp = await _confirm(async_client, mission_id)
    assert resp.status_code == 200
    body = resp.json()

    # No order/payment identifiers anywhere in the response
    for key in ("order_id", "payment_id", "razorpay_order_id", "razorpay_payment_id"):
        assert key not in body
        assert key not in body["checkout"]

    # Mission-level invariants: nothing was bought
    reopened = (await async_client.get(f"/missions/{mission_id}")).json()
    assert reopened["total_recovery_spend"] == "0"
    assert reopened["allowed_actions"] == []
    assert reopened["status"] not in ("executing", "negotiating", "awaiting_human_approval")


# --------------------------------------------------------------------------- #
# TEST 14: selection intact after confirmation
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_selection_remains_intact_after_confirmation(
    async_client: AsyncClient, install_mock_discovery, sample_products
):
    """TEST 14: Confirming checkout neither clears nor alters the product selection or
    the discovery results."""
    install_mock_discovery(sample_results=sample_products)
    await _register(async_client, "Tara Intact", "tara@shop.com")
    mission_id = await _create_selected_mission(async_client)
    await _confirm(async_client, mission_id)

    reopened = (await async_client.get(f"/missions/{mission_id}")).json()
    intent = reopened["shopping_intent"]
    assert intent["selected_product"]["product_id"] == "item_dove"
    assert intent["selected_product"]["merchant"] == "Nykaa"
    assert len(intent["discovered_products"]) == 2
