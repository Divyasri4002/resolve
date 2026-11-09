"""
CORE-5 Phase 5: deterministic product reference resolution.

What is under test:

* The resolver (app.agents.reference_resolution) is pure, deterministic code that resolves
  "the second one", "the cheapest", "the one from Amazon", "that one" and "the one you
  recommended" against the mission's own persisted result set — never the model.
* Failing loud is the contract: out-of-range ordinals and missing merchants are NOT_FOUND,
  phrases that match several products are AMBIGUOUS with the candidates attached, phrases
  this module deliberately does not implement ("cheaper ones", "the second cheapest") are
  UNSUPPORTED, and nothing is ever approximated or silently chosen.
* Verified facts only: a missing price is never zero, a missing rating is never zero,
  review count is never converted into a rating, ties keep persisted order.
* Server authority: no message content can mint product identity — only a product already
  in the ACTIVE result set can be returned, and arbitrary submitted ids/prices cannot
  influence the outcome. The agent context carries the resolution, the model is told what
  the server resolved, and the canonical `product_reference` conversation event persists
  the anchor for later turns.
* Integration: the resolver runs inside the agent turn, `recommend_product` is the one
  deliberate registry addition, and the whole thing runs hermetically — no SerpApi, no
  Gemini, no Groq, no Razorpay, no Shopify.
"""
import copy
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.agents.constraints import CONSTRAINT_FIELDS
from app.agents.reference_resolution import (
    AMBIGUOUS,
    ANCHOR_EVENT_TYPE,
    ANCHOR_KIND_RECOMMENDATION,
    ANCHOR_KIND_RESOLVED,
    KIND_CHEAPEST,
    KIND_CONVERSATION,
    KIND_MERCHANT,
    KIND_MOST_EXPENSIVE,
    KIND_ORDINAL,
    KIND_RECOMMENDATION,
    NOT_FOUND,
    NO_REFERENCE,
    RESOLVED,
    UNSUPPORTED,
    active_result_products,
    conversation_anchors,
    resolve_product_reference,
)
from app.agents.search_planning import plan_search, record_search_set
from app.agents.tools import tool_names
from app.database import Base, get_db
from app.main import app
from app.models.agent import AgentStepDecision
from app.models.mission import ShoppingIntent
from app.models.product import ProductResult
from app.routers import missions as missions_module

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

