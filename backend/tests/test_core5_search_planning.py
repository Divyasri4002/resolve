"""
CORE-5 Phase 4: intelligent search planning + result-set reuse.

What is under test:

* The planner (app.agents.search_planning) is deterministic rule code, not a model: a changed
  constraint either evaluates against facts the current results already verify (price, merchant,
  count) and reuses the set through local filtering, or it forces a real provider search.
* Verified versus unknown is the load-bearing distinction: a missing price never satisfies a
  price filter, a missing merchant never satisfies a brand or an exclusion, and no filter is
  ever claimed to have matched when the fact it needs is absent.
* Result-set history: a new search appends and preserves; a reuse derives and links; nothing
  is destroyed, nothing grows unbounded, and pre-Phase-4 missions remain readable.
* The whole thing stays inside the existing search_products capability, touches no payment,
  order, authority or merchant-catalog surface, and runs hermetically: no SerpApi, no Gemini,
  no Groq, no Razorpay, no Shopify — the provider is the offline mock, the reasoning is a
  scripted stub.
"""
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.agents.constraints import CONSTRAINT_FIELDS, merge_intent_turn
from app.agents.reasoning_provider import MockReasoningProvider
from app.agents.search_planning import (
    MAX_RESULT_SETS,
    NEW_SEARCH,
    REUSE,
    apply_filters,
    apply_reuse,
    plan_search,
    query_from_constraints,
    record_search_set,
    reuse_message,
)
from app.agents.tools import tool_names
from app.database import Base, get_db
from app.main import app
from app.models.agent import AgentStepDecision
from app.models.mission import ShoppingIntent
from app.models.product import ProductResult
from app.routers import missions as missions_module

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

SEARCH_PLANNING_SOURCE = (
    Path(__file__).resolve().parents[1] / "app" / "agents" / "search_planning.py"
).read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Fixtures and helpers
# --------------------------------------------------------------------------- #


def _product(
    pid: str,
    title: str = "Repair Shampoo",
    price: str | None = "399.00",
    extracted: float | None = 399.0,
    merchant: str | None = "Nykaa",
    rating: float | None = 4.5,
    reviews: int | None = 3400,
    currency: str = "INR",
) -> ProductResult:
    return ProductResult(
        product_id=pid,
        title=title,
        price=price,
        extracted_price=Decimal(str(extracted)) if extracted is not None else None,
        currency=currency,
        merchant=merchant,
        product_url=f"https://example.com/{pid}",
        image_url=f"https://example.com/{pid}.jpg",
        rating=rating,
        review_count=reviews,
    )


@pytest.fixture
def products() -> list[ProductResult]:
    """Three verified results: prices, merchants, ratings and review counts all present."""
    return [
        _product("p_loreal", "L'Oreal Repair Shampoo", "549.00", 549, "Amazon.in", 4.3, 1250),
        _product("p_dove", "Dove Hair Fall Shampoo", "399.00", 399, "Nykaa", 4.5, 3400),
        _product(
            "p_premium", "Premium Repair Shampoo", "1200.00", 1200, "Flipkart", 4.1, 89
        ),
    ]


def _intent_with_set(
    products: list[ProductResult], query: str = "shampoo anti-hairfall", **constraints
) -> ShoppingIntent:
    """An intent that has already searched, recorded under the constraints it searched with."""
    base = dict(
        raw_prompt="I want shampoo under 600 for anti-hairfall",
        category="shampoo",
        budget=Decimal("600"),
        currency="INR",
        state="discovery_completed",
    )
    base.update(constraints)
    intent = ShoppingIntent(**base)
    record_search_set(intent, products, query)
    return intent


class _ScriptedProvider:
    """
    A reasoning provider with both halves scripted: the interpretation proposes exactly the
    constraint updates the test names, and the step choice always names one declared tool.
    Nothing here reaches a vendor, and it flows through the same merge, guards and registry
    the real providers do.
    """

    def __init__(self, updates: list[dict] | None = None) -> None:
        self.updates = updates or []

    async def interpret_shopping_intent(self, prompt_text: str) -> dict:
        return {
            "constraint_updates": list(self.updates),
            "ambiguous": False,
            "clarification": None,
            "missing_info": [],
            "state": None,
        }

    async def choose_agent_action(
        self, context: dict, tool_declarations: list[dict]
    ) -> AgentStepDecision:
        return AgentStepDecision.tool_call("search_products", {})

    async def summarize_product_results(
        self, query: str, budget: Decimal | None, products_json: list[dict]
    ) -> str:
        return f"{len(products_json)} products found for {query}."


