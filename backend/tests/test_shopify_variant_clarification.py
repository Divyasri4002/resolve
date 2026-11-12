"""
Tests for Task 14C.7E — variant clarification, and the variant GID's journey into a return.

Two questions, answered against the real code with only the network stood in for:

  1. When Shopify's catalog says "this product has several variants and you have not told me which
     one", can RESOLVE ask about it — offering only the option values Shopify itself published — and
     can the buyer's answer make exactly one `ProductVariant` canonical, without RESOLVE or a
     reasoning model ever picking a variant on its own?

  2. When a return must be aimed, does the `ProductVariant` GID 14C.7D stored on the order row
     actually narrow the target, so a multi-line order resolves to the one shipped line it should —
     and fail closed rather than guess when it does not?

Nothing here creates a Shopify order, draft order or return. The return half runs against a stand-in
client that only implements the existing READ, and the clarification half runs against a stand-in
provider, so no Shopify document is sent at all.
"""
from __future__ import annotations

import ast
import inspect
import json
import pathlib
from decimal import Decimal

import pytest
from httpx import AsyncClient
from pydantic import ValidationError

from app.config import Settings
from app.models.db_models import ResolveOrderRecord
from app.models.mission import ShoppingIntent
from app.models.product import (
    SHOPIFY_SOURCE,
    ProductResult,
    VariantClarification,
    VariantOption,
    shopify_identity_problem,
)
from app.routers import missions as missions_module
from app.routers.missions import ClarifyVariantRequest
from app.tools.product_discovery import ProductDiscoveryTool
from app.tools.product_search.base import ProductSearchError
from app.tools.product_search.shopify_provider import ShopifyProductSearchProvider
from app.tools.returns import get_return_provider
from app.tools.returns.base import (
    ReturnError,
    ReturnProviderUnavailable,
    ReturnTarget,
)
from app.tools.shopify.client import _RETURN_TARGETS_QUERY
from app.tools.shopify.errors import ShopifyError
from app.tools.shopify.models import (
    ShopifyCatalog,
    ShopifyCatalogProduct,
    ShopifyCatalogVariant,
    ShopifyOrderLineRef,
    ShopifyOrderReturnTargets,
    ShopifyShippedFulfillmentLine,
)
from app.tools.shopify.return_provider import ShopifyReturnProvider, ShopifyReturnRequest
from app.tools.shopify.sanitizer import reset_registered_secrets

from tests.test_refund_execution import (  # noqa: F401  (fixtures re-used by name)
    _checkout_signature,
    _register,
    mock_razorpay,
    restart_env,
)

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
STORE = "resolve-commerce-lab.myshopify.com"
PRODUCT_GID = "gid://shopify/Product/10622982422815"
PRODUCT_GID_OTHER = "gid://shopify/Product/10622982717727"
VARIANT_GID = "gid://shopify/ProductVariant/52923834990879"
VARIANT_GID_OTHER = "gid://shopify/ProductVariant/52923835973919"
GOOGLE_PRODUCT_ID = "1462412133584222299"
ORDER_GID = "gid://shopify/Order/7990366601503"
LINE_ITEM_GID = "gid://shopify/LineItem/18801909727519"
LINE_ITEM_GID_OTHER = "gid://shopify/LineItem/18801909990000"
FULFILLMENT_GID = "gid://shopify/Fulfillment/7166055481631"
FULFILLMENT_LINE_GID = "gid://shopify/FulfillmentLineItem/16492059328799"
FULFILLMENT_LINE_GID_OTHER = "gid://shopify/FulfillmentLineItem/16492059328800"
FULFILLMENT_ORDER_LINE_GID = "gid://shopify/FulfillmentOrderLineItem/18989131727135"
RETURN_GID = "gid://shopify/Return/9988776655440"
PROMPT = "I want the shampoo from my own store under ₹600."


@pytest.fixture(autouse=True)
def _clean_secrets():
    reset_registered_secrets()
    yield
    reset_registered_secrets()


# --------------------------------------------------------------------------- #
# Stand-ins
# --------------------------------------------------------------------------- #
def _variant(
    gid: str,
    *,
    options: tuple[tuple[str, str], ...] = (("Size", "360ml"),),
    price: str = "410.00",
    available: bool = True,
) -> ShopifyCatalogVariant:
    return ShopifyCatalogVariant(
        variant_id=gid,
        product_id=PRODUCT_GID,
        title="Head & Shoulders Anti-Hairfall Shampoo",
        handle="head-shoulders",
        status="ACTIVE",
        sku=None,
        price=Decimal(price),
        currency="USD",
        available_for_sale=available,
        selected_options=options,
        image_url=None,
    )


def _product(
    *variants: ShopifyCatalogVariant,
    product_id: str = PRODUCT_GID,
    title: str = "Head & Shoulders Anti-Hairfall Shampoo",
) -> ShopifyCatalogProduct:
    return ShopifyCatalogProduct(
        product_id=product_id,
        title=title,
        handle="head-shoulders",
        status="ACTIVE",
        variants=tuple(variants),
    )


def _catalog(*products: ShopifyCatalogProduct) -> ShopifyCatalog:
    return ShopifyCatalog(
        store_domain=STORE,
        shop_name="RESOLVE Commerce Lab",
        currency="USD",
        products=tuple(products),
    )


class CatalogClient:
    """Stands where the read-only Shopify client stands; implements the catalog read and nothing else."""

    def __init__(self, catalog: ShopifyCatalog):
        self.catalog = catalog
        self.asks: list[dict] = []

    async def read_catalog(self, **kwargs) -> ShopifyCatalog:
        self.asks.append(kwargs)
        return self.catalog


async def _search(catalog: ShopifyCatalog, **kwargs) -> list[ProductResult]:
    provider = ShopifyProductSearchProvider(client=CatalogClient(catalog))
    return await provider.search("shampoo", **kwargs)


# --------------------------------------------------------------------------- #
# A / B / C — an unresolved variant carries Shopify's own question
# --------------------------------------------------------------------------- #
async def test_case_a_multiple_valid_variants_require_clarification():
    results = await _search(
        _catalog(
            _product(
                _variant(VARIANT_GID),
                _variant(VARIANT_GID_OTHER, options=(("Size", "750ml"),)),
            )
        )
    )
    [result] = results
    assert result.requires_variant_clarification is True
    assert result.shopify_variant_gid is None
    assert result.shopify_product_gid == PRODUCT_GID  # the product itself is still known
    assert result.variant_clarification is not None