REFERENCE_RESOLUTION_SOURCE = (
    Path(__file__).resolve().parents[1] / "app" / "agents" / "reference_resolution.py"
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


def _anchor(product_id: str, kind: str = ANCHOR_KIND_RESOLVED, sequence: int = 1) -> dict:
    return {
        "role": "system",
        "event_type": ANCHOR_EVENT_TYPE,
        "content": f"anchor {sequence}",
        "tool_name": None,
        "payload": {"kind": kind, "product_id": product_id, "set_id": "rs_current"},
        "sequence": sequence,
    }


def _active_set_id(intent: ShoppingIntent) -> str:
    return intent.active_result_set_id


class _ScriptedAgent:
    """
    A reasoning provider whose step choices are scripted and whose contexts are captured.
    The interpretation half proposes nothing, so a turn's only live inputs are the user
    message and what the server resolved for it.
    """

    def __init__(self, steps: list[AgentStepDecision] | None = None) -> None:
        self.steps = list(steps or [])
        self.contexts: list[dict] = []

    async def interpret_shopping_intent(self, prompt_text: str) -> dict:
        return {
            "constraint_updates": [],
            "ambiguous": False,
            "clarification": None,
            "missing_info": [],
            "state": None,
        }

    async def choose_agent_action(
        self, context: dict, tool_declarations: list[dict]
    ) -> AgentStepDecision:
        self.contexts.append(copy.deepcopy(context))
        if self.steps:
            return self.steps.pop(0)
        return AgentStepDecision.respond("Understood.")

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


async def _run_agent_turn(
    client: AsyncClient, mission_id: str, user_input: str, steps: list[AgentStepDecision]
) -> dict:
    monkey_provider = _ScriptedAgent(steps)
    from unittest.mock import patch as _patch  # noqa: PLC0415 — test-local seam switch

    with _patch.object(missions_module, "get_reasoning_provider", lambda *a, **k: monkey_provider):
        resp = await client.post(f"/missions/{mission_id}/agent", json={"user_input": user_input})
    assert resp.status_code == 200
    return resp.json()


# --------------------------------------------------------------------------- #
# Scenarios 1-6: ordinals resolve against the persisted ordering of the active set
# --------------------------------------------------------------------------- #


def test_scenario1_the_first_one_resolves_to_position_one(products):
    intent = _intent_with_set(products)
    ref = resolve_product_reference("I'll take the first one", intent)
    assert ref.state == RESOLVED and ref.kind == KIND_ORDINAL
    assert ref.product is products[0]
    assert ref.position == 1 and ref.set_id == _active_set_id(intent)


def test_scenario2_the_second_one(products):
    intent = _intent_with_set(products)
    ref = resolve_product_reference("make it the second one", intent)
    assert ref.state == RESOLVED and ref.product.product_id == "p_dove"
    assert ref.position == 2


def test_scenario3_the_third_one(products):
    intent = _intent_with_set(products)
    ref = resolve_product_reference("the third one please", intent)
    assert ref.state == RESOLVED and ref.product.product_id == "p_premium"
    assert ref.position == 3


def test_scenario4_number_ordinal(products):
    intent = _intent_with_set(products)
    assert resolve_product_reference("number 2", intent).product.product_id == "p_dove"
    assert resolve_product_reference("number 3", intent).product.product_id == "p_premium"


def test_scenario5_option_and_hash_ordinal(products):
    intent = _intent_with_set(products)
    assert resolve_product_reference("option 3", intent).product.product_id == "p_premium"
    assert resolve_product_reference("#1", intent).product.product_id == "p_loreal"
    assert resolve_product_reference("2 product", intent).product.product_id == "p_dove"


def test_scenario5b_suffix_ordinals(products):
    intent = _intent_with_set(products)
    ref = resolve_product_reference("the 2nd product", intent)
    assert ref.state == RESOLVED and ref.product.product_id == "p_dove"


def test_scenario6_ordinal_past_the_end_is_not_found(products):
    intent = _intent_with_set(products)
    ref = resolve_product_reference("the fifth one", intent)
    assert ref.state == NOT_FOUND and ref.product is None
    assert "3" in (ref.question or "")


def test_ordinals_never_wrap_or_randomize(products):
    """Repeated calls with the same inputs give the same answer — counting is code, not sampling."""
    intent = _intent_with_set(products)
    first = resolve_product_reference("the second one", intent)
    again = resolve_product_reference("the second one", intent)
    assert first == again
    for number in ("number 4", "number 9", "number 27"):
        assert resolve_product_reference(number, intent).state == NOT_FOUND


def test_the_last_one_resolves_to_the_end_of_the_set(products):
    intent = _intent_with_set(products)
    ref = resolve_product_reference("the last one", intent)
    assert ref.state == RESOLVED and ref.product.product_id == "p_premium"
    assert ref.position == 3


def test_two_ordinals_are_ambiguous(products):
    intent = _intent_with_set(products)
    ref = resolve_product_reference("the first one or the second one", intent)
    assert ref.state == AMBIGUOUS and ref.kind == KIND_ORDINAL
    assert {p.product_id for p in ref.candidates} == {"p_loreal", "p_dove"}


# --------------------------------------------------------------------------- #
# Scenarios 7-14: rankings on verified facts only
# --------------------------------------------------------------------------- #


def test_scenario7_cheapest_resolves_on_verified_price(products):
    intent = _intent_with_set(products)
    ref = resolve_product_reference("which is the cheapest", intent)
    assert ref.state == RESOLVED and ref.kind == KIND_CHEAPEST
    assert ref.product.product_id == "p_dove" and ref.position == 2


def test_scenario8_the_most_expensive(products):
    intent = _intent_with_set(products)
    ref = resolve_product_reference("the most expensive one", intent)
    assert ref.state == RESOLVED and ref.kind == KIND_MOST_EXPENSIVE
    assert ref.product.product_id == "p_premium"


def test_scenario9_highest_rated(products):
    intent = _intent_with_set(products)
    ref = resolve_product_reference("the best rated one", intent)
    assert ref.state == RESOLVED and ref.product.product_id == "p_dove"


def test_scenario10_lowest_rated(products):
    intent = _intent_with_set(products)
    ref = resolve_product_reference("the lowest rated", intent)
    assert ref.state == RESOLVED and ref.product.product_id == "p_premium"


def test_rank_phrasings_all_land_on_the_same_mechanism(products):
    intent = _intent_with_set(products)
    for message in (
        "the cheapest",
        "the lowest price",
        "the least expensive",
        "Which one is cheapest?",
        "cheapest option",
    ):
        ref = resolve_product_reference(message, intent)
        assert ref.state == RESOLVED and ref.product.product_id == "p_dove", message


def test_scenario11_ranking_ignores_products_missing_the_fact():
    unpriced = [
        _product("p_ghost", "Ghost Shampoo", price=None, extracted=None),
        _product("p_b", "Cheap Shampoo", "399.00", 399),
        _product("p_c", "Costly Shampoo", "799.00", 799),
    ]
    intent = _intent_with_set(unpriced)
    assert resolve_product_reference("the cheapest", intent).product.product_id == "p_b"
    assert resolve_product_reference("the most expensive", intent).product.product_id == "p_c"


def test_scenario11b_rank_without_any_verified_price_is_not_found():
    unpriced = [
        _product("p_x", "Mystery One", price=None, extracted=None),
        _product("p_y", "Mystery Two", price=None, extracted=None),
    ]
    intent = _intent_with_set(unpriced)
    ref = resolve_product_reference("the cheapest", intent)
    assert ref.state == NOT_FOUND and ref.product is None


def test_scenario12_missing_rating_is_never_zero():
    partly = [
        _product("p_unrated", "Unrated Shampoo", rating=None),
        _product("p_rated", "Rated Shampoo", "499.00", 499, rating=4.0),
    ]
    intent = _intent_with_set(partly)
    ref = resolve_product_reference("the best rated one", intent)
    assert ref.product.product_id == "p_rated"
    ref = resolve_product_reference("the lowest rated", intent)
    # The unrated product is skipped, not treated as a zero that would win "lowest".
    assert ref.product.product_id == "p_rated"


def test_scenario12b_no_verified_rating_is_not_found():
    unrated = [_product("p_x", rating=None), _product("p_y", rating=None)]
    intent = _intent_with_set(unrated)
    assert resolve_product_reference("the best rated one", intent).state == NOT_FOUND


def test_scenario13_price_tie_keeps_persisted_order():
    tied = [
        _product("p_t1", "First Tie", "399.00", 399),
        _product("p_t2", "Second Tie", "399.00", 399),
    ]
    intent = _intent_with_set(tied)
    ref = resolve_product_reference("the cheapest", intent)
    assert ref.product.product_id == "p_t1" and ref.position == 1


def test_scenario14_rating_tie_keeps_persisted_order():
    tied = [_product("p_t1", rating=4.0), _product("p_t2", rating=4.0)]
    intent = _intent_with_set(tied)
    ref = resolve_product_reference("the best rated one", intent)
    assert ref.product.product_id == "p_t1"


def test_review_count_is_never_converted_into_a_rating(products):
    review_heavy = [
        _product("p_big", "Popular Shampoo", rating=3.9, reviews=9000),
        _product("p_small", "Loved Shampoo", rating=4.8, reviews=10),
    ]
    intent = _intent_with_set(review_heavy)
    assert resolve_product_reference("the most reviewed", intent).product.product_id == "p_big"
    assert resolve_product_reference("the best rated one", intent).product.product_id == "p_small"


def test_compound_rank_is_unsupported(products):
    intent = _intent_with_set(products)
    ref = resolve_product_reference("the second cheapest", intent)
    assert ref.state == UNSUPPORTED and ref.product is None


def test_plural_request_is_unsupported(products):
    intent = _intent_with_set(products)
    assert resolve_product_reference("show me the first two", intent).state == UNSUPPORTED


def test_comparatives_are_unsupported(products):
    """'Actually show me cheaper ones' is a new search (Phase 4's job), never a silent pick."""
    intent = _intent_with_set(products)
    for message in ("show me cheaper ones", "something more expensive", "another one"):
        ref = resolve_product_reference(message, intent)
        assert ref.state == UNSUPPORTED, message


def test_two_ranks_are_ambiguous(products):
    intent = _intent_with_set(products)
    ref = resolve_product_reference("the cheapest or the most expensive one", intent)
    assert ref.state == AMBIGUOUS


def test_bare_rank_word_stays_a_search_request(products):
    """'Cheapest shampoo' names a new search; it is not a pointer into the current results."""
    intent = _intent_with_set(products)
    assert resolve_product_reference("cheapest shampoo under 400", intent).state == NO_REFERENCE


# --------------------------------------------------------------------------- #
# Scenarios 15-18: merchant and source matching on persisted values only
# --------------------------------------------------------------------------- #


def test_scenario15_merchant_match_resolves(products):
    intent = _intent_with_set(products)
    ref = resolve_product_reference("the one from Nykaa", intent)
    assert ref.state == RESOLVED and ref.kind == KIND_MERCHANT
    assert ref.product.product_id == "p_dove" and ref.position == 2


def test_scenario16_merchant_match_is_case_insensitive(products):
    intent = _intent_with_set(products)
    assert resolve_product_reference("the one from nykaa", intent).product.product_id == "p_dove"
    assert resolve_product_reference("which one is from AMAZON?", intent).product.product_id == (
        "p_loreal"
    )


def test_scenario17_multiple_merchant_matches_are_ambiguous():
    two_nykaa = [
        _product("p_a", "Nykaa One", "399.00", 399, "Nykaa"),
        _product("p_b", "Nykaa Two", "499.00", 499, "Nykaa"),
        _product("p_c", "Flipkart One", "599.00", 599, "Flipkart"),
    ]
    intent = _intent_with_set(two_nykaa)
    ref = resolve_product_reference("the one from Nykaa", intent)
    assert ref.state == AMBIGUOUS
    assert [p.product_id for p in ref.candidates] == ["p_a", "p_b"]


def test_scenario18_merchant_absent_from_the_set_is_not_found(products):
    intent = _intent_with_set(products)
    for message in ("the one from Myntra", "which one is from Myntra?"):
        ref = resolve_product_reference(message, intent)
        assert ref.state == NOT_FOUND and ref.kind == KIND_MERCHANT, message
        assert ref.product is None


def test_merchant_scopes_ordinal_and_rank(products):
    intent = _intent_with_set(products)
    ref = resolve_product_reference("the first one from amazon", intent)
    assert ref.state == RESOLVED and ref.product.product_id == "p_loreal"
    # The subset the merchant names is the whole pool: a second one from Amazon does not exist.
    assert resolve_product_reference("the second one from amazon", intent).state == NOT_FOUND
    ref = resolve_product_reference("the cheapest one from nykaa", intent)
    assert ref.state == RESOLVED and ref.product.product_id == "p_dove"


def test_source_words_can_never_match_a_merchant(products):
    """'google'/'shopping' name no product's source — only the persisted value could, and it is
    deliberately excluded so a common phrase cannot turn every result into a candidate."""
    intent = _intent_with_set(products)
    ref = resolve_product_reference("the one from google shopping", intent)
    assert ref.kind != KIND_MERCHANT or ref.state != RESOLVED


def test_from_something_without_a_definite_marker_is_a_search_request(products):
    intent = _intent_with_set(products)
    ref = resolve_product_reference("get me something from amazon", intent)
    assert ref.state == NO_REFERENCE


# --------------------------------------------------------------------------- #
# Scenarios 19-21: conservative title matching
# --------------------------------------------------------------------------- #


def test_scenario19_exact_title_match_resolves(products):
    intent = _intent_with_set(products)
    ref = resolve_product_reference("the dove hair fall shampoo", intent)
    assert ref.state == RESOLVED and ref.product.product_id == "p_dove"
    assert ref.position == 2


def test_scenario20_multiple_title_matches_are_ambiguous():
    two_aloe = [
        _product("p_a", "Aloe Soothing Shampoo"),
        _product("p_b", "Aloe Calming Shampoo"),
        _product("p_c", "Deep Clean Shampoo"),
    ]
    intent = _intent_with_set(two_aloe)
    ref = resolve_product_reference("the aloe shampoo", intent)
    assert ref.state == AMBIGUOUS
    assert {p.product_id for p in ref.candidates} == {"p_a", "p_b"}


def test_scenario21_approximate_titles_never_resolve(products):
    """A typo or a near-miss gets NOT_FOUND — the resolver never silently picks the closest."""
    intent = _intent_with_set(products)
    for message in ("the doven shampoo", "the repairing shampoo", "the herbal shampoo"):
        ref = resolve_product_reference(message, intent)
        assert ref.state == NOT_FOUND, message
        assert ref.product is None


def test_vague_noun_phrases_are_no_reference(products):
    intent = _intent_with_set(products)
    for message in ("the shirt", "I'm shopping for shampoo"):
        assert resolve_product_reference(message, intent).state == NO_REFERENCE


# --------------------------------------------------------------------------- #
# Scenarios 22-26: conversational pointers and recommendations from persisted anchors
# --------------------------------------------------------------------------- #


def test_scenario22_this_one_follows_the_persisted_anchor(products):
    intent = _intent_with_set(products)
    history = [_anchor("p_dove", ANCHOR_KIND_RESOLVED, sequence=4)]
    ref = resolve_product_reference("this one", intent, history)
    assert ref.state == RESOLVED and ref.kind == KIND_CONVERSATION
    assert ref.product.product_id == "p_dove" and ref.position == 2


def test_scenario23_pointer_without_any_pinned_product_is_ambiguous(products):
    intent = _intent_with_set(products)
    ref = resolve_product_reference("this one", intent)
    assert ref.state == AMBIGUOUS and (ref.question or "").strip()


def test_scenario24_that_one_uses_the_newest_anchor(products):
    intent = _intent_with_set(products)
    history = [
        _anchor("p_loreal", ANCHOR_KIND_RESOLVED, sequence=3),
        _anchor("p_dove", ANCHOR_KIND_RESOLVED, sequence=6),
    ]
    ref = resolve_product_reference("that one", intent, history)
    assert ref.state == RESOLVED and ref.product.product_id == "p_dove"


def test_pointer_to_a_product_no_longer_in_the_set_is_not_found(products):
    intent = _intent_with_set(products)
    history = [_anchor("p_gone", ANCHOR_KIND_RESOLVED, sequence=2)]
    ref = resolve_product_reference("this one", intent, history)
    assert ref.state == NOT_FOUND and ref.product is None


def test_pointer_falls_back_to_the_selected_product(products):
    intent = _intent_with_set(products)
    intent.selected_product = products[0]
    ref = resolve_product_reference("this one", intent)
    assert ref.state == RESOLVED and ref.product.product_id == "p_loreal"


def test_prose_mentions_are_never_anchors(products):
    """An agent sentence naming a product in prose pins nothing — only structured payloads do."""
    intent = _intent_with_set(products)
    history = [
        {
            "role": "agent",
            "event_type": "agent_message",
            "content": "I recommend the Dove Hair Fall Shampoo.",
            "tool_name": None,
            "payload": None,
            "sequence": 2,
        }
    ]
    assert resolve_product_reference("this one", intent, history).state == AMBIGUOUS
    assert resolve_product_reference("the one you recommended", intent, history).state == AMBIGUOUS


def test_scenario25_recommendation_resolves_from_the_recorded_anchor(products):
    intent = _intent_with_set(products)
    history = [_anchor("p_premium", ANCHOR_KIND_RECOMMENDATION, sequence=5)]
    for message in ("the one you recommended", "your recommendation"):
        ref = resolve_product_reference(message, intent, history)
        assert ref.state == RESOLVED and ref.kind == KIND_RECOMMENDATION, message
        assert ref.product.product_id == "p_premium" and ref.position == 3


def test_recommendation_without_a_recorded_anchor_is_ambiguous(products):
    intent = _intent_with_set(products)
    ref = resolve_product_reference("the one you recommended", intent)
    assert ref.state == AMBIGUOUS and (ref.question or "").strip()


def test_recommended_product_gone_from_the_set_is_not_found(products):
    intent = _intent_with_set(products)
    history = [_anchor("p_gone", ANCHOR_KIND_RECOMMENDATION, sequence=5)]
    ref = resolve_product_reference("the one you recommended", intent, history)
    assert ref.state == NOT_FOUND


def test_conversation_anchors_read_rows_and_dicts_alike(products):
    dict_row = _anchor("p_dove", sequence=1)
    row = SimpleNamespace(
        event_type=ANCHOR_EVENT_TYPE,
        payload={"kind": ANCHOR_KIND_RESOLVED, "product_id": "p_loreal"},
        sequence=2,
    )
    prose = SimpleNamespace(
        event_type="agent_message", payload=None, content="the dove one is nice", sequence=3
    )
    foreign_kind = SimpleNamespace(
        event_type=ANCHOR_EVENT_TYPE, payload={"kind": "other", "product_id": "p_x"}, sequence=4
    )
    anchors = conversation_anchors([dict_row, row, prose, foreign_kind])
    assert [a["product_id"] for a in anchors] == ["p_dove", "p_loreal"]


# --------------------------------------------------------------------------- #
# Scenarios 27-31: active-set discipline and server authority
# --------------------------------------------------------------------------- #


def test_scenario27_no_active_result_set_fails_safely():
    intent = ShoppingIntent(
        raw_prompt="some shampoo", category="shampoo", state="shopping_intent"
    )
    ordinal = resolve_product_reference("the first one", intent)
    assert ordinal.state == NOT_FOUND and ordinal.product is None
    pointer = resolve_product_reference("this one", intent)
    assert pointer.state == AMBIGUOUS and pointer.product is None


def test_scenario28_stale_result_sets_are_never_used():
    stale = [_product("p_loreal"), _product("p_dove"), _product("p_premium")]
    fresh = [_product("p_only")]
    intent = _intent_with_set(stale)
    record_search_set(intent, fresh, "shampoo gentle")
    assert intent.active_result_set_id == intent.result_sets[-1].set_id
    # Ordinals count the active set only — never the union of history.
    assert resolve_product_reference("the second one", intent).state == NOT_FOUND
    # An anchor into the stale set resolves to nothing.
    history = [_anchor("p_dove", ANCHOR_KIND_RESOLVED, sequence=3)]
    assert resolve_product_reference("that one", intent, history).state == NOT_FOUND
    assert active_result_products(intent) == fresh


def test_scenario29_an_arbitrary_submitted_product_id_cannot_mint_identity(products):
    intent = _intent_with_set(products)
    ref = resolve_product_reference("give me product_id intruder_99", intent)
    assert ref.product is None
    known = {p.product_id for p in products}
    assert ref.product is None or ref.product.product_id in known
    # A selection-style message is not a resolver phrase at all: selection stays a capability.
    assert resolve_product_reference("select item_dove", intent).state == NO_REFERENCE


def test_scenario30_a_price_in_the_message_cannot_influence_resolution(products):
    intent = _intent_with_set(products)
    # Ranking reads the persisted rows; a stated number changes nothing.
    ref = resolve_product_reference("the cheapest under 50", intent)
    assert ref.state == RESOLVED and ref.product.product_id == "p_dove"
    assert "verified price" in (ref.reason or "")
    # A stated price that matches no product resolves to nothing.
    ref = resolve_product_reference("the one priced 39900", intent)
    assert ref.product is None and ref.state == AMBIGUOUS


def test_scenario31_the_canonical_product_is_returned_untouched(products):
    intent = _intent_with_set(products)
    ref = resolve_product_reference("the second one", intent)
    assert ref.product is products[1]
    assert ref.product.model_dump(mode="json") == products[1].model_dump(mode="json")
    assert ref.position == 2 and ref.set_id == _active_set_id(intent)


def test_resolution_is_deterministic(products):
    intent = _intent_with_set(products)
    messages = ("the second one", "the cheapest", "this one", "the one from Nykaa")
    history = [_anchor("p_dove", sequence=1)]
    for message in messages:
        assert resolve_product_reference(message, intent, history) == resolve_product_reference(
            message, intent, history
        )


def test_resolution_never_mutates_the_intent(products):
    intent = _intent_with_set(products)
    before = intent.model_dump(mode="json")
    for message in ("the second one", "the cheapest", "this one", "the one from Nykaa", "number 9"):
        resolve_product_reference(message, intent, [_anchor("p_dove", sequence=1)])
    assert intent.model_dump(mode="json") == before


def test_a_mission_without_a_shopping_intent_has_no_reference():
    ref = resolve_product_reference("the first one", None)
    assert ref.state == NO_REFERENCE and ref.product is None and ref.position is None


def test_empty_active_set_fails_safely():
    intent = ShoppingIntent(raw_prompt="shampoo", state="discovery_completed")
    record_search_set(intent, [], "shampoo rare")
    ref = resolve_product_reference("the first one", intent)
    assert ref.state == NOT_FOUND and ref.product is None


# --------------------------------------------------------------------------- #
# The module itself: pure, incapable of I/O and execution (scenarios 32-35)
# --------------------------------------------------------------------------- #


def test_resolver_module_is_incapable_of_io_and_execution():
    """No network clients, no environment, no subprocess, no eval, and none of the surfaces a
    resolver must never touch — the module cannot reach a provider, a payment or a store."""
    banned = [
        "httpx", "aiohttp", "urllib", "requests", "socket", "subprocess", "eval(", "exec(",
        "os.environ", "getenv", "api_key", "apikey", "authorization", "bearer",
        "razorpay", "payment", "checkout", "refund", "create_order", "authority",
        "shopify", "serpapi", "gemini", "groq", "openai",
        "link_order_to_shopify_order", "order_linkage", "app.integrations",
        "provider_order_reference",
    ]
    low = REFERENCE_RESOLUTION_SOURCE.casefold()
    for token in banned:
        assert token not in low, token


# --------------------------------------------------------------------------- #
# The registry stays closed (scenario 36)
# --------------------------------------------------------------------------- #


def test_scenario36_registry_is_closed_with_the_one_deliberate_addition():
    assert set(tool_names()) == {
        "search_products",
        "clarify_variant",
        "select_product",
        "recommend_product",
        "prepare_checkout",
        "report_issue",
        "assess_issue",
        "resolve_issue",
        "request_more_information",
        "escalate_to_human",
        # Buyer-flow Phase 3 §7: the mandated read-only purchase-eligibility check.
        "check_purchase_eligibility",
    }
    declared = not set(tool_names()) & {
        "create_payment",
        "confirm_payment",
        "confirm_checkout",
        "create_order",
        "delegate_authority",
        "shopify_mutation",
        "lookup_product_by_id",
    }
    assert declared


def test_phase3_and_phase4_invariants_still_hold(products):
    assert len(CONSTRAINT_FIELDS) == 11 and "budget" in CONSTRAINT_FIELDS
    intent = _intent_with_set(products)
    plan = plan_search(intent)
    assert plan.action == "reuse" and plan.filters == ()


# --------------------------------------------------------------------------- #
# Integration: the resolver inside the agent turn (scenarios 37-38 + the loop)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_resolved_ordinal_reaches_the_model_and_persists_an_anchor(
    async_client, monkeypatch, products
):
    holders = _install_counting_discovery(monkeypatch, sample_results=products)
    mission_id = await _create_shampoo_mission(async_client)
    assert _provider_calls(holders) == 1  # the creation search, and nothing else

    data = await _run_agent_turn(
        async_client,
        mission_id,
        "I'll take the second one",
        [AgentStepDecision.respond("The Dove Hair Fall Shampoo, then.")],
    )
    assert data["mission_status"] == "discovery_completed"
    assert _provider_calls(holders) == 1  # resolution consumed no provider calls

    conversation = await async_client.get(f"/missions/{mission_id}/agent/conversation")
    events = conversation.json()["events"]
    anchors = [event for event in events if event["event_type"] == ANCHOR_EVENT_TYPE]
    assert len(anchors) == 1
    payload = anchors[0]["payload"]
    assert payload["kind"] == ANCHOR_KIND_RESOLVED
    assert payload["product_id"] == "p_dove"
    assert payload["position"] == 2
    assert payload["set_id"] == data["shopping_intent"]["active_result_set_id"]
    sequences = [event["sequence"] for event in events]
    assert sequences == list(range(len(sequences)))
    # Nothing was selected or checked out by a resolution — identity only.
    assert data["shopping_intent"]["selected_product"] is None


