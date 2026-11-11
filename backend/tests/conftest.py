"""
Hermetic test configuration for the RESOLVE backend suite.

Every test here runs against mocks ONLY — no live LLM calls, no live SerpApi calls:

- REASONING_PROVIDER=mock        -> MockReasoningProvider (deterministic, offline)
- REASONING_API_KEY=""           -> never touches a real vendor SDK key
- PRODUCT_SEARCH_PROVIDER=mock   -> MockProductSearchProvider (offline)
- SERPAPI_API_KEY=""             -> the real SerpApi integration is never invoked
- SHOPPING_REFUND_AUTHORITY_*    -> the bounded refund ceiling of Task 11B is configured ON in
  this hermetic environment with a small ceiling (₹500), so tests exercise both outcomes; the
  production default stays OFF. Tests that need the disabled or over-ceiling behaviour flip it
  explicitly.
- SHOPPING_REPLACEMENT_AUTHORITY_ENABLED=true -> same reasoning for Task 12A's replacement
  switch (production default OFF). REPLACEMENT_PROVIDER stays 'none': no merchant fulfilment
  provider exists, and tests install a fake through the same seam rather than configuring one.
- SHOPPING_RETURN_AUTHORITY_ENABLED=true -> the same treatment for Task 12B's return switch
  (production default OFF), with RETURN_PROVIDER='none' — no merchant return provider exists.
- SHOPPING_PURCHASE_AUTHORITY_ENABLED=true -> the same treatment for the buyer-flow Phase 3
  purchase switch (production default OFF). MERCHANT_TRIGGER_URL/TOKEN stay empty here: tests
  that exercise the submission install an httpx.MockTransport through the purchase_trigger
  seam, so no test ever opens a network connection to a trigger process.
- SHOPPING_DEMO_DELIVERY_ENABLED=false -> the demo-environment delivery transition (M22 §1)
  stays OFF for the whole suite. The dev/demo .env ships it ON for the live demo, and pydantic
  would otherwise let that leak into every test run; tests that prove the delivery (the M22 P3
  battery) flip it explicitly per test via the settings object.

Environment variables take priority over backend/.env in pydantic-settings, and this
module is imported by pytest BEFORE any ``app.*`` import, so the cached Settings()
always resolve to the hermetic configuration.
"""
import os

os.environ["REASONING_PROVIDER"] = "mock"
os.environ["REASONING_API_KEY"] = ""
os.environ["PRODUCT_SEARCH_PROVIDER"] = "mock"
os.environ["SERPAPI_API_KEY"] = ""
os.environ["SHOPPING_REFUND_AUTHORITY_ENABLED"] = "true"
os.environ["SHOPPING_REFUND_MAX_INR"] = "500"
os.environ["SHOPPING_REPLACEMENT_AUTHORITY_ENABLED"] = "true"
os.environ["REPLACEMENT_PROVIDER"] = "none"
os.environ["SHOPPING_RETURN_AUTHORITY_ENABLED"] = "true"
os.environ["RETURN_PROVIDER"] = "none"
os.environ["SHOPPING_PURCHASE_AUTHORITY_ENABLED"] = "true"
os.environ["SHOPPING_DEMO_DELIVERY_ENABLED"] = "false"

import pytest
from decimal import Decimal

from app.models.product import ProductResult

# --------------------------------------------------------------------------- #
# Hermetic product feed, shaped exactly like normalized SerpApi Google Shopping
# results so the discovery chain (filter -> normalize -> ground) is exercised.
# --------------------------------------------------------------------------- #
SAMPLE_DISCOVERY_PRODUCTS = [
    ProductResult(
        product_id="item_loreal",
        title="L'Oreal Paris Total Repair 5 Shampoo, 650ml",
        price="₹549.00",
        extracted_price=Decimal("549.0"),
        currency="INR",
        merchant="Amazon.in",
        product_url="https://example.com/loreal-shampoo",
        image_url="https://example.com/loreal.jpg",
        rating=4.3,
        review_count=1250,
        availability="In stock",
        extensions=["Top rated", "Free shipping"],
    ),
    ProductResult(
        product_id="item_dove",
        title="Dove Hair Fall Rescue Shampoo",
        price="₹399.00",
        extracted_price=Decimal("399.0"),
        currency="INR",
        merchant="Nykaa",
        product_url="https://example.com/dove-shampoo",
        image_url="https://example.com/dove.jpg",
        rating=4.5,
        review_count=3400,
        availability="In stock",
        extensions=["Sale"],
    ),
    # Deliberately above the budgets used in tests (₹600 / ₹1,200) so the
    # deterministic budget filter is what keeps it out of grounding, not the LLM.
    ProductResult(
        product_id="item_premium",
        title="Premium Organic Salon Care Shampoo 1000ml",
        price="₹1,200.00",
        extracted_price=Decimal("1200.0"),
        currency="INR",
        merchant="Flipkart",
        product_url="https://example.com/premium-shampoo",
        rating=4.1,
        review_count=89,
    ),
]


@pytest.fixture
def sample_products() -> list[ProductResult]:
    """Hermetic product feed for discovery tests (Task 04B)."""
    return list(SAMPLE_DISCOVERY_PRODUCTS)


@pytest.fixture
def install_mock_discovery(monkeypatch):
    """
    Install a mock-backed ProductDiscoveryTool factory into the missions router.

    The production chain is Gemini -> ProductDiscoveryTool -> SerpApi provider; tests
    replace the discovery-tool factory (the same seam the production code uses) so the
    router executes the identical code path with an offline provider.

    Usage:
        install_mock_discovery(sample_results=[...])
        install_mock_discovery(error=ProductSearchError("boom", status_code=503))
    """
    from app.routers import missions as missions_module
    from app.tools.product_discovery import ProductDiscoveryTool
    from app.tools.product_search.mock_provider import MockProductSearchProvider

    def _install(sample_results: list[ProductResult] | None = None, error: Exception | None = None):
        def factory() -> ProductDiscoveryTool:
            provider = MockProductSearchProvider(
                sample_results=list(sample_results) if sample_results is not None else []
            )
            if error is not None:
                async def _fail(**kwargs):  # type: ignore[no-untyped-def]
                    raise error

                provider.search = _fail  # type: ignore[method-assign]
            return ProductDiscoveryTool(provider=provider)

        monkeypatch.setattr(missions_module, "get_product_discovery_tool", factory)
        return factory

    return _install


@pytest.fixture(autouse=True)
def hermetic_db_enforcement(monkeypatch):
    """
    Arm the Phase 4.6.1 protected-operation gate for the hermetic suite.

    Route tests drive the app through httpx's ASGITransport, which does not run the
    FastAPI lifespan, so the startup verification (app.db_enforcement.initialize) never
    executes here and the module gate would refuse every protected operation. This
    fixture installs exactly the report the runtime would produce on the SQLite test
    dialect — role separation is out of scope there (enforcement is proven on isolated
    PostgreSQL 16 by test_role_separation.py) — so purchase/linkage route tests exercise
    their real logic. A test may install a refused report through the same seam to prove
    the 503 fail-closed path.
    """
    from app import db_enforcement

    report = db_enforcement.EnforcementReport(
        dialect="sqlite",
        protected_ops_allowed=True,
        runtime_user=None,
        failures=["hermetic suite: dialect 'sqlite' cannot express role separation"],
    )
    monkeypatch.setattr(db_enforcement, "_report", report)