async def test_case_b_clarification_offers_only_shopifs_own_option_values():
    results = await _search(
        _catalog(
            _product(
                _variant(VARIANT_GID, options=(("Size", "360ml"), ("Scent", "Fresh"))),
                _variant(VARIANT_GID_OTHER, options=(("Size", "750ml"), ("Scent", "Mint"))),
            )
        )
    )
    clarification = results[0].variant_clarification
    assert clarification.option_names == ["Size", "Scent"]
    assert [(o.name, o.value) for o in clarification.options] == [
        ("Size", "360ml"),
        ("Scent", "Fresh"),
        ("Size", "750ml"),
        ("Scent", "Mint"),
    ]
    assert "Which Size or Scent do you want?" in clarification.question
    # Nothing but Shopify's own option data can appear: no price, no title, no currency, no id.
    rendered = json.dumps(clarification.model_dump(mode="json"))
    for forbidden in ("410", "699", "Head & Shoulders", "USD", "gid://"):
        assert forbidden not in rendered


async def test_only_the_dimension_that_actually_differs_is_asked_about():
    results = await _search(
        _catalog(
            _product(
                _variant(VARIANT_GID, options=(("Size", "360ml"), ("Scent", "Fresh"))),
                _variant(VARIANT_GID_OTHER, options=(("Size", "360ml"), ("Scent", "Mint"))),
            )
        )
    )
    clarification = results[0].variant_clarification
    # Size is identical on both candidates, so asking about it would answer nothing.
    assert clarification.option_names == ["Scent"]
    assert [(o.name, o.value) for o in clarification.options] == [
        ("Size", "360ml"),
        ("Scent", "Fresh"),
        ("Scent", "Mint"),
    ]


async def test_an_option_value_shared_by_every_candidate_is_offered_once():
    results = await _search(
        _catalog(
            _product(
                _variant(VARIANT_GID, options=(("Title", "Default Title"), ("Size", "360ml"))),
                _variant(VARIANT_GID_OTHER, options=(("Title", "Default Title"), ("Size", "750ml"))),
            )
        )
    )
    pairs = [(o.name, o.value) for o in results[0].variant_clarification.options]
    assert pairs.count(("Title", "Default Title")) == 1
    assert results[0].variant_clarification.option_names == ["Size"]


async def test_a_product_with_one_available_variant_carries_no_clarification():
    [result] = await _search(_catalog(_product(_variant(VARIANT_GID))))
    assert result.variant_clarification is None
    assert result.requires_variant_clarification is False
    assert result.shopify_variant_gid == VARIANT_GID


async def test_a_product_with_no_available_variant_offers_nothing_to_choose():
    [result] = await _search(_catalog(_product(_variant(VARIANT_GID, available=False))))
    clarification = result.variant_clarification
    assert clarification is not None
    assert clarification.options == []
    assert clarification.option_names == []
    assert "no available variant" in clarification.question
    assert result.availability == "Out of stock"


async def test_a_value_the_buyer_asked_for_is_never_echoed_back_as_an_option():
    # Asked for 1L; Shopify publishes 360ml and 750ml. The offer must be Shopify's, not the request's.
    [result] = await _search(
        _catalog(
            _product(
                _variant(VARIANT_GID, options=(("Size", "360ml"),)),
                _variant(VARIANT_GID_OTHER, options=(("Size", "750ml"),)),
            )
        ),
        size_or_variant="1L",
    )
    assert [(o.name, o.value) for o in result.variant_clarification.options] == [
        ("Size", "360ml"),
        ("Size", "750ml"),
    ]


# --------------------------------------------------------------------------- #
# D / E / F / G — Shopify resolves the answer, RESOLVE does not
# --------------------------------------------------------------------------- #
async def test_case_d_an_answer_that_names_one_variant_makes_that_variant_canonical():
    catalog = _catalog(
        _product(
            _variant(VARIANT_GID, options=(("Size", "360ml"),)),
            _variant(VARIANT_GID_OTHER, options=(("Size", "750ml"),), price="699.00"),
        )
    )
    assert (await _search(catalog))[0].requires_variant_clarification is True

    resolved = (await _search(catalog, size_or_variant="750ml"))[0]
    assert resolved.shopify_variant_gid == VARIANT_GID_OTHER
    assert resolved.requires_variant_clarification is False
    assert resolved.variant_clarification is None
    assert resolved.extracted_price == Decimal("699.00")  # Shopify's price for THAT variant
    assert shopify_identity_problem(resolved) is None


async def test_case_e_an_answer_naming_an_unavailable_variant_substitutes_nothing():
    catalog = _catalog(
        _product(
            _variant(VARIANT_GID, options=(("Size", "360ml"),), available=False),
            _variant(VARIANT_GID_OTHER, options=(("Size", "750ml"),)),
        )
    )
    [result] = await _search(catalog, size_or_variant="360ml")
    assert result.shopify_variant_gid is None
    assert result.requires_variant_clarification is True
    assert any("nothing is substituted" in note for note in result.extensions)
    # The 360ml the buyer asked for is not offered back as if it were available.
    assert [(o.name, o.value) for o in result.variant_clarification.options] == [("Size", "750ml")]


async def test_case_f_a_value_shopify_never_published_selects_nothing():
    catalog = _catalog(
        _product(
            _variant(VARIANT_GID, options=(("Size", "360ml"),)),
            _variant(VARIANT_GID_OTHER, options=(("Size", "750ml"),)),
        )
    )
    [result] = await _search(catalog, size_or_variant="two litres")
    assert result.shopify_variant_gid is None
    assert result.requires_variant_clarification is True
    assert "no available Shopify variant matches the requested variant" in " ".join(
        result.extensions
    )


async def test_case_g_an_answer_that_still_matches_two_variants_stays_ambiguous():
    catalog = _catalog(
        _product(
            _variant(VARIANT_GID, options=(("Size", "360ml"), ("Scent", "Fresh"))),
            _variant(VARIANT_GID_OTHER, options=(("Size", "360ml"), ("Scent", "Mint"))),
        )
    )
    [first] = await _search(catalog, size_or_variant="360ml")
    assert first.shopify_variant_gid is None
    assert first.variant_clarification.option_names == ["Scent"]

    [second] = await _search(catalog, size_or_variant="360ml fresh")
    assert second.shopify_variant_gid is None  # "360ml fresh" is one token, not two answers
    assert second.requires_variant_clarification is True