@pytest.mark.asyncio
async def test_the_model_is_shown_what_the_server_resolved(
    async_client, monkeypatch, products
):
    holders = _install_counting_discovery(monkeypatch, sample_results=products)
    mission_id = await _create_shampoo_mission(async_client)

    scripted = _ScriptedAgent([AgentStepDecision.respond("Noted.")])
    monkeypatch.setattr(missions_module, "get_reasoning_provider", lambda *a, **k: scripted)
    resp = await async_client.post(
        f"/missions/{mission_id}/agent", json={"user_input": "the second one please"}
    )
    assert resp.status_code == 200

    summary = scripted.contexts[-1]["product_reference"]
    assert summary["state"] == RESOLVED
    assert summary["product"]["product_id"] == "p_dove"
    assert summary["position"] == 2
    assert summary["note"]  # the model is told this identity is the server's, not its own


@pytest.mark.asyncio
async def test_no_reference_leaves_the_context_without_one(async_client, monkeypatch, products):
    holders = _install_counting_discovery(monkeypatch, sample_results=products)
    mission_id = await _create_shampoo_mission(async_client)

    scripted = _ScriptedAgent([AgentStepDecision.respond("Happy to help!")])
    monkeypatch.setattr(missions_module, "get_reasoning_provider", lambda *a, **k: scripted)
    resp = await async_client.post(
        f"/missions/{mission_id}/agent", json={"user_input": "thank you"}
    )
    assert resp.status_code == 200
    assert scripted.contexts[-1]["product_reference"] is None


