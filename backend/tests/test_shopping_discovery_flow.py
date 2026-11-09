"""
Tests for Task 04B: Gemini -> Product Discovery Tool -> SerpApi provider -> grounded
response -> shopping UI.

The production stack is wired to REAL Gemini + REAL SerpApi; these tests run the exact
same router/provider code paths with hermetic mocks and assert the 04B contract:

1. Mission creation executes product discovery through the Discovery Tool and persists
   normalized ProductResults into the receipt.
2. Budget filtering is deterministic code that runs BEFORE any reasoning model sees the
   product list (an LLM can never surface an over-budget product).
3. The reasoning summary is GROUNDED: it references only real discovered product fields
   (title / price / merchant), never invented data.
4. Gemini's summarize payload carries the exact, verbatim product payload and is fully
   isolated from search-provider credentials (SerpApi keys never reach the LLM layer).
5. The real-time agent endpoint returns discovered_products so the shopping UI renders
   real product cards.
6. Provider outages degrade gracefully to ready_for_discovery (mission never crashes).
"""
import json

import pytest
import pytest_asyncio
from decimal import Decimal
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.agents.reasoning_provider import GeminiReasoningProvider
from app.database import Base, get_db
from app.main import app
from app.tools.product_search.base import ProductSearchError

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture
async def async_client() -> AsyncClient:
    """
    FastAPI test client over an in-memory SQLite DB.

    Uses @pytest_asyncio.fixture explicitly so the fixture works under
    pytest-asyncio 0.24 strict mode (plain @pytest.fixture on async generators
    is rejected there).
    """
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


async def _register_user(client: AsyncClient, email: str) -> None:
    resp = await client.post(
        "/auth/register",
        json={"name": "Discovery Tester", "email": email, "password": "SecurePassword123!"},
    )
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_mission_creation_runs_discovery_and_persists_products(
    async_client: AsyncClient, install_mock_discovery, sample_products
):
    """
    Full 04B chain through the API: prompt -> intent -> decision -> discovery tool ->
    normalized products -> grounded summary -> persisted receipt with discovered_products.
    """
    install_mock_discovery(sample_results=sample_products)
    await _register_user(async_client, "alice.disco@shop.com")

    resp = await async_client.post(
        "/missions/shopping",
        json={"prompt": "I want to order shampoo under ₹600 for anti-hairfall."},
    )
    assert resp.status_code == 200
    data = resp.json()

    # Discovery executed at creation time
    assert data["status"] == "discovery_completed"
    assert data["mission_type"] == "shopping"
    assert data["recovery_budget"] == "0"

    intent = data["shopping_intent"]
    assert intent["category"] == "shampoo"
    assert float(intent["budget"]) == 600.0
    assert "anti-hairfall" in intent["preferences"]
    assert intent["state"] == "discovery_completed"

    # Deterministic budget filter removed the ₹1,200 item before anything was grounded
    products = intent["discovered_products"]
    assert len(products) == 2
    assert all(float(p["extracted_price"]) <= 600.0 for p in products)
    assert {p["merchant"] for p in products} == {"Amazon.in", "Nykaa"}
    for p in products:
        assert p["source"] == "google_shopping"
        assert p["title"] and p["price"] and p["merchant"]
        # Product payload contains only provider data — never any credentials
        assert "api_key" not in json.dumps(p).lower()

    # Grounded response references ONLY real products that survived filtering
    summary = data["resolution_summary"]
    assert summary and summary.strip()
    assert "shampoo" in summary.lower()
    for p in products:
        assert p["title"] in summary
        assert p["price"] in summary
        assert p["merchant"] in summary
    over_budget_title = "Premium Organic Salon Care Shampoo"
    assert over_budget_title not in summary  # over-budget item never leaked to the summary

    # Receipt served back from the DB still carries the discovered products
    get_resp = await async_client.get(f"/missions/{data['mission_id']}")
    assert get_resp.status_code == 200
    persisted = get_resp.json()
    assert persisted["status"] == "discovery_completed"
    assert len(persisted["shopping_intent"]["discovered_products"]) == 2


