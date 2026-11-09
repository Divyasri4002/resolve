"""
Tests for Task 05: Shopping Product Selection.

Covers:
- TEST 1: Successful selection persists the canonical server-side ProductResult.
- TEST 2: Selection survives reload (GET returns selected_product).
- TEST 3: A product filtered out of discovery (not in discovered_products) is rejected.
- TEST 4: An arbitrary, never-discovered product_id is rejected and nothing is persisted.
- TEST 5: Another user cannot select a product on someone else's mission (404).
- TEST 6: Product selection is refused on non-shopping missions.
- TEST 7: Re-selecting the same product is idempotent; replacing a selection is deterministic.
- TEST 8: Selection never triggers checkout/payment state (no spend, no executing status).

Hermetic: REASONING_PROVIDER=mock and PRODUCT_SEARCH_PROVIDER=mock via tests/conftest.py —
no live Gemini or SerpApi calls.
"""
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base, get_db
from app.main import app

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

SHAMPOO_PROMPT = "I want to order shampoo under ₹600 for anti-hairfall."


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
        json={"name": name, "email": email, "password": "SecurePassword123!"},
    )
    assert reg.status_code == 201


async def _create_discovered_mission(client: AsyncClient) -> dict:
    resp = await client.post("/missions/shopping", json={"prompt": SHAMPOO_PROMPT})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "discovery_completed"
    assert len(data["shopping_intent"]["discovered_products"]) == 2  # ₹600 budget filter applied
    return data


@pytest.mark.asyncio
async def test_select_product_success_persists_canonical_result(
    async_client: AsyncClient, install_mock_discovery, sample_products
):
    """TEST 1: Selecting a discovered product persists the exact server-side ProductResult."""
    install_mock_discovery(sample_results=sample_products)
    await _register(async_client, "Selma Shopper", "selma@shop.com")
    mission = await _create_discovered_mission(async_client)
    mission_id = mission["mission_id"]

    resp = await async_client.post(
        f"/missions/{mission_id}/shopping/select-product",
        json={"product_id": "item_dove"},
    )
    assert resp.status_code == 200
    data = resp.json()

    assert data["mission_id"] == mission_id
    assert data["mission_status"] == "product_selected"
    assert data["total_recovery_spend"] == "0"

    selected = data["selected_product"]
    assert selected["product_id"] == "item_dove"
    assert selected["title"] == "Dove Hair Fall Rescue Shampoo"
    assert selected["price"] == "₹399.00"
    assert float(selected["extracted_price"]) == 399.0
    assert selected["currency"] == "INR"
    assert selected["merchant"] == "Nykaa"
    assert selected["product_url"] == "https://example.com/dove-shampoo"
    assert selected["image_url"] == "https://example.com/dove.jpg"
    assert selected["rating"] == 4.5
    assert selected["review_count"] == 3400
    assert selected["availability"] == "In stock"
    assert selected["source"] == "google_shopping"

    # Intent state advanced and the selection is visible in the persisted shopping state
    intent = data["shopping_intent"]
    assert intent["state"] == "product_selected"
    assert intent["selected_product"]["product_id"] == "item_dove"
    assert len(intent["discovered_products"]) == 2  # discovery results untouched

    # Agent acknowledgement uses the real title + merchant
    assert "Dove Hair Fall Rescue Shampoo" in data["agent_message"]
    assert "Nykaa" in data["agent_message"]


@pytest.mark.asyncio
async def test_selected_product_persists_across_reload(
    async_client: AsyncClient, install_mock_discovery, sample_products
):
    """TEST 2: Reopening the mission returns the same selected_product."""
    install_mock_discovery(sample_results=sample_products)
    await _register(async_client, "Rita Reload", "rita@shop.com")
    mission = await _create_discovered_mission(async_client)
    mission_id = mission["mission_id"]

    sel = await async_client.post(
        f"/missions/{mission_id}/shopping/select-product",
        json={"product_id": "item_loreal"},
    )
    assert sel.status_code == 200

    for _ in range(2):  # simulate refresh + reopen reads
        get_resp = await async_client.get(f"/missions/{mission_id}")
        assert get_resp.status_code == 200
        reopened = get_resp.json()
        assert reopened["status"] == "product_selected"
        selected = reopened["shopping_intent"]["selected_product"]
        assert selected["product_id"] == "item_loreal"
        assert selected["title"] == "L'Oreal Paris Total Repair 5 Shampoo, 650ml"
        assert selected["merchant"] == "Amazon.in"
        assert reopened["shopping_intent"]["state"] == "product_selected"
        assert len(reopened["shopping_intent"]["discovered_products"]) == 2


@pytest.mark.asyncio
async def test_select_product_not_in_discovered_results_rejected(
    async_client: AsyncClient, install_mock_discovery, sample_products
):
    """TEST 3: item_premium was filtered out by the ₹600 budget — it is NOT a discovered
    product of this mission and must be rejected."""
    install_mock_discovery(sample_results=sample_products)
    await _register(async_client, "Pam Picky", "pam@shop.com")
    mission = await _create_discovered_mission(async_client)
    mission_id = mission["mission_id"]
    discovered_ids = {p["product_id"] for p in mission["shopping_intent"]["discovered_products"]}
    assert "item_premium" not in discovered_ids

    resp = await async_client.post(
        f"/missions/{mission_id}/shopping/select-product",
        json={"product_id": "item_premium"},
    )
    assert resp.status_code == 400
    assert "discovered" in resp.json()["detail"].lower()

    get_resp = await async_client.get(f"/missions/{mission_id}")
    assert get_resp.json()["status"] == "discovery_completed"
    assert get_resp.json()["shopping_intent"]["selected_product"] is None