async def test_an_answer_never_becomes_part_of_a_variant_gid():
    """
    The buyer's wording is a selector, not an identifier. `shopify_variant_gid` is only ever a GID
    Shopify returned, never a string RESOLVE assembled around what the buyer typed.
    """
    catalog = _catalog(
        _product(
            _variant(VARIANT_GID),
            _variant(VARIANT_GID_OTHER, options=(("Size", "750ml"),)),
        )
    )
    [result] = await _search(catalog, size_or_variant="750ml")
    assert result.shopify_variant_gid == VARIANT_GID_OTHER
    assert "750ml" not in result.shopify_variant_gid
    provider_source = (
        BACKEND_ROOT / "app/tools/product_search/shopify_provider.py"
    ).read_text(encoding="utf-8")
    assert 'f"gid://' not in provider_source
    assert "gid://shopify/" not in provider_source  # the literal never appears at all


# --------------------------------------------------------------------------- #
# H / I / J — the two identity systems still cannot impersonate each other
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "variant_gid,expected_fragment",
    [
        (PRODUCT_GID, "not a canonical Shopify variant global id"),
        (GOOGLE_PRODUCT_ID, "not a canonical Shopify variant global id"),
        (ORDER_GID, "not a canonical Shopify variant global id"),
        ("gid://shopify/ProductVariant/", "not a canonical Shopify variant global id"),
    ],
)
def test_case_h_a_product_gid_is_never_accepted_in_a_variant_slot(variant_gid, expected_fragment):
    result = ProductResult(
        title="T",
        source=SHOPIFY_SOURCE,
        shopify_product_gid=PRODUCT_GID,
        shopify_variant_gid=variant_gid,
        shopify_store_domain=STORE,
        variant_clarification=VariantClarification(
            question="Which Size do you want?",
            option_names=["Size"],
            options=[VariantOption(name="Size", value="360ml")],
        ),
    )
    assert expected_fragment in shopify_identity_problem(result)


def test_case_i_a_serpapi_result_carries_no_clarification_and_no_identity():
    from app.tools.product_search.serpapi_provider import SerpApiProductSearchProvider

    result = SerpApiProductSearchProvider(api_key="test-key").normalize_item(
        {"title": "Head & Shoulders Anti-hairfall Shampoo 360ml", "product_id": GOOGLE_PRODUCT_ID}
    )
    assert result.requires_variant_clarification is False
    assert result.variant_clarification is None
    assert result.shopify_product_gid is None
    assert shopify_identity_problem(result) is None


def test_a_non_shopify_result_cannot_carry_a_clarification():
    forged = ProductResult(
        title="T",
        source="google_shopping",
        variant_clarification=VariantClarification(
            question="Which Size?", option_names=["Size"], options=[]
        ),
    )
    problem = shopify_identity_problem(forged)
    assert "non-Shopify result" in problem and "variant_clarification" in problem


def test_the_clarification_fields_are_additive_and_default_to_absent():
    fields = ProductResult.model_fields
    assert fields["variant_clarification"].default is None
    assert fields["requires_variant_clarification"].default is False
    for name in (
        "product_id", "title", "price", "extracted_price", "currency", "merchant",
        "shopify_product_gid", "shopify_variant_gid", "shopify_store_domain", "source",
        "extensions",
    ):
        assert name in fields
    # A clarification with no dimensions may still be a question about what is available; one with
    # dimensions must carry Shopify's values for them.
    with pytest.raises(ValidationError):
        VariantOption(name="Size", value="")


# --------------------------------------------------------------------------- #
# The round-trip over HTTP
# --------------------------------------------------------------------------- #
def _ambiguous(**overrides) -> ProductResult:
    base = {
        "product_id": PRODUCT_GID,
        "title": "Head & Shoulders Anti-Hairfall Shampoo",
        "price": "USD 410.00",
        "extracted_price": Decimal("410.00"),
        "currency": "USD",
        "merchant": "RESOLVE Commerce Lab",
        "source": SHOPIFY_SOURCE,
        "shopify_product_gid": PRODUCT_GID,
        "shopify_variant_gid": None,
        "shopify_store_domain": STORE,
        "requires_variant_clarification": True,
        "variant_clarification": VariantClarification(
            question="Which Size do you want?",
            option_names=["Size"],
            options=[
                VariantOption(name="Size", value="360ml"),
                VariantOption(name="Size", value="750ml"),
            ],
        ),
    }
    base.update(overrides)
    return ProductResult(**base)


def _resolved(answer: str) -> ProductResult:
    gid, price = (VARIANT_GID, "410.00") if answer == "360ml" else (VARIANT_GID_OTHER, "699.00")
    return _ambiguous(
        requires_variant_clarification=False,
        variant_clarification=None,
        shopify_variant_gid=gid,
        extracted_price=Decimal(price),
        price=f"USD {price}",
        extensions=[f"Size: {answer}"],
    )


class SequencedProvider:
    """
    Stands where the configured provider stands. It answers the way Shopify would once the buyer has
    named a variant, records every question it was asked, and knows nothing about identity beyond
    copying what its own catalog said.
    """

    name = SHOPIFY_SOURCE

    def __init__(self, *, resolves_on: str | None = "360ml", results=None, error=None):
        self.resolves_on = resolves_on
        self.results = results
        self.error = error
        self.calls: list[dict] = []

    async def search(self, query: str, **kwargs) -> list[ProductResult]:
        self.calls.append({"query": query, **kwargs})
        if self.error is not None:
            raise self.error
        if self.results is not None:
            return list(self.results)
        answer = kwargs.get("size_or_variant")
        if self.resolves_on is not None and answer == self.resolves_on:
            return [_resolved(answer)]
        return [_ambiguous()]


@pytest.fixture
def install_sequenced_provider(monkeypatch):
    def _install(provider: SequencedProvider) -> SequencedProvider:
        monkeypatch.setattr(
            missions_module,
            "get_product_discovery_tool",
            lambda: ProductDiscoveryTool(provider=provider),
        )
        return provider

    return _install


async def _shopping_mission(client: AsyncClient, email: str) -> str:
    await _register(client, "Shopify Shopper", email)
    response = await client.post("/missions/shopping", json={"prompt": PROMPT})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "discovery_completed", body
    return body["mission_id"]


async def _clarify(client: AsyncClient, mission_id: str, answer: str, product_id: str = PRODUCT_GID):
    return await client.post(
        f"/missions/{mission_id}/shopping/clarify-variant",
        json={"product_id": product_id, "answer": answer},
    )