def _install_counting_discovery(
    monkeypatch, sample_results: list[ProductResult] | None = None, error: Exception | None = None
) -> list:
    """Mock discovery with a handle on every provider instance, so call counts are observable."""
    from app.tools.product_discovery import ProductDiscoveryTool
    from app.tools.product_search.mock_provider import MockProductSearchProvider

    holders: list[MockProductSearchProvider] = []

    def factory() -> ProductDiscoveryTool:
        provider = MockProductSearchProvider(
            sample_results=list(sample_results) if sample_results is not None else []
        )
        if error is not None:
            async def _fail(**kwargs):  # type: ignore[no-untyped-def]
                raise error

            provider.search = _fail  # type: ignore[method-assign]
        holders.append(provider)
        return ProductDiscoveryTool(provider=provider)

    monkeypatch.setattr(missions_module, "get_product_discovery_tool", factory)
    return holders


def _provider_calls(holders: list) -> int:
    return sum(len(holder.search_calls) for holder in holders)


@pytest_asyncio.fixture
async def async_client() -> AsyncClient:
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


async def _create_shampoo_mission(client: AsyncClient) -> str:
    resp = await client.post(
        "/missions/shopping",
        json={"prompt": "I want to order shampoo under ₹600 for anti-hairfall."},
    )
    assert resp.status_code == 200
    return resp.json()["mission_id"]


# --------------------------------------------------------------------------- #
# Scenarios 1-7: constraints that verified facts can evaluate locally
# --------------------------------------------------------------------------- #


def test_price_ceiling_reuses_verified_prices(products):
    intent = _intent_with_set(products)
    intent.budget = Decimal("400")
    plan = plan_search(intent)
    assert plan.action == REUSE
    assert plan.filters == ({"kind": "max_price", "value": Decimal("400")},)
    assert plan.source_set_id == intent.result_sets[0].set_id
    reused = apply_reuse(intent, plan)
    assert [p.product_id for p in reused.products] == ["p_dove"]
    # The source set is untouched: three products, still the active history's first entry.
    assert len(intent.result_sets[0].products) == 3


def test_price_floor_filter_on_verified_prices(products):
    kept = apply_filters(products, [{"kind": "min_price", "value": Decimal("500")}])
    assert [p.product_id for p in kept] == ["p_loreal", "p_premium"]


def test_rating_threshold_filter_on_verified_ratings(products):
    kept = apply_filters(products, [{"kind": "min_rating", "value": Decimal("4.4")}])
    assert [p.product_id for p in kept] == ["p_dove"]


def test_review_count_threshold_filter_on_verified_counts(products):
    kept = apply_filters(products, [{"kind": "min_reviews", "value": 1000}])
    assert [p.product_id for p in kept] == ["p_loreal", "p_dove"]


def test_unchanged_constraints_reuse_the_set(products):
    """'Which is cheapest?' names no new constraint: the set already answers it."""
    intent = _intent_with_set(products)
    plan = plan_search(intent)
    assert plan.action == REUSE
    assert plan.filters == ()
    assert "have not changed" in plan.reason


def test_highest_rated_question_reuses_set_carrying_ratings(products):
    intent = _intent_with_set(products)
    plan = plan_search(intent)
    assert plan.action == REUSE
    assert all(p.rating is not None for p in intent.discovered_products)


def test_brand_reuse_when_merchant_is_verified(products):
    intent = _intent_with_set(products)
    intent.brand = "Nykaa"
    plan = plan_search(intent)
    assert plan.action == REUSE
    assert plan.filters == ({"kind": "merchant_is", "value": "Nykaa"},)
    reused = apply_reuse(intent, plan)
    assert [p.product_id for p in reused.products] == ["p_dove"]