@pytest.mark.asyncio
async def test_agent_endpoint_returns_discovered_products_for_ui(
    async_client: AsyncClient, install_mock_discovery, sample_products
):
    """
    Real-time agent endpoint: a user refinement (budget) triggers discovery and the
    response returns discovered_products + grounded agent_message that the UI renders.
    """
    install_mock_discovery(sample_results=sample_products)
    await _register_user(async_client, "bob.disco@shop.com")

    # Start with an incomplete intent (no budget) -> needs_clarification
    resp = await async_client.post(
        "/missions/shopping",
        json={"prompt": "I want to buy a good camera"},
    )
    assert resp.status_code == 200
    mission_id = resp.json()["mission_id"]
    assert resp.json()["status"] == "needs_clarification"

    # User provides the missing constraint through the live agent endpoint
    agent_resp = await async_client.post(
        f"/missions/{mission_id}/agent",
        json={"user_input": "My budget is under ₹20000"},
    )
    assert agent_resp.status_code == 200
    agent_data = agent_resp.json()

    assert agent_data["mission_status"] == "discovery_completed"
    assert agent_data["agent_proposal"]["action"] == "product_discovery"
    # Task CORE-2, Phase 11. This used to assert `authority_decision["approved"] is True`, which the
    # endpoint produced without ever calling the Authority Engine — a decision it had not made. A
    # product search delegates nothing, spends nothing and contacts no merchant, so the honest answer
    # is now no answer: the key is still there, and it says nothing was evaluated.
    assert "authority_decision" in agent_data
    assert agent_data["authority_decision"] is None

    # What the shopping UI needs to render real product cards
    products = agent_data["discovered_products"]
    assert len(products) == 3  # all samples <= ₹20,000 budget
    assert {p["merchant"] for p in products} == {"Amazon.in", "Nykaa", "Flipkart"}

    agent_message = agent_data["agent_message"]
    assert agent_message and len(agent_message) > 0
    assert any(p["title"] in agent_message for p in products)

    # The mission record was updated in place with the products
    get_resp = await async_client.get(f"/missions/{mission_id}")
    assert get_resp.status_code == 200
    persisted = get_resp.json()
    assert persisted["status"] == "discovery_completed"
    assert len(persisted["shopping_intent"]["discovered_products"]) == 3


@pytest.mark.asyncio
async def test_search_failure_degrades_gracefully(
    async_client: AsyncClient, install_mock_discovery, sample_products
):
    """Provider/network outage: mission stays usable (ready_for_discovery), never 500s."""
    install_mock_discovery(
        sample_results=sample_products,
        error=ProductSearchError("SerpApi is unavailable", status_code=503),
    )
    await _register_user(async_client, "carol.disco@shop.com")

    resp = await async_client.post(
        "/missions/shopping",
        json={"prompt": "I want to order shampoo under ₹600 for anti-hairfall."},
    )
    assert resp.status_code == 200  # graceful, not a 5xx
    data = resp.json()

    assert data["status"] == "ready_for_discovery"
    assert data["shopping_intent"]["state"] == "ready_for_discovery"
    assert data["shopping_intent"]["discovered_products"] == []
    assert "serpapi" in data["resolution_summary"].lower()
    assert "unavailable" in data["resolution_summary"].lower()


@pytest.mark.asyncio
async def test_gemini_summarize_payload_is_grounded_and_credential_free(monkeypatch):
    """
    Gemini grounding contract:
    - the whole 'products' payload is passed to the model VERBATIM (nothing invented),
    - the model is told to ground strictly on that payload,
    - no search-provider API key / auth material is ever in the LLM request.
    """
    provider = GeminiReasoningProvider(api_key="test-gemini-key")
    captured: dict = {}

    async def fake_call_model(system: str, user: str, json_mode: bool = True) -> str:
        captured["system"] = system
        captured["user"] = user
        captured["json_mode"] = json_mode
        return "Here are your top 2 matches."

    monkeypatch.setattr(provider, "_call_model", fake_call_model)

    products = [
        {
            "title": "Dove Hair Fall Rescue Shampoo",
            "price": "₹399.00",
            "extracted_price": "399.0",
            "currency": "INR",
            "merchant": "Nykaa",
            "source": "google_shopping",
            "extensions": ["Sale"],
            "availability": "In stock",
        },
        {
            "title": "L'Oreal Paris Total Repair 5 Shampoo, 650ml",
            "price": "₹549.00",
            "extracted_price": "549.0",
            "currency": "INR",
            "merchant": "Amazon.in",
            "source": "google_shopping",
            "extensions": [],
        },
    ]

    result = await provider.summarize_product_results("shampoo", Decimal("600"), products)

    assert result == "Here are your top 2 matches."
    assert captured["json_mode"] is False  # conversational, not strict-JSON

    # Grounding rules are explicit in the system prompt
    system = captured["system"]
    assert "Ground your response ONLY in the provided products data" in system
    assert "NEVER invent prices" in system

    # The user payload carries the exact product data + budget, byte-for-byte
    payload = json.loads(captured["user"])
    assert payload["user_query"] == "shampoo"
    assert payload["user_budget"] == "600"
    assert payload["products"] == products

    # Credential isolation: the Gemini key and any SerpApi material never reach the model
    raw = captured["user"] + captured["system"]
    assert "test-gemini-key" not in raw
    assert "SERPAPI_API_KEY" not in raw
    assert "serpapi" not in raw.lower()


