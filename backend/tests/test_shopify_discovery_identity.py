"""
Tests for Task 14C.7D — Shopify-native discovery and authoritative Product/Variant identity.

The question this file answers is narrow and worth stating exactly: when a product enters RESOLVE
from the connected Shopify store, does Shopify's own identity survive all the way to the order row —
and when a product enters from anywhere else, does it stay honestly identity-free?

Everything runs against the real normalisation code with an injected stand-in for the Shopify
client, so no network is touched and no Shopify product, order, draft order or return is created,
changed, or linked. The stand-in catalogs are shaped the way Shopify actually answers (global ids,
`availableForSale`, `selectedOptions`, a null SKU, the store's own currency), and the cases that
matter are the ones where a tempting shortcut — take the first variant, match the title, convert
the price — would have been wrong.
"""
from __future__ import annotations

import ast
import inspect
import pathlib
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.config import Settings, get_settings
from app.models.product import SHOPIFY_SOURCE, ProductResult, shopify_identity_problem
from app.models.db_models import ResolveOrderRecord
from app.routers import missions as missions_module
from app.tools.product_discovery import ProductDiscoveryTool
from app.tools.product_search import get_product_search_provider
from app.tools.product_search import shopify_provider as shopify_provider_module
from app.tools.product_search.base import ProductSearchError
from app.tools.product_search.serpapi_provider import SerpApiProductSearchProvider
from app.tools.product_search.shopify_provider import ShopifyProductSearchProvider
from app.tools.returns import get_return_provider
from app.tools.returns.base import ReturnProviderUnavailable, ReturnRequest
from app.tools.shopify.client import ShopifyReadOnlyClient
from app.tools.shopify.errors import ShopifyError, ShopifyRateLimitError
from app.tools.shopify.models import (
    ShopifyCatalog,
    ShopifyCatalogProduct,
    ShopifyCatalogVariant,
)
from app.tools.shopify.return_provider import ShopifyReturnProvider
from app.tools.shopify.sanitizer import reset_registered_secrets

# `_register`, `_checkout_signature` and the two client fixtures are re-used from the refund suite;
# `install_mock_discovery` and `sample_products` are conftest fixtures and arrive by argument name.
from tests.test_refund_execution import (  # noqa: F401
    _checkout_signature,
    _register,
    mock_razorpay,
    restart_env,
)

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
STORE = "resolve-commerce-lab.myshopify.com"
PRODUCT_GID = "gid://shopify/Product/10622982422815"
VARIANT_GID = "gid://shopify/ProductVariant/52923834990879"
OTHER_VARIANT_GID = "gid://shopify/ProductVariant/52923835973919"
GOOGLE_PRODUCT_ID = "1462412133584222299"
PROMPT = "I want the shampoo from my own store under ₹600."


@pytest.fixture(autouse=True)
def _clean_secrets():
    reset_registered_secrets()
    yield
    reset_registered_secrets()


# --------------------------------------------------------------------------- #
# Stand-ins shaped like Shopify's real answers
# --------------------------------------------------------------------------- #
def _variant(
    gid: str,
    *,
    price: str = "410.00",
    available: bool = True,
    options: tuple[tuple[str, str], ...] = (("Title", "Default Title"),),
    sku: str | None = None,
) -> ShopifyCatalogVariant:
    return ShopifyCatalogVariant(
        variant_id=gid,
        product_id=PRODUCT_GID,
        title="Head & Shoulders Anti-Hairfall Shampoo 360ml",
        handle="head-shoulders-anti-hairfall-shampoo-360ml",
        status="ACTIVE",
        sku=sku,
        price=Decimal(price),
        currency="USD",
        available_for_sale=available,
        selected_options=options,
        image_url=None,
    )


def _product(
    *variants: ShopifyCatalogVariant,
    product_id: str = PRODUCT_GID,
    title: str = "Head & Shoulders Anti-Hairfall Shampoo 360ml",
) -> ShopifyCatalogProduct:
    return ShopifyCatalogProduct(
        product_id=product_id,
        title=title,
        handle="head-shoulders-anti-hairfall-shampoo-360ml",
        status="ACTIVE",
        variants=tuple(variants),
    )


def _catalog(*products: ShopifyCatalogProduct, currency: str | None = "USD") -> ShopifyCatalog:
    return ShopifyCatalog(
        store_domain=STORE,
        shop_name="RESOLVE Commerce Lab",
        currency=currency,
        products=tuple(products),
    )


class CatalogClient:
    """Stands where the read-only Shopify client stands, recording every question asked of it."""

    def __init__(self, catalog: ShopifyCatalog | None = None, error: Exception | None = None):
        self.catalog = (
            catalog if catalog is not None else _catalog(_product(_variant(VARIANT_GID)))
        )
        self.error = error
        self.asks: list[dict] = []
        self.store_domain = STORE

    async def read_catalog(self, **kwargs) -> ShopifyCatalog:
        self.asks.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.catalog


def _provider(catalog: ShopifyCatalog | None = None) -> tuple[ShopifyProductSearchProvider, CatalogClient]:
    client = CatalogClient(catalog)
    return ShopifyProductSearchProvider(client=client), client


async def _one(provider: ShopifyProductSearchProvider, **kwargs) -> ProductResult:
    results = await provider.search(**{"query": "shampoo", **kwargs})
    assert len(results) == 1, results
    return results[0]