async def test_mission_creation_persists_the_question_the_provider_was_asked(
    restart_env, install_sequenced_provider
):
    make_client, _test_session = restart_env
    provider = install_sequenced_provider(SequencedProvider(resolves_on=None))
    async with make_client() as client:
        mission_id = await _shopping_mission(client, "dq@shop.test")
        intent = (await client.get(f"/missions/{mission_id}")).json()["shopping_intent"]
    assert intent["discovery_query"] == provider.calls[0]["query"]
    assert intent["discovered_products"][0]["requires_variant_clarification"] is True


async def test_clarification_re_runs_the_same_discovery_path_with_the_buyers_answer(
    restart_env, install_sequenced_provider
):
    make_client, _test_session = restart_env
    provider = install_sequenced_provider(SequencedProvider(resolves_on="360ml"))
    async with make_client() as client:
        mission_id = await _shopping_mission(client, "rt@shop.test")
        response = await _clarify(client, mission_id, "360ml")
        assert response.status_code == 200, response.text
        body = response.json()

        assert body["resolved"] is True
        assert body["requires_variant_clarification"] is False
        assert body["variant_clarification"] is None
        assert provider.calls[1]["query"] == provider.calls[0]["query"]
        assert provider.calls[0]["size_or_variant"] is None
        assert provider.calls[1]["size_or_variant"] == "360ml"
        assert body["shopping_intent"]["size_or_variant"] == "360ml"
        assert [
            p["shopify_variant_gid"] for p in body["shopping_intent"]["discovered_products"]
        ] == [VARIANT_GID]
        # The buyer's answer never reached the provider as a GID, and never became one.
        assert VARIANT_GID not in json.dumps(provider.calls[1], default=str)


async def test_a_clarified_product_becomes_selectable_and_keeps_both_shopify_ids(
    restart_env, install_sequenced_provider, mock_razorpay
):
    make_client, _test_session = restart_env
    install_sequenced_provider(SequencedProvider(resolves_on="360ml"))
    async with make_client() as client:
        mission_id = await _shopping_mission(client, "e2e@shop.test")
        # Before the answer: discovery found it, selection refuses it.
        refused = await client.post(
            f"/missions/{mission_id}/shopping/select-product", json={"product_id": PRODUCT_GID}
        )
        assert refused.status_code == 400
        assert "no single Shopify variant has been identified" in refused.json()["detail"]

        assert (await _clarify(client, mission_id, "360ml")).status_code == 200
        selected = await client.post(
            f"/missions/{mission_id}/shopping/select-product", json={"product_id": PRODUCT_GID}
        )
        assert selected.status_code == 200, selected.text
        product = selected.json()["selected_product"]
        assert product["shopify_product_gid"] == PRODUCT_GID
        assert product["shopify_variant_gid"] == VARIANT_GID

        checkout = await client.get(f"/missions/{mission_id}/shopping/checkout")
        assert checkout.status_code == 200
        snap = checkout.json()["checkout"]["product"]
        assert snap["shopify_variant_gid"] == VARIANT_GID
        assert snap["shopify_product_gid"] == PRODUCT_GID

        # Payment still refuses the store's USD instead of converting it: the 14C.7D boundary is
        # untouched by a clarification round-trip.
        assert (
            await client.post(f"/missions/{mission_id}/shopping/checkout/confirm")
        ).status_code == 200
        created = await client.post(
            f"/missions/{mission_id}/shopping/payment/create-order", json={}
        )
        assert created.status_code == 400
        assert "does not convert" in created.json()["detail"]
        assert mock_razorpay["n"] == 0


async def test_clarification_stays_unresolved_when_the_answer_matches_nothing(
    restart_env, install_sequenced_provider
):
    make_client, _test_session = restart_env
    install_sequenced_provider(SequencedProvider(resolves_on="360ml"))
    async with make_client() as client:
        mission_id = await _shopping_mission(client, "unres@shop.test")
        response = await _clarify(client, mission_id, "two litres")
        body = response.json()
        assert response.status_code == 200
        assert body["resolved"] is False
        assert body["requires_variant_clarification"] is True
        assert [
            option["value"] for option in body["variant_clarification"]["options"]
        ] == ["360ml", "750ml"]
        assert "two litres" not in json.dumps(body["variant_clarification"])
        # Selection is still refused, and the mission has not advanced.
        refused = await client.post(
            f"/missions/{mission_id}/shopping/select-product", json={"product_id": PRODUCT_GID}
        )
        assert refused.status_code == 400
        assert (await client.get(f"/missions/{mission_id}")).json()["status"] == "discovery_completed"


async def test_a_no_match_catalog_answer_leaves_the_buyers_list_intact(
    restart_env, install_sequenced_provider
):
    make_client, _test_session = restart_env
    install_sequenced_provider(SequencedProvider(resolves_on=None))
    async with make_client() as client:
        mission_id = await _shopping_mission(client, "empty@shop.test")
        # Now the catalog answers the narrowed question with nothing at all.
        install_sequenced_provider(SequencedProvider(results=[]))
        response = await _clarify(client, mission_id, "750ml")
        body = response.json()

    assert response.status_code == 200
    assert body["resolved"] is False
    # An empty answer from the catalog does not wipe the products the buyer was shown.
    assert [
        p["product_id"] for p in body["shopping_intent"]["discovered_products"]
    ] == [PRODUCT_GID]
    assert body["shopping_intent"]["discovered_products"][0]["requires_variant_clarification"] is True
    assert "no catalog result" in body["agent_message"]
    # The answer is still recorded as what the buyer wants, and nothing was invented for it.
    assert body["shopping_intent"]["size_or_variant"] == "750ml"


async def test_a_provider_failure_is_reported_and_invents_no_variant(
    restart_env, install_sequenced_provider
):
    make_client, _test_session = restart_env
    install_sequenced_provider(SequencedProvider(resolves_on=None))
    async with make_client() as client:
        mission_id = await _shopping_mission(client, "fail@shop.test")
        install_sequenced_provider(
            SequencedProvider(error=ProductSearchError("Shopify is unavailable", status_code=503))
        )
        response = await _clarify(client, mission_id, "360ml")
        assert response.status_code == 503
        assert "unavailable" in response.json()["detail"]
        intent = (await client.get(f"/missions/{mission_id}")).json()["shopping_intent"]
    # The failure left the mission exactly as discovery had: unresolved, and unselectable.
    assert intent["discovered_products"][0]["shopify_variant_gid"] is None
    assert intent.get("size_or_variant") != "360ml"