def test_merchant_name_reuse_matches_verified_merchant_substrings(products):
    """'From Amazon' evaluates against the verified merchant 'Amazon.in'."""
    intent = _intent_with_set(products)
    intent.brand = "amazon"
    plan = plan_search(intent)
    assert plan.action == REUSE
    reused = apply_reuse(intent, plan)
    assert [p.product_id for p in reused.products] == ["p_loreal"]


# --------------------------------------------------------------------------- #
# Scenarios 8-13: constraints the results cannot verify force a real search
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "field_name, value",
    [
        ("color", "black"),
        ("material", "linen"),
        ("gender", "women"),
    ],
)
def test_unverifiable_concept_change_forces_new_search(products, field_name, value):
    intent = _intent_with_set(products)
    setattr(intent, field_name, value)
    plan = plan_search(intent)
    assert plan.action == NEW_SEARCH
    assert field_name in plan.reason


def test_category_change_forces_conservative_new_search(products):
    intent = _intent_with_set(products)
    intent.category = "conditioner"
    plan = plan_search(intent)
    assert plan.action == NEW_SEARCH
    assert "category" in plan.reason


def test_brand_no_verified_merchant_forces_conservative_new_search():
    partial = [_product("p_known", merchant="Nykaa"), _product("p_unknown", merchant=None)]
    intent = _intent_with_set(partial)
    intent.brand = "Nykaa"
    plan = plan_search(intent)
    assert plan.action == NEW_SEARCH
    assert "merchant" in plan.reason


def test_brand_matching_no_verified_merchant_forces_new_search(products):
    intent = _intent_with_set(products)
    intent.brand = "Pantene"
    plan = plan_search(intent)
    assert plan.action == NEW_SEARCH
    assert "verified merchant" in plan.reason


def test_unverifiable_exclusion_forces_new_search(products):
    """'Anything but leather' cannot be evaluated against merchant facts."""
    intent = _intent_with_set(products)
    intent.exclusions = ["leather"]
    plan = plan_search(intent)
    assert plan.action == NEW_SEARCH
    assert "exclusion" in plan.reason


def test_raised_budget_forces_new_search(products):
    """A higher ceiling may find products the narrower search never saw."""
    intent = _intent_with_set(products)
    intent.budget = Decimal("900")
    plan = plan_search(intent)
    assert plan.action == NEW_SEARCH
    assert "raised" in plan.reason


def test_budget_without_verified_prices_forces_new_search(products):
    unpriced = [_product("p_a", extracted=None, price=None), _product("p_b")]
    intent = _intent_with_set(unpriced)
    intent.budget = Decimal("400")
    plan = plan_search(intent)
    assert plan.action == NEW_SEARCH
    assert "price" in plan.reason


# --------------------------------------------------------------------------- #
# Scenario 13 (continued): exclusions and quantities that do evaluate
# --------------------------------------------------------------------------- #


def test_evaluable_exclusion_filters_verified_merchants():
    mixed = [
        _product("p_amazon_1", merchant="Amazon.in"),
        _product("p_nykaa", merchant="Nykaa"),
        _product("p_amazon_2", merchant="Amazon Fresh"),
    ]
    intent = _intent_with_set(mixed)
    intent.exclusions = ["amazon"]
    plan = plan_search(intent)
    assert plan.action == REUSE
    assert plan.filters == ({"kind": "merchant_not", "value": "amazon"},)
    reused = apply_reuse(intent, plan)
    assert [p.product_id for p in reused.products] == ["p_nykaa"]


def test_exclusion_removal_forces_new_search():
    mixed = [_product("p_amazon", merchant="Amazon.in"), _product("p_nykaa", merchant="Nykaa")]
    intent = _intent_with_set(mixed, exclusions=["amazon"])
    intent.exclusions = []
    plan = plan_search(intent)
    assert plan.action == NEW_SEARCH


def test_quantity_within_remaining_results_reuses_with_limit(products):
    intent = _intent_with_set(products)
    intent.quantity = 2
    plan = plan_search(intent)
    assert plan.action == REUSE
    assert plan.filters == ({"kind": "limit", "value": 2},)
    reused = apply_reuse(intent, plan)
    assert len(reused.products) == 2