# --------------------------------------------------------------------------- #
# 1. The provider's results carry Shopify's identity, and nothing else's
# --------------------------------------------------------------------------- #
async def test_shopify_result_carries_authoritative_product_and_variant_identity():
    provider, _client = _provider()
    result = await _one(provider)
    assert result.shopify_product_gid == PRODUCT_GID
    assert result.shopify_variant_gid == VARIANT_GID
    assert result.shopify_store_domain == STORE
    assert result.source == SHOPIFY_SOURCE
    # The selection handle is Shopify's own Product id — and the dedicated slots say so explicitly
    # rather than leaving a reader to infer a source from the shape of a string.
    assert result.product_id == PRODUCT_GID
    assert shopify_identity_problem(result) is None


async def test_price_and_currency_come_from_shopify_not_from_the_request():
    provider, _client = _provider()
    result = await _one(provider, currency="INR")
    assert result.extracted_price == Decimal("410.00")
    assert result.currency == "USD"  # the store's currency, untouched
    assert result.price == "USD 410.00"
    assert result.merchant == "RESOLVE Commerce Lab"  # display context only
    assert result.availability == "In stock"
    assert result.rating is None and result.review_count is None
    assert result.product_url is None  # Shopify published none; nothing is assembled


async def test_the_catalog_is_read_once_per_search_through_the_existing_client():
    provider, client = _provider()
    await provider.search("shampoo")
    assert len(client.asks) == 1
    assert client.asks[0]["search_query"] == "shampoo"
    assert set(client.asks[0]) == {"search_query", "first", "variants_per_product"}


# --------------------------------------------------------------------------- #
# 2. Variant selection: Shopify decides, RESOLVE does not guess
# --------------------------------------------------------------------------- #
async def test_case_a_single_available_variant_is_selected_deterministically():
    provider, _ = _provider()
    result = await _one(provider)
    assert result.shopify_variant_gid == VARIANT_GID
    assert result.requires_variant_clarification is False


async def test_case_b_an_explicitly_requested_variant_selects_that_variant():
    small = _variant(VARIANT_GID, options=(("Size", "360ml"),))
    large = _variant(OTHER_VARIANT_GID, options=(("Size", "1L"),), price="699.00")
    provider, _ = _provider(_catalog(_product(small, large)))
    result = await _one(provider, size_or_variant="1L")
    assert result.shopify_variant_gid == OTHER_VARIANT_GID
    assert result.extracted_price == Decimal("699.00")
    assert result.requires_variant_clarification is False


async def test_case_c_one_available_variant_among_unavailable_ones():
    provider, _ = _provider(
        _catalog(_product(_variant(VARIANT_GID, available=False), _variant(OTHER_VARIANT_GID)))
    )
    result = await _one(provider)
    assert result.shopify_variant_gid == OTHER_VARIANT_GID  # never the unavailable one


async def test_case_d_equally_valid_variants_are_left_ambiguous():
    provider, _ = _provider(
        _catalog(
            _product(
                _variant(VARIANT_GID, options=(("Size", "360ml"),)),
                _variant(OTHER_VARIANT_GID, options=(("Size", "750ml"),)),
            )
        )
    )
    result = await _one(provider)
    assert result.requires_variant_clarification is True
    assert result.shopify_variant_gid is None
    assert result.shopify_product_gid == PRODUCT_GID  # the product itself is still known
    # An ambiguous product is still in stock: the ambiguity is about WHICH variant, not availability.
    assert result.availability == "In stock"
    assert any("2 Shopify variants are available" in note for note in result.extensions)
    assert "no single Shopify variant has been identified" in shopify_identity_problem(result)


async def test_case_e_an_unavailable_requested_variant_is_not_silently_substituted():
    provider, _ = _provider(
        _catalog(
            _product(
                _variant(VARIANT_GID, available=False, options=(("Size", "360ml"),)),
                _variant(OTHER_VARIANT_GID, options=(("Size", "1L"),)),
            )
        )
    )
    result = await _one(provider, size_or_variant="360ml")
    assert result.shopify_variant_gid is None
    assert result.requires_variant_clarification is True
    assert any("nothing is substituted" in note for note in result.extensions)


async def test_case_f_an_unavailable_variant_is_never_selected():
    provider, _ = _provider(_catalog(_product(_variant(VARIANT_GID, available=False))))
    result = await _one(provider)
    assert result.shopify_variant_gid is None
    assert result.availability == "Out of stock"
    assert result.requires_variant_clarification is True


async def test_case_g_a_null_sku_still_carries_full_identity():
    # Measured on the live dev store: every variant's SKU is null. Identity is the GID.
    provider, _ = _provider(_catalog(_product(_variant(VARIANT_GID, sku=None))))
    result = await _one(provider)
    assert result.shopify_variant_gid == VARIANT_GID
    assert shopify_identity_problem(result) is None


async def test_a_populated_sku_can_select_a_variant():
    provider, _ = _provider(
        _catalog(
            _product(
                _variant(VARIANT_GID, sku="DHS-360"),
                _variant(OTHER_VARIANT_GID, sku="DHS-1L"),
            )
        )
    )
    result = await _one(provider, size_or_variant="dhs-1l")
    assert result.shopify_variant_gid == OTHER_VARIANT_GID


async def test_options_are_matched_against_variant_attributes_not_against_the_title():
    # The product title contains "360ml"; no variant option does. Title text is not a selector.
    provider, _ = _provider(_catalog(_product(_variant(VARIANT_GID, options=(("Scent", "Fresh"),)))))
    result = await _one(provider, size_or_variant="360ml")
    assert result.requires_variant_clarification is True
    assert result.shopify_variant_gid is None