# --------------------------------------------------------------------------- #
# K / L — authority stays server-side across the new endpoint
# --------------------------------------------------------------------------- #
async def test_case_k_a_client_cannot_name_the_variant_it_wants(
    restart_env, install_sequenced_provider
):
    make_client, _test_session = restart_env
    provider = install_sequenced_provider(SequencedProvider(resolves_on="360ml"))
    async with make_client() as client:
        mission_id = await _shopping_mission(client, "ovr@shop.test")
        response = await client.post(
            f"/missions/{mission_id}/shopping/clarify-variant",
            json={
                "product_id": PRODUCT_GID,
                "answer": "360ml",
                "shopify_variant_gid": "gid://shopify/ProductVariant/9999999",
                "shopify_product_gid": "gid://shopify/Product/9999999",
                "shopify_store_domain": "attacker.example",
                "variant_clarification": {"question": "anything", "options": []},
                "price": "USD 0.01",
                "currency": "INR",
                "requires_variant_clarification": False,
            },
        )
        assert response.status_code == 200, response.text
        product = response.json()["shopping_intent"]["discovered_products"][0]
    assert product["shopify_variant_gid"] == VARIANT_GID  # what the provider said, not the body
    assert product["shopify_product_gid"] == PRODUCT_GID
    assert product["shopify_store_domain"] == STORE
    assert product["price"] == "USD 410.00"
    assert product["currency"] == "USD"
    # Only the product handle and the answer ever reached the provider, and the provider was handed
    # the discovery tool's own budget parameter (`max_price`), never a client-supplied identity.
    assert set(provider.calls[-1]) == {
        "query", "max_price", "currency", "preferences", "brand", "size_or_variant", "limit",
    }


async def test_a_serpapi_product_cannot_be_variant_clarified(
    restart_env, install_mock_discovery, sample_products
):
    make_client, _test_session = restart_env
    install_mock_discovery(sample_results=sample_products)
    async with make_client() as client:
        await _register(client, "Serp Shopper", "serp@shop.test")
        mission_id = (
            await client.post("/missions/shopping", json={"prompt": PROMPT})
        ).json()["mission_id"]
        seeded = (await client.get(f"/missions/{mission_id}")).json()["shopping_intent"]
        google_product_id = seeded["discovered_products"][0]["product_id"]
        response = await _clarify(client, mission_id, "360ml", product_id=google_product_id)
        assert response.status_code == 400
        assert "Shopify-catalog products" in response.json()["detail"]
        after = (await client.get(f"/missions/{mission_id}")).json()["shopping_intent"]
    # The refusal changed nothing at all: the SerpApi list and the intent are exactly as left.
    assert after == seeded
    assert all(p.get("variant_clarification") is None for p in after["discovered_products"])
    assert all(p.get("shopify_product_gid") is None for p in after["discovered_products"])
    assert after.get("size_or_variant") != "360ml"


async def test_clarification_refuses_a_product_that_is_not_on_this_mission(
    restart_env, install_sequenced_provider
):
    make_client, _test_session = restart_env
    provider = install_sequenced_provider(SequencedProvider())
    async with make_client() as client:
        mission_id = await _shopping_mission(client, "absent@shop.test")
        calls_before = len(provider.calls)
        response = await _clarify(
            client, mission_id, "360ml", product_id="gid://shopify/Product/404404404"
        )
        assert response.status_code == 400
        assert "not part of this mission" in response.json()["detail"]
    # An unknown handle never reaches the provider at all.
    assert len(provider.calls) == calls_before


async def test_clarification_refuses_an_already_resolved_or_already_selected_product(
    restart_env, install_sequenced_provider
):
    make_client, _test_session = restart_env
    provider = install_sequenced_provider(SequencedProvider(resolves_on="360ml"))
    async with make_client() as client:
        mission_id = await _shopping_mission(client, "twice@shop.test")
        assert (await _clarify(client, mission_id, "360ml")).status_code == 200

        second = await _clarify(client, mission_id, "750ml")
        assert second.status_code == 400
        assert "already identified one variant" in second.json()["detail"]

        await client.post(
            f"/missions/{mission_id}/shopping/select-product", json={"product_id": PRODUCT_GID}
        )
        after = await _clarify(client, mission_id, "750ml")
        assert after.status_code == 400
        assert "already selected" in after.json()["detail"]
    # Neither refusal re-asked the provider: the last call is the one that resolved it.
    assert provider.calls[-1]["size_or_variant"] == "360ml"
    assert len(provider.calls) == 2


async def test_a_gid_that_arrives_through_the_intent_never_becomes_identity(
    restart_env, install_sequenced_provider
):
    """
    Case J. Even a value that looks exactly like a Shopify GID — which is what a reasoning model
    could hallucinate — is only ever a selector to match against Shopify's option values. It cannot
    become the variant identity, because the identity comes from the provider's own catalog answer.
    """
    make_client, _test_session = restart_env
    provider = install_sequenced_provider(SequencedProvider(resolves_on=None))
    async with make_client() as client:
        mission_id = await _shopping_mission(client, "gem@shop.test")
        response = await _clarify(client, mission_id, VARIANT_GID_OTHER)
        body = response.json()
    assert body["resolved"] is False
    assert body["shopping_intent"]["discovered_products"][0]["shopify_variant_gid"] is None
    assert provider.calls[-1]["size_or_variant"] == VARIANT_GID_OTHER


async def test_checkout_still_derives_everything_server_side_after_clarification(
    restart_env, install_sequenced_provider
):
    make_client, _test_session = restart_env
    install_sequenced_provider(SequencedProvider(resolves_on="360ml"))
    async with make_client() as client:
        mission_id = await _shopping_mission(client, "chk@shop.test")
        await _clarify(client, mission_id, "360ml")
        await client.post(
            f"/missions/{mission_id}/shopping/select-product", json={"product_id": PRODUCT_GID}
        )
        review = await client.get(
            f"/missions/{mission_id}/shopping/checkout",
        )
        summary = review.json()["checkout"]
    assert summary["currency"] == "USD"  # the store's own currency, not the buyer's INR framing
    assert summary["subtotal"] == "410.00"
    assert summary["quantity"] == 1
    assert summary["product"]["shopify_variant_gid"] == VARIANT_GID