def test_quantity_beyond_available_results_forces_new_search(products):
    intent = _intent_with_set(products)
    intent.quantity = 5
    plan = plan_search(intent)
    assert plan.action == NEW_SEARCH


# --------------------------------------------------------------------------- #
# The dry run: a filter that would keep nothing is not a reuse
# --------------------------------------------------------------------------- #


def test_filter_that_would_exclude_everything_forces_new_search(products):
    intent = _intent_with_set(products)
    intent.budget = Decimal("100")
    plan = plan_search(intent)
    assert plan.action == NEW_SEARCH
    assert "would leave none" in plan.reason


# --------------------------------------------------------------------------- #
# Scenario 19: missing values never satisfy a filter
# --------------------------------------------------------------------------- #


def test_missing_values_never_satisfy_filters():
    # Each product below is missing exactly one fact, so every filter demonstrably drops
    # precisely the product whose fact it needs — never anything else, and never itself.
    gapful = [
        _product("p_no_price", extracted=None, price=None),
        _product("p_no_merchant", merchant=None),
        _product("p_no_rating", rating=None),
        _product("p_no_reviews", reviews=None),
        _product("p_complete", rating=4.8, reviews=900),
    ]
    # A missing price is dropped by a price filter, even at the most permissive bound.
    assert [p.product_id for p in apply_filters(gapful, [{"kind": "max_price", "value": Decimal("900")}])] == [
        "p_no_merchant", "p_no_rating", "p_no_reviews", "p_complete",
    ]
    assert [p.product_id for p in apply_filters(gapful, [{"kind": "min_price", "value": Decimal("1")}])] == [
        "p_no_merchant", "p_no_rating", "p_no_reviews", "p_complete",
    ]
    # A missing merchant is dropped by a brand filter...
    assert [p.product_id for p in apply_filters(gapful, [{"kind": "merchant_is", "value": "nykaa"}])] == [
        "p_no_price", "p_no_rating", "p_no_reviews", "p_complete",
    ]
    # ...and by an exclusion too: an unknown merchant is not proof of satisfying it.
    assert [p.product_id for p in apply_filters(gapful, [{"kind": "merchant_not", "value": "amazon"}])] == [
        "p_no_price", "p_no_rating", "p_no_reviews", "p_complete",
    ]
    assert [p.product_id for p in apply_filters(gapful, [{"kind": "min_rating", "value": Decimal("4")}])] == [
        "p_no_price", "p_no_merchant", "p_no_reviews", "p_complete",
    ]
    assert [p.product_id for p in apply_filters(gapful, [{"kind": "min_reviews", "value": 1}])] == [
        "p_no_price", "p_no_merchant", "p_no_rating", "p_complete",
    ]


def test_filtering_never_mutates_canonical_products(products):
    original = [p.model_copy(deep=True) for p in products]
    apply_filters(products, [{"kind": "max_price", "value": Decimal("400")}])
    assert [p.model_dump() for p in products] == [p.model_dump() for p in original]


def test_unknown_filter_kind_is_refused(products):
    with pytest.raises(ValueError):
        apply_filters(products, [{"kind": "color_is", "value": "black"}])


# --------------------------------------------------------------------------- #
# Scenarios 16-18, 27: result-set history
# --------------------------------------------------------------------------- #


def test_previous_result_set_preserved_when_a_new_search_records(products):
    intent = _intent_with_set(products)
    first = intent.result_sets[0]
    fresh = [_product("p_new_one"), _product("p_new_two")]
    record_search_set(intent, fresh, "shampoo black")
    assert len(intent.result_sets) == 2
    assert intent.result_sets[0] is first
    assert [p.product_id for p in first.products] == ["p_loreal", "p_dove", "p_premium"]
    assert first.constraint_snapshot["budget"] == "600"
    assert first.query == "shampoo anti-hairfall"