async def test_two_equally_matching_variants_are_not_resolved_by_tie_break():
    provider, _ = _provider(
        _catalog(
            _product(
                _variant(VARIANT_GID, options=(("Size", "360ml"), ("Scent", "Fresh"))),
                _variant(OTHER_VARIANT_GID, options=(("Size", "360ml"), ("Scent", "Mint"))),
            )
        )
    )
    result = await _one(provider, size_or_variant="360ml")
    assert result.requires_variant_clarification is True
    assert any("match the requested options equally" in note for note in result.extensions)


# --------------------------------------------------------------------------- #
# 3. Money: no conversion, and no comparison across currencies
# --------------------------------------------------------------------------- #
async def test_a_budget_in_another_currency_does_not_silently_drop_or_keep_products():
    provider, _ = _provider()
    result = await _one(provider, max_price=Decimal("300"), currency="INR")
    # 410 USD is not comparable to a ₹300 budget, so the ceiling is not applied — and the result
    # says so, instead of quietly keeping an item the buyer may not have budgeted for.
    assert result.extracted_price == Decimal("410.00")
    assert any("budget not applied" in note for note in result.extensions)


async def test_a_budget_in_the_store_currency_is_applied_to_shopifs_own_price():
    provider, _ = _provider()
    within = await provider.search("shampoo", max_price=Decimal("500"), currency="USD")
    assert [result.extracted_price for result in within] == [Decimal("410.00")]
    beyond = await provider.search("shampoo", max_price=Decimal("100"), currency="USD")
    assert beyond == []


async def test_money_stays_decimal_and_exact():
    provider, _ = _provider()
    result = await _one(provider)
    assert isinstance(result.extracted_price, Decimal)
    assert str(result.extracted_price) == "410.00"


async def test_a_store_that_reports_no_currency_does_not_get_one_invented():
    provider, _ = _provider(_catalog(_product(_variant(VARIANT_GID)), currency=None))
    result = await _one(provider, currency="INR")
    assert result.price == "410.00"  # no symbol, no assumed code
    assert result.currency == "INR"  # the request context, visibly — never a silent conversion


# --------------------------------------------------------------------------- #
# 4. The search itself: structured intent, generic over any catalog
# --------------------------------------------------------------------------- #
async def test_the_intent_is_joined_into_one_deterministic_query():
    _provider_obj, client = _provider()
    provider = ShopifyProductSearchProvider(client=client)
    await provider.search(
        "shampoo", brand="Dove", preferences=["anti-hairfall", "Shampoo"], size_or_variant="360ml"
    )
    assert client.asks[0]["search_query"] == "Dove shampoo 360ml anti-hairfall"


async def test_an_empty_intent_reads_the_catalog_without_a_filter():
    _provider_obj, client = _provider()
    provider = ShopifyProductSearchProvider(client=client)
    await provider.search("   ")
    assert client.asks[0]["search_query"] is None


async def test_the_provider_is_not_specific_to_any_particular_product():
    earbuds = _product(
        _variant("gid://shopify/ProductVariant/1", price="2299.00"),
        product_id="gid://shopify/Product/1",
        title="OnePlus Wireless Earbuds",
    )
    provider, _ = _provider(_catalog(earbuds))
    result = await _one(provider, query="earbuds")
    assert result.title == "OnePlus Wireless Earbuds"
    assert result.shopify_product_gid == "gid://shopify/Product/1"
    assert result.extracted_price == Decimal("2299.00")


async def test_the_result_list_is_bounded_by_the_requested_limit():
    products = [
        _product(
            _variant(f"gid://shopify/ProductVariant/{index}"),
            product_id=f"gid://shopify/Product/{index}",
            title=f"Product {index}",
        )
        for index in range(6)
    ]
    provider, _ = _provider(_catalog(*products))
    results = await provider.search("product", limit=3)
    assert len(results) == 3


async def test_an_empty_catalog_is_an_empty_answer_not_a_failure():
    provider, _ = _provider(_catalog())
    assert await provider.search("nothing matches this") == []


def test_a_product_shopify_publishes_no_image_for_still_normalizes():
    """
    Measured live: this store answers `featuredImage: null` and `sku: null` for its products.
    A missing display field is not a missing product — and must not raise on the way in.
    """
    product = ShopifyCatalogProduct.from_node(
        {
            "id": PRODUCT_GID,
            "title": "Head & Shoulders Anti-Hairfall Shampoo 360ml",
            "handle": "head-shoulders-anti-hairfall-shampoo-360ml",
            "status": "ACTIVE",
            "featuredImage": None,
            "variants": {
                "edges": [
                    {
                        "node": {
                            "id": VARIANT_GID,
                            "sku": None,
                            "price": "410.00",
                            "availableForSale": True,
                            "selectedOptions": [{"name": "Title", "value": "Default Title"}],
                        }
                    }
                ]
            },
        },
        currency="USD",
    )
    assert product is not None
    assert product.variants[0].variant_id == VARIANT_GID
    assert product.variants[0].image_url is None
    assert product.variants[0].sku is None
    assert product.variants[0].price == Decimal("410.00")


# --------------------------------------------------------------------------- #
# 5. Failure is reported, never dressed up as "the merchant has nothing"
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "error,expected_status",
    [
        (ShopifyRateLimitError(), 429),
        (ShopifyError("Shopify could not be reached", status_code=503), 503),
    ],
)
async def test_shopify_failures_become_product_search_errors_with_their_status(error, expected_status):
    provider = ShopifyProductSearchProvider(client=CatalogClient(error=error))
    with pytest.raises(ProductSearchError) as excinfo:
        await provider.search("shampoo")
    assert excinfo.value.status_code == expected_status