@pytest.mark.asyncio
async def test_ambiguity_reaches_the_model_with_candidates_and_a_question(
    async_client, monkeypatch, products
):
    holders = _install_counting_discovery(monkeypatch, sample_results=products)
    mission_id = await _create_shampoo_mission(async_client)

    scripted = _ScriptedAgent([AgentStepDecision.respond("Which shampoo?")])
    monkeypatch.setattr(missions_module, "get_reasoning_provider", lambda *a, **k: scripted)
    resp = await async_client.post(
        f"/missions/{mission_id}/agent", json={"user_input": "the shampoo product"}
    )
    assert resp.status_code == 200

    summary = scripted.contexts[-1]["product_reference"]
    assert summary["state"] == AMBIGUOUS
    # The mission's active set is the two products under its ₹600 budget.
    assert len(summary["other_candidates"]) == 2
    assert {p["product_id"] for p in summary["other_candidates"]} == {"p_loreal", "p_dove"}
    assert summary["question"]


@pytest.mark.asyncio
async def test_unsupported_comparison_reaches_the_model_honestly(
    async_client, monkeypatch, products
):
    holders = _install_counting_discovery(monkeypatch, sample_results=products)
    mission_id = await _create_shampoo_mission(async_client)

    scripted = _ScriptedAgent([AgentStepDecision.respond("Let me search cheaper options.")])
    monkeypatch.setattr(missions_module, "get_reasoning_provider", lambda *a, **k: scripted)
    resp = await async_client.post(
        f"/missions/{mission_id}/agent", json={"user_input": "show me cheaper ones"}
    )
    assert resp.status_code == 200
    summary = scripted.contexts[-1]["product_reference"]
    assert summary["state"] == UNSUPPORTED
    assert "cheaper" in summary["expression"]
    assert resp.json()["mission_status"] == "discovery_completed"


