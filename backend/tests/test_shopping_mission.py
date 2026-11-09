"""
Tests for Task 03: Shopping Mission / Purchase Intent.

Covers:
- TEST 1: User creates a shopping mission from natural language with complete intent ("shampoo under ₹600 for anti-hairfall").
- TEST 2: Structured shopping intent contains category, budget (600 INR), preferences (anti-hairfall), and state=ready_for_discovery.
- TEST 3: User creates a shopping mission missing budget -> state=needs_clarification, missing_info contains budget.
- TEST 4: Agent interaction clarifies missing budget -> updates intent and transitions status to ready_for_discovery.
- TEST 5: Shopping missions have recovery_budget=0 and total_recovery_spend=0 (no spend authorized, no payment).
- TEST 6: Multi-user isolation for shopping missions (User B cannot access User A's shopping mission).
- TEST 7: Legacy recovery missions continue to work unchanged with existing structure.
"""
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base, get_db
from app.main import app

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


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


@pytest.mark.asyncio
async def test_create_shopping_mission_complete_intent(
    async_client: AsyncClient, install_mock_discovery, sample_products
):
    """Test 1 & 2: Natural language intent parsing with complete parameters.

    With Task 04B the creation route runs product discovery immediately, so a complete
    intent transitions all the way to discovery_completed with real normalized products.
    """
    install_mock_discovery(sample_results=sample_products)
    # Register user
    reg = await async_client.post(
        "/auth/register",
        json={"name": "Alice Shopper", "email": "alice@shop.com", "password": "SecurePassword123!"},
    )
    assert reg.status_code == 201

    # Create shopping mission
    resp = await async_client.post(
        "/missions/shopping",
        json={"prompt": "I want to order shampoo under ₹600 for anti-hairfall."},
    )
    assert resp.status_code == 200
    data = resp.json()

    assert data["mission_type"] == "shopping"
    assert data["status"] == "discovery_completed"
    assert data["recovery_budget"] == "0"
    assert data["total_recovery_spend"] == "0"

    intent = data["shopping_intent"]
    assert intent is not None
    assert intent["category"] == "shampoo"
    assert float(intent["budget"]) == 600.0
    assert intent["currency"] == "INR"
    assert "anti-hairfall" in intent["preferences"]
    assert intent["state"] == "discovery_completed"
    assert intent["missing_info"] == []

    # Task 04B: discovery ran during creation; budget-filtered products are persisted
    products = intent["discovered_products"]
    assert len(products) == 2  # the ₹1,200 sample is excluded by the ₹600 budget
    assert all(float(p["extracted_price"]) <= 600.0 for p in products)
    assert {p["merchant"] for p in products} == {"Amazon.in", "Nykaa"}
    assert "L'Oreal Paris Total Repair" in data["resolution_summary"]


@pytest.mark.asyncio
async def test_create_shopping_mission_needs_clarification(async_client: AsyncClient):
    """Test 3: Natural language intent missing budget sets needs_clarification."""
    reg = await async_client.post(
        "/auth/register",
        json={"name": "Bob Shopper", "email": "bob@shop.com", "password": "SecurePassword123!"},
    )
    assert reg.status_code == 201

    resp = await async_client.post(
        "/missions/shopping",
        json={"prompt": "I want to buy running shoes from Nike"},
    )
    assert resp.status_code == 200
    data = resp.json()

    assert data["mission_type"] == "shopping"
    assert data["status"] == "needs_clarification"

    intent = data["shopping_intent"]
    assert intent is not None
    assert intent["category"] == "running shoes" or intent["category"] == "shoes"
    assert intent["brand"] == "Nike"
    assert intent["budget"] is None
    assert "budget" in intent["missing_info"]
    assert intent["clarification_prompt"] is not None
    assert intent["state"] == "needs_clarification"