async def test_an_unexpected_failure_is_reported_and_never_fakes_results():
    class Exploding:
        store_domain = STORE

        async def read_catalog(self, **kwargs):
            raise RuntimeError("boom")

    provider = ShopifyProductSearchProvider(client=Exploding())
    with pytest.raises(ProductSearchError) as excinfo:
        await provider.search("shampoo")
    assert excinfo.value.status_code == 502
    assert "RuntimeError" in excinfo.value.message
    assert "boom" not in excinfo.value.message  # the underlying text is not relayed


async def test_the_discovery_tool_passes_shopify_results_through_unchanged():
    provider, _ = _provider()
    tool = ProductDiscoveryTool(provider=provider)
    [result] = await tool.search("shampoo", budget=Decimal("999"), currency="USD")
    assert result.shopify_variant_gid == VARIANT_GID
    assert result.source == SHOPIFY_SOURCE


# --------------------------------------------------------------------------- #
# 6. Catalog confusion: the two identity systems cannot impersonate each other
# --------------------------------------------------------------------------- #
def test_a_google_shopping_result_has_no_shopify_identity_at_all():
    normalized = SerpApiProductSearchProvider(api_key="test-key").normalize_item(
        {
            "title": "Head & Shoulders Anti-hairfall Shampoo 360ml",
            "product_id": GOOGLE_PRODUCT_ID,
            "price": "₹410.00",
            "extracted_price": 410.0,
            "source": "TabletBox",
            "link": "https://example.test/product",
        }
    )
    assert normalized is not None
    assert normalized.source == "google_shopping"
    assert normalized.shopify_product_gid is None
    assert normalized.shopify_variant_gid is None
    assert normalized.shopify_store_domain is None
    assert normalized.requires_variant_clarification is False
    # An absent identity is the CORRECT answer for this source, not a defect.
    assert shopify_identity_problem(normalized) is None


def test_a_google_shopping_product_id_cannot_pass_as_a_shopify_product_gid():
    forged = ProductResult(
        title="T",
        source=SHOPIFY_SOURCE,
        shopify_product_gid=GOOGLE_PRODUCT_ID,
        shopify_variant_gid=VARIANT_GID,
        shopify_store_domain=STORE,
    )
    assert "not a canonical Shopify Product global id" in shopify_identity_problem(forged)


@pytest.mark.parametrize(
    "product_gid,variant_gid,domain",
    [
        (None, VARIANT_GID, STORE),
        (PRODUCT_GID, None, STORE),
        (PRODUCT_GID, VARIANT_GID, None),
        (PRODUCT_GID, VARIANT_GID, "   "),
        (PRODUCT_GID, PRODUCT_GID, STORE),  # a product id is not a variant id
        (VARIANT_GID, VARIANT_GID, STORE),  # a variant id is not a product id
        ("gid://shopify/Order/123", VARIANT_GID, STORE),
        ("gid://shopify/Product/", VARIANT_GID, STORE),
        ("gid://shopify/Product/abc", VARIANT_GID, STORE),
    ],
)
def test_incomplete_or_wrongly_shaped_shopify_identity_is_always_caught(
    product_gid, variant_gid, domain
):
    result = ProductResult(
        title="T",
        source=SHOPIFY_SOURCE,
        shopify_product_gid=product_gid,
        shopify_variant_gid=variant_gid,
        shopify_store_domain=domain,
    )
    assert shopify_identity_problem(result) is not None


def test_a_non_shopify_result_cannot_carry_shopify_identity():
    forged = ProductResult(
        title="T", product_id=PRODUCT_GID, source="google_shopping", shopify_product_gid=PRODUCT_GID
    )
    assert "non-Shopify result" in shopify_identity_problem(forged)


def test_the_serpapi_position_fallback_is_never_mistaken_for_shopify_identity():
    # Measured in 14C.7C: with no product_id, SerpApi falls back to the result's position index.
    positional = SerpApiProductSearchProvider(api_key="k").normalize_item(
        {"title": "Something", "position": 2}
    )
    assert positional.product_id == "2"
    assert positional.shopify_product_gid is None
    assert shopify_identity_problem(positional) is None


def test_no_shopify_identity_is_ever_derived_by_matching():
    """
    The identity slots have exactly two writers — the client's normalization of Shopify's answer
    and the provider copying it — and neither of them is a matcher. The banned names are the actual
    proximity-matching APIs, not English words a docstring could legitimately use.
    """
    for path in (
        BACKEND_ROOT / "app/tools/product_search/shopify_provider.py",
        BACKEND_ROOT / "app/tools/shopify/models.py",
    ):
        source = path.read_text(encoding="utf-8")
        for banned in (
            "difflib",
            "SequenceMatcher",
            "get_close_matches",
            "rapidfuzz",
            "fuzzywuzzy",
            "Levenshtein",
        ):
            assert banned not in source, (path.name, banned)
    # A GID is assembled nowhere: it is always Shopify's own string.
    provider_source = inspect.getsource(shopify_provider_module)
    assert 'f"gid://' not in provider_source
    assert "gid://shopify/Product/\" +" not in provider_source
    assert "gid://shopify/" not in provider_source  # the literal never appears at all


# --------------------------------------------------------------------------- #
# 7. Factory and laziness
# --------------------------------------------------------------------------- #
def test_the_factory_selects_the_shopify_provider():
    assert isinstance(get_product_search_provider("shopify"), ShopifyProductSearchProvider)