@pytest.mark.asyncio
async def test_pointer_without_history_asks_the_buyer(async_client, monkeypatch, products):
    holders = _install_counting_discovery(monkeypatch, sample_results=products)
    mission_id = await _create_shampoo_mission(async_client)

    scripted = _ScriptedAgent([AgentStepDecision.respond("Which one do you mean?")])
    monkeypatch.setattr(missions_module, "get_reasoning_provider", lambda *a, **k: scripted)
    resp = await async_client.post(
        f"/missions/{mission_id}/agent", json={"user_input": "this one"}
    )
    assert resp.status_code == 200
    summary = scripted.contexts[-1]["product_reference"]
    assert summary["state"] == AMBIGUOUS and summary["question"]


@pytest.mark.asyncio
async def test_recommend_then_the_one_you_recommended_end_to_end(
    async_client, monkeypatch, products
):
    holders = _install_counting_discovery(monkeypatch, sample_results=products)
    mission_id = await _create_shampoo_mission(async_client)
    assert _provider_calls(holders) == 1

    data = await _run_agent_turn(
        async_client,
        mission_id,
        "which do you recommend?",
        [
            AgentStepDecision.tool_call("recommend_product", {"product_id": "p_dove"}),
            AgentStepDecision.respond("I would go with the Dove Hair Fall Shampoo."),
        ],
    )
    assert data["mission_status"] == "discovery_completed"
    assert any(
        entry["tool"] == "recommend_product" and entry["success"] is True
        for entry in data["agent_tools"]
    )

    conversation = await async_client.get(f"/missions/{mission_id}/agent/conversation")
    anchors = [
        event
        for event in conversation.json()["events"]
        if event["event_type"] == ANCHOR_EVENT_TYPE
    ]
    assert [a["payload"]["kind"] for a in anchors] == [ANCHOR_KIND_RECOMMENDATION]

    # The next turn: the recorded anchor is what answers the phrase — no search, no model counting.
    scripted = _ScriptedAgent([AgentStepDecision.respond("The Dove, as recommended.")])
    monkeypatch.setattr(missions_module, "get_reasoning_provider", lambda *a, **k: scripted)
    resp = await async_client.post(
        f"/missions/{mission_id}/agent", json={"user_input": "the one you recommended"}
    )
    assert resp.status_code == 200
    assert _provider_calls(holders) == 1  # still only the creation search

    summary = scripted.contexts[-1]["product_reference"]
    assert summary["state"] == RESOLVED and summary["product"]["product_id"] == "p_dove"
    assert summary["position"] == 2

    conversation = await async_client.get(f"/missions/{mission_id}/agent/conversation")
    anchors = [
        event
        for event in conversation.json()["events"]
        if event["event_type"] == ANCHOR_EVENT_TYPE
    ]
    assert [a["payload"]["kind"] for a in anchors] == [
        ANCHOR_KIND_RECOMMENDATION,
        ANCHOR_KIND_RESOLVED,
    ]