# --------------------------------------------------------------------------- #
# N/O/P/Q/R/S/T — the variant GID's role in return target resolution
# --------------------------------------------------------------------------- #
def _targets_payload(
    *,
    order_lines: list[tuple[str, str | None, str | None]],
    shipped: list[dict],
    return_status: str = "NO_RETURN",
) -> dict:
    """One `read_return_targets` answer, shaped the way Shopify answers it."""
    return {
        "order_id": ORDER_GID,
        "return_status": return_status,
        "order_lines": [
            ShopifyOrderLineRef(
                order_line_item_id=line_id, product_id=product_id, variant_id=variant_id
            )
            for line_id, product_id, variant_id in order_lines
        ],
        "shipped_lines": [
            ShopifyShippedFulfillmentLine(
                fulfillment_line_item_id=line["id"],
                quantity=line.get("quantity"),
                order_line_item_id=line.get("line_item_id", LINE_ITEM_GID),
                fulfillment_id=line.get("fulfillment_id", FULFILLMENT_GID),
                fulfillment_status=line.get("fulfillment_status", "SUCCESS"),
            )
            for line in shipped
        ],
    }


class TargetReadClient:
    """
    Stands where the read-only client stands. It implements the one read target resolution needs,
    and has no transport, no token and no mutation path at all — so a provider held against it could
    not submit anything even if it tried.
    """

    def __init__(self, payload: dict):
        self.targets = ShopifyOrderReturnTargets(**payload)
        self.reads: list[str] = []

    async def read_return_targets(self, order_id: str) -> ShopifyOrderReturnTargets:
        self.reads.append(order_id)
        return self.targets


def _provider(payload: dict) -> tuple[ShopifyReturnProvider, TargetReadClient]:
    client = TargetReadClient(payload)
    return ShopifyReturnProvider(client=client), client


def _request(**overrides) -> ShopifyReturnRequest:
    fields = {
        "mission_id": "mission_1",
        "issue_id": "issue_1",
        "order_id": "order_1",
        "return_id": "ret_1",
        "product_id": PRODUCT_GID,
        "title": "Head & Shoulders Anti-Hairfall Shampoo",
        "merchant": "RESOLVE Commerce Lab",
        "issue_type": "damaged",
        "provider_order_reference": ORDER_GID,
        "provider_product_reference": PRODUCT_GID,
        "provider_variant_reference": VARIANT_GID,
    }
    fields.update(overrides)
    return ShopifyReturnRequest(**fields)


_TWO_LINE = _targets_payload(
    order_lines=[
        (LINE_ITEM_GID, PRODUCT_GID, VARIANT_GID),
        (LINE_ITEM_GID_OTHER, PRODUCT_GID_OTHER, VARIANT_GID_OTHER),
    ],
    shipped=[
        {"id": FULFILLMENT_LINE_GID, "quantity": 1},
        {"id": FULFILLMENT_LINE_GID_OTHER, "quantity": 2, "line_item_id": LINE_ITEM_GID_OTHER},
    ],
)


async def test_case_n_the_persisted_variant_gid_picks_the_line_it_belongs_to():
    provider, client = _provider(_TWO_LINE)
    target = await provider.resolve_return_target(_request())
    assert isinstance(target, ReturnTarget)
    assert target.provider_order_reference == ORDER_GID
    assert target.provider_line_reference == FULFILLMENT_LINE_GID
    assert target.provider_quantity == 1
    assert client.reads == [ORDER_GID]


async def test_case_o_the_other_variant_on_the_same_order_picks_the_other_line():
    provider, _client = _provider(_TWO_LINE)
    target = await provider.resolve_return_target(
        _request(
            provider_product_reference=PRODUCT_GID_OTHER,
            provider_variant_reference=VARIANT_GID_OTHER,
        )
    )
    assert target.provider_line_reference == FULFILLMENT_LINE_GID_OTHER
    assert target.provider_quantity == 2  # what shipped on THAT line, not on the other one


async def test_the_variant_is_matched_through_shopifs_own_order_line_not_locally():
    # Shopify names the variant on the order line; a shipped line whose order line carries NO variant
    # cannot be matched to a variant reference, so it fails closed instead of falling back.
    payload = _targets_payload(
        order_lines=[(LINE_ITEM_GID, PRODUCT_GID, None)],
        shipped=[{"id": FULFILLMENT_LINE_GID, "quantity": 1}],
    )
    provider, _client = _provider(payload)
    with pytest.raises(ReturnProviderUnavailable) as excinfo:
        await provider.resolve_return_target(_request())
    assert "no shipped line" in str(excinfo.value)


async def test_case_p_an_identity_that_matches_nothing_never_falls_back_to_the_first_line():
    provider, _client = _provider(_TWO_LINE)
    with pytest.raises(ReturnProviderUnavailable) as excinfo:
        await provider.resolve_return_target(
            _request(provider_variant_reference="gid://shopify/ProductVariant/404404")
        )
    assert "no shipped line" in str(excinfo.value)


async def test_an_ambiguous_shipped_pair_still_fails_closed():
    # Two shipped lines for the SAME product and variant: nothing in canonical data separates them,
    # so the answer is a refusal, not a coin toss or "the newest".
    payload = _targets_payload(
        order_lines=[
            (LINE_ITEM_GID, PRODUCT_GID, VARIANT_GID),
            (LINE_ITEM_GID_OTHER, PRODUCT_GID, VARIANT_GID),
        ],
        shipped=[
            {"id": FULFILLMENT_LINE_GID, "quantity": 1, "line_item_id": LINE_ITEM_GID},
            {
                "id": FULFILLMENT_LINE_GID_OTHER,
                "quantity": 1,
                "line_item_id": LINE_ITEM_GID_OTHER,
            },
        ],
    )
    provider, _client = _provider(payload)
    with pytest.raises(ReturnProviderUnavailable) as excinfo:
        await provider.resolve_return_target(_request())
    assert "2 candidate shipped lines" in str(excinfo.value)


async def test_an_unshipped_line_is_never_a_target_even_when_the_variant_matches():
    payload = _targets_payload(
        order_lines=[(LINE_ITEM_GID, PRODUCT_GID, VARIANT_GID)],
        shipped=[{"id": FULFILLMENT_LINE_GID, "quantity": 0}],  # nothing of it shipped
    )
    provider, _client = _provider(payload)
    with pytest.raises(ReturnProviderUnavailable):
        await provider.resolve_return_target(_request())


async def test_no_reference_narrows_nothing_and_single_line_behaviour_is_kept():
    payload = _targets_payload(
        order_lines=[(LINE_ITEM_GID, PRODUCT_GID, VARIANT_GID)],
        shipped=[{"id": FULFILLMENT_LINE_GID, "quantity": 1}],
    )
    provider, _client = _provider(payload)
    target = await provider.resolve_return_target(
        _request(provider_product_reference=None, provider_variant_reference=None)
    )
    assert target.provider_line_reference == FULFILLMENT_LINE_GID