def test_serpapi_and_mock_selection_are_unchanged():
    assert isinstance(
        get_product_search_provider("serpapi", api_key="abc"), SerpApiProductSearchProvider
    )
    assert type(get_product_search_provider("mock")).__name__ == "MockProductSearchProvider"


def test_an_unknown_provider_still_fails_safely_and_names_the_options():
    with pytest.raises(ValueError) as excinfo:
        get_product_search_provider("no_such_provider")
    assert "no_such_provider" in str(excinfo.value)


def test_selecting_serpapi_or_mock_never_builds_a_shopify_client(monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("a non-Shopify deployment must not construct a Shopify client")

    monkeypatch.setattr("app.tools.shopify.get_shopify_client", explode)
    # Since 14C.7O there is a second factory, and it is the one that hands out a write capability.
    # A discovery path that never touches returns must not reach it either.
    monkeypatch.setattr("app.tools.shopify.get_shopify_clients", explode)
    assert isinstance(
        get_product_search_provider("serpapi", api_key="abc"), SerpApiProductSearchProvider
    )
    assert type(get_product_search_provider("mock")).__name__ == "MockProductSearchProvider"


def test_shopify_is_not_the_configured_default():
    # The declared default, not this suite's hermetic PRODUCT_SEARCH_PROVIDER=mock override:
    # selecting Shopify must remain an opt-in a deployment states out loud.
    assert Settings.model_fields["product_search_provider"].default == "serpapi"
    assert type(get_product_search_provider()).__name__ == "MockProductSearchProvider"


def test_the_shopify_provider_module_ships_no_mutation_document():
    source = inspect.getsource(shopify_provider_module)
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert not node.value.lstrip().startswith("mutation")
    for banned in (
        "orderCreate", "draftOrderCreate", "returnCreate", "fulfillmentCreate", "mutation",
        "productUpdate", "inventorySetQuantities", "execute_graphql", "graphql",
    ):
        assert banned not in source, banned


def test_the_catalog_read_ships_no_mutation_document():
    """The one new client method holds one query document, and it is a read."""
    source = inspect.getsource(shopify_provider_module)  # provider sees no GraphQL at all
    assert "query " not in source
    module = inspect.getmodule(ShopifyReadOnlyClient)
    constants = {
        node.value.value
        for node in ast.walk(ast.parse(inspect.getsource(module)))
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
        and node.value.value.lstrip().startswith("query ")
    }
    assert any("ResolveReadCatalog" in text for text in constants)
    assert not [text for text in constants if text.lstrip().startswith("mutation")]


# --------------------------------------------------------------------------- #
# 8. Selection stays server-authoritative over HTTP
# --------------------------------------------------------------------------- #
def _shopify_result(**overrides) -> ProductResult:
    base = {
        "product_id": PRODUCT_GID,
        "title": "Head & Shoulders Anti-Hairfall Shampoo 360ml",
        "price": "INR 410.00",
        "extracted_price": Decimal("410.00"),
        "currency": "INR",
        "merchant": "RESOLVE Commerce Lab",
        "source": SHOPIFY_SOURCE,
        "shopify_product_gid": PRODUCT_GID,
        "shopify_variant_gid": VARIANT_GID,
        "shopify_store_domain": STORE,
    }
    base.update(overrides)
    return ProductResult(**base)


async def _mission_with(client: AsyncClient, install_mock_discovery, products, email: str) -> str:
    install_mock_discovery(sample_results=products)
    await _register(client, "Shopify Shopper", email)
    response = await client.post("/missions/shopping", json={"prompt": PROMPT})
    assert response.status_code == 200, response.text
    return response.json()["mission_id"]


async def test_selection_materialises_the_servers_own_shopify_identity(restart_env, install_mock_discovery):
    make_client, _test_session = restart_env
    async with make_client() as client:
        mission_id = await _mission_with(
            client, install_mock_discovery, [_shopify_result()], "sel@shop.test"
        )
        response = await client.post(
            f"/missions/{mission_id}/shopping/select-product", json={"product_id": PRODUCT_GID}
        )
        assert response.status_code == 200, response.text
        product = response.json()["selected_product"]
        assert product["shopify_product_gid"] == PRODUCT_GID
        assert product["shopify_variant_gid"] == VARIANT_GID
        assert product["shopify_store_domain"] == STORE


async def test_the_client_cannot_override_shopify_identity_or_price_at_selection(
    restart_env, install_mock_discovery
):
    make_client, _test_session = restart_env
    async with make_client() as client:
        mission_id = await _mission_with(
            client, install_mock_discovery, [_shopify_result()], "ovr@shop.test"
        )
        response = await client.post(
            f"/missions/{mission_id}/shopping/select-product",
            json={
                "product_id": PRODUCT_GID,
                "shopify_product_gid": "gid://shopify/Product/9999999",
                "shopify_variant_gid": "gid://shopify/ProductVariant/9999999",
                "shopify_store_domain": "attacker.example",
                "price": "INR 0.01",
                "currency": "USD",
                "merchant": "Nobody",
                "title": "Something else entirely",
            },
        )
        assert response.status_code == 200, response.text
        product = response.json()["selected_product"]
        assert product["shopify_product_gid"] == PRODUCT_GID
        assert product["shopify_variant_gid"] == VARIANT_GID
        assert product["shopify_store_domain"] == STORE
        assert product["extracted_price"] == "410.00"
        assert product["currency"] == "INR"
        assert product["merchant"] == "RESOLVE Commerce Lab"
        assert "Head & Shoulders" in product["title"]


async def test_selection_refuses_a_result_whose_variant_shopify_never_named(
    restart_env, install_mock_discovery
):
    make_client, _test_session = restart_env
    ambiguous = _shopify_result(shopify_variant_gid=None, requires_variant_clarification=True)
    async with make_client() as client:
        mission_id = await _mission_with(
            client, install_mock_discovery, [ambiguous], "amb@shop.test"
        )
        response = await client.post(
            f"/missions/{mission_id}/shopping/select-product", json={"product_id": PRODUCT_GID}
        )
        assert response.status_code == 400
        detail = response.json()["detail"]
        assert "Shopify identity is unresolved" in detail
        assert "no single Shopify variant has been identified" in detail
        # Nothing was persisted: checkout still refuses, which is the observable proof.
        checkout = await client.get(f"/missions/{mission_id}/shopping/checkout")
        assert checkout.status_code == 400
        assert "Select a product" in checkout.json()["detail"]


async def test_a_forged_shopify_identity_in_the_discovery_list_is_refused_at_selection(
    restart_env, install_mock_discovery
):
    make_client, _test_session = restart_env
    forged = _shopify_result(shopify_product_gid=GOOGLE_PRODUCT_ID)
    async with make_client() as client:
        mission_id = await _mission_with(
            client, install_mock_discovery, [forged], "frg@shop.test"
        )
        response = await client.post(
            f"/missions/{mission_id}/shopping/select-product", json={"product_id": PRODUCT_GID}
        )
        assert response.status_code == 400
        assert "is not a canonical Shopify Product global id" in response.json()["detail"]


async def test_serpapi_selection_still_works_and_grows_no_identity(
    restart_env, install_mock_discovery, sample_products
):
    make_client, _test_session = restart_env
    async with make_client() as client:
        mission_id = await _mission_with(
            client, install_mock_discovery, sample_products, "ser@shop.test"
        )
        response = await client.post(
            f"/missions/{mission_id}/shopping/select-product",
            json={"product_id": sample_products[0].product_id},
        )
        assert response.status_code == 200, response.text
        product = response.json()["selected_product"]
        assert product["source"] == "google_shopping"
        assert product["shopify_product_gid"] is None
        assert product["shopify_variant_gid"] is None
        assert product["shopify_store_domain"] is None


# --------------------------------------------------------------------------- #
# 9. Identity survives checkout, payment and the order row
# --------------------------------------------------------------------------- #
async def _pay_and_create_order(client: AsyncClient, mission_id: str) -> dict:
    assert (
        await client.post(f"/missions/{mission_id}/shopping/checkout/confirm")
    ).status_code == 200
    created = await client.post(
        f"/missions/{mission_id}/shopping/payment/create-order", json={}
    )
    assert created.status_code == 200, created.text
    razorpay_order_id = created.json()["payment"]["razorpay_order_id"]
    payment_id = "pay_TESTSHOP1"
    verified = await client.post(
        f"/missions/{mission_id}/shopping/payment/verify",
        json={
            "razorpay_order_id": razorpay_order_id,
            "razorpay_payment_id": payment_id,
            "razorpay_signature": _checkout_signature(razorpay_order_id, payment_id),
        },
    )
    assert verified.status_code == 200, verified.text
    order = await client.post(f"/missions/{mission_id}/order", json={})
    assert order.status_code == 200, order.text
    return order.json()


async def test_the_checkout_snapshot_keeps_the_shopify_identity(restart_env, install_mock_discovery):
    make_client, _test_session = restart_env
    async with make_client() as client:
        mission_id = await _mission_with(
            client, install_mock_discovery, [_shopify_result()], "chk@shop.test"
        )
        await client.post(
            f"/missions/{mission_id}/shopping/select-product", json={"product_id": PRODUCT_GID}
        )
        checkout = await client.get(f"/missions/{mission_id}/shopping/checkout")
        assert checkout.status_code == 200
        product = checkout.json()["checkout"]["product"]
        assert product["shopify_product_gid"] == PRODUCT_GID
        assert product["shopify_variant_gid"] == VARIANT_GID
        assert product["shopify_store_domain"] == STORE
        assert checkout.json()["checkout"]["currency"] == "INR"
        assert checkout.json()["checkout"]["subtotal"] == "410.00"


async def test_the_order_row_carries_identity_and_still_no_merchant_order(
    restart_env, install_mock_discovery, mock_razorpay
):
    make_client, test_session = restart_env
    async with make_client() as client:
        mission_id = await _mission_with(
            client, install_mock_discovery, [_shopify_result()], "ord@shop.test"
        )
        await client.post(
            f"/missions/{mission_id}/shopping/select-product", json={"product_id": PRODUCT_GID}
        )
        order = await _pay_and_create_order(client, mission_id)

    async with test_session() as session:
        row = await session.get(ResolveOrderRecord, order["order_id"])
    assert row.shopify_product_gid == PRODUCT_GID
    assert row.shopify_variant_gid == VARIANT_GID
    assert row.shopify_store_domain == STORE
    assert row.product_id == PRODUCT_GID
    assert row.provider_order_reference is None  # identity is not linkage, and nothing was linked
    assert str(row.amount) == "410.00" and row.currency == "INR"


async def test_the_order_row_ignores_a_tampered_body_about_shopify_identity(
    restart_env, install_mock_discovery, mock_razorpay
):
    make_client, test_session = restart_env
    async with make_client() as client:
        mission_id = await _mission_with(
            client, install_mock_discovery, [_shopify_result()], "tam@shop.test"
        )
        await client.post(
            f"/missions/{mission_id}/shopping/select-product", json={"product_id": PRODUCT_GID}
        )
        order = await _pay_and_create_order(client, mission_id)
        # A second call is idempotent and must not re-derive anything from a hostile body.
        repeated = await client.post(
            f"/missions/{mission_id}/order",
            json={
                "shopify_product_gid": "gid://shopify/Product/123456",
                "shopify_variant_gid": "gid://shopify/ProductVariant/123456",
                "shopify_store_domain": "attacker.example",
                "amount": "0.01",
            },
        )
        assert repeated.status_code == 200
        assert repeated.json()["order_id"] == order["order_id"]

    async with test_session() as session:
        row = await session.get(ResolveOrderRecord, order["order_id"])
    assert row.shopify_product_gid == PRODUCT_GID
    assert row.shopify_store_domain == STORE
    assert str(row.amount) == "410.00"


async def test_a_serpapi_order_row_keeps_the_shopify_columns_null(
    restart_env, install_mock_discovery, sample_products, mock_razorpay
):
    make_client, test_session = restart_env
    async with make_client() as client:
        mission_id = await _mission_with(
            client, install_mock_discovery, sample_products, "nul@shop.test"
        )
        await client.post(
            f"/missions/{mission_id}/shopping/select-product",
            json={"product_id": sample_products[0].product_id},
        )
        order = await _pay_and_create_order(client, mission_id)
    async with test_session() as session:
        row = await session.get(ResolveOrderRecord, order["order_id"])
    assert row.shopify_product_gid is None
    assert row.shopify_variant_gid is None
    assert row.shopify_store_domain is None
    assert row.provider_order_reference is None


async def test_payment_refuses_a_foreign_currency_instead_of_converting_it(
    restart_env, install_mock_discovery, mock_razorpay
):
    make_client, _test_session = restart_env
    usd_product = _shopify_result(currency="USD", price="USD 410.00")
    async with make_client() as client:
        mission_id = await _mission_with(
            client, install_mock_discovery, [usd_product], "usd@shop.test"
        )
        await client.post(
            f"/missions/{mission_id}/shopping/select-product", json={"product_id": PRODUCT_GID}
        )
        # Identity and checkout still work in the store's own currency...
        checkout = await client.get(f"/missions/{mission_id}/shopping/checkout")
        assert checkout.json()["checkout"]["currency"] == "USD"
        assert (
            await client.post(f"/missions/{mission_id}/shopping/checkout/confirm")
        ).status_code == 200
        # ...and payment stops rather than inventing a conversion.
        created = await client.post(
            f"/missions/{mission_id}/shopping/payment/create-order", json={}
        )
        detail = created.json()["detail"]
        assert created.status_code == 400
        assert "USD" in detail and "INR" in detail and "does not convert" in detail
        assert mock_razorpay["n"] == 0  # no Razorpay order was ever created
        # The refusal is not a state change: checkout is still confirmed in the store's own currency.
        after = await client.get(f"/missions/{mission_id}/shopping/checkout")
        assert after.status_code == 200
        assert after.json()["confirmed"] is True
        assert after.json()["checkout"]["currency"] == "USD"
        repeated = await client.post(
            f"/missions/{mission_id}/shopping/payment/create-order", json={}
        )
        assert repeated.status_code == 400
        assert mock_razorpay["n"] == 0


# --------------------------------------------------------------------------- #
# 10. Schema: additive, nullable, never backfilled
# --------------------------------------------------------------------------- #
def test_the_new_identity_columns_are_nullable_and_additive():
    columns = {column.name: column for column in ResolveOrderRecord.__table__.columns}
    for name in ("shopify_product_gid", "shopify_variant_gid", "shopify_store_domain"):
        assert name in columns
        assert columns[name].nullable is True
    # Identity is not a constraint: many orders may legitimately be the same Shopify product, and
    # the existing 1:1 merchant-order invariant is the only unique index this table keeps.
    assert {index.name for index in ResolveOrderRecord.__table__.indexes} == {
        "ix_resolve_orders_mission_id",
        "ix_resolve_orders_user_id",
        "ix_resolve_orders_status",
        "uq_resolve_orders_provider_order_reference",
    }


def test_the_startup_upgrade_list_declares_the_new_columns():
    from app.database import _SCHEMA_UPGRADES

    declared = {name for name, _ddl in _SCHEMA_UPGRADES["resolve_orders"]}
    assert {
        "provider_order_reference",
        "shopify_product_gid",
        "shopify_variant_gid",
        "shopify_store_domain",
    } <= declared


async def test_startup_migration_adds_the_columns_without_fabricating_identity(tmp_path, monkeypatch):
    from sqlalchemy import inspect as sqla_inspect
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    import app.database as database

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'mig.db'}", future=True)
    # An existing database exactly as it stood before 14C.7D, holding a Google Shopping order.
    async with engine.begin() as conn:
        await conn.execute(
            text(
                """
                CREATE TABLE resolve_orders (
                  order_id VARCHAR(64) NOT NULL PRIMARY KEY,
                  mission_id VARCHAR(64) NOT NULL,
                  user_id VARCHAR(64) NOT NULL,
                  product_id VARCHAR(128),
                  title VARCHAR(512) NOT NULL,
                  merchant VARCHAR(255),
                  product JSON NOT NULL,
                  amount NUMERIC(12, 2) NOT NULL,
                  currency VARCHAR(8) NOT NULL,
                  razorpay_order_id VARCHAR(64) NOT NULL,
                  razorpay_payment_id VARCHAR(64) NOT NULL,
                  provider_order_reference VARCHAR(128),
                  status VARCHAR(32) NOT NULL,
                  created_at TIMESTAMP,
                  updated_at TIMESTAMP
                )
                """
            )
        )
        await conn.execute(
            text(
                "INSERT INTO resolve_orders (order_id, mission_id, user_id, product_id, title,"
                " merchant, product, amount, currency, razorpay_order_id, razorpay_payment_id,"
                " status) VALUES ('order_old','mission_old','user_old', :product_id, :title,"
                " 'TabletBox', '{}', 410.00, 'INR', 'order_T', 'pay_T', 'order_confirmed')"
            ),
            {
                "product_id": GOOGLE_PRODUCT_ID,
                "title": "Head & Shoulders Anti-hairfall Shampoo 360ml",
            },
        )

    monkeypatch.setattr(database, "engine", engine)
    await database.init_models()

    def columns(sync_conn):
        return {column["name"] for column in sqla_inspect(sync_conn).get_columns("resolve_orders")}

    async with engine.begin() as conn:
        present = await conn.run_sync(columns)
        rows = (
            await conn.execute(
                text(
                    "SELECT order_id, product_id, shopify_product_gid, shopify_variant_gid,"
                    " shopify_store_domain, provider_order_reference FROM resolve_orders"
                )
            )
        ).fetchall()
    assert {
        "shopify_product_gid",
        "shopify_variant_gid",
        "shopify_store_domain",
    } <= present
    # The historical row survives intact and gains NO Shopify identity from the migration.
    assert rows == [("order_old", GOOGLE_PRODUCT_ID, None, None, None, None)]

    await database.init_models()  # idempotent: a second startup changes nothing
    async with engine.begin() as conn:
        again = await conn.run_sync(columns)
        rows_again = (
            await conn.execute(text("SELECT shopify_product_gid FROM resolve_orders"))
        ).fetchall()
    assert again == present
    assert rows_again == [(None,)]
    await engine.dispose()