@pytest.mark.asyncio
async def test_recommend_product_refuses_a_product_outside_the_active_set(
    async_client, monkeypatch, products
):
    holders = _install_counting_discovery(monkeypatch, sample_results=products)
    mission_id = await _create_shampoo_mission(async_client)

    data = await _run_agent_turn(
        async_client,
        mission_id,
        "can you recommend something?",
        [
            AgentStepDecision.tool_call("recommend_product", {"product_id": "intruder_99"}),
            AgentStepDecision.respond("I cannot recommend that one."),
        ],
    )
    failed = [entry for entry in data["agent_tools"] if entry["tool"] == "recommend_product"]
    assert failed and failed[0]["success"] is False
    assert failed[0]["error_code"] == "not_in_current_results"

    conversation = await async_client.get(f"/missions/{mission_id}/agent/conversation")
    assert not [
        event
        for event in conversation.json()["events"]
        if event["event_type"] == ANCHOR_EVENT_TYPE
    ]


@pytest.mark.asyncio
async def test_recommend_product_without_an_active_set_fails_honestly(
    async_client, monkeypatch, products
):
    _install_counting_discovery(monkeypatch, sample_results=products, error=RuntimeError("down"))
    resp = await async_client.post(
        "/missions/shopping",
        json={"prompt": "I want to order shampoo under ₹600 for anti-hairfall."},
    )
    mission_id = resp.json()["mission_id"]
    assert resp.json()["status"] == "ready_for_discovery"

    data = await _run_agent_turn(
        async_client,
        mission_id,
        "which do you recommend?",
        [
            AgentStepDecision.tool_call("recommend_product", {"product_id": "p_dove"}),
            AgentStepDecision.respond("I have nothing to recommend from yet."),
        ],
    )
    failed = [entry for entry in data["agent_tools"] if entry["tool"] == "recommend_product"]
    assert failed and failed[0]["success"] is False
    assert failed[0]["error_code"] == "no_active_results"