def test_new_result_set_becomes_current(products):
    intent = _intent_with_set(products)
    fresh = [_product("p_new_one")]
    record = record_search_set(intent, fresh, "shampoo black")
    assert intent.active_result_set_id == record.set_id
    assert intent.result_sets[-1] is record
    assert [p.product_id for p in intent.discovered_products] == ["p_new_one"]
    assert record.origin == "search"
    assert record.constraint_snapshot["budget"] == "600"


def test_derived_result_set_retains_original_product_identity(products):
    intent = _intent_with_set(products)
    intent.budget = Decimal("400")
    reused = apply_reuse(intent, plan_search(intent))
    assert reused.origin == "filtered"
    assert reused.source_set_id == intent.result_sets[0].set_id
    for derived, original in zip(reused.products, [products[1]]):
        assert derived is original
        assert derived.product_id == original.product_id
        assert derived.source == original.source
        assert derived.product_url == original.product_url
        assert derived.title == original.title


def test_history_does_not_grow_unbounded_during_ordinary_filtering(products):
    intent = _intent_with_set(products)
    for step in range(15):
        intent.budget = Decimal(str(590 - step * 10))
        plan = plan_search(intent)
        assert plan.action == REUSE
        apply_reuse(intent, plan)
        assert len(intent.result_sets) <= MAX_RESULT_SETS
    assert len(intent.result_sets) == MAX_RESULT_SETS
    assert intent.active_result_set_id == intent.result_sets[-1].set_id


def test_reuse_message_names_filtering_for_what_it_is(products):
    intent = _intent_with_set(products)
    intent.budget = Decimal("400")
    reused = apply_reuse(intent, plan_search(intent))
    message = reuse_message(reused, intent.result_sets[0])
    assert "same" in message
    assert "filtered" in message
    assert "No new search was run" in message
    assert str(len(reused.products)) in message


# --------------------------------------------------------------------------- #
# Scenario 21: pre-Phase-4 missions remain readable and plan honestly
# --------------------------------------------------------------------------- #


def test_legacy_mission_without_result_sets_imports_its_set(products):
    legacy = ShoppingIntent(
        raw_prompt="shampoo",
        category="shampoo",
        budget=Decimal("600"),
        currency="INR",
        state="discovery_completed",
        discovered_products=products,
        discovery_query="shampoo",
    )
    assert legacy.result_sets == []
    plan = plan_search(legacy)
    assert len(legacy.result_sets) == 1
    imported = legacy.result_sets[0]
    assert imported.origin == "imported"
    assert imported.products == products
    assert imported.query == "shampoo"
    # The constraints that search ran under are not recorded anywhere, so the honest
    # snapshot is empty and every named constraint is re-evaluated, never assumed satisfied.
    assert imported.constraint_snapshot == {}
    assert plan.action == NEW_SEARCH  # category is set but verifiable against nothing
    assert legacy.active_result_set_id == imported.set_id


def test_legacy_mission_with_no_constraints_reuses_without_a_call(products):
    legacy = ShoppingIntent(
        raw_prompt="shampoo",
        state="discovery_completed",
        discovered_products=products,
        discovery_query="shampoo",
    )
    plan = plan_search(legacy)
    assert plan.action == REUSE
    assert plan.filters == ()
    assert [p.product_id for p in legacy.discovered_products] == [
        p.product_id for p in products
    ]


def test_existing_persisted_intent_round_trips_through_the_additive_fields(products):
    """A row written by this code reads back identically; a row without the fields reads too."""
    intent = _intent_with_set(products)
    round_tripped = ShoppingIntent(**intent.model_dump(mode="json"))
    assert round_tripped.active_result_set_id == intent.active_result_set_id
    assert round_tripped.result_sets[0].set_id == intent.result_sets[0].set_id
    assert [p.product_id for p in round_tripped.discovered_products] == [
        p.product_id for p in products
    ]
    bare = ShoppingIntent(raw_prompt="x")
    assert bare.result_sets == []
    assert bare.active_result_set_id is None


# --------------------------------------------------------------------------- #
# The query a real search sends
# --------------------------------------------------------------------------- #