def test_the_flow_has_no_backfill_path_for_historical_rows():
    """
    No code anywhere updates an existing order row's Shopify identity. Migration adds nullable
    columns; only `create_resolve_order` ever writes them, from a fresh server-side snapshot.
    """
    for relative in (
        "app/database.py",
        "app/tools/product_search/shopify_provider.py",
        "app/tools/shopify/client.py",
    ):
        source = (BACKEND_ROOT / relative).read_text(encoding="utf-8")
        assert "UPDATE resolve_orders" not in source.upper(), relative
    # The migration layer declares columns and nothing else: no SELECT-then-write over order rows.
    migration_source = (BACKEND_ROOT / "app/database.py").read_text(encoding="utf-8")
    assert "shopify_product_gid" in migration_source  # the column is added
    assert ".shopify_product_gid" not in migration_source  # but never written per-row


def test_no_module_updates_resolve_order_rows_at_all():
    offenders = []
    for path in (BACKEND_ROOT / "app").rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if getattr(node.func, "attr", None) != "update":
                continue
            argument = node.args[0] if node.args else None
            if getattr(argument, "attr", None) == "ResolveOrderRecord":
                offenders.append(path.name)
    assert offenders == []


# --------------------------------------------------------------------------- #
# 11. The return provider's boundary is untouched by this task
# --------------------------------------------------------------------------- #
async def test_a_shopify_identity_without_a_merchant_order_still_cannot_be_returned():
    """
    The identity fields are the first two links of the chain and deliberately not the last two:

        Shopify Product GID -> Shopify Variant GID -> Shopify Order GID -> FulfillmentLineItem GID

    With a real Product GID on the request and no linked merchant order, the provider must refuse
    exactly as it always did — on the missing linkage — and read nothing at all.
    """
    request = ReturnRequest(
        mission_id="mission_x",
        issue_id="issue_x",
        order_id="order_x",
        return_id="ret_x",
        product_id=PRODUCT_GID,
        title="Head & Shoulders Anti-Hairfall Shampoo 360ml",
        merchant="RESOLVE Commerce Lab",
        provider_order_reference=None,
    )
    asks: list[str] = []

    class RecordingClient:
        store_domain = STORE

        async def read_catalog(self, **kwargs):
            asks.append("read_catalog")
            return _catalog()

        async def read_return_targets(self, order_gid):
            asks.append("read_return_targets")
            raise AssertionError("the provider must not read targets for an unlinked order")

    with pytest.raises(ReturnProviderUnavailable) as excinfo:
        await ShopifyReturnProvider(client=RecordingClient()).resolve_return_target(request)
    assert "No canonical Shopify order is linked" in str(excinfo.value)
    assert asks == []


def test_the_discovery_layer_touches_no_linkage_or_return_code():
    for relative in (
        "app/tools/product_search/shopify_provider.py",
        "app/tools/product_discovery.py",
        "app/models/product.py",
    ):
        source = (BACKEND_ROOT / relative).read_text(encoding="utf-8")
        for banned in (
            "link_order_to_shopify_order",
            "provider_order_reference",
            "returnCreate",
            "resolve_return_target",
            "fulfillment",
        ):
            assert banned not in source, (relative, banned)


def test_the_catalog_read_is_only_reachable_through_the_client():
    assert "read_catalog" in inspect.getsource(ShopifyReadOnlyClient)
    assert "read_catalog" not in inspect.getsource(missions_module)


def test_return_provider_configuration_is_still_the_untouched_default():
    assert get_settings().return_provider == "none"
    assert get_return_provider().name == "none"