def test_gemini_provider_requires_key():
    """Regression guard: the Gemini provider must never be instantiated empty-keyed."""
    with pytest.raises(ValueError):
        GeminiReasoningProvider(api_key="")


class _FailingReasoning:
    """Simulates a reasoning provider (Gemini/Groq) that is down or rate-limited."""

    async def interpret_shopping_intent(self, prompt: str) -> dict:
        raise RuntimeError("simulated provider outage")

    async def decide_shopping_action(self, context: dict) -> dict:
        raise RuntimeError("simulated provider outage")


@pytest.mark.asyncio
async def test_reasoning_outage_create_degrades_gracefully(
    async_client: AsyncClient, monkeypatch
):
    """
    LLM outage at CREATION time: the request must never 500 — the mission is parked in
    needs_clarification with the user's prompt persisted for a later retry.
    """
    from app.routers import missions as missions_module

    monkeypatch.setattr(
        missions_module, "get_reasoning_provider", lambda *a, **k: _FailingReasoning()
    )
    await _register_user(async_client, "dana.outage@shop.com")

    resp = await async_client.post(
        "/missions/shopping",
        json={"prompt": "I want to order shampoo under ₹600 for anti-hairfall."},
    )
    assert resp.status_code == 200  # graceful, not a 5xx
    data = resp.json()

    assert data["status"] == "needs_clarification"
    intent = data["shopping_intent"]
    assert intent["state"] == "needs_clarification"
    assert intent["discovered_products"] == []
    # The original request is preserved so the user can retry without re-typing
    assert intent["raw_prompt"] == "I want to order shampoo under ₹600 for anti-hairfall."
    assert "reasoning service" in data["resolution_summary"].lower()
    assert data["mission_id"]


@pytest.mark.asyncio
async def test_reasoning_outage_agent_degrades_gracefully(
    async_client: AsyncClient, install_mock_discovery, sample_products, monkeypatch
):
    """
    LLM outage on the real-time agent endpoint: existing mission state is preserved,
    the response is a friendly retryable message (never a 500), no products returned.
    """
    from app.routers import missions as missions_module

    install_mock_discovery(sample_results=sample_products)
    await _register_user(async_client, "erin.outage@shop.com")

    # Mission is created and discovers products first (happy path)
    resp = await async_client.post(
        "/missions/shopping",
        json={"prompt": "I want to order shampoo under ₹600 for anti-hairfall."},
    )
    assert resp.status_code == 200
    mission_id = resp.json()["mission_id"]
    assert resp.json()["status"] == "discovery_completed"

    # Now the reasoning provider goes down mid-conversation
    monkeypatch.setattr(
        missions_module, "get_reasoning_provider", lambda *a, **k: _FailingReasoning()
    )

    agent_resp = await async_client.post(
        f"/missions/{mission_id}/agent",
        json={"user_input": "Actually change my budget to 500 rupees"},
    )
    assert agent_resp.status_code == 200  # graceful, not a 5xx
    agent_data = agent_resp.json()

    assert agent_data["mission_status"] == "needs_clarification"
    assert agent_data["agent_proposal"]["action"] == "clarify_intent"
    assert agent_data["discovered_products"] == []
    assert "reasoning service" in agent_data["agent_message"].lower()