def test_query_is_built_from_structured_constraints_not_the_raw_prompt():
    intent = ShoppingIntent(
        raw_prompt="make it under 700",
        category="shirts",
        brand="Zava",
        color="black",
        preferences=["linen-free", "slim fit"],
    )
    assert query_from_constraints(intent) == "Zava shirts black linen-free slim fit"


def test_query_falls_back_to_caller_suggestion_then_raw_prompt():
    bare = ShoppingIntent(raw_prompt="something under 600", budget=Decimal("600"))
    assert query_from_constraints(bare, fallback="shirts") == "shirts"
    assert query_from_constraints(bare) == "something under 600"
    assert query_from_constraints(ShoppingIntent(raw_prompt="  ")) is None


def test_query_is_capped_at_two_hundred_characters():
    intent = ShoppingIntent(raw_prompt="x", category="a" * 500)
    assert len(query_from_constraints(intent) or "") == 200


# --------------------------------------------------------------------------- #
# Scenarios 14, 15, 20: the controlled search execution, end to end
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_local_reuse_makes_zero_provider_calls(async_client, monkeypatch, products):
    holders = _install_counting_discovery(monkeypatch, sample_results=products)
    monkeypatch.setattr(
        missions_module, "get_reasoning_provider", lambda *a, **k: MockReasoningProvider()
    )
    mission_id = await _create_shampoo_mission(async_client)
    assert _provider_calls(holders) == 1  # the creation search, and nothing else

    monkeypatch.setattr(
        missions_module,
        "get_reasoning_provider",
        lambda *a, **k: _ScriptedProvider(
            updates=[{"field": "budget", "op": "set", "value": 400}]
        ),
    )
    resp = await async_client.post(
        f"/missions/{mission_id}/agent", json={"user_input": "make it under 400"}
    )
    assert resp.status_code == 200
    assert _provider_calls(holders) == 1  # reuse ran without touching the provider

    data = resp.json()
    assert data["mission_status"] == "discovery_completed"
    intent = data["shopping_intent"]
    assert float(intent["budget"]) == 400.0
    assert [p["product_id"] for p in intent["discovered_products"]] == ["p_dove"]
    assert len(intent["result_sets"]) == 2
    search_entry, filtered_entry = intent["result_sets"]
    assert filtered_entry["origin"] == "filtered"
    assert filtered_entry["source_set_id"] == search_entry["set_id"]
    assert intent["active_result_set_id"] == filtered_entry["set_id"]
    assert "No new search was run" in data["agent_message"]
    # The products handed to the loop are the same derived set.
    assert [p["product_id"] for p in data["discovered_products"]] == ["p_dove"]


@pytest.mark.asyncio
async def test_new_search_makes_exactly_one_provider_call_and_records_it(
    async_client, monkeypatch, products
):
    holders = _install_counting_discovery(monkeypatch, sample_results=products)
    monkeypatch.setattr(
        missions_module, "get_reasoning_provider", lambda *a, **k: MockReasoningProvider()
    )
    mission_id = await _create_shampoo_mission(async_client)
    assert _provider_calls(holders) == 1

    monkeypatch.setattr(
        missions_module,
        "get_reasoning_provider",
        lambda *a, **k: _ScriptedProvider(
            updates=[{"field": "color", "op": "set", "value": "black"}]
        ),
    )
    resp = await async_client.post(
        f"/missions/{mission_id}/agent", json={"user_input": "make them black"}
    )
    assert resp.status_code == 200
    assert _provider_calls(holders) == 2  # exactly one new provider call for this turn

    intent = resp.json()["shopping_intent"]
    assert len(intent["result_sets"]) == 2
    first, second = intent["result_sets"]
    assert first["origin"] == "search"
    assert second["origin"] == "search"
    assert second["source_set_id"] is None
    assert second["query"] == "shampoo black anti-hairfall"  # structured constraints, not the raw message
    assert intent["discovery_query"] == "shampoo black anti-hairfall"
    assert intent["active_result_set_id"] == second["set_id"]
    # The pre-Phase-4 set was preserved with the products and constraints it was found under.
    assert [p["product_id"] for p in first["products"]] == ["p_loreal", "p_dove"]
    assert Decimal(str(first["constraint_snapshot"]["budget"])) == Decimal("600")