@pytest.mark.asyncio
async def test_arbitrary_client_product_rejected(
    async_client: AsyncClient, install_mock_discovery, sample_products
):
    """TEST 4: A fabricated product_id (injection attempt) is rejected; extra client-supplied
    product fields are ignored because the request schema only carries product_id."""
    install_mock_discovery(sample_results=sample_products)
    await _register(async_client, "Ivy Inject", "ivy@shop.com")
    mission = await _create_discovered_mission(async_client)
    mission_id = mission["mission_id"]

    resp = await async_client.post(
        f"/missions/{mission_id}/shopping/select-product",
        json={
            "product_id": "item_fake",
            "title": "Free iPhone",
            "price": "₹1",
            "merchant": "scam.example",
        },
    )
    assert resp.status_code == 400

    get_resp = await async_client.get(f"/missions/{mission_id}")
    intent = get_resp.json()["shopping_intent"]
    assert intent["selected_product"] is None
    assert get_resp.json()["status"] == "discovery_completed"


@pytest.mark.asyncio
async def test_unauthorized_user_cannot_select_product(
    async_client: AsyncClient, install_mock_discovery, sample_products
):
    """TEST 5: User B gets 404 (safe behavior) when selecting on User A's mission; the
    anonymous legacy caller is likewise blocked from an owned mission."""
    install_mock_discovery(sample_results=sample_products)
    await _register(async_client, "Owner Olga", "olga@shop.com")
    mission = await _create_discovered_mission(async_client)
    mission_id = mission["mission_id"]

    await _register(async_client, "Mallory", "mallory@shop.com")
    resp = await async_client.post(
        f"/missions/{mission_id}/shopping/select-product",
        json={"product_id": "item_dove"},
    )
    assert resp.status_code == 404

    # Owner's mission is untouched
    get_resp = await async_client.get(f"/missions/{mission_id}")
    assert get_resp.status_code == 404  # Mallory still can't even read it

    # New anonymous client (no cookie) also cannot touch an owned mission
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as anon_client:
        anon_resp = await anon_client.post(
            f"/missions/{mission_id}/shopping/select-product",
            json={"product_id": "item_dove"},
        )
        assert anon_resp.status_code == 404


@pytest.mark.asyncio
async def test_non_shopping_mission_rejects_product_selection(async_client: AsyncClient):
    """TEST 6: Recovery missions do not accept shopping product selection."""
    await _register(async_client, "Rex Recovery", "rex@shop.com")
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

    sel = await async_client.post(
        f"/missions/{mission_id}/shopping/select-product",
        json={"product_id": "item_dove"},
    )
    assert sel.status_code == 400
    assert "shopping" in sel.json()["detail"].lower()

    get_resp = await async_client.get(f"/missions/{mission_id}")
    assert get_resp.json()["status"] == "monitoring"


@pytest.mark.asyncio
async def test_reselection_is_deterministic(
    async_client: AsyncClient, install_mock_discovery, sample_products
):
    """TEST 7: Re-selecting the same product is idempotent (200, same canonical result);
    selecting a different discovered product replaces the selection deterministically."""
    install_mock_discovery(sample_results=sample_products)
    await _register(async_client, "Dan Deterministic", "dan@shop.com")
    mission = await _create_discovered_mission(async_client)
    mission_id = mission["mission_id"]

    first = await async_client.post(
        f"/missions/{mission_id}/shopping/select-product",
        json={"product_id": "item_dove"},
    )
    assert first.status_code == 200

    again = await async_client.post(
        f"/missions/{mission_id}/shopping/select-product",
        json={"product_id": "item_dove"},
    )
    assert again.status_code == 200
    assert again.json()["selected_product"] == first.json()["selected_product"]
    assert again.json()["mission_status"] == "product_selected"

    replaced = await async_client.post(
        f"/missions/{mission_id}/shopping/select-product",
        json={"product_id": "item_loreal"},
    )
    assert replaced.status_code == 200
    assert replaced.json()["selected_product"]["product_id"] == "item_loreal"
    assert replaced.json()["selected_product"]["title"].startswith("L'Oreal")

    get_resp = await async_client.get(f"/missions/{mission_id}")
    assert get_resp.json()["shopping_intent"]["selected_product"]["product_id"] == "item_loreal"


@pytest.mark.asyncio
async def test_selection_never_triggers_checkout(
    async_client: AsyncClient, install_mock_discovery, sample_products
):
    """TEST 8: After selection the mission is product_selected — not executing/negotiating,
    no recovery spend, no authority actions granted."""
    install_mock_discovery(sample_results=sample_products)
    await _register(async_client, "Cora Checkout", "cora@shop.com")
    mission = await _create_discovered_mission(async_client)
    mission_id = mission["mission_id"]

    sel = await async_client.post(
        f"/missions/{mission_id}/shopping/select-product",
        json={"product_id": "item_dove"},
    )
    assert sel.status_code == 200

    reopened = (await async_client.get(f"/missions/{mission_id}")).json()
    assert reopened["status"] == "product_selected"
    assert reopened["status"] not in ("executing", "negotiating", "awaiting_human_approval")
    assert reopened["total_recovery_spend"] == "0"
    assert reopened["recovery_budget"] == "0"
    assert reopened["allowed_actions"] == []


@pytest.mark.asyncio
async def test_empty_product_id_rejected(async_client: AsyncClient):
    """Guard: the request schema requires a non-empty product_id."""
    await _register(async_client, "Ed Empty", "ed@shop.com")
    resp = await async_client.post(
        "/missions/whatever/shopping/select-product",
        json={"product_id": ""},
    )
    assert resp.status_code == 422