async def test_the_pre_14c7e_product_id_clue_still_narrows():
    provider, _client = _provider(_TWO_LINE)
    target = await provider.resolve_return_target(
        _request(provider_product_reference=None, provider_variant_reference=None)
    )
    # product_id is itself a canonical Product GID, so it narrows exactly as it did before.
    assert target.provider_line_reference == FULFILLMENT_LINE_GID


@pytest.mark.parametrize(
    "variant_reference",
    [PRODUCT_GID, GOOGLE_PRODUCT_ID, ORDER_GID, FULFILLMENT_LINE_GID, "gid://shopify/ProductVariant/"],
)
async def test_case_q_a_non_variant_id_in_the_variant_slot_is_refused_before_any_read(
    variant_reference,
):
    provider, client = _provider(_TWO_LINE)
    with pytest.raises(ReturnError) as excinfo:
        await provider.resolve_return_target(_request(provider_variant_reference=variant_reference))
    assert "is not a canonical Shopify global id of the form gid://shopify/ProductVariant/<id>" in str(excinfo.value)
    assert excinfo.value.status_code == 400
    assert client.reads == []  # refused on RESOLVE's own data, before asking Shopify anything


@pytest.mark.parametrize("product_reference", [VARIANT_GID, ORDER_GID, GOOGLE_PRODUCT_ID])
async def test_a_non_product_id_in_the_product_slot_is_refused_before_any_read(product_reference):
    provider, client = _provider(_TWO_LINE)
    with pytest.raises(ReturnError) as excinfo:
        await provider.resolve_return_target(
            _request(provider_product_reference=product_reference)
        )
    assert "is not a canonical Shopify global id of the form gid://shopify/Product/<id>" in str(excinfo.value)
    assert client.reads == []


async def test_an_unlinked_order_is_still_refused_before_identity_is_even_considered():
    provider, client = _provider(_TWO_LINE)
    with pytest.raises(ReturnProviderUnavailable) as excinfo:
        await provider.resolve_return_target(_request(provider_order_reference=None))
    assert "No canonical Shopify order is linked" in str(excinfo.value)
    assert client.reads == []


async def test_case_r_a_fulfillment_order_line_id_is_still_refused_as_a_target():
    payload = _targets_payload(
        order_lines=[(LINE_ITEM_GID, PRODUCT_GID, VARIANT_GID)],
        shipped=[{"id": FULFILLMENT_ORDER_LINE_GID, "quantity": 1}],
    )
    provider, _client = _provider(payload)
    with pytest.raises((ReturnError, ReturnProviderUnavailable)) as excinfo:
        await provider.resolve_return_target(_request())
    message = str(excinfo.value)
    assert (
        "FulfillmentOrderLineItem" in message  # refused by name, by _validated_fulfillment_line_gid
        or "no shipped line" in message
    )


async def test_case_s_the_returned_quantity_is_what_shopify_said_shipped():
    payload = _targets_payload(
        order_lines=[(LINE_ITEM_GID, PRODUCT_GID, VARIANT_GID)],
        shipped=[{"id": FULFILLMENT_LINE_GID, "quantity": 3}],
    )
    provider, _client = _provider(payload)
    target = await provider.resolve_return_target(_request())
    assert target.provider_quantity == target.line_quantity == 3
    assert target.provider_reason == "DEFECTIVE"


async def test_a_quantity_above_the_shipped_line_is_still_refused_before_submission():
    """The shipped-line ceiling from 14C.4 keeps binding after variant narrowing."""
    payload = _targets_payload(
        order_lines=[(LINE_ITEM_GID, PRODUCT_GID, VARIANT_GID)],
        shipped=[{"id": FULFILLMENT_LINE_GID, "quantity": 1}],
    )
    provider, _client = _provider(payload)
    target = await provider.resolve_return_target(_request())
    overreaching = ReturnTarget(
        provider_order_reference=target.provider_order_reference,
        provider_line_reference=target.provider_line_reference,
        provider_quantity=2,
        provider_reason=target.provider_reason,
        line_quantity=1,
    )
    with pytest.raises(ReturnError) as excinfo:
        await provider.request_return(_request(), overreaching)
    assert "exceeds the 1 units" in str(excinfo.value)


async def test_case_t_remaining_quantity_never_enters_the_return_providers_code():
    """
    14C.2 measured that `remainingQuantity` counts units AWAITING fulfilment and reads 0 on a
    shipped line, so the file explains that at length — in prose. What must be absent is the CODE:
    no read of it, no field asked for it, no ceiling computed from it.
    """
    path = BACKEND_ROOT / "app/tools/shopify/return_provider.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    tree.body = [
        node
        for node in tree.body
        if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant))
    ]
    code = ast.unparse(tree)
    for forbidden in ("remainingQuantity", "remaining_quantity", "remainingToBeFulfilled"):
        assert forbidden not in code, forbidden
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            assert node.attr != "remaining_quantity"
    # And the one read that feeds a target does not even ask Shopify for it.
    assert "remainingQuantity" not in _RETURN_TARGETS_QUERY


async def test_case_v_target_resolution_reads_once_and_cannot_submit_anything():
    provider, client = _provider(_TWO_LINE)
    await provider.resolve_return_target(_request())
    assert client.reads == [ORDER_GID]
    # The stand-in client has no transport and no execute_graphql at all: nothing could be sent.
    assert not hasattr(client, "execute_graphql")
    assert not hasattr(client, "obtain_access_token")


def test_the_return_targets_read_now_asks_shopify_for_the_line_variant():
    assert "variant { id }" in _RETURN_TARGETS_QUERY
    assert "fulfillmentLineItems" in _RETURN_TARGETS_QUERY
    assert "fulfillmentOrders" not in _RETURN_TARGETS_QUERY
    assert not _RETURN_TARGETS_QUERY.lstrip().startswith("mutation")


def test_the_order_line_reference_carries_both_shopify_ids_and_neither_invents_one():
    line = ShopifyOrderLineRef.from_node(
        {"id": LINE_ITEM_GID, "product": {"id": PRODUCT_GID}, "variant": {"id": VARIANT_GID}}
    )
    assert (line.order_line_item_id, line.product_id, line.variant_id) == (
        LINE_ITEM_GID,
        PRODUCT_GID,
        VARIANT_GID,
    )
    absent = ShopifyOrderLineRef.from_node({"id": LINE_ITEM_GID, "product": None, "variant": None})
    assert absent.product_id is None and absent.variant_id is None
    assert ShopifyOrderLineRef.from_node({"title": "no id"}) is None