@pytest.mark.asyncio
async def test_provider_failure_degrades_honestly_and_preserves_the_set(
    async_client, monkeypatch, products
):
    holders = _install_counting_discovery(monkeypatch, sample_results=products)
    monkeypatch.setattr(
        missions_module, "get_reasoning_provider", lambda *a, **k: MockReasoningProvider()
    )
    mission_id = await _create_shampoo_mission(async_client)

    _install_counting_discovery(
        monkeypatch, sample_results=products, error=RuntimeError("provider down")
    )
    monkeypatch.setattr(
        missions_module,
        "get_reasoning_provider",
        lambda *a, **k: _ScriptedProvider(
            updates=[{"field": "color", "op": "set", "value": "black"}]
        ),
    )
    resp = await async_client.post(
        f"/missions/{mission_id}/agent", json={"user_input": "make them black"}
    )
    assert resp.status_code == 200
    data = resp.json()
    # The failed search itself is what the trace records; the identical retry the scripted
    # chooser then proposes is stopped by the loop's duplicate guard, so the turn ends.
    assert any(
        entry["tool"] == "search_products" and entry["success"] is False
        for entry in data["agent_tools"]
    )
    # Nothing was lost: the last good set is still current, still mirrored, still one entry.
    intent = data["shopping_intent"]
    assert len(intent["result_sets"]) == 1
    assert intent["result_sets"][0]["origin"] == "search"
    assert [p["product_id"] for p in intent["discovered_products"]] == ["p_loreal", "p_dove"]
    # The degradation contract is unchanged: a failed re-search parks the mission in the
    # retryable state (the lifecycle guard protects product_selected and beyond), while the
    # last good set stays intact and current.
    assert data["mission_status"] == "ready_for_discovery"


# --------------------------------------------------------------------------- #
# Scenarios 22-25: the planner's boundaries
# --------------------------------------------------------------------------- #


def test_planner_module_exposes_no_credentials_or_execution_surface():
    lowered = SEARCH_PLANNING_SOURCE.casefold()
    for token in (
        "api_key",
        "apikey",
        "authorization",
        "bearer",
        "httpx",
        "aiohttp",
        "urllib",
        "requests",
        "os.environ",
        "getenv",
        "subprocess",
        "socket",
        "eval(",
        "exec(",
    ):
        assert token not in lowered, f"search_planning.py must not contain {token!r}"


def test_planner_module_touches_no_payment_order_or_authority_surface():
    lowered = SEARCH_PLANNING_SOURCE.casefold()
    for token in ("razorpay", "payment", "checkout", "refund", "create_order", "authority"):
        assert token not in lowered, f"search_planning.py must not contain {token!r}"


def test_planner_module_never_names_the_merchant_catalog_vendor():
    assert "shopify" not in SEARCH_PLANNING_SOURCE.casefold()


def test_agent_registry_remains_closed():
    names = tool_names()
    assert "search_products" in names
    assert "filter_products" not in names
    assert len(names) == len(set(names))
    assert all("plan" not in name for name in names)


def test_phase3_constraint_registry_and_merge_unchanged():
    assert len(CONSTRAINT_FIELDS) == 11
    merged = merge_intent_turn(
        {"raw_prompt": "shampoo", "budget": Decimal("600")},
        {
            "constraint_updates": [{"field": "budget", "op": "set", "value": 400}],
            "ambiguous": False,
            "clarification": None,
        },
        "make it 400",
    )
    assert merged["budget"] == Decimal("400")
    assert merged["raw_prompt"] == "shampoo"


# --------------------------------------------------------------------------- #
# Determinism: the same state always plans the same way
# --------------------------------------------------------------------------- #


def test_planning_is_deterministic_across_repeated_runs(products):
    intent = _intent_with_set(products)
    intent.budget = Decimal("400")
    plans = [plan_search(intent) for _ in range(5)]
    first = plans[0]
    for other in plans[1:]:
        assert other.action == first.action
        assert other.reason == first.reason
        assert other.filters == first.filters
        assert other.source_set_id == first.source_set_id