@pytest.mark.asyncio
async def test_clarify_shopping_intent_via_agent(
    async_client: AsyncClient, install_mock_discovery, sample_products
):
    """Test 4: Agent interaction clarifies missing budget and transitions state.

    With Task 04B the clarification triggers discovery through the tool, so the mission
    ends in discovery_completed with products returned to the shopping UI.
    """
    install_mock_discovery(sample_results=sample_products)
    reg = await async_client.post(
        "/auth/register",
        json={"name": "Charlie", "email": "charlie@shop.com", "password": "SecurePassword123!"},
    )
    assert reg.status_code == 201

    # Create initial intent without budget
    resp = await async_client.post(
        "/missions/shopping",
        json={"prompt": "I want to buy headphones with noise cancelling"},
    )
    assert resp.status_code == 200
    data = resp.json()
    mission_id = data["mission_id"]
    assert data["status"] == "needs_clarification"

    # User replies with budget
    agent_resp = await async_client.post(
        f"/missions/{mission_id}/agent",
        json={"user_input": "My budget is under ₹2500 from Sony"},
    )
    assert agent_resp.status_code == 200
    agent_data = agent_resp.json()

    assert agent_data["mission_status"] == "discovery_completed"
    intent = agent_data["shopping_intent"]
    assert float(intent["budget"]) == 2500.0
    assert intent["brand"] == "Sony"
    assert intent["state"] == "discovery_completed"
    assert len(agent_data["discovered_products"]) == 3  # all samples within ₹2,500

    # Verify mission record persisted
    get_resp = await async_client.get(f"/missions/{mission_id}")
    assert get_resp.status_code == 200
    persisted = get_resp.json()
    assert persisted["status"] == "discovery_completed"
    assert float(persisted["shopping_intent"]["budget"]) == 2500.0
    assert len(persisted["shopping_intent"]["discovered_products"]) == 3


@pytest.mark.asyncio
async def test_shopping_mission_cross_user_isolation(async_client: AsyncClient):
    """Test 6: User B cannot access or modify User A's shopping mission."""
    # Register Alice
    reg_a = await async_client.post(
        "/auth/register",
        json={"name": "Alice User", "email": "alice.iso@shop.com", "password": "Password123!"},
    )
    assert reg_a.status_code == 201

    # Alice creates shopping mission
    resp = await async_client.post(
        "/missions/shopping",
        json={"prompt": "Order organic sunscreen under ₹800"},
    )
    assert resp.status_code == 200
    alice_mission_id = resp.json()["mission_id"]

    # Register Bob (session switches to Bob)
    reg_b = await async_client.post(
        "/auth/register",
        json={"name": "Bob User", "email": "bob.iso@shop.com", "password": "Password123!"},
    )
    assert reg_b.status_code == 201

    # Bob attempts to get Alice's mission -> 404
    get_b = await async_client.get(f"/missions/{alice_mission_id}")
    assert get_b.status_code == 404

    # Bob attempts to send message to Alice's mission -> 404
    agent_b = await async_client.post(
        f"/missions/{alice_mission_id}/agent",
        json={"user_input": "Change budget to 100"},
    )
    assert agent_b.status_code == 404


@pytest.mark.asyncio
async def test_recovery_mission_compatibility_unbroken(async_client: AsyncClient):
    """Test 7: Standard recovery mission creation and fields remain intact."""
    reg = await async_client.post(
        "/auth/register",
        json={"name": "Recovery User", "email": "rec@shop.com", "password": "Password123!"},
    )
    assert reg.status_code == 201

    resp = await async_client.post(
        "/missions",
        json={
            "mission_type": "recovery",
            "objective": {
                "description": "Recover damaged package for camera",
                "success_criteria": ["working lens"],
            },
            "authority": {
                "allowed_actions": ["request_refund", "request_replacement"],
                "recovery_budget": "5000",
            },
            "transaction": {
                "merchant_name": "Electronics Store",
                "order_id": "ord_999",
                "amount_paid": "12000",
            },
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["mission_type"] == "recovery"
    assert data["status"] == "monitoring"
    assert data["recovery_budget"] == "5000"
    assert data["shopping_intent"] is None