def test_the_return_contract_gained_identity_slots_but_no_target_slots():
    import dataclasses

    base_fields = {f.name for f in dataclasses.fields(ShopifyReturnRequest)}
    assert {"provider_product_reference", "provider_variant_reference"} <= base_fields
    # What names a RETURN TARGET is still resolved from Shopify, never carried on a request.
    for forbidden in (
        "provider_line_reference",
        "provider_quantity",
        "fulfillment_line_item_id",
        "client",
        "answer",
    ):
        assert forbidden not in base_fields


def test_the_provider_request_assembly_cannot_be_reached_from_a_client_body():
    """`_return_request` reads the persisted row's identity columns and nothing else."""
    source = inspect.getsource(missions_module._return_request)
    assert "order.shopify_product_gid" in source
    assert "order.shopify_variant_gid" in source
    assert "payload" not in source
    assert "request.json" not in source


# --------------------------------------------------------------------------- #
# W / X / Y / Z and the structural guarantees
# --------------------------------------------------------------------------- #
def test_deployed_defaults_are_unchanged():
    assert Settings.model_fields["product_search_provider"].default == "serpapi"
    assert Settings.model_fields["return_provider"].default == "none"
    assert get_return_provider().name == "none"
    assert get_return_provider("none").name == "none"


def test_the_clarification_request_carries_no_merchant_identity_input():
    assert set(ClarifyVariantRequest.model_fields) == {"product_id", "answer"}
    assert ClarifyVariantRequest.model_config.get("extra") == "ignore"
    with pytest.raises(ValidationError):
        ClarifyVariantRequest(product_id=PRODUCT_GID)  # an answer is required
    with pytest.raises(ValidationError):
        ClarifyVariantRequest(product_id=PRODUCT_GID, answer="")


def test_the_clarification_endpoint_writes_no_order_return_or_payment_state():
    source = inspect.getsource(missions_module.clarify_shopping_variant)
    for forbidden in (
        "ResolveOrderRecord",
        "ReturnRecord",
        "OrderIssueRecord",
        "provider_order_reference",
        "returnCreate",
        "orderCreate",
        "draftOrderCreate",
        "link_order_to_shopify_order",
        "read_return_targets",
        "execute_graphql",
        "create_test_order",
        "db.add(",
        "RefundRecord",
    ):
        assert forbidden not in source, forbidden


def test_no_new_schema_columns_were_needed_or_added():
    from app.database import _SCHEMA_UPGRADES

    assert {name for name, _ddl in _SCHEMA_UPGRADES["resolve_orders"]} == {
        "provider_order_reference",
        "shopify_product_gid",
        "shopify_variant_gid",
        "shopify_store_domain",
        # M22 later committed the delivery/review facts (all nullable, additive); the pin stays exact.
        "delivered_at",
        "delivery_mode",
        # M22 P3 demo delivery: the persisted rung of the packed->delivered ladder.
        "delivery_stage",
        "delivery_stage_at",
        "shipping_address",
        "shipping_address_confirmed_at",
        "review_rating",
        "review_body",
        "review_created_at",
        "review_updated_at",
    }
    columns = {column.name for column in ResolveOrderRecord.__table__.columns}
    assert "variant_clarification" not in columns
    assert "requires_variant_clarification" not in columns
    # Clarification state lives in the mission's own JSON intent, where discovery results already live.
    assert "discovery_query" in ShoppingIntent.model_fields
    assert "discovery_query" not in columns


def test_the_discovery_provider_abstraction_gained_no_shopify_specific_argument():
    parameters = inspect.signature(ProductDiscoveryTool.search).parameters
    assert list(parameters) == [
        "self", "query", "budget", "currency", "preferences", "brand", "size_or_variant", "limit",
    ]
    from app.tools.product_search.base import ProductSearchProvider

    assert list(inspect.signature(ProductSearchProvider.search).parameters) == [
        "self", "query", "max_price", "currency", "preferences", "brand", "size_or_variant", "limit",
    ]
    assert ShopifyProductSearchProvider.name == SHOPIFY_SOURCE


async def test_a_shopify_failure_during_clarification_reaches_the_provider_error_path():
    class Broken:
        async def read_catalog(self, **kwargs):
            raise ShopifyError("Shopify could not be reached", status_code=503)

    provider = ShopifyProductSearchProvider(client=Broken())
    with pytest.raises(ProductSearchError) as excinfo:
        await provider.search("shampoo", size_or_variant="360ml")
    assert excinfo.value.status_code == 503


async def test_clarification_never_selects_a_variant_for_a_single_variant_product():
    # "Default Title" is what this store actually publishes; a single available variant resolves
    # without any question being asked at all.
    [result] = await _search(
        _catalog(_product(_variant(VARIANT_GID, options=(("Title", "Default Title"),))))
    )
    assert result.requires_variant_clarification is False
    assert result.variant_clarification is None
    assert result.shopify_variant_gid == VARIANT_GID


# --------------------------------------------------------------------------- #
# The client side of the round-trip (same mount convention as the 14C.7B scan)
# --------------------------------------------------------------------------- #
def _frontend_root() -> pathlib.Path | None:
    import os

    configured = os.environ.get("FRONTEND_ROOT")
    candidates = []
    if configured:
        candidates.append(pathlib.Path(configured))
    candidates.append(BACKEND_ROOT.parent / "frontend")
    return next((path for path in candidates if path.is_dir()), None)


def test_the_client_answers_with_a_value_and_a_handle_and_nothing_else():
    """
    What the browser is allowed to send for a clarification: which discovered product, and the
    Shopify option value the buyer chose. Never a variant id, never a price.
    """
    root = _frontend_root()
    if root is None:
        pytest.skip(
            "the frontend tree is not visible in this container — run with "
            "-v <repo>/frontend:/frontend:ro -e FRONTEND_ROOT=/frontend to execute this scan"
        )
    page = (root / "app" / "workspace" / "page.tsx").read_text(encoding="utf-8")
    start = page.index("const handleClarifyVariant")
    body = page[start : page.index("const handleOpenCheckout")]

    assert "shopping/clarify-variant" in body
    assert "JSON.stringify({ product_id: product.product_id, answer })" in body
    for forbidden in ("shopify_variant_gid", "shopify_product_gid", "requires_variant_clarification"):
        assert forbidden not in body, forbidden

    results = (
        root / "components" / "workspace" / "ProductResults.tsx"
    ).read_text(encoding="utf-8")
    # An unresolved variant renders no Select affordance and only Shopify's own values as choices.
    assert "needsVariant ? (" in results
    assert "p.variant_clarification?.options" in results
    assert "onClarifyVariant?.(p, value)" in